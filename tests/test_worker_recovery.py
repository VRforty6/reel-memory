"""Tests for worker orphaned-job recovery (A.2).

Root cause of the real-device report: a video upload sat in "Processing..."
for ~an hour because the worker died (or was never running) between the
RUNNING claim and job close. Nothing ever requeued the job — no startup
recovery, no reaper — so the memory stayed in its stage forever and the
Android card froze on Processing after its 90s poll budget.

Repo convention: fake session doubles, not a live database.

Covered:
  * startup recovery requeues an orphaned RUNNING job via legal transitions
    (mid-pipeline stage -> FAILED_RETRYABLE -> QUEUED), clears partial
    evidence, and resets job bookkeeping;
  * CAPTURED/QUEUED orphans go straight back to QUEUED;
  * exhausted attempt budget -> FAILED_PERMANENT with WORKER_LOST_JOB
    (never requeued forever);
  * terminal memory + RUNNING job -> job closed as DONE/FAILED, memory
    untouched (bookkeeping only);
  * the periodic reaper skips fresh jobs (age gate) and jobs claimed by
    this process, and reaps stale unclaimed ones.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.pipeline.failures import MAX_ATTEMPTS
from app.pipeline.state_machine import ProcessingStatus
from app.pipeline.worker import Worker, WorkerAlreadyRunningError


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _FakeQuery:
    """Chained query double: join/filter are no-ops, all() returns canned
    rows, delete() clears the canned rows and reports the count."""

    def __init__(self, rows):
        self._rows = list(rows)

    def join(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def all(self):
        return list(self._rows)

    def delete(self):
        n = len(self._rows)
        self._rows.clear()
        return n


class _FakeDB:
    """Routes db.query(...) by the first entity's class name, like the
    endpoint-test doubles in test_reprocess.py."""

    def __init__(self, running_rows=(), segments=(), embeddings=(), tags=()):
        self._running = _FakeQuery(running_rows)
        self._segments = _FakeQuery(segments)
        self._embeddings = _FakeQuery(embeddings)
        self._tags = _FakeQuery(tags)
        self.commits = 0

    def query(self, *entities, **k):
        name = entities[0].__name__ if entities else ""
        if name == "ProcessingJob":
            return self._running
        if name == "MemorySegment":
            return self._segments
        if name == "MemoryFrameEmbedding":
            return self._embeddings
        if name == "MemoryTag":
            return self._tags
        raise AssertionError(f"unexpected query entity: {name}")

    def commit(self):
        self.commits += 1


_UNSET = object()


def _job(status="RUNNING", attempt_count=1, started_at=_UNSET, job_id=None):
    return SimpleNamespace(
        id=job_id or uuid.uuid4(),
        memory_id=uuid.uuid4(),
        status=status,
        stage="ANALYZING_VISUALS",
        attempt_count=attempt_count,
        failure_code=None,
        failure_message=None,
        started_at=_utcnow() if started_at is _UNSET else started_at,
        finished_at=None,
    )


def _memory(status: ProcessingStatus):
    return SimpleNamespace(
        id=uuid.uuid4(),
        processing_status=status.value,
    )


def _worker():
    return Worker(session_factory=lambda: None)


# --- startup recovery --------------------------------------------------------

def test_recovery_requeues_orphaned_mid_pipeline_job():
    job = _job(started_at=_utcnow() - timedelta(hours=2))
    memory = _memory(ProcessingStatus.ANALYZING_VISUALS)
    db = _FakeDB(
        running_rows=[(job, memory)],
        segments=[object(), object()],
        embeddings=[object()],
        tags=[object()],
    )
    n = _worker().recover_orphaned_jobs(db)
    assert n == 1
    # Legal requeue path: FAILED_RETRYABLE -> QUEUED, then bookkeeping reset.
    assert memory.processing_status == ProcessingStatus.QUEUED.value
    assert job.status == "QUEUED"
    assert job.stage is None
    assert job.started_at is None
    assert job.finished_at is None
    assert job.failure_code is None
    assert job.failure_message is None
    # Partial evidence from the dead attempt was cleared (no duplicates on
    # the re-run).
    assert db._segments._rows == []
    assert db._embeddings._rows == []
    assert db._tags._rows == []


def test_recovery_queued_orphan_goes_straight_to_queued():
    job = _job(started_at=_utcnow() - timedelta(hours=2))
    memory = _memory(ProcessingStatus.QUEUED)
    db = _FakeDB(running_rows=[(job, memory)])
    n = _worker().recover_orphaned_jobs(db)
    assert n == 1
    assert memory.processing_status == ProcessingStatus.QUEUED.value
    assert job.status == "QUEUED"


def test_recovery_captured_orphan_goes_to_queued():
    job = _job(started_at=_utcnow() - timedelta(hours=2))
    memory = _memory(ProcessingStatus.CAPTURED)
    db = _FakeDB(running_rows=[(job, memory)])
    n = _worker().recover_orphaned_jobs(db)
    assert n == 1
    assert memory.processing_status == ProcessingStatus.QUEUED.value
    assert job.status == "QUEUED"


def test_recovery_exhausted_attempts_fails_permanent():
    job = _job(attempt_count=MAX_ATTEMPTS, started_at=_utcnow() - timedelta(hours=5))
    memory = _memory(ProcessingStatus.TRANSCRIBING)
    db = _FakeDB(running_rows=[(job, memory)])
    n = _worker().recover_orphaned_jobs(db)
    assert n == 1
    assert memory.processing_status == ProcessingStatus.FAILED_PERMANENT.value
    assert job.status == "FAILED"
    assert job.failure_code == "WORKER_LOST_JOB"
    assert job.finished_at is not None


def test_recovery_terminal_memory_only_closes_job():
    job = _job(started_at=_utcnow() - timedelta(hours=2))
    memory = _memory(ProcessingStatus.READY)
    db = _FakeDB(running_rows=[(job, memory)])
    n = _worker().recover_orphaned_jobs(db)
    assert n == 1
    assert job.status == "DONE"
    assert job.finished_at is not None
    # The memory itself is untouched — no reprocessing of finished work.
    assert memory.processing_status == ProcessingStatus.READY.value


def test_recovery_terminal_failed_memory_closes_job_as_failed():
    job = _job(started_at=_utcnow() - timedelta(hours=2))
    memory = _memory(ProcessingStatus.FAILED_PERMANENT)
    db = _FakeDB(running_rows=[(job, memory)])
    n = _worker().recover_orphaned_jobs(db)
    assert n == 1
    assert job.status == "FAILED"
    assert memory.processing_status == ProcessingStatus.FAILED_PERMANENT.value


def test_recovery_no_orphans_returns_zero():
    db = _FakeDB(running_rows=[])
    assert _worker().recover_orphaned_jobs(db) == 0


# --- periodic reaper ---------------------------------------------------------

def test_recovery_preserves_attempt_count_already_consumed_by_claim():
    # poll_once increments attempt_count in the same transaction as RUNNING.
    # Recovery must not double-count that crash.
    job = _job(attempt_count=1, started_at=_utcnow() - timedelta(hours=2))
    memory = _memory(ProcessingStatus.ANALYZING_VISUALS)
    db = _FakeDB(running_rows=[(job, memory)])
    worker = _worker()
    n = worker.recover_orphaned_jobs(db)
    assert n == 1
    assert job.status == "QUEUED"
    assert job.attempt_count == 1


def test_recovery_crash_loop_terminates_at_budget():
    # A claim that consumed the last attempt fails on recovery rather than
    # being queued for an impossible extra attempt.
    job = _job(attempt_count=MAX_ATTEMPTS - 1,
               started_at=_utcnow() - timedelta(hours=2))
    memory = _memory(ProcessingStatus.ANALYZING_VISUALS)
    db = _FakeDB(running_rows=[(job, memory)])
    worker = _worker()
    assert worker.recover_orphaned_jobs(db) == 1
    assert job.status == "QUEUED"
    assert job.attempt_count == MAX_ATTEMPTS - 1
    # Simulate the next atomic claim (QUEUED -> RUNNING + increment) and crash.
    job.status = "RUNNING"
    job.attempt_count += 1
    job.started_at = _utcnow() - timedelta(hours=2)
    db._running._rows.clear()
    db._running._rows.append((job, memory))
    assert worker.recover_orphaned_jobs(db) == 1
    assert job.status == "FAILED"
    assert job.failure_code == "WORKER_LOST_JOB"
    assert memory.processing_status == ProcessingStatus.FAILED_PERMANENT.value


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _LockDB:
    def __init__(self, acquired):
        self.acquired = acquired
        self.calls = []
        self.closed = False

    def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params))
        if "pg_try_advisory_lock" in sql:
            return _ScalarResult(self.acquired)
        if "pg_advisory_unlock" in sql:
            return _ScalarResult(True)
        if sql.strip() == "SELECT 1":
            return _ScalarResult(1)
        raise AssertionError(sql)

    def close(self):
        self.closed = True


def test_single_worker_lock_is_held_until_explicit_release():
    db = _LockDB(acquired=True)
    worker = Worker(lambda: db)
    worker._acquire_single_worker_lock()
    assert worker._lock_db is db
    assert not db.closed
    worker._assert_worker_lock_alive()
    worker._release_single_worker_lock()
    assert worker._lock_db is None
    assert db.closed
    assert any("pg_advisory_unlock" in sql for sql, _ in db.calls)


class _ClaimResult:
    def __init__(self, job):
        self.job = job

    def scalar_one_or_none(self):
        return self.job


class _ClaimDB:
    def __init__(self, job, memory, item):
        self.job = job
        self.memory = memory
        self.item = item
        self.commits = 0
        self.closed = False

    def execute(self, statement):
        return _ClaimResult(self.job)

    def get(self, model, key):
        return self.memory if model.__name__ == "Memory" else self.item

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def test_poll_claim_atomically_consumes_attempt(monkeypatch):
    job = _job(status="QUEUED", attempt_count=0)
    memory = _memory(ProcessingStatus.QUEUED)
    item = SimpleNamespace(id=uuid.uuid4())
    memory.source_item_id = item.id
    db = _ClaimDB(job, memory, item)
    observed = []
    worker = Worker(lambda: db)
    monkeypatch.setattr(
        worker,
        "_process_job",
        lambda _db, claimed, _memory, _item: observed.append(
            (claimed.status, claimed.attempt_count, db.commits)
        ),
    )

    assert worker.poll_once() is True
    assert observed == [("RUNNING", 1, 1)]
    assert db.closed


def test_second_worker_fails_without_running_recovery():
    db = _LockDB(acquired=False)
    worker = Worker(lambda: db)
    try:
        worker._acquire_single_worker_lock()
    except WorkerAlreadyRunningError:
        pass
    else:
        raise AssertionError("second worker unexpectedly acquired the lock")
    assert worker._lock_db is None
    assert db.closed


def test_reaper_skips_fresh_job():
    job = _job(started_at=_utcnow() - timedelta(minutes=10))
    memory = _memory(ProcessingStatus.ANALYZING_VISUALS)
    db = _FakeDB(running_rows=[(job, memory)])
    n = _worker().recover_orphaned_jobs(db, only_older_than_s=3600.0)
    assert n == 0
    assert job.status == "RUNNING"
    assert memory.processing_status == ProcessingStatus.ANALYZING_VISUALS.value


def test_reaper_skips_claimed_job():
    job_id = uuid.uuid4()
    job = _job(started_at=_utcnow() - timedelta(hours=2), job_id=job_id)
    memory = _memory(ProcessingStatus.ANALYZING_VISUALS)
    db = _FakeDB(running_rows=[(job, memory)])
    n = _worker().recover_orphaned_jobs(
        db, only_older_than_s=3600.0, exclude_job_ids={job_id}
    )
    assert n == 0
    assert job.status == "RUNNING"


def test_reaper_reaps_stale_unclaimed_job():
    job = _job(started_at=_utcnow() - timedelta(hours=2))
    memory = _memory(ProcessingStatus.RUNNING_OCR)
    db = _FakeDB(running_rows=[(job, memory)])
    n = _worker().recover_orphaned_jobs(
        db, only_older_than_s=3600.0, exclude_job_ids=set()
    )
    assert n == 1
    assert job.status == "QUEUED"
    assert memory.processing_status == ProcessingStatus.QUEUED.value


def test_reaper_recovers_job_with_missing_started_at():
    # started_at None: claimed by an ancient worker build — treat as stale.
    job = _job(started_at=None)
    memory = _memory(ProcessingStatus.MEDIA_READY)
    db = _FakeDB(running_rows=[(job, memory)])
    n = _worker().recover_orphaned_jobs(db, only_older_than_s=3600.0)
    assert n == 1
    assert job.status == "QUEUED"
