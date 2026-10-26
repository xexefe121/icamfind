"""Reset the shared Demo home: stop the replay camera, erase the demo memory (cloud + camera), restart.

    python scripts/reset_demo.py --yes

The camera then replays its clip once from the start (about 2.5 minutes) and holds the final scene.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]


def run_on_camera(ssm, instance: str, commands: list[str]) -> None:
    cid = ssm.send_command(InstanceIds=[instance], DocumentName="AWS-RunShellScript",
                           Parameters={"commands": commands})["Command"]["CommandId"]
    for _ in range(60):
        time.sleep(2)
        try:
            status = ssm.get_command_invocation(CommandId=cid, InstanceId=instance)["Status"]
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if status not in ("Pending", "InProgress", "Delayed"):
            if status != "Success":
                sys.exit(f"camera command failed: {status}")
            return
    sys.exit("camera command timed out")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--yes", action="store_true")
    args = p.parse_args()
    if not args.yes:
        sys.exit("refusing to reset without --yes")
    env = dict(line.split("=", 1) for line in (ROOT / ".icamfind.env").read_text().splitlines()
               if "=" in line and not line.startswith("#"))
    region, instance = env["AWS_REGION"], env["ICAMFIND_DEMO_INSTANCE"]
    ssm = boto3.client("ssm", region_name=region)
    run_on_camera(ssm, instance, ["systemctl stop icamfind"])
    subprocess.run([sys.executable, str(ROOT / "scripts" / "reset_memory.py"), "--yes", "--household", "demo",
                    "--region", region], check=True)
    run_on_camera(ssm, instance, ["rm -rf /opt/icamfind/data", "systemctl start icamfind"])
    print("demo home reset; the camera is replaying its clip (about 2.5 minutes)")


if __name__ == "__main__":
    main()
