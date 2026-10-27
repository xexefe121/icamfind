# Browser camera: contract between `edge.js` and the web app

The laptop, tablet or phone running the web app can itself be the camera. `cloud/src/web/edge.js`
runs the iCamFind scene-memory pipeline in the browser using **OpenCV 5.0.0 compiled to
WebAssembly**. Video never leaves the device; only event crops, occasional privacy snapshots and
live-check crops are uploaded, exactly like the Python edge app.

## Loading OpenCV

```html
<script src="https://cdn.jsdelivr.net/npm/@techstark/opencv-js@5.0.0-release.1/dist/opencv.js"
        integrity="sha384-16eddoZUbhJFP+dmyqNO/+vH8UdDOg8MJo+zi1NSy2clHxpKiQaoSQWtQnPHzYvc"
        crossorigin="anonymous" async></script>
```

`edge.js` injects this tag itself on demand (`ICamEdge.load()`), so the 13 MB download happens only
when someone actually turns on a browser camera. In this build the global `cv` may be a Promise
(await it) or a module object (wait for `cv.onRuntimeInitialized`). `cv.accumulateWeighted` and
`cv.imencode` are **not** present: blend with `cv.addWeighted` plus masking, and encode JPEG through
a canvas (`canvas.toBlob(..., "image/jpeg", 0.85)`).

## Global API (`window.ICamEdge`)

```js
await ICamEdge.load();                      // inject opencv.js and wait for the runtime; idempotent
const engine = ICamEdge.create({
  video,                  // HTMLVideoElement already playing a getUserMedia stream
  overlay,                // optional HTMLCanvasElement drawn on top of the video (zones, objects, settling regions)
  apiBase: "",            // same-origin API ("" = relative "api/device/...")
  deviceToken,            // this browser camera's device token (stored by the app; never shown to users)
  zones,                  // [{name, polygon: [[x,y],...]}] in processing coordinates (frame width = 640)
  doorZone,               // zone name or null
  essentials,             // ["keys","wallet","phone","glasses","pill","medication"]
  onEvent(ev),            // {type, label, zone, from_zone?, ts, cropDataUrl} after every committed event
  onStatus(s),            // {state: "loading"|"learning"|"watching"|"paused"|"error", fps, objects, message}
  onAlert(a),             // {kind: "door_essentials", message}
});
engine.start();                 // begin processing (~8 fps) + heartbeat + command polling
engine.stop();                  // stop everything, release Mats
engine.setZones(zones, doorZone);
engine.resetBackground();       // re-learn the empty room
engine.objects;                 // [{id, label, zone, status, box}] current local memory
engine.frameSize;               // [w, h] processing size (w = 640)
engine.snapshotDataUrl();       // current frame as JPEG data URL (for the zone editor background)
engine.highlight(objectId, ms); // pulse a "Here" ring around a remembered object on the live overlay
engine.outlinedFrameDataUrl(objectId, box?); // current picture with that object circled (JPEG data URL)
ICamEdge.drawSpotlight(ctx, [x, y, w, h], W, H, label, pulse = 1, px = 1); // same look on any canvas
```

## What `edge.js` must do (parity with `icamfind/edge`)

- **Scene memory:** a faithful port of `icamfind/edge/scene.py`:
  - MOG2 short-term motion model;
  - long-term float reference background, with slow masked drift and absorption;
  - a time-based evidence accumulator;
  - grouping with a 15×15 close, committing at 85% ready or 2.5× stable time;
  - a quiet check;
  - the lighting guard;
  - min/max area (`max_object_area` 0.12 in the browser, since people sit in front of laptops);
  - patch-match removal test;
  - Sobel boundary ghost test;
  - re-ID with ORB + HSV histogram + shape;
  - verify and scan, using ORB + RANSAC homography and multi-scale `matchTemplate`.

  Keep the same thresholds and comments explaining them.
- **Browser-only guards (`placesOnly`, no person detector):** only the drawn places produce objects;
  a candidate that touches the border of a place is someone reaching in (typically the person sitting
  in front of a laptop camera) and is held back without being absorbed, and the status line says so;
  a candidate is committed only while most of it still differs from memory, so evidence left behind
  by someone who has moved away is not remembered. Crops of placed or moved objects are cut again from
  the camera's full-resolution picture so Claude can name small things. The cloud asks Claude whether
  a crop mainly shows a person; such objects are marked `ignored` and never listed.
- **Zones:** point-in-polygon at the bottom-centre (`cv.pointPolygonTest`), same rules as `geometry.py`.
- **Uplink:** same JSON as the Python edge (`docs/API.md` device routes), with header
  `x-icamfind-device-token`:
  - `POST api/device/events` with crop JPEG base64 (max side 320). Updates labels from the
    response's `object.label` / `essential`.
  - `POST api/device/heartbeat` every 20 s: zones, door_zone, frame_size. Add a snapshot on the
    first heartbeat, every 5 minutes, and when asked.
  - `GET api/device/commands` every 2 s. Run `verify_object`, `scan_for_object` (adopting the found
    position like `adopt_found`), `snapshot`, and `speak` (via `speechSynthesis`). Then
    `POST api/device/commands/{id}/result` with a crop.
  - Retry failed event uploads from an in-memory queue, and persist the queue to localStorage.
- **Door alert:** no person detector in the browser. Trigger it when significant motion occupies
  the door zone (more than 8% of its pixels moving for at least 0.5 s) while essential objects are
  present, with a 45 s cooldown. Speak through `speechSynthesis` and POST `api/device/alerts`.
- **Persistence:** save object memory to localStorage under `icamfind.edge.<deviceId>` (metadata +
  PNG data URLs of appearance/under/mask, capped at 60 objects), wrapped in try/catch, and restore
  it on start.
- **DOM-free core:** structure the code so the scene memory class runs in Node too (no DOM
  access). Export it via `if (typeof module !== "undefined") module.exports = {...}`, so it can be
  validated against the synthetic benchmark (`scripts/web_engine_bench.js`).
- **Release every `cv.Mat`** (`.delete()`) on each frame. Memory leaks in WASM kill tabs.

## Server support

- `POST api/devices {name, kind: "browser"}` (user route) returns `{device, token}`. The web app
  stores `{deviceId, token, household}` in localStorage, so the same browser resumes as the same
  camera after reload. The token is never displayed.
- `POST api/devices/pair-code {name}` (user route) returns `{code: "K7P2-9QXA", expires_in: 600}`
  for connecting another computer or CCTV box. On that machine, `python -m icamfind pair K7P2-9QXA
  --api <url>` exchanges the code at `POST api/device/pair` and saves the token locally. Users never
  copy a token.
- `PUT api/devices/{id}/zones {zones, door_zone}` (user route) saves zones drawn in the web app.
  Python edge devices fetch them with `GET api/device/config`.
