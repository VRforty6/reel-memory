"""Unit tests: duplicate handling (PRD §13). Pure logic."""

from app.capture.dedupe import should_reprocess, source_identity


def test_source_identity_is_platform_tuple():
    assert source_identity("instagram", "AbC123") == ("instagram", "AbC123")
    assert source_identity("instagram", "AbC123") != source_identity("tiktok", "AbC123")


def test_ready_same_version_no_reprocess():
    assert should_reprocess("READY", "v0.1.0", "v0.1.0") is False


def test_version_bump_triggers_reprocess():
    assert should_reprocess("READY", "v0.1.0", "v0.2.0") is True
    assert should_reprocess("METADATA_ONLY", "v0.1.0", "v0.2.0") is True


def test_failed_states_reprocess_without_version_change():
    for status in (
        "FAILED_PERMANENT",
        "SOURCE_UNAVAILABLE",
        "SOURCE_REQUIRES_ACCESS",
    ):
        assert should_reprocess(status, "v0.1.0", "v0.1.0") is True, status


def test_failed_retryable_is_worker_owned_not_dedupe_reprocessable():
    # FAILED_RETRYABLE is owned by the worker's automatic retry loop
    # (FAILED_RETRYABLE -> QUEUED); a duplicate share must not open a
    # second reprocess path for it.
    assert should_reprocess("FAILED_RETRYABLE", "v0.1.0", "v0.1.0") is False


def test_settled_good_states_do_not_reprocess():
    for status in ("READY", "METADATA_ONLY"):
        assert should_reprocess(status, "v0.1.0", "v0.1.0") is False, status


def test_explicit_always_reprocesses():
    assert should_reprocess("READY", "v0.1.0", "v0.1.0", explicit=True) is True
