"""Redrive messages from a Filedrop DLQ back onto its source queue.

Usage:
    python scripts/dlq_redrive.py --queue filedrop-notify-dlq --limit 50
    python scripts/dlq_redrive.py --queue https://sqs.../notify-dlq --dry-run

Semantics:
    * Reads in batches of 10 (SQS max) up to --limit total.
    * Sends to --dest if given, otherwise derives it by stripping "-dlq" suffix.
    * Deletes from the DLQ only after a successful send — a crash mid-run
      leaves the message safely on the DLQ for the next run.
    * --dry-run prints bodies and attributes without moving anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

import boto3


@dataclass
class Stats:
    read: int = 0
    redriven: int = 0
    failed: int = 0

    def to_json(self) -> str:
        return json.dumps({"read": self.read, "redriven": self.redriven, "failed": self.failed})


def _resolve_url(sqs, name_or_url: str) -> str:
    if name_or_url.startswith("https://"):
        return name_or_url
    return sqs.get_queue_url(QueueName=name_or_url)["QueueUrl"]


def _derive_dest_name(dlq_name: str) -> str:
    if not dlq_name.endswith("-dlq"):
        raise SystemExit(f"cannot derive destination from '{dlq_name}' — pass --dest")
    return dlq_name[: -len("-dlq")]


def _dlq_name_from_url(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def redrive(*, queue: str, dest: str | None, limit: int, dry_run: bool) -> Stats:
    sqs = boto3.client("sqs")
    stats = Stats()

    dlq_url = _resolve_url(sqs, queue)
    dlq_name = _dlq_name_from_url(dlq_url)
    dest_url = _resolve_url(sqs, dest or _derive_dest_name(dlq_name))

    print(f"redriving from {dlq_url}\n            to {dest_url}\n  limit={limit} dry_run={dry_run}")

    while stats.read < limit:
        want = min(10, limit - stats.read)
        resp = sqs.receive_message(
            QueueUrl=dlq_url,
            MaxNumberOfMessages=want,
            WaitTimeSeconds=1,
            MessageAttributeNames=["All"],
            AttributeNames=["All"],
        )
        messages = resp.get("Messages", [])
        if not messages:
            break
        stats.read += len(messages)

        for m in messages:
            body = m["Body"]
            attrs = m.get("MessageAttributes") or {}
            if dry_run:
                print(f"--- messageId={m['MessageId']}")
                print(body)
                if attrs:
                    print(f"attrs={list(attrs.keys())}")
                continue
            try:
                sqs.send_message(
                    QueueUrl=dest_url,
                    MessageBody=body,
                    MessageAttributes={
                        k: {"DataType": v["DataType"], "StringValue": v.get("StringValue", "")}
                        for k, v in attrs.items()
                    },
                )
                sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=m["ReceiptHandle"])
                stats.redriven += 1
            except Exception as e:
                # Message may have been picked up by a concurrent consumer;
                # leave it on the DLQ (visibility timeout will make it visible again).
                stats.failed += 1
                print(f"failed messageId={m['MessageId']}: {e}", file=sys.stderr)

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--queue", required=True, help="DLQ name or URL")
    ap.add_argument("--dest", help="Destination queue name/URL (default: strip -dlq suffix)")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    stats = redrive(
        queue=args.queue,
        dest=args.dest,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print(f"summary: {stats.to_json()}")
    return 0 if stats.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
