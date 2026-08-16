"""request_upload — issues a presigned PUT URL for a single upload.

Flow:
    1. Parse + validate JSON body (email, filename, content_type, size_bytes)
    2. Anti-abuse: conditional PutItem on the email-index table with 24h TTL.
       If the email already has a row, reject with HTTP 429.
    3. Generate upload_id (uuid4) and DynamoDB item with status AWAITING_UPLOAD
    4. Sign a PUT URL for uploads/{upload_id}/{filename} pinned to the declared
       Content-Type. 15-minute expiry — see ADR 005.
    5. Return {upload_id, upload_url, expires_at, key} as 201.

Size enforcement note (see ADR 005): we chose presigned PUT over presigned POST.
PUT cannot carry a content-length-range condition, so we validate declared size
in this handler and re-verify actual size in the process Lambda after upload.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from aws_lambda_powertools import Logger, Tracer
from botocore.config import Config
from botocore.exceptions import ClientError

from shared.models import (
    PRESIGNED_PUT_EXPIRY_SECONDS,
    TTL_HOURS,
    UPLOAD_KEY_PREFIX,
    UploadStatus,
)
from shared.validation import ValidationError, parse_upload_request

logger = Logger()
tracer = Tracer()

UPLOADS_TABLE = os.environ["UPLOADS_TABLE"]
UPLOADS_BUCKET = os.environ["UPLOADS_BUCKET"]
EMAIL_INDEX_TABLE = os.environ["EMAIL_INDEX_TABLE"]

# 24-hour anti-abuse window per email address for the public demo.
EMAIL_COOLDOWN_HOURS: int = 24

# Presigned URL correctness requires all three:
#   * signature_version=s3v4 — SigV4, required in regions launched after 2019
#   * region_name — pins the credential scope (Lambda sets AWS_REGION for us)
#   * addressing_style=virtual — forces `bucket.s3.<region>.amazonaws.com`.
#     Without it boto3 still emits the legacy `bucket.s3.amazonaws.com` host
#     even when region_name is set. Client follows the 307 redirect but the
#     host is in SignedHeaders so the resigned request fails validation (403).
_s3 = boto3.client(
    "s3",
    region_name=os.environ["AWS_REGION"],
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)
_uploads = boto3.resource("dynamodb").Table(UPLOADS_TABLE)
_email_index = boto3.resource("dynamodb").Table(EMAIL_INDEX_TABLE)


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def _reserve_email(email: str, upload_id: str, now: datetime) -> bool:
    """Try to claim this email for a 24h window.

    Returns True if reserved. Returns False if the email already has an active
    reservation — the caller should surface a 429 with retry-after guidance.
    """
    cooldown_ttl = int((now + timedelta(hours=EMAIL_COOLDOWN_HOURS)).timestamp())
    try:
        _email_index.put_item(
            Item={
                "email": email,
                "upload_id": upload_id,
                "reserved_at": now.isoformat(),
                "ttl": cooldown_ttl,
            },
            ConditionExpression="attribute_not_exists(email)",
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


@tracer.capture_lambda_handler
@logger.inject_lambda_context(clear_state=True)
def lambda_handler(event: dict, _context) -> dict:  # noqa: ANN001
    raw = event.get("body")
    if raw is None:
        return _response(400, {"error": "request body required"})
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _response(400, {"error": "body must be JSON"})
    if not isinstance(payload, dict):
        return _response(400, {"error": "body must be a JSON object"})

    try:
        req = parse_upload_request(payload)
    except ValidationError as e:
        logger.info("validation_failed", extra={"reason": str(e)})
        return _response(400, {"error": str(e)})

    upload_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    if not _reserve_email(req.email, upload_id, now):
        logger.info("email_cooldown_active", extra={"email": req.email})
        return _response(
            429,
            {
                "error": (
                    f"This email has already used the demo in the last "
                    f"{EMAIL_COOLDOWN_HOURS} hours. Try again tomorrow or use "
                    "a different address."
                ),
                "retry_after_hours": EMAIL_COOLDOWN_HOURS,
            },
        )

    key = f"{UPLOAD_KEY_PREFIX}{upload_id}/{req.filename}"
    ttl_epoch = int((now + timedelta(hours=TTL_HOURS)).timestamp())

    _uploads.put_item(
        Item={
            "upload_id": upload_id,
            "email": req.email,
            "filename": req.filename,
            "content_type": req.content_type,
            "declared_size": req.size_bytes,
            "s3_key": key,
            "status": UploadStatus.AWAITING_UPLOAD.value,
            "created_at": now.isoformat(),
            "ttl": ttl_epoch,
        }
    )

    upload_url = _s3.generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": UPLOADS_BUCKET,
            "Key": key,
            # Signing the Content-Type pins the header — the client MUST send it
            # exactly, so we can trust the value we record here.
            "ContentType": req.content_type,
        },
        ExpiresIn=PRESIGNED_PUT_EXPIRY_SECONDS,
    )
    expires_at = (now + timedelta(seconds=PRESIGNED_PUT_EXPIRY_SECONDS)).isoformat()

    logger.info(
        "upload_slot_created",
        extra={"upload_id": upload_id, "key": key, "size_declared": req.size_bytes},
    )
    return _response(
        201,
        {
            "upload_id": upload_id,
            "upload_url": upload_url,
            "expires_at": expires_at,
            "key": key,
            "method": "PUT",
            "required_headers": {"Content-Type": req.content_type},
        },
    )
