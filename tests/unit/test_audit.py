"""audit handler — appends a row per SNS event, tolerates partial failures."""

from __future__ import annotations

import json
from datetime import UTC, datetime


def _sqs_event(*payloads):
    return {
        "Records": [{"messageId": f"m{i}", "body": json.dumps(p)} for i, p in enumerate(payloads)]
    }


def test_writes_event_row(aws, audit_table, load_handler, lambda_ctx):
    mod = load_handler("audit")
    ts = datetime.now(UTC).isoformat()

    resp = mod.lambda_handler(
        _sqs_event(
            {
                "event_type": "file_uploaded",
                "upload_id": "u1",
                "emitted_at": ts,
                "content_type": "application/pdf",
                "details": {"size_bytes": 1024},
            }
        ),
        lambda_ctx,
    )
    assert resp == {"batchItemFailures": []}
    item = audit_table.get_item(Key={"upload_id": "u1", "event_timestamp": ts})["Item"]
    assert item["event_type"] == "file_uploaded"


def test_bad_message_becomes_batch_failure(aws, load_handler, lambda_ctx):
    mod = load_handler("audit")
    resp = mod.lambda_handler(
        {"Records": [{"messageId": "m1", "body": "not-json"}]},
        lambda_ctx,
    )
    assert resp["batchItemFailures"] == [{"itemIdentifier": "m1"}]
