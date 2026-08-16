"""audit — SQS-triggered Lambda that appends an event record for every SNS message.

No filter policy on this queue, so audit sees both file_uploaded and
file_rejected events. Writes are append-only; the sort key is the emitted-at
timestamp so a single upload_id can accrue multiple rows.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import boto3
from aws_lambda_powertools import Logger, Tracer

logger = Logger()
tracer = Tracer()

AUDIT_TABLE = os.environ["AUDIT_TABLE"]
_ddb = boto3.resource("dynamodb").Table(AUDIT_TABLE)


def _process_record(body: str) -> None:
    payload = json.loads(body)
    upload_id = payload["upload_id"]
    event_type = payload["event_type"]
    # Prefer the producer's timestamp; fall back to now() if the field is missing
    # so we never lose the row over a schema hiccup.
    emitted_at = payload.get("emitted_at") or datetime.now(UTC).isoformat()

    _ddb.put_item(
        Item={
            "upload_id": upload_id,
            "event_timestamp": emitted_at,
            "event_type": event_type,
            "details": payload.get("details", {}),
            "content_type": payload.get("content_type"),
        }
    )
    logger.info("audited", extra={"upload_id": upload_id, "event_type": event_type})


@tracer.capture_lambda_handler
@logger.inject_lambda_context(clear_state=True)
def lambda_handler(event: dict, _context) -> dict:  # noqa: ANN001
    batch_item_failures = []
    for record in event.get("Records", []):
        try:
            _process_record(record["body"])
        except Exception:
            logger.exception("audit_record_failed", extra={"message_id": record.get("messageId")})
            batch_item_failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": batch_item_failures}
