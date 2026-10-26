"""Minimal ground-truth annotator for real recordings.

Play the clip; when a hand has just left after putting something down / picking it up, press
  p = placed   r = removed   m = moved
then click the object's location (zone is looked up from the config) and type its name in the
console. Space pauses, a/d step one second, s saves, q quits.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2 as cv

from ..edge.config import Config
from ..edge.geometry import RoomGeometry


def annotate(video: Path, cfg: Config, out: Path) -> None:
    cap = cv.VideoCapture(str(video))
    fps = cap.get(cv.CAP_PROP_FPS) or 15.0
    total = int(cap.get(cv.CAP_PROP_FRAME_COUNT))
    geometry = RoomGeometry(cfg)
    events = json.loads(out.read_text())["events"] if out.exists() else []
    state = {"pending": None, "paused": False, "frame": 0}

    def on_mouse(event, x, y, *_):
        if event == cv.EVENT_LBUTTONDOWN and state["pending"]:
            kind = state["pending"]
            scale = cfg.process_width / frame.shape[1]
            zone = geometry.zone_at((x * scale, y * scale), exclude_door=True)
            name = input(f"{kind} at {zone} t={state['frame'] / fps:.1f}s — object name: ").strip() or "object"
            ev = {"t": round(state["frame"] / fps, 2), "type": kind, "object": name, "zone": zone}
            if kind == "moved":
                ev["from_zone"] = input("moved from zone: ").strip()
            events.append(ev)
            print("added", ev)
            state["pending"] = None

    cv.namedWindow("annotate")
    cv.setMouseCallback("annotate", on_mouse)
    ok, frame = cap.read()
    while ok:
        view = frame.copy()
        cv.putText(view, f"t={state['frame'] / fps:6.1f}s  events={len(events)}  "
                         f"{'PAUSED' if state['paused'] else ''} {state['pending'] or ''}",
                   (8, 20), cv.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
        cv.imshow("annotate", view)
        key = cv.waitKey(0 if state["paused"] else int(1000 / fps)) & 0xFF
        if key == ord("q"):
            break
        if key == ord(" "):
            state["paused"] = not state["paused"]
        elif key in (ord("p"), ord("r"), ord("m")):
            state["pending"] = {"p": "placed", "r": "removed", "m": "moved"}[chr(key)]
            state["paused"] = True
            print(f"click the {state['pending']} object's location")
        elif key == ord("s"):
            break
        elif key in (ord("a"), ord("d")):
            target = state["frame"] + int(fps) * (1 if key == ord("d") else -1)
            state["frame"] = max(0, min(total - 1, target))
            cap.set(cv.CAP_PROP_POS_FRAMES, state["frame"])
            ok, frame = cap.read()
            continue
        if not state["paused"]:
            ok, frame = cap.read()
            state["frame"] += 1
    cap.release()
    cv.destroyAllWindows()
    events.sort(key=lambda e: e["t"])
    out.write_text(json.dumps({
        "video": video.name, "fps": fps,
        "zones": [{"name": z.name, "polygon": z.polygon} for z in cfg.zones], "door_zone": cfg.door_zone,
        "frame_size": [cfg.process_width, None], "events": events,
    }, indent=1), encoding="utf-8")
    print(f"saved {len(events)} events to {out}")
