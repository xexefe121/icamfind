"""Interactive setup: draw named zones, mark the door, and pick four floor points for the room map.

Controls (shown on screen):
  left click  add a polygon point        Enter  finish the polygon and name it
  m           map mode: click 4 points on the floor/table plane (TL, TR, BR, BL of the room map)
  u           undo last point             s      save and quit        q  quit without saving
"""

from __future__ import annotations

import cv2 as cv
import numpy as np

from .app import open_source, resize_for_processing
from .config import Config, Zone


def _ask(prompt: str, default: str = "") -> str:
    try:
        value = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        value = ""
    return value or default


def calibrate(cfg: Config, config_path: str) -> None:
    cap = open_source(cfg.source)
    ok, raw = cap.read()
    for _ in range(10):  # let auto-exposure settle
        ok, raw = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError("could not read a frame from the camera")
    frame = resize_for_processing(raw, cfg.process_width)

    points: list[list[int]] = []
    map_points: list[list[int]] = []
    mode = {"map": False}

    def on_mouse(event, x, y, *_):
        if event == cv.EVENT_LBUTTONDOWN:
            (map_points if mode["map"] else points).append([x, y])
            if mode["map"] and len(map_points) > 4:
                map_points.pop(0)

    cv.namedWindow("iCamFind setup")
    cv.setMouseCallback("iCamFind setup", on_mouse)
    while True:
        view = frame.copy()
        for z in cfg.zones:
            poly = np.array(z.polygon, np.int32)
            color = (0, 140, 255) if z.name == cfg.door_zone else (255, 200, 0)
            cv.polylines(view, [poly], True, color, 2)
            cv.putText(view, z.name, tuple(poly[0]), cv.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
        if points:
            cv.polylines(view, [np.array(points, np.int32)], False, (0, 255, 0), 1)
            for p in points:
                cv.circle(view, tuple(p), 3, (0, 255, 0), -1)
        for i, p in enumerate(map_points):
            cv.circle(view, tuple(p), 5, (255, 0, 255), -1)
            cv.putText(view, "TL TR BR BL".split()[i], (p[0] + 6, p[1]), cv.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 255), 1)
        hint = ("MAP: click 4 floor points TL,TR,BR,BL  |  m: back to zones" if mode["map"] else
                "ZONES: click points, Enter=name zone | m: map | u: undo | s: save | q: quit")
        cv.rectangle(view, (0, 0), (view.shape[1], 22), (0, 0, 0), -1)
        cv.putText(view, hint, (6, 15), cv.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        cv.imshow("iCamFind setup", view)
        key = cv.waitKey(30) & 0xFF
        if key in (13, 10) and len(points) >= 3:
            name = _ask("Zone name (e.g. desk, kitchen counter, door)", f"zone{len(cfg.zones) + 1}")
            cfg.zones.append(Zone(name=name, polygon=[list(map(float, p)) for p in points]))
            if _ask("Is this the exit door area? (y/n)", "n").lower().startswith("y"):
                cfg.door_zone = name
            points.clear()
        elif key == ord("m"):
            mode["map"] = not mode["map"]
        elif key == ord("u"):
            target = map_points if mode["map"] else points
            if target:
                target.pop()
            elif not mode["map"] and cfg.zones:
                cfg.zones.pop()
        elif key == ord("s"):
            if len(map_points) == 4:
                cfg.map_image_points = [list(map(float, p)) for p in map_points]
                cfg.map_world_points = [[0, 0], [1, 0], [1, 1], [0, 1]]
            cfg.save(config_path)
            print(f"saved {len(cfg.zones)} zones to {config_path}")
            break
        elif key == ord("q"):
            break
    cv.destroyAllWindows()
