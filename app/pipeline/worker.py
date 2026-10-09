"""Background processing worker — PRD §17 pipeline, §30-32 failures, §40 cleanup.

Polls processing_jobs with SELECT ... FOR UPDATE SKIP LOCKED so multiple
workers can run without double-processing. Advances each memory through the
state machine, maps exceptions to FailureCode, retries with bounded
exponential backoff (max 3 attempts), and always deletes temp media in a
finally block (PRD §8.6: raw video is a temporary processing artifact).
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Collection, Optional

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.capture.canonicalize import CanonicalURL
from app.config import settings
from app.models import (
    Memory,
    MemoryFrameEmbedding,
    MemorySegment,
    MemoryTag,
    ProcessingJob,
    SourceItem,
)
from app.pipeline.failures import MAX_ATTEMPTS, FailureCode, backoff_seconds
from app.pipeline import media as media_decode
from app.pipeline import frame_index as visual_frame_index
from app.pipeline.frame_index import FrameSample
from app.pipeline.visual import (
    VisualProviderError,
    VisualProviderNotConfiguredError,
    build_visual_provider,
)
from app.pipeline.memory_builder import build_memory
from app.pipeline.providers import (
    PipelineEvidence,
    ProviderError,
    ProviderNotConfiguredError,
    Providers,
    TranscriptSegment,
    UnconfiguredVisionProvider,
)
from app.pipeline.state_machine import (
    ProcessingStatus,
    assert_transition,
    coerce,
    is_terminal,
)
from app.sources.base import (
    AlbumMedia,
    ArticleContent,
    AuthenticationRequired,
    MetadataOnly,
    ResolvedMedia,
    RetryableFailure,
    SourceMetadata,
    TranscriptContent,
    Unavailable,
    Unsupported,
)
from app.sources.registry import get_adapter
from app.taxonomy import categorize_memory

log = logging.getLogger("reel-memory.worker")

# PostgreSQL session-level advisory lock namespace for the one allowed worker.
# The lock lives on a dedicated Session for the lifetime of run_forever() and
# is released automatically by PostgreSQL if the process/connection dies.
WORKER_ADVISORY_LOCK_ID = 0x52454C4D  # ASCII-ish "RELM"

# Screenshot/photo uploads: no audio track — the image itself is the frame.
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})


def _media_kind_for_media(suffix: str) -> str:
    """Pure media-kind classification for the Milestone A UX filters.

    'image' for photo/screenshot uploads, 'video' otherwise. Album and
    article paths set their kind literally at their call sites.
    """
    return "image" if suffix.lower() in _IMAGE_SUFFIXES else "video"


class StageError(Exception):
    """A pipeline stage failed with a classified FailureCode.

    retryable_override lets the exception source narrow a broadly retryable
    taxonomy code. Example: TRANSCRIPTION_FAILED is normally retryable for a
    transient provider outage, but "provider not configured" can never heal
    by sleeping and retrying.
    """

    def __init__(
        self, code: FailureCode, message: str, *, retryable_override: bool | None = None
    ) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.message = message
        self.retryable_override = retryable_override

    @property
    def retryable(self) -> bool:
        return self.code.retryable if self.retryable_override is None else self.retryable_override


class TerminalState(Exception):
    """Raise to land the memory in a classified terminal state (not a crash)."""

    def __init__(self, status: ProcessingStatus, code: Optional[str], message: str) -> None:
        super().__init__(f"{status.value}: {message}")
        self.status = status
        self.code = code
        self.message = message


class WorkerAlreadyRunningError(RuntimeError):
    """Raised when another process already owns the database worker lock."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _lead_summary(text: str, limit: int = 2000) -> str:
    """Honest truncation for article summaries: cut at a sentence boundary
    (else word boundary) near `limit` chars and mark the cut with an
    ellipsis. Nothing is invented."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "! ", "? ", "\n"):
        idx = cut.rfind(sep)
        if idx > limit * 0.5:
            return cut[: idx + 1].strip()
    idx = cut.rfind(" ")
    return ((cut[:idx] if idx > 0 else cut).strip() + "…")


class Worker:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        providers: Optional[Providers] = None,
    ) -> None:
        self._session_factory = session_factory
        self.providers = providers or Providers()
        self._stage_t0: Optional[float] = None
        self._lock_db: Optional[Session] = None
        # Job ids currently being processed by THIS worker process. The
        # stale-job reaper never touches these: a slow-but-alive job must
        # not be requeued underneath its owner.
        self._claimed: set[uuid.UUID] = set()

    # -- main loop ------------------------------------------------------

    def run_forever(self) -> None:
        self._acquire_single_worker_lock()
        try:
            log.info("worker starting (poll every %.1fs)", settings.worker_poll_interval_s)
            # Recovery is safe only after the global lock is owned: no healthy
            # worker can still be processing one of these RUNNING rows.
            self._recover_at_startup()
            last_reap = time.monotonic()
            while True:
                # If PostgreSQL drops the dedicated lock connection, exit.
                # Continuing would permit a replacement worker to acquire
                # the lock while this process still handles jobs.
                self._assert_worker_lock_alive()
                try:
                    worked = self.poll_once()
                except Exception:
                    log.exception("worker poll crashed; continuing")
                    worked = False
                if time.monotonic() - last_reap >= settings.worker_reap_interval_s:
                    last_reap = time.monotonic()
                    try:
                        self._reap_stale_jobs()
                    except Exception:
                        log.exception("stale-job reaper crashed; continuing")
                if not worked:
                    time.sleep(settings.worker_poll_interval_s)
        finally:
            self._release_single_worker_lock()

    def _acquire_single_worker_lock(self) -> None:
        """Own the database-wide worker lock or fail before recovery/polling.

        A dedicated session is intentionally kept open. PostgreSQL advisory
        locks are connection-scoped, so a crash releases the lock while a
        second live worker receives False and exits without touching jobs.
        """
        if self._lock_db is not None:
            return
        db = self._session_factory()
        try:
            acquired = bool(
                db.execute(
                    text("SELECT pg_try_advisory_lock(:lock_id)"),
                    {"lock_id": WORKER_ADVISORY_LOCK_ID},
                ).scalar_one()
            )
        except Exception:
            db.close()
            raise
        if not acquired:
            db.close()
            raise WorkerAlreadyRunningError(
                "another Reel Memory worker already owns the database lock"
            )
        self._lock_db = db
        log.info("acquired single-worker database lock")

    def _assert_worker_lock_alive(self) -> None:
        if self._lock_db is None:
            raise RuntimeError("worker database lock is not held")
        self._lock_db.execute(text("SELECT 1")).scalar_one()

    def _release_single_worker_lock(self) -> None:
        db, self._lock_db = self._lock_db, None
        if db is None:
            return
        try:
            db.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": WORKER_ADVISORY_LOCK_ID},
            )
        except Exception:
            # Closing the underlying connection still releases the lock.
            log.warning("explicit worker-lock release failed", exc_info=True)
        finally:
            db.close()

    def _recover_at_startup(self) -> None:
        """Requeue every job orphaned by a previous dead worker.

        A crash/OOM/reboot/deploy between the RUNNING claim and job close
        used to leave the memory stuck in its stage forever — nothing ever
        requeued it. Now the fresh worker adopts those jobs on boot.
        """
        db = self._session_factory()
        try:
            try:
                n = self.recover_orphaned_jobs(db)
            except Exception:
                log.exception("startup recovery failed; continuing without it")
                return
            if n:
                log.warning("startup recovery: requeued/closed %d orphaned job(s)", n)
            else:
                log.info("startup recovery: no orphaned jobs")
        finally:
            db.close()

    def _reap_stale_jobs(self) -> None:
        """Periodically requeue RUNNING jobs with no live owner.

        Covers workers that died without this process restarting (e.g. a
        second worker on another host). Jobs claimed by this process are
        excluded via the claimed set; the age gate (worker_stuck_after_s)
        is beyond the worst legit single claim, so a slow-but-alive job is
        never reaped.
        """
        db = self._session_factory()
        try:
            n = self.recover_orphaned_jobs(
                db,
                only_older_than_s=settings.worker_stuck_after_s,
                exclude_job_ids=self._claimed,
            )
            if n:
                log.warning(
                    "stale-job reaper: requeued/closed %d job(s) RUNNING for over %.0fs",
                    n, settings.worker_stuck_after_s,
                )
        finally:
            db.close()

    def recover_orphaned_jobs(
        self,
        db: Session,
        *,
        only_older_than_s: Optional[float] = None,
        exclude_job_ids: Collection[uuid.UUID] = frozenset(),
    ) -> int:
        """Requeue (or honestly close) jobs left in RUNNING with no owner.

        Returns the number of jobs recovered. Recovery rules, in order:

        1. Memory already terminal but job still RUNNING: bookkeeping only —
           close the job as DONE/FAILED to match, never reprocess.
        2. Attempt budget (MAX_ATTEMPTS, crashes included) already spent:
           FAILED_PERMANENT with code WORKER_LOST_JOB — fail honestly
           instead of requeueing forever.
        3. Otherwise: delete partial evidence from the dead attempt (so the
           re-run cannot duplicate segments/embeddings/tags) and requeue via
           legal transitions only (mid-pipeline stages step through
           FAILED_RETRYABLE, which reads honestly in the audit trail).
           attempt_count is left unchanged: poll_once increments it in the
           same transaction that changes QUEUED to RUNNING, so every crash
           has already consumed exactly one attempt.

        Crash recovery is an operator-level reset, not a live pipeline step:
        the requeue path uses assert_transition-checked transitions, and the
        exhausted path uses the narrow _force_terminal escape hatch.
        """
        rows = (
            db.query(ProcessingJob, Memory)
            .join(Memory, ProcessingJob.memory_id == Memory.id)
            .filter(ProcessingJob.status == "RUNNING")
            .all()
        )
        now = _utcnow()
        recovered = 0
        for job, memory in rows:
            if job.id in exclude_job_ids:
                continue
            if only_older_than_s is not None and job.started_at is not None:
                age_s = (now - job.started_at).total_seconds()
                if age_s < only_older_than_s:
                    continue
                # started_at None: claimed by an ancient worker build — stale.
            status = coerce(memory.processing_status)
            if is_terminal(status):
                job.status = (
                    "FAILED"
                    if status == ProcessingStatus.FAILED_PERMANENT
                    else "DONE"
                )
                job.finished_at = now
                db.commit()
                log.warning(
                    "orphaned job %s: memory %s already %s; closed job as %s",
                    job.id, memory.id, status.value, job.status,
                )
                recovered += 1
                continue
            if (job.attempt_count or 0) >= MAX_ATTEMPTS:
                self._force_terminal(
                    db, memory, job, ProcessingStatus.FAILED_PERMANENT,
                    code="WORKER_LOST_JOB",
                    message=(
                        f"worker died while the memory was {status.value}; "
                        f"attempt budget ({MAX_ATTEMPTS}, crashes included) "
                        "exhausted — not requeued"
                    ),
                )
                job.status = "FAILED"
                job.finished_at = now
                db.commit()
                log.warning(
                    "orphaned job %s: attempts exhausted; memory %s FAILED_PERMANENT",
                    job.id, memory.id,
                )
                recovered += 1
                continue
            self._clear_partial_evidence(db, memory.id)
            if status in (ProcessingStatus.CAPTURED, ProcessingStatus.QUEUED):
                # QUEUED -> QUEUED is the idempotent no-op; CAPTURED -> QUEUED
                # is legal. The next poll picks the job up from the top.
                self._transition(db, memory, job, ProcessingStatus.QUEUED)
            elif status == ProcessingStatus.FAILED_RETRYABLE:
                # A worker can die after persisting FAILED_RETRYABLE but before
                # it moves the row back to QUEUED. Repeating
                # FAILED_RETRYABLE -> FAILED_RETRYABLE is illegal; resume the
                # already-recorded retry directly.
                self._transition(db, memory, job, ProcessingStatus.QUEUED)
            else:
                # Every other mid-pipeline stage may step to FAILED_RETRYABLE
                # and from there back to QUEUED — all assert-transition checked.
                self._transition(
                    db, memory, job, ProcessingStatus.FAILED_RETRYABLE,
                    code="WORKER_RECOVERED",
                    message=(
                        f"worker died while the memory was {status.value}; "
                        "requeued automatically"
                    ),
                )
                self._transition(db, memory, job, ProcessingStatus.QUEUED)
            job.stage = None
            job.status = "QUEUED"  # poll_once claims on status == "QUEUED"
            job.started_at = None
            job.finished_at = None
            job.failure_code = None
            job.failure_message = None
            db.commit()
            log.warning(
                "requeued orphaned job %s (memory %s was %s)",
                job.id, memory.id, status.value,
            )
            recovered += 1
        return recovered

    def _force_terminal(
        self,
        db: Session,
        memory: Memory,
        job: ProcessingJob,
        status: ProcessingStatus,
        *,
        code: str,
        message: str,
    ) -> None:
        """Crash-recovery-only escape hatch: set a terminal status directly.

        Used solely when the attempt budget is already spent and no legal
        live transition exists (e.g. CAPTURED has no FAILED_PERMANENT edge).
        Never used on the live pipeline path — _transition governs that.
        """
        log.warning(
            "recovery forcing memory %s %s -> %s (%s)",
            memory.id, memory.processing_status, status.value, code,
        )
        memory.processing_status = status.value
        job.stage = status.value
        job.failure_code = code
        job.failure_message = message

    def _clear_partial_evidence(self, db: Session, memory_id: uuid.UUID) -> None:
        """Delete evidence rows written by a dead attempt.

        The requeued run restarts _run_stages from the top and re-persists
        everything; without this the re-run would duplicate segments,
        frame embeddings, and tags.
        """
        seg_n = (
            db.query(MemorySegment)
            .filter(MemorySegment.memory_id == memory_id)
            .delete()
        )
        emb_n = (
            db.query(MemoryFrameEmbedding)
            .filter(MemoryFrameEmbedding.memory_id == memory_id)
            .delete()
        )
        tag_n = (
            db.query(MemoryTag).filter(MemoryTag.memory_id == memory_id).delete()
        )
        if seg_n or emb_n or tag_n:
            log.info(
                "cleared partial evidence for memory %s: %d segments, "
                "%d frame embeddings, %d tags",
                memory_id, seg_n, emb_n, tag_n,
            )

    def poll_once(self) -> bool:
        """Claim one QUEUED job (SKIP LOCKED) and process it. True if work was done."""
        db = self._session_factory()
        try:
            job = (
                db.execute(
                    select(ProcessingJob)
                    .where(ProcessingJob.status == "QUEUED")
                    .with_for_update(skip_locked=True)
                    .limit(1)
                ).scalar_one_or_none()
            )
            if job is None:
                return False
            # Count the attempt atomically with the durable RUNNING claim.
            # A crash immediately after this commit has therefore consumed
            # one attempt; recovery must not guess or double-count it.
            job.attempt_count = (job.attempt_count or 0) + 1
            job.status = "RUNNING"
            job.started_at = _utcnow()
            db.commit()
            # Owned by this process from here until _process_job returns —
            # the stale-job reaper must not touch it meanwhile.
            self._claimed.add(job.id)
            try:
                memory = db.get(Memory, job.memory_id)
                item = db.get(SourceItem, memory.source_item_id)
                self._stage_t0 = None
                self._process_job(db, job, memory, item)
                return True
            finally:
                self._claimed.discard(job.id)
        finally:
            db.close()

    # -- job processing --------------------------------------------------

    def _process_job(
        self, db: Session, job: ProcessingJob, memory: Memory, item: SourceItem
    ) -> None:
        # PRD §40: temp media lives only inside this directory; it is removed
        # in the finally block whether processing succeeds or fails.
        tmp = Path(tempfile.mkdtemp(prefix="reel-memory-", dir=settings.temp_dir))
        try:
            # poll_once normally persisted attempt 1 with the RUNNING claim.
            # Direct/internal callers with an unclaimed job still get the
            # same semantics by starting the first attempt here.
            attempt = job.attempt_count or 0
            if attempt == 0:
                attempt = 1
                job.attempt_count = attempt
                db.commit()
            while attempt <= MAX_ATTEMPTS:
                try:
                    # Every queued attempt restarts the pipeline from the
                    # beginning. Remove evidence from an earlier completed
                    # run (explicit reprocess) or failed attempt before
                    # persisting replacement segments; otherwise speech/OCR
                    # rows accumulate and distort FTS results.
                    if coerce(memory.processing_status) == ProcessingStatus.QUEUED:
                        self._clear_partial_evidence(db, memory.id)
                        db.commit()
                    self._run_stages(db, job, memory, item, tmp)
                except TerminalState as t:
                    self._finish(
                        db, job, memory, t.status,
                        job_status="DONE", code=t.code, message=t.message,
                    )
                    log.info("job %s terminal: %s", job.id, t.status.value)
                    self._discard_upload_source(item)
                    return
                except StageError as e:
                    if e.retryable and attempt < MAX_ATTEMPTS:
                        wait = backoff_seconds(attempt)
                        self._transition(
                            db, memory, job, ProcessingStatus.FAILED_RETRYABLE,
                            code=e.code.value,
                            message=f"attempt {attempt}/{MAX_ATTEMPTS}: {e.message}",
                        )
                        log.warning("job %s: %s; retrying in %.0fs", job.id, e, wait)
                        time.sleep(wait)
                        self._transition(db, memory, job, ProcessingStatus.QUEUED)
                        attempt += 1
                        job.attempt_count = attempt
                        db.commit()
                        continue
                    self._finish(
                        db, job, memory, ProcessingStatus.FAILED_PERMANENT,
                        job_status="FAILED", code=e.code.value,
                        message=f"attempt {attempt}/{MAX_ATTEMPTS}: {e.message}",
                    )
                    log.warning("job %s failed permanently: %s", job.id, e)
                    self._discard_upload_source(item)
                    return
                # _run_stages completed -> memory is READY; just close the job.
                job.status = "DONE"
                job.finished_at = _utcnow()
                db.commit()
                log.info("job %s READY", job.id)
                self._discard_upload_source(item)
                return
        finally:
            # PRD §8.6 / §40: delete raw video, extracted audio, frames — always.
            shutil.rmtree(tmp, ignore_errors=True)
            log.debug("cleaned temp dir %s", tmp)

    def _discard_upload_source(self, item: SourceItem) -> None:
        """PRD §8.6: a directly-uploaded video is raw media — delete it once
        the job reaches a terminal state (READY, terminal, or permanently
        failed). Never called on the retry path, so pending attempts keep
        their bytes. Album captures store one file per photo under the
        combined hash; the same glob removes them all."""
        if item.platform != "upload":
            return
        upload_dir = Path(settings.resolve_upload_dir())
        for path in upload_dir.glob(f"{item.platform_item_id}.*"):
            try:
                path.unlink()
                log.info("deleted processed upload %s", path.name)
            except OSError:
                log.warning("could not delete processed upload %s", path, exc_info=True)

    def _finish(
        self,
        db: Session,
        job: ProcessingJob,
        memory: Memory,
        status: ProcessingStatus,
        *,
        job_status: str,
        code: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        self._transition(db, memory, job, status, code=code, message=message)
        job.status = job_status
        job.finished_at = _utcnow()
        db.commit()

    def _transition(
        self,
        db: Session,
        memory: Memory,
        job: ProcessingJob,
        status: ProcessingStatus,
        *,
        code: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        src = coerce(memory.processing_status)
        assert_transition(src, status)
        now = time.monotonic()
        if self._stage_t0 is not None:
            # §48 cost hook: per-stage durations in logs; correlate with
            # attempt counts and provider usage for cost-per-reel tracking.
            log.info(
                "memory %s stage %s -> %s took %.1fs",
                memory.id, src.value, status.value, now - self._stage_t0,
            )
        self._stage_t0 = now
        memory.processing_status = status.value
        job.stage = status.value
        if code is not None:
            job.failure_code = code
        if message is not None:
            job.failure_message = message
        db.commit()

    def _call(self, code: FailureCode, fn: Callable, *args, **kwargs):
        """Run a stage callable, mapping any exception to the stage's FailureCode."""
        try:
            return fn(*args, **kwargs)
        except (StageError, TerminalState):
            raise
        except ProviderError as e:
            # A configured provider failed at call time; honor its classified
            # hint when present, else the stage's code (both stay in taxonomy).
            raise StageError(e.failure_code or code, str(e)) from e
        except ProviderNotConfiguredError as e:
            # Configuration errors are permanent until an operator changes
            # settings; retrying the same call wastes time and blocks the queue.
            raise StageError(code, str(e), retryable_override=False) from e
        except SQLAlchemyError as e:
            raise StageError(
                FailureCode.DATABASE_FAILED, f"{type(e).__name__}: {e}"
            ) from e
        except Exception as e:  # noqa: BLE001 - stage boundary: classify everything
            raise StageError(code, f"{type(e).__name__}: {e}") from e

    # -- pipeline stages (PRD §17) ----------------------------------------

    def _run_stages(
        self, db: Session, job: ProcessingJob, memory: Memory, item: SourceItem, tmp: Path
    ) -> None:
        if coerce(memory.processing_status) == ProcessingStatus.CAPTURED:
            self._transition(db, memory, job, ProcessingStatus.QUEUED)

        adapter = get_adapter(item.platform)
        canon = CanonicalURL(
            platform=item.platform,
            platform_item_id=item.platform_item_id,
            canonical_url=item.canonical_url,
            original_url=item.original_url,
        )

        self._transition(db, memory, job, ProcessingStatus.RESOLVING_SOURCE)
        result = self._call(
            FailureCode.SOURCE_RESOLUTION_FAILED,
            adapter.resolve,
            canon,
            work_dir=str(tmp),
        )
        if isinstance(result, RetryableFailure):
            try:
                code = FailureCode(result.code)
            except ValueError:
                code = FailureCode.SOURCE_RESOLUTION_FAILED
            raise StageError(code, result.detail)
        if isinstance(result, Unavailable):
            raise TerminalState(
                ProcessingStatus.SOURCE_UNAVAILABLE,
                FailureCode.SOURCE_DELETED.value,
                result.reason,
            )
        if isinstance(result, AuthenticationRequired):
            # PRD §15: never circumvent access restrictions; record and stop.
            raise TerminalState(
                ProcessingStatus.SOURCE_REQUIRES_ACCESS,
                FailureCode.SOURCE_LOGIN_REQUIRED.value,
                result.detail,
            )
        if isinstance(result, Unsupported):
            raise TerminalState(
                ProcessingStatus.FAILED_PERMANENT,
                FailureCode.UNSUPPORTED_SOURCE.value,
                result.detail,
            )
        if isinstance(result, MetadataOnly):
            self._persist_metadata(db, memory, item, result.metadata)
            raise TerminalState(
                ProcessingStatus.METADATA_ONLY, None,
                "metadata captured; media unavailable",
            )

        if isinstance(result, ArticleContent):
            # Website ingestion: the article text IS the content — no
            # audio/video stages apply. Persist + embed + land READY.
            self._ingest_article(db, memory, item, job, result)
            return

        if isinstance(result, TranscriptContent):
            # YouTube: use public timestamped subtitles when available. No raw
            # video bytes or generated cloud transcription are required.
            self._ingest_transcript_content(db, memory, item, job, result)
            return

        if isinstance(result, AlbumMedia):
            # Carousel/album: one memory over many files. Every file is
            # processed; each segment carries its album_index so citations
            # can name the photo ("photo 7" = index 6).
            self._apply_source_metadata(db, item, result.metadata, source_status="RESOLVED")
            self._process_album(db, memory, item, job, result, tmp)
            return

        media: ResolvedMedia = result
        self._apply_source_metadata(db, item, media.metadata, source_status="RESOLVED")
        self._transition(db, memory, job, ProcessingStatus.MEDIA_READY)
        if not media.media_path:
            raise StageError(
                FailureCode.MEDIA_DOWNLOAD_FAILED,
                "adapter resolved media but provided no bytes",
            )

        suffix = Path(media.media_path).suffix.lower()
        if suffix in _IMAGE_SUFFIXES:
            # Screenshot/photo upload: no audio to extract; the single image
            # is the frame set for vision + OCR.
            audio_path = None
            frame_paths = [media.media_path]
        else:
            audio_path = self._extract_audio(media.media_path, tmp)
            frame_paths = self._extract_frames(media.media_path, tmp)
        # Milestone A UX: one small persisted thumbnail + media kind, once.
        memory.media_kind = _media_kind_for_media(suffix)
        self._persist_thumbnail(memory, frame_paths, tmp)
        # Visual search index: select frames once, now (uniform + bounded
        # scene-cut extras). Embedding happens in the ANALYZING_VISUALS
        # stage below; the GPT vision pipeline right after is untouched.
        visual_samples = self._select_visual_samples(
            media.media_path, tmp, frame_paths, album_index=None
        )

        transcript = []
        if audio_path is not None:
            self._transition(db, memory, job, ProcessingStatus.TRANSCRIBING)
            transcript = self._call(
                FailureCode.TRANSCRIPTION_FAILED, self.providers.speech.transcribe, audio_path
            )
            self._persist_segments(
                db, memory, "speech",
                [(s.start_ms, s.end_ms, s.text) for s in transcript],
            )

        self._transition(db, memory, job, ProcessingStatus.ANALYZING_VISUALS)
        if isinstance(self.providers.vision, UnconfiguredVisionProvider):
            # Semantic vision is optional in the local-first pipeline. OCR +
            # transcript + OpenCLIP can make a memory searchable without a
            # multimodal LLM; "none" is an explicit operator choice, not a
            # silent provider failure.
            visuals = []
            log.info("semantic vision disabled; continuing with local text/visual indexing")
        else:
            visuals = self._call(
                FailureCode.VISION_FAILED, self.providers.vision.analyze_frames, frame_paths
            )
        self._persist_segments(
            db, memory, "visual",
            [(o.timestamp_ms, None, o.description) for o in visuals],
        )
        # One-time visual frame index (local joint image/text model; free).
        # Retrieval use only — the GPT descriptions above remain the readable
        # evidence for Q&A. Skipped when VISUAL_EMBEDDING_PROVIDER=none.
        self._index_visual_frames(db, memory, visual_samples)

        self._transition(db, memory, job, ProcessingStatus.RUNNING_OCR)
        ocr = self._call(
            FailureCode.OCR_FAILED, self.providers.ocr.extract_text, frame_paths
        )
        self._persist_segments(
            db, memory, "ocr",
            [(s.timestamp_ms, None, s.text) for s in ocr],
        )

        evidence = PipelineEvidence(
            transcript=transcript,
            visuals=visuals,
            ocr=ocr,
            metadata=SourceMetadata(
                creator_handle=item.creator_handle,
                caption=item.caption,
                published_at=item.published_at,
            ),
        )
        self._generate_memory_and_index(db, memory, item, job, evidence)

    def _process_album(
        self,
        db: Session,
        memory: Memory,
        item: SourceItem,
        job: ProcessingJob,
        album: AlbumMedia,
        tmp: Path,
    ) -> None:
        """Album/carousel path: one memory over many files.

        Stages run per-modality (not per-file) so the state machine stays
        legal: MEDIA_READY -> TRANSCRIBING (only when some file has audio)
        -> ANALYZING_VISUALS -> RUNNING_OCR -> shared generation. Every
        segment carries metadata_json {"album_index": i}; the single video,
        if present, goes through the normal video path.
        """
        self._transition(db, memory, job, ProcessingStatus.MEDIA_READY)
        indexed = list(enumerate(album.files))
        audio_paths: dict[int, str] = {}
        frame_sets: dict[int, list[str]] = {}
        for idx, path in indexed:
            if Path(path).suffix.lower() in _IMAGE_SUFFIXES:
                # Photo: the image itself is the frame set for vision + OCR.
                frame_sets[idx] = [path]
            else:
                # Video: decode audio + frames. A video with no audio stream
                # yields None and is skipped by the transcription stage below.
                audio_path = self._extract_audio(path, tmp)
                if audio_path is not None:
                    audio_paths[idx] = audio_path
                frame_sets[idx] = self._extract_frames(path, tmp)
        # Milestone A UX: one small persisted thumbnail (first file's frames)
        # + media kind, once. Never reprocessed for old memories.
        memory.media_kind = "album"
        first_frames = frame_sets[0] if 0 in frame_sets else []
        self._persist_thumbnail(memory, first_frames, tmp)
        # Visual search index: one sample list per album file (album_index
        # tags which photo/video each frame came from for "photo N"
        # citations). Embedding happens in ANALYZING_VISUALS below.
        visual_samples_all: list[FrameSample] = []
        for idx, path in indexed:
            visual_samples_all.extend(
                self._select_visual_samples(path, tmp, frame_sets[idx], album_index=idx)
            )

        transcript_all = []
        if audio_paths:
            self._transition(db, memory, job, ProcessingStatus.TRANSCRIBING)
            for idx in sorted(audio_paths):
                transcript = self._call(
                    FailureCode.TRANSCRIPTION_FAILED,
                    self.providers.speech.transcribe,
                    audio_paths[idx],
                )
                self._persist_segments(
                    db, memory, "speech",
                    [(s.start_ms, s.end_ms, s.text) for s in transcript],
                    metadata={"album_index": idx},
                )
                transcript_all.extend(transcript)

        self._transition(db, memory, job, ProcessingStatus.ANALYZING_VISUALS)
        visuals_all = []
        if isinstance(self.providers.vision, UnconfiguredVisionProvider):
            log.info("semantic vision disabled for album; continuing with OCR/visual indexing")
        else:
            for idx, _path in indexed:
                visuals = self._call(
                    FailureCode.VISION_FAILED,
                    self.providers.vision.analyze_frames,
                    frame_sets[idx],
                )
                self._persist_segments(
                    db, memory, "visual",
                    [(o.timestamp_ms, None, o.description) for o in visuals],
                    metadata={"album_index": idx},
                )
                visuals_all.extend(visuals)
        # One-time visual frame index (see single-file path above).
        self._index_visual_frames(db, memory, visual_samples_all)

        self._transition(db, memory, job, ProcessingStatus.RUNNING_OCR)
        ocr_all = []
        for idx, _path in indexed:
            ocr = self._call(
                FailureCode.OCR_FAILED,
                self.providers.ocr.extract_text,
                frame_sets[idx],
            )
            self._persist_segments(
                db, memory, "ocr",
                [(s.timestamp_ms, None, s.text) for s in ocr],
                metadata={"album_index": idx},
            )
            ocr_all.extend(ocr)

        evidence = PipelineEvidence(
            transcript=transcript_all,
            visuals=visuals_all,
            ocr=ocr_all,
            metadata=SourceMetadata(
                creator_handle=item.creator_handle,
                caption=item.caption,
                published_at=item.published_at,
            ),
        )
        self._generate_memory_and_index(db, memory, item, job, evidence)

    def _generate_memory_and_index(
        self,
        db: Session,
        memory: Memory,
        item: SourceItem,
        job: ProcessingJob,
        evidence: PipelineEvidence,
        *,
        preferred_title: str | None = None,
    ) -> None:
        """Shared tail of the media pipeline: memory generation, indexing,
        READY. Used by both the single-file path and the album path."""
        self._transition(db, memory, job, ProcessingStatus.GENERATING_MEMORY)
        try:
            generated = self.providers.memory_generator.generate(evidence)
        except ProviderNotConfiguredError:
            # Deterministic fallback so the pipeline is testable without AI
            # spend; Milestone 4 wires the real MemoryGenerator provider.
            log.info("memory generator not configured; using deterministic builder")
            generated = build_memory(evidence)
        memory.title = (preferred_title or generated.title)[:200]
        memory.summary = generated.summary
        memory.category = generated.category
        memory.language = generated.language
        memory.processing_version = settings.processing_version
        if item.caption:
            self._persist_segments(db, memory, "caption", [(None, None, item.caption)])
        self._persist_segments(db, memory, "summary", [(None, None, generated.summary)])
        db.query(MemoryTag).filter(MemoryTag.memory_id == memory.id).delete()
        for tag in generated.tags:
            db.add(MemoryTag(memory_id=memory.id, tag=tag[:128], confidence=None))
        db.commit()

        # Flat categories were too broad and brittle. Classify from all
        # persisted evidence into the per-user hierarchy; this also rewrites
        # memory.category to the specific primary leaf for legacy clients.
        categorize_memory(db, memory, item)

        self._index_segments(db, memory, job)

        self._transition(db, memory, job, ProcessingStatus.READY)

    def _ingest_transcript_content(
        self,
        db: Session,
        memory: Memory,
        item: SourceItem,
        job: ProcessingJob,
        content: TranscriptContent,
    ) -> None:
        """YouTube/text-first video path: persist public subtitle cues and index.

        Empty subtitle lists are honest: title/description still become a READY
        memory, but no speech evidence is fabricated.
        """
        self._apply_source_metadata(db, item, content.metadata, source_status="RESOLVED")
        memory.media_kind = "video"
        transcript = [
            TranscriptSegment(start_ms=start, end_ms=end, text=text)
            for start, end, text in content.segments
            if text and text.strip()
        ]
        self._persist_segments(
            db, memory, "speech",
            [(s.start_ms, s.end_ms, s.text) for s in transcript],
        )
        evidence = PipelineEvidence(
            transcript=transcript,
            metadata=SourceMetadata(
                creator_handle=item.creator_handle,
                caption=item.caption,
                published_at=item.published_at,
            ),
        )
        self._generate_memory_and_index(
            db, memory, item, job, evidence, preferred_title=content.title
        )

    def _ingest_article(
        self,
        db: Session,
        memory: Memory,
        item: SourceItem,
        job: ProcessingJob,
        article: ArticleContent,
    ) -> None:
        """Website ingestion path (platform "web").

        The extracted article text IS the content: store it as an "article"
        segment, build the memory deterministically (page title + lead
        summary — honest truncation, nothing invented), embed, land READY.
        """
        self._transition(db, memory, job, ProcessingStatus.GENERATING_MEMORY)
        title = (article.title or item.canonical_url or "Untitled page").strip()
        text = article.text.strip()
        memory.title = title[:200]
        memory.summary = _lead_summary(text)
        memory.category = None
        memory.language = None
        memory.media_kind = "article"  # Milestone A UX: honest library filter
        memory.processing_version = settings.processing_version
        db.commit()
        self._persist_segments(db, memory, "article", [(None, None, text)])
        categorize_memory(db, memory, item)
        self._index_segments(db, memory, job)
        self._transition(db, memory, job, ProcessingStatus.READY)

    def _index_segments(
        self, db: Session, memory: Memory, job: ProcessingJob
    ) -> None:
        """INDEXING stage: embed every segment of the memory (shared by the
        media pipeline and the article ingestion path)."""
        self._transition(db, memory, job, ProcessingStatus.INDEXING)
        segments = (
            db.query(MemorySegment)
            .filter(MemorySegment.memory_id == memory.id)
            .order_by(MemorySegment.id)
            .all()
        )
        texts = [s.content for s in segments]
        vectors = self._call(
            FailureCode.EMBEDDING_FAILED, self.providers.embedding.embed, texts
        )
        if len(vectors) != len(segments):
            raise StageError(
                FailureCode.EMBEDDING_FAILED,
                f"embedding count mismatch: {len(vectors)} vectors for {len(segments)} segments",
            )
        for seg, vec in zip(segments, vectors):
            if len(vec) != settings.embedding_dim:
                raise StageError(
                    FailureCode.EMBEDDING_FAILED,
                    f"embedding dim {len(vec)} != configured {settings.embedding_dim}",
                )
            seg.embedding = vec
        db.commit()

    # -- helpers ------------------------------------------------------------

    def _persist_segments(
        self,
        db: Session,
        memory: Memory,
        modality: str,
        rows: list[tuple],
        metadata: dict | None = None,
    ) -> None:
        for start_ms, end_ms, content in rows:
            if content and content.strip():
                db.add(
                    MemorySegment(
                        memory_id=memory.id,
                        modality=modality,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        content=content.strip()[:20000],
                        metadata_json=dict(metadata) if metadata else {},
                    )
                )
        db.commit()

    def _apply_source_metadata(
        self, db: Session, item: SourceItem, metadata: SourceMetadata, *, source_status: str | None = None
    ) -> None:
        """Persist source metadata without creating evidence rows.

        Resolved media later writes caption evidence in the shared generation tail;
        keeping this helper evidence-free avoids duplicate caption segments.
        """
        item.creator_handle = metadata.creator_handle
        item.caption = metadata.caption
        item.published_at = metadata.published_at
        if source_status is not None:
            item.source_status = source_status
        db.commit()

    def _persist_metadata(
        self, db: Session, memory: Memory, item: SourceItem, metadata: SourceMetadata
    ) -> None:
        self._apply_source_metadata(db, item, metadata, source_status="METADATA_ONLY")
        if metadata.caption:
            self._persist_segments(db, memory, "caption", [(None, None, metadata.caption)])

    def _extract_audio(self, media_path: str, tmp: Path) -> str | None:
        """Decode the video's audio to 16 kHz mono WAV (Milestone 3: ffmpeg).

        Returns None when the container has no audio stream — the caller then
        skips transcription honestly instead of producing an empty transcript.
        Any decode failure raises StageError(MEDIA_DECODE_FAILED)."""
        try:
            return media_decode.extract_audio(
                media_path,
                tmp,
                ffmpeg_bin=settings.ffmpeg_bin,
                ffprobe_bin=settings.ffprobe_bin,
                max_duration_s=settings.max_media_duration_s,
                probe_timeout_s=settings.media_probe_timeout_s,
                decode_timeout_s=settings.media_decode_timeout_s,
            )
        except media_decode.MediaDecodeError as e:
            raise StageError(FailureCode.MEDIA_DECODE_FAILED, str(e)) from e

    def _extract_frames(self, media_path: str, tmp: Path) -> list[str]:
        """Sample representative JPEG frames across the video (Milestone 3:
        ffmpeg). Any decode failure raises StageError(MEDIA_DECODE_FAILED) —
        an empty frame list is never returned silently."""
        try:
            return media_decode.extract_frames(
                media_path,
                tmp,
                count=settings.media_frame_count,
                max_dimension_px=settings.media_max_dimension_px,
                ffmpeg_bin=settings.ffmpeg_bin,
                ffprobe_bin=settings.ffprobe_bin,
                max_duration_s=settings.max_media_duration_s,
                probe_timeout_s=settings.media_probe_timeout_s,
                decode_timeout_s=settings.media_decode_timeout_s,
            )
        except media_decode.MediaDecodeError as e:
            raise StageError(FailureCode.MEDIA_DECODE_FAILED, str(e)) from e

    # -- thumbnails + media kind (Milestone A UX) ---------------------------

    def _persist_thumbnail(
        self,
        memory: Memory,
        frame_paths: list[str],
        tmp: Path,
    ) -> None:
        """Best-effort: persist one small JPEG thumbnail on the memory.

        Uses the middle extracted frame (most representative single still).
        Never fails the job — a thumbnail is a UI nicety and must not lose
        an otherwise good memory. Old memories keep thumbnail_jpeg NULL.
        """
        if not frame_paths:
            return
        try:
            src = frame_paths[len(frame_paths) // 2]
            thumb_path = media_decode.make_thumbnail_jpeg(
                src, tmp, ffmpeg_bin=settings.ffmpeg_bin
            )
            memory.thumbnail_jpeg = Path(thumb_path).read_bytes()
        except Exception as e:
            log.warning("thumbnail failed for memory %s: %s", memory.id, e)

    # -- visual frame indexing (visual search) ------------------------------

    def _select_visual_samples(
        self,
        media_path: str,
        tmp: Path,
        frame_paths: list[str],
        album_index: int | None,
    ) -> list[FrameSample]:
        """Choose the frames to visually index: the existing uniform frames
        plus bounded scene-cut extras (true timestamps). Photos index as a
        single 'photo' sample. Returns [] when visual indexing is disabled
        (VISUAL_EMBEDDING_PROVIDER=none) — the common default."""
        if settings.visual_embedding_provider == "none":
            return []
        if Path(media_path).suffix.lower() in _IMAGE_SUFFIXES:
            return [
                FrameSample(
                    path=media_path,
                    timestamp_ms=0,
                    selection="photo",
                    album_index=album_index,
                )
            ]
        try:
            return visual_frame_index.select_video_frames(
                media_path, tmp, frame_paths, album_index=album_index
            )
        except media_decode.MediaDecodeError as e:
            raise StageError(FailureCode.MEDIA_DECODE_FAILED, str(e)) from e

    def _index_visual_frames(
        self, db: Session, memory: Memory, samples: list[FrameSample]
    ) -> None:
        """Embed frame samples with the local joint image/text model and
        persist memory_frame_embeddings rows. No-op when disabled. A
        misconfigured/broken visual provider fails the job loudly with
        VISUAL_INDEX_FAILED — visual coverage is never silently faked."""
        if not samples:
            return
        try:
            provider = build_visual_provider()
        except VisualProviderNotConfiguredError as e:
            raise StageError(FailureCode.VISUAL_INDEX_FAILED, str(e)) from e
        # Fail fast on model/column dim mismatch rather than writing vectors
        # the index cannot compare (changing the model needs a migration).
        try:
            model_dim = provider.dim
        except (VisualProviderNotConfiguredError, VisualProviderError) as e:
            raise StageError(FailureCode.VISUAL_INDEX_FAILED, str(e)) from e
        if model_dim != settings.visual_embedding_dim:
            raise StageError(
                FailureCode.VISUAL_INDEX_FAILED,
                f"visual model dim {model_dim} != VISUAL_EMBEDDING_DIM "
                f"{settings.visual_embedding_dim} (migration "
                "memory_frame_embeddings.embedding vector(N) must match the "
                "model; re-migrate and re-index to change models)",
            )
        try:
            n = visual_frame_index.index_visual_frames(
                db, memory.id, samples, provider
            )
        except (VisualProviderNotConfiguredError, VisualProviderError) as e:
            raise StageError(FailureCode.VISUAL_INDEX_FAILED, str(e)) from e
        log.info("visually indexed %d frames for memory %s", n, memory.id)
