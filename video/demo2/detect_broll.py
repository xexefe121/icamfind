"""Run the real browser engine on a CCTV feed, name what it found with Claude, and burn the result in.

    python video/demo2/detect_broll.py bedroom "nightstand" 200,170,470,330

The engine (cloud/src/web/edge.js on OpenCV.js 5.0, browser settings) watches the given place; each event's
crop is named by the same describe_crop call the cloud uses. Writes feeds/<name>_det.mp4 and <name>_events.json.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import cv2 as cv
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FEEDS = HERE / "feeds"
SCRATCH = Path(os.environ.get("SCRATCH", "Y:/Temp/claude/N--iFindcam/244541ac-696d-4c63-9703-f9a8684f6767/scratchpad"))
OPENCV_JS = SCRATCH / "ocvjs" / "opencv.js"
PY = sys.executable


def engine_events(name: str, place: str, zone: list[int]) -> list[dict]:
    work = SCRATCH / f"det_{name}"
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run([PY, str(ROOT / "scripts" / "dump_frames.py"), str(FEEDS / f"{name}.mp4"), str(work / "f.bin")],
                   check=True, capture_output=True)
    x0, y0, x1, y1 = zone
    (work / "truth.json").write_text(json.dumps({"zones": [{"name": place, "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}],
                                                 "places_only": True}))
    subprocess.run(["node", str(ROOT / "scripts" / "web_engine_bench.js"), str(OPENCV_JS), str(work / "truth.json"),
                    str(work / "out.json"), str(work / "f.bin")], check=True)
    return json.loads((work / "out.json").read_text())["events"]


def main() -> None:
    name, place = sys.argv[1], sys.argv[2]
    zone = [int(v) for v in sys.argv[3].split(",")]
    named = "--no-name" not in sys.argv  # without Claude's name the overlay says only what the engine saw
    # --label X: the cloud already named this object in the recorded app run; reuse that name, as the cloud does
    fixed = sys.argv[sys.argv.index("--label") + 1] if "--label" in sys.argv else None
    events = engine_events(name, place, zone)
    cap = cv.VideoCapture(str(FEEDS / f"{name}.mp4"))
    fps = cap.get(cv.CAP_PROP_FPS)
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    k = frames[0].shape[1] / 640
    sys.path.insert(0, str(ROOT / "cloud" / "src"))
    os.environ.setdefault("AWS_REGION", "ap-southeast-2")
    import llm  # noqa: E402

    for e in events:
        x, y, w, h = [int(round(v * k)) for v in e["bbox"]]
        i = min(len(frames) - 1, int(e["ts"] * fps) + (0 if e["type"] != "removed" else -int(3 * fps)))
        src = frames[max(0, int(e["ts"] * fps) - int(4 * fps))] if e["type"] == "removed" else frames[i]
        m = int(max(w, h) * 0.5)
        crop = src[max(0, y - m):y + h + m, max(0, x - m):x + w + m]
        ok, jpg = cv.imencode(".jpg", crop, [cv.IMWRITE_JPEG_QUALITY, 90])
        if fixed:
            info = {"label": fixed, "kind": "object"}
        else:
            info = llm.describe_crop(jpg.tobytes(), "object") if named else {"label": "", "kind": "object"}
        e.update(label=info["label"], kind=info["kind"], box=[x, y, w, h])
        print(f"{e['ts']:.2f}s {e['type']} {info['label']} ({info['kind']})")
    (FEEDS / f"{name}_events.json").write_text(json.dumps(events, indent=1))

    out = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s",
                            f"{frames[0].shape[1]}x{frames[0].shape[0]}", "-r", str(fps), "-i", "-", "-c:v", "libx264",
                            "-crf", "17", "-pix_fmt", "yuv420p", str(FEEDS / f"{name}_det.mp4")], stdin=subprocess.PIPE)
    for i, f in enumerate(frames):
        t = i / fps
        f = f.copy()
        for e in events:
            if t < e["ts"]:
                continue
            age = t - e["ts"]
            x, y, w, h = e["box"]
            placed = e["type"] != "removed"
            col = (120, 220, 90) if placed else (40, 170, 245)
            if placed or age < 4:
                cv.rectangle(f, (x, y), (x + w, y + h), col, 2, cv.LINE_AA)
            if age < 5:
                verb = "Remembered" if placed else "Picked up"
                prep = "on the" if placed else "from the"
                text = f"{verb}: {e['label']} {prep} {place}" if e["label"] else                     f"{'Put down' if placed else 'Picked up'} {'on' if placed else 'from'} the {place}"
                alpha = min(1.0, age * 3, (5 - age) * 2)
                bar = f.copy()
                (tw, th), _ = cv.getTextSize(text, cv.FONT_HERSHEY_DUPLEX, 0.7, 1)
                bx, by = (f.shape[1] - tw) // 2 - 16, f.shape[0] - 92
                cv.rectangle(bar, (bx, by), (bx + tw + 32, by + 40), (25, 25, 25), -1)
                cv.circle(bar, (bx + 14, by + 20), 5, col, -1, cv.LINE_AA)
                cv.putText(bar, text, (bx + 26, by + 27), cv.FONT_HERSHEY_DUPLEX, 0.7, (245, 245, 245), 1, cv.LINE_AA)
                f = cv.addWeighted(bar, alpha * 0.92, f, 1 - alpha * 0.92, 0)
            tag = e["label"] or "new"
            if placed:
                (tw, th), _ = cv.getTextSize(tag, cv.FONT_HERSHEY_SIMPLEX, 0.5, 1)
                cv.rectangle(f, (x, y - th - 10), (x + tw + 10, y), col, -1)
                cv.putText(f, tag, (x + 5, y - 6), cv.FONT_HERSHEY_SIMPLEX, 0.5, (20, 20, 20), 1, cv.LINE_AA)
        out.stdin.write(f.tobytes())
    out.stdin.close()
    out.wait()
    print("wrote", FEEDS / f"{name}_det.mp4")


if __name__ == "__main__":
    main()
