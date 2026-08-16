"""notify — SQS-triggered Lambda that emails the recipient with a download link.

For each file_uploaded event (filtered by SNS subscription):
    1. Read the uploads row to recover the recipient + original filename.
    2. Sign a 24-hour GET URL for the object.
    3. Send an SES email (text + minimal HTML) with the link + expiry timestamp.
    4. Mark status NOTIFIED conditionally on status = UPLOADED — same
       idempotency pattern as the process Lambda (ADR 004).

Any raised exception here fails the SQS batch item; SQS will redeliver up to
maxReceiveCount=3, then the message lands in the DLQ.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from aws_lambda_powertools import Logger, Tracer
from botocore.config import Config
from botocore.exceptions import ClientError

from shared.models import PRESIGNED_GET_EXPIRY_SECONDS, UploadStatus
from shared.validation import human_readable_size

logger = Logger()
tracer = Tracer()

UPLOADS_TABLE = os.environ["UPLOADS_TABLE"]
UPLOADS_BUCKET = os.environ["UPLOADS_BUCKET"]
SENDER_EMAIL = os.environ["SENDER_EMAIL"]

# See request_upload/handler.py for the full explanation — same three-part fix.
_s3 = boto3.client(
    "s3",
    region_name=os.environ["AWS_REGION"],
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)
_ses = boto3.client("ses")
_ddb = boto3.resource("dynamodb").Table(UPLOADS_TABLE)


def _render_email(
    *, filename: str, size_bytes: int, content_type: str, url: str, expires_at: str
) -> tuple[str, str]:
    """Return (text_body, html_body). Kept trivial on purpose — no templating dep."""
    pretty_size = human_readable_size(size_bytes)
    text = (
        "Your file is ready.\n\n"
        f"Filename:     {filename}\n"
        f"Size:         {pretty_size}\n"
        f"Content type: {content_type}\n"
        f"Download:     {url}\n\n"
        f"This link expires at {expires_at} (UTC). After that it stops working.\n"
        "— Filedrop"
    )
    html = (
        "<p>Your file is ready.</p>"
        "<ul>"
        f"<li><b>Filename:</b> {filename}</li>"
        f"<li><b>Size:</b> {pretty_size}</li>"
        f"<li><b>Content type:</b> {content_type}</li>"
        "</ul>"
        f'<p><a href="{url}">Download your file</a></p>'
        f"<p style='color:#666'>Link expires at {expires_at} (UTC).</p>"
    )
    return text, html


def _process_record(body: str) -> None:
    payload: dict[str, Any] = json.loads(body)
    upload_id = payload["upload_id"]
    logger.append_keys(upload_id=upload_id)

    item = _ddb.get_item(Key={"upload_id": upload_id}).get("Item")
    if item is None:
        logger.warning("uploads_row_missing")
        return

    key = item["s3_key"]
    url = _s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": UPLOADS_BUCKET, "Key": key},
        ExpiresIn=PRESIGNED_GET_EXPIRY_SECONDS,
    )
    expires_at = (datetime.now(UTC) + timedelta(seconds=PRESIGNED_GET_EXPIRY_SECONDS)).isoformat()

    # DDB values come back as a broad union; cast the fields we control to their
    # actual runtime types so mypy has the same view we do.
    filename = str(item["filename"])
    email = str(item["email"])
    size_raw = item.get("actual_size") or item.get("declared_size") or 0
    text, html = _render_email(
        filename=filename,
        size_bytes=int(size_raw),  # type: ignore[arg-type]
        content_type=str(item.get("actual_content_type") or item.get("content_type")),
        url=url,
        expires_at=expires_at,
    )

    _ses.send_email(
        Source=SENDER_EMAIL,
        Destination={"ToAddresses": [email]},
        Message={
            "Subject": {"Data": "Your file is ready — Filedrop", "Charset": "UTF-8"},
            "Body": {
                "Text": {"Data": text, "Charset": "UTF-8"},
                "Html": {"Data": html, "Charset": "UTF-8"},
            },
        },
    )

    # Same conditional pattern as process: only the first delivery flips
    # UPLOADED → NOTIFIED. Duplicate deliveries are dropped silently.
    try:
        _ddb.update_item(
            Key={"upload_id": upload_id},
            UpdateExpression="SET #s = :n, notified_at = :t",
            ConditionExpression="#s = :u",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":n": UploadStatus.NOTIFIED.value,
                ":u": UploadStatus.UPLOADED.value,
                ":t": datetime.now(UTC).isoformat(),
            },
        )
        logger.info("notified", extra={"email": email})
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info("duplicate_notify_suppressed")
            return
        raise


@tracer.capture_lambda_handler
@logger.inject_lambda_context(clear_state=True)
def lambda_handler(event: dict, _context) -> dict:  # noqa: ANN001
    records = event.get("Records", [])
    batch_item_failures = []
    for record in records:
        try:
            _process_record(record["body"])
        except Exception:
            logger.exception("record_failed", extra={"message_id": record.get("messageId")})
            # Partial-batch response: only failed items are re-delivered.
            batch_item_failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": batch_item_failures}
