"""End-to-end checks against the deployed API (uses .icamfind.env).

    python scripts/e2e.py ask "Where are my keys?"
    python scripts/e2e.py routine <routine_id>
    python scripts/e2e.py state

Prints the agent trace and saves it under docs/evidence/ as reproducible evidence.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
env = dict(line.split("=", 1) for line in (ROOT / ".icamfind.env").read_text().splitlines()
           if "=" in line and not line.startswith("#"))
URL, TOKEN = env["ICAMFIND_API_URL"].strip(), env["ICAMFIND_TOKEN"].strip()
H = {"x-icamfind-token": TOKEN}


def wait_job(job_id: str, label: str) -> dict:
    for _ in range(80):
        time.sleep(2)
        data = requests.get(f"{URL}/api/jobs/{job_id}", headers=H, timeout=20).json()
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
    out = ROOT / "docs" / "evidence" / f"trace_{label}_{job_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    for s in data["steps"]:
        s.pop("image_url", None)  # presigned URLs expire; keep the trace itself
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return data


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "state"
    if cmd == "ask":
        job = requests.post(f"{URL}/api/ask", headers=H, json={"question": sys.argv[2], "tz": "Australia/Brisbane"},
                            timeout=20).json()
        wait_job(job["job_id"], "ask")
    elif cmd == "routine":
        job = requests.post(f"{URL}/api/routines/{sys.argv[2]}/check", headers=H, timeout=20).json()
        wait_job(job["job_id"], "routine")
    else:
        state = requests.get(f"{URL}/api/state", headers=H, timeout=20).json()
        dev = state["device"] or {}
        print("device", dev.get("id"), "online" if dev.get("online") else "offline")
        for o in state["objects"]:
            print(f"  {o['id']} {o.get('label')!r:<22} {o.get('status'):<8} {o.get('zone')}")
        print("approvals", [(a["id"], a["message"][:60]) for a in state["approvals"]])
        print("routines", [(r["id"], r["name"], r.get("last_status")) for r in state["routines"]])


if __name__ == "__main__":
    main()
