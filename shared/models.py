"""Constants and status enums shared across handlers.

Kept as module-level constants (not env vars) because they define the
protocol contract between Lambdas — changing them is a code change, not
a config change.
"""

from __future__ import annotations

from enum import StrEnum

MAX_SIZE_BYTES: int = 25 * 1024 * 1024
PRESIGNED_PUT_EXPIRY_SECONDS: int = 15 * 60
PRESIGNED_GET_EXPIRY_SECONDS: int = 24 * 60 * 60
TTL_HOURS: int = 48

ALLOWED_EXTENSIONS: frozenset[str] = frozenset({"pdf", "txt", "md", "png", "jpg", "zip", "csv"})

UPLOAD_KEY_PREFIX: str = "uploads/"


class UploadStatus(StrEnum):
    AWAITING_UPLOAD = "AWAITING_UPLOAD"
    UPLOADED = "UPLOADED"
    NOTIFIED = "NOTIFIED"
    REJECTED = "REJECTED"


class EventType(StrEnum):
    FILE_UPLOADED = "file_uploaded"
    FILE_REJECTED = "file_rejected"
