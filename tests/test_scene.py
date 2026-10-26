"""Scene-memory behaviour on controlled synthetic frames (no camera, no DNN)."""

import numpy as np

from icamfind.edge.config import Config, Zone
from icamfind.edge.geometry import RoomGeometry
from icamfind.edge.scene import SceneMemory

FPS = 15.0


def _scene():
    cfg = Config(zones=[Zone("desk", [[0, 240], [640, 240], [640, 480], [0, 480]]),
                        Zone("shelf", [[0, 0], [640, 0], [640, 239], [0, 239]])])
    return SceneMemory(cfg, RoomGeometry(cfg))


def _background(seed=0):
    rng = np.random.default_rng(seed)
    base = rng.integers(90, 110, (480, 640, 3)).astype(np.uint8)
    base[240:] = (base[240:] * 0.7).astype(np.uint8)
    return base


def _object(seed=1):
    rng = np.random.default_rng(seed)
    obj = rng.integers(0, 255, (40, 60, 3)).astype(np.uint8)
    obj[:, :, 2] = 220  # strongly red, textured
    return obj


def _run(scene, frames, t0):
    events = []
    for i, frame in enumerate(frames):
        noise = np.random.default_rng(i).normal(0, 2, frame.shape)
        events += scene.process(np.clip(frame + noise, 0, 255).astype(np.uint8), t0 + i / FPS)
    return events


def _hand_over(frame, x, y):
    f = frame.copy()
    f[max(0, y - 30):y + 70, max(0, x - 20):x + 90] = (120, 150, 200)
    return f


def test_place_remove_move_cycle():
    scene, bg, obj = _scene(), _background(), _object()
    t = 0.0
    events = _run(scene, [bg] * int(3 * FPS), t)  # learn the empty room
    assert events == []
    t += 3

    with_obj = bg.copy()
    with_obj[300:340, 100:160] = obj
    hand = [_hand_over(bg, 100 + k * 3, 300) for k in range(8)]  # hand moving in, occluding
    events = _run(scene, hand + [with_obj] * int(4 * FPS), t)
    t += (len(hand) + 4 * FPS) / FPS
    placed = [e for e in events if e.type == "placed"]
    assert len(placed) == 1 and placed[0].zone == "desk"
    oid = placed[0].object_id

    events = _run(scene, [_hand_over(with_obj, 110 + k * 3, 300) for k in range(8)] + [bg] * int(4 * FPS), t)
    t += (8 + 4 * FPS) / FPS
    removed = [e for e in events if e.type == "removed"]
    assert len(removed) == 1 and removed[0].object_id == oid

    on_shelf = bg.copy()
    on_shelf[80:120, 400:460] = obj
    events = _run(scene, [_hand_over(bg, 400 + k * 3, 80) for k in range(8)] + [on_shelf] * int(4 * FPS), t)
    moved = [e for e in events if e.type in ("moved", "placed")]
    assert len(moved) == 1
    assert moved[0].type == "moved" and moved[0].object_id == oid
    assert moved[0].from_zone == "desk" and moved[0].zone == "shelf"


def test_lighting_jump_is_not_an_event():
    scene, bg = _scene(), _background()
    _run(scene, [bg] * int(3 * FPS), 0.0)
    brighter = np.clip(bg.astype(np.float32) * 1.35, 0, 255).astype(np.uint8)
    events = _run(scene, [brighter] * int(5 * FPS), 3.0)
    assert events == []
    assert scene.stats["rebaselines"] == 1


def test_verify_reports_presence_and_absence():
    scene, bg, obj = _scene(), _background(), _object()
    _run(scene, [bg] * int(3 * FPS), 0.0)
    with_obj = bg.copy()
    with_obj[300:340, 100:160] = obj
    events = _run(scene, [with_obj] * int(4 * FPS), 3.0)
    oid = events[0].object_id
    assert scene.verify(oid, with_obj, 10.0)["present"] is True
    assert scene.verify(oid, bg, 10.0)["present"] is False
