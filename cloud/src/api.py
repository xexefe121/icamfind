"""HTTP API for the edge device and the web app (API Gateway HTTP API, payload format 2.0)."""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import time
from pathlib import Path

import boto3

import store

log = logging.getLogger()
log.setLevel(logging.INFO)

TOKEN = os.environ.get("ACCESS_TOKEN", "")
AGENT_FUNCTION = os.environ.get("AGENT_FUNCTION", "")
WEB_DIR = Path(__file__).parent / "web"
_lambda = boto3.client("lambda", region_name=store.REGION)

STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "application/javascript"), "/style.css": ("style.css", "text/css")}
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff", "referrer-policy": "no-referrer", "x-frame-options": "DENY",
    "content-security-policy": "default-src 'self'; img-src 'self' https: data: blob:; "
                               "style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
}


def _resp(status: int, body, headers: dict | None = None) -> dict:
    return {"statusCode": status, "headers": {"content-type": "application/json", "cache-control": "no-store",
                                               **SECURITY_HEADERS, **(headers or {})},
            "body": json.dumps(body, default=str)}


def _json(event) -> dict:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    return json.loads(raw) if raw else {}


def _with_urls(obj: dict) -> dict:
    if obj is None:
        return obj
    obj = dict(obj)
    obj["crop_url"] = store.url(obj.pop("crop_key", None))
    return obj


def _event_out(e: dict) -> dict:
    e = dict(e)
    e["crop_url"] = store.url(e.pop("crop_key", None))
    return e


def _start_agent(job: dict, tz: str | None = None) -> None:
    _lambda.invoke(FunctionName=AGENT_FUNCTION, InvocationType="Event",
                   Payload=json.dumps({"job": job, "tz": tz}).encode())


# ------------------------------------------------------------------ edge routes

def post_event(body: dict) -> dict:
    device, ev = body["device"], body["event"]
    crop_key = None
    if body.get("crop_b64"):
        crop_key = store.put_image(f"crops/{device}/{time.strftime('%Y/%m/%d')}/{ev['id']}.jpg", body["crop_b64"])
    obj = store.get_object(ev["object_id"]) or {
        "id": ev["object_id"], "label": ev.get("label") or ev.get("local_label") or "object",
        "local_label": ev.get("local_label", "object"), "description": "", "first_seen": ev["ts"],
        "essential": bool(ev.get("essential")), "device": device,
    }
    # Open-vocabulary naming with Claude vision the first time we see an object.
    if crop_key and ev["type"] in ("placed", "removed") and not obj.get("described"):
        try:
            import llm

            info = llm.describe_crop(base64.b64decode(body["crop_b64"]), ev.get("local_label", "object"))
            obj.update(label=info["label"], description=info["description"],
                       essential=bool(info["essential"]) or obj.get("essential", False), described=True)
        except Exception as exc:  # never lose an event because labelling failed
            log.warning("describe_crop failed: %s", exc)
    obj.update(status="removed" if ev["type"] == "removed" else "present", zone=ev.get("zone"),
               bbox=ev.get("bbox"), map_xy=ev.get("map_xy"), last_seen=ev["ts"], last_event=ev["type"])
    if crop_key and (ev["type"] != "removed" or not obj.get("crop_key")):
        obj["crop_key"] = crop_key
    store.put_object(obj)
    store.put_event({**ev, "label": obj["label"], "crop_key": crop_key, "device": device})
    return _resp(200, {"object": _with_urls(obj)})


def post_heartbeat(body: dict) -> dict:
    device = store.get_device(body["device"]) or {"id": body["device"]}
    device.update(last_heartbeat=time.time(), zones=body.get("zones", []), door_zone=body.get("door_zone"),
                  frame_size=body.get("frame_size"), map=body.get("map"))
    if body.get("snapshot_b64"):
        device["snapshot_key"] = store.put_image(f"snapshots/{body['device']}/{int(time.time())}.jpg",
                                                 body["snapshot_b64"])
        device["snapshot_at"] = time.time()
    store.put_device(device)
    return _resp(200, {"ok": True})


def post_result(command_id: str, body: dict) -> dict:
    image_key = None
    if body.get("crop_b64"):
        image_key = store.put_image(f"commands/{command_id}.jpg", body["crop_b64"])
    store.complete_command(command_id, body.get("result", {}), image_key)
    return _resp(200, {"ok": True})


def post_alert(body: dict) -> dict:
    alert = {"id": store.new_id("alr"), "kind": body.get("kind", "alert"), "message": body.get("message", ""),
             "ts": body.get("ts") or time.time(), "objects": body.get("objects", []), "device": body.get("device")}
    store.put_alert(alert)
    return _resp(200, {"ok": True})


# ------------------------------------------------------------------ ui routes

def get_state() -> dict:
    import agent

    devices = sorted(store.list_devices(), key=lambda d: d.get("last_heartbeat", 0), reverse=True)
    device = devices[0] if devices else None
    if device:
        device = dict(device)
        device["online"] = agent.device_online(device)
        device["snapshot_url"] = store.url(device.pop("snapshot_key", None))
    return _resp(200, {
        "device": device,
        "objects": [_with_urls(o) for o in store.list_objects()],
        "alerts": store.list_alerts(20),
        "approvals": store.list_approvals("pending"),
        "routines": store.list_routines(),
    })


def get_job(job_id: str) -> dict:
    job, steps = store.get_job(job_id)
    if job is None:
        return _resp(404, {"error": "job not found"})
    for s in steps:
        s["image_url"] = store.url(s.pop("image_key", None))
    return _resp(200, {"job": job, "steps": steps})


def post_ask(body: dict) -> dict:
    question = (body.get("question") or "").strip()[:500]
    if not question:
        return _resp(400, {"error": "question required"})
    job = store.create_job(question, "ask")
    _start_agent(job, body.get("tz"))
    return _resp(200, {"job_id": job["id"]})


def post_snapshot() -> dict:
    import agent

    device = agent._device()
    if not agent.device_online(device):
        return _resp(409, {"error": "camera offline"})
    cmd = store.create_command(device["id"], "snapshot", {})
    return _resp(200, {"job_id": cmd["id"]})


def post_object(object_id: str, body: dict) -> dict:
    obj = store.get_object(object_id)
    if obj is None:
        return _resp(404, {"error": "object not found"})
    if "label" in body and str(body["label"]).strip():
        obj["label"] = str(body["label"]).strip()[:60]
        obj["described"] = True
    if "essential" in body:
        obj["essential"] = bool(body["essential"])
    store.put_object(obj)
    return _resp(200, {"object": _with_urls(obj)})


def get_object(object_id: str) -> dict:
    obj = store.get_object(object_id)
    if obj is None:
        return _resp(404, {"error": "object not found"})
    return _resp(200, {"object": _with_urls(obj), "events": [_event_out(e) for e in store.object_events(object_id)]})


TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def post_routine(body: dict) -> dict:
    from zoneinfo import ZoneInfo

    start, end, tz = body.get("start", ""), body.get("end", ""), body.get("tz") or "UTC"
    if not (TIME_RE.match(start) and TIME_RE.match(end)) or not store.get_object(body.get("object_id", "")):
        return _resp(400, {"error": "need object_id and start/end as HH:MM"})
    try:
        ZoneInfo(tz)
    except Exception:
        return _resp(400, {"error": "unknown time zone"})
    routine = {"id": store.new_id("rtn"), "object_id": body["object_id"],
               "name": (body.get("name") or "Daily routine")[:80], "start": start, "end": end, "tz": tz,
               "last_status": "pending", "created": time.time()}
    return _resp(200, {"routine": store.put_routine(routine)})


def check_routine(routine_id: str) -> dict:
    routine = store.get_routine(routine_id)
    if routine is None:
        return _resp(404, {"error": "routine not found"})
    job = store.create_job(f"Routine check: {routine['name']}", "routine", {"routine_id": routine_id})
    _start_agent(job, routine.get("tz"))
    return _resp(200, {"job_id": job["id"]})


def post_approval(approval_id: str, body: dict) -> dict:
    import agent

    approval = store.get_approval(approval_id)
    if approval is None:
        return _resp(404, {"error": "approval not found"})
    if approval["status"] != "pending":
        return _resp(409, {"error": f"already {approval['status']}"})
    decision = body.get("decision")
    if decision not in ("approve", "deny"):
        return _resp(400, {"error": "decision must be approve or deny"})
    approval["status"] = "approved" if decision == "approve" else "denied"
    approval["decided"] = time.time()
    if decision == "approve":
        agent.send_caregiver(approval["message"])
    store.put_approval(approval)
    return _resp(200, {"approval": approval})


# ------------------------------------------------------------------ router

ROUTES = [
    ("POST", r"/api/events", lambda m, b: post_event(b)),
    ("POST", r"/api/devices/heartbeat", lambda m, b: post_heartbeat(b)),
    ("GET", r"/api/devices/([\w-]+)/commands", lambda m, b: _resp(200, {"commands": store.pending_commands(m[1])})),
    ("POST", r"/api/commands/(cmd_\w+)/result", lambda m, b: post_result(m[1], b)),
    ("POST", r"/api/alerts", lambda m, b: post_alert(b)),
    ("GET", r"/api/state", lambda m, b: get_state()),
    ("GET", r"/api/events", None),  # handled specially for query string
    ("GET", r"/api/objects/(obj_\w+)", lambda m, b: get_object(m[1])),
    ("POST", r"/api/objects/(obj_\w+)", lambda m, b: post_object(m[1], b)),
    ("POST", r"/api/ask", lambda m, b: post_ask(b)),
    ("GET", r"/api/jobs/(job_\w+)", lambda m, b: get_job(m[1])),
    ("POST", r"/api/snapshot", lambda m, b: post_snapshot()),
    ("POST", r"/api/routines", lambda m, b: post_routine(b)),
    ("DELETE", r"/api/routines/(rtn_\w+)", lambda m, b: (store.delete_routine(m[1]), _resp(200, {"ok": True}))[1]),
    ("POST", r"/api/routines/(rtn_\w+)/check", lambda m, b: check_routine(m[1])),
    ("POST", r"/api/approvals/(apr_\w+)", lambda m, b: post_approval(m[1], b)),
]


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")
    if method == "GET" and path in STATIC:
        name, ctype = STATIC[path]
        return {"statusCode": 200, "headers": {"content-type": ctype, "cache-control": "no-cache", **SECURITY_HEADERS},
                "body": (WEB_DIR / name).read_text(encoding="utf-8")}
    if not path.startswith("/api/"):
        return _resp(404, {"error": "not found"})

    supplied = (event.get("headers") or {}).get("x-icamfind-token", "")
    if not TOKEN or not hmac.compare_digest(supplied.encode(), TOKEN.encode()):
        return _resp(401, {"error": "unauthorized"})

    try:
        body = _json(event) if method in ("POST", "PUT") else {}
        if method == "GET" and path == "/api/events":
            qs = event.get("queryStringParameters") or {}
            limit = max(1, min(200, int(qs.get("limit", 50))))
            return _resp(200, {"events": [_event_out(e) for e in store.recent_events(limit)]})
        for m_, pattern, fn in ROUTES:
            if m_ == method and fn is not None:
                match = re.fullmatch(pattern, path)
                if match:
                    return fn(match, body)
        return _resp(404, {"error": "not found"})
    except (KeyError, ValueError, TypeError) as exc:
        log.warning("bad request %s %s: %s", method, path, exc)
        return _resp(400, {"error": f"bad request: {exc}"})
    except Exception:
        log.exception("unhandled error %s %s", method, path)
        return _resp(500, {"error": "internal error"})
