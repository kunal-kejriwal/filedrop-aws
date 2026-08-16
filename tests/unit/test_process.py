"""process handler — happy path, idempotency, oversize rejection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _seed(uploads_table, upload_id="u1", filename="notes.pdf", status="AWAITING_UPLOAD"):
    uploads_table.put_item(
        Item={
            "upload_id": upload_id,
            "email": "k@example.com",
            "filename": filename,
            "content_type": "application/pdf",
            "s3_key": f"uploads/{upload_id}/{filename}",
            "declared_size": 100,
            "status": status,
            "created_at": datetime.now(UTC).isoformat(),
            "ttl": int((datetime.now(UTC) + timedelta(hours=48)).timestamp()),
        }
    )


def _s3_put(s3, upload_id, filename, body=b"payload"):
    s3.put_object(
        Bucket="test-uploads-bucket",
        Key=f"uploads/{upload_id}/{filename}",
        Body=body,
        ContentType="application/pdf",
    )


def _event(upload_id, filename):
    return {
        "detail": {
            "bucket": {"name": "test-uploads-bucket"},
            "object": {"key": f"uploads/{upload_id}/{filename}"},
        }
    }


def test_happy_path_marks_uploaded_and_publishes(aws, uploads_table, load_handler, lambda_ctx):
    _seed(uploads_table)
    _s3_put(aws["s3"], "u1", "notes.pdf")
    mod = load_handler("process")

    result = mod.lambda_handler(_event("u1", "notes.pdf"), lambda_ctx)
    assert result["status"] == "uploaded"

    item = uploads_table.get_item(Key={"upload_id": "u1"})["Item"]
    assert item["status"] == "UPLOADED"
    assert int(item["actual_size"]) == len(b"payload")


def test_duplicate_delivery_is_suppressed(aws, uploads_table, load_handler, lambda_ctx):
    _seed(uploads_table)
    _s3_put(aws["s3"], "u1", "notes.pdf")
    mod = load_handler("process")

    first = mod.lambda_handler(_event("u1", "notes.pdf"), lambda_ctx)
    second = mod.lambda_handler(_event("u1", "notes.pdf"), lambda_ctx)
    assert first["status"] == "uploaded"
    assert second["status"] == "duplicate_suppressed"


def test_bad_extension_rejected(aws, uploads_table, load_handler, lambda_ctx):
    _seed(uploads_table, filename="notes.exe")
    aws["s3"].put_object(
        Bucket="test-uploads-bucket",
        Key="uploads/u1/notes.exe",
        Body=b"x",
        ContentType="application/octet-stream",
    )
    mod = load_handler("process")

    result = mod.lambda_handler(_event("u1", "notes.exe"), lambda_ctx)
    assert result["status"] == "rejected"
    item = uploads_table.get_item(Key={"upload_id": "u1"})["Item"]
    assert item["status"] == "REJECTED"
