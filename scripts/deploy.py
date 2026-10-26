"""Build and deploy the iCamFind backend with the AWS CLI (no SAM CLI or Docker needed).

    python scripts/deploy.py [--region ap-southeast-2] [--stack icamfind] [--caregiver-email you@example.com]

Steps: bundle cloud/src + web + linux/arm64 wheels into build/lambda, upload with
`aws cloudformation package`, deploy the SAM template, then write .icamfind.env with the API URL
and access token for the edge device.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "lambda"
ENV_FILE = ROOT / ".icamfind.env"


def aws(*args: str, capture: bool = True) -> str:
    cmd = ["aws", *args]
    print("+", " ".join(a.split("=")[0] + "=****" if "DemoDeviceToken=" in a else a for a in cmd))
    res = subprocess.run(cmd, text=True, capture_output=capture, shell=sys.platform == "win32")
    if res.returncode != 0:
        # Re-raise without the argument list: it can contain the demo device token.
        raise subprocess.CalledProcessError(res.returncode, f"aws {args[0]} {args[1] if len(args) > 1 else ''}",
                                            output=res.stdout, stderr=res.stderr)
    return res.stdout.strip() if capture else ""


def read_env() -> dict:
    if not ENV_FILE.exists():
        return {}
    out = {}
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def build() -> None:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)
    for f in (ROOT / "cloud" / "src").glob("*.py"):
        shutil.copy2(f, BUILD / f.name)
    shutil.copytree(ROOT / "cloud" / "src" / "web", BUILD / "web")
    subprocess.run([
        sys.executable, "-m", "pip", "install", "-q", "-r", str(ROOT / "cloud" / "requirements.txt"),
        "--target", str(BUILD), "--platform", "manylinux2014_aarch64", "--implementation", "cp",
        "--python-version", "3.12", "--only-binary=:all:", "--upgrade",
    ], check=True)
    for junk in BUILD.rglob("__pycache__"):
        shutil.rmtree(junk, ignore_errors=True)


DEMO_ZONES = [
    {"name": "desk", "polygon": [[20, 250], [400, 250], [400, 470], [20, 470]]},
    {"name": "shelf", "polygon": [[430, 60], [630, 60], [630, 200], [430, 200]]},
    {"name": "side table", "polygon": [[430, 280], [630, 280], [630, 470], [430, 470]]},
    {"name": "door", "polygon": [[0, 0], [150, 0], [150, 230], [0, 230]]},
]


def build_demo_bundle(clip: Path, zones: list | None) -> Path:
    """Zip the edge package + a replay config + the demo clip for the EC2 demo camera."""
    if not clip.exists():
        sys.path.insert(0, str(ROOT))
        from icamfind.eval.synth import make_clip

        make_clip(clip.parent, int(clip.stem.split("_")[-1]) if clip.stem.split("_")[-1].isdigit() else 1)
    out = ROOT / "build" / "demo_bundle.zip"
    config = {"source": clip.name, "device_id": "dev_demo_replay", "data_dir": "data", "speak": False,
              "use_dnn": False, "replay_pause_seconds": 600, "zones": zones or DEMO_ZONES, "door_zone": "door"}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted((ROOT / "icamfind").rglob("*.py")):
            z.write(f, f.relative_to(ROOT).as_posix())
        z.writestr("config.demo.json", json.dumps(config, indent=1))
        z.write(clip, clip.name)
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--region", default=None)
    p.add_argument("--stack", default="icamfind")
    p.add_argument("--caregiver-email", default="")
    p.add_argument("--model", default="au.anthropic.claude-haiku-4-5-20251001-v1:0")
    p.add_argument("--fallback-model", default="au.anthropic.claude-sonnet-4-6")
    p.add_argument("--demo-clip", default="bench/synthetic_1.mp4",
                   help="recorded clip the cloud demo camera replays (record one with `python -m icamfind record`)")
    p.add_argument("--demo-zones", default=None, help="JSON file with zones for the demo clip (default: benchmark zones)")
    p.add_argument("--no-demo-camera", action="store_true", help="skip the EC2 demo replay camera")
    p.add_argument("--skip-build", action="store_true")
    args = p.parse_args()

    region = args.region or aws("configure", "get", "region") or "ap-southeast-2"
    account = json.loads(aws("sts", "get-caller-identity", "--output", "json"))["Account"]
    env = read_env()
    demo_token = env.get("ICAMFIND_DEMO_DEVICE_TOKEN") or "icf_" + secrets.token_urlsafe(32)

    if not args.skip_build:
        build()

    bucket = f"icamfind-artifacts-{account}-{region}"
    try:
        aws("s3api", "head-bucket", "--bucket", bucket, "--region", region)
    except subprocess.CalledProcessError:
        extra = [] if region == "us-east-1" else ["--create-bucket-configuration", f"LocationConstraint={region}"]
        aws("s3api", "create-bucket", "--bucket", bucket, "--region", region, *extra)
        aws("s3api", "put-public-access-block", "--bucket", bucket, "--region", region,
            "--public-access-block-configuration",
            "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true")

    bundle_changed = False
    if not args.no_demo_camera:
        zones = json.loads(Path(args.demo_zones).read_text()) if args.demo_zones else None
        bundle = build_demo_bundle(ROOT / args.demo_clip, zones)
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        bundle_changed = digest != env.get("ICAMFIND_DEMO_BUNDLE_SHA256")
        if bundle_changed:
            aws("s3", "cp", str(bundle), f"s3://{bucket}/demo/bundle.zip", "--region", region, "--only-show-errors")
        env["ICAMFIND_DEMO_BUNDLE_SHA256"] = digest

    packaged = ROOT / "build" / "packaged.yaml"
    aws("cloudformation", "package", "--template-file", str(ROOT / "cloud" / "template.yaml"),
        "--s3-bucket", bucket, "--s3-prefix", args.stack, "--output-template-file", str(packaged),
        "--region", region, capture=False)
    overrides = [f"DemoDeviceToken={demo_token}", f"DemoDeviceTokenSha256={hashlib.sha256(demo_token.encode()).hexdigest()}",
                 f"ArtifactBucket={bucket}", f"ModelId={args.model}", f"FallbackModelId={args.fallback_model}",
                 f"EnableDemoCamera={'false' if args.no_demo_camera else 'true'}"]
    if not args.no_demo_camera:
        vpc = aws("ec2", "describe-vpcs", "--region", region, "--filters", "Name=isDefault,Values=true",
                  "--query", "Vpcs[0].VpcId", "--output", "text")
        overrides.append(f"DemoVpcId={vpc}")
    if args.caregiver_email:
        overrides.append(f"CaregiverEmail={args.caregiver_email}")
    aws("cloudformation", "deploy", "--template-file", str(packaged), "--stack-name", args.stack,
        "--capabilities", "CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND", "--region", region,
        "--no-fail-on-empty-changeset", "--parameter-overrides", *overrides, capture=False)

    outputs = {o["OutputKey"]: o["OutputValue"] for o in json.loads(aws(
        "cloudformation", "describe-stacks", "--stack-name", args.stack, "--region", region,
        "--query", "Stacks[0].Outputs", "--output", "json"))}
    api_url = outputs["ApiUrl"]
    instance = outputs.get("DemoCameraInstanceId")
    if instance and bundle_changed and env.get("ICAMFIND_DEMO_INSTANCE") == instance:
        # Restart the camera service so it downloads the new bundle (no-op while first boot is still installing).
        aws("ssm", "send-command", "--region", region, "--instance-ids", instance, "--document-name",
            "AWS-RunShellScript", "--parameters",
            'commands=["systemctl list-unit-files icamfind.service >/dev/null && systemctl restart icamfind || true"]')

    env.update({"ICAMFIND_API_URL": api_url, "AWS_REGION": region, "ICAMFIND_DEMO_DEVICE_TOKEN": demo_token,
                "ICAMFIND_USER_POOL_ID": outputs["UserPoolId"], "ICAMFIND_CLIENT_ID": outputs["UserPoolClientId"],
                "ICAMFIND_DEMO_INSTANCE": instance or ""})
    env.pop("ICAMFIND_TOKEN", None)
    ENV_FILE.write_text("# written by scripts/deploy.py - keep private\n" +
                        "".join(f"{k}={v}\n" for k, v in env.items()))
    print(f"\nDeployed. Web app: {api_url}/  (sign up there, or create a judge account with scripts/create_user.py)")
    print("Connect a camera: in the web app open Cameras > Add a camera, then run the command it shows.")


if __name__ == "__main__":
    main()
