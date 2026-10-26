# iCamFind Cloud API

One Amazon API Gateway HTTP API serves the web app, the edge devices and signed-in users.

## Authentication

| Caller | Routes | Credential |
|---|---|---|
| Anyone | `GET /`, `/app.js`, `/style.css`, `GET /config.json` | none |
| Signed-in person (web app) | `/api/*` except `/api/device/*` | `Authorization: Bearer <Cognito ID token>`, verified by the API Gateway JWT authorizer (issuer = the Cognito user pool, audience = the web app client). Optional `x-icamfind-household: <household id>` selects which household to act on; it must be one the user belongs to (else `403`). Default is the user's first household. |
| Edge device | `/api/device/*` | `x-icamfind-device-token: <token>` issued per device (stored only as a SHA-256 hash). The token determines the household and device id. |

`GET /config.json` returns `{"cognito_domain": "https://<prefix>.auth.<region>.amazoncognito.com", "client_id": "...", "redirect_uri": "https://<api>/", "region": "..."}`.
The web app signs in with the Cognito hosted UI (authorization code + PKCE): it redirects to
`{cognito_domain}/oauth2/authorize?response_type=code&client_id=..&redirect_uri=..&scope=openid+email+profile&code_challenge=..&code_challenge_method=S256&state=..`,
then exchanges `?code=` at `{cognito_domain}/oauth2/token` (form POST, `grant_type=authorization_code`, `client_id`, `code`, `redirect_uri`, `code_verifier`) for `id_token`, `access_token`, `refresh_token`. It refreshes with `grant_type=refresh_token`. Sign-out: `{cognito_domain}/logout?client_id=..&logout_uri=<redirect_uri>`.

Errors are `{"error": "..."}` with status 400/401/403/404/409/500.

## Households

Every user gets a private household ("My home") on first sign-in and is also a member of the shared
**Demo home** (`demo: true`), which is fed by a cloud-hosted replay camera. In the demo home anyone
can ask questions, add routines and decide approvals, but cannot add cameras.

## Shared shapes

```jsonc
// Object
{ "id": "obj_8f3a1c", "label": "keys", "local_label": "object", "description": "silver keys on a red lanyard",
  "status": "present|removed", "zone": "desk", "bbox": [x, y, w, h], "map_xy": [mx, my] | null,
  "first_seen": 1793000000.0, "last_seen": 1793000300.0, "last_event": "placed|removed|moved|verified|corrected",
  "essential": true, "crop_url": "https://..." }
// Event
{ "id": "ev_...", "object_id": "obj_...", "type": "placed|removed|moved|verified|corrected", "ts": 1793000300.0,
  "zone": "desk", "from_zone": "shelf", "label": "keys", "confidence": 0.82, "crop_url": "https://...", "note": "..." }
// Job step (agent trace)
{ "n": 3, "ts": 1793000301.2, "kind": "thought|tool_call|tool_result|answer|error", "tool": "verify_object_live",
  "input": {...}, "output": {...}, "text": "...", "image_url": "https://..." }
```

## User routes (Bearer ID token)

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/me` | – | `{user: {sub, email}, households: [{id, name, role: "owner"|"member", demo: bool}], current: "<household id>"}` |
| GET | `/api/state` | – | `{household: {id, name, demo}, device: {id, name, online, last_heartbeat, zones, door_zone, frame_size, snapshot_url} \| null, devices: [{id, name, online, last_heartbeat}], objects: [Object], alerts: [...], approvals: [pending], routines: [...]}` |
| GET | `/api/objects/{id}` | – | `{object, events: [Event]}` |
| POST | `/api/objects/{id}` | `{label?, essential?}` | `{object}` |
| GET | `/api/events?limit=50` | – | `{events: [Event]}` |
| POST | `/api/ask` | `{question, tz?}` | `{job_id}` |
| GET | `/api/jobs/{id}` | – | `{job: {id, status, question, mode, answer, object_id?, created, finished?, model?}, steps: [Step]}` |
| POST | `/api/snapshot` | – | `{job_id}` |
| POST | `/api/routines` | `{object_id, name, start: "08:00", end: "09:00", tz}` | `{routine}` |
| DELETE | `/api/routines/{id}` | – | `{ok: true}` |
| POST | `/api/routines/{id}/check` | – | `{job_id}` |
| POST | `/api/approvals/{id}` | `{decision: "approve"|"deny"}` | `{approval}` |
| GET | `/api/devices` | – | `{devices: [{id, name, online, last_heartbeat}]}` |
| POST | `/api/devices` | `{name}` | `{device: {id, name}, token, run_command}`. The token is shown only once. Owner of a non-demo household only, else `403`. |

## Device routes (`x-icamfind-device-token`)

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/device/events` | `{event: {id, type, object_id, ts, zone, from_zone?, bbox, map_xy, local_label, confidence, essential?}, crop_b64?}` | `{object}` (with refined `label`) |
| POST | `/api/device/heartbeat` | `{zones, door_zone?, frame_size, map?, snapshot_b64?}` | `{ok: true}` |
| GET | `/api/device/commands` | – | `{commands: [{id, kind, args, created}]}` |
| POST | `/api/device/commands/{id}/result` | `{result, crop_b64?}` | `{ok: true}` |
| POST | `/api/device/alerts` | `{kind, message, objects, ts}` | `{ok: true}` |

Command kinds: `verify_object {object_id, bbox}`, `scan_for_object {object_id}`, `snapshot {}`, `speak {text}`.
