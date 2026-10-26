"""Local-first journal plus best-effort cloud sync.

Every event is written to `data/journal.jsonl` (and its crop to `data/crops/`) before any network
call, so memory survives offline periods; a background thread drains the journal to the cloud API.
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger("icamfind.uplink")


class Uplink:
    def __init__(self, api_url: str | None, token: str | None, device_id: str, data_dir: str | Path):
        self.api_url = api_url.rstrip("/") if api_url else None
        self.token = token
        self.device_id = device_id
        self.data = Path(data_dir)
        (self.data / "crops").mkdir(parents=True, exist_ok=True)
        self.journal = self.data / "journal.jsonl"
        self.synced_path = self.data / "synced.txt"
        self.synced: set[str] = set(self.synced_path.read_text().split()) if self.synced_path.exists() else set()
        self.session = requests.Session()
        if token:
            self.session.headers["x-icamfind-device-token"] = token
        self.outbox: queue.Queue = queue.Queue()
        self.label_updates: queue.Queue = queue.Queue()
        self.online = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_url and self.token)

    # ----------------------------------------------------------------- journal

    def record(self, event: dict, crop_jpeg: bytes | None) -> None:
        if crop_jpeg:
            (self.data / "crops" / f"{event['id']}.jpg").write_bytes(crop_jpeg)
        with self.journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
        if self.enabled:
            self.outbox.put(("event", event["id"]))

    def _load_event(self, event_id: str) -> tuple[dict | None, bytes | None]:
        if not self.journal.exists():
            return None, None
        with self.journal.open(encoding="utf-8") as f:
            for line in f:
                if f'"{event_id}"' in line:
                    ev = json.loads(line)
                    if ev.get("id") == event_id:
                        crop = self.data / "crops" / f"{event_id}.jpg"
                        return ev, crop.read_bytes() if crop.exists() else None
        return None, None

    def pending_ids(self) -> list[str]:
        if not self.journal.exists():
            return []
        ids = []
        with self.journal.open(encoding="utf-8") as f:
            for line in f:
                try:
                    eid = json.loads(line)["id"]
                except (ValueError, KeyError):
                    continue
                if eid not in self.synced:
                    ids.append(eid)
        return ids

    # ----------------------------------------------------------------- http

    def _post(self, path: str, body: dict, timeout: float = 20.0) -> dict | None:
        try:
            r = self.session.post(f"{self.api_url}{path}", json=body, timeout=timeout)
            self.online = r.status_code < 500
            if r.status_code == 401:
                log.error("cloud rejected the device token (401); check ICAMFIND_DEVICE_TOKEN")
                return None
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            self.online = False
            log.warning("POST %s failed: %s", path, exc)
            return None

    def _get(self, path: str, timeout: float = 10.0) -> dict | None:
        try:
            r = self.session.get(f"{self.api_url}{path}", timeout=timeout)
            self.online = r.status_code < 500
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            self.online = False
            log.debug("GET %s failed: %s", path, exc)
            return None

    def send_event(self, event_id: str) -> bool:
        event, crop = self._load_event(event_id)
        if event is None:
            return True
        body = {"event": event}
        if crop:
            body["crop_b64"] = base64.b64encode(crop).decode()
        res = self._post("/api/device/events", body, timeout=40.0)
        if res is None:
            return False
        self.synced.add(event_id)
        with self.synced_path.open("a") as f:
            f.write(event_id + "\n")
        obj = res.get("object") or {}
        if obj.get("id") and obj.get("label"):
            self.label_updates.put((obj["id"], obj["label"], bool(obj.get("essential"))))
        return True

    def heartbeat(self, payload: dict, snapshot_jpeg: bytes | None = None) -> None:
        body = dict(payload)
        if snapshot_jpeg:
            body["snapshot_b64"] = base64.b64encode(snapshot_jpeg).decode()
        self._post("/api/device/heartbeat", body, timeout=20.0)

    def fetch_commands(self) -> list[dict]:
        res = self._get("/api/device/commands")
        return (res or {}).get("commands", [])

    def send_result(self, command_id: str, result: dict, image_jpeg: bytes | None = None) -> None:
        body = {"result": result}
        if image_jpeg:
            body["crop_b64"] = base64.b64encode(image_jpeg).decode()
        self._post(f"/api/device/commands/{command_id}/result", body)

    def send_alert(self, alert: dict) -> None:
        if self.enabled:
            self.outbox.put(("alert", alert))

    # ----------------------------------------------------------------- worker

    def start(self) -> None:
        if not self.enabled:
            log.info("cloud uplink disabled (no ICAMFIND_API_URL / ICAMFIND_DEVICE_TOKEN); running local-only")
            return
        for eid in self.pending_ids():
            self.outbox.put(("event", eid))
        self._thread = threading.Thread(target=self._run, name="uplink", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                kind, item = self.outbox.get(timeout=1.0)
            except queue.Empty:
                continue
            ok = True
            if kind == "event":
                ok = self.send_event(item)
            elif kind == "alert":
                ok = self._post("/api/device/alerts", item) is not None
            if ok:
                backoff = 1.0
            else:
                self.outbox.put((kind, item))
                time.sleep(backoff)
                backoff = min(30.0, backoff * 2)
