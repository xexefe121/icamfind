"""DynamoDB single-table store and S3 image store for iCamFind, partitioned by household.

Every request handler calls `set_household(hid)` first; all household data keys are prefixed with
`H#<hid>#`, so one household can never read another's memory. Items addressed by a globally unique
id (commands, jobs, approvals, routines) carry a `household` attribute that is checked on read.
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
import uuid
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key

TABLE = os.environ.get("TABLE_NAME", "icamfind")
BUCKET = os.environ.get("BUCKET_NAME", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")
DEMO_HOUSEHOLD = "demo"

_ddb = boto3.resource("dynamodb", region_name=REGION)
_s3 = boto3.client("s3", region_name=REGION)
table = _ddb.Table(TABLE)

HOUSEHOLD: str | None = None


def set_household(household_id: str) -> None:
    global HOUSEHOLD
    HOUSEHOLD = household_id


def _h(key: str) -> str:
    if not HOUSEHOLD:
        raise RuntimeError("household not set")
    return f"H#{HOUSEHOLD}#{key}"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def now() -> float:
    return time.time()


def to_ddb(value: Any) -> Any:
    """Convert floats to Decimal recursively (DynamoDB rejects float)."""
    if isinstance(value, float):
        return Decimal(str(round(value, 6)))
    if isinstance(value, dict):
        return {k: to_ddb(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [to_ddb(v) for v in value]
    return value


def from_ddb(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() and abs(value) < 2**53 else float(value)
    if isinstance(value, dict):
        return {k: from_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_ddb(v) for v in value]
    return value


def clean(item: dict | None) -> dict | None:
    if item is None:
        return None
    item = from_ddb(item)
    for k in ("pk", "sk", "gsi1pk", "gsi1sk", "ttl"):
        item.pop(k, None)
    return item


def _mine(item: dict | None) -> dict | None:
    """Return a globally-addressed item only if it belongs to the current household."""
    item = clean(item)
    if item is None or item.get("household") != HOUSEHOLD:
        return None
    return item


# ------------------------------------------------------------------- users, households, device tokens

def get_user(sub: str) -> dict | None:
    return clean(table.get_item(Key={"pk": f"USER#{sub}", "sk": "META"}).get("Item"))


def ensure_user(sub: str, email: str) -> dict:
    """First sign-in creates a private household and joins the shared demo household."""
    user = get_user(sub)
    if user:
        return user
    hid = new_id("hh")
    table.put_item(Item=to_ddb({"pk": f"HH#{hid}", "sk": "META", "id": hid, "name": "My home", "owner": sub,
                                "created": now()}))
    user = {"sub": sub, "email": email, "households": [hid, DEMO_HOUSEHOLD], "owner_of": [hid], "created": now()}
    table.put_item(Item=to_ddb({"pk": f"USER#{sub}", "sk": "META", **user}),
                   ConditionExpression="attribute_not_exists(pk)")
    return user


def get_household(hid: str) -> dict | None:
    if hid == DEMO_HOUSEHOLD:
        return {"id": DEMO_HOUSEHOLD, "name": "Demo home", "demo": True}
    item = clean(table.get_item(Key={"pk": f"HH#{hid}", "sk": "META"}).get("Item"))
    if item:
        item["demo"] = False
    return item


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_device(name: str, kind: str = "computer") -> tuple[dict, str]:
    import secrets

    token = "icf_" + secrets.token_urlsafe(32)
    device = {"id": new_id("dev"), "name": name[:60] or "Camera", "kind": kind, "household": HOUSEHOLD,
              "created": now()}
    table.put_item(Item=to_ddb({"pk": f"DEVTOKEN#{token_hash(token)}", "sk": "META", **device}))
    table.put_item(Item=to_ddb({"pk": _h(f"DEV#{device['id']}"), "sk": "META", "gsi1pk": _h("DEVICES"),
                                "gsi1sk": device["id"], **device}))
    return device, token


PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I confusion


def create_pair_code(name: str, ttl: int = 600) -> dict:
    """Short-lived one-time code that lets another computer join this home without copying a token."""
    import secrets

    raw = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(8))
    code = f"{raw[:4]}-{raw[4:]}"
    item = {"code": code, "household": HOUSEHOLD, "name": name[:60] or "Camera", "created": now(),
            "expires": now() + ttl, "used": False}
    table.put_item(Item=to_ddb({"pk": f"PAIR#{code}", "sk": "META", "ttl": int(now()) + ttl, **item}))
    return item


def redeem_pair_code(code: str) -> dict | None:
    """Atomically consume a pairing code; returns its item (household, name) or None."""
    code = code.strip().upper().replace(" ", "")
    if len(code) == 8:
        code = f"{code[:4]}-{code[4:]}"
    try:
        res = table.update_item(
            Key={"pk": f"PAIR#{code}", "sk": "META"},
            UpdateExpression="SET used = :t",
            ConditionExpression="attribute_exists(pk) AND used = :f AND expires > :now",
            ExpressionAttributeValues=to_ddb({":t": True, ":f": False, ":now": now()}),
            ReturnValues="ALL_NEW")
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return None
    return clean(res["Attributes"])


def device_for_token(token: str) -> dict | None:
    if not token:
        return None
    return clean(table.get_item(Key={"pk": f"DEVTOKEN#{token_hash(token)}", "sk": "META"}).get("Item"))


def register_device_token(token_sha256: str, household: str, device_id: str, name: str) -> None:
    """Idempotently bind a pre-generated device token hash (used for the demo replay camera)."""
    table.put_item(Item=to_ddb({"pk": f"DEVTOKEN#{token_sha256}", "sk": "META", "id": device_id, "name": name,
                                "household": household, "created": now()}))


# ------------------------------------------------------------------- images

def put_image(key: str, data_b64: str | None = None, data: bytes | None = None) -> str | None:
    if data is None and data_b64:
        data = base64.b64decode(data_b64)
    if not data:
        return None
    kind, _, rest = key.partition("/")  # keep the type prefix first so S3 lifecycle rules apply
    key = f"{kind}/{HOUSEHOLD}/{rest}"
    _s3.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType="image/jpeg", ServerSideEncryption="AES256")
    return key


def _owned(key: str) -> bool:
    parts = key.split("/")
    return len(parts) > 2 and parts[1] == HOUSEHOLD


def get_image(key: str) -> bytes:
    if not _owned(key):
        raise PermissionError("image belongs to another household")
    return _s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()


def url(key: str | None, expires: int = 3600) -> str | None:
    if not key or not _owned(key):
        return None
    return _s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=expires)


# ------------------------------------------------------------------- objects & events

def get_object(object_id: str) -> dict | None:
    return clean(table.get_item(Key={"pk": _h(f"OBJ#{object_id}"), "sk": "STATE"}).get("Item"))


def put_object(obj: dict) -> dict:
    item = {"pk": _h(f"OBJ#{obj['id']}"), "sk": "STATE", "gsi1pk": _h("OBJECTS"),
            "gsi1sk": f"{obj.get('last_seen', now()):017.6f}", **obj}
    table.put_item(Item=to_ddb(item))
    return obj


def list_objects(limit: int = 200) -> list[dict]:
    res = table.query(IndexName="gsi1", KeyConditionExpression=Key("gsi1pk").eq(_h("OBJECTS")),
                      ScanIndexForward=False, Limit=limit)
    return [clean(i) for i in res.get("Items", [])]


def put_event(event: dict) -> dict:
    sk = f"EV#{event['ts']:017.6f}#{event['id']}"
    item = {"pk": _h(f"OBJ#{event['object_id']}"), "sk": sk, "gsi1pk": _h("EVENTS"), "gsi1sk": sk[3:], **event}
    table.put_item(Item=to_ddb(item))
    return event


def object_events(object_id: str, limit: int = 50) -> list[dict]:
    res = table.query(KeyConditionExpression=Key("pk").eq(_h(f"OBJ#{object_id}")) & Key("sk").begins_with("EV#"),
                      ScanIndexForward=False, Limit=limit)
    return [clean(i) for i in res.get("Items", [])]


def recent_events(limit: int = 50) -> list[dict]:
    res = table.query(IndexName="gsi1", KeyConditionExpression=Key("gsi1pk").eq(_h("EVENTS")),
                      ScanIndexForward=False, Limit=limit)
    return [clean(i) for i in res.get("Items", [])]


# ------------------------------------------------------------------- devices & commands

def get_device(device_id: str) -> dict | None:
    return clean(table.get_item(Key={"pk": _h(f"DEV#{device_id}"), "sk": "META"}).get("Item"))


def put_device(device: dict) -> None:
    table.put_item(Item=to_ddb({"pk": _h(f"DEV#{device['id']}"), "sk": "META", "gsi1pk": _h("DEVICES"),
                                "gsi1sk": device["id"], **device, "household": HOUSEHOLD}))


def list_devices() -> list[dict]:
    res = table.query(IndexName="gsi1", KeyConditionExpression=Key("gsi1pk").eq(_h("DEVICES")))
    return [clean(i) for i in res.get("Items", [])]


def create_command(device_id: str, kind: str, args: dict) -> dict:
    cmd = {"id": new_id("cmd"), "device": device_id, "household": HOUSEHOLD, "kind": kind, "args": args,
           "status": "pending", "created": now()}
    table.put_item(Item=to_ddb({"pk": f"CMD#{cmd['id']}", "sk": "META", "gsi1pk": _h(f"PENDING#{device_id}"),
                                "gsi1sk": f"{cmd['created']:017.6f}", "ttl": int(now()) + 86400, **cmd}))
    return cmd


def pending_commands(device_id: str) -> list[dict]:
    res = table.query(IndexName="gsi1", KeyConditionExpression=Key("gsi1pk").eq(_h(f"PENDING#{device_id}")))
    cmds = []
    for item in res.get("Items", []):
        cmd = clean(item)
        status = "expired" if now() - cmd["created"] > 120 else "sent"  # stale: device was offline
        table.update_item(Key={"pk": f"CMD#{cmd['id']}", "sk": "META"},
                          UpdateExpression="SET #s = :s REMOVE gsi1pk, gsi1sk",
                          ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": status})
        if status == "sent":
            cmds.append({k: cmd[k] for k in ("id", "kind", "args", "created")})
    return cmds


def complete_command(command_id: str, device_id: str, result: dict, image_key: str | None) -> bool:
    try:
        table.update_item(
            Key={"pk": f"CMD#{command_id}", "sk": "META"},
            UpdateExpression="SET #s = :s, #r = :r, image_key = :i, completed = :c",
            ConditionExpression="household = :h AND device = :d",
            ExpressionAttributeNames={"#s": "status", "#r": "result"},
            ExpressionAttributeValues=to_ddb({":s": "done", ":r": result, ":i": image_key or "", ":c": now(),
                                              ":h": HOUSEHOLD, ":d": device_id}),
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def get_command(command_id: str) -> dict | None:
    return _mine(table.get_item(Key={"pk": f"CMD#{command_id}", "sk": "META"}, ConsistentRead=True).get("Item"))


# ------------------------------------------------------------------- jobs (agent runs)

def create_job(question: str, mode: str, extra: dict | None = None) -> dict:
    job = {"id": new_id("job"), "household": HOUSEHOLD, "question": question, "mode": mode, "status": "running",
           "created": now(), **(extra or {})}
    table.put_item(Item=to_ddb({"pk": f"JOB#{job['id']}", "sk": "META", "ttl": int(now()) + 7 * 86400, **job}))
    return job


def update_job(job_id: str, **fields) -> None:
    names = {f"#{k}": k for k in fields}
    values = {f":{k}": v for k, v in fields.items()}
    table.update_item(Key={"pk": f"JOB#{job_id}", "sk": "META"},
                      UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
                      ExpressionAttributeNames=names, ExpressionAttributeValues=to_ddb(values))


def add_step(job_id: str, n: int, step: dict) -> None:
    table.put_item(Item=to_ddb({"pk": f"JOB#{job_id}", "sk": f"STEP#{n:04d}", "ttl": int(now()) + 7 * 86400,
                                "n": n, "ts": now(), **step}))


def get_job(job_id: str) -> tuple[dict | None, list[dict]]:
    res = table.query(KeyConditionExpression=Key("pk").eq(f"JOB#{job_id}"), ConsistentRead=True)
    meta, steps = None, []
    for item in res.get("Items", []):
        if item["sk"] == "META":
            meta = clean(item)
        else:
            steps.append(clean(item))
    if meta is None or meta.get("household") != HOUSEHOLD:
        return None, []
    steps.sort(key=lambda s: s["n"])
    return meta, steps


# ------------------------------------------------------------------- routines, alerts, approvals

def _put_listed(kind: str, item: dict, sort: str) -> dict:
    item = {**item, "household": HOUSEHOLD}
    table.put_item(Item=to_ddb({"pk": f"{kind}#{item['id']}", "sk": "META", "gsi1pk": _h(f"{kind}S"),
                                "gsi1sk": sort, **item}))
    return item


def _list(kind: str, limit: int = 50, newest_first: bool = True) -> list[dict]:
    res = table.query(IndexName="gsi1", KeyConditionExpression=Key("gsi1pk").eq(_h(f"{kind}S")),
                      ScanIndexForward=not newest_first, Limit=limit)
    return [clean(i) for i in res.get("Items", [])]


def put_routine(r: dict) -> dict:
    return _put_listed("ROUTINE", r, r["id"])


def list_routines() -> list[dict]:
    return _list("ROUTINE", 100, newest_first=False)


def all_routines() -> list[dict]:
    """Every household's routines (scheduler only)."""
    items, start = [], None
    while True:
        kwargs = {"FilterExpression": Attr("pk").begins_with("ROUTINE#")}
        if start:
            kwargs["ExclusiveStartKey"] = start
        page = table.scan(**kwargs)
        items += [clean(i) for i in page.get("Items", [])]
        start = page.get("LastEvaluatedKey")
        if not start:
            return items


def get_routine(rid: str) -> dict | None:
    return _mine(table.get_item(Key={"pk": f"ROUTINE#{rid}", "sk": "META"}).get("Item"))


def delete_routine(rid: str) -> bool:
    if get_routine(rid) is None:
        return False
    table.delete_item(Key={"pk": f"ROUTINE#{rid}", "sk": "META"})
    return True


def put_alert(a: dict) -> dict:
    return _put_listed("ALERT", a, f"{a['ts']:017.6f}")


def list_alerts(limit: int = 20) -> list[dict]:
    return _list("ALERT", limit)


def put_approval(a: dict) -> dict:
    return _put_listed("APPROVAL", a, f"{a['created']:017.6f}")


def get_approval(aid: str) -> dict | None:
    return _mine(table.get_item(Key={"pk": f"APPROVAL#{aid}", "sk": "META"}).get("Item"))


def list_approvals(status: str | None = "pending", limit: int = 20) -> list[dict]:
    items = _list("APPROVAL", limit)
    return [a for a in items if status is None or a.get("status") == status]
