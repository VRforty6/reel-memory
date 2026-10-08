"""Unit tests: failure codes + retry policy (PRD §31/§32). Pure logic."""

from app.pipeline.failures import BACKOFF_CAP_S, MAX_ATTEMPTS, FailureCode, backoff_seconds


def test_all_prd_codes_present():
    names = {c.value for c in FailureCode}
    assert names == {
        "INVALID_URL",
        "UNSUPPORTED_SOURCE",
        "SOURCE_DELETED",
        "SOURCE_PRIVATE",
        "SOURCE_LOGIN_REQUIRED",
        "SOURCE_RATE_LIMITED",
        "SOURCE_RESOLUTION_FAILED",
        "MEDIA_DOWNLOAD_FAILED",
        "MEDIA_DECODE_FAILED",
        "TRANSCRIPTION_FAILED",
        "VISION_FAILED",
        "OCR_FAILED",
        "EMBEDDING_FAILED",
        "VISUAL_INDEX_FAILED",
        "DATABASE_FAILED",
        "INDEXING_FAILED",
    }


def test_retryable_flags():
    retryable = {
        "SOURCE_RATE_LIMITED",
        "SOURCE_RESOLUTION_FAILED",
        "MEDIA_DOWNLOAD_FAILED",
        "TRANSCRIPTION_FAILED",
        "VISION_FAILED",
        "OCR_FAILED",
        "EMBEDDING_FAILED",
        "VISUAL_INDEX_FAILED",
        "DATABASE_FAILED",
        "INDEXING_FAILED",
    }
    for code in FailureCode:
        assert code.retryable == (code.value in retryable), code.value


def test_permanent_codes_not_retryable():
    for name in (
        "INVALID_URL",
        "UNSUPPORTED_SOURCE",
        "SOURCE_DELETED",
        "SOURCE_PRIVATE",
        "SOURCE_LOGIN_REQUIRED",
        "MEDIA_DECODE_FAILED",
    ):
        assert FailureCode(name).retryable is False


def test_backoff_is_bounded_exponential():
    assert backoff_seconds(1) == 2.0
    assert backoff_seconds(2) == 4.0
    assert backoff_seconds(3) == 8.0
    assert backoff_seconds(100) == BACKOFF_CAP_S  # never unbounded


def test_max_attempts_is_three():
    assert MAX_ATTEMPTS == 3
