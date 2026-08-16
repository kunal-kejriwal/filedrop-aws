"""End-to-end smoke test against a deployed Filedrop stack.

Skipped unless ``FILEDROP_API_URL`` is set. Meant to be run from CI *after*
a deploy, or locally with the URL exported.
"""

from __future__ import annotations

import os
import uuid

import pytest
import requests


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("FILEDROP_API_URL"),
    reason="FILEDROP_API_URL not set — skipping live smoke test",
)
def test_request_upload_smoke():
    api = os.environ["FILEDROP_API_URL"].rstrip("/")
    email = os.environ.get("TEST_EMAIL", "smoke@example.com")

    resp = requests.post(
        f"{api}/uploads",
        json={
            "email": email,
            "filename": f"smoke-{uuid.uuid4().hex[:6]}.txt",
            "content_type": "text/plain",
            "size_bytes": 12,
        },
        timeout=10,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["upload_url"].startswith("https://")
    assert body["method"] == "PUT"


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("FILEDROP_API_URL"),
    reason="FILEDROP_API_URL not set — skipping live smoke test",
)
def test_rejects_bad_extension():
    api = os.environ["FILEDROP_API_URL"].rstrip("/")
    resp = requests.post(
        f"{api}/uploads",
        json={
            "email": "x@example.com",
            "filename": "evil.exe",
            "content_type": "application/octet-stream",
            "size_bytes": 100,
        },
        timeout=10,
    )
    assert resp.status_code == 400
