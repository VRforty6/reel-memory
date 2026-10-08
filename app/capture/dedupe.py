"""Duplicate handling — PRD §13.

The primary source identity is (platform, platform_item_id). A duplicate share
must NOT trigger new AI processing; it only bumps save_count / last_saved_at.
Reprocessing is allowed only when the previous attempt failed, the processing
version changed, or it was explicitly requested.
"""

from __future__ import annotations

# Memory processing statuses that represent a failed/unresolved attempt and may
# be retried without an explicit user request.
#
# FAILED_RETRYABLE is deliberately NOT here: it is owned by the worker's
# automatic retry loop (state machine FAILED_RETRYABLE -> QUEUED, PRD §32).
# A duplicate share must not open a second reprocess path for it — the
# worker already owns the retry.
REPROCESSABLE_STATUSES = frozenset(
    {
        "FAILED_PERMANENT",
        "SOURCE_UNAVAILABLE",
        "SOURCE_REQUIRES_ACCESS",
    }
)


def source_identity(platform: str, platform_item_id: str) -> tuple[str, str]:
    """Canonical duplicate key for a source item."""
    return (platform, platform_item_id)


def should_reprocess(
    processing_status: str,
    stored_version: str | None,
    current_version: str,
    explicit: bool = False,
) -> bool:
    """Decide whether a duplicate share (or reprocess call) may run AI again."""
    if explicit:
        return True
    if stored_version != current_version:
        # Processing pipeline changed -> previous results are stale.
        return True
    return processing_status in REPROCESSABLE_STATUSES
