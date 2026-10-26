"""Decode a video into raw BGR frames for the browser-engine benchmark (scripts/web_engine_bench.js).

    python scripts/dump_frames.py VIDEO OUT.bin      writes OUT.bin + OUT.json ({w, h, fps, n})
    python scripts/dump_frames.py VIDEO -            streams to stdout: one JSON header line, then frames

Frames are resized to 640 px wide (the processing width used by both pipelines); every frame is kept.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2 as cv


def frames(video: str):
    cap = cv.VideoCapture(video)
    if not cap.isOpened():
        sys.exit(f"cannot open {video}")
    fps = cap.get(cv.CAP_PROP_FPS) or 15.0
    n = int(cap.get(cv.CAP_PROP_FRAME_COUNT))
    first = True
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame.shape[1] != 640:
            h, w = frame.shape[:2]
            frame = cv.resize(frame, (640, int(h * 640 / w)), interpolation=cv.INTER_AREA)
        if first:
            yield {"w": frame.shape[1], "h": frame.shape[0], "fps": fps, "n": n}
            first = False
        yield frame
    cap.release()


def main() -> None:
    video, out = sys.argv[1], sys.argv[2]
    gen = frames(video)
    header = next(gen)
    if out == "-":
        stream = sys.stdout.buffer
        stream.write((json.dumps(header) + "\n").encode())
        for f in gen:
            stream.write(f.tobytes())
        stream.flush()
        return
    count = 0
    with open(out, "wb") as fh:
        for f in gen:
            fh.write(f.tobytes())
            count += 1
    header["n"] = count
    Path(out).with_suffix(".json").write_text(json.dumps(header), encoding="utf-8")
    print(f"wrote {count} frames to {out}")


if __name__ == "__main__":
    main()
