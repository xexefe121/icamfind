"""DynamoDB single-table store and S3 image store for iCamFind."""

from __future__ import annotations

import base64
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

_ddb = boto3.resource("dynamodb", region_name=REGION)
_s3 = boto3.client("s3", region_name=REGION)
table = _ddb.Table(TABLE)


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


# ------------------------------------------------------------------- images

def put_image(key: str, data_b64: str | None = None, data: bytes | None = None) -> str | None:
    if data is None and data_b64:
        data = base64.b64decode(data_b64)
    if not data:
        return None
    _s3.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType="image/jpeg",
                   ServerSideEncryption="AES256")
    return key


def get_image(key: str) -> bytes:
    return _s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()


def url(key: str | None, expires: int = 3600) -> str | None:
    if not key:
        return None
    return _s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=expires)


# ------------------------------------------------------------------- objects & events

def get_object(object_id: str) -> dict | None:
    return clean(table.get_item(Key={"pk": f"OBJ#{object_id}", "sk": "STATE"}).get("Item"))


def put_object(obj: dict) -> dict:
    item = {"pk": f"OBJ#{obj['id']}", "sk": "STATE", "gsi1pk": "OBJECTS",
            "gsi1sk": f"{obj.get('last_seen', now()):017.6f}", **obj}
    table.put_item(Item=to_ddb(item))
    return obj


def list_objects(limit: int = 200) -> list[dict]:
    res = table.query(IndexName="gsi1", KeyConditionExpression=Key("gsi1pk").eq("OBJECTS"),
                      ScanIndexForward=False, Limit=limit)
    return [clean(i) for i in res.get("Items", [])]


def put_event(event: dict) -> dict:
    sk = f"EV#{event['ts']:017.6f}#{event['id']}"
    item = {"pk": f"OBJ#{event['object_id']}", "sk": sk, "gsi1pk": "EVENTS", "gsi1sk": sk[3:], **event}
    table.put_item(Item=to_ddb(item))
    return event


def object_events(object_id: str, limit: int = 50) -> list[dict]:
    res = table.query(KeyConditionExpression=Key("pk").eq(f"OBJ#{object_id}") & Key("sk").begins_with("EV#"),
                      ScanIndexForward=False, Limit=limit)
    return [clean(i) for i in res.get("Items", [])]


def recent_events(limit: int = 50, since: float | None = None) -> list[dict]:
    cond = Key("gsi1pk").eq("EVENTS")
    if since is not None:
        cond = cond & Key("gsi1sk").gte(f"{since:017.6f}")
    res = table.query(IndexName="gsi1", KeyConditionExpression=cond, ScanIndexForward=False, Limit=limit)
    return [clean(i) for i in res.get("Items", [])]


# ------------------------------------------------------------------- devices & commands

def get_device(device_id: str) -> dict | None:
    return clean(table.get_item(Key={"pk": f"DEV#{device_id}", "sk": "META"}).get("Item"))


def put_device(device: dict) -> None:
    table.put_item(Item=to_ddb({"pk": f"DEV#{device['id']}", "sk": "META", "gsi1pk": "DEVICES",
                                "gsi1sk": device["id"], **device}))


def list_devices() -> list[dict]:
    res = table.query(IndexName="gsi1", KeyConditionExpression=Key("gsi1pk").eq("DEVICES"))
    return [clean(i) for i in res.get("Items", [])]


def create_command(device_id: str, kind: str, args: dict) -> dict:
    cmd = {"id": new_id("cmd"), "device": device_id, "kind": kind, "args": args, "status": "pending",
           "created": now()}
    table.put_item(Item=to_ddb({"pk": f"CMD#{cmd['id']}", "sk": "META", "gsi1pk": f"PENDING#{device_id}",
                                "gsi1sk": f"{cmd['created']:017.6f}", "ttl": int(now()) + 86400, **cmd}))
    return cmd


def pending_commands(device_id: str) -> list[dict]:
    res = table.query(IndexName="gsi1", KeyConditionExpression=Key("gsi1pk").eq(f"PENDING#{device_id}"))
    cmds = []
    for item in res.get("Items", []):
        cmd = clean(item)
        if now() - cmd["created"] > 120:
            # Stale (device was offline); expire instead of executing late.
            table.update_item(Key={"pk": f"CMD#{cmd['id']}", "sk": "META"},
                              UpdateExpression="SET #s = :s REMOVE gsi1pk, gsi1sk",
                              ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "expired"})
            continue
        table.update_item(Key={"pk": f"CMD#{cmd['id']}", "sk": "META"},
                          UpdateExpression="SET #s = :s REMOVE gsi1pk, gsi1sk",
                          ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "sent"})
        cmds.append({k: cmd[k] for k in ("id", "kind", "args", "created")})
    return cmds


def complete_command(command_id: str, result: dict, image_key: str | None) -> None:
    table.update_item(
        Key={"pk": f"CMD#{command_id}", "sk": "META"},
        UpdateExpression="SET #s = :s, #r = :r, image_key = :i, completed = :c",
        ExpressionAttributeNames={"#s": "status", "#r": "result"},
        ExpressionAttributeValues=to_ddb({":s": "done", ":r": result, ":i": image_key or "", ":c": now()}),
    )


def get_command(command_id: str) -> dict | None:
    return clean(table.get_item(Key={"pk": f"CMD#{command_id}", "sk": "META"}, ConsistentRead=True).get("Item"))


# ------------------------------------------------------------------- jobs (agent runs)

def create_job(question: str, mode: str, extra: dict | None = None) -> dict:
    job = {"id": new_id("job"), "question": question, "mode": mode, "status": "running", "created": now(),
           **(extra or {})}
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
    steps.sort(key=lambda s: s["n"])
    return meta, steps


# ------------------------------------------------------------------- routines, alerts, approvals

def _put_listed(kind: str, item: dict, sort: str) -> dict:
    table.put_item(Item=to_ddb({"pk": f"{kind}#{item['id']}", "sk": "META", "gsi1pk": f"{kind}S",
                                "gsi1sk": sort, **item}))
    return item


def _list(kind: str, limit: int = 50, newest_first: bool = True) -> list[dict]:
    res = table.query(IndexName="gsi1", KeyConditionExpression=Key("gsi1pk").eq(f"{kind}S"),
                      ScanIndexForward=not newest_first, Limit=limit)
    return [clean(i) for i in res.get("Items", [])]


def put_routine(r: dict) -> dict:
    return _put_listed("ROUTINE", r, r["id"])


def list_routines() -> list[dict]:
    return _list("ROUTINE", 100, newest_first=False)


def get_routine(rid: str) -> dict | None:
    return clean(table.get_item(Key={"pk": f"ROUTINE#{rid}", "sk": "META"}).get("Item"))


def delete_routine(rid: str) -> None:
    table.delete_item(Key={"pk": f"ROUTINE#{rid}", "sk": "META"})


def put_alert(a: dict) -> dict:
    return _put_listed("ALERT", a, f"{a['ts']:017.6f}")


def list_alerts(limit: int = 20) -> list[dict]:
    return _list("ALERT", limit)


def put_approval(a: dict) -> dict:
    return _put_listed("APPROVAL", a, f"{a['created']:017.6f}")


def get_approval(aid: str) -> dict | None:
    return clean(table.get_item(Key={"pk": f"APPROVAL#{aid}", "sk": "META"}).get("Item"))


def list_approvals(status: str | None = "pending", limit: int = 20) -> list[dict]:
    items = _list("APPROVAL", limit)
    return [a for a in items if status is None or a.get("status") == status]


__all__ = ["Attr"]
