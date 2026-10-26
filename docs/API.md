# iCamFind Cloud API

All routes are served by one Amazon API Gateway HTTP API backed by the `api` Lambda function.
The web UI (`/`, `/app.js`, `/style.css`) is public static content. Every `/api/*` route requires the
header `x-icamfind-token: <token>`; a wrong or missing token returns `401 {"error": "unauthorized"}`.

Timestamps are Unix epoch seconds (float). Crop and snapshot images are returned as short-lived
presigned S3 URLs (`crop_url`, `snapshot_url`, 1 hour).

## Shared shapes

```jsonc
// Object
{
  "id": "obj_8f3a1c",          // stable identity, survives moves
  "label": "keys",              // best label (cloud vision label, else local detector label)
  "local_label": "object",      // OpenCV DNN (YOLOX/COCO) label or "object"
  "description": "silver keys on a red lanyard",
  "status": "present",          // present | removed
  "zone": "desk",               // named zone from calibration, or "unzoned"
  "bbox": [x, y, w, h],         // in camera pixels (processing resolution)
  "map_xy": [mx, my],           // top-down room-map coordinates (0..1 each) or null
  "first_seen": 1793000000.0,
  "last_seen": 1793000300.0,
  "last_event": "placed",       // placed | removed | moved | verified | corrected
  "essential": true,            // flagged as must-not-forget (door alert)
  "crop_url": "https://..."
}

// Event
{
  "id": "ev_...", "object_id": "obj_...", "type": "placed|removed|moved|verified|corrected",
  "ts": 1793000300.0, "zone": "desk", "from_zone": "shelf",  // from_zone only on moved
  "label": "keys", "confidence": 0.82, "crop_url": "https://...", "note": "..."
}

// Job step (agent trace)
{ "n": 3, "ts": 1793000301.2, "kind": "thought|tool_call|tool_result|answer|error",
  "tool": "verify_object_live", "input": {...}, "output": {...}, "text": "...", "image_url": "https://..." }
```

## Edge device routes

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/events` | `{device, event: {id, type, object_id, ts, zone, from_zone?, bbox, map_xy, local_label, confidence, essential?}, crop_b64?}` | `{object}` (with refined `label`) |
| POST | `/api/devices/heartbeat` | `{device, zones: [{name, polygon:[[x,y],...]}], door_zone?, frame_size:[w,h], map?:{width, height}, snapshot_b64?}` | `{ok: true}` |
| GET | `/api/devices/{device}/commands` | – | `{commands: [{id, kind, args, created}]}` (marks them `sent`) |
| POST | `/api/commands/{id}/result` | `{device, result: {...}, crop_b64?}` | `{ok: true}` |
| POST | `/api/alerts` | `{device, kind: "door_essentials", message, objects:[ids], ts}` | `{ok: true}` |

Command kinds: `verify_object {object_id, bbox}`, `scan_for_object {object_id}`, `snapshot {}`, `speak {text}`.

## UI routes

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/state` | – | `{device: {id, online, last_heartbeat, zones, door_zone, frame_size, snapshot_url}, objects: [Object], alerts: [...latest 20], approvals: [pending], routines: [...]}` |
| GET | `/api/objects/{id}` | – | `{object, events: [Event]}` |
| POST | `/api/objects/{id}` | `{label?, essential?}` | `{object}` |
| GET | `/api/events?limit=50` | – | `{events: [Event]}` (newest first) |
| POST | `/api/ask` | `{question}` | `{job_id}` |
| GET | `/api/jobs/{id}` | – | `{job: {id, status: running|done|error, question, mode, answer, object_id?, created}, steps: [Step]}` |
| POST | `/api/snapshot` | – | `{job_id}` (asks the device for a fresh privacy-blurred snapshot) |
| POST | `/api/routines` | `{object_id, name, start: "08:00", end: "09:00", tz: "Australia/Brisbane"}` | `{routine}` |
| DELETE | `/api/routines/{id}` | – | `{ok: true}` |
| POST | `/api/routines/{id}/check` | – | `{job_id}` (runs the routine agent now) |
| POST | `/api/approvals/{id}` | `{decision: "approve"|"deny"}` | `{approval}` |

Routine: `{id, object_id, name, start, end, tz, last_status: "ok|missed|reminded|escalated", last_checked}`.
Approval: `{id, kind: "caregiver_notification", message, reason, status: pending|approved|denied, created, job_id}`.
Alert: `{id, kind, message, ts, objects}`.
