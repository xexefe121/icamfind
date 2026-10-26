"""HTTP API for edge devices and signed-in users (API Gateway HTTP API, payload format 2.0).

Auth:
  * /api/device/*  edge devices, `x-icamfind-device-token` (hash lookup -> household + device)
  * /api/*         people, Cognito ID token verified by the API Gateway JWT authorizer; the household
                   comes from `x-icamfind-household` and must be one the user belongs to
  * static files and /config.json are public
"""

from __future__ import annotations

import base64
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

AGENT_FUNCTION = os.environ.get("AGENT_FUNCTION", "")
COGNITO_DOMAIN = os.environ.get("COGNITO_DOMAIN", "")
CLIENT_ID = os.environ.get("USER_POOL_CLIENT_ID", "")
API_URL = os.environ.get("API_URL", "")
DEMO_DEVICE_TOKEN_SHA256 = os.environ.get("DEMO_DEVICE_TOKEN_SHA256", "")
WEB_DIR = Path(__file__).parent / "web"
_lambda = boto3.client("lambda", region_name=store.REGION)

STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "application/javascript"), "/style.css": ("style.css", "text/css")}
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff", "referrer-policy": "no-referrer", "x-frame-options": "DENY",
    "content-security-policy": "default-src 'self'; img-src 'self' https: data: blob:; "
                               "style-src 'self' 'unsafe-inline'; "
                               f"connect-src 'self' https://*.amazoncognito.com; frame-ancestors 'none'",
}


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


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


# ------------------------------------------------------------------ device routes

def post_event(device: dict, body: dict) -> dict:
    ev = body["event"]
    crop_key = None
    if body.get("crop_b64"):
        crop_key = store.put_image(f"crops/{time.strftime('%Y/%m/%d')}/{ev['id']}.jpg", body["crop_b64"])
    obj = store.get_object(ev["object_id"]) or {
        "id": ev["object_id"], "label": ev.get("label") or ev.get("local_label") or "object",
        "local_label": ev.get("local_label", "object"), "description": "", "first_seen": ev["ts"],
        "essential": bool(ev.get("essential")), "device": device["id"],
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
    store.put_event({**ev, "label": obj["label"], "crop_key": crop_key, "device": device["id"]})
    return _resp(200, {"object": _with_urls(obj)})


def post_heartbeat(device: dict, body: dict) -> dict:
    record = store.get_device(device["id"]) or {"id": device["id"], "name": device.get("name", "Camera")}
    record.update(last_heartbeat=time.time(), zones=body.get("zones", []), door_zone=body.get("door_zone"),
                  frame_size=body.get("frame_size"), map=body.get("map"))
    if body.get("snapshot_b64"):
        record["snapshot_key"] = store.put_image(f"snapshots/{device['id']}/{int(time.time())}.jpg",
                                                 body["snapshot_b64"])
        record["snapshot_at"] = time.time()
    store.put_device(record)
    return _resp(200, {"ok": True})


def post_result(device: dict, command_id: str, body: dict) -> dict:
    image_key = store.put_image(f"commands/{command_id}.jpg", body["crop_b64"]) if body.get("crop_b64") else None
    if not store.complete_command(command_id, device["id"], body.get("result", {}), image_key):
        raise HttpError(404, "command not found")
    return _resp(200, {"ok": True})


def post_alert(device: dict, body: dict) -> dict:
    store.put_alert({"id": store.new_id("alr"), "kind": body.get("kind", "alert"), "message": body.get("message", ""),
                     "ts": body.get("ts") or time.time(), "objects": body.get("objects", []), "device": device["id"]})
    return _resp(200, {"ok": True})


DEVICE_ROUTES = [
    ("POST", r"/api/device/events", lambda d, m, b: post_event(d, b)),
    ("POST", r"/api/device/heartbeat", lambda d, m, b: post_heartbeat(d, b)),
    ("GET", r"/api/device/commands", lambda d, m, b: _resp(200, {"commands": store.pending_commands(d["id"])})),
    ("POST", r"/api/device/commands/(cmd_\w+)/result", lambda d, m, b: post_result(d, m[1], b)),
    ("POST", r"/api/device/alerts", lambda d, m, b: post_alert(d, b)),
]


def authenticate_device(event) -> dict:
    token = (event.get("headers") or {}).get("x-icamfind-device-token", "")
    device = store.device_for_token(token)
    if device is None and token and DEMO_DEVICE_TOKEN_SHA256 and store.token_hash(token) == DEMO_DEVICE_TOKEN_SHA256:
        store.register_device_token(DEMO_DEVICE_TOKEN_SHA256, store.DEMO_HOUSEHOLD, "dev_demo_replay",
                                    "Demo replay camera")
        device = store.device_for_token(token)
    if device is None:
        raise HttpError(401, "unauthorized")
    store.set_household(device["household"])
    return device


# ------------------------------------------------------------------ user routes

def get_state(ctx: dict) -> dict:
    import agent

    devices = sorted(store.list_devices(), key=lambda d: d.get("last_heartbeat", 0), reverse=True)
    for d in devices:
        d["online"] = agent.device_online(d)
    device = dict(devices[0]) if devices else None
    if device:
        device["snapshot_url"] = store.url(device.pop("snapshot_key", None))
    return _resp(200, {
        "household": ctx["household"],
        "device": device,
        "devices": [{k: d.get(k) for k in ("id", "name", "online", "last_heartbeat")} for d in devices],
        "objects": [_with_urls(o) for o in store.list_objects()],
        "alerts": store.list_alerts(20),
        "approvals": store.list_approvals("pending"),
        "routines": store.list_routines(),
    })


def get_me(ctx: dict) -> dict:
    user = ctx["user"]
    households = []
    for hid in user.get("households", []):
        hh = store.get_household(hid)
        if hh:
            households.append({"id": hid, "name": hh.get("name"), "demo": bool(hh.get("demo")),
                               "role": "owner" if hid in user.get("owner_of", []) else "member"})
    return _resp(200, {"user": {"sub": user["sub"], "email": user.get("email")}, "households": households,
                       "current": ctx["household"]["id"]})


def get_job(job_id: str) -> dict:
    job, steps = store.get_job(job_id)
    if job is None:
        raise HttpError(404, "job not found")
    for s in steps:
        s["image_url"] = store.url(s.pop("image_key", None))
    job.pop("household", None)
    return _resp(200, {"job": job, "steps": steps})


def post_ask(body: dict) -> dict:
    question = (body.get("question") or "").strip()[:500]
    if not question:
        raise HttpError(400, "question required")
    job = store.create_job(question, "ask")
    _start_agent(job, body.get("tz"))
    return _resp(200, {"job_id": job["id"]})


def post_snapshot() -> dict:
    import agent

    device = agent._device()
    if not agent.device_online(device):
        raise HttpError(409, "camera offline")
    cmd = store.create_command(device["id"], "snapshot", {})
    return _resp(200, {"job_id": cmd["id"]})


def post_object(object_id: str, body: dict) -> dict:
    obj = store.get_object(object_id)
    if obj is None:
        raise HttpError(404, "object not found")
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
        raise HttpError(404, "object not found")
    return _resp(200, {"object": _with_urls(obj), "events": [_event_out(e) for e in store.object_events(object_id)]})


TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def post_routine(body: dict) -> dict:
    from zoneinfo import ZoneInfo

    start, end, tz = body.get("start", ""), body.get("end", ""), body.get("tz") or "UTC"
    if not (TIME_RE.match(start) and TIME_RE.match(end)) or not store.get_object(body.get("object_id", "")):
        raise HttpError(400, "need object_id and start/end as HH:MM")
    try:
        ZoneInfo(tz)
    except Exception:
        raise HttpError(400, "unknown time zone")
    routine = {"id": store.new_id("rtn"), "object_id": body["object_id"],
               "name": (body.get("name") or "Daily routine")[:80], "start": start, "end": end, "tz": tz,
               "last_status": "pending", "created": time.time()}
    return _resp(200, {"routine": store.put_routine(routine)})


def delete_routine(routine_id: str) -> dict:
    if not store.delete_routine(routine_id):
        raise HttpError(404, "routine not found")
    return _resp(200, {"ok": True})


def check_routine(routine_id: str) -> dict:
    routine = store.get_routine(routine_id)
    if routine is None:
        raise HttpError(404, "routine not found")
    job = store.create_job(f"Routine check: {routine['name']}", "routine", {"routine_id": routine_id})
    _start_agent(job, routine.get("tz"))
    return _resp(200, {"job_id": job["id"]})


def post_approval(approval_id: str, body: dict) -> dict:
    import agent

    approval = store.get_approval(approval_id)
    if approval is None:
        raise HttpError(404, "approval not found")
    if approval["status"] != "pending":
        raise HttpError(409, f"already {approval['status']}")
    decision = body.get("decision")
    if decision not in ("approve", "deny"):
        raise HttpError(400, "decision must be approve or deny")
    approval["status"] = "approved" if decision == "approve" else "denied"
    approval["decided"] = time.time()
    if decision == "approve":
        agent.send_caregiver(approval["message"])
        store.put_alert({"id": store.new_id("alr"), "kind": "caregiver_notified", "ts": time.time(),
                         "message": f"Caregiver notified: {approval['message']}", "objects": []})
    store.put_approval(approval)
    return _resp(200, {"approval": approval})


def get_devices() -> dict:
    import agent

    devices = store.list_devices()
    return _resp(200, {"devices": [{"id": d["id"], "name": d.get("name"), "online": agent.device_online(d),
                                    "last_heartbeat": d.get("last_heartbeat")} for d in devices]})


def post_device(ctx: dict, body: dict) -> dict:
    hh = ctx["household"]
    if hh.get("demo") or hh["id"] not in ctx["user"].get("owner_of", []):
        raise HttpError(403, "only the owner of a private home can add cameras")
    if len(store.list_devices()) >= 5:
        raise HttpError(409, "a home can have at most 5 cameras")
    device, token = store.create_device(str(body.get("name") or "Camera"))
    command = (f"ICAMFIND_API_URL={API_URL} ICAMFIND_DEVICE_TOKEN={token} python -m icamfind run")
    return _resp(200, {"device": {"id": device["id"], "name": device["name"]}, "token": token,
                       "run_command": command,
                       "run_command_powershell": f"$env:ICAMFIND_API_URL='{API_URL}'; "
                                                 f"$env:ICAMFIND_DEVICE_TOKEN='{token}'; python -m icamfind run"})


USER_ROUTES = [
    ("GET", r"/api/me", lambda c, m, b: get_me(c)),
    ("GET", r"/api/state", lambda c, m, b: get_state(c)),
    ("GET", r"/api/objects/(obj_\w+)", lambda c, m, b: get_object(m[1])),
    ("POST", r"/api/objects/(obj_\w+)", lambda c, m, b: post_object(m[1], b)),
    ("POST", r"/api/ask", lambda c, m, b: post_ask(b)),
    ("GET", r"/api/jobs/(job_\w+)", lambda c, m, b: get_job(m[1])),
    ("POST", r"/api/snapshot", lambda c, m, b: post_snapshot()),
    ("POST", r"/api/routines", lambda c, m, b: post_routine(b)),
    ("DELETE", r"/api/routines/(rtn_\w+)", lambda c, m, b: delete_routine(m[1])),
    ("POST", r"/api/routines/(rtn_\w+)/check", lambda c, m, b: check_routine(m[1])),
    ("POST", r"/api/approvals/(apr_\w+)", lambda c, m, b: post_approval(m[1], b)),
    ("GET", r"/api/devices", lambda c, m, b: get_devices()),
    ("POST", r"/api/devices", lambda c, m, b: post_device(c, b)),
]


def authenticate_user(event) -> dict:
    claims = (event.get("requestContext", {}).get("authorizer", {}) or {}).get("jwt", {}).get("claims") or {}
    sub = claims.get("sub")
    if not sub:
        raise HttpError(401, "unauthorized")  # the JWT authorizer normally rejects before we get here
    user = store.ensure_user(sub, claims.get("email", ""))
    requested = (event.get("headers") or {}).get("x-icamfind-household") or user["households"][0]
    if requested not in user.get("households", []):
        raise HttpError(403, "not a member of that home")
    household = store.get_household(requested)
    if household is None:
        raise HttpError(404, "home not found")
    store.set_household(requested)
    return {"user": user, "household": {"id": requested, "name": household.get("name"),
                                        "demo": bool(household.get("demo"))}}


# ------------------------------------------------------------------ entry point

def _dispatch(routes, method, path, *args):
    for m_, pattern, fn in routes:
        if m_ == method:
            match = re.fullmatch(pattern, path)
            if match:
                return fn(*args[:1], match, *args[1:])
    raise HttpError(404, "not found")


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")
    if method == "GET" and path in STATIC:
        name, ctype = STATIC[path]
        return {"statusCode": 200, "headers": {"content-type": ctype, "cache-control": "no-cache", **SECURITY_HEADERS},
                "body": (WEB_DIR / name).read_text(encoding="utf-8")}
    if method == "GET" and path == "/config.json":
        return _resp(200, {"cognito_domain": COGNITO_DOMAIN, "client_id": CLIENT_ID, "redirect_uri": f"{API_URL}/",
                           "region": store.REGION}, {"cache-control": "max-age=300"})
    if not path.startswith("/api/"):
        return _resp(404, {"error": "not found"})

    try:
        body = _json(event) if method in ("POST", "PUT") else {}
        if path.startswith("/api/device/"):
            device = authenticate_device(event)
            return _dispatch(DEVICE_ROUTES, method, path, device, body)
        if method == "GET" and path == "/api/events":
            authenticate_user(event)
            qs = event.get("queryStringParameters") or {}
            limit = max(1, min(200, int(qs.get("limit", 50))))
            return _resp(200, {"events": [_event_out(e) for e in store.recent_events(limit)]})
        ctx = authenticate_user(event)
        return _dispatch(USER_ROUTES, method, path, ctx, body)
    except HttpError as exc:
        return _resp(exc.status, {"error": str(exc)})
    except (KeyError, ValueError, TypeError) as exc:
        log.warning("bad request %s %s: %s", method, path, exc)
        return _resp(400, {"error": f"bad request: {exc}"})
    except Exception:
        log.exception("unhandled error %s %s", method, path)
        return _resp(500, {"error": "internal error"})
