"""Build and deploy the iCamFind backend with the AWS CLI (no SAM CLI or Docker needed).

    python scripts/deploy.py [--region ap-southeast-2] [--stack icamfind] [--caregiver-email you@example.com]

Steps: bundle cloud/src + web + linux/arm64 wheels into build/lambda, upload with
`aws cloudformation package`, deploy the SAM template, then write .icamfind.env with the API URL
and access token for the edge device.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "lambda"
ENV_FILE = ROOT / ".icamfind.env"


def aws(*args: str, capture: bool = True) -> str:
    cmd = ["aws", *args]
    print("+", " ".join(a if "Token=" not in a else "AccessToken=****" for a in cmd))
    res = subprocess.run(cmd, check=True, text=True, capture_output=capture, shell=sys.platform == "win32")
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


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--region", default=None)
    p.add_argument("--stack", default="icamfind")
    p.add_argument("--caregiver-email", default="")
    p.add_argument("--model", default="au.anthropic.claude-haiku-4-5-20251001-v1:0")
    p.add_argument("--fallback-model", default="au.anthropic.claude-sonnet-4-6")
    p.add_argument("--skip-build", action="store_true")
    args = p.parse_args()

    region = args.region or aws("configure", "get", "region") or "ap-southeast-2"
    account = json.loads(aws("sts", "get-caller-identity", "--output", "json"))["Account"]
    env = read_env()
    token = env.get("ICAMFIND_TOKEN") or secrets.token_urlsafe(32)

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

    packaged = ROOT / "build" / "packaged.yaml"
    aws("cloudformation", "package", "--template-file", str(ROOT / "cloud" / "template.yaml"),
        "--s3-bucket", bucket, "--s3-prefix", args.stack, "--output-template-file", str(packaged),
        "--region", region, capture=False)
    overrides = [f"AccessToken={token}", f"ModelId={args.model}", f"FallbackModelId={args.fallback_model}"]
    if args.caregiver_email:
        overrides.append(f"CaregiverEmail={args.caregiver_email}")
    aws("cloudformation", "deploy", "--template-file", str(packaged), "--stack-name", args.stack,
        "--capabilities", "CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND", "--region", region,
        "--no-fail-on-empty-changeset", "--parameter-overrides", *overrides, capture=False)

    outputs = json.loads(aws("cloudformation", "describe-stacks", "--stack-name", args.stack, "--region", region,
                             "--query", "Stacks[0].Outputs", "--output", "json"))
    api_url = next(o["OutputValue"] for o in outputs if o["OutputKey"] == "ApiUrl")
    ENV_FILE.write_text(f"# written by scripts/deploy.py - keep private\nICAMFIND_API_URL={api_url}\n"
                        f"ICAMFIND_TOKEN={token}\nAWS_REGION={region}\n")
    print(f"\nDeployed. Web app: {api_url}/   (sign in with the token in {ENV_FILE.name})")
    print("Edge device picks up .icamfind.env automatically: python -m icamfind run")


if __name__ == "__main__":
    main()
