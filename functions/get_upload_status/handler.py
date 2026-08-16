"""get_upload_status — polled by the browser demo to discover when a file finished.

Returns:
    {
        "status": "AWAITING_UPLOAD" | "UPLOADED" | "NOTIFIED" | "REJECTED",
        "filename": str,
        "size_bytes": int,
        "content_type": str,
        "download_url": str,        # only when status in {UPLOADED, NOTIFIED}
        "expires_at":   str,        # ISO-8601 UTC, same condition
        "rejection_reason": str,    # only when status == REJECTED
    }

Path: GET /uploads/{upload_id}/status
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from aws_lambda_powertools import Logger, Tracer
from botocore.config import Config

from shared.models import PRESIGNED_GET_EXPIRY_SECONDS, UploadStatus

logger = Logger()
tracer = Tracer()

UPLOADS_TABLE = os.environ["UPLOADS_TABLE"]
UPLOADS_BUCKET = os.environ["UPLOADS_BUCKET"]

_s3 = boto3.client(
    "s3",
    region_name=os.environ["AWS_REGION"],
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)
_uploads = boto3.resource("dynamodb").Table(UPLOADS_TABLE)

# UUIDv4 pattern for the path param — anything else is a 400 without hitting DDB.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


@tracer.capture_lambda_handler
@logger.inject_lambda_context(clear_state=True)
def lambda_handler(event: dict, _context) -> dict:  # noqa: ANN001
    upload_id = (event.get("pathParameters") or {}).get("upload_id", "")
    if not _UUID_RE.match(upload_id):
        return _response(400, {"error": "invalid upload_id"})

    logger.append_keys(upload_id=upload_id)
    item = _uploads.get_item(Key={"upload_id": upload_id}).get("Item")
    if item is None:
        return _response(404, {"error": "unknown upload_id"})

    # DDB values come back as a broad union; extract + cast explicitly so mypy
    # (and any future reader) can see what we actually expect at runtime.
    status = str(item.get("status", ""))
    size_raw = item.get("actual_size") or item.get("declared_size") or 0
    body: dict[str, Any] = {
        "status": status,
        "filename": str(item.get("filename", "")),
        "size_bytes": int(size_raw),  # type: ignore[arg-type]
        "content_type": str(item.get("actual_content_type") or item.get("content_type") or ""),
    }

    if status == UploadStatus.REJECTED.value:
        body["rejection_reason"] = str(item.get("rejection_reason", "unknown"))
        return _response(200, body)

    if status in (UploadStatus.UPLOADED.value, UploadStatus.NOTIFIED.value):
        key = str(item["s3_key"])
        body["download_url"] = _s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": UPLOADS_BUCKET, "Key": key},
            ExpiresIn=PRESIGNED_GET_EXPIRY_SECONDS,
        )
        body["expires_at"] = (
            datetime.now(UTC) + timedelta(seconds=PRESIGNED_GET_EXPIRY_SECONDS)
        ).isoformat()

    return _response(200, body)
