"""Turn a stock clip into a CCTV-style camera feed with a scripted timeline.

    python video/demo2/make_feed.py hall      # writes video/demo2/feeds/hall.y4m (fake webcam) + hall.mp4

Timeline for "hall" (Pexels 7986194, someone picking keys up off a hall table): the clip is played in reverse
to show the keys being put down, then held, then forwards to show them being picked up. Holds get fresh sensor
noise on every frame so the camera looks live.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import cv2 as cv
import numpy as np

HERE = Path(__file__).resolve().parent
FOOT = HERE.parent / "footage"
OUT = HERE / "feeds"
W, H = 960, 540


def load(name: str) -> tuple[list[np.ndarray], float]:
    cap = cv.VideoCapture(str(next(FOOT.glob(f"pexels_{name}-*.mp4"))))
    fps = cap.get(cv.CAP_PROP_FPS)
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    return frames, fps


class Grader:
    """Security-camera look: mild barrel distortion, desaturated cool grade, vignette, grain, OSD."""

    def __init__(self, cam: str, start_clock: float, seed: int = 7):
        self.cam = cam
        self.clock0 = start_clock
        self.rng = np.random.default_rng(seed)
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        cx, cy = W / 2, H / 2
        nx, ny = (xx - cx) / cx, (yy - cy) / cy
        r2 = nx * nx + ny * ny
        k = -0.06
        self.mapx = (cx + nx * (1 + k * r2) * cx).astype(np.float32)
        self.mapy = (cy + ny * (1 + k * r2) * cy).astype(np.float32)
        self.vig = (1.0 - 0.35 * np.clip(r2 / 2.0, 0, 1))[..., None].astype(np.float32)

    def __call__(self, frame: np.ndarray, t: float) -> np.ndarray:
        f = cv.resize(frame, (W, H), interpolation=cv.INTER_AREA)
        f = cv.remap(f, self.mapx, self.mapy, cv.INTER_LINEAR, borderMode=cv.BORDER_REFLECT)
        hsv = cv.cvtColor(f, cv.COLOR_BGR2HSV).astype(np.float32)
        hsv[..., 1] *= 0.55
        f = cv.cvtColor(hsv.astype(np.uint8), cv.COLOR_HSV2BGR).astype(np.float32)
        f = (f - 128) * 1.08 + 122
        f[..., 0] += 6  # slightly cool
        f[..., 2] -= 4
        f *= self.vig
        f += self.rng.normal(0, 2.6, (H, W, 1)).astype(np.float32)  # luma grain
        f = np.clip(f, 0, 255).astype(np.uint8)
        f = cv.GaussianBlur(f, (3, 3), 0.6)
        stamp = time.strftime("%Y-%m-%d  %H:%M:%S", time.localtime(self.clock0 + t))
        self._text(f, stamp, (18, 34))
        self._text(f, self.cam, (18, H - 20))
        if int(t * 2) % 2 == 0:
            cv.circle(f, (W - 92, 27), 7, (40, 40, 230), -1, cv.LINE_AA)
        self._text(f, "REC", (W - 78, 34))
        return f

    @staticmethod
    def _text(img, s, org):
        cv.putText(img, s, org, cv.FONT_HERSHEY_DUPLEX, 0.62, (0, 0, 0), 4, cv.LINE_AA)
        cv.putText(img, s, org, cv.FONT_HERSHEY_DUPLEX, 0.62, (235, 235, 235), 1, cv.LINE_AA)


def timeline_hall(fps_out: float):
    frames, fps = load("7986194")
    fwd = frames
    rev = frames[::-1]
    segs = [("hold", fwd[-1], 50.0), ("play", rev, None), ("hold", fwd[0], 80.0), ("play", fwd, None),
            ("hold", fwd[-1], 90.0)]
    t = 0.0
    for kind, src, dur in segs:
        if kind == "hold":
            for i in range(int(dur * fps_out)):
                yield src, t
                t += 1 / fps_out
        else:
            n = int(len(src) / fps * fps_out)
            for i in range(n):
                yield src[min(len(src) - 1, int(i * fps / fps_out))], t
                t += 1 / fps_out


def timeline_simple(clip: str, pre: float, post: float, fps_out: float, reverse: bool = False):
    """Hold the first frame, play the clip once, hold the last frame."""
    frames, fps = load(clip)
    if reverse:
        frames = frames[::-1]
    t = 0.0
    for _ in range(int(pre * fps_out)):
        yield frames[0], t
        t += 1 / fps_out
    for i in range(int(len(frames) / fps * fps_out)):
        yield frames[min(len(frames) - 1, int(i * fps / fps_out))], t
        t += 1 / fps_out
    for _ in range(int(post * fps_out)):
        yield frames[-1], t
        t += 1 / fps_out


def write(name: str, gen, cam: str, fps_out: float = 8.0, y4m_out: bool = True) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # FEED_CLOCK (epoch seconds) sets the on-screen clock at the first frame, so it can match the app's times.
    clock0 = float(os.environ.get("FEED_CLOCK") or time.mktime((2026, 10, 27, 8, 13, 50, 0, 0, -1)))
    grade = Grader(cam, start_clock=clock0)
    y4m = open(OUT / f"{name}.y4m", "wb") if y4m_out else None
    if y4m:
        y4m.write(f"YUV4MPEG2 W{W} H{H} F{int(fps_out)}:1 Ip A1:1 C420jpeg\n".encode())
    mp4 = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
                            "-r", str(fps_out), "-i", "-", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                            str(OUT / f"{name}.mp4")], stdin=subprocess.PIPE)
    n = 0
    for src, t in gen:
        f = grade(src, t)
        mp4.stdin.write(f.tobytes())
        if y4m:
            y4m.write(b"FRAME\n")
            y4m.write(cv.cvtColor(f, cv.COLOR_BGR2YUV_I420).tobytes())
        n += 1
    if y4m:
        y4m.close()
    mp4.stdin.close()
    mp4.wait()
    print(f"{name}: {n} frames, {n / fps_out:.0f} s")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "hall"
    if which == "hall":
        write("hall", timeline_hall(8.0), "CAM 2   HALL TABLE")
    elif which == "broll":
        # B-roll cameras at 25 fps (mp4 only): bedroom phone, medicine tray, hallway, kitchen table.
        write("bedroom", timeline_simple("12803301", 3, 9, 25.0), "CAM 3   BEDROOM", 25.0, False)
        write("meds", timeline_simple("5867578", 3, 7, 25.0), "CAM 4   BEDSIDE TRAY", 25.0, False)
        write("hallway", timeline_simple("7986201", 1, 3, 25.0), "CAM 1   HALLWAY", 25.0, False)
        write("kitchen", timeline_simple("4994521", 0, 0, 25.0), "CAM 4   KITCHEN TABLE", 25.0, False)
