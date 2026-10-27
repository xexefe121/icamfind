"""Serve the iCamFind web app locally against the deployed backend, for recording the demo video.

The real API code (cloud/src/api.py) runs in this process with the deployed stack's settings, so data goes
to the real DynamoDB table and S3 bucket and questions go to the real agent Lambda (Claude on Bedrock).
Only sign-in is local: a stub Cognito hands out an id token for one fixed demo identity, which gets its own
private household. Nothing here needs a password.

    python video/demo2/local_stack.py [--port 8790] [--sub video-demo-1] [--email margaret@icamfind.example]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import boto3

ROOT = Path(__file__).resolve().parents[2]
REGION = "ap-southeast-2"


def deployed_env() -> dict:
    cf = boto3.client("cloudformation", region_name=REGION)
    res = {r["LogicalResourceId"]: r["PhysicalResourceId"]
           for r in cf.describe_stack_resources(StackName="icamfind")["StackResources"]}
    lam = boto3.client("lambda", region_name=REGION)
    return lam.get_function_configuration(FunctionName=res["ApiFunction"])["Environment"]["Variables"]


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(sub: str, email: str) -> str:
    head = b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    body = b64url(json.dumps({"sub": sub, "email": email, "email_verified": True,
                              "exp": int(time.time()) + 12 * 3600}).encode())
    return f"{head}.{body}.local"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--sub", default="video-demo-1")
    p.add_argument("--email", default="margaret@icamfind.example")
    args = p.parse_args()
    base = f"http://localhost:{args.port}"

    os.environ.update(deployed_env())
    os.environ.update({"AWS_REGION": REGION, "API_URL": base, "COGNITO_DOMAIN": f"{base}/stubcognito",
                       "USER_POOL_CLIENT_ID": "local"})
    sys.path.insert(0, str(ROOT / "cloud" / "src"))
    import api  # noqa: E402  (reads the environment at import)

    claims = {"sub": args.sub, "email": args.email}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            if "/api/device/commands" not in (a[0] if a else ""):
                sys.stderr.write("%s\n" % (fmt % a))

        def _send(self, status, body: bytes, headers: dict):
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location):
            self._send(302, b"", {"location": location})

        def _handle(self, method):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n) if n else b""
            if u.path == "/stubcognito/oauth2/authorize":
                return self._redirect(f"{qs['redirect_uri'][0]}?{urlencode({'code': 'local', 'state': qs.get('state', [''])[0]})}")
            if u.path == "/stubcognito/oauth2/token":
                tok = make_jwt(args.sub, args.email)
                body = json.dumps({"id_token": tok, "access_token": tok, "refresh_token": "local",
                                   "expires_in": 12 * 3600, "token_type": "Bearer"}).encode()
                return self._send(200, body, {"content-type": "application/json"})
            if u.path == "/stubcognito/logout":
                return self._redirect(qs.get("logout_uri", ["/"])[0])
            event = {
                "requestContext": {"http": {"method": method}},
                "rawPath": u.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "queryStringParameters": {k: v[0] for k, v in qs.items()} or None,
                "body": raw.decode() if raw else None,
                "isBase64Encoded": False,
            }
            if u.path.startswith("/api/") and not u.path.startswith("/api/device/"):
                event["requestContext"]["authorizer"] = {"jwt": {"claims": claims}}
            res = api.handler(event, None)
            body = res.get("body") or ""
            data = base64.b64decode(body) if res.get("isBase64Encoded") else body.encode()
            self._send(res.get("statusCode", 200), data, res.get("headers") or {})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def do_DELETE(self):
            self._handle("DELETE")

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"serving {base} (identity {args.sub})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
