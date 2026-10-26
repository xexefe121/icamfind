"""Erase the cloud memory (objects, events, jobs, alerts, approvals, routines, devices) before a demo.

    python scripts/reset_memory.py --yes [--keep-routines]

Images in S3 are left to their lifecycle rules. Local edge memory lives in data/ (delete that folder).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

import boto3


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--stack", default="icamfind")
    p.add_argument("--region", default=None)
    p.add_argument("--yes", action="store_true", help="required: confirms deletion")
    p.add_argument("--keep-routines", action="store_true")
    args = p.parse_args()
    if not args.yes:
        sys.exit("refusing to erase without --yes")
    session = boto3.session.Session(region_name=args.region)
    cfn = session.client("cloudformation")
    outputs = cfn.describe_stacks(StackName=args.stack)["Stacks"][0]["Outputs"]
    table_name = next(o["OutputValue"] for o in outputs if o["OutputKey"] == "TableName")
    table = session.resource("dynamodb").Table(table_name)
    deleted, start = 0, None
    with table.batch_writer() as batch:
        while True:
            kwargs = {"ProjectionExpression": "pk, sk"}
            if start:
                kwargs["ExclusiveStartKey"] = start
            page = table.scan(**kwargs)
            for item in page["Items"]:
                if args.keep_routines and item["pk"].startswith("ROUTINE#"):
                    continue
                batch.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
                deleted += 1
            start = page.get("LastEvaluatedKey")
            if not start:
                break
    print(json.dumps({"table": table_name, "deleted_items": deleted}))


if __name__ == "__main__":
    main()
