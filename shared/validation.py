"""Pure validation helpers for upload-slot requests.

Each validator returns the normalized value on success or raises
``ValidationError`` with a user-safe message. Handlers convert those into
400 responses; nothing here should ever raise a bare exception.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from shared.models import ALLOWED_EXTENSIONS, MAX_SIZE_BYTES

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._\- ]{1,120}$")

MAX_EMAIL_LENGTH = 254


class ValidationError(ValueError):
    """User-facing validation failure. Message is safe to return in a 400."""


@dataclass(frozen=True)
class UploadRequest:
    email: str
    filename: str
    content_type: str
    size_bytes: int


def validate_email(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValidationError("email must be a string")
    email = raw.strip()
    if not email or len(email) > MAX_EMAIL_LENGTH:
        raise ValidationError("email length invalid")
    if not _EMAIL_RE.match(email):
        raise ValidationError("email format invalid")
    return email


def validate_filename(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValidationError("filename must be a string")
    name = raw.strip()
    if not name:
        raise ValidationError("filename required")
    # Path-traversal guards: reject anything that could escape the intended prefix.
    if "/" in name or "\\" in name or ".." in name or name.startswith("."):
        raise ValidationError("filename contains disallowed characters")
    if not _FILENAME_RE.match(name):
        raise ValidationError("filename contains disallowed characters")
    if "." not in name:
        raise ValidationError("filename must include an extension")
    ext = name.rsplit(".", 1)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(
            f"extension .{ext} not allowed; allowed: {sorted(ALLOWED_EXTENSIONS)}"
        )
    return name


def validate_content_type(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError("content_type required")
    ct = raw.strip()
    # Bound length and reject control characters; format is otherwise opaque to us.
    if len(ct) > 127 or any(ord(c) < 0x20 for c in ct):
        raise ValidationError("content_type invalid")
    return ct


def validate_size(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValidationError("size_bytes must be an integer")
    if raw <= 0:
        raise ValidationError("size_bytes must be positive")
    if raw > MAX_SIZE_BYTES:
        raise ValidationError(f"size_bytes exceeds max of {MAX_SIZE_BYTES}")
    return raw


def parse_upload_request(payload: dict) -> UploadRequest:
    """Validate and normalize an upload-request payload.

    Raises ValidationError on the first failed field so the caller can
    surface a single 400 with a specific message.
    """
    return UploadRequest(
        email=validate_email(payload.get("email")),
        filename=validate_filename(payload.get("filename")),
        content_type=validate_content_type(payload.get("content_type")),
        size_bytes=validate_size(payload.get("size_bytes")),
    )


def human_readable_size(n: int) -> str:
    step = 1024.0
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < step:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= step
    return f"{size:.1f} TB"
