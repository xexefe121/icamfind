"""The on-device loop: capture, perceive, remember, sync, and answer live camera requests."""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path

import cv2 as cv
import numpy as np

from .config import Config
from .detector import Detection, YoloxDetector
from .geometry import RoomGeometry
from .privacy import blur_boxes, crop_with_margin, encode_jpeg
from .scene import SceneEvent, SceneMemory
from .speech import Speaker
from .uplink import Uplink

log = logging.getLogger("icamfind.edge")


def open_source(source) -> cv.VideoCapture:
    """Open a webcam index, an RTSP/HTTP CCTV stream or a video file. 'auto' probes webcams."""
    if source == "auto":
        for index in range(4):
            cap = cv.VideoCapture(index, cv.CAP_DSHOW) if hasattr(cv, "CAP_DSHOW") else cv.VideoCapture(index)
            if cap.isOpened() and cap.read()[0]:
                log.info("using webcam %d", index)
                return cap
            cap.release()
        raise RuntimeError("no webcam found; set \"source\" in config.json")
    if isinstance(source, int):
        cap = cv.VideoCapture(source, cv.CAP_DSHOW) if hasattr(cv, "CAP_DSHOW") else cv.VideoCapture(source)
    else:
        cap = cv.VideoCapture(source)
    if not cap.isOpened():
        raise RuntimeError(f"could not open video source {source!r}")
    return cap


def resize_for_processing(frame: np.ndarray, width: int) -> np.ndarray:
    h, w = frame.shape[:2]
    if w == width:
        return frame
    return cv.resize(frame, (width, int(h * width / w)), interpolation=cv.INTER_AREA)


class PersonWorker(threading.Thread):
    """Runs the DNN detector on the newest frame in the background (keeps the main loop real-time)."""

    def __init__(self, detector: YoloxDetector, interval: float):
        super().__init__(name="detector", daemon=True)
        self.detector = detector
        self.interval = interval
        self._frame: tuple[np.ndarray, float] | None = None
        self._lock = threading.Lock()
        self.result: tuple[list[Detection], float] = ([], 0.0)
        self.latency = 0.0

    def submit(self, frame: np.ndarray, t: float) -> None:
        with self._lock:
            self._frame = (frame, t)

    def run(self) -> None:
        while True:
            with self._lock:
                item, self._frame = self._frame, None
            if item is None:
                time.sleep(0.02)
                continue
            frame, t = item
            start = time.perf_counter()
            dets = self.detector.detect(frame, 0.3)
            self.latency = time.perf_counter() - start
            self.result = (dets, t)
            time.sleep(max(0.0, self.interval - self.latency))


class EdgeApp:
    def __init__(self, cfg: Config, show: bool = True):
        self.cfg = cfg
        self.show = show
        self.geometry = RoomGeometry(cfg)
        self.detector = YoloxDetector(cfg.model_path)
        self.scene = SceneMemory(cfg, self.geometry, self.detector)
        restored = self.scene.load(cfg.data_dir)
        if restored:
            log.info("restored %d remembered objects", restored)
        self.uplink = Uplink(cfg.api_url, cfg.api_token, cfg.device_id, cfg.data_dir)
        self.speaker = Speaker(cfg.speak)
        self.worker = PersonWorker(self.detector, cfg.person_interval)
        self.commands: queue.Queue = queue.Queue()
        self.frame: np.ndarray | None = None
        self.frame_size = (0, 0)
        self._last_door_alert = 0.0
        self._last_heartbeat = 0.0
        self._last_snapshot = 0.0
        self._snapshot_requested = True
        self.event_log: list[str] = []

    # ------------------------------------------------------------------ helpers

    def _snapshot_jpeg(self, t: float) -> bytes:
        img = self.frame.copy()
        if self.cfg.blur_people:
            img = blur_boxes(img, self.scene.person_boxes(t, hold=3.0))
        return encode_jpeg(img, quality=80, max_side=960)

    def _publish(self, ev: SceneEvent) -> None:
        payload = ev.payload()
        jpeg = encode_jpeg(ev.crop, quality=88, max_side=320)
        self.uplink.record(payload, jpeg)
        text = f"{time.strftime('%H:%M:%S')} {ev.type:<7} {ev.label} @ {ev.zone}" + (
            f" (from {ev.from_zone})" if ev.from_zone else "")
        log.info(text)
        self.event_log = (self.event_log + [text])[-6:]
        self.scene.save(self.cfg.data_dir)

    def _heartbeat_payload(self) -> dict:
        return {
            "zones": [{"name": z.name, "polygon": z.polygon} for z in self.cfg.zones],
            "door_zone": self.cfg.door_zone,
            "frame_size": list(self.frame_size),
            "map": {"width": 1, "height": 1} if self.geometry.homography is not None else None,
        }

    def _cloud_loop(self) -> None:
        """Polls the command queue and sends heartbeats (own thread; commands execute in main loop)."""
        while True:
            t = time.time()
            if self.frame is not None and t - self._last_heartbeat > 20:
                snap = None
                if self._snapshot_requested or t - self._last_snapshot > 300:
                    snap = self._snapshot_jpeg(t)
                    self._last_snapshot, self._snapshot_requested = t, False
                self.uplink.heartbeat(self._heartbeat_payload(), snap)
                self._last_heartbeat = t
            for cmd in self.uplink.fetch_commands():
                self.commands.put(cmd)
            time.sleep(1.5)

    def _apply_label_updates(self) -> None:
        while not self.uplink.label_updates.empty():
            object_id, label, essential = self.uplink.label_updates.get()
            obj = self.scene.objects.get(object_id)
            if obj:
                obj.label = label
                obj.essential = essential or self.scene._is_essential(label)

    def _run_command(self, cmd: dict, t: float) -> None:
        kind, args = cmd.get("kind"), cmd.get("args") or {}
        image = None
        if kind == "verify_object":
            result = self.scene.verify(args.get("object_id", ""), self.frame, t)
            if result.get("ok"):
                obj = self.scene.objects[args["object_id"]]
                crop, offset = crop_with_margin(self.frame, obj.box, margin=0.8)
                image = encode_jpeg(blur_boxes(crop, self.scene.person_boxes(t, 3.0), offset), max_side=360)
        elif kind == "scan_for_object":
            result = self.scene.scan(args.get("object_id", ""), self.frame, t)
            if result.get("found"):
                ev = self.scene.adopt_found(args["object_id"], result["bbox"], self.frame, t)
                if ev:
                    self._publish(ev)
                crop, offset = crop_with_margin(self.frame, tuple(result["bbox"]), margin=0.8)
                image = encode_jpeg(blur_boxes(crop, self.scene.person_boxes(t, 3.0), offset), max_side=360)
            else:
                image = self._snapshot_jpeg(t)
        elif kind == "snapshot":
            image = self._snapshot_jpeg(t)
            result = {"ok": True, "frame_size": list(self.frame_size)}
            self.uplink.heartbeat(self._heartbeat_payload(), image)
            self._last_heartbeat = self._last_snapshot = t
        elif kind == "speak":
            self.speaker.say(str(args.get("text", ""))[:300])
            result = {"ok": True, "spoken": True}
        else:
            result = {"ok": False, "error": f"unknown command {kind}"}
        log.info("command %s -> %s", kind, {k: v for k, v in result.items() if k != "bbox"})
        threading.Thread(target=self.uplink.send_result, args=(cmd["id"], result, image), daemon=True).start()

    def _door_check(self, t: float) -> None:
        if not self.cfg.door_zone or t - self._last_door_alert < 45:
            return
        if not any(self.geometry.in_door_zone(b) for b in self.scene.person_boxes(t, hold=0.6)):
            return
        left = [o for o in self.scene.objects.values() if o.status == "present" and o.essential]
        if not left:
            return
        self._last_door_alert = t
        names = ", ".join(f"your {o.label} on the {o.zone}" for o in left[:3])
        message = f"Wait — {names} {'is' if len(left) == 1 else 'are'} still here."
        self.speaker.say(message)
        self.uplink.send_alert({"kind": "door_essentials", "message": message,
                                "objects": [o.id for o in left], "ts": t})

    # ------------------------------------------------------------------ loop

    def run(self, max_seconds: float | None = None) -> None:
        cap = open_source(self.cfg.source)
        started = time.time()
        self.worker.start()
        self.uplink.start()
        if self.uplink.enabled:
            threading.Thread(target=self._cloud_loop, name="cloud", daemon=True).start()
        log.info("iCamFind running: camera %s, %d zones, cloud %s, %d remembered objects (q to quit)",
                 self.cfg.source, len(self.cfg.zones), self.cfg.api_url if self.uplink.enabled else "off (local-only)",
                 len(self.scene.objects))
        min_dt = 1.0 / self.cfg.target_fps
        # A recorded clip can stand in for a camera (demo replays): play it at its native frame rate.
        is_file = isinstance(self.cfg.source, str) and Path(self.cfg.source).exists()
        file_dt = 1.0 / (cap.get(cv.CAP_PROP_FPS) or 15.0) if is_file else 0.0
        fps = 0.0
        last = time.time()
        try:
            while True:
                ok, raw = cap.read()
                if not ok:
                    log.warning("end of clip; replaying" if is_file else "frame grab failed; reconnecting")
                    cap.release()
                    time.sleep(0.2 if is_file else 1.0)
                    cap = open_source(self.cfg.source)
                    continue
                t = time.time()
                if max_seconds and t - started > max_seconds:
                    break
                if is_file:
                    time.sleep(max(0.0, file_dt - (t - last)))
                    t = time.time()
                elif t - last < min_dt:
                    continue
                fps = 0.9 * fps + 0.1 / max(1e-3, t - last)
                last = t
                frame = resize_for_processing(raw, self.cfg.process_width)
                self.frame = frame
                self.frame_size = (frame.shape[1], frame.shape[0])

                self.worker.submit(frame, t)
                dets, det_t = self.worker.result
                if det_t:
                    self.scene.update_persons(dets, det_t)

                for ev in self.scene.process(frame, t):
                    self._publish(ev)
                self._apply_label_updates()
                self._door_check(t)
                while not self.commands.empty():
                    self._run_command(self.commands.get(), t)

                if self.show:
                    view = frame.copy()
                    self.geometry.draw(view)
                    self.scene.draw(view, t)
                    status = (f"{fps:4.1f} fps | dnn {self.worker.latency * 1000:.0f} ms | objects "
                              f"{sum(o.status == 'present' for o in self.scene.objects.values())} | cloud "
                              f"{'on' if self.uplink.online else ('off' if self.uplink.enabled else 'local')}")
                    cv.putText(view, status, (8, view.shape[0] - 10), cv.FONT_HERSHEY_SIMPLEX, 0.45,
                               (255, 255, 255), 1, cv.LINE_AA)
                    for i, line in enumerate(reversed(self.event_log)):
                        cv.putText(view, line, (8, 18 + 18 * i), cv.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1,
                                   cv.LINE_AA)
                    cv.imshow("iCamFind", view)
                    key = cv.waitKey(1) & 0xFF
                    if key in (ord("q"), 27):
                        break
                    if key == ord("r"):
                        self.scene.bg_long = None
                        log.info("background reset")
        finally:
            cap.release()
            self.scene.save(self.cfg.data_dir)
            self.uplink.stop()
            if self.show:
                cv.destroyAllWindows()
