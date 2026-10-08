"""Processing state machine — PRD §30.

Legal lifecycle:

    CAPTURED -> QUEUED -> RESOLVING_SOURCE -> MEDIA_READY -> TRANSCRIBING
      -> ANALYZING_VISUALS -> RUNNING_OCR -> GENERATING_MEMORY -> INDEXING -> READY

Terminal states: READY, METADATA_ONLY, SOURCE_UNAVAILABLE, SOURCE_REQUIRES_ACCESS,
FAILED_PERMANENT, DELETED. FAILED_RETRYABLE may return to QUEUED for another
attempt. QUEUED -> QUEUED is an idempotent no-op (tolerated, never raises).
Any non-DELETED state may transition to DELETED (user deletion, SEC-007);
reprocess (explicit) may return terminal states to QUEUED.
"""

from __future__ import annotations

from enum import Enum


class ProcessingStatus(str, Enum):
    CAPTURED = "CAPTURED"
    QUEUED = "QUEUED"
    RESOLVING_SOURCE = "RESOLVING_SOURCE"
    MEDIA_READY = "MEDIA_READY"
    TRANSCRIBING = "TRANSCRIBING"
    ANALYZING_VISUALS = "ANALYZING_VISUALS"
    RUNNING_OCR = "RUNNING_OCR"
    GENERATING_MEMORY = "GENERATING_MEMORY"
    INDEXING = "INDEXING"
    READY = "READY"
    METADATA_ONLY = "METADATA_ONLY"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    SOURCE_REQUIRES_ACCESS = "SOURCE_REQUIRES_ACCESS"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    DELETED = "DELETED"


_TERMINAL = frozenset(
    {
        ProcessingStatus.READY,
        ProcessingStatus.METADATA_ONLY,
        ProcessingStatus.SOURCE_UNAVAILABLE,
        ProcessingStatus.SOURCE_REQUIRES_ACCESS,
        ProcessingStatus.FAILED_PERMANENT,
        ProcessingStatus.DELETED,
    }
)

_NON_TERMINAL_FAILURE_EXITS = frozenset(
    {
        ProcessingStatus.FAILED_RETRYABLE,
        ProcessingStatus.FAILED_PERMANENT,
    }
)

_TRANSITIONS: dict[ProcessingStatus, frozenset[ProcessingStatus]] = {
    ProcessingStatus.CAPTURED: frozenset({ProcessingStatus.QUEUED}),
    ProcessingStatus.QUEUED: frozenset(
        {
            ProcessingStatus.RESOLVING_SOURCE,
            ProcessingStatus.DELETED,
            # Idempotent no-op: worker polling / retry loops may attempt
            # QUEUED -> QUEUED when the memory is already QUEUED (e.g. a
            # manual reprocess raced the automatic retry and the worker's
            # backoff elapsed meanwhile). Tolerate it instead of crashing
            # the poll with IllegalTransitionError. Genuinely illegal
            # transitions still raise.
            ProcessingStatus.QUEUED,
        }
    ),
    ProcessingStatus.RESOLVING_SOURCE: frozenset(
        {
            ProcessingStatus.MEDIA_READY,
            ProcessingStatus.METADATA_ONLY,
            ProcessingStatus.SOURCE_UNAVAILABLE,
            ProcessingStatus.SOURCE_REQUIRES_ACCESS,
            ProcessingStatus.FAILED_RETRYABLE,
            ProcessingStatus.FAILED_PERMANENT,
        }
    ),
    ProcessingStatus.MEDIA_READY: frozenset(
        {
            ProcessingStatus.TRANSCRIBING,
            # Image uploads (and albums of images) have no audio track, so
            # they legitimately skip TRANSCRIBING. Without this the image
            # path died here with IllegalTransitionError.
            ProcessingStatus.ANALYZING_VISUALS,
            ProcessingStatus.FAILED_RETRYABLE,
            ProcessingStatus.FAILED_PERMANENT,
        }
    ),
    ProcessingStatus.TRANSCRIBING: frozenset(
        {
            ProcessingStatus.ANALYZING_VISUALS,
            ProcessingStatus.FAILED_RETRYABLE,
            ProcessingStatus.FAILED_PERMANENT,
        }
    ),
    ProcessingStatus.ANALYZING_VISUALS: frozenset(
        {
            ProcessingStatus.RUNNING_OCR,
            ProcessingStatus.FAILED_RETRYABLE,
            ProcessingStatus.FAILED_PERMANENT,
        }
    ),
    ProcessingStatus.RUNNING_OCR: frozenset(
        {
            ProcessingStatus.GENERATING_MEMORY,
            ProcessingStatus.FAILED_RETRYABLE,
            ProcessingStatus.FAILED_PERMANENT,
        }
    ),
    ProcessingStatus.GENERATING_MEMORY: frozenset(
        {
            ProcessingStatus.INDEXING,
            ProcessingStatus.FAILED_RETRYABLE,
            ProcessingStatus.FAILED_PERMANENT,
        }
    ),
    ProcessingStatus.INDEXING: frozenset(
        {
            ProcessingStatus.READY,
            ProcessingStatus.FAILED_RETRYABLE,
            ProcessingStatus.FAILED_PERMANENT,
        }
    ),
    ProcessingStatus.FAILED_RETRYABLE: frozenset(
        {ProcessingStatus.QUEUED, ProcessingStatus.DELETED}
    ),
    # Terminal data states: reprocess (explicit) -> QUEUED, or user delete.
    ProcessingStatus.READY: frozenset(
        {ProcessingStatus.QUEUED, ProcessingStatus.DELETED}
    ),
    ProcessingStatus.METADATA_ONLY: frozenset(
        {ProcessingStatus.QUEUED, ProcessingStatus.DELETED}
    ),
    ProcessingStatus.SOURCE_UNAVAILABLE: frozenset(
        {ProcessingStatus.QUEUED, ProcessingStatus.DELETED}
    ),
    ProcessingStatus.SOURCE_REQUIRES_ACCESS: frozenset(
        {ProcessingStatus.QUEUED, ProcessingStatus.DELETED}
    ),
    ProcessingStatus.FAILED_PERMANENT: frozenset(
        {ProcessingStatus.QUEUED, ProcessingStatus.DELETED}
    ),
    ProcessingStatus.DELETED: frozenset(),
}


class IllegalTransitionError(ValueError):
    def __init__(self, src: ProcessingStatus, dst: ProcessingStatus) -> None:
        super().__init__(f"illegal processing transition: {src.value} -> {dst.value}")
        self.src = src
        self.dst = dst


def is_terminal(status: ProcessingStatus) -> bool:
    return status in _TERMINAL


def can_transition(src: ProcessingStatus, dst: ProcessingStatus) -> bool:
    """Guard: is src -> dst a legal transition?"""
    return dst in _TRANSITIONS.get(src, frozenset())


def assert_transition(src: ProcessingStatus, dst: ProcessingStatus) -> None:
    """Raise IllegalTransitionError if src -> dst is not allowed."""
    if not can_transition(src, dst):
        raise IllegalTransitionError(src, dst)


def coerce(value: str) -> ProcessingStatus:
    """Parse a stored status string, raising ValueError on unknown values."""
    return ProcessingStatus(value)
