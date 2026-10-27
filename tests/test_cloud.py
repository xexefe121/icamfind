"""Cloud handlers that run without AWS calls: auth, static web app, routine scheduling rules."""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ.setdefault("AWS_REGION", "ap-southeast-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud" / "src"))

import api  # noqa: E402
import scheduler  # noqa: E402


def _event(method, path, headers=None, claims=None):
    ctx = {"http": {"method": method}}
    if claims:
        ctx["authorizer"] = {"jwt": {"claims": claims}}
    return {"requestContext": ctx, "rawPath": path, "headers": headers or {}}


def test_user_routes_require_a_verified_identity():
    res = api.handler(_event("GET", "/api/state"), None)
    assert res["statusCode"] == 401


def test_device_routes_require_a_device_token():
    res = api.handler(_event("POST", "/api/device/heartbeat"), None)
    assert res["statusCode"] == 401
    assert json.loads(res["body"])["error"] == "unauthorized"


def test_web_app_is_served_with_security_headers():
    res = api.handler(_event("GET", "/"), None)
    assert res["statusCode"] == 200
    assert "<html" in res["body"].lower()
    assert res["headers"]["x-frame-options"] == "DENY"
    assert "amazoncognito.com" in res["headers"]["content-security-policy"]


def test_public_config_exposes_only_sign_in_settings():
    res = api.handler(_event("GET", "/config.json"), None)
    body = json.loads(res["body"])
    assert res["statusCode"] == 200
    assert set(body) == {"cognito_domain", "client_id", "redirect_uri", "region"}


def test_routine_needs_check_rules():
    tz = ZoneInfo("Australia/Brisbane")
    routine = {"start": "08:00", "end": "09:00", "tz": "Australia/Brisbane"}
    assert not scheduler.needs_check(routine, datetime(2026, 10, 27, 8, 30, tzinfo=tz))  # window open
    assert scheduler.needs_check(routine, datetime(2026, 10, 27, 9, 5, tzinfo=tz))
    settled = {**routine, "last_day": "2026-10-27", "last_status": "ok"}
    assert not scheduler.needs_check(settled, datetime(2026, 10, 27, 9, 5, tzinfo=tz))
    reminded = {**routine, "last_day": "2026-10-27", "last_status": "reminded", "reminded_at": 0}
    assert scheduler.needs_check(reminded, datetime(2026, 10, 27, 9, 30, tzinfo=tz))


def test_person_crops_are_ignored_not_remembered(monkeypatch):
    import base64

    import llm

    saved, events = {}, []
    monkeypatch.setattr(api.store, "put_image", lambda key, data_b64=None, data=None: key)
    monkeypatch.setattr(api.store, "get_object", lambda oid: saved.get(oid))
    monkeypatch.setattr(api.store, "put_object", lambda obj: saved.__setitem__(obj["id"], dict(obj)) or obj)
    monkeypatch.setattr(api.store, "put_event", lambda ev: events.append(ev) or ev)
    monkeypatch.setattr(api.store, "url", lambda key, expires=3600: key)
    kind = {"value": "person"}
    monkeypatch.setattr(llm, "describe_crop", lambda jpeg, local: {
        "label": "face", "description": "", "essential": False, "kind": kind["value"]})
    crop = base64.b64encode(b"jpeg").decode()

    def post(oid, typ):
        body = {"event": {"id": f"ev_{oid}_{typ}", "type": typ, "object_id": oid, "ts": 1.0, "zone": "desk",
                          "bbox": [1, 2, 3, 4]}, "crop_b64": crop}
        return json.loads(api.post_event({"id": "dev_1"}, body)["body"])

    assert post("obj_face", "placed") == {"object": None, "ignored": True}
    assert post("obj_face", "removed") == {"object": None, "ignored": True}
    assert saved["obj_face"]["ignored"] and not events

    kind["value"] = "background"  # advisory only: plain items are easily mistaken for wall texture
    assert post("obj_box", "placed")["object"]["status"] == "present"
    assert len(events) == 1
