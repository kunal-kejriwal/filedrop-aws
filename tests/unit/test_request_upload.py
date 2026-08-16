"""request_upload handler — validation, DDB write, presigned-URL shape."""

from __future__ import annotations

import json


def _body(**overrides):
    payload = {
        "email": "kunal@example.com",
        "filename": "notes.pdf",
        "content_type": "application/pdf",
        "size_bytes": 1024,
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_happy_path_returns_201_and_writes_ddb(aws, uploads_table, load_handler, lambda_ctx):
    mod = load_handler("request_upload")
    resp = mod.lambda_handler({"body": _body()}, lambda_ctx)
    assert resp["statusCode"] == 201

    body = json.loads(resp["body"])
    assert "upload_url" in body and body["upload_url"].startswith("http")
    assert body["method"] == "PUT"
    assert body["required_headers"]["Content-Type"] == "application/pdf"

    item = uploads_table.get_item(Key={"upload_id": body["upload_id"]})["Item"]
    assert item["status"] == "AWAITING_UPLOAD"
    assert item["email"] == "kunal@example.com"
    assert int(item["ttl"]) > 0


def test_validation_failure_returns_400(aws, load_handler, lambda_ctx):
    mod = load_handler("request_upload")
    resp = mod.lambda_handler({"body": _body(filename="../evil.pdf")}, lambda_ctx)
    assert resp["statusCode"] == 400
    assert "filename" in json.loads(resp["body"])["error"]


def test_malformed_json_returns_400(aws, load_handler, lambda_ctx):
    mod = load_handler("request_upload")
    resp = mod.lambda_handler({"body": "not-json"}, lambda_ctx)
    assert resp["statusCode"] == 400


def test_missing_body_returns_400(aws, load_handler, lambda_ctx):
    mod = load_handler("request_upload")
    resp = mod.lambda_handler({}, lambda_ctx)
    assert resp["statusCode"] == 400
