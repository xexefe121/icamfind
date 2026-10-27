"""Edit the CCTV demo video (1920x1080, 30 fps, H.264 + AAC) from the feeds, the UI recording and the voice-over.

    python video/demo2/build_demo.py [--ui rec/ui_hall2.mp4]

Inputs: feeds/*.mp4 (CCTV-graded stock clips, *_det.mp4 with real engine detections burned in),
rec/<ui>.mp4 + .marks.json (record_ui.py), vo/sceneN.mp3 (vo_demo.py). Cards and music come from video/build.py.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import build as lv  # noqa: E402  (launch-video helpers: cards, captions, music)

FEEDS, WORK = HERE / "feeds", HERE / "work"
W, H, FPS, XF = 1920, 1080, 30, 0.45
UI_W, UI_H, BAR = 1440, 900, 38


def run(cmd):
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit(f"ffmpeg failed: {' '.join(map(str, cmd[:8]))}...\n{res.stderr[-2000:]}")


def ui_frame_bg(path: Path) -> tuple[int, int]:
    """Warm background with a rounded browser window; returns where the page goes."""
    img = lv.bg_warm()
    fw, fh = UI_W, UI_H + BAR
    x0, y0 = (W - fw) // 2, (H - fh) // 2
    shadow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shadow).rounded_rectangle([x0 + 8, y0 + 18, x0 + fw + 8, y0 + fh + 18], 24, fill=110)
    img.paste((0, 0, 0), (0, 0), shadow.filter(ImageFilter.GaussianBlur(24)).point(lambda v: v // 2))
    frame = Image.new("RGB", (fw, fh), (252, 251, 249))
    fd = ImageDraw.Draw(frame)
    for i, c in enumerate([(238, 106, 94), (245, 190, 79), (98, 197, 84)]):
        fd.ellipse([20 + i * 24, 12, 33 + i * 24, 25], fill=c)
    fd.rounded_rectangle([fw // 2 - 260, 7, fw // 2 + 260, 30], 11, fill=(236, 233, 228))
    fd.text((fw // 2, 18), "icamfind · your home", font=lv.font("segoeui.ttf", 15), fill=lv.SOFT, anchor="mm")
    mask = Image.new("L", (fw, fh), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, fw - 1, fh - 1], 18, fill=255)
    img.paste(frame, (x0, y0), mask)
    img.save(path)
    return x0, y0 + BAR


def tag_png(text: str, path: Path) -> None:
    """Small top-left chapter tag."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if text:
        d = ImageDraw.Draw(img)
        f = lv.font("segoeuib.ttf", 30)
        tw = int(d.textlength(text, font=f))
        d.rounded_rectangle([48, 44, 48 + tw + 56, 100], 28, fill=(15, 118, 110, 230))
        d.text((76, 72), text, font=f, fill=(255, 255, 255, 255), anchor="lm")
    img.save(path)


def render(i: int, seg: dict, ui: Path, ui_pos: tuple[int, int]) -> Path:
    out = WORK / f"seg{i:02d}.mp4"
    dur = seg["dur"]
    cap, tag = WORK / f"cap{i:02d}.png", WORK / f"tag{i:02d}.png"
    lv.caption_png(seg.get("caption", ""), cap)
    tag_png(seg.get("tag", ""), tag)
    kind = seg["kind"]
    if kind == "grid":
        inputs, parts = [], []
        for j, (name, t0) in enumerate(seg["cams"]):
            inputs += ["-ss", f"{t0}", "-i", str(FEEDS / name)]
            parts.append(f"[{j}:v]fps={FPS},scale=960:540,setsar=1[g{j}]")
        graph = parts + [f"[g0][g1][g2][g3]xstack=inputs=4:layout=0_0|960_0|0_540|960_540[base]"]
        n = 4
    elif kind == "cctv":
        sp = seg.get("speed", 1.0)
        inputs = ["-ss", f"{seg['t0']}", "-i", str(FEEDS / seg["src"])]
        graph = [f"[0:v]setpts=PTS/{sp},fps={FPS},scale={W}:{H}:flags=lanczos,setsar=1[base]"]
        n = 1
    elif kind == "ui":
        sp = seg.get("speed", 1.0)
        bg = WORK / "uibg.png"
        inputs = ["-loop", "1", "-i", str(bg), "-ss", f"{seg['t0']}", "-i", str(ui)]
        graph = [f"[1:v]setpts=PTS/{sp},fps={FPS},setsar=1[pg]", f"[0:v][pg]overlay={ui_pos[0]}:{ui_pos[1]}:shortest=1[base]"]
        if seg.get("zoom"):
            zx, zy, zw = seg["zoom"]  # zoom into a page region: x, y, width in page pixels
            zh = zw * H / W
            graph[-1] = graph[-1].replace("[base]", "[full]")
            graph.append(f"[full]crop={zw}:{zh:.0f}:{ui_pos[0] + zx}:{ui_pos[1] + zy},scale={W}:{H}:flags=lanczos[base]")
        n = 2
    else:  # still
        inputs = ["-loop", "1", "-i", str(seg["img"])]
        frames = int(dur * FPS)
        graph = [f"[0:v]scale={W * 2}:{H * 2},zoompan=z='1.0+0.04*on/{frames}':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':"
                 f"d={frames}:s={W}x{H}:fps={FPS}[base]"]
        n = 1
    inputs += ["-loop", "1", "-i", str(cap), "-loop", "1", "-i", str(tag)]
    graph.append(f"[base][{n}:v]overlay=0:0:format=auto[c1]")
    graph.append(f"[c1][{n + 1}:v]overlay=0:0:format=auto,format=yuv420p[v]")
    run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(graph), "-map", "[v]", "-frames:v", str(int(dur * FPS)),
         "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", str(out)])
    return out


def plan(m: dict, lengths: dict) -> list[dict]:
    """m: UI recording marks by label prefix -> seconds. Durations follow the voice-over."""
    det = lambda name: json.loads((FEEDS / f"{name}_events.json").read_text())  # noqa: E731
    hall_ev = det("hall")
    put, pick = hall_ev[0]["ts"], hall_ev[-1]["ts"]
    bed = det("bedroom")[0]["ts"]
    s = []
    s.append(dict(kind="grid", dur=max(7.5, lengths["1"] + 1.0), vo=1, tag="Live demo · 4 home cameras",
                  cams=[("hallway.mp4", 0.5), ("hall_det.mp4", 30), ("bedroom.mp4", 0), ("kitchen.mp4", 0)]))
    s.append(dict(kind="cctv", src="hallway.mp4", t0=2.0, dur=lengths["2"] + 1.0, vo=2))
    a, b = m["camera setup"] - 0.5, m["watching"] + 1.5
    d3 = lengths["3"] + 1.2
    s.append(dict(kind="ui", t0=a, speed=(b - a) / d3, dur=d3, vo=3, tag="Setup: one box",
                  caption="Draw around the place where things get put down"))
    s.append(dict(kind="cctv", src="hall_det.mp4", t0=put - 9.0, dur=11.0, vo=4, tag="CAM 2 · real OpenCV detection"))
    s.append(dict(kind="ui", t0=m["placed toast"] - 0.8, dur=6.5, tag="Named by Claude", zoom=(480, 360, 960)))
    s.append(dict(kind="cctv", src="bedroom_det.mp4", t0=max(0, bed - 4.5), dur=max(7.0, lengths["5"] + 1.0), vo=5,
                  tag="CAM 3 · bedroom"))
    a1, r1 = m["ask 1: asked"] - 3.8, m["ask 1: answered"]
    s.append(dict(kind="ui", t0=a1, speed=1.4, dur=(r1 + 0.6 - a1) / 1.4, vo=6, tag="Ask"))
    s.append(dict(kind="ui", t0=r1 + 0.6, dur=max(5.5, lengths["6"] - (r1 + 0.6 - a1) / 1.4 + XF + 1.5),
                  tag="It looked again, just now", zoom=(80, 120, 1280)))
    s.append(dict(kind="ui", t0=m["waiting for pickup"] - 5.0, dur=4.5, tag="Shown on the live camera"))
    s.append(dict(kind="cctv", src="hall_det.mp4", t0=pick - 6.5, dur=7.5, vo=7, tag="CAM 2 · picked up"))
    # Question typed, a moment of "Looking...", then a cut to the answer (the wall-clock wait is not shown).
    a2, q2, r2 = m["ask 2: asked"] - 3.8, m["ask 2: asked"] + 2.0, m["ask 2: answered"]
    s.append(dict(kind="ui", t0=a2, speed=1.2, dur=(q2 - a2) / 1.2, tag="Ask again"))
    s.append(dict(kind="ui", t0=r2 - 0.3, dur=max(6.5, lengths["7"] - 7.5 - (q2 - a2) / 1.2 + 2 * XF + 2.0),
                  tag="Honest when it's gone", zoom=(80, 120, 1280)))
    s.append(dict(kind="still", img=WORK / "arch.png", dur=lengths["8"] / 2 + 1.0, vo=8))
    s.append(dict(kind="still", img=WORK / "results.png", dur=lengths["8"] / 2 + 1.0))
    s.append(dict(kind="still", img=WORK / "end.png", dur=lengths["9"] + 4.0, vo=9))
    return s


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--ui", default=str(HERE / "rec" / "ui_hall2.mp4"))
    p.add_argument("--out", default=str(HERE / "iCamFind_demo_cctv.mp4"))
    args = p.parse_args()
    ui = Path(args.ui)
    WORK.mkdir(exist_ok=True)
    marks = {}
    for mk in json.loads(ui.with_suffix(".marks.json").read_text()):
        key = mk["label"].split(":")[0] if not mk["label"].startswith("ask") else ":".join(mk["label"].split(":")[:2])
        marks.setdefault(key, mk["t"])
    lengths = json.loads((HERE / "vo" / "lengths.json").read_text())
    ui_pos = ui_frame_bg(WORK / "uibg.png")
    lv.arch_card().save(WORK / "arch.png")
    lv.results_card().save(WORK / "results.png")
    end = lv.end_card()
    d = ImageDraw.Draw(end)
    d.text((W // 2, 840), "Camera footage: Pexels stock video, graded to look like CCTV. The app, OpenCV detections and "
           "agent answers are real, recorded live.", font=lv.font("segoeui.ttf", 24), fill=lv.SOFT, anchor="mm")
    end.save(WORK / "end.png")

    segs = plan(marks, lengths)
    files = [render(i, sg, ui, ui_pos) for i, sg in enumerate(segs)]
    durs = [sg["dur"] for sg in segs]
    inputs, fc, last, offset, starts = [], [], "[0:v]", 0.0, [0.0]
    for f in files:
        inputs += ["-i", str(f)]
    for i in range(1, len(files)):
        offset += durs[i - 1] - XF
        starts.append(offset)
        fc.append(f"{last}[{i}:v]xfade=transition=fade:duration={XF}:offset={offset:.3f}[x{i}]")
        last = f"[x{i}]"
    total = starts[-1] + durs[-1]
    video = WORK / "video_only.mp4"
    run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(fc), "-map", last, "-c:v", "libx264", "-preset", "medium",
         "-crf", "17", "-pix_fmt", "yuv420p", "-r", str(FPS), str(video)])

    music = WORK / "music.wav"
    lv.synth_music(total + 1, music)
    a_in, a_fc, labels, k = ["-i", str(music)], [], [], 1
    for i, sg in enumerate(segs):
        if sg.get("vo"):
            a_in += ["-i", str(HERE / "vo" / f"scene{sg['vo']}.mp3")]
            delay = int((starts[i] + 0.4) * 1000)
            a_fc.append(f"[{k}:a]aresample=44100,adelay={delay}|{delay},apad[v{k}]")
            labels.append(f"[v{k}]")
            k += 1
    a_fc += [f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest,volume=1.6[vo]",
             "[vo]asplit=2[vo1][vo2]", "[0:a]aresample=44100,volume=0.26[mus]",
             "[mus][vo1]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck]",
             f"[duck][vo2]amix=inputs=2:normalize=0:duration=first,atrim=0:{total:.2f},loudnorm=I=-16:TP=-1.5:LRA=11[aout]"]
    audio = WORK / "audio.m4a"
    run(["ffmpeg", "-y", *a_in, "-filter_complex", ";".join(a_fc), "-map", "[aout]", "-c:a", "aac", "-b:a", "192k",
         "-ar", "48000", str(audio)])
    run(["ffmpeg", "-y", "-i", str(video), "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c", "copy", "-shortest",
         "-movflags", "+faststart", args.out])
    print(json.dumps({"output": args.out, "duration_s": round(total, 1), "segments": len(segs)}))


if __name__ == "__main__":
    main()
