"""Simulated iCamFind backend for browser tests (stdlib only + OpenCV for test pictures).

Serves the real web app (cloud/src/web) with the same security headers as the deployed API, a fake
Cognito hosted UI (authorization code + PKCE), and an in-memory version of every user and device
route in docs/API.md. The agent is simulated: a question becomes a job whose steps appear over a few
seconds, and its live check is a real round trip through the browser camera's command queue.

    python tests/ui/mock_server.py [--port 8765]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "cloud" / "src" / "web"

os.environ.setdefault("AWS_REGION", "ap-southeast-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
sys.path.insert(0, str(ROOT / "cloud" / "src"))
import api as real_api  # noqa: E402  (only for SECURITY_HEADERS, so the mock sends the deployed CSP)

SECURITY_HEADERS = dict(real_api.SECURITY_HEADERS)
# OpenCV.js (Emscripten embind) builds functions with `new Function`, which needs 'unsafe-eval'.
# Until cloud/src/api.py allows it, the mock applies that fix so the rest of the app can be tested;
# e2e.py's "csp allows opencv" check reports whether the deployed header is fixed.
CSP_NEEDS_FIX = "'unsafe-eval'" not in SECURITY_HEADERS["content-security-policy"]
if CSP_NEEDS_FIX and os.environ.get("MOCK_REAL_CSP") != "1":
    SECURITY_HEADERS["content-security-policy"] = SECURITY_HEADERS["content-security-policy"].replace(
        "'wasm-unsafe-eval'", "'unsafe-eval' 'wasm-unsafe-eval'")
STATIC = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/edge.js": "edge.js",
          "/style.css": "style.css"}
CTYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript", ".css": "text/css"}
DEMO_ZONES = [
    {"name": "desk", "polygon": [[20, 250], [400, 250], [400, 470], [20, 470]]},
    {"name": "shelf", "polygon": [[430, 60], [630, 60], [630, 200], [430, 200]]},
    {"name": "side table", "polygon": [[430, 280], [630, 280], [630, 470], [430, 470]]},
    {"name": "door", "polygon": [[0, 0], [150, 0], [150, 230], [0, 230]]},
]


def _now() -> float:
    return time.time()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(email: str) -> str:
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"sub": "user-1", "email": email, "exp": int(_now()) + 3600}).encode())
    return f"{header}.{payload}.sig"


def _test_pictures() -> tuple[bytes, bytes]:
    """A demo-room snapshot (frame of the benchmark clip) and a small close-up, as JPEG bytes."""
    import cv2 as cv
    import numpy as np

    snap = None
    clip = ROOT / "bench" / "synthetic_1.mp4"
    if clip.exists():
        cap = cv.VideoCapture(str(clip))
        cap.set(cv.CAP_PROP_POS_FRAMES, 15 * 35)
        ok, frame = cap.read()
        cap.release()
        if ok:
            snap = frame
    if snap is None:
        snap = np.full((480, 640, 3), (120, 140, 160), np.uint8)
    closeup = cv.resize(snap[250:470, 20:400], (320, 185))
    return cv.imencode(".jpg", snap)[1].tobytes(), cv.imencode(".jpg", closeup)[1].tobytes()


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.images: dict[str, bytes] = {}
        snap, close = _test_pictures()
        self.images["demo-snap"] = snap
        self.images["closeup"] = close
        self.tokens: dict[str, tuple[str, str]] = {}  # device token -> (household, device id)
        self.pair_codes: dict[str, dict] = {}
        self.homes = {hid: self._empty() for hid in ("hh_mine", "demo")}
        self.debug = {"events_received": {"hh_mine": 0, "demo": 0}, "devices_created": 0, "zones_put": 0,
                      "renames": [], "essential": [], "approvals": [], "commands_done": 0, "snapshots": 0,
                      "force_401": 0, "refreshes": 0}
        self._seed_demo()

    @staticmethod
    def _empty() -> dict:
        return {"objects": {}, "events": [], "devices": {}, "alerts": [], "approvals": {}, "routines": {},
                "jobs": {}, "commands": [], "labels_given": 0}

    def _seed_demo(self):
        h = self.homes["demo"]
        h["devices"]["dev_demo_replay"] = {
            "id": "dev_demo_replay", "name": "Demo replay camera", "kind": "computer", "zones": DEMO_ZONES,
            "door_zone": "door", "frame_size": [640, 480], "snapshot_key": "demo-snap", "snapshot_at": _now() - 120,
            "last_heartbeat": _now(), "demo_live": True}
        seed = [("leather wallet", "shelf", [478, 128, 60, 42]), ("house keys", "desk", [263, 355, 36, 24]),
                ("The Hobbit (book)", "side table", [470, 330, 110, 78])]
        for i, (label, zone, bbox) in enumerate(seed):
            oid = f"obj_demo{i}"
            h["objects"][oid] = {"id": oid, "label": label, "local_label": "object", "description": f"A {label}.",
                                 "status": "present", "zone": zone, "bbox": bbox, "map_xy": None,
                                 "first_seen": _now() - 3600, "last_seen": _now() - 300 * (i + 1),
                                 "last_event": "placed", "essential": label != "The Hobbit (book)",
                                 "crop_key": "closeup", "device": "dev_demo_replay"}
            h["events"].append({"id": _id("ev"), "object_id": oid, "type": "placed", "ts": _now() - 300 * (i + 1),
                                "zone": zone, "label": label, "confidence": 0.9, "crop_key": "closeup"})
        h["alerts"].append({"id": _id("alr"), "kind": "door_essentials", "message": "Wait - your house keys on the desk are still here.",
                            "ts": _now() - 600, "objects": ["obj_demo1"]})

    # ---------------------------------------------------------------- helpers
    def url(self, base: str, key: str | None, bust: float | None = None) -> str | None:
        if not key or key not in self.images:
            return None
        return f"{base}/img/{key}" + (f"?t={bust:.3f}" if bust else "")

    def obj_out(self, base: str, o: dict) -> dict:
        out = {k: v for k, v in o.items() if k != "crop_key"}
        out["crop_url"] = self.url(base, o.get("crop_key"))
        return out

    def ev_out(self, base: str, e: dict) -> dict:
        out = {k: v for k, v in e.items() if k != "crop_key"}
        out["crop_url"] = self.url(base, e.get("crop_key"))
        return out


STORE = Store()


class Handler(BaseHTTPRequestHandler):
    server_version = "iCamFindMock/1.0"

    def log_message(self, fmt, *args):  # quiet
        if os.environ.get("MOCK_VERBOSE"):
            super().log_message(fmt, *args)

    @property
    def base(self) -> str:
        return f"http://{self.headers.get('Host')}"

    # ------------------------------------------------------------ responses
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj):
        self._send(status, json.dumps(obj, default=str).encode(), "application/json")

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("location", location)
        self.send_header("content-length", "0")
        self.end_headers()

    def _body(self) -> dict:
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b""
        ctype = self.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in ctype:
            return {k: v[0] for k, v in parse_qs(raw.decode()).items()}
        return json.loads(raw) if raw else {}

    # ------------------------------------------------------------ routing
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method: str):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        try:
            if method == "GET" and path in STATIC:
                f = WEB / STATIC[path]
                return self._send(200, f.read_bytes(), CTYPES[f.suffix])
            if method == "GET" and path == "/config.json":
                return self._json(200, {"cognito_domain": f"{self.base}/mockcognito", "client_id": "test",
                                        "redirect_uri": f"{self.base}/", "region": "ap-southeast-2"})
            if method == "GET" and path.startswith("/img/"):
                key = path[5:]
                data = STORE.images.get(key)
                if data is None:
                    return self._json(404, {"error": "not found"})
                return self._send(200, data, "image/jpeg")
            if method == "POST" and path == "/__debug/expire":
                STORE.debug["force_401"] = 1  # next user API call answers 401, as an expired token would
                return self._json(200, {"ok": True})
            if method == "GET" and path == "/__debug":
                with STORE.lock:
                    dbg = dict(STORE.debug)
                    dbg["devices"] = {h: list(v["devices"].keys()) for h, v in STORE.homes.items()}
                    dbg["objects"] = {h: len(v["objects"]) for h, v in STORE.homes.items()}
                    dbg["object_list"] = {h: [{k: o[k] for k in ("id", "label", "status", "zone", "essential")}
                                              for o in v["objects"].values()] for h, v in STORE.homes.items()}
                    dbg["zones"] = {h: {d: dev.get("zones_user") for d, dev in v["devices"].items()}
                                    for h, v in STORE.homes.items()}
                return self._json(200, dbg)
            if path.startswith("/mockcognito/"):
                return self._cognito(method, path, qs)
            if path.startswith("/api/device/"):
                return self._device(method, path)
            if path.startswith("/api/"):
                return self._user(method, path, qs)
            return self._json(404, {"error": "not found"})
        except Exception as exc:  # surface server bugs to the test
            import traceback

            traceback.print_exc()
            return self._json(500, {"error": f"mock error: {exc}"})

    # ------------------------------------------------------------ fake Cognito
    def _cognito(self, method, path, qs):
        if path == "/mockcognito/oauth2/authorize":
            if qs.get("code_challenge_method", [""])[0] != "S256" or not qs.get("code_challenge"):
                return self._json(400, {"error": "pkce required"})
            q = urlencode({"code": "abc", "state": qs.get("state", [""])[0]})
            return self._redirect(f"{qs['redirect_uri'][0]}?{q}")
        if path == "/mockcognito/oauth2/token" and method == "POST":
            body = self._body()
            grant = body.get("grant_type")
            if grant == "authorization_code" and (body.get("code") != "abc" or not body.get("code_verifier")):
                return self._json(400, {"error": "invalid_grant", "error_description": "bad code or verifier"})
            if grant == "refresh_token" and not body.get("refresh_token"):
                return self._json(400, {"error": "invalid_grant"})
            if grant == "refresh_token":
                STORE.debug["refreshes"] += 1
            if grant not in ("authorization_code", "refresh_token"):
                return self._json(400, {"error": "unsupported_grant_type"})
            return self._json(200, {"id_token": make_jwt("judge@example.test"), "access_token": "x",
                                    "refresh_token": "refresh-1", "expires_in": 3600, "token_type": "Bearer"})
        if path == "/mockcognito/logout":
            return self._redirect(qs.get("logout_uri", ["/"])[0])
        return self._json(404, {"error": "not found"})

    # ------------------------------------------------------------ device routes
    def _device(self, method, path):
        if path == "/api/device/pair" and method == "POST":
            body = self._body()
            item = STORE.pair_codes.pop(str(body.get("code", "")).upper(), None)
            if not item:
                return self._json(400, {"error": "that code is wrong or has expired"})
            token = "icf_" + secrets.token_urlsafe(16)
            dev = {"id": _id("dev"), "name": item["name"], "kind": "computer"}
            with STORE.lock:
                STORE.homes[item["household"]]["devices"][dev["id"]] = dev
                STORE.tokens[token] = (item["household"], dev["id"])
            return self._json(200, {"token": token, "api_url": self.base, "device": dev})
        token = self.headers.get("x-icamfind-device-token", "")
        if token not in STORE.tokens:
            return self._json(401, {"error": "unauthorized"})
        hid, did = STORE.tokens[token]
        h = STORE.homes[hid]
        with STORE.lock:
            dev = h["devices"][did]
            if path == "/api/device/config" and method == "GET":
                return self._json(200, {"name": dev.get("name"), "zones": dev.get("zones_user") or [],
                                        "door_zone": dev.get("door_zone_user")})
            if path == "/api/device/heartbeat" and method == "POST":
                b = self._body()
                dev.update(last_heartbeat=_now(), zones=b.get("zones", []), door_zone=b.get("door_zone"),
                           frame_size=b.get("frame_size"))
                if b.get("snapshot_b64"):
                    key = f"snap-{did}-{int(_now() * 1000)}"
                    STORE.images[key] = base64.b64decode(b["snapshot_b64"])
                    dev["snapshot_key"], dev["snapshot_at"] = key, _now()
                return self._json(200, {"ok": True})
            if path == "/api/device/events" and method == "POST":
                b = self._body()
                ev = b["event"]
                key = None
                if b.get("crop_b64"):
                    key = f"crop-{ev['id']}"
                    STORE.images[key] = base64.b64decode(b["crop_b64"])
                obj = h["objects"].get(ev["object_id"])
                if obj is None:
                    h["labels_given"] += 1
                    label = {1: "keys", 2: "wallet"}.get(h["labels_given"], "object")
                    obj = {"id": ev["object_id"], "label": label, "local_label": ev.get("local_label", "object"),
                           "description": f"Test {label}", "first_seen": ev["ts"], "essential": label in ("keys", "wallet"),
                           "device": did}
                    h["objects"][obj["id"]] = obj
                obj.update(status="removed" if ev["type"] == "removed" else "present", zone=ev.get("zone"),
                           bbox=ev.get("bbox"), map_xy=ev.get("map_xy"), last_seen=ev["ts"], last_event=ev["type"])
                if key and (ev["type"] != "removed" or not obj.get("crop_key")):
                    obj["crop_key"] = key
                h["events"].append({**ev, "label": obj["label"], "crop_key": key, "device": did})
                STORE.debug["events_received"][hid] += 1
                return self._json(200, {"object": STORE.obj_out(self.base, obj)})
            if path == "/api/device/commands" and method == "GET":
                out = [c for c in h["commands"] if c["device"] == did and c["status"] == "pending"]
                for c in out:
                    c["status"] = "sent"
                return self._json(200, {"commands": [{k: c[k] for k in ("id", "kind", "args", "created")} for c in out]})
            if path.startswith("/api/device/commands/") and path.endswith("/result") and method == "POST":
                cid = path.split("/")[4]
                b = self._body()
                for c in h["commands"]:
                    if c["id"] == cid:
                        c["status"], c["result"] = "done", b.get("result", {})
                        if b.get("crop_b64"):
                            STORE.images[f"cmd-{cid}"] = base64.b64decode(b["crop_b64"])
                            c["image_key"] = f"cmd-{cid}"
                        STORE.debug["commands_done"] += 1
                        return self._json(200, {"ok": True})
                return self._json(404, {"error": "command not found"})
            if path == "/api/device/alerts" and method == "POST":
                b = self._body()
                h["alerts"].append({"id": _id("alr"), "kind": b.get("kind"), "message": b.get("message"),
                                    "ts": b.get("ts") or _now(), "objects": b.get("objects", [])})
                return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})

    # ------------------------------------------------------------ user routes
    def _user(self, method, path, qs):
        auth = self.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth.count(".") < 2:
            return self._json(401, {"error": "unauthorized"})
        if STORE.debug["force_401"] > 0:
            STORE.debug["force_401"] -= 1
            return self._json(401, {"error": "unauthorized"})
        hid = self.headers.get("x-icamfind-household") or "hh_mine"
        if hid not in STORE.homes:
            return self._json(403, {"error": "not a member of that home"})
        h = STORE.homes[hid]
        demo = hid == "demo"
        base = self.base
        with STORE.lock:
            if demo:
                d = h["devices"]["dev_demo_replay"]
                d["last_heartbeat"] = _now()
                pending = d.get("snap_requested")
                if pending and _now() - pending > 2:
                    d["snapshot_at"], d["snap_requested"] = _now(), None
            if path == "/api/me" and method == "GET":
                return self._json(200, {"user": {"sub": "user-1", "email": "judge@example.test"},
                                        "households": [{"id": "hh_mine", "name": "My home", "role": "owner", "demo": False},
                                                       {"id": "demo", "name": "Demo home", "role": "member", "demo": True}],
                                        "current": "hh_mine"})
            if path == "/api/state" and method == "GET":
                devs = sorted(h["devices"].values(), key=lambda x: x.get("last_heartbeat", 0), reverse=True)
                for x in devs:
                    x["online"] = _now() - x.get("last_heartbeat", 0) < 60
                dev = dict(devs[0]) if devs else None
                if dev:
                    dev["snapshot_url"] = STORE.url(base, dev.pop("snapshot_key", None), dev.get("snapshot_at"))
                return self._json(200, {
                    "household": {"id": hid, "name": "Demo home" if demo else "My home", "demo": demo},
                    "device": dev,
                    "devices": [{k: x.get(k) for k in ("id", "name", "kind", "online", "last_heartbeat")} for x in devs],
                    "objects": [STORE.obj_out(base, o) for o in sorted(h["objects"].values(), key=lambda o: -o["last_seen"])],
                    "alerts": h["alerts"][-20:],
                    "approvals": [a for a in h["approvals"].values() if a["status"] == "pending"],
                    "routines": list(h["routines"].values())})
            if path == "/api/events" and method == "GET":
                limit = int(qs.get("limit", ["50"])[0])
                evs = sorted(h["events"], key=lambda e: -e["ts"])[:limit]
                return self._json(200, {"events": [STORE.ev_out(base, e) for e in evs]})
            if path.startswith("/api/objects/"):
                oid = path.split("/")[3]
                o = h["objects"].get(oid)
                if o is None:
                    return self._json(404, {"error": "object not found"})
                if method == "POST":
                    b = self._body()
                    if "label" in b:
                        o["label"] = b["label"]
                        STORE.debug["renames"].append(b["label"])
                    if "essential" in b:
                        o["essential"] = bool(b["essential"])
                        STORE.debug["essential"].append([oid, o["essential"]])
                    return self._json(200, {"object": STORE.obj_out(base, o)})
                evs = sorted([e for e in h["events"] if e["object_id"] == oid], key=lambda e: -e["ts"])
                return self._json(200, {"object": STORE.obj_out(base, o), "events": [STORE.ev_out(base, e) for e in evs]})
            if path == "/api/ask" and method == "POST":
                b = self._body()
                job = self._new_job(hid, b.get("question", ""), "ask")
                threading.Thread(target=run_ask_job, args=(hid, job["id"], base), daemon=True).start()
                return self._json(200, {"job_id": job["id"]})
            if path.startswith("/api/jobs/") and method == "GET":
                job = h["jobs"].get(path.split("/")[3])
                if job is None:
                    return self._json(404, {"error": "job not found"})
                return self._json(200, {"job": {k: v for k, v in job.items() if k != "steps"}, "steps": job["steps"]})
            if path == "/api/snapshot" and method == "POST":
                devs = sorted(h["devices"].values(), key=lambda x: x.get("last_heartbeat", 0), reverse=True)
                if not devs or _now() - devs[0].get("last_heartbeat", 0) > 60:
                    return self._json(409, {"error": "camera offline"})
                STORE.debug["snapshots"] += 1
                if demo:
                    devs[0]["snap_requested"] = _now()
                    return self._json(200, {"job_id": _id("cmd")})
                cmd = self._command(hid, devs[0]["id"], "snapshot", {})
                return self._json(200, {"job_id": cmd["id"]})
            if path == "/api/devices" and method == "GET":
                return self._json(200, {"devices": list(h["devices"].values())})
            if path == "/api/devices" and method == "POST":
                if demo:
                    return self._json(403, {"error": "cameras can only be added to your own home"})
                b = self._body()
                token = "icf_" + secrets.token_urlsafe(16)
                dev = {"id": _id("dev"), "name": b.get("name") or "Camera", "kind": b.get("kind", "computer")}
                h["devices"][dev["id"]] = dev
                STORE.tokens[token] = (hid, dev["id"])
                STORE.debug["devices_created"] += 1
                return self._json(200, {"device": dev, "token": token, "run_command": "python -m icamfind run"})
            if path == "/api/devices/pair-code" and method == "POST":
                if demo:
                    return self._json(403, {"error": "cameras can only be added to your own home"})
                code = "K7P2-9QXA"
                STORE.pair_codes[code] = {"household": hid, "name": self._body().get("name", "Camera")}
                return self._json(200, {"code": code, "expires_in": 600,
                                        "command": f"python -m icamfind pair {code} --api {base}"})
            if path.startswith("/api/devices/") and path.endswith("/zones") and method == "PUT":
                did = path.split("/")[3]
                dev = h["devices"].get(did)
                if dev is None:
                    return self._json(404, {"error": "camera not found"})
                b = self._body()
                dev["zones_user"], dev["door_zone_user"] = b.get("zones"), b.get("door_zone")
                STORE.debug["zones_put"] += 1
                return self._json(200, {"ok": True})
            if path == "/api/routines" and method == "POST":
                b = self._body()
                if b.get("object_id") not in h["objects"]:
                    return self._json(400, {"error": "need object_id"})
                r = {"id": _id("rtn"), "object_id": b["object_id"], "name": b.get("name") or "Routine",
                     "start": b["start"], "end": b["end"], "tz": b.get("tz"), "last_status": "pending", "created": _now()}
                h["routines"][r["id"]] = r
                return self._json(200, {"routine": r})
            if path.startswith("/api/routines/") and method == "DELETE":
                h["routines"].pop(path.split("/")[3], None)
                return self._json(200, {"ok": True})
            if path.startswith("/api/routines/") and path.endswith("/check") and method == "POST":
                rid = path.split("/")[3]
                if rid not in h["routines"]:
                    return self._json(404, {"error": "routine not found"})
                job = self._new_job(hid, "Routine check", "routine")
                threading.Thread(target=run_routine_job, args=(hid, job["id"], rid, base), daemon=True).start()
                return self._json(200, {"job_id": job["id"]})
            if path.startswith("/api/approvals/") and method == "POST":
                a = h["approvals"].get(path.split("/")[3])
                if a is None:
                    return self._json(404, {"error": "approval not found"})
                if a["status"] != "pending":
                    return self._json(409, {"error": f"already {a['status']}"})
                decision = self._body().get("decision")
                a["status"] = "approved" if decision == "approve" else "denied"
                STORE.debug["approvals"].append([a["id"], a["status"]])
                if decision == "approve":
                    h["alerts"].append({"id": _id("alr"), "kind": "caregiver_notified", "ts": _now(),
                                        "message": f"Carer told: {a['message']}", "objects": []})
                return self._json(200, {"approval": a})
        return self._json(404, {"error": "not found"})

    def _new_job(self, hid, question, mode):
        job = {"id": _id("job"), "status": "running", "question": question, "mode": mode, "created": _now(),
               "steps": [], "answer": None, "object_id": ""}
        STORE.homes[hid]["jobs"][job["id"]] = job
        return job

    def _command(self, hid, did, kind, args):
        cmd = {"id": _id("cmd"), "device": did, "kind": kind, "args": args, "status": "pending", "created": _now()}
        STORE.homes[hid]["commands"].append(cmd)
        return cmd


# ---------------------------------------------------------------- simulated agent

def _step(job, **kw):
    with STORE.lock:
        job["steps"].append({"n": len(job["steps"]) + 1, "ts": _now(), **kw})


def _live_check(hid, obj, base):
    """Ask the home's camera to verify the object (real round trip for browser cameras)."""
    h = STORE.homes[hid]
    with STORE.lock:
        devs = [d for d in h["devices"].values() if _now() - d.get("last_heartbeat", 0) < 60]
        dev = next((d for d in devs if obj.get("device") == d["id"]), devs[0] if devs else None)
        if dev is None or dev.get("demo_live"):
            return {"ok": True, "present": True, "similarity": 0.9, "match_object": 0.9, "zone": obj.get("zone"),
                    "bbox": obj.get("bbox")}, f"{base}/img/closeup"
        cmd = {"id": _id("cmd"), "device": dev["id"], "kind": "verify_object",
               "args": {"object_id": obj["id"], "bbox": obj.get("bbox")}, "status": "pending", "created": _now()}
        h["commands"].append(cmd)
    deadline = _now() + 10
    while _now() < deadline:
        time.sleep(0.3)
        if cmd["status"] == "done":
            res = dict(cmd.get("result") or {})
            res["similarity"] = res.get("match_object")
            return res, (f"{base}/img/{cmd['image_key']}" if cmd.get("image_key") else None)
    return {"error": "camera did not answer in 10 s"}, None


def run_ask_job(hid, job_id, base):
    h = STORE.homes[hid]
    job = h["jobs"][job_id]
    time.sleep(0.4)
    _step(job, kind="thought", text="Let me check my memory.")
    _step(job, kind="tool_call", tool="list_objects", input={})
    with STORE.lock:
        present = sorted([o for o in h["objects"].values() if o.get("status") == "present"], key=lambda o: -o["last_seen"])
        listing = [{"object_id": o["id"], "label": o["label"], "zone": o.get("zone"), "status": o["status"]}
                   for o in h["objects"].values()]
    time.sleep(0.5)
    _step(job, kind="tool_result", tool="list_objects", output={"objects": listing})
    q = job["question"].lower()
    target = next((o for o in present if o["label"].split()[-1] in q or o["label"] in q), present[0] if present else None)
    if target is None:
        time.sleep(0.4)
        answer = "I haven't seen that yet. Put it down where the camera can see it."
        _step(job, kind="tool_call", tool="final_answer", input={"answer": answer, "confidence": "low"})
        _step(job, kind="tool_result", tool="final_answer", output={"ok": True})
        _step(job, kind="answer", text=answer)
        with STORE.lock:
            job.update(status="done", answer=answer, finished=_now())
        return
    _step(job, kind="tool_call", tool="verify_object_live", input={"object_id": target["id"]})
    out, img = _live_check(hid, target, base)
    _step(job, kind="tool_result", tool="verify_object_live", output=out, image_url=img)
    answer = f"Your {target['label']} is on the {target.get('zone') or 'table'}. I just checked with the camera."
    time.sleep(0.4)
    _step(job, kind="tool_call", tool="final_answer", input={"answer": answer, "object_id": target["id"], "confidence": "high"})
    _step(job, kind="tool_result", tool="final_answer", output={"ok": True})
    _step(job, kind="answer", text=answer)
    with STORE.lock:
        job.update(status="done", answer=answer, object_id=target["id"], finished=_now(), model="mock")


def run_routine_job(hid, job_id, rid, base):
    h = STORE.homes[hid]
    job = h["jobs"][job_id]
    r = h["routines"][rid]
    obj = h["objects"].get(r["object_id"], {})
    time.sleep(0.4)
    _step(job, kind="tool_call", tool="verify_object_live", input={"object_id": r["object_id"]})
    time.sleep(0.5)
    _step(job, kind="tool_result", tool="verify_object_live",
          output={"ok": True, "present": True, "similarity": 0.88, "zone": obj.get("zone")}, image_url=f"{base}/img/closeup")
    _step(job, kind="tool_call", tool="speak_on_device", input={"message": "Time for your pills."})
    _step(job, kind="tool_result", tool="speak_on_device", output={"spoken": True})
    _step(job, kind="tool_call", tool="request_caregiver_notification",
          input={"message": f"{r['name']}: not picked up yet.", "reason": "reminder unanswered"})
    with STORE.lock:
        apr = {"id": _id("apr"), "kind": "caregiver_notification", "message": f"{r['name']} was not picked up today.",
               "reason": "The reminder was not answered.", "status": "pending", "created": _now(), "job_id": job_id}
        h["approvals"][apr["id"]] = apr
    _step(job, kind="tool_result", tool="request_caregiver_notification", output={"status": "pending", "approval_id": apr["id"]})
    _step(job, kind="tool_call", tool="record_routine_status", input={"status": "escalated", "note": "asked carer"})
    _step(job, kind="tool_result", tool="record_routine_status", output={"ok": True})
    answer = "The pills weren't picked up, so I reminded you and asked your carer's OK to tell them."
    _step(job, kind="answer", text=answer)
    with STORE.lock:
        r.update(last_status="escalated", last_checked=_now())
        job.update(status="done", answer=answer, object_id=r["object_id"], finished=_now())


def serve(port: int = 0) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    httpd = serve(args.port)
    print(f"mock iCamFind on http://127.0.0.1:{httpd.server_address[1]}/")
    threading.Event().wait()
