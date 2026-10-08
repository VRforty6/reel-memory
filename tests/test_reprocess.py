"""Tests for POST /v1/memories/{id}/reprocess policy (ITEM 2b/2c).

Repo convention: endpoint wiring is covered by calling the endpoint function
directly with a fake session double, not a live database.

Policy under test:
  * FAILED_RETRYABLE is owned by the worker's automatic retry loop — manual
    reprocess returns 409 for it (never steals the retry).
  * FAILED_PERMANENT (terminal) may be manually reprocessed.
  * QUEUED/processing states return 409.
  * QUEUED -> QUEUED is an idempotent no-op in the state machine, so a
    worker poll racing a manual reprocess never crashes with
    IllegalTransitionError and exactly one owner wins.
"""

from __future__ import annotations

import threading
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.memories import reprocess_memory
from app.pipeline.state_machine import (
    IllegalTransitionError,
    ProcessingStatus,
    assert_transition,
    coerce,
)
from app.pipeline.worker import Worker


class _FakeQuery:
    def __init__(self, memory):
        self._memory = memory

    def join(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def first(self):
        return self._memory


class _FakeDB:
    def __init__(self, memory):
        self._memory = memory
        self.added = []

    def query(self, *a, **k):
        return _FakeQuery(self._memory)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass


def _memory_with(status: ProcessingStatus) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        processing_status=status.value,
        processing_version="v0.1.0",
    )


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4())


# --- (i) / (ii): reprocess policy --------------------------------------------


def test_reprocess_failed_retryable_returns_409():
    # (i) FAILED_RETRYABLE belongs to the worker's automatic retry loop;
    # a manual reprocess must not steal it.
    mem = _memory_with(ProcessingStatus.FAILED_RETRYABLE)
    db = _FakeDB(mem)
    with pytest.raises(HTTPException) as exc:
        reprocess_memory(mem.id, db=db, user=_user())
    assert exc.value.status_code == 409
    assert db.added == []  # no second job created
    assert mem.processing_status == ProcessingStatus.FAILED_RETRYABLE.value


def test_reprocess_failed_permanent_is_allowed():
    # (ii) terminal failure states may be manually reprocessed.
    mem = _memory_with(ProcessingStatus.FAILED_PERMANENT)
    db = _FakeDB(mem)
    resp = reprocess_memory(mem.id, db=db, user=_user())
    assert resp.processing_status == ProcessingStatus.QUEUED.value
    assert mem.processing_status == ProcessingStatus.QUEUED.value
    assert len(db.added) == 1
    assert db.added[0].memory_id == mem.id
    assert db.added[0].status == "QUEUED"


def test_reprocess_queued_and_processing_states_return_409():
    # QUEUED and in-flight pipeline states are still processing: 409.
    for status in (
        ProcessingStatus.QUEUED,
        ProcessingStatus.CAPTURED,
        ProcessingStatus.RESOLVING_SOURCE,
        ProcessingStatus.TRANSCRIBING,
    ):
        mem = _memory_with(status)
        db = _FakeDB(mem)
        with pytest.raises(HTTPException) as exc:
            reprocess_memory(mem.id, db=db, user=_user())
        assert exc.value.status_code == 409, status
        assert db.added == []


# --- (iv): worker retry vs manual reprocess race ------------------------------


def test_worker_retry_vs_manual_reprocess_race():
    """(iv) The worker's retry loop moves FAILED_RETRYABLE -> QUEUED while a
    manual POST /reprocess races it (both directions of the interleaving).

    No IllegalTransitionError may escape from either side, and exactly one
    owner wins: the worker's retry owns the attempt; the manual reprocess
    gets 409 and creates no second job.
    """
    for _ in range(50):
        mem = _memory_with(ProcessingStatus.FAILED_RETRYABLE)
        db = _FakeDB(mem)  # shared by both racers, like the real DB row
        worker = Worker(session_factory=lambda: db)
        wjob = SimpleNamespace(
            status="RUNNING",
            stage=ProcessingStatus.FAILED_RETRYABLE.value,
            attempt_count=1,
            failure_code="SOURCE_RESOLUTION_FAILED",
            failure_message="boom",
            started_at=None,
            finished_at=None,
        )
        barrier = threading.Barrier(2)
        errors: list = []
        endpoint_outcome: dict = {}

        def worker_side():
            # Mirrors the worker retry loop's requeue step, using the real
            # Worker._transition (state machine guard + status write).
            try:
                barrier.wait()
                worker._transition(db, mem, wjob, ProcessingStatus.QUEUED)
            except Exception as e:  # noqa: BLE001 - collected, asserted below
                errors.append(e)

        def endpoint_side():
            # The real endpoint function, racing the worker.
            try:
                barrier.wait()
                reprocess_memory(mem.id, db=db, user=_user())
                endpoint_outcome["result"] = "allowed"
            except HTTPException as e:
                endpoint_outcome["result"] = f"http_{e.status_code}"
            except Exception as e:  # noqa: BLE001 - collected, asserted below
                errors.append(e)

        t1 = threading.Thread(target=worker_side)
        t2 = threading.Thread(target=endpoint_side)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not any(
            isinstance(e, IllegalTransitionError) for e in errors
        ), errors
        assert not errors, errors
        assert mem.processing_status == ProcessingStatus.QUEUED.value
        assert wjob.stage == ProcessingStatus.QUEUED.value
        # Exactly one owner: the worker's retry. The manual attempt 409s
        # whether it reads FAILED_RETRYABLE or the already-QUEUED row.
        assert endpoint_outcome["result"] == "http_409"
        assert db.added == []  # no duplicate job from the manual attempt


def test_queued_to_queued_transition_used_by_worker_never_raises():
    # The state-machine half of the race: even if both sides reach the
    # transition, QUEUED -> QUEUED is tolerated.
    assert_transition(ProcessingStatus.QUEUED, ProcessingStatus.QUEUED)
    assert coerce("QUEUED") is ProcessingStatus.QUEUED
