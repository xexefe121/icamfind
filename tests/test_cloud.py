"""Cloud handlers that run without AWS calls: auth, static web app, routine scheduling rules."""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ.setdefault("AWS_REGION", "ap-southeast-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ACCESS_TOKEN", "test-token-0123456789abcdef")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud" / "src"))

import api  # noqa: E402
import scheduler  # noqa: E402


def _event(method, path, token=None):
    headers = {"x-icamfind-token": token} if token else {}
    return {"requestContext": {"http": {"method": method}}, "rawPath": path, "headers": headers}


def test_api_requires_token():
    res = api.handler(_event("GET", "/api/state"), None)
    assert res["statusCode"] == 401
    res = api.handler(_event("GET", "/api/state", "wrong"), None)
    assert res["statusCode"] == 401


def test_web_app_is_served_with_security_headers():
    res = api.handler(_event("GET", "/"), None)
    assert res["statusCode"] == 200
    assert "<html" in res["body"].lower()
    assert res["headers"]["x-frame-options"] == "DENY"


def test_unknown_route_is_404_after_auth():
    res = api.handler(_event("GET", "/api/nope", os.environ["ACCESS_TOKEN"]), None)
    assert res["statusCode"] == 404
    assert json.loads(res["body"])["error"] == "not found"


def test_routine_needs_check_rules():
    tz = ZoneInfo("Australia/Brisbane")
    routine = {"start": "08:00", "end": "09:00", "tz": "Australia/Brisbane"}
    assert not scheduler.needs_check(routine, datetime(2026, 10, 27, 8, 30, tzinfo=tz))  # window open
    assert scheduler.needs_check(routine, datetime(2026, 10, 27, 9, 5, tzinfo=tz))
    settled = {**routine, "last_day": "2026-10-27", "last_status": "ok"}
    assert not scheduler.needs_check(settled, datetime(2026, 10, 27, 9, 5, tzinfo=tz))
    reminded = {**routine, "last_day": "2026-10-27", "last_status": "reminded", "reminded_at": 0}
    assert scheduler.needs_check(reminded, datetime(2026, 10, 27, 9, 30, tzinfo=tz))
