"""Claude on Amazon Bedrock via bedrock-runtime InvokeModel (Messages API request body).

This account's Bedrock access covers the ARN-versioned Claude 4.6 generation, which is served through
InvokeModel with cross-region inference profiles (e.g. global.anthropic.claude-opus-4-6-v1). The request
body sets `anthropic_version` explicitly, so it is unaffected by SDK API-version defaults.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

log = logging.getLogger("icamfind.llm")

REGION = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION", "ap-southeast-2")
PRIMARY_MODEL = os.environ.get("MODEL_ID", "au.anthropic.claude-haiku-4-5-20251001-v1:0")
FALLBACK_MODEL = os.environ.get("FALLBACK_MODEL_ID", "au.anthropic.claude-sonnet-4-6")
EFFORT = os.environ.get("AGENT_EFFORT", "")  # Haiku 4.5 has no effort control; set e.g. "low" for Opus/Sonnet 4.6

_runtime = boto3.client("bedrock-runtime", region_name=REGION,
                        config=Config(read_timeout=90, retries={"max_attempts": 3, "mode": "adaptive"}))
_active_model = PRIMARY_MODEL
_UNAVAILABLE = ("AccessDeniedException", "ResourceNotFoundException", "ValidationException")


def create(**params) -> dict:
    """Invoke Claude with a client-side fallback: if the primary model is unavailable to this account
    or declines the request, the same request is retried on the fallback model."""
    global _active_model
    body = {"anthropic_version": "bedrock-2023-05-31", **params}
    if "output_config" in body and not body["output_config"]:
        body.pop("output_config")
    models = [_active_model] + ([FALLBACK_MODEL] if FALLBACK_MODEL and FALLBACK_MODEL != _active_model else [])
    last_exc: Exception | None = None
    for model in models:
        try:
            res = _runtime.invoke_model(modelId=model, body=json.dumps(body))
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            msg = exc.response.get("Error", {}).get("Message", "")
            if code == "ValidationException" and "output_config" in body and "effort" in msg.lower():
                body.pop("output_config")  # model without effort support: retry the same model plainly
                try:
                    res = _runtime.invoke_model(modelId=model, body=json.dumps(body))
                except ClientError as exc2:
                    last_exc = exc2
                    continue
            elif code in _UNAVAILABLE:
                log.warning("model %s unavailable: %s %s", model, code, msg[:200])
                last_exc = exc
                if model == _active_model and FALLBACK_MODEL and code != "ValidationException":
                    _active_model = FALLBACK_MODEL
                continue
            else:
                raise
        response = json.loads(res["body"].read())
        response["model"] = model
        if response.get("stop_reason") == "refusal" and model != models[-1]:
            log.warning("model %s refused; retrying on fallback", model)
            continue
        return response
    if last_exc:
        raise last_exc
    return response


def active_model() -> str:
    return _active_model


def text_of(response: dict) -> str:
    return " ".join(b.get("text", "") for b in response.get("content", []) if b.get("type") == "text").strip()


def describe_crop(jpeg: bytes, local_label: str) -> dict:
    """Open-vocabulary label for an object crop: {label, description, essential, kind}.

    kind is "object" for a thing that can be put down, "person" when the crop mainly shows someone (a laptop
    camera often sees the person in front of it), "background" for walls, windows or furniture.
    """
    response = create(
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.standard_b64encode(jpeg).decode()}},
                {"type": "text", "text": (
                    "A home camera noticed a change in this region (centred in the crop). Usually it is an object "
                    f"someone put down or picked up (an on-device detector guessed '{local_label}'), but it can be a "
                    "false alarm: part of a person (face, head, hair, hand, arm, worn clothes) or just a wall, window, "
                    "light, shadow or fixed furniture. First decide which. Then name what is there the way the person "
                    "would when asking where it is. If it is a book, magazine, medicine box, bottle or document and you "
                    "can read a title or product name, use it (e.g. \"The Hobbit (book)\", \"Panadol box\"); otherwise "
                    "give a 2-3 word everyday name in lowercase (e.g. \"house keys\", \"blue hardcover book\"). Reply "
                    "with only a JSON object: {\"kind\": \"person\" if a face, head, hair, hand, arm or worn clothing "
                    "fills most of the crop, \"background\" if there is only wall, window, light, shadow or fixed "
                    "furniture and no movable item, otherwise \"object\", \"label\": the name, \"description\": one "
                    "short sentence with colour, size, material and any readable text, \"essential\": true if it is "
                    "keys, wallet, phone, glasses, medication, hearing aid or ID card, else false}.")},
            ],
        }],
    )
    if response.get("stop_reason") == "refusal":
        return {"label": local_label, "description": "", "essential": False, "kind": "object"}
    match = re.search(r"\{.*\}", text_of(response), re.S)
    data = json.loads(match.group(0)) if match else {}
    kind = str(data.get("kind", "object")).lower()
    return {"label": str(data.get("label") or local_label)[:60], "description": str(data.get("description", ""))[:200],
            "essential": bool(data.get("essential", False)),
            "kind": kind if kind in ("object", "person", "background") else "object"}
