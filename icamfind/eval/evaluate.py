"""Score the scene-memory pipeline against ground-truth event annotations for a video clip.

Ground truth JSON: {"zones": [...], "events": [{"t": 12.5, "type": "placed|removed|moved",
"object": "keys", "zone": "desk", "from_zone": "shelf"?}]}.  "t" is when the hand has left.

A predicted event matches a truth event when the type agrees ("moved" also satisfies "placed"),
it is reported between 0.5 s before and `max_latency` seconds after the truth time, and neither
was matched before. Zone accuracy, latency and identity consistency are computed over matches.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import cv2 as cv

from ..edge.config import Config, Zone
from ..edge.detector import YoloxDetector
from ..edge.geometry import RoomGeometry
from ..edge.scene import SceneMemory


def run_pipeline(video: Path, cfg: Config, use_dnn: bool, person_every: float = 0.4) -> tuple[list[dict], dict]:
    cap = cv.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {video}")
    fps = cap.get(cv.CAP_PROP_FPS) or 15.0
    geometry = RoomGeometry(cfg)
    detector = YoloxDetector(cfg.model_path) if use_dnn else None
    scene = SceneMemory(cfg, geometry, detector)
    events, f, next_person = [], 0, 0.0
    frame_times = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = f / fps
        f += 1
        if frame.shape[1] != cfg.process_width:
            h, w = frame.shape[:2]
            frame = cv.resize(frame, (cfg.process_width, int(h * cfg.process_width / w)), interpolation=cv.INTER_AREA)
        start = time.perf_counter()
        if detector is not None and t >= next_person:
            scene.update_persons(detector.detect(frame, 0.3), t)
            next_person = t + person_every
        for ev in scene.process(frame, t):
            events.append(ev.payload())
        frame_times.append(time.perf_counter() - start)
    cap.release()
    frame_times.sort()
    perf = {
        "frames": f, "video_fps": fps,
        "median_ms_per_frame": round(1000 * frame_times[len(frame_times) // 2], 2) if frame_times else None,
        "p95_ms_per_frame": round(1000 * frame_times[int(len(frame_times) * 0.95)], 2) if frame_times else None,
        "rebaselines": scene.stats["rebaselines"],
    }
    return events, perf


def score(pred: list[dict], truth: list[dict], max_latency: float = 6.0) -> dict:
    used, matches = set(), []
    for ti, tr in enumerate(truth):
        best, best_dt = None, None
        for pi, p in enumerate(pred):
            if pi in used:
                continue
            ok_type = p["type"] == tr["type"] or (tr["type"] == "placed" and p["type"] == "moved") \
                or (tr["type"] == "moved" and p["type"] == "placed")
            dt = p["ts"] - tr["t"]
            if ok_type and -0.5 <= dt <= max_latency and (best_dt is None or abs(dt) < abs(best_dt)):
                best, best_dt = pi, dt
        if best is not None:
            used.add(best)
            matches.append((ti, best, best_dt))

    tp = len(matches)
    precision = tp / len(pred) if pred else 1.0
    recall = tp / len(truth) if truth else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    zone_ok = sum(pred[pi]["zone"] == truth[ti]["zone"] for ti, pi, _ in matches)
    exact_type = sum(pred[pi]["type"] == truth[ti]["type"] for ti, pi, _ in matches)

    # Identity: every truth object should map to one predicted object id across its events.
    ids: dict[str, set[str]] = {}
    for ti, pi, _ in matches:
        ids.setdefault(truth[ti]["object"], set()).add(pred[pi]["object_id"])
    multi_event = {k: v for k, v in ids.items() if sum(t["object"] == k for t in truth) > 1}
    identity_ok = sum(len(v) == 1 for v in multi_event.values())

    by_type = {}
    for kind in ("placed", "removed", "moved"):
        n = sum(t["type"] == kind for t in truth)
        hit = sum(truth[ti]["type"] == kind for ti, _, _ in matches)
        by_type[kind] = {"truth": n, "detected": hit, "recall": round(hit / n, 3) if n else None}

    missed = [truth[i] for i in range(len(truth)) if i not in {m[0] for m in matches}]
    false_pos = [pred[i] for i in range(len(pred)) if i not in used]
    return {
        "truth_events": len(truth), "predicted_events": len(pred), "true_positives": tp,
        "precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3),
        "zone_accuracy": round(zone_ok / tp, 3) if tp else None,
        "exact_type_accuracy": round(exact_type / tp, 3) if tp else None,
        "mean_latency_s": round(sum(m[2] for m in matches) / tp, 2) if tp else None,
        "identity_consistency": f"{identity_ok}/{len(multi_event)}",
        "by_type": by_type,
        "missed": missed,
        "false_positives": [{k: p[k] for k in ("type", "ts", "zone", "label")} for p in false_pos],
    }


def evaluate(video: Path, truth_path: Path, cfg: Config | None = None, use_dnn: bool = True) -> dict:
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    cfg = cfg or Config()
    if truth.get("zones"):
        cfg.zones = [Zone(z["name"], z["polygon"]) for z in truth["zones"]]
        cfg.door_zone = truth.get("door_zone")
    if truth.get("frame_size"):
        cfg.process_width = int(truth["frame_size"][0])
    pred, perf = run_pipeline(video, cfg, use_dnn)
    result = score(pred, truth["events"])
    result["video"] = video.name
    result["performance"] = perf
    result["predictions"] = [{k: p[k] for k in ("type", "ts", "zone", "label", "object_id")} | (
        {"from_zone": p["from_zone"]} if p.get("from_zone") else {}) for p in pred]
    return result
