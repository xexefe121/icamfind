"""Record the real iCamFind web app using a CCTV-style feed as the webcam.

Needs local_stack.py running (real API code + deployed backend). Edge gets the feed as its fake camera,
the page is captured with the DevTools screencast (JPEG frames with timestamps, sharper than Playwright's
built-in recorder) and a drawn cursor, and the frames are assembled into a constant 30 fps MP4.

    python video/demo2/record_ui.py --feed video/demo2/feeds/hall.y4m --out video/demo2/rec/ui_hall.mp4
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

CURSOR_JS = """
(() => {
  const add = () => {
    if (document.getElementById('__cur')) return;
    const c = document.createElement('div');
    c.id = '__cur';
    c.style.cssText = 'position:fixed;left:0;top:0;width:22px;height:22px;margin:-3px 0 0 -3px;z-index:2147483647;' +
      'pointer-events:none;transition:transform .05s linear;';
    c.innerHTML = '<svg width="22" height="22" viewBox="0 0 22 22"><path d="M3 2l14 8-6 1.5 3.5 6.5-2.5 1.3-3.5-6.6L4 17z" ' +
      'fill="#fff" stroke="#111" stroke-width="1.4" stroke-linejoin="round"/></svg>';
    document.documentElement.appendChild(c);
    addEventListener('mousemove', (e) => { c.style.transform = `translate(${e.clientX}px, ${e.clientY}px)`; }, true);
    addEventListener('mousedown', () => { c.firstChild.style.transform = 'scale(.85)'; }, true);
    addEventListener('mouseup', () => { c.firstChild.style.transform = ''; }, true);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', add); else add();
})();
"""


class Screencast:
    def __init__(self, page, out_dir: Path):
        self.dir = out_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.frames: list[tuple[float, str]] = []
        self.marks: list[tuple[float, str]] = []
        self.cdp = page.context.new_cdp_session(page)
        self.cdp.on("Page.screencastFrame", self._frame)

    def _frame(self, ev):
        ts = ev["metadata"]["timestamp"]
        name = f"f{len(self.frames):06d}.jpg"
        (self.dir / name).write_bytes(base64.b64decode(ev["data"]))
        self.frames.append((ts, name))
        try:
            self.cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
        except Exception:
            pass

    def start(self, w, h):
        self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": w, "maxHeight": h})

    def mark(self, label: str):
        self.marks.append((time.time(), label))
        print(f"[{time.strftime('%H:%M:%S')}] {label}", flush=True)

    def stop(self):
        self.cdp.send("Page.stopScreencast")

    def assemble(self, out: Path, fps: int = 30):
        """Constant-rate video: each screencast frame is held until the next one arrives."""
        lst = self.dir / "frames.txt"
        lines = []
        for i, (ts, name) in enumerate(self.frames):
            nxt = self.frames[i + 1][0] if i + 1 < len(self.frames) else ts + 1 / fps
            lines.append(f"file '{name}'\nduration {max(0.001, nxt - ts):.4f}\n")
        lines.append(f"file '{self.frames[-1][1]}'\n")
        lst.write_text("".join(lines))
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-vf", f"fps={fps},scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-crf", "16",
                        "-pix_fmt", "yuv420p", str(out)], check=True)
        t0 = self.frames[0][0]
        (out.with_suffix(".marks.json")).write_text(json.dumps(
            [{"t": round(m - t0, 2), "label": l} for m, l in self.marks], indent=1))


def slow_type(page, selector, text, delay=0.07):
    page.click(selector)
    page.fill(selector, "")
    for ch in text:
        page.keyboard.type(ch)
        time.sleep(delay)


def glide(page, selector, steps=18):
    box = page.locator(selector).first.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=steps)
    time.sleep(0.25)


def click(page, selector):
    glide(page, selector)
    page.click(selector)


def ask(page, rec, question, label):
    click(page, '.tab[data-view="ask"]')
    time.sleep(0.8)
    glide(page, "#ask-input")
    slow_type(page, "#ask-input", question)
    time.sleep(0.4)
    prev = page.evaluate("() => { const c = document.querySelector('#answer-card'); return c && !c.hidden ? c.innerText : ''; }")
    page.press("#ask-input", "Enter")
    rec.mark(f"{label}: asked")
    page.wait_for_selector("#trace-list li", state="attached", timeout=30000)
    page.wait_for_function(
        "(prev) => { const c = document.querySelector('#answer-card'); return c && !c.hidden && c.innerText.trim() && c.innerText !== prev; }",
        arg=prev, timeout=120000)
    rec.mark(f"{label}: answered")
    time.sleep(1.5)
    try:
        page.wait_for_selector("#answer-card .here-pic img", timeout=20000)
    except Exception:
        pass
    time.sleep(4)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://localhost:8790")
    p.add_argument("--feed", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--place", default="Hall table")
    p.add_argument("--zone", default="30,185,590,300", help="x0,y0,x1,y1 in 640-wide processing pixels")
    p.add_argument("--w", type=int, default=1440)
    p.add_argument("--h", type=int, default=900)
    args = p.parse_args()
    out = Path(args.out)
    frames_dir = out.parent / (out.stem + "_frames")
    x0, y0, x1, y1 = (int(v) for v in args.zone.split(","))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=False, args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-video-capture={Path(args.feed).resolve()}", "--autoplay-policy=no-user-gesture-required",
            f"--window-size={args.w},{args.h + 120}"])
        ctx = browser.new_context(viewport={"width": args.w, "height": args.h}, timezone_id="Australia/Brisbane",
                                  locale="en-AU", permissions=["camera", "microphone"])
        ctx.add_init_script(CURSOR_JS)
        page = ctx.new_page()
        rec = Screencast(page, frames_dir)
        page.goto(f"{args.base}/")
        page.wait_for_selector("#login-btn:not([disabled])", timeout=20000)
        page.mouse.move(args.w / 2, args.h / 2)
        rec.start(args.w, args.h)
        time.sleep(1.5)
        rec.mark("sign in")
        click(page, "#login-btn")
        page.wait_for_selector("#app:not([hidden])", timeout=30000)
        time.sleep(2)

        rec.mark("camera setup")
        click(page, '.tab[data-view="camera"]')
        page.wait_for_selector("#cam-setup:not([hidden])", timeout=15000)
        time.sleep(1.5)
        click(page, "#cam-setup-btn")
        page.wait_for_selector("#wizard[open]", timeout=5000)
        time.sleep(1.5)
        click(page, "#wiz-next")
        page.wait_for_function("() => document.querySelector('#wiz-ready').textContent.includes('Ready')", timeout=90000)
        rec.mark("camera on")
        time.sleep(2.5)
        click(page, "#wiz-next")
        page.wait_for_selector("#wiz-step-2:not([hidden])")
        page.wait_for_function("() => document.querySelector('#draw-canvas').width === 640")
        time.sleep(1.5)
        box = page.locator("#draw-canvas").bounding_box()
        ch = page.evaluate("() => document.querySelector('#draw-canvas').height")
        sx, sy = box["width"] / 640, box["height"] / ch
        page.mouse.move(box["x"] + x0 * sx, box["y"] + y0 * sy, steps=20)
        time.sleep(0.4)
        page.mouse.down()
        page.mouse.move(box["x"] + x1 * sx, box["y"] + y1 * sy, steps=40)
        page.mouse.up()
        page.wait_for_selector("#name-panel:not([hidden])", timeout=5000)
        time.sleep(1)
        glide(page, "#place-name")
        slow_type(page, "#place-name", args.place)
        time.sleep(0.4)
        click(page, "#place-custom button[type=submit]")
        page.wait_for_selector("#name-panel", state="hidden")
        rec.mark("place drawn")
        time.sleep(2)
        click(page, "#wiz-next")
        page.wait_for_selector("#wiz-step-3:not([hidden])")
        time.sleep(2)
        click(page, "#wiz-next")
        page.wait_for_selector("#wizard", state="hidden", timeout=30000)
        page.wait_for_function("() => document.querySelector('#cam-status-text').textContent.includes('Watching')",
                               timeout=90000)
        rec.mark("watching")

        page.wait_for_selector("#event-toast:not([hidden])", timeout=180000)
        rec.mark("placed toast: " + page.inner_text("#event-toast").replace("\n", " ")[:80])
        try:
            page.wait_for_function("() => !/something/.test(document.querySelector('#event-toast').textContent)",
                                   timeout=8000)
            rec.mark("named toast: " + page.inner_text("#event-toast").replace("\n", " ")[:80])
        except Exception:
            rec.mark("toast not renamed")
        time.sleep(4)

        ask(page, rec, "Where are my keys?", "ask 1")
        if page.locator("#trace summary").count():
            click(page, "#trace summary")
            time.sleep(4)
        if page.locator("#answer-card .here-show").count():
            click(page, "#answer-card .here-show")
            time.sleep(5)
        rec.mark("waiting for pickup")
        page.click('.tab[data-view="camera"]') if "#camera" not in page.url else None
        try:
            page.wait_for_function(
                "() => { const t = document.querySelector('#event-toast'); return t && !t.hidden && /pick|took|gone/i.test(t.textContent); }",
                timeout=150000)
            rec.mark("pickup toast: " + page.inner_text("#event-toast").replace("\n", " ")[:80])
        except Exception:
            rec.mark("no pickup toast seen")
        time.sleep(5)

        ask(page, rec, "Where are my keys?", "ask 2")
        if page.locator("#trace summary").count():
            click(page, "#trace summary")
            time.sleep(1)
            page.mouse.wheel(0, 500)
            time.sleep(6)
        click(page, '.tab[data-view="things"]')
        time.sleep(5)
        rec.mark("end")
        rec.stop()
        browser.close()
    rec.assemble(out)
    print("wrote", out)


if __name__ == "__main__":
    main()
