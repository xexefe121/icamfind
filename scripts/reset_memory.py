"""Erase cloud memory before a demo: everything, or one household.

    python scripts/reset_memory.py --yes                      # all data (keeps nothing)
    python scripts/reset_memory.py --yes --household demo     # one home: objects, events, jobs, routines,
                                                              # alerts, approvals, commands, its cameras' records
    python scripts/reset_memory.py --yes --household hh_123 --forget-cameras   # also revoke that home's cameras

For the demo home, the replay camera's credential is always kept. Images in S3 are left to their
lifecycle rules. Local edge memory lives in data/ (delete that folder).
"""

from __future__ import annotations

import argparse
import json
import sys

import boto3


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--stack", default="icamfind")
    p.add_argument("--region", default=None)
    p.add_argument("--yes", action="store_true", help="required: confirms deletion")
    p.add_argument("--household", default=None, help="only erase this household's data")
    p.add_argument("--forget-cameras", action="store_true", help="with --household: also revoke its camera credentials")
    p.add_argument("--keep-routines", action="store_true")
    args = p.parse_args()
    if not args.yes:
        sys.exit("refusing to erase without --yes")
    session = boto3.session.Session(region_name=args.region)
    outputs = session.client("cloudformation").describe_stacks(StackName=args.stack)["Stacks"][0]["Outputs"]
    table_name = next(o["OutputValue"] for o in outputs if o["OutputKey"] == "TableName")
    table = session.resource("dynamodb").Table(table_name)

    def doomed(item: dict) -> bool:
        pk = item["pk"]
        if args.keep_routines and pk.startswith("ROUTINE#"):
            return False
        if not args.household:
            return True
        hid = args.household
        if pk.startswith(f"H#{hid}#"):
            return True
        if item.get("household") != hid:
            return False
        if pk.startswith("DEVTOKEN#"):
            return args.forget_cameras and hid != "demo"
        return not pk.startswith(("USER#", "HH#"))

    deleted, start = 0, None
    with table.batch_writer() as batch:
        while True:
            kwargs = {"ProjectionExpression": "pk, sk, household"}
            if start:
                kwargs["ExclusiveStartKey"] = start
            page = table.scan(**kwargs)
            for item in page["Items"]:
                if doomed(item):
                    batch.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
                    deleted += 1
            start = page.get("LastEvaluatedKey")
            if not start:
                break
    print(json.dumps({"table": table_name, "household": args.household or "ALL", "deleted_items": deleted}))


if __name__ == "__main__":
    main()
