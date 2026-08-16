"""process — reacts to S3 Object Created events (via EventBridge).

Responsibilities:
    1. HEAD the uploaded object — get actual size, content type, ETag.
    2. Enforce post-upload policy: size ceiling, extension allowlist.
       On violation: tag object quarantine=true, mark DynamoDB REJECTED,
       publish a file_rejected SNS event, and exit success.
    3. Otherwise flip status to UPLOADED using a conditional update so that
       duplicate S3/EventBridge deliveries are suppressed idempotently
       (see ADR 004). Then publish a file_uploaded event.

At-least-once delivery is the norm here — never treat a duplicate as an error.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import boto3
from aws_lambda_powertools import Logger, Tracer
from botocore.exceptions import ClientError

from shared.models import (
    ALLOWED_EXTENSIONS,
    MAX_SIZE_BYTES,
    EventType,
    UploadStatus,
)

logger = Logger()
tracer = Tracer()

UPLOADS_TABLE = os.environ["UPLOADS_TABLE"]
UPLOADS_BUCKET = os.environ["UPLOADS_BUCKET"]
EVENTS_TOPIC_ARN = os.environ["EVENTS_TOPIC_ARN"]

_s3 = boto3.client("s3")
_sns = boto3.client("sns")
_ddb = boto3.resource("dynamodb").Table(UPLOADS_TABLE)


def _extract_key(event: dict) -> str:
    """Get the S3 object key from an EventBridge Object Created event."""
    detail = event.get("detail") or {}
    obj = detail.get("object") or {}
    key = obj.get("key")
    if not key:
        raise ValueError("event missing detail.object.key")
    return str(key)


def _upload_id_from_key(key: str) -> str:
    # uploads/<upload_id>/<filename>
    parts = key.split("/", 2)
    if len(parts) < 3 or parts[0] != "uploads":
        raise ValueError(f"unexpected key layout: {key}")
    return parts[1]


def _publish(event_type: EventType, upload_id: str, content_type: str, details: dict) -> None:
    _sns.publish(
        TopicArn=EVENTS_TOPIC_ARN,
        # Raw-message delivery is on for the SQS subscriptions, so this string is
        # what audit/notify Lambdas will actually parse.
        Message=json.dumps(
            {
                "event_type": event_type.value,
                "upload_id": upload_id,
                "content_type": content_type,
                "details": details,
                "emitted_at": datetime.now(UTC).isoformat(),
            }
        ),
        # Message attributes drive SNS filter policies — notify-queue only
        # matches event_type=file_uploaded.
        MessageAttributes={
            "event_type": {"DataType": "String", "StringValue": event_type.value},
            "content_type": {"DataType": "String", "StringValue": content_type},
        },
    )


def _reject(upload_id: str, key: str, reason: str, size: int, content_type: str) -> None:
    logger.warning("rejecting_upload", extra={"upload_id": upload_id, "reason": reason})
    try:
        _s3.put_object_tagging(
            Bucket=UPLOADS_BUCKET,
            Key=key,
            Tagging={
                "TagSet": [
                    {"Key": "quarantine", "Value": "true"},
                    {"Key": "reason", "Value": reason[:250]},
                ]
            },
        )
    except ClientError:
        # Tagging is best-effort; SNS + DynamoDB status are the source of truth.
        logger.exception("tagging_failed", extra={"key": key})
    _ddb.update_item(
        Key={"upload_id": upload_id},
        UpdateExpression="SET #s = :rej, rejection_reason = :r, processed_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":rej": UploadStatus.REJECTED.value,
            ":r": reason,
            ":t": datetime.now(UTC).isoformat(),
        },
    )
    _publish(
        EventType.FILE_REJECTED,
        upload_id,
        content_type,
        {"reason": reason, "size_bytes": size, "s3_key": key},
    )


@tracer.capture_lambda_handler
@logger.inject_lambda_context(clear_state=True)
def lambda_handler(event: dict, _context) -> dict:  # noqa: ANN001
    key = _extract_key(event)
    upload_id = _upload_id_from_key(key)
    logger.append_keys(upload_id=upload_id, s3_key=key)

    head = _s3.head_object(Bucket=UPLOADS_BUCKET, Key=key)
    size = int(head["ContentLength"])
    content_type = str(head.get("ContentType", "application/octet-stream"))
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""

    if size > MAX_SIZE_BYTES:
        _reject(upload_id, key, f"size {size} > max {MAX_SIZE_BYTES}", size, content_type)
        return {"status": "rejected", "reason": "oversize"}
    if ext not in ALLOWED_EXTENSIONS:
        _reject(upload_id, key, f"extension .{ext} not allowed", size, content_type)
        return {"status": "rejected", "reason": "extension"}

    # Idempotency gate: only the first delivery flips AWAITING_UPLOAD → UPLOADED.
    # Any redelivery finds status != AWAITING_UPLOAD and gets a ConditionalCheckFailed.
    try:
        _ddb.update_item(
            Key={"upload_id": upload_id},
            UpdateExpression=(
                "SET #s = :up, actual_size = :sz, actual_content_type = :ct, "
                "etag = :et, processed_at = :t"
            ),
            ConditionExpression="#s = :aw",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":up": UploadStatus.UPLOADED.value,
                ":aw": UploadStatus.AWAITING_UPLOAD.value,
                ":sz": size,
                ":ct": content_type,
                ":et": head.get("ETag", "").strip('"'),
                ":t": datetime.now(UTC).isoformat(),
            },
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info("duplicate_event_suppressed")
            return {"status": "duplicate_suppressed"}
        raise

    _publish(
        EventType.FILE_UPLOADED,
        upload_id,
        content_type,
        {"size_bytes": size, "s3_key": key},
    )
    return {"status": "uploaded", "size_bytes": size}
