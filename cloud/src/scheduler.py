"""EventBridge-scheduled routine checker (every 5 minutes).

Deterministic code decides *when* a routine needs attention; the agent decides *what to do* about it
using live camera evidence.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3

import store

log = logging.getLogger()
log.setLevel(logging.INFO)
AGENT_FUNCTION = os.environ.get("AGENT_FUNCTION", "")
GRACE = float(os.environ.get("ESCALATION_GRACE_SECONDS", "600"))
_lambda = boto3.client("lambda", region_name=store.REGION)


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def needs_check(routine: dict, now: datetime) -> bool:
    today = now.strftime("%Y-%m-%d")
    minutes = now.hour * 60 + now.minute
    if minutes < _minutes(routine["end"]):
        return False  # window still open
    if routine.get("last_day") == today and routine.get("last_status") in ("ok", "escalated", "missed"):
        return False  # already settled today
    if routine.get("last_day") == today and routine.get("last_status") == "reminded":
        return time.time() - routine.get("reminded_at", 0) >= GRACE  # time to escalate?
    return True


def taken_in_window(routine: dict, now: datetime) -> bool:
    tz = ZoneInfo(routine.get("tz", "UTC"))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + _minutes(routine["start"]) * 60
    end = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + _minutes(routine["end"]) * 60
    for e in store.object_events(routine["object_id"], 30):
        if start <= e["ts"] <= end and e["type"] in ("removed", "moved"):
            return True
    return False


def handler(event, context):
    started = 0
    for routine in store.all_routines():
        try:
            store.set_household(routine["household"])
            tz = ZoneInfo(routine.get("tz", "UTC"))
            now = datetime.now(tz)
            if not needs_check(routine, now):
                continue
            if taken_in_window(routine, now):
                routine.update(last_status="ok", last_day=now.strftime("%Y-%m-%d"), last_checked=time.time(),
                               last_note="pick-up detected during the window")
                store.put_routine(routine)
                continue
            job = store.create_job(f"Routine check: {routine['name']}", "routine", {"routine_id": routine["id"]})
            _lambda.invoke(FunctionName=AGENT_FUNCTION, InvocationType="Event",
                           Payload=json.dumps({"job": job, "tz": routine.get("tz")}).encode())
            started += 1
        except Exception:
            log.exception("routine %s failed", routine.get("id"))
    log.info("routine sweep started %d agent runs", started)
    return {"started": started}
