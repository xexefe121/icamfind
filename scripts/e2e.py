"""End-to-end checks against the deployed API as a signed-in user (uses .icamfind.env).

    python scripts/e2e.py [--home demo|mine] ask "Where are my keys?"
    python scripts/e2e.py [--home demo|mine] routine <routine_id>
    python scripts/e2e.py [--home demo|mine] state

Signs in with the test account saved by `scripts/create_user.py --save`, prints the agent trace and
saves it under docs/evidence/ as reproducible evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import boto3
import requests

ROOT = Path(__file__).resolve().parents[1]
env = dict(line.split("=", 1) for line in (ROOT / ".icamfind.env").read_text().splitlines()
           if "=" in line and not line.startswith("#"))
URL = env["ICAMFIND_API_URL"].strip()


def sign_in() -> str:
    cognito = boto3.client("cognito-idp", region_name=env["AWS_REGION"])
    res = cognito.admin_initiate_auth(
        UserPoolId=env["ICAMFIND_USER_POOL_ID"], ClientId=env["ICAMFIND_CLIENT_ID"],
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": env["ICAMFIND_TEST_EMAIL"], "PASSWORD": env["ICAMFIND_TEST_PASSWORD"]})
    return res["AuthenticationResult"]["IdToken"]


def wait_job(h: dict, job_id: str, label: str) -> dict:
    for _ in range(80):
        time.sleep(2)
        data = requests.get(f"{URL}/api/jobs/{job_id}", headers=h, timeout=20).json()
        if data["job"]["status"] != "running":
            break
    job = data["job"]
    print(f"[{label}] {job['status']} in {round(job.get('finished', time.time()) - job['created'], 1)} s "
          f"({job.get('model')}): {job.get('answer')}")
    for s in data["steps"]:
        if s["kind"] in ("tool_call", "tool_result", "error"):
            payload = s.get("input") if s["kind"] == "tool_call" else (s.get("output") or s.get("text"))
            print(f"  {s['n']:>2} {s['kind']:<11} {s.get('tool', ''):<30} {json.dumps(payload, ensure_ascii=False)[:150]}"
                  + ("  [image]" if s.get("image_url") else ""))
    for s in data["steps"]:
        s.pop("image_url", None)  # presigned URLs expire; keep the trace itself
    out = ROOT / "docs" / "evidence" / f"trace_{label}_{job_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return data


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--home", default="demo", help="'demo' or 'mine'")
    p.add_argument("cmd", nargs="?", default="state")
    p.add_argument("arg", nargs="?")
    args = p.parse_args()
    h = {"Authorization": f"Bearer {sign_in()}"}
    me = requests.get(f"{URL}/api/me", headers=h, timeout=20).json()
    home = next(x for x in me["households"] if x["demo"] == (args.home == "demo"))
    h["x-icamfind-household"] = home["id"]
    print(f"signed in as {me['user']['email']}; home: {home['name']} ({home['id']})")
    if args.cmd == "ask":
        job = requests.post(f"{URL}/api/ask", headers=h, json={"question": args.arg, "tz": "Australia/Brisbane"},
                            timeout=20).json()
        wait_job(h, job["job_id"], "ask")
    elif args.cmd == "routine":
        job = requests.post(f"{URL}/api/routines/{args.arg}/check", headers=h, timeout=20).json()
        wait_job(h, job["job_id"], "routine")
    else:
        state = requests.get(f"{URL}/api/state", headers=h, timeout=20).json()
        dev = state["device"] or {}
        print("camera", dev.get("name"), "online" if dev.get("online") else "offline")
        for o in state["objects"]:
            print(f"  {o['id']} {o.get('label')!r:<22} {o.get('status'):<8} {o.get('zone')}")
        print("approvals", [(a["id"], a["message"][:60]) for a in state["approvals"]])
        print("routines", [(r["id"], r["name"], r.get("last_status")) for r in state["routines"]])


if __name__ == "__main__":
    main()
