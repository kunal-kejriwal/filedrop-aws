"""Validation is pure — test it exhaustively, since every branch is cheap."""

from __future__ import annotations

import pytest

from shared.validation import (
    ValidationError,
    human_readable_size,
    parse_upload_request,
    validate_email,
    validate_filename,
    validate_size,
)


class TestEmail:
    @pytest.mark.parametrize(
        "value",
        ["a@b.co", "kunal.resolute+tag@gmail.com", "x.y-z@sub.example.org"],
    )
    def test_valid(self, value):
        assert validate_email(value) == value

    @pytest.mark.parametrize(
        "value",
        ["", "no-at-sign", "@nolocal.com", "spaces in@it.com", "a@b", None, 123],
    )
    def test_invalid(self, value):
        with pytest.raises(ValidationError):
            validate_email(value)

    def test_too_long(self):
        with pytest.raises(ValidationError):
            validate_email("a" * 250 + "@b.co")


class TestFilename:
    @pytest.mark.parametrize("name", ["report.pdf", "data-2026.csv", "photo 1.jpg"])
    def test_valid(self, name):
        assert validate_filename(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "../etc/passwd",
            "foo/bar.pdf",
            "foo\\bar.pdf",
            ".hidden.pdf",
            "noext",
            "bad.exe",
            "danger.php",
            "",
        ],
    )
    def test_rejected(self, name):
        with pytest.raises(ValidationError):
            validate_filename(name)


class TestSize:
    def test_upper_bound(self):
        assert validate_size(25 * 1024 * 1024) == 25 * 1024 * 1024

    @pytest.mark.parametrize("v", [0, -1, 25 * 1024 * 1024 + 1, "10", True, None])
    def test_bad(self, v):
        with pytest.raises(ValidationError):
            validate_size(v)


def test_parse_full_payload():
    req = parse_upload_request(
        {
            "email": "a@b.co",
            "filename": "x.pdf",
            "content_type": "application/pdf",
            "size_bytes": 10,
        }
    )
    assert req.email == "a@b.co"
    assert req.size_bytes == 10


def test_human_size():
    assert human_readable_size(0) == "0 B"
    assert human_readable_size(1024) == "1.0 KB"
    assert human_readable_size(5 * 1024 * 1024) == "5.0 MB"
