"""The iCamFind agent: a perception -> decision -> action loop over remembered and live camera evidence.

Claude (on Amazon Bedrock) decides which tool to use next. The tools are backed by the OpenCV 5
pipeline on the home device: memory lookups read events it recorded, and the live tools make the
device look again (patch verification, ORB+RANSAC / template search across the whole frame) and
return a fresh, privacy-filtered crop that Claude sees as an image. What the camera reports changes
what the agent does next: a verified object is answered immediately, a missing one triggers a room
scan, a found one updates memory, and a missed medication routine escalates to a caregiver only
through a policy enforced in code (human approval, or a reminder that went unanswered).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3

import llm
import store

log = logging.getLogger("icamfind.agent")
SNS_TOPIC = os.environ.get("TOPIC_ARN", "")
ESCALATION_GRACE = float(os.environ.get("ESCALATION_GRACE_SECONDS", "600"))
DEVICE_ONLINE_SECONDS = 60
MAX_TURNS = 10
_sns = boto3.client("sns", region_name=store.REGION)

SYSTEM = """You are iCamFind, a calm memory assistant for people who misplace things, including older adults \
and people living with dementia or ADHD, and their caregivers. A home camera running OpenCV records when \
objects are put down, picked up and moved, in named zones (desk, shelf, ...). You can also ask the camera \
to look again right now.

How to work:
- Start from memory: call list_objects, and get_object_history when timing matters.
- Before stating where something is, confirm with the live camera when it is online: verify_object_live at \
the remembered place. If it is not there (or the camera says the surface looks empty), call \
scan_room_for_object. If memory says it was picked up, scan the room once in case it was put back somewhere \
else. If the camera is offline, answer from memory and say when it was last seen.
- If live evidence contradicts memory, call update_object_status so memory stays truthful.
- You can see the camera crops that tools return. Use them; do not claim to see things you did not see.
- Never invent locations or times. Use the zone names and times the tools give you, in plain words \
("on the desk, about 20 minutes ago").
- Notifying a caregiver is a serious action: only for routines or safety, never for an ordinary question \
unless the user asks. request_caregiver_notification returns whether it was sent or is waiting for approval.
- Finish by calling final_answer with one or two short, warm sentences a tired or anxious person can act on. \
No emoji. Include the object_id when the answer is about one object."""

TOOLS = [
    {"name": "list_objects", "description": "All remembered objects with label, description, status "
     "(present = on a surface / removed = picked up), zone and when last seen.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_object_history", "description": "Recent events for one object (placed/removed/moved with "
     "zones and times), newest first.",
     "input_schema": {"type": "object", "properties": {"object_id": {"type": "string"},
                                                       "limit": {"type": "integer"}},
                      "required": ["object_id"], "additionalProperties": False}},
    {"name": "verify_object_live", "description": "Ask the home camera to look right now at where the object "
     "was last seen. Returns present true/false with match scores and a fresh crop image.",
     "input_schema": {"type": "object", "properties": {"object_id": {"type": "string"}},
                      "required": ["object_id"], "additionalProperties": False}},
    {"name": "scan_room_for_object", "description": "Ask the camera to search the whole current view for the "
     "object's appearance (feature matching). Use when it is not where memory says. Returns found, zone, "
     "confidence and an image.",
     "input_schema": {"type": "object", "properties": {"object_id": {"type": "string"}},
                      "required": ["object_id"], "additionalProperties": False}},
    {"name": "update_object_status", "description": "Correct memory after live evidence contradicts it.",
     "input_schema": {"type": "object", "properties": {
         "object_id": {"type": "string"}, "status": {"type": "string", "enum": ["present", "removed"]},
         "zone": {"type": "string"}, "reason": {"type": "string"}},
         "required": ["object_id", "status", "reason"], "additionalProperties": False}},
    {"name": "speak_on_device", "description": "Say a short message out loud through the home device "
     "(reminders).",
     "input_schema": {"type": "object", "properties": {"message": {"type": "string"}},
                      "required": ["message"], "additionalProperties": False}},
    {"name": "request_caregiver_notification", "description": "Notify the caregiver by email. Policy is "
     "enforced by the system: it is sent directly only for an escalating routine whose reminder went "
     "unanswered; otherwise it waits for human approval in the app.",
     "input_schema": {"type": "object", "properties": {"message": {"type": "string"},
                                                       "reason": {"type": "string"}},
                      "required": ["message", "reason"], "additionalProperties": False}},
    {"name": "record_routine_status", "description": "Routine runs only: record the outcome of this check.",
     "input_schema": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["ok", "reminded", "escalated", "missed"]},
         "note": {"type": "string"}}, "required": ["status", "note"], "additionalProperties": False}},
    {"name": "final_answer", "description": "Give the final answer to the user and stop.",
     "input_schema": {"type": "object", "properties": {
         "answer": {"type": "string"}, "object_id": {"type": "string"},
         "confidence": {"type": "string", "enum": ["high", "medium", "low"]}},
         "required": ["answer", "confidence"], "additionalProperties": False}},
]


def _ago(ts: float | None) -> str:
    if not ts:
        return "unknown"
    s = max(0, time.time() - ts)
    if s < 90:
        return f"{int(s)} seconds ago"
    if s < 5400:
        return f"{int(s // 60)} minutes ago"
    if s < 172800:
        return f"{s / 3600:.1f} hours ago"
    return f"{int(s // 86400)} days ago"


def _device() -> dict | None:
    devices = sorted(store.list_devices(), key=lambda d: d.get("last_heartbeat", 0), reverse=True)
    return devices[0] if devices else None


def device_online(device: dict | None) -> bool:
    return bool(device) and time.time() - device.get("last_heartbeat", 0) < DEVICE_ONLINE_SECONDS


class Run:
    def __init__(self, job: dict, local_tz: str = "UTC"):
        self.job = job
        self.n = 0
        self.local_tz = local_tz
        self.final: dict | None = None
        self.routine = store.get_routine(job["routine_id"]) if job.get("routine_id") else None

    def step(self, **step) -> None:
        self.n += 1
        store.add_step(self.job["id"], self.n, step)

    # ------------------------------------------------------------- tools

    def _live(self, kind: str, object_id: str) -> tuple[dict, bytes | None]:
        device = _device()
        if not device_online(device):
            return {"error": "camera offline", "last_heartbeat": _ago(device.get("last_heartbeat") if device else None)}, None
        obj = store.get_object(object_id)
        if obj is None:
            return {"error": f"unknown object_id {object_id}"}, None
        cmd = store.create_command(device["id"], kind, {"object_id": object_id, "bbox": obj.get("bbox")})
        deadline = time.time() + 25
        while time.time() < deadline:
            time.sleep(0.8)
            done = store.get_command(cmd["id"])
            if done and done.get("status") == "done":
                image = store.get_image(done["image_key"]) if done.get("image_key") else None
                return done.get("result", {}), image
        return {"error": "camera did not answer in 25 s"}, None

    def tool(self, name: str, args: dict) -> tuple[dict, bytes | None]:
        if name == "list_objects":
            objs = store.list_objects()
            return {"objects": [{
                "object_id": o["id"], "label": o.get("label"), "description": o.get("description", ""),
                "status": o.get("status"), "zone": o.get("zone"), "last_seen": _ago(o.get("last_seen")),
                "last_event": o.get("last_event"), "essential": o.get("essential", False)} for o in objs],
                "now": datetime.now(ZoneInfo(self.local_tz)).strftime("%A %H:%M")}, None
        if name == "get_object_history":
            events = store.object_events(args["object_id"], int(args.get("limit") or 10))
            return {"events": [{"type": e["type"], "zone": e.get("zone"), "from_zone": e.get("from_zone"),
                                "when": _ago(e.get("ts")),
                                "local_time": datetime.fromtimestamp(e["ts"], ZoneInfo(self.local_tz)).strftime("%a %H:%M"),
                                "note": e.get("note", "")} for e in events]}, None
        if name == "verify_object_live":
            res, image = self._live("verify_object", args["object_id"])
            if "error" not in res:
                res["similarity"] = res.get("match_object")
                if res.get("present"):
                    obj = store.get_object(args["object_id"])
                    if obj:
                        obj["last_seen"] = time.time()
                        obj["last_event"] = "verified"
                        store.put_object(obj)
            return res, image
        if name == "scan_room_for_object":
            res, image = self._live("scan_for_object", args["object_id"])
            if "error" not in res:
                res["confidence"] = res.get("score")
            return res, image
        if name == "update_object_status":
            obj = store.get_object(args["object_id"])
            if obj is None:
                return {"error": "unknown object"}, None
            from_zone = obj.get("zone")
            obj["status"] = args["status"]
            if args.get("zone"):
                obj["zone"] = args["zone"]
            obj["last_event"] = "corrected"
            obj["last_seen"] = time.time()
            store.put_object(obj)
            store.put_event({"id": store.new_id("ev"), "object_id": obj["id"], "type": "corrected",
                             "ts": time.time(), "zone": obj.get("zone"), "from_zone": from_zone,
                             "label": obj.get("label"), "note": args.get("reason", "")})
            return {"ok": True, "object_id": obj["id"], "status": obj["status"], "zone": obj.get("zone")}, None
        if name == "speak_on_device":
            device = _device()
            if not device_online(device):
                return {"spoken": False, "error": "device offline"}, None
            store.create_command(device["id"], "speak", {"text": args["message"][:300]})
            if self.routine:
                self.routine["reminded_at"] = time.time()
                store.put_routine(self.routine)
            return {"spoken": True}, None
        if name == "request_caregiver_notification":
            return self._notify(args["message"], args["reason"]), None
        if name == "record_routine_status":
            if not self.routine:
                return {"error": "not a routine run"}, None
            self.routine["last_status"] = args["status"]
            self.routine["last_checked"] = time.time()
            self.routine["last_note"] = args["note"]
            self.routine["last_day"] = datetime.now(ZoneInfo(self.routine.get("tz", "UTC"))).strftime("%Y-%m-%d")
            store.put_routine(self.routine)
            return {"ok": True}, None
        if name == "final_answer":
            self.final = args
            return {"ok": True}, None
        return {"error": f"unknown tool {name}"}, None

    def _notify(self, message: str, reason: str) -> dict:
        r = self.routine
        escalation_allowed = bool(
            r and r.get("reminded_at") and time.time() - r["reminded_at"] >= ESCALATION_GRACE
            and r.get("escalated_day") != datetime.now(ZoneInfo(r.get("tz", "UTC"))).strftime("%Y-%m-%d"))
        if escalation_allowed:
            send_caregiver(message)
            r["escalated_day"] = datetime.now(ZoneInfo(r.get("tz", "UTC"))).strftime("%Y-%m-%d")
            store.put_routine(r)
            return {"status": "sent", "policy": "routine reminder unanswered past grace period"}
        approval = store.put_approval({"id": store.new_id("apr"), "kind": "caregiver_notification",
                                       "message": message, "reason": reason, "status": "pending",
                                       "created": time.time(), "job_id": self.job["id"]})
        return {"status": "pending", "approval_id": approval["id"],
                "policy": "needs human approval in the app (no unanswered reminder on record)"}

    # ------------------------------------------------------------- loop

    def run(self, question: str) -> None:
        messages = [{"role": "user", "content": question}]
        for _ in range(MAX_TURNS):
            response = llm.create(
                max_tokens=4000, system=SYSTEM, tools=TOOLS, messages=messages,
                output_config={"effort": llm.EFFORT} if llm.EFFORT else None,
            )
            if response.get("stop_reason") == "refusal":
                self.step(kind="error", text="The model declined this request.")
                break
            content = response.get("content", [])
            messages.append({"role": "assistant", "content": content})
            for block in content:
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.step(kind="thought", text=block["text"].strip())
            calls = [b for b in content if b.get("type") == "tool_use"]
            if not calls:
                text = llm.text_of(response)
                self.final = self.final or {"answer": text or "I could not work that out.", "confidence": "low"}
                break
            results = []
            for call in calls:
                name = call["name"]
                args = call.get("input") if isinstance(call.get("input"), dict) else {}
                self.step(kind="tool_call", tool=name, input=args)
                try:
                    output, image = self.tool(name, args)
                except Exception as exc:  # tool failure is reported to the model, not fatal
                    log.exception("tool %s failed", name)
                    output, image = {"error": str(exc)}, None
                image_key = None
                if image:
                    image_key = store.put_image(f"jobs/{self.job['id']}/{self.n + 1}.jpg", data=image)
                self.step(kind="tool_result", tool=name, output=output, image_key=image_key)
                result_content = [{"type": "text", "text": json.dumps(output, default=str)}]
                if image:
                    result_content.append({"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(image).decode()}})
                results.append({"type": "tool_result", "tool_use_id": call["id"], "content": result_content,
                                **({"is_error": True} if "error" in output else {})})
            messages.append({"role": "user", "content": results})
            if self.final:
                break
        final = self.final or {"answer": "I ran out of steps before I was sure. Please try again.", "confidence": "low"}
        self.step(kind="answer", text=final["answer"])
        store.update_job(self.job["id"], status="done", answer=final["answer"],
                         object_id=final.get("object_id") or "", confidence=final.get("confidence", "low"),
                         model=llm.active_model(), finished=time.time())


def send_caregiver(message: str) -> None:
    if SNS_TOPIC:
        _sns.publish(TopicArn=SNS_TOPIC, Subject="iCamFind alert", Message=message)


def routine_prompt(routine: dict) -> str:
    tz = ZoneInfo(routine.get("tz", "UTC"))
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def at(hhmm: str) -> float:
        h, m = hhmm.split(":")
        return today + int(h) * 3600 + int(m) * 60

    start, end = at(routine["start"]), at(routine["end"])
    obj = store.get_object(routine["object_id"]) or {}
    events = [e for e in store.object_events(routine["object_id"], 30) if e["ts"] >= today]
    picked = [e for e in events if e["type"] in ("removed", "moved") and start <= e["ts"] <= end]
    now_ts = time.time()
    window = ("has already ENDED for today" if now_ts > end else
              "is OPEN right now" if now_ts >= start else "has not started yet today")
    return (
        f"ROUTINE CHECK (automatic, no user is asking). Routine '{routine['name']}': the user should pick up "
        f"'{obj.get('label', routine['object_id'])}' (object_id {routine['object_id']}) between {routine['start']} "
        f"and {routine['end']} ({routine.get('tz')}). Local time now: {datetime.now(tz).strftime('%H:%M')}; "
        f"today's window {window}.\n"
        f"Events for it today: {json.dumps([{'type': e['type'], 'zone': e.get('zone'), 'time': datetime.fromtimestamp(e['ts'], tz).strftime('%H:%M')} for e in events])}.\n"
        f"Pick-ups detected INSIDE the routine window: {len(picked)} (pick-ups outside the window do not count). "
        f"Reminder already spoken today: "
        f"{'yes, ' + _ago(routine.get('reminded_at')) if routine.get('reminded_at') and routine['reminded_at'] >= today else 'no'}.\n"
        "Decide: if it was clearly taken, record ok. Otherwise look with the camera to confirm it is still "
        "untouched. If untouched and no reminder yet, speak a gentle reminder and record reminded. If a "
        "reminder was already given and it is still untouched, request a caregiver notification and record "
        "escalated. Then give a one-sentence final_answer summarising what you did."
    )


def handler(event, context):
    job = event["job"]
    run = Run(job, local_tz=event.get("tz") or "UTC")
    try:
        question = routine_prompt(run.routine) if job.get("mode") == "routine" and run.routine else job["question"]
        run.run(question)
    except Exception as exc:
        log.exception("agent failed")
        run.step(kind="error", text=f"{type(exc).__name__}: {exc}")
        store.update_job(job["id"], status="error", answer="Something went wrong while checking. Please try again.",
                         finished=time.time())
    return {"ok": True}
