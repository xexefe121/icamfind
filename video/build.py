"""Render the iCamFind launch video (1920x1080, 30 fps, H.264 + AAC).

    python video/build.py            (needs ffmpeg; voice-over from video/vo.py in video/vo/)

Pipeline: Pillow draws every still (title cards, framed app screenshots, architecture, results) and a
caption overlay per segment; ffmpeg animates each still with a gentle Ken Burns move, overlays the
caption, crossfades all segments, lays the per-scene voice-over at its scene start, adds a synthesized
piano/pad bed ducked under the voice, and normalizes loudness.
"""

from __future__ import annotations

import json
import math
import subprocess
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SHOTS = ROOT / "tests" / "ui" / "screenshots"
WORK = HERE / "work"
CARDS = HERE / "cards"
W, H, FPS, XF = 1920, 1080, 30, 0.5  # crossfade seconds
TEAL, OFFWHITE, CHAR, SOFT = (15, 118, 110), (246, 243, 238), (34, 34, 38), (110, 108, 104)
F = "C:/Windows/Fonts/"


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(F + name, size)


# ----------------------------------------------------------------------------- stills

def bg_warm(dark: bool = False) -> Image.Image:
    """Soft radial gradient background (warm off-white, or deep charcoal for the cold open)."""
    y, x = np.mgrid[0:H, 0:W]
    d = np.sqrt(((x - W * 0.5) / W) ** 2 + ((y - H * 0.45) / H) ** 2)
    if dark:
        base = np.array([28, 30, 34]) * (1 - 0.0) + 0
        img = np.clip(np.array([44, 44, 48])[None, None] * (1 - d[..., None] * 0.9) + base * d[..., None] * 0.5, 0, 255)
    else:
        img = np.clip(np.array(OFFWHITE)[None, None] - d[..., None] * np.array([18, 20, 24]), 0, 255)
    return Image.fromarray(img.astype(np.uint8), "RGB")


def logo(draw: ImageDraw.ImageDraw, cx: int, cy: int, scale: float = 1.0, color=TEAL, text_color=CHAR):
    r = int(46 * scale)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=max(4, int(12 * scale)))
    draw.ellipse([cx - r // 3, cy - r // 3, cx + r // 3, cy + r // 3], fill=color)
    f = font("segoeuib.ttf", int(96 * scale))
    draw.text((cx + r + int(28 * scale), cy), "iCamFind", font=f, fill=text_color, anchor="lm")


def centered(draw, text, y, f, fill, width=1500, spacing=14):
    lines, line = [], ""
    for word in text.split():
        test = (line + " " + word).strip()
        if draw.textlength(test, font=f) > width and line:
            lines.append(line)
            line = word
        else:
            line = test
    lines.append(line)
    total = len(lines) * (f.size + spacing)
    yy = y - total // 2
    for ln in lines:
        draw.text((W // 2, yy), ln, font=f, fill=fill, anchor="ma")
        yy += f.size + spacing


def card_text(lines: list[tuple[str, str, int, tuple]], dark=False, accent_line=True) -> Image.Image:
    img = bg_warm(dark)
    d = ImageDraw.Draw(img)
    y = H // 2 - sum(s + 30 for _, _, s, _ in lines) // 2 + 20
    for text, fname, size, color in lines:
        f = font(fname, size)
        centered(d, text, y + size // 2, f, color)
        y += size + 30
    if accent_line:
        d.rounded_rectangle([W // 2 - 60, y + 30, W // 2 + 60, y + 36], 3, fill=TEAL)
    return img


def framed(shot: Path, caption_top: str | None = None, mobile: bool = False) -> Image.Image:
    """App screenshot in a premium rounded 'window' card with a soft shadow on the warm background."""
    img = bg_warm()
    s = Image.open(shot).convert("RGB")
    max_w, max_h = (560, 900) if mobile else (1560, 900)
    k = min(max_w / s.width, max_h / s.height)
    s = s.resize((int(s.width * k), int(s.height * k)), Image.LANCZOS)
    bar = 0 if mobile else 38
    fw, fh = s.width, s.height + bar
    x0, y0 = (W - fw) // 2, (H - fh) // 2 - (10 if caption_top is None else -30)
    shadow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shadow).rounded_rectangle([x0 + 8, y0 + 18, x0 + fw + 8, y0 + fh + 18], 28, fill=110)
    shadow = shadow.filter(ImageFilter.GaussianBlur(26))
    img.paste((0, 0, 0), (0, 0), shadow.point(lambda v: v // 2))
    mask = Image.new("L", (fw, fh), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, fw - 1, fh - 1], 26 if not mobile else 46, fill=255)
    frame = Image.new("RGB", (fw, fh), (252, 251, 249))
    fd = ImageDraw.Draw(frame)
    if bar:
        for i, c in enumerate([(238, 106, 94), (245, 190, 79), (98, 197, 84)]):
            fd.ellipse([22 + i * 26, 13, 36 + i * 26, 27], fill=c)
        fd.rounded_rectangle([fw // 2 - 260, 8, fw // 2 + 260, 30], 11, fill=(236, 233, 228))
        fd.text((fw // 2, 19), "icamfind · your home", font=font("segoeui.ttf", 15), fill=SOFT, anchor="mm")
    frame.paste(s, (0, bar))
    img.paste(frame, (x0, y0), mask)
    if caption_top:
        ImageDraw.Draw(img).text((W // 2, y0 - 42), caption_top, font=font("segoeuib.ttf", 34), fill=TEAL, anchor="mm")
    return img


def results_card() -> Image.Image:
    img = bg_warm()
    d = ImageDraw.Draw(img)
    d.text((W // 2, 150), "Measured, not claimed", font=font("segoeuib.ttf", 64), fill=CHAR, anchor="mm")
    d.text((W // 2, 222), "Reproducible synthetic benchmark (5 seeds, 65 events) + live cloud tests",
           font=font("segoeui.ttf", 30), fill=SOFT, anchor="mm")
    stats = [("0.98", "F1 score\nput-down / pick-up / move"), ("100%", "of events placed\nin the right zone"),
             ("~1.5 s", "from hand-away\nto memory"), ("= Python", "browser OpenCV 5\nWASM engine parity"),
             ("49/49", "live end-to-end\ncloud checks")]
    cw, gap = 316, 28
    x = (W - (cw * 5 + gap * 4)) // 2
    for big, small in stats:
        d.rounded_rectangle([x, 340, x + cw, 760], 28, fill=(255, 255, 255), outline=(228, 224, 216), width=2)
        d.text((x + cw // 2, 480), big, font=font("segoeuib.ttf", 84 if len(big) < 6 else 62), fill=TEAL, anchor="mm")
        for i, ln in enumerate(small.split("\n")):
            d.text((x + cw // 2, 610 + i * 42), ln, font=font("segoeui.ttf", 29), fill=CHAR, anchor="mm")
        x += cw + gap
    d.text((W // 2, 850), "Details: docs/EVALUATION.md · docs/BROWSER_PARITY.md · docs/evidence/",
           font=font("segoeui.ttf", 26), fill=SOFT, anchor="mm")
    return img


def arch_card() -> Image.Image:
    img = bg_warm()
    a = Image.open(ROOT / "docs" / "architecture.png").convert("RGB")
    k = min(1800 / a.width, 1000 / a.height)
    a = a.resize((int(a.width * k), int(a.height * k)), Image.LANCZOS)
    img.paste(a, ((W - a.width) // 2, (H - a.height) // 2))
    return img


def end_card() -> Image.Image:
    img = bg_warm()
    d = ImageDraw.Draw(img)
    logo(d, W // 2 - 230, 250, 1.25)
    d.text((W // 2, 390), "Memory you can ask.", font=font("georgiai.ttf", 66), fill=CHAR, anchor="mm")
    d.rounded_rectangle([W // 2 - 700, 480, W // 2 + 700, 570], 45, fill=(255, 255, 255), outline=(220, 216, 208), width=2)
    d.text((W // 2, 525), "o91wekq4k6.execute-api.ap-southeast-2.amazonaws.com", font=font("segoeuib.ttf", 40),
           fill=TEAL, anchor="mm")
    d.text((W // 2, 620), "Sign up and open the Demo home", font=font("segoeui.ttf", 34), fill=SOFT, anchor="mm")
    d.text((W // 2, 720), "Team iCamFind: Ela S & Sammo  ·  OpenCV AI Competition 2026, powered by AWS",
           font=font("segoeui.ttf", 30), fill=SOFT, anchor="mm")
    return img


def caption_png(text: str, path: Path) -> None:
    """Lower-third caption: white text on a translucent dark band, max 2 lines."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if text:
        d = ImageDraw.Draw(img)
        f = font("segoeui.ttf", 38)
        words, lines, line = text.split(), [], ""
        for wd in words:
            t = (line + " " + wd).strip()
            if d.textlength(t, font=f) > 1500 and line:
                lines.append(line)
                line = wd
            else:
                line = t
        lines.append(line)
        lines = lines[:2]
        bw = int(max(d.textlength(ln, font=f) for ln in lines)) + 80
        bh = len(lines) * 52 + 34
        x0, y0 = (W - bw) // 2, H - 70 - bh
        d.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], 22, fill=(20, 20, 24, 170))
        for i, ln in enumerate(lines):
            d.text((W // 2, y0 + 18 + i * 52), ln, font=f, fill=(255, 255, 255, 255), anchor="ma")
    img.save(path)


# ----------------------------------------------------------------------------- plan

def plain(ssml: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", ssml).replace("  ", " ").strip()


def sentences(text: str) -> list[str]:
    import re
    parts = re.split(r"(?<=[.?!])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def build_plan(lengths: dict) -> list[dict]:
    from vo import SCENES  # noqa: E402

    s = {n: plain(t) for n, t in SCENES.items()}
    stills = {
        1: [card_text([("There's a moment we all know.", "georgiai.ttf", 84, (240, 236, 228))], dark=True),
            card_text([("You put something down…", "georgiai.ttf", 84, (240, 236, 228)),
                       ("and the world moves on without it.", "georgiai.ttf", 64, (200, 196, 188))], dark=True),
            card_text([("For someone living with dementia,", "georgiai.ttf", 70, (240, 236, 228)),
                       ("it's the moment the day starts to slip away.", "georgiai.ttf", 60, (200, 196, 188))], dark=True)],
        2: [card_text([("“Where did I put it?”", "georgiai.ttf", 120, CHAR)]),
            card_text([("It was never really about the keys.", "georgiai.ttf", 72, CHAR)]),
            card_text([("Independence.  Dignity.  Trusting your own mind.", "segoeuil.ttf", 70, TEAL)])],
        3: [None],  # logo card drawn below
        "team": [card_text([("Team iCamFind", "segoeuib.ttf", 96, CHAR),
                            ("Built by Ela S & Sammo", "segoeui.ttf", 54, TEAL)])],
        4: [framed(SHOTS / "c-setup-card.png", "1 · Use this computer's camera"),
            framed(SHOTS / "c-wizard-2-places.png", "2 · Draw your places once"),
            framed(SHOTS / "c-camera-live.png", "3 · OpenCV 5 watches, right in the browser"),
            framed(SHOTS / "c-event-toast.png", "Remembered — only a small photo leaves the room")],
        5: [framed(SHOTS / "d-answer-trace.png", "“Where are my keys?”"),
            framed(SHOTS / "d-answer-here.png", "It looks again — and circles the answer"),
            framed(SHOTS / "d-camera-pulse.png", "Not there? It scans the room and fixes its memory")],
        6: [framed(SHOTS / "m-camera-mobile.png", "A reminder at the door", mobile=True),
            framed(SHOTS / "g-routine-check.png", "Evening pills untouched? A gentle reminder first"),
            framed(SHOTS / "g-approval-banner.png", "A person always approves before a carer is contacted")],
        7: [arch_card(), arch_card(), framed(SHOTS / "e-demo-camera.png", "Try it now: the shared Demo home")],
        8: [results_card()],
        9: [end_card()],
    }
    logo_card = bg_warm()
    logo(ImageDraw.Draw(logo_card), W // 2 - 230, 470, 1.4)
    ImageDraw.Draw(logo_card).text((W // 2, 640), "Memory you can ask.", font=font("georgiai.ttf", 70), fill=SOFT,
                                   anchor="mm")
    stills[3] = [logo_card]

    # Ken Burns per scene: (zoom start, zoom end, pan x fraction start->end, pan y)
    moves = {1: (1.0, 1.06, .5, .5, .5, .5), 2: (1.0, 1.05, .5, .5, .5, .5), 3: (1.0, 1.04, .5, .5, .5, .5),
             "team": (1.0, 1.02, .5, .5, .5, .5), 4: (1.0, 1.05, .5, .5, .5, .5), 5: (1.0, 1.06, .5, .5, .5, .5),
             6: (1.0, 1.05, .5, .5, .5, .5), 7: None, 8: (1.0, 1.03, .5, .5, .5, .5), 9: (1.0, 1.03, .5, .5, .5, .5)}
    order = [1, 2, 3, "team", 4, 5, 6, 7, 8, 9]
    plan = []
    for n in order:
        imgs = stills[n]
        if n == "team":
            plan.append({"scene": n, "img": imgs[0], "dur": 3.0, "caption": "", "move": moves[n], "vo": None})
            continue
        dur = lengths[str(n)] + 0.9 + XF  # lead-in + tail + crossfade overlap
        sents = sentences(s[n])
        k = len(imgs)
        # distribute sentences over images by character count
        groups = [[] for _ in range(k)]
        total = sum(len(x) for x in sents)
        acc = 0
        for sent in sents:
            idx = min(k - 1, int(acc / total * k))
            groups[idx].append(sent)
            acc += len(sent)
        weights = [max(1, sum(len(x) for x in g)) for g in groups]
        for i, img in enumerate(imgs):
            seg = dur * weights[i] / sum(weights)
            move = moves[n]
            if n == 7:  # architecture: zoom into the edge half, then the AWS half, then demo home
                move = [(1.15, 1.35, .25, .22, .5, .5), (1.15, 1.35, .72, .78, .5, .5), (1.0, 1.04, .5, .5, .5, .5)][i]
            plan.append({"scene": n, "img": img, "dur": round(seg, 2), "caption": " ".join(groups[i]), "move": move,
                         "vo": n if i == 0 else None})
    return plan


# ----------------------------------------------------------------------------- music

def synth_music(seconds: float, path: Path) -> None:
    sr = 44100
    t_total = int(seconds * sr)
    out = np.zeros(t_total)
    # Cmaj9 - Am9 - Fmaj7 - G6sus, 4 s each, piano arpeggio + warm pad
    chords = [[48, 55, 64, 71, 74], [45, 52, 60, 67, 71], [41, 48, 57, 64, 69], [43, 50, 59, 62, 69]]

    def hz(m):
        return 440 * 2 ** ((m - 69) / 12)

    bar = 4.0
    n_bars = int(math.ceil(seconds / bar))
    rng = np.random.default_rng(3)
    for b in range(n_bars):
        ch = chords[b % 4]
        start = int(b * bar * sr)
        # pad
        L = int(bar * 1.6 * sr)
        tt = np.arange(L) / sr
        env = np.minimum(1, tt / 1.2) * np.exp(-tt / 5.0)
        pad = sum(np.sin(2 * np.pi * hz(m) * tt * (1 + d)) for m in ch[:4] for d in (-0.0015, 0.0015)) / 8
        if start >= t_total:
            break
        seg = slice(start, min(t_total, start + L))
        out[seg] += 0.10 * (pad * env)[: seg.stop - seg.start]
        # piano arpeggio (soft): 8 notes per bar
        for i in range(8):
            m = ch[[1, 2, 3, 4, 3, 2, 3, 4][i]] + 12
            s0 = start + int(i * bar / 8 * sr)
            if s0 >= t_total:
                break
            L2 = int(2.2 * sr)
            t2 = np.arange(L2) / sr
            note = sum(np.sin(2 * np.pi * hz(m) * k * t2) / k ** 1.6 for k in range(1, 5))
            env2 = (1 - np.exp(-t2 * 80)) * np.exp(-t2 * 2.4)
            vel = 0.055 + 0.015 * rng.random()
            seg2 = slice(s0, min(t_total, s0 + L2))
            out[seg2] += vel * (note * env2)[: seg2.stop - seg2.start]
    # simple reverb: convolve with decaying noise impulse
    ir_len = int(2.2 * sr)
    ir = rng.standard_normal(ir_len) * np.exp(-np.arange(ir_len) / sr * 3.2)
    ir /= np.abs(ir).sum() / 6
    wet = np.fft.irfft(np.fft.rfft(out, t_total + ir_len) * np.fft.rfft(ir, t_total + ir_len))[:t_total]
    mix = 0.75 * out + 0.25 * wet
    fade = int(3 * sr)
    mix[:fade] *= np.linspace(0, 1, fade)
    mix[-fade:] *= np.linspace(1, 0, fade)
    mix /= np.abs(mix).max() + 1e-9
    data = (mix * 0.8 * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data.tobytes())


# ----------------------------------------------------------------------------- render

def run(cmd: list[str]) -> None:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit(f"ffmpeg failed: {' '.join(cmd[:6])}...\n{res.stderr[-1500:]}")


def chunks(text: str, max_chars: int = 92) -> list[str]:
    """Split a caption into pieces that fit two lines, at sentence and then comma boundaries."""
    import re
    out = []
    for sent in sentences(text):
        if len(sent) <= max_chars:
            out.append(sent)
            continue
        part = ""
        for piece in re.split(r"(?<=[,:;])\s+", sent):
            if part and len(part) + len(piece) + 1 > max_chars:
                out.append(part)
                part = piece
            else:
                part = (part + " " + piece).strip()
        if part:
            out.append(part)
    merged = []  # join short neighbours so captions don't flicker
    for c in out:
        if merged and len(merged[-1]) + len(c) + 1 <= max_chars:
            merged[-1] = merged[-1] + " " + c
        else:
            merged.append(c)
    return merged


def render_segment(i: int, seg: dict) -> Path:
    still = CARDS / f"seg{i:02d}.png"
    seg["img"].save(still)
    pieces = chunks(seg["caption"]) if seg["caption"] else []
    caps = []
    for j, piece in enumerate(pieces or [""]):
        cap = CARDS / f"cap{i:02d}_{j}.png"
        caption_png(piece, cap)
        caps.append(cap)
    out = WORK / f"seg{i:02d}.mp4"
    frames = int(round(seg["dur"] * FPS))
    import os
    if os.environ.get("REUSE_SEGMENTS") == "1" and out.exists():
        return out
    z0, z1, x0, x1, y0, y1 = seg["move"]
    # zoompan on a 2x upscale for smooth motion
    zexpr = f"{z0}+({z1}-{z0})*on/{frames}"
    xexpr = f"(iw-iw/zoom)*({x0}+({x1}-{x0})*on/{frames})"
    yexpr = f"(ih-ih/zoom)*({y0}+({y1}-{y0})*on/{frames})"
    # caption timing: proportional to text length over the spoken part of the segment
    lead = 0.35 if seg.get("vo") else 0.1
    speak = max(0.5, seg["dur"] - lead - XF)
    total_chars = sum(len(p) for p in pieces) or 1
    t = lead
    graph = [f"[0:v]scale={W * 2}:{H * 2},zoompan=z='{zexpr}':x='{xexpr}':y='{yexpr}':d={frames}:s={W}x{H}:fps={FPS},"
             f"format=yuv420p[b0]"]
    inputs = ["-loop", "1", "-i", str(still)]
    for j, cap in enumerate(caps):
        inputs += ["-loop", "1", "-i", str(cap)]
        t0 = t
        t1 = t + speak * (len(pieces[j]) / total_chars if pieces else 1)
        t = t1
        end = seg["dur"] if j == len(caps) - 1 else t1
        graph.append(f"[b{j}][{j + 1}:v]overlay=0:0:format=auto:enable='between(t,{t0 if j else 0:.2f},{end:.2f})'[b{j + 1}]")
    graph.append(f"[b{len(caps)}]format=yuv420p[v]")
    run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(graph), "-map", "[v]", "-frames:v", str(frames),
         "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", str(out)])
    return out


def main() -> None:
    import sys

    sys.path.insert(0, str(HERE))
    WORK.mkdir(exist_ok=True)
    CARDS.mkdir(exist_ok=True)
    lengths = json.loads((HERE / "vo" / "lengths.json").read_text())
    plan = build_plan(lengths)
    segs = [render_segment(i, s) for i, s in enumerate(plan)]
    durs = [s["dur"] for s in plan]

    # video: chain xfades
    inputs = []
    for p in segs:
        inputs += ["-i", str(p)]
    fc, last, offset = [], "[0:v]", 0.0
    starts = [0.0]
    for i in range(1, len(segs)):
        offset += durs[i - 1] - XF
        starts.append(offset)
        tag = f"[x{i}]"
        fc.append(f"{last}[{i}:v]xfade=transition=fade:duration={XF}:offset={offset:.3f}{tag}")
        last = tag
    total = starts[-1] + durs[-1]
    video = WORK / "video_only.mp4"
    run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(fc), "-map", last, "-c:v", "libx264", "-preset", "medium",
         "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(FPS), str(video)])

    # audio: voice-over at each scene's first segment + 0.35 s, music bed ducked under it
    music = WORK / "music.wav"
    synth_music(total + 1, music)
    a_inputs, a_fc, labels = ["-i", str(music)], [], []
    k = 1
    for i, seg in enumerate(plan):
        if seg["vo"]:
            a_inputs += ["-i", str(HERE / "vo" / f"scene{seg['vo']}.mp3")]
            delay = int((starts[i] + 0.35) * 1000)
            a_fc.append(f"[{k}:a]aresample=44100,adelay={delay}|{delay},apad[v{k}]")
            labels.append(f"[v{k}]")
            k += 1
    a_fc.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest,volume=1.6[vo]")
    a_fc.append("[vo]asplit=2[vo1][vo2]")
    a_fc.append("[0:a]aresample=44100,volume=0.30[mus]")
    a_fc.append("[mus][vo1]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck]")
    a_fc.append(f"[duck][vo2]amix=inputs=2:normalize=0:duration=first,atrim=0:{total:.2f},"
                f"loudnorm=I=-16:TP=-1.5:LRA=11[aout]")
    audio = WORK / "audio.m4a"
    run(["ffmpeg", "-y", *a_inputs, "-filter_complex", ";".join(a_fc), "-map", "[aout]", "-c:a", "aac", "-b:a", "192k",
         "-ar", "48000", str(audio)])

    out = HERE / "iCamFind_launch.mp4"
    run(["ffmpeg", "-y", "-i", str(video), "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "copy",
         "-shortest", "-movflags", "+faststart", str(out)])
    # thumbnail from the logo card
    logo_idx = next(i for i, s in enumerate(plan) if s["scene"] == 3)
    Image.open(CARDS / f"seg{logo_idx:02d}.png").resize((1280, 720), Image.LANCZOS).save(HERE / "thumbnail.png")
    print(json.dumps({"output": str(out), "duration_s": round(total, 2), "segments": len(plan)}))


if __name__ == "__main__":
    main()
