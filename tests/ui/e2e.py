"""End-to-end browser test of the iCamFind web app against the simulated backend.

Runs the real app (cloud/src/web) in headless Microsoft Edge with a fake webcam that plays the
benchmark room (Y4M), through sign-in, camera setup with place drawing, live detection with
OpenCV 5 WebAssembly, asking, the "Here it is" view, things, routines, approvals, pairing,
reload/resume, accessibility checks and sign-out. Screenshots go to tests/ui/screenshots/.

    python tests/ui/e2e.py [--y4m path/to/room.y4m] [--opencv path/to/opencv.js] [--cdn] [--headed]

--cdn also runs a pass that loads OpenCV.js from jsDelivr (real CSP + SRI check).
Exit code 0 when every scenario passes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
import urllib.request
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mock_server  # noqa: E402

SHOTS = HERE / "screenshots"
OPENCV_URL = "https://cdn.jsdelivr.net/npm/@techstark/opencv-js@5.0.0-release.1/dist/opencv.js"
SCRATCH = Path("Y:/Temp/claude/N--iFindcam/244541ac-696d-4c63-9703-f9a8684f6767/scratchpad")
DEFAULT_Y4M = SCRATCH / "webcam_test" / "room.y4m"
DEFAULT_OPENCV = SCRATCH / "ocvjs" / "opencv.js"
ZONES = mock_server.DEMO_ZONES  # benchmark room zones in 640x480 processing pixels

USER_WORDS = re.compile(r"\b(tokens?|devices?|api|household)\b", re.I)
EXPECTED = {"401": False}  # set while a scenario provokes a 401 on purpose (browser logs it as an error)


class Suite:
    def __init__(self):
        self.results: list[tuple[str, bool, str]] = []
        self.console_errors: list[str] = []

    def run(self, name: str, fn, *args):
        start = time.time()
        before = len(self.console_errors)
        try:
            fn(*args)
            new_errors = self.console_errors[before:]
            if new_errors:
                raise AssertionError("console errors: " + " | ".join(new_errors[:5]))
            self.results.append((name, True, f"{time.time() - start:.1f}s"))
            print(f"PASS {name} ({time.time() - start:.1f}s)", flush=True)
        except Exception as exc:  # keep going; report at the end
            msg = f"{type(exc).__name__}: {(str(exc).splitlines() or [repr(exc)])[0]}"
            page = args[0] if args and hasattr(args[0], "screenshot") else None
            if page is not None:
                try:
                    shot(page, "FAIL-" + name.split()[0])
                    print("  state:", page.evaluate("""() => ({hash: location.hash,
                        wizReady: (document.querySelector('#wiz-ready') || {}).textContent,
                        wizOpen: !!document.querySelector('#wizard[open]'),
                        wizErr: (document.querySelector('#wiz-finish-error') || {}).textContent,
                        camStatus: (document.querySelector('#cam-status-text') || {}).textContent,
                        toast: (document.querySelector('#toast') || {}).textContent})"""), flush=True)
                except Exception:
                    pass
            self.results.append((name, False, msg))
            print(f"FAIL {name}: {msg}", flush=True)
            traceback.print_exc()

    def watch(self, page, tag: str):
        def on_console(m):
            if m.type == "error":
                if EXPECTED["401"] and "status of 401" in m.text:
                    return
                self.console_errors.append(f"[{tag}] {m.text}")

        page.on("console", on_console)
        page.on("pageerror", lambda e: self.console_errors.append(f"[{tag}] pageerror {e}"))
        page.on("dialog", lambda d: d.accept())


def debug(base: str) -> dict:
    with urllib.request.urlopen(f"{base}/__debug") as r:
        return json.loads(r.read())


def shot(page, name: str):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"))


def wait_until(cond, timeout: float, what: str, interval: float = 0.5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}")


def visible_text(page) -> str:
    return page.evaluate("""() => {
        const parts = [];
        const walk = (el) => {
            if (el.nodeType === 3) { parts.push(el.textContent); return; }
            if (el.nodeType !== 1) return;
            const cs = getComputedStyle(el);
            if (el.hidden || cs.display === 'none' || cs.visibility === 'hidden') return;
            if (['SCRIPT', 'STYLE', 'CODE'].includes(el.tagName)) return;
            for (const c of el.childNodes) walk(c);
        };
        walk(document.body);
        return parts.join(' ').replace(/\\s+/g, ' ');
    }""")


def no_horizontal_overflow(page) -> tuple[int, int]:
    return page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")


def unnamed_controls(page) -> list[str]:
    return page.evaluate("""() => {
        const bad = [];
        const els = document.querySelectorAll('button, input:not([type=hidden]), select, textarea, a[href], [role=button], summary');
        for (const el of els) {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            if (!r.width || !r.height || cs.visibility === 'hidden' || el.closest('[hidden]')) continue;
            let name = el.getAttribute('aria-label') || '';
            const lb = el.getAttribute('aria-labelledby');
            if (!name && lb) name = lb.split(/\\s+/).map((id) => (document.getElementById(id) || {}).textContent || '').join(' ');
            if (!name && el.labels && el.labels.length) name = Array.from(el.labels).map((l) => l.textContent).join(' ');
            if (!name) name = el.textContent || '';
            if (!name) name = el.getAttribute('title') || el.getAttribute('placeholder') || '';
            if (!name.trim()) bad.push(el.outerHTML.slice(0, 120));
        }
        return bad;
    }""")


def sign_in(page, base: str):
    page.goto(f"{base}/")
    page.wait_for_selector("#login-btn:not([disabled])", timeout=15000)
    page.click("#login-btn")
    page.wait_for_selector("#app:not([hidden])", timeout=20000)
    page.wait_for_function("() => document.querySelectorAll('#household-select option').length >= 2", timeout=15000)


# ------------------------------------------------------------------ scenarios

def s_a_signin(page, base):
    page.goto(f"{base}/")
    page.wait_for_selector("#login-btn:not([disabled])", timeout=15000)
    shot(page, "a-login")
    page.click("#login-btn")
    page.wait_for_selector("#app:not([hidden])", timeout=20000)
    assert "code=" not in page.url, "callback query string was not removed"
    page.click("#menu-btn")
    email = page.inner_text("#user-email")
    assert "judge@example.test" in email, f"email not shown: {email!r}"
    page.keyboard.press("Escape")
    opts = page.eval_on_selector_all("#household-select option", "els => els.map(e => e.textContent)")
    assert opts == ["My home", "Demo home"], f"homes: {opts}"
    shot(page, "a-signed-in")


def s_b_navigation(page, base):
    seen_hashes = []
    for view in ("ask", "camera", "things", "routines"):
        page.click(f'.tab[data-view="{view}"]')
        page.wait_for_function(f"() => location.hash === '#{view}'")
        visible = page.eval_on_selector_all(".view", "els => els.filter(e => !e.hidden).map(e => e.dataset.view)")
        assert visible == [view], f"{view}: visible views {visible}"
        cur = page.get_attribute(f'.tab[data-view="{view}"]', "aria-current")
        assert cur == "page", f"{view} tab not marked current"
        seen_hashes.append(view)
        sw, cw = no_horizontal_overflow(page)
        assert sw <= cw + 1, f"{view}: horizontal overflow {sw}>{cw}"
        shot(page, f"b-{view}")
    page.go_back()
    page.wait_for_function("() => location.hash === '#things'")
    vis = page.eval_on_selector_all(".view", "els => els.filter(e => !e.hidden).map(e => e.dataset.view)")
    assert vis == ["things"], f"back button: {vis}"
    page.click('.tab[data-view="ask"]')
    fit = page.evaluate("() => [document.scrollingElement.scrollHeight, innerHeight]")
    assert fit[0] <= fit[1] + 2, f"Ask view scrolls at 1280x800: {fit}"


def drag(page, canvas_box, zone_poly):
    xs = [p[0] for p in zone_poly]
    ys = [p[1] for p in zone_poly]
    sx = canvas_box["width"] / 640
    sy = canvas_box["height"] / 480
    x0, y0 = canvas_box["x"] + (min(xs) + 6) * sx, canvas_box["y"] + (min(ys) + 6) * sy
    x1, y1 = canvas_box["x"] + (max(xs) - 6) * sx, canvas_box["y"] + (max(ys) - 6) * sy
    page.mouse.move(x0, y0)
    page.mouse.down()
    page.mouse.move((x0 + x1) / 2, (y0 + y1) / 2, steps=5)
    page.mouse.move(x1, y1, steps=5)
    page.mouse.up()
    page.wait_for_selector("#name-panel:not([hidden])", timeout=5000)


def s_c_camera_wizard(page, base):
    page.click('.tab[data-view="camera"]')
    page.wait_for_selector("#cam-setup:not([hidden])", timeout=10000)
    shot(page, "c-setup-card")
    page.click("#cam-setup-btn")
    page.wait_for_selector("#wizard[open]", timeout=5000)
    shot(page, "c-wizard-1")
    page.click("#wiz-next")  # Turn on camera
    page.wait_for_selector("#wiz-video-placeholder[hidden]", state="attached", timeout=15000)
    page.wait_for_function("() => document.querySelector('#wiz-ready').textContent.includes('Ready')", timeout=60000)
    shot(page, "c-wizard-1-camera-on")
    page.click("#wiz-next")  # Next -> places
    page.wait_for_selector("#wiz-step-2:not([hidden])")
    page.wait_for_function("() => document.querySelector('#draw-canvas').width === 640")
    time.sleep(0.5)
    box = page.locator("#draw-canvas").bounding_box()
    names = {"desk": "Desk", "shelf": "Shelf", "door": "Door"}
    for z in ZONES:
        drag(page, box, z["polygon"])
        if z["name"] in names:
            page.click(f'#name-panel [data-place="{names[z["name"]]}"]')
        else:
            page.fill("#place-name", z["name"])
            page.click("#place-custom button[type=submit]")
        page.wait_for_selector("#name-panel", state="hidden")
    items = page.eval_on_selector_all("#places-list .place-name", "els => els.map(e => e.textContent.trim())")
    assert items == ["desk", "shelf", "side table", "door · exit door"], f"places: {items}"
    shot(page, "c-wizard-2-places")
    page.click("#wiz-next")  # Next -> done
    page.wait_for_selector("#wiz-step-3:not([hidden])")
    shot(page, "c-wizard-3")
    page.click("#wiz-next")  # Start watching
    page.wait_for_selector("#wizard", state="hidden", timeout=30000)
    page.wait_for_selector("#cam-live:not([hidden])", timeout=10000)
    page.wait_for_function("() => document.querySelector('#cam-status-text').textContent.includes('Watching')", timeout=60000)
    dbg = debug(base)
    assert dbg["devices_created"] == 1, f"devices created {dbg['devices_created']}"
    assert dbg["zones_put"] >= 1, "places were not saved to the server"
    saved = [z for d in dbg["zones"]["hh_mine"].values() if d for z in d]
    assert {z["name"] for z in saved} == {"desk", "shelf", "side table", "door"}, saved
    page.wait_for_selector("#event-toast:not([hidden])", timeout=90000)
    shot(page, "c-event-toast")
    wait_until(lambda: debug(base)["events_received"]["hh_mine"] >= 2, 90, "2 events at the server")
    shot(page, "c-camera-live")


def s_d_ask_here(page, base):
    page.click('.tab[data-view="ask"]')
    page.fill("#ask-input", "Where are my keys?")
    page.press("#ask-input", "Enter")
    page.wait_for_selector("#trace-list li", state="attached", timeout=10000)
    page.wait_for_selector("#answer-card:not([hidden])", timeout=30000)
    page.wait_for_selector("#answer-card .here-view .here-pic img[src^='data:image/jpeg']", timeout=20000)
    cap = page.inner_text("#answer-card .here-caption")
    assert "Live from this camera" in cap, f"caption {cap!r}"
    has_closeup = page.locator("#answer-card .here-closeup img").count() > 0
    steps = page.evaluate("() => document.querySelector('#trace-list').textContent")
    assert "Looked with the camera" in steps, steps
    assert page.locator("#read-aloud-btn").is_visible(), "read-aloud button hidden"
    shot(page, "d-answer-here")
    page.click("#trace summary")
    shot(page, "d-answer-trace")
    if not has_closeup:
        print("  note: no close-up (the camera reported the thing not present)", flush=True)
    page.click("#answer-card .here-show")
    page.wait_for_function("() => location.hash === '#camera'")
    time.sleep(0.8)
    shot(page, "d-camera-pulse")


def s_e_demo(page, base):
    page.select_option("#household-select", "demo")
    page.click('.tab[data-view="camera"]')
    page.wait_for_selector("#cam-demo:not([hidden])", timeout=10000)
    page.wait_for_selector("#demo-room img", timeout=10000)
    assert page.locator("#cam-setup").is_hidden() and page.locator("#cam-live").is_hidden()
    assert page.locator("#pair-btn").is_hidden(), "pairing offered in the demo home"
    # This computer keeps watching My home while you look around the Demo home.
    page.wait_for_selector("#tab-cam-dot:not([hidden])", timeout=10000)
    shot(page, "e-demo-camera")
    page.click('.tab[data-view="ask"]')
    page.fill("#ask-input", "Where is the leather wallet?")
    page.press("#ask-input", "Enter")
    page.wait_for_selector("#answer-card:not([hidden])", timeout=30000)
    page.wait_for_selector("#answer-card .here-pic canvas", timeout=20000)
    cap = page.inner_text("#answer-card .here-caption")
    assert "Camera picture" in cap, cap
    shot(page, "e-demo-answer")
    page.select_option("#household-select", "hh_mine")
    page.wait_for_function("() => !document.querySelector('#demo-badge') || document.querySelector('#demo-badge').hidden")


def s_f_things(page, base):
    page.click('.tab[data-view="things"]')
    page.wait_for_selector("#objects-grid .obj-card", timeout=15000)
    assert page.locator("#objects-grid .obj-card img.obj-photo").count() >= 1, "no photos"
    shot(page, "f-things")
    card = page.locator("#objects-grid .obj-card:not(.is-removed)").first
    card.locator(".obj-open").click()
    page.wait_for_selector("#object-dialog[open]")
    page.wait_for_selector("#dialog-body .timeline, #dialog-body .muted", timeout=10000)
    btn = page.locator('#dialog-body [data-action="where"]')
    assert btn.count() == 1, "no Show me where button"
    btn.click()
    page.wait_for_selector("#dialog-body .detail-where .here-pic img, #dialog-body .detail-where .here-pic canvas", timeout=20000)
    shot(page, "f-detail-where")
    page.click("#dialog-close")
    page.wait_for_selector("#object-dialog", state="hidden")
    card = page.locator("#objects-grid .obj-card").first
    oid = card.locator(".obj-open").get_attribute("data-id")
    card.locator('[data-action="rename"]').click()
    page.fill(f"#rn-{oid}", "Spare keys")
    page.click(f'form[data-id="{oid}"] button[type=submit]')
    wait_until(lambda: "Spare keys" in debug(base)["renames"], 10, "rename at the server")
    page.wait_for_selector(f'#objects-grid .obj-open[data-id="{oid}"] .obj-label:has-text("Spare keys")', timeout=10000)
    page.locator(f'#objects-grid [data-action="star"][data-id="{oid}"]').click()
    wait_until(lambda: any(e[0] == oid for e in debug(base)["essential"]), 10, "important toggle at the server")


def s_g_routines(page, base):
    page.click('.tab[data-view="routines"]')
    page.click("details.add-routine summary")
    page.fill("#routine-name", "Evening pills")
    first = page.eval_on_selector("#routine-object", "s => s.options[0] && s.options[0].value")
    assert first, "no things to pick"
    page.select_option("#routine-object", first)
    page.click("#routine-form button[type=submit]")
    page.wait_for_selector("#routines-list li:has-text('Evening pills')", timeout=10000)
    shot(page, "g-routines")
    page.click("#routines-list [data-action='check']")
    page.wait_for_selector("#job-dialog[open]")
    page.wait_for_selector("#jd-answer:not([hidden])", timeout=20000)
    shot(page, "g-routine-check")
    page.click("#job-dialog-close")
    page.wait_for_selector("#approvals-list [data-action='approve']", timeout=15000)
    page.click("#approvals-list [data-action='approve']")
    wait_until(lambda: any(a[1] == "approved" for a in debug(base)["approvals"]), 10, "approval at the server")
    page.click("#routines-list [data-action='check']")
    page.wait_for_selector("#jd-answer:not([hidden])", timeout=20000)
    page.click("#job-dialog-close")
    page.click('.tab[data-view="ask"]')
    page.wait_for_selector("#banners [data-action='deny']", timeout=15000)
    shot(page, "g-approval-banner")
    page.click("#banners [data-action='deny']")
    wait_until(lambda: any(a[1] == "denied" for a in debug(base)["approvals"]), 10, "deny at the server")


def s_h_pair_and_wording(page, base):
    page.click('.tab[data-view="camera"]')
    page.click("#pair-btn")
    page.wait_for_selector("#pair-ready:not([hidden])", timeout=10000)
    assert page.inner_text("#pair-code") == "K7P2-9QXA"
    cmd = page.inner_text("#pair-command")
    assert cmd.startswith("python -m icamfind pair K7P2-9QXA --api http://127.0.0.1:"), cmd
    assert page.locator("#pair-copy").is_visible()
    shot(page, "h-pair")
    texts = [visible_text(page)]
    page.click("#pair-close")
    for view in ("ask", "camera", "things", "routines"):
        page.click(f'.tab[data-view="{view}"]')
        time.sleep(0.3)
        texts.append(visible_text(page))
    bad = sorted({m.group(0) for t in texts for m in USER_WORDS.finditer(t)})
    assert not bad, f"technical words visible to users: {bad}"


def s_i_reload(page, base):
    page.select_option("#household-select", "hh_mine") if page.input_value("#household-select") != "hh_mine" else None
    before = debug(base)["devices_created"]
    page.reload()
    page.wait_for_selector("#app:not([hidden])", timeout=20000)
    page.click('.tab[data-view="camera"]')
    page.wait_for_selector("#cam-live:not([hidden])", timeout=10000)
    page.wait_for_function("() => /Watching|Learning/.test(document.querySelector('#cam-status-text').textContent)", timeout=60000)
    assert debug(base)["devices_created"] == before, "a duplicate camera was created on reload"
    zones = page.evaluate("() => JSON.parse(localStorage.getItem('icamfind.browserCam.hh_mine') || '{}').zones || []")
    assert len(zones) == 4, f"places not persisted: {zones}"
    shot(page, "i-after-reload")


def s_l_change_places_pause(page, base):
    page.click('.tab[data-view="camera"]')
    page.wait_for_selector("#cam-live:not([hidden])")
    page.click("#cam-places")
    page.wait_for_selector("#wizard[open]")
    page.wait_for_selector("#wiz-step-2:not([hidden])")
    before = debug(base)["zones_put"]
    page.click('#places-list [aria-label="Remove side table"]')
    page.click("#wiz-next")  # Save places
    page.wait_for_selector("#wizard", state="hidden", timeout=10000)
    wait_until(lambda: debug(base)["zones_put"] > before, 10, "changed places saved")
    zones = page.evaluate("() => JSON.parse(localStorage.getItem('icamfind.browserCam.hh_mine')).zones.map(z => z.name)")
    assert zones == ["desk", "shelf", "door"], zones
    page.click("#cam-pause")
    page.wait_for_selector("#cam-paused-cover:not([hidden])", timeout=5000)
    assert page.inner_text("#cam-pause") == "Resume"
    page.click("#cam-pause")
    page.wait_for_function("() => /Watching|Learning/.test(document.querySelector('#cam-status-text').textContent)", timeout=60000)
    assert page.inner_text("#cam-pause") == "Pause"


def s_m_token_refresh(page, base):
    before = debug(base)["refreshes"]
    EXPECTED["401"] = True
    urllib.request.urlopen(urllib.request.Request(f"{base}/__debug/expire", method="POST"))
    page.click('.tab[data-view="things"]')
    page.click('.seg[data-filter="present"]')
    page.click('.tab[data-view="ask"]')
    page.fill("#ask-input", "Where is my wallet?")
    page.press("#ask-input", "Enter")
    page.wait_for_selector("#answer-card:not([hidden])", timeout=30000)
    assert page.locator("#login").is_hidden(), "user was signed out instead of refreshing"
    assert debug(base)["refreshes"] > before, "no token refresh happened"
    time.sleep(1)
    EXPECTED["401"] = False


def s_n_camera_blocked(pw, base, suite, local_opencv):
    browser = pw.chromium.launch(channel="msedge", headless=True)
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    local_opencv(ctx)
    page = ctx.new_page()
    suite.watch(page, "blocked")
    sign_in(page, base)
    page.click('.tab[data-view="camera"]')
    if page.locator("#cam-setup-btn").is_visible():
        page.click("#cam-setup-btn")
        page.wait_for_selector("#wizard[open]")
        page.click("#wiz-next")
        page.wait_for_selector("#wiz-cam-error:not([hidden])", timeout=15000)
        msg = page.inner_text("#wiz-cam-error")
        assert re.search(r"blocked|No camera|couldn't start|busy", msg), msg
        assert page.inner_text("#wiz-next") == "Try again"
        shot(page, "n-camera-blocked")
    browser.close()


def s_k_accessibility(page, base):
    problems = []
    for view in ("ask", "camera", "things", "routines"):
        page.click(f'.tab[data-view="{view}"]')
        time.sleep(0.3)
        problems += [f"{view}: {b}" for b in unnamed_controls(page)]
    assert not problems, "controls without accessible names: " + " || ".join(problems[:6])
    page.click('.tab[data-view="ask"]')
    page.focus("#ask-input")
    page.keyboard.press("Tab")
    focus = page.evaluate("""() => { const a = document.activeElement; const cs = getComputedStyle(a);
        return [a.tagName + '#' + a.id, cs.outlineStyle, cs.outlineWidth, cs.boxShadow]; }""")
    assert focus[1] != "none" or focus[3] != "none", f"no visible focus on {focus}"
    size0 = page.evaluate("() => parseFloat(getComputedStyle(document.querySelector('#ask-title')).fontSize)")
    page.click("#menu-btn")
    page.click("#large-text-toggle")
    size1 = page.evaluate("() => parseFloat(getComputedStyle(document.querySelector('#ask-title')).fontSize)")
    assert size1 > size0, f"large text did nothing ({size0} -> {size1})"
    shot(page, "k-large-text")
    page.click("#menu-btn") if page.locator("#menu-panel").is_hidden() else None
    page.click("#large-text-toggle")
    page.keyboard.press("Escape")


def s_j_signout(page, base):
    page.click("#menu-btn")
    page.click("#logout-btn")
    page.wait_for_selector("#login:not([hidden])", timeout=15000)
    assert page.evaluate("() => localStorage.getItem('icamfind.auth')") is None
    shot(page, "j-signed-out")


def s_mobile(browser, base, suite, y4m_route):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True,
                              has_touch=True)
    y4m_route(ctx)
    page = ctx.new_page()
    suite.watch(page, "mobile")
    sign_in(page, base)
    for view in ("ask", "camera", "things", "routines"):
        page.click(f'.tab[data-view="{view}"]')
        time.sleep(0.4)
        sw, cw = no_horizontal_overflow(page)
        assert sw <= cw + 1, f"mobile {view}: horizontal overflow {sw}>{cw}"
        shot(page, f"m-{view}-mobile")
    page.click('.tab[data-view="camera"]')
    if page.locator("#cam-setup-btn").is_visible():
        page.click("#cam-setup-btn")
        page.wait_for_selector("#wizard[open]")
        sw, cw = no_horizontal_overflow(page)
        assert sw <= cw + 1, f"mobile wizard overflow {sw}>{cw}"
        shot(page, "m-wizard-mobile")
        page.click("#wiz-close")
    page.select_option("#household-select", "demo")
    page.click('.tab[data-view="ask"]')
    page.fill("#ask-input", "Where are my house keys?")
    page.press("#ask-input", "Enter")
    page.wait_for_selector("#answer-card .here-pic canvas", timeout=25000)
    sw, cw = no_horizontal_overflow(page)
    assert sw <= cw + 1, f"mobile answer overflow {sw}>{cw}"
    shot(page, "m-answer-mobile")
    ctx.close()


def s_cdn(browser, base, suite):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    suite.watch(page, "cdn")
    csp_violations = []
    page.on("console", lambda m: csp_violations.append(m.text) if "Content Security Policy" in m.text else None)
    sign_in(page, base)
    page.click('.tab[data-view="camera"]')
    if page.locator("#cam-live").is_visible():  # this browser profile is fresh, so it should be the setup card
        raise AssertionError("unexpected live camera in a fresh profile")
    page.click("#cam-setup-btn")
    page.wait_for_selector("#wizard[open]")
    page.click("#wiz-next")
    page.wait_for_function("() => document.querySelector('#wiz-ready').textContent.includes('Ready')", timeout=180000)
    loaded = page.evaluate("() => !!(self.cv && (self.cv.Mat || self.cv instanceof Promise))")
    assert loaded, "OpenCV not loaded from the CDN"
    assert not csp_violations, f"CSP violations: {csp_violations[:3]}"
    page.click("#wiz-close")
    ctx.close()


def s_csp_policy(*_):
    """The deployed Content-Security-Policy must let OpenCV.js run (embind needs 'unsafe-eval')."""
    csp = mock_server.real_api.SECURITY_HEADERS["content-security-policy"]
    assert "'unsafe-eval'" in csp, ("cloud/src/api.py CSP blocks OpenCV.js: add 'unsafe-eval' to script-src "
                                    "(the browser camera cannot start in production without it)")
    assert "https://cdn.jsdelivr.net" in csp and "'wasm-unsafe-eval'" in csp


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--y4m", default=str(DEFAULT_Y4M))
    p.add_argument("--opencv", default=str(DEFAULT_OPENCV))
    p.add_argument("--cdn", action="store_true")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--only", default="")
    args = p.parse_args()

    httpd = mock_server.serve(0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    print(f"mock backend at {base}", flush=True)
    suite = Suite()
    opencv_bytes = Path(args.opencv).read_bytes()

    def local_opencv(ctx):
        ctx.route(OPENCV_URL, lambda route: route.fulfill(
            status=200, body=opencv_bytes,
            headers={"content-type": "application/javascript", "access-control-allow-origin": "*"}))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=not args.headed, args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-video-capture={args.y4m}", "--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        local_opencv(ctx)
        page = ctx.new_page()
        suite.watch(page, "desktop")
        flow = [("a sign-in", s_a_signin), ("b navigation", s_b_navigation), ("c camera wizard", s_c_camera_wizard),
                ("d ask + here", s_d_ask_here), ("f things", s_f_things), ("g routines", s_g_routines),
                ("h pairing + wording", s_h_pair_and_wording), ("e demo home", s_e_demo),
                ("i reload + resume", s_i_reload), ("l change places + pause", s_l_change_places_pause),
                ("m token refresh", s_m_token_refresh), ("k accessibility", s_k_accessibility),
                ("j sign-out", s_j_signout)]
        suite.run("csp allows opencv (cloud/src/api.py)", s_csp_policy)
        for name, fn in flow:
            if args.only and not any(name.startswith(o) for o in args.only.split(",")):
                continue
            suite.run(name, fn, page, base)
        ctx.close()
        if not args.only or "mobile" in args.only:
            suite.run("mobile layout", s_mobile, browser, base, suite, local_opencv)
        if not args.only or "blocked" in args.only:
            suite.run("n camera blocked", s_n_camera_blocked, pw, base, suite, local_opencv)
        if args.cdn:
            suite.run("cdn opencv + csp/sri", s_cdn, browser, base, suite)
        browser.close()
    httpd.shutdown()

    print("\nSUMMARY")
    for name, ok, info in suite.results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}  {info if not ok else ''}")
    print(f"screenshots: {SHOTS}")
    return 0 if all(ok for _, ok, _ in suite.results) else 1


if __name__ == "__main__":
    sys.exit(main())
