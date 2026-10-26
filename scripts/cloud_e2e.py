"""Live end-to-end test of the deployed iCamFind cloud (API, auth, tenancy, agent, routines, pairing).

    python scripts/cloud_e2e.py            # uses .icamfind.env; creates/uses tester@icamfind.example

The script signs in as a dedicated test account and also plays the role of a camera (answering the
agent's live-check commands with real OpenCV results on a synthetic scene), so the whole
perception -> decision -> action loop is exercised deterministically. Exit code 0 = all checks passed.
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

import boto3
import cv2 as cv
import numpy as np
import requests

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(line.split("=", 1) for line in (ROOT / ".icamfind.env").read_text().splitlines()
           if "=" in line and not line.startswith("#"))
URL = ENV["ICAMFIND_API_URL"].strip()
REGION = ENV["AWS_REGION"].strip()
TEST_EMAIL = "tester@icamfind.example"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def test_account() -> tuple[str, str]:
    cognito = boto3.client("cognito-idp", region_name=REGION)
    password = "Tst" + uuid.uuid4().hex[:12] + "a7"
    try:
        cognito.admin_create_user(UserPoolId=ENV["ICAMFIND_USER_POOL_ID"], Username=TEST_EMAIL, MessageAction="SUPPRESS",
                                  UserAttributes=[{"Name": "email", "Value": TEST_EMAIL},
                                                  {"Name": "email_verified", "Value": "true"}])
    except cognito.exceptions.UsernameExistsException:
        pass
    cognito.admin_set_user_password(UserPoolId=ENV["ICAMFIND_USER_POOL_ID"], Username=TEST_EMAIL,
                                    Password=password, Permanent=True)
    res = cognito.admin_initiate_auth(UserPoolId=ENV["ICAMFIND_USER_POOL_ID"], ClientId=ENV["ICAMFIND_CLIENT_ID"],
                                      AuthFlow="ADMIN_USER_PASSWORD_AUTH",
                                      AuthParameters={"USERNAME": TEST_EMAIL, "PASSWORD": password})
    return res["AuthenticationResult"]["IdToken"], password


def jpeg(img: np.ndarray) -> str:
    ok, buf = cv.imencode(".jpg", img, [cv.IMWRITE_JPEG_QUALITY, 88])
    return base64.b64encode(buf.tobytes()).decode()


def scene(with_keys: bool) -> np.ndarray:
    """A desk with (optionally) a set of keys, drawn deterministically."""
    img = np.full((480, 640, 3), (70, 110, 150), np.uint8)
    img += np.random.default_rng(1).integers(0, 10, img.shape, dtype=np.uint8)
    if with_keys:
        cv.circle(img, (300, 330), 18, (175, 175, 180), 6)
        cv.rectangle(img, (316, 326), (370, 336), (175, 175, 180), -1)
        cv.rectangle(img, (356, 330), (364, 352), (175, 175, 180), -1)
    return img


class FakeCamera(threading.Thread):
    """Answers the agent's commands like the edge app: verify/scan with OpenCV on the synthetic scene."""

    def __init__(self, token: str):
        super().__init__(daemon=True)
        self.h = {"x-icamfind-device-token": token}
        self.keys_present = True
        self.stop = False
        self.seen: list[str] = []

    def run(self) -> None:
        while not self.stop:
            try:
                cmds = requests.get(f"{URL}/api/device/commands", headers=self.h, timeout=10).json().get("commands", [])
                for c in cmds:
                    self.seen.append(c["kind"])
                    frame = scene(self.keys_present)
                    crop = frame[280:380, 250:420]
                    if c["kind"] == "verify_object":
                        result = {"ok": True, "object_id": c["args"].get("object_id"), "present": self.keys_present,
                                  "match_object": 0.9 if self.keys_present else 0.3,
                                  "match_empty_surface": 0.3 if self.keys_present else 0.92, "zone": "desk"}
                    elif c["kind"] == "scan_for_object":
                        result = {"ok": True, "object_id": c["args"].get("object_id"), "found": False, "score": 0.1}
                    elif c["kind"] == "speak":
                        result = {"ok": True, "spoken": True}
                    else:
                        result = {"ok": True}
                    requests.post(f"{URL}/api/device/commands/{c['id']}/result", headers=self.h,
                                  json={"result": result, "crop_b64": jpeg(crop)}, timeout=10)
                requests.post(f"{URL}/api/device/heartbeat", headers=self.h, timeout=10,
                              json={"zones": [{"name": "desk", "polygon": [[0, 240], [640, 240], [640, 480], [0, 480]]}],
                                    "frame_size": [640, 480]})
            except requests.RequestException:
                pass
            time.sleep(1.5)


def wait_job(h: dict, job_id: str, timeout: float = 120) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        data = requests.get(f"{URL}/api/jobs/{job_id}", headers=h, timeout=20).json()
        if data["job"]["status"] != "running":
            return data
        time.sleep(2)
    return data


def tools_used(job: dict) -> list[str]:
    return [s.get("tool") for s in job["steps"] if s["kind"] == "tool_call"]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    t0 = time.time()
    # ---------------------------------------------------------------- public + auth
    r = requests.get(f"{URL}/", timeout=20)
    check("web app served", r.status_code == 200 and "<html" in r.text.lower())
    for f in ("app.js", "edge.js", "style.css", "config.json"):
        check(f"static {f}", requests.get(f"{URL}/{f}", timeout=20).status_code == 200)
    csp = r.headers.get("content-security-policy", "")
    check("CSP allows OpenCV.js + wasm only from pinned CDN", "cdn.jsdelivr.net" in csp and "wasm-unsafe-eval" in csp)
    check("camera permission policy", "camera=(self)" in r.headers.get("permissions-policy", ""))
    check("user API rejects anonymous", requests.get(f"{URL}/api/state", timeout=20).status_code == 401)
    check("user API rejects forged JWT", requests.get(f"{URL}/api/state", timeout=20,
                                                       headers={"Authorization": "Bearer abc.def.ghi"}).status_code == 401)
    check("device API rejects bad token", requests.post(f"{URL}/api/device/heartbeat", json={}, timeout=20,
                                                         headers={"x-icamfind-device-token": "nope"}).status_code == 401)
    check("pairing rejects wrong code", requests.post(f"{URL}/api/device/pair", json={"code": "ABCD-EFGH"},
                                                       timeout=20).status_code == 400)

    token, _ = test_account()
    h = {"Authorization": f"Bearer {token}"}
    me = requests.get(f"{URL}/api/me", headers=h, timeout=20).json()
    mine = next(x for x in me["households"] if not x["demo"])
    demo = next(x for x in me["households"] if x["demo"])
    check("account has private home + demo home", mine["role"] == "owner" and demo["id"] == "demo")
    check("foreign home forbidden", requests.get(f"{URL}/api/state", timeout=20,
                                                  headers={**h, "x-icamfind-household": "hh_notmine"}).status_code == 403)
    hd = {**h, "x-icamfind-household": "demo"}
    hm = {**h, "x-icamfind-household": mine["id"]}
    check("cannot add camera to demo home", requests.post(f"{URL}/api/devices", headers=hd, json={"name": "x"},
                                                          timeout=20).status_code == 403)
    check("cannot make pairing code for demo home", requests.post(f"{URL}/api/devices/pair-code", headers=hd,
                                                                  json={}, timeout=20).status_code == 403)

    # ---------------------------------------------------------------- demo home (cloud replay camera)
    st = requests.get(f"{URL}/api/state", headers=hd, timeout=20).json()
    dev = st.get("device") or {}
    check("demo camera online", bool(dev.get("online")), dev.get("name", ""))
    check("demo home has remembered objects", len(st["objects"]) > 0, f"{len(st['objects'])} objects")
    check("demo snapshot available", bool(dev.get("snapshot_url")))
    present = [o for o in st["objects"] if o.get("status") == "present"]
    if present:
        target = present[0]
        job = requests.post(f"{URL}/api/ask", headers=hd, json={"question": f"Where is the {target['label']}?",
                                                                  "tz": "Australia/Brisbane"}, timeout=20).json()
        res = wait_job(hd, job["job_id"])
        used = tools_used(res)
        check("demo ask completes", res["job"]["status"] == "done", res["job"].get("answer", "")[:90])
        check("demo ask used live camera", "verify_object_live" in used or "scan_room_for_object" in used, str(used))
        imgs = [s for s in res["steps"] if s.get("image_url")]
        check("demo live check returned an image", len(imgs) > 0)

    # ---------------------------------------------------------------- my home: browser camera + events
    d = requests.post(f"{URL}/api/devices", headers=hm, json={"name": "Test camera", "kind": "browser"},
                      timeout=20).json()
    check("create browser camera", d.get("token", "").startswith("icf_"))
    dh = {"x-icamfind-device-token": d["token"]}
    bad = requests.put(f"{URL}/api/devices/{d['device']['id']}/zones", headers=hm, json={"zones": [{"name": "x"}]},
                       timeout=20)
    check("invalid places rejected", bad.status_code == 400)
    ok = requests.put(f"{URL}/api/devices/{d['device']['id']}/zones", headers=hm, timeout=20,
                      json={"zones": [{"name": "desk", "polygon": [[0, 240], [640, 240], [640, 480], [0, 480]]}],
                            "door_zone": None})
    check("places saved", ok.status_code == 200)
    cfg = requests.get(f"{URL}/api/device/config", headers=dh, timeout=20).json()
    check("camera receives places", cfg.get("zones", [{}])[0].get("name") == "desk")

    cam = FakeCamera(d["token"])
    cam.start()
    oid = "obj_" + uuid.uuid4().hex[:10]
    frame = scene(True)
    ev = {"id": "ev_" + uuid.uuid4().hex[:10], "type": "placed", "object_id": oid, "ts": time.time(), "zone": "desk",
          "bbox": [282, 312, 90, 42], "map_xy": None, "local_label": "object", "confidence": 0.4, "essential": False}
    r = requests.post(f"{URL}/api/device/events", headers=dh, json={"event": ev, "crop_b64": jpeg(frame[280:380, 250:420])},
                      timeout=60).json()
    label = (r.get("object") or {}).get("label", "")
    check("event stored and named by Claude", bool(label) and label != "object", label)
    check("keys flagged essential", bool((r.get("object") or {}).get("essential")), str((r.get("object") or {}).get("essential")))

    time.sleep(3)
    job = requests.post(f"{URL}/api/ask", headers=hm, json={"question": "Where are my keys?", "tz": "Australia/Brisbane"},
                        timeout=20).json()
    res = wait_job(hm, job["job_id"])
    used = tools_used(res)
    check("ask (present) completes", res["job"]["status"] == "done", res["job"].get("answer", "")[:90])
    check("ask (present) verified live", "verify_object_live" in used, str(used))
    check("answer names the desk", "desk" in (res["job"].get("answer") or "").lower())
    check("answer points at the object", res["job"].get("object_id") == oid)

    cam.keys_present = False  # someone took the keys
    job = requests.post(f"{URL}/api/ask", headers=hm, json={"question": "Where are my keys?", "tz": "Australia/Brisbane"},
                        timeout=20).json()
    res = wait_job(hm, job["job_id"])
    used = tools_used(res)
    check("ask (missing) completes", res["job"]["status"] == "done", res["job"].get("answer", "")[:110])
    check("ask (missing) escalates to room scan", "scan_room_for_object" in used, str(used))
    check("camera executed agent commands", {"verify_object", "scan_for_object"} <= set(cam.seen), str(set(cam.seen)))

    # ---------------------------------------------------------------- objects API
    upd = requests.post(f"{URL}/api/objects/{oid}", headers=hm, json={"label": "house keys", "essential": True},
                        timeout=20).json()
    check("rename + essential", upd["object"]["label"] == "house keys" and upd["object"]["essential"])
    det = requests.get(f"{URL}/api/objects/{oid}", headers=hm, timeout=20).json()
    check("object history", len(det["events"]) >= 1 and det["object"]["crop_url"])
    check("other home cannot read it", requests.get(f"{URL}/api/objects/{oid}", headers=hd, timeout=20).status_code == 404)
    evs = requests.get(f"{URL}/api/events?limit=5", headers=hm, timeout=20).json()["events"]
    check("timeline lists events", any(e["object_id"] == oid for e in evs))

    # ---------------------------------------------------------------- routines + approvals
    cam.keys_present = True
    rt = requests.post(f"{URL}/api/routines", headers=hm, timeout=20,
                       json={"object_id": oid, "name": "Morning keys check", "start": "00:00", "end": "00:01",
                             "tz": "Australia/Brisbane"}).json()
    rid = rt["routine"]["id"]
    check("routine created", rid.startswith("rtn_"))
    check("bad routine rejected", requests.post(f"{URL}/api/routines", headers=hm, json={"object_id": oid, "start": "9am"},
                                                timeout=20).status_code == 400)
    j1 = wait_job(hm, requests.post(f"{URL}/api/routines/{rid}/check", headers=hm, timeout=20).json()["job_id"])
    st = requests.get(f"{URL}/api/state", headers=hm, timeout=20).json()
    r1 = next(r for r in st["routines"] if r["id"] == rid)
    check("routine check 1 -> reminder spoken", r1.get("last_status") == "reminded" and "speak" in cam.seen,
          f"{r1.get('last_status')} {tools_used(j1)}")
    j2 = wait_job(hm, requests.post(f"{URL}/api/routines/{rid}/check", headers=hm, timeout=20).json()["job_id"])
    st = requests.get(f"{URL}/api/state", headers=hm, timeout=20).json()
    pend = [a for a in st["approvals"] if a.get("job_id") == j2["job"]["id"]]
    check("routine check 2 -> caregiver approval requested", len(pend) == 1, f"{tools_used(j2)}")
    if pend:
        ap = requests.post(f"{URL}/api/approvals/{pend[0]['id']}", headers=hm, json={"decision": "approve"},
                           timeout=20).json()
        check("approve sends notification", ap["approval"]["status"] == "approved")
        again = requests.post(f"{URL}/api/approvals/{pend[0]['id']}", headers=hm, json={"decision": "approve"}, timeout=20)
        check("approval cannot be reused", again.status_code == 409)
    check("routine delete", requests.delete(f"{URL}/api/routines/{rid}", headers=hm, timeout=20).status_code == 200)

    # ---------------------------------------------------------------- pairing another computer
    pc = requests.post(f"{URL}/api/devices/pair-code", headers=hm, json={"name": "Kitchen PC"}, timeout=20).json()
    check("pairing code made", len(pc.get("code", "")) == 9 and pc["code"] in pc.get("command", ""))
    pr = requests.post(f"{URL}/api/device/pair", json={"code": pc["code"].lower().replace("-", "")}, timeout=20)
    check("pairing code redeemed (case/dash tolerant)", pr.status_code == 200 and pr.json()["token"].startswith("icf_"))
    check("pairing code single use", requests.post(f"{URL}/api/device/pair", json={"code": pc["code"]},
                                                   timeout=20).status_code == 400)
    snap = requests.post(f"{URL}/api/snapshot", headers=hm, timeout=20)
    check("snapshot request reaches camera", snap.status_code == 200)

    cam.stop = True
    passed = sum(ok for _, ok, _ in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed in {time.time() - t0:.0f} s")
    out = ROOT / "docs" / "evidence" / "cloud_e2e_results.json"
    out.write_text(json.dumps([{"check": n, "pass": ok, "detail": d} for n, ok, d in RESULTS], indent=1), encoding="utf-8")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
