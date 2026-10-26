"""Synthetic benchmark: a rendered desk scene with ground-truth put-down / pick-up / move events.

It is not a substitute for real footage, but it is fully reproducible (seeded), so judges can rerun
the exact numbers in the report without a camera. Each clip contains sensor noise, slow lighting
drift, a sudden lights-on jump, a "hand" that occludes objects while placing and taking them,
distractor motion that should produce no events, and objects carried between zones.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import cv2 as cv
import numpy as np

W, H, FPS = 640, 480, 15

ZONES = {
    "desk": [[20, 250], [400, 250], [400, 470], [20, 470]],
    "shelf": [[430, 60], [630, 60], [630, 200], [430, 200]],
    "side table": [[430, 280], [630, 280], [630, 470], [430, 470]],
    "door": [[0, 0], [150, 0], [150, 230], [0, 230]],
}


@dataclass
class Item:
    name: str
    size: tuple[int, int]
    color: tuple[int, int, int]
    shape: str  # rect | ellipse | ring


ITEMS = [
    Item("keys", (34, 22), (170, 170, 175), "ring"),
    Item("wallet", (58, 40), (40, 60, 120), "rect"),
    Item("pill bottle", (26, 46), (30, 140, 230), "rect"),
    Item("glasses", (64, 24), (30, 30, 30), "ellipse"),
    Item("book", (110, 78), (60, 150, 60), "rect"),
    Item("mug", (44, 50), (200, 200, 230), "ellipse"),
]


def _texture(rng: np.random.Generator, h: int, w: int, base: tuple[int, int, int], amp: float) -> np.ndarray:
    noise = rng.normal(0, 1, (h // 8 + 1, w // 8 + 1)).astype(np.float32)
    noise = cv.resize(noise, (w, h), interpolation=cv.INTER_CUBIC)
    stripes = np.sin(np.linspace(0, 40, w))[None, :].astype(np.float32)
    img = np.empty((h, w, 3), np.float32)
    for c in range(3):
        img[..., c] = base[c] + amp * noise + 0.4 * amp * stripes
    return img


def _render_item(rng: np.random.Generator, item: Item) -> tuple[np.ndarray, np.ndarray]:
    w, h = item.size
    patch = _texture(rng, h, w, item.color, 18).clip(0, 255).astype(np.uint8)
    mask = np.zeros((h, w), np.uint8)
    if item.shape == "rect":
        cv.rectangle(mask, (0, 0), (w - 1, h - 1), 255, -1)
        cv.rectangle(patch, (2, 2), (w - 3, h - 3), tuple(int(c * 0.6) for c in item.color), 2)
    elif item.shape == "ellipse":
        cv.ellipse(mask, (w // 2, h // 2), (w // 2 - 1, h // 2 - 1), 0, 0, 360, 255, -1)
    else:  # ring of keys: ring + two teeth
        cv.circle(mask, (h // 2, h // 2), h // 2 - 1, 255, 4)
        cv.rectangle(mask, (h // 2, h // 2 - 4), (w - 1, h // 2 + 3), 255, -1)
        cv.rectangle(mask, (w - 10, h // 2), (w - 4, h - 2), 255, -1)
    return patch, mask


def _background(rng: np.random.Generator) -> np.ndarray:
    bg = _texture(rng, H, W, (150, 160, 170), 6)  # wall
    bg[250:, :410] = _texture(rng, H - 250, 410, (70, 110, 150), 10)  # wooden desk
    bg[60:200, 430:] = _texture(rng, 140, W - 430, (120, 120, 110), 8)  # shelf
    bg[280:, 430:] = _texture(rng, H - 280, W - 430, (90, 80, 140), 9)  # side table
    bg[0:230, 0:150] = _texture(rng, 230, 150, (60, 70, 80), 5)  # door
    # A pre-existing object (part of the remembered background at start, taken mid-clip).
    with_remote = bg.copy()
    with_remote[300:340, 300:360] = _texture(rng, 40, 60, (20, 20, 200), 10)
    return bg, with_remote


def _zone_point(rng: np.random.Generator, zone: str, size: tuple[int, int]) -> tuple[int, int]:
    pts = np.array(ZONES[zone])
    x0, y0 = pts.min(0) + 10
    x1, y1 = pts.max(0) - 10 - np.array(size)
    return int(rng.integers(x0, max(x0 + 1, x1))), int(rng.integers(y0, max(y0 + 1, y1)))


def make_clip(out_dir: Path, seed: int, duration: float = 150.0) -> tuple[Path, Path]:
    rng = np.random.default_rng(seed)
    bg, bg_with_remote = _background(rng)
    sprites = {it.name: _render_item(rng, it) for it in ITEMS}

    # Build an action script: (start_time, action, item, zone)
    script, placed = [], {}
    t = 4.0
    names = [it.name for it in ITEMS]
    rng.shuffle(names)
    plan = []
    for name in names[:4]:
        plan.append(("place", name, rng.choice(["desk", "shelf", "side table"])))
    plan.append(("distract", None, None))
    plan.append(("take_preexisting", None, None))
    for name in names[:4]:
        r = rng.random()
        plan.append(("move", name, None) if r < 0.5 else ("take", name, None))
    plan.append(("lights", None, None))
    for name in names[4:]:
        plan.append(("place", name, rng.choice(["desk", "side table"])))
    plan.append(("distract", None, None))

    truth = []
    for action, name, zone in plan:
        if action == "place":
            pos = _zone_point(rng, zone, sprites[name][0].shape[1::-1])
            script.append((t, "place", name, pos))
            placed[name] = (pos, zone)
            truth.append({"t": round(t + 2.0, 2), "type": "placed", "object": name, "zone": zone})
            t += 7.0
        elif action == "take":
            if name not in placed:
                continue
            pos, zone = placed.pop(name)
            script.append((t, "take", name, pos))
            truth.append({"t": round(t + 2.0, 2), "type": "removed", "object": name, "zone": zone})
            t += 7.0
        elif action == "move":
            if name not in placed:
                continue
            pos, zone = placed.pop(name)
            new_zone = rng.choice([z for z in ("desk", "shelf", "side table") if z != zone])
            new_pos = _zone_point(rng, new_zone, sprites[name][0].shape[1::-1])
            script.append((t, "take", name, pos))
            truth.append({"t": round(t + 2.0, 2), "type": "removed", "object": name, "zone": zone})
            script.append((t + 5.0, "place", name, new_pos))
            truth.append({"t": round(t + 7.0, 2), "type": "moved", "object": name, "zone": new_zone,
                          "from_zone": zone})
            placed[name] = (new_pos, new_zone)
            t += 12.0
        elif action == "take_preexisting":
            script.append((t, "take_pre", "remote", (300, 300)))
            truth.append({"t": round(t + 2.0, 2), "type": "removed", "object": "remote", "zone": "desk"})
            t += 7.0
        elif action == "distract":
            script.append((t, "wave", None, (200, 120)))
            t += 6.0
        elif action == "lights":
            script.append((t, "lights", None, None))
            t += 6.0
    duration = max(duration, t + 6.0)

    out_dir.mkdir(parents=True, exist_ok=True)
    video_path = out_dir / f"synthetic_{seed}.mp4"
    truth_path = out_dir / f"synthetic_{seed}.truth.json"
    writer = cv.VideoWriter(str(video_path), cv.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))

    on_scene: dict[str, tuple[int, int]] = {}
    pre_present = True
    gain = 1.0
    n_frames = int(duration * FPS)
    events = sorted(script, key=lambda s: s[0])
    for f in range(n_frames):
        ft = f / FPS
        img = (bg_with_remote if pre_present else bg).copy()
        hand = None
        for start, action, name, pos in events:
            dt = ft - start
            if action in ("place", "take", "take_pre") and 0 <= dt < 2.0:
                # The hand travels in from the bottom-right, dwells, and leaves.
                target = np.array(pos) + 10
                origin = np.array([W - 40, H + 60])
                k = dt / 0.7 if dt < 0.7 else (1.0 if dt < 1.3 else 1 - (dt - 1.3) / 0.7)
                hand = origin + (target - origin) * min(1.0, max(0.0, k))
                if dt >= 1.0:
                    if action == "place":
                        on_scene[name] = pos
                    elif action == "take":
                        on_scene.pop(name, None)
                    else:
                        pre_present = False
            elif action == "wave" and 0 <= dt < 3.0:
                hand = np.array(pos) + np.array([80 * math.sin(dt * 6), 30 * math.cos(dt * 4)])
            elif action == "lights" and dt >= 0:
                gain = 1.25
        for name, (x, y) in on_scene.items():
            patch, mask = sprites[name]
            h, w = mask.shape
            roi = img[y:y + h, x:x + w]
            roi[mask > 0] = patch[mask > 0]
        drift = 1.0 + 0.04 * math.sin(ft / 40)
        frame = img * gain * drift
        if hand is not None:
            hx, hy = int(hand[0]), int(hand[1])
            cv.line(frame, (hx, hy), (hx + 160, hy + 260), (110, 140, 190), 38)
            cv.ellipse(frame, (hx, hy), (26, 20), 30, 0, 360, (120, 150, 200), -1)
        frame += rng.normal(0, 3.0, frame.shape)
        writer.write(frame.clip(0, 255).astype(np.uint8))
    writer.release()

    truth_path.write_text(json.dumps({
        "video": video_path.name, "fps": FPS, "frame_size": [W, H], "seed": seed,
        "zones": [{"name": k, "polygon": v} for k, v in ZONES.items()], "door_zone": "door",
        "events": truth,
    }, indent=1), encoding="utf-8")
    return video_path, truth_path
