"""Failure classification — PRD §31 and retry policy — PRD §32.

Every failure gets a structured code (never just "something went wrong"), each
code declares whether it is retryable, and retries use bounded exponential
backoff with a hard cap of MAX_ATTEMPTS attempts.
"""

from __future__ import annotations

from enum import Enum


class FailureCode(str, Enum):
    INVALID_URL = "INVALID_URL"
    UNSUPPORTED_SOURCE = "UNSUPPORTED_SOURCE"
    SOURCE_DELETED = "SOURCE_DELETED"
    SOURCE_PRIVATE = "SOURCE_PRIVATE"
    SOURCE_LOGIN_REQUIRED = "SOURCE_LOGIN_REQUIRED"
    SOURCE_RATE_LIMITED = "SOURCE_RATE_LIMITED"
    SOURCE_RESOLUTION_FAILED = "SOURCE_RESOLUTION_FAILED"
    MEDIA_DOWNLOAD_FAILED = "MEDIA_DOWNLOAD_FAILED"
    MEDIA_DECODE_FAILED = "MEDIA_DECODE_FAILED"
    TRANSCRIPTION_FAILED = "TRANSCRIPTION_FAILED"
    VISION_FAILED = "VISION_FAILED"
    OCR_FAILED = "OCR_FAILED"
    EMBEDDING_FAILED = "EMBEDDING_FAILED"
    VISUAL_INDEX_FAILED = "VISUAL_INDEX_FAILED"
    DATABASE_FAILED = "DATABASE_FAILED"
    INDEXING_FAILED = "INDEXING_FAILED"

    @property
    def retryable(self) -> bool:
        """Whether a failure with this code may be retried with backoff."""
        return self in _RETRYABLE_CODES


_RETRYABLE_CODES = frozenset(
    {
        FailureCode.SOURCE_RATE_LIMITED,
        FailureCode.SOURCE_RESOLUTION_FAILED,
        FailureCode.MEDIA_DOWNLOAD_FAILED,
        FailureCode.TRANSCRIPTION_FAILED,
        FailureCode.VISION_FAILED,
        FailureCode.OCR_FAILED,
        FailureCode.EMBEDDING_FAILED,
        FailureCode.VISUAL_INDEX_FAILED,
        FailureCode.DATABASE_FAILED,
        FailureCode.INDEXING_FAILED,
    }
)

MAX_ATTEMPTS = 3  # PRD §32: bounded — never retry indefinitely.
BACKOFF_CAP_S = 60.0


def backoff_seconds(completed_attempts: int) -> float:
    """Bounded exponential backoff after `completed_attempts` failed attempts.

    attempt 1 -> 2s, attempt 2 -> 4s, attempt 3 -> 8s, capped at BACKOFF_CAP_S.
    """
    return min(2.0 ** max(completed_attempts, 1), BACKOFF_CAP_S)
