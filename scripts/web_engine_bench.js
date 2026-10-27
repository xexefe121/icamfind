/*
 * Run the browser engine core (cloud/src/web/edge.js) in Node on decoded video frames, so it can be
 * scored with the same rules as the Python pipeline.
 *
 *   python scripts/dump_frames.py bench/synthetic_1.mp4 - | \
 *     node scripts/web_engine_bench.js <opencv.js> bench/synthetic_1.truth.json out.json
 *   node scripts/web_engine_bench.js <opencv.js> truth.json out.json frames.bin      (frames.json beside it)
 *
 * Writes {events: [{type, ts, zone, label, object_id, from_zone?}], perf: {...}} to out.json.
 * placesOnly is off so the result is directly comparable with `python -m icamfind benchmark`, unless the
 * truth file sets "places_only": true (browser Engine settings).
 */
"use strict";

const fs = require("fs");
const path = require("path");

const [cvPath, truthPath, outPath, binPath] = process.argv.slice(2);
if (!cvPath || !truthPath || !outPath) {
  console.error("usage: node web_engine_bench.js <opencv.js> <truth.json> <out.json> [frames.bin]");
  process.exit(2);
}

const edge = require(path.resolve(__dirname, "..", "cloud", "src", "web", "edge.js"));

async function loadCv() {
  let cv = require(path.resolve(cvPath));
  if (cv instanceof Promise) cv = await cv;
  else if (!cv.Mat) await new Promise((r) => { cv.onRuntimeInitialized = r; });
  return cv;
}

function frameSource(onHeader, onFrame, onEnd) {
  if (binPath) {
    const header = JSON.parse(fs.readFileSync(binPath.replace(/\.bin$/, ".json"), "utf8"));
    onHeader(header);
    const size = header.w * header.h * 3;
    const fd = fs.openSync(binPath, "r");
    const buf = Buffer.alloc(size);
    for (let i = 0; ; i++) {
      const n = fs.readSync(fd, buf, 0, size, i * size);
      if (n < size) break;
      onFrame(buf);
    }
    fs.closeSync(fd);
    onEnd();
    return;
  }
  // Streamed from dump_frames.py: one JSON header line, then raw BGR frames.
  let header = null;
  let size = 0;
  let pending = Buffer.alloc(0);
  process.stdin.on("data", (chunk) => {
    pending = pending.length ? Buffer.concat([pending, chunk]) : chunk;
    if (!header) {
      const nl = pending.indexOf(10);
      if (nl < 0) return;
      header = JSON.parse(pending.slice(0, nl).toString("utf8"));
      size = header.w * header.h * 3;
      pending = pending.slice(nl + 1);
      onHeader(header);
    }
    while (pending.length >= size) {
      onFrame(pending.subarray(0, size));
      pending = pending.subarray(size);
    }
    if (pending.length) pending = Buffer.from(pending); // detach from the big chunk
  });
  process.stdin.on("end", onEnd);
}

(async () => {
  const cv = await loadCv();
  const truth = JSON.parse(fs.readFileSync(truthPath, "utf8"));
  const geometry = new edge.RoomGeometry(cv, truth.zones || [], truth.door_zone || null);
  // truth.places_only replays a clip with the browser Engine's settings (only the drawn places watched).
  const cfg = truth.places_only ? { placesOnly: true, maxObjectArea: 0.25, minObjectArea: 0.0005 } : { placesOnly: false };
  const scene = new edge.SceneMemory(cv, cfg, geometry);
  const events = [];
  const times = [];
  let fps = 15;
  let w = 640;
  let h = 480;
  let i = 0;
  frameSource(
    (hd) => { fps = hd.fps || 15; w = hd.w; h = hd.h; },
    (buf) => {
      const bgr = new cv.Mat(h, w, cv.CV_8UC3);
      bgr.data.set(buf);
      const rgb = new cv.Mat();
      cv.cvtColor(bgr, rgb, cv.COLOR_BGR2RGB);
      const t = i / fps;
      const started = process.hrtime.bigint();
      const evs = scene.process(rgb, t);
      times.push(Number(process.hrtime.bigint() - started) / 1e6);
      for (const ev of evs) {
        const e = { type: ev.type, ts: ev.ts, zone: ev.zone, label: ev.label, object_id: ev.object_id, bbox: ev.bbox };
        if (ev.from_zone) e.from_zone = ev.from_zone;
        events.push(e);
        ev.crop.delete();
      }
      bgr.delete();
      rgb.delete();
      i++;
    },
    () => {
      times.sort((a, b) => a - b);
      const perf = {
        frames: i,
        median_ms_per_frame: times.length ? Math.round(times[times.length >> 1] * 100) / 100 : null,
        p95_ms_per_frame: times.length ? Math.round(times[Math.floor(times.length * 0.95)] * 100) / 100 : null,
        rebaselines: scene.stats.rebaselines,
      };
      fs.writeFileSync(outPath, JSON.stringify({ events, perf }, null, 1));
      console.log(`frames=${i} events=${events.length} median=${perf.median_ms_per_frame}ms p95=${perf.p95_ms_per_frame}ms`);
      scene.dispose();
    },
  );
})();
