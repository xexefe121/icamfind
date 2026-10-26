"""Create a ready-to-use iCamFind account (e.g. for judges) in the deployed Cognito user pool.

    python scripts/create_user.py --email judges@icamfind.example [--password ...] [--save]

The account is created verified with a permanent password, so no email is sent. On first sign-in it
gets its own private home and joins the shared Demo home. --save stores the credentials in
.icamfind.env (git-ignored) for scripts/e2e.py.
"""

from __future__ import annotations

import argparse
import secrets
import string
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".icamfind.env"


def read_env() -> dict:
    out = {}
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def make_password() -> str:
    alphabet = string.ascii_letters + string.digits
    core = "".join(secrets.choice(alphabet) for _ in range(14))
    return core + "a7"  # guarantees a lowercase letter and a digit


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--email", required=True)
    p.add_argument("--password", default=None)
    p.add_argument("--save", action="store_true")
    args = p.parse_args()
    env = read_env()
    cognito = boto3.client("cognito-idp", region_name=env["AWS_REGION"])
    pool = env["ICAMFIND_USER_POOL_ID"]
    password = args.password or make_password()
    try:
        cognito.admin_create_user(UserPoolId=pool, Username=args.email, MessageAction="SUPPRESS",
                                  UserAttributes=[{"Name": "email", "Value": args.email},
                                                  {"Name": "email_verified", "Value": "true"}])
    except cognito.exceptions.UsernameExistsException:
        print("user exists; resetting the password")
    cognito.admin_set_user_password(UserPoolId=pool, Username=args.email, Password=password, Permanent=True)
    if args.save:
        env.update({"ICAMFIND_TEST_EMAIL": args.email, "ICAMFIND_TEST_PASSWORD": password})
        ENV_FILE.write_text("# written by scripts/deploy.py - keep private\n" +
                            "".join(f"{k}={v}\n" for k, v in env.items()))
        print(f"account ready: {args.email} (password saved to {ENV_FILE.name})")
    else:
        print(f"account ready: {args.email}  password: {password}")


if __name__ == "__main__":
    main()
