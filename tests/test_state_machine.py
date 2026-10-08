"""Unit tests: processing state machine (PRD §30). Pure logic."""

import pytest

from app.pipeline.state_machine import (
    IllegalTransitionError,
    ProcessingStatus as S,
    assert_transition,
    can_transition,
    coerce,
    is_terminal,
)

HAPPY_PATH = [
    S.CAPTURED,
    S.QUEUED,
    S.RESOLVING_SOURCE,
    S.MEDIA_READY,
    S.TRANSCRIBING,
    S.ANALYZING_VISUALS,
    S.RUNNING_OCR,
    S.GENERATING_MEMORY,
    S.INDEXING,
    S.READY,
]


def test_happy_path_all_legal():
    for src, dst in zip(HAPPY_PATH, HAPPY_PATH[1:]):
        assert can_transition(src, dst), f"{src} -> {dst}"
        assert_transition(src, dst)  # must not raise


def test_illegal_jumps_raise():
    for src, dst in [
        (S.CAPTURED, S.READY),          # skipping the pipeline
        (S.QUEUED, S.TRANSCRIBING),     # skipping resolution
        (S.READY, S.TRANSCRIBING),      # backwards from terminal
        (S.CAPTURED, S.CAPTURED),       # self-loop not allowed
        (S.INDEXING, S.QUEUED),         # backwards mid-pipeline
    ]:
        assert not can_transition(src, dst), f"{src} -> {dst}"
        with pytest.raises(IllegalTransitionError):
            assert_transition(src, dst)


def test_retry_loop():
    assert can_transition(S.RESOLVING_SOURCE, S.FAILED_RETRYABLE)
    assert can_transition(S.FAILED_RETRYABLE, S.QUEUED)
    assert can_transition(S.QUEUED, S.RESOLVING_SOURCE)


def test_terminal_states_have_no_forward_exits():
    for status in (
        S.READY,
        S.METADATA_ONLY,
        S.SOURCE_UNAVAILABLE,
        S.SOURCE_REQUIRES_ACCESS,
        S.FAILED_PERMANENT,
        S.DELETED,
    ):
        assert is_terminal(status), status
    # terminal states may only go to QUEUED (explicit reprocess) or DELETED
    for status in (S.READY, S.METADATA_ONLY, S.FAILED_PERMANENT):
        assert can_transition(status, S.QUEUED)
        assert can_transition(status, S.DELETED)
        assert not can_transition(status, S.READY)


def test_deleted_is_absorbing():
    for status in S:
        if status is S.DELETED:
            continue
        assert not can_transition(S.DELETED, status)


def test_non_terminal_not_terminal():
    for status in (S.CAPTURED, S.QUEUED, S.TRANSCRIBING, S.FAILED_RETRYABLE):
        assert not is_terminal(status), status


def test_coerce_roundtrip_and_unknown():
    assert coerce("READY") is S.READY
    with pytest.raises(ValueError):
        coerce("BOGUS")


def test_queued_to_queued_is_idempotent_noop():
    # (iii) worker polling / retry loops may attempt QUEUED -> QUEUED when
    # the memory is already queued (e.g. a manual reprocess raced the
    # automatic retry). This is tolerated, not an IllegalTransitionError.
    assert can_transition(S.QUEUED, S.QUEUED)
    assert_transition(S.QUEUED, S.QUEUED)  # must not raise


def test_queued_self_loop_is_the_only_self_loop_allowed():
    # Genuinely illegal transitions still raise — other self-loops included.
    for status in (S.CAPTURED, S.READY, S.FAILED_RETRYABLE, S.METADATA_ONLY):
        assert not can_transition(status, status), status
        with pytest.raises(IllegalTransitionError):
            assert_transition(status, status)
