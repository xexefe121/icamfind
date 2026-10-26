"""Scene memory: detects when objects are put down, picked up or moved.

The approach is a dual-background static-object detector (in the spirit of Porikli et al.,
"Robust abandoned object detection using dual foregrounds", 2008), adapted to a home:

  * A short-term MOG2 model absorbs anything that stops moving within a fraction of a second,
    so its foreground is "what is moving right now".
  * A long-term reference background (our own float image) changes only through slow selective
    drift and explicit absorption after an event, so its foreground is "what differs from memory".
  * Pixels that differ from memory, are not moving, and are not covered by a person accumulate
    evidence over time. A region that stays stable for `stable_seconds` with no person or motion
    nearby is committed as an event and absorbed into the long-term background.

Each committed region is then explained:
  * overlaps a remembered object and now looks like what was underneath it  -> removed (picked up)
  * otherwise, boundary gradients say whether the new edges belong to the current frame
    (an object appeared -> placed) or to the old background (a pre-existing object left -> removed)
  * a newly placed object is re-identified against recently removed ones (ORB + colour + shape),
    so "keys picked up from the desk and put on the shelf" becomes one "moved" event.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import cv2 as cv
import numpy as np

from . import appearance as app
from .config import Config
from .detector import Detection, YoloxDetector
from .geometry import RoomGeometry


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@dataclass
class TrackedObject:
    id: str
    label: str
    local_label: str
    confidence: float
    box: tuple[int, int, int, int]
    zone: str
    map_xy: list[float] | None
    status: str  # present | removed
    first_seen: float
    last_seen: float
    appearance: np.ndarray  # BGR patch of the object (tight box)
    under: np.ndarray  # BGR patch of what was there before the object
    mask: np.ndarray  # uint8 mask of the object inside its box
    essential: bool = False
    removed_at: float | None = None
    sig: app.Signature | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.sig is None:
            self.sig = app.signature(self.appearance, self.mask)

    def meta(self) -> dict:
        return {
            "id": self.id, "label": self.label, "local_label": self.local_label,
            "confidence": round(self.confidence, 3), "box": list(map(int, self.box)), "zone": self.zone,
            "map_xy": self.map_xy, "status": self.status, "first_seen": self.first_seen,
            "last_seen": self.last_seen, "essential": self.essential, "removed_at": self.removed_at,
        }


@dataclass
class SceneEvent:
    type: str  # placed | removed | moved
    object_id: str
    ts: float
    zone: str
    box: tuple[int, int, int, int]
    map_xy: list[float] | None
    label: str
    local_label: str
    confidence: float
    crop: np.ndarray
    crop_offset: tuple[int, int]
    from_zone: str | None = None
    note: str = ""
    essential: bool = False
    id: str = field(default_factory=lambda: _new_id("ev"))

    def payload(self) -> dict:
        data = {
            "id": self.id, "type": self.type, "object_id": self.object_id, "ts": self.ts,
            "zone": self.zone, "bbox": list(map(int, self.box)), "map_xy": self.map_xy,
            "label": self.label, "local_label": self.local_label,
            "confidence": round(float(self.confidence), 3), "note": self.note, "essential": self.essential,
        }
        if self.from_zone:
            data["from_zone"] = self.from_zone
        return data


class SceneMemory:
    REID_WINDOW = 12 * 3600.0
    REID_THRESHOLD = 0.6
    PERSON_HOLD = 1.5  # seconds a person box keeps masking after the last detection

    def __init__(self, cfg: Config, geometry: RoomGeometry, detector: YoloxDetector | None = None):
        self.cfg = cfg
        self.geometry = geometry
        self.detector = detector
        self.short = cv.createBackgroundSubtractorMOG2(history=40, varThreshold=20, detectShadows=False)
        self.bg_long: np.ndarray | None = None
        self.acc: np.ndarray | None = None
        self.objects: dict[str, TrackedObject] = {}
        self.persons: list[tuple[tuple[int, int, int, int], float]] = []
        self.last_frame_detections: list[Detection] = []
        self.started_at: float | None = None
        self.last_t: float | None = None
        self._lighting_since: float | None = None
        self.motion_fraction = 0.0
        self.stats = {"frames": 0, "rebaselines": 0, "events": 0}
        self._k3 = cv.getStructuringElement(cv.MORPH_ELLIPSE, (3, 3))
        self._k7 = cv.getStructuringElement(cv.MORPH_ELLIPSE, (7, 7))
        self._k15 = cv.getStructuringElement(cv.MORPH_ELLIPSE, (15, 15))

    # ------------------------------------------------------------------ inputs

    def update_persons(self, detections: list[Detection], t: float) -> None:
        self.last_frame_detections = detections
        fresh = [(d.box, t) for d in detections if d.is_person and d.confidence >= self.cfg.person_conf]
        self.persons = [p for p in self.persons if t - p[1] < self.PERSON_HOLD] + fresh

    def person_boxes(self, t: float, hold: float | None = None) -> list[tuple[int, int, int, int]]:
        hold = self.PERSON_HOLD if hold is None else hold
        return [b for b, seen in self.persons if t - seen < hold]

    # ------------------------------------------------------------------ main loop

    def process(self, frame: np.ndarray, t: float) -> list[SceneEvent]:
        self.stats["frames"] += 1
        smooth = cv.GaussianBlur(frame, (5, 5), 0)
        if self.bg_long is None:
            self.bg_long = smooth.astype(np.float32)
            self.acc = np.zeros(frame.shape[:2], np.float32)
            self.started_at = self.last_t = t
            self.short.apply(smooth)
            return []
        dt = float(np.clip(t - self.last_t, 0.0, 0.5))
        self.last_t = t

        moving = self.short.apply(smooth)
        moving = cv.morphologyEx(moving, cv.MORPH_OPEN, self._k3)
        moving = cv.dilate(moving, self._k15) > 0
        self.motion_fraction = float(moving.mean())

        bg_u8 = cv.convertScaleAbs(self.bg_long)
        diff = cv.absdiff(smooth, bg_u8).max(axis=2)
        differs = diff > self.cfg.long_diff_threshold

        if self.started_at is not None and t - self.started_at < 1.5:
            # Let the short-term model learn the scene before trusting it.
            cv.accumulateWeighted(smooth, self.bg_long, 0.3)
            return []

        if self._handle_global_change(smooth, differs, t):
            return []

        person_mask = np.zeros(frame.shape[:2], np.uint8)
        for x, y, w, h in self.person_boxes(t):
            px, py = int(w * 0.15), int(h * 0.1)
            cv.rectangle(person_mask, (x - px, y - py), (x + w + px, y + h + py), 255, -1)
        occluded = moving | (person_mask > 0)

        static = differs & ~occluded
        self.acc[static] += dt
        self.acc[~differs] = np.maximum(self.acc[~differs] - 2 * dt, 0)

        # Slow drift (lighting through the day) only where nothing is happening.
        quiet = (~differs & ~occluded).astype(np.uint8)
        cv.accumulateWeighted(smooth, self.bg_long, 0.002, mask=quiet)

        # Group evidence into candidate regions first (closing joins fragments of one object), and
        # commit a region only once nearly all of it is stable, so one object yields one event.
        evidence = self.acc > 0.25
        building = cv.morphologyEx(evidence.astype(np.uint8) * 255, cv.MORPH_CLOSE, self._k15)
        building = cv.morphologyEx(building, cv.MORPH_OPEN, self._k3)
        n, labels, comp_stats, _ = cv.connectedComponentsWithStats(building, connectivity=8)

        area_total = frame.shape[0] * frame.shape[1]
        events: list[SceneEvent] = []
        for i in range(1, n):
            x, y, w, h, area = comp_stats[i]
            box = (int(x), int(y), int(w), int(h))
            region = labels[y:y + h, x:x + w] == i
            pix = region & evidence[y:y + h, x:x + w]
            if not pix.any():
                continue
            acc_vals = self.acc[y:y + h, x:x + w][pix]
            ready = float((acc_vals >= self.cfg.stable_seconds).mean())
            if ready < 0.85 and acc_vals.max() < 2.5 * self.cfg.stable_seconds:
                continue  # still settling
            comp = np.zeros(frame.shape[:2], np.uint8)
            comp[y:y + h, x:x + w][pix] = 255
            comp = cv.morphologyEx(comp, cv.MORPH_CLOSE, self._k7)
            area = int(np.count_nonzero(comp))
            ys, xs = np.nonzero(comp)
            box = (int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1))
            if area < self.cfg.min_object_area * area_total:
                if area < 0.25 * self.cfg.min_object_area * area_total:
                    self._absorb(smooth, comp)  # sensor noise
                continue
            if area > self.cfg.max_object_area * area_total:
                self._absorb(smooth, comp)  # furniture moved or camera bumped: not an object
                continue
            if not self._is_quiet(box, moving, t):
                continue  # a hand may still be on it; wait
            events.extend(self._explain(frame, smooth, comp, box, t))
            self._absorb(smooth, comp)
        self.stats["events"] += len(events)
        return events

    # ------------------------------------------------------------------ helpers

    def _handle_global_change(self, smooth: np.ndarray, differs: np.ndarray, t: float) -> bool:
        changed = float(differs.mean())
        if changed > self.cfg.lighting_change_fraction and self.motion_fraction < 0.5:
            if self._lighting_since is None:
                self._lighting_since = t
            elif t - self._lighting_since > 1.0:
                # Lights switched on/off or auto-exposure jump: re-learn instead of hallucinating events.
                self.bg_long = smooth.astype(np.float32)
                self.acc[:] = 0
                self._lighting_since = None
                self.stats["rebaselines"] += 1
            return True
        self._lighting_since = None
        return False

    def _is_quiet(self, box, moving: np.ndarray, t: float) -> bool:
        x, y, w, h = box
        m = 12
        y0, y1 = max(0, y - m), min(moving.shape[0], y + h + m)
        x0, x1 = max(0, x - m), min(moving.shape[1], x + w + m)
        if moving[y0:y1, x0:x1].mean() > 0.02:
            return False
        grown = (x - m, y - m, w + 2 * m, h + 2 * m)
        return not any(_intersects(grown, p) for p in self.person_boxes(t, hold=1.0))

    def _absorb(self, smooth: np.ndarray, comp: np.ndarray) -> None:
        grown = cv.dilate(comp, self._k7) > 0
        self.bg_long[grown] = smooth[grown]
        self.acc[grown] = 0

    def _boundary_gradients(self, cur: np.ndarray, under: np.ndarray, mask: np.ndarray) -> tuple[float, float]:
        ring = cv.morphologyEx(mask, cv.MORPH_GRADIENT, self._k3) > 0
        if not ring.any():
            return 0.0, 0.0

        def grad(img: np.ndarray) -> np.ndarray:
            g = cv.cvtColor(img, cv.COLOR_BGR2GRAY)
            gx = cv.Sobel(g, cv.CV_32F, 1, 0, ksize=3)
            gy = cv.Sobel(g, cv.CV_32F, 0, 1, ksize=3)
            return cv.magnitude(gx, gy)

        return float(grad(cur)[ring].mean()), float(grad(under)[ring].mean())

    def _explain(self, frame, smooth, comp, box, t) -> list[SceneEvent]:
        x, y, w, h = box
        bg_u8 = cv.convertScaleAbs(self.bg_long)
        cur = frame[y:y + h, x:x + w].copy()
        under = bg_u8[y:y + h, x:x + w].copy()
        mask = comp[y:y + h, x:x + w].copy()
        events: list[SceneEvent] = []

        # 1) Was a remembered object picked up?
        consumed = np.zeros_like(mask)
        for obj in self._overlapping(box):
            ox, oy, ow, oh = obj.box
            now = frame[oy:oy + oh, ox:ox + ow]
            if now.shape != obj.under.shape:
                continue
            s_under = app.patch_match(now, obj.under)
            s_obj = app.patch_match(now, obj.appearance)
            if s_under > s_obj + 0.03:
                events.append(self._removed(obj, t, frame, s_under - s_obj))
                cv.rectangle(consumed, (ox - x, oy - y), (ox - x + ow, oy - y + oh), 255, -1)
        remaining = cv.bitwise_and(mask, cv.bitwise_not(consumed))
        if events and remaining.sum() < 0.3 * mask.sum():
            return events
        if remaining.sum() < 0.3 * mask.sum():
            return events  # change was on a remembered object that is still there (lighting)

        # 2) New edges belong to the current frame (placed) or to the old background (ghost)?
        g_cur, g_under = self._boundary_gradients(cur, under, mask)
        if g_cur >= 0.85 * g_under:
            events.append(self._placed(frame, cur, under, mask, box, t))
        else:
            events.append(self._ghost_removed(frame, cur, under, mask, box, t))
        return events

    def _overlapping(self, box) -> list[TrackedObject]:
        out = []
        for obj in self.objects.values():
            if obj.status != "present":
                continue
            inter = _intersection(box, obj.box)
            if inter > 0.3 * obj.box[2] * obj.box[3] or inter > 0.3 * box[2] * box[3]:
                out.append(obj)
        return out

    def _label(self, frame, box) -> tuple[str, float]:
        if self.detector is None:
            return "object", 0.0
        return self.detector.label_region(frame, box)

    def _placed(self, frame, cur, under, mask, box, t) -> SceneEvent:
        sig = app.signature(cur, mask)
        zone = self.geometry.zone_for_box(box)
        best, best_score = None, 0.0
        for obj in self.objects.values():
            if obj.status == "removed" and obj.removed_at and t - obj.removed_at < self.REID_WINDOW:
                score = app.similarity(sig, obj.sig)
                if score > best_score:
                    best, best_score = obj, score

        crop, offset = self._crop(frame, box, t)
        if best is not None and best_score >= self.REID_THRESHOLD:
            from_zone = best.zone
            best.box, best.zone, best.map_xy = box, zone, self.geometry.to_map(box)
            best.appearance, best.under, best.mask, best.sig = cur, under, mask, sig
            best.status, best.removed_at, best.last_seen = "present", None, t
            moved = from_zone != zone
            return SceneEvent(
                "moved" if moved else "placed", best.id, t, zone, box, best.map_xy, best.label,
                best.local_label, best_score, crop, offset, from_zone=from_zone if moved else None,
                note=f"re-identified (similarity {best_score:.2f})" + ("" if moved else ", put back"),
                essential=best.essential,
            )

        label, conf = self._label(frame, box)
        obj = TrackedObject(
            id=_new_id("obj"), label=label, local_label=label, confidence=conf, box=box, zone=zone,
            map_xy=self.geometry.to_map(box), status="present", first_seen=t, last_seen=t,
            appearance=cur, under=under, mask=mask, sig=sig,
            essential=self._is_essential(label),
        )
        self.objects[obj.id] = obj
        return SceneEvent("placed", obj.id, t, zone, box, obj.map_xy, label, label, conf, crop, offset,
                          essential=obj.essential)

    def _removed(self, obj: TrackedObject, t: float, frame, margin: float) -> SceneEvent:
        obj.status, obj.removed_at, obj.last_seen = "removed", t, t
        crop, offset = self._crop(frame, obj.box, t)
        return SceneEvent("removed", obj.id, t, obj.zone, obj.box, obj.map_xy, obj.label, obj.local_label,
                          min(1.0, 0.5 + margin), crop, offset, note="picked up", essential=obj.essential)

    def _ghost_removed(self, frame, cur, under, mask, box, t) -> SceneEvent:
        """An object that was already there when memory started has been taken away."""
        bg_frame = cv.convertScaleAbs(self.bg_long)
        label, conf = self._label(bg_frame, box)
        obj = TrackedObject(
            id=_new_id("obj"), label=label, local_label=label, confidence=conf, box=box,
            zone=self.geometry.zone_for_box(box), map_xy=self.geometry.to_map(box), status="removed",
            first_seen=t, last_seen=t, appearance=under, under=cur, mask=mask, removed_at=t,
            essential=self._is_essential(label),
        )
        self.objects[obj.id] = obj
        crop, offset = self._crop(bg_frame, box, t)
        return SceneEvent("removed", obj.id, t, obj.zone, box, obj.map_xy, label, label, conf, crop, offset,
                          note="pre-existing object taken (seen in remembered background)",
                          essential=obj.essential)

    def _crop(self, frame, box, t):
        from .privacy import blur_boxes, crop_with_margin

        crop, offset = crop_with_margin(frame, box)
        if self.cfg.blur_people:
            crop = blur_boxes(crop, self.person_boxes(t, hold=3.0), offset)
        return crop, offset

    def _is_essential(self, label: str) -> bool:
        return any(e.lower() in label.lower() for e in self.cfg.essentials)

    # ------------------------------------------------------------------ live perception tools

    def verify(self, object_id: str, frame: np.ndarray, t: float) -> dict:
        obj = self.objects.get(object_id)
        if obj is None:
            return {"ok": False, "error": f"unknown object {object_id}"}
        ox, oy, ow, oh = obj.box
        now = frame[oy:oy + oh, ox:ox + ow]
        occluded = any(_intersects(obj.box, p) for p in self.person_boxes(t, hold=1.0))
        s_obj = app.patch_match(now, obj.appearance)
        s_under = app.patch_match(now, obj.under)
        present = s_obj > max(0.6, s_under)
        if present:
            obj.last_seen = t
        return {
            "ok": True, "object_id": object_id, "present": bool(present), "occluded": occluded,
            "match_object": round(s_obj, 3), "match_empty_surface": round(s_under, 3),
            "zone": obj.zone, "bbox": list(map(int, obj.box)),
        }

    def scan(self, object_id: str, frame: np.ndarray, t: float) -> dict:
        obj = self.objects.get(object_id)
        if obj is None:
            return {"ok": False, "error": f"unknown object {object_id}"}
        mask = np.full(frame.shape[:2], 255, np.uint8)
        for x, y, w, h in self.person_boxes(t, hold=1.0):
            cv.rectangle(mask, (x, y), (x + w, y + h), 0, -1)
        box, score = app.locate(obj.appearance, frame, obj.sig, mask)
        if box is None or score < 0.5:
            return {"ok": True, "object_id": object_id, "found": False, "score": round(score, 3)}
        zone = self.geometry.zone_for_box(box)
        return {"ok": True, "object_id": object_id, "found": True, "score": round(score, 3),
                "zone": zone, "bbox": list(map(int, box)), "map_xy": self.geometry.to_map(box)}

    def adopt_found(self, object_id: str, box, frame: np.ndarray, t: float) -> SceneEvent | None:
        """After a successful scan, move the remembered object to where it was found."""
        obj = self.objects.get(object_id)
        if obj is None:
            return None
        box = tuple(int(v) for v in box)
        x, y, w, h = box
        from_zone = obj.zone
        obj.box, obj.zone, obj.map_xy = box, self.geometry.zone_for_box(box), self.geometry.to_map(box)
        obj.appearance = frame[y:y + h, x:x + w].copy()
        obj.mask = np.full((h, w), 255, np.uint8)
        obj.under = obj.appearance.copy()  # unknown; refreshed on next pick-up/put-down
        obj.sig = app.signature(obj.appearance, obj.mask)
        obj.status, obj.removed_at, obj.last_seen = "present", None, t
        crop, offset = self._crop(frame, box, t)
        return SceneEvent("moved" if from_zone != obj.zone else "placed", obj.id, t, obj.zone, box, obj.map_xy,
                          obj.label, obj.local_label, 0.7, crop, offset,
                          from_zone=from_zone if from_zone != obj.zone else None,
                          note="found by camera scan", essential=obj.essential)

    # ------------------------------------------------------------------ persistence

    def save(self, directory: str | Path) -> None:
        d = Path(directory) / "objects"
        d.mkdir(parents=True, exist_ok=True)
        index = []
        for obj in self.objects.values():
            cv.imwrite(str(d / f"{obj.id}_app.png"), obj.appearance)
            cv.imwrite(str(d / f"{obj.id}_under.png"), obj.under)
            cv.imwrite(str(d / f"{obj.id}_mask.png"), obj.mask)
            index.append(obj.meta())
        (d / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")

    def load(self, directory: str | Path) -> int:
        d = Path(directory) / "objects"
        index_path = d / "index.json"
        if not index_path.exists():
            return 0
        for meta in json.loads(index_path.read_text(encoding="utf-8")):
            try:
                appearance = cv.imread(str(d / f"{meta['id']}_app.png"))
                under = cv.imread(str(d / f"{meta['id']}_under.png"))
                mask = cv.imread(str(d / f"{meta['id']}_mask.png"), cv.IMREAD_GRAYSCALE)
                if appearance is None or under is None or mask is None:
                    continue
                self.objects[meta["id"]] = TrackedObject(
                    id=meta["id"], label=meta["label"], local_label=meta["local_label"],
                    confidence=meta["confidence"], box=tuple(meta["box"]), zone=meta["zone"],
                    map_xy=meta["map_xy"], status=meta["status"], first_seen=meta["first_seen"],
                    last_seen=meta["last_seen"], appearance=appearance, under=under, mask=mask,
                    essential=meta.get("essential", False), removed_at=meta.get("removed_at"),
                )
            except (KeyError, cv.error):
                continue
        return len(self.objects)

    def draw(self, frame: np.ndarray, t: float) -> None:
        for obj in self.objects.values():
            if obj.status != "present":
                continue
            x, y, w, h = obj.box
            color = (60, 220, 60) if not obj.essential else (0, 215, 255)
            cv.rectangle(frame, (x, y), (x + w, y + h), color, 2)
            cv.putText(frame, f"{obj.label} @ {obj.zone}", (x, max(12, y - 5)), cv.FONT_HERSHEY_SIMPLEX,
                       0.45, color, 1, cv.LINE_AA)
        for x, y, w, h in self.person_boxes(t):
            cv.rectangle(frame, (x, y), (x + w, y + h), (200, 80, 255), 1)
        if self.acc is not None:
            building = (self.acc > 0.3).astype(np.uint8)
            contours, _ = cv.findContours(building, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE)
            cv.drawContours(frame, contours, -1, (0, 255, 255), 1)


def _intersection(a, b) -> int:
    ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return ix * iy


def _intersects(a, b) -> bool:
    return _intersection(a, b) > 0


def now() -> float:
    return time.time()
