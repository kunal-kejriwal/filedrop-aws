"""notify handler — SES send + status transition + duplicate suppression."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta


def _seed(uploads_table, status="UPLOADED"):
    uploads_table.put_item(
        Item={
            "upload_id": "u1",
            "email": "recipient@example.com",
            "filename": "notes.pdf",
            "content_type": "application/pdf",
            "actual_content_type": "application/pdf",
            "actual_size": 512,
            "s3_key": "uploads/u1/notes.pdf",
            "status": status,
            "created_at": datetime.now(UTC).isoformat(),
            "ttl": int((datetime.now(UTC) + timedelta(hours=48)).timestamp()),
        }
    )


def _sqs_event(payload):
    return {
        "Records": [
            {"messageId": "m1", "body": json.dumps(payload)},
        ]
    }


def test_happy_path_sends_email_and_marks_notified(aws, uploads_table, load_handler, lambda_ctx):
    _seed(uploads_table)
    aws["s3"].put_object(
        Bucket="test-uploads-bucket",
        Key="uploads/u1/notes.pdf",
        Body=b"payload",
        ContentType="application/pdf",
    )
    mod = load_handler("notify")

    resp = mod.lambda_handler(
        _sqs_event({"event_type": "file_uploaded", "upload_id": "u1"}),
        lambda_ctx,
    )
    assert resp == {"batchItemFailures": []}
    assert uploads_table.get_item(Key={"upload_id": "u1"})["Item"]["status"] == "NOTIFIED"

    sent = aws["ses"].list_identities()
    assert "sender@example.com" in sent["Identities"]


def test_duplicate_notify_is_suppressed(aws, uploads_table, load_handler, lambda_ctx):
    _seed(uploads_table, status="NOTIFIED")
    aws["s3"].put_object(
        Bucket="test-uploads-bucket",
        Key="uploads/u1/notes.pdf",
        Body=b"payload",
        ContentType="application/pdf",
    )
    mod = load_handler("notify")

    resp = mod.lambda_handler(
        _sqs_event({"event_type": "file_uploaded", "upload_id": "u1"}),
        lambda_ctx,
    )
    # No exception raised, no batch failure — the ConditionalCheckFailed is swallowed.
    assert resp == {"batchItemFailures": []}


def test_missing_row_is_not_a_failure(aws, load_handler, lambda_ctx):
    mod = load_handler("notify")
    resp = mod.lambda_handler(
        _sqs_event({"event_type": "file_uploaded", "upload_id": "does-not-exist"}),
        lambda_ctx,
    )
    assert resp == {"batchItemFailures": []}
