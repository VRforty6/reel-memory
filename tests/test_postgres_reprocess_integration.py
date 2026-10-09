"""PostgreSQL regression coverage for reprocessing derived evidence.

External acquisition/AI boundaries are deterministic doubles.  Worker state
transitions, commits, evidence replacement, pgvector writes, and PostgreSQL
full-text search all run against a real isolated PostgreSQL schema.
"""

from __future__ import annotations

import uuid
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.api.memories import reprocess_memory
from app.config import settings
from app.models import Base, Memory, MemorySegment, ProcessingJob, SourceItem, User
from app.pipeline.providers import (
    DeterministicMemoryGenerator,
    HashingEmbeddingProvider,
    OCRSegment,
    Providers,
    TranscriptSegment,
)
from app.pipeline.state_machine import ProcessingStatus
from app.pipeline.worker import Worker
from app.search.hybrid import hybrid_search
from app.sources.base import ResolvedMedia, SourceMetadata

SPEECH_PHRASE = "comet orchard speech marker"
OCR_PHRASE = "copper lighthouse screen marker"
CAPTION_PHRASE = "amber zeppelin caption marker"


@pytest.fixture
def postgres_session_factory():
    """Create an isolated schema in the configured PostgreSQL database.

    The normal database and its user data are untouched. Tests skip only when
    PostgreSQL/pgvector is unavailable; schema/setup failures after a
    successful connection remain real failures.
    """

    schema = f"pytest_reprocess_{uuid.uuid4().hex}"
    admin_engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        try:
            with admin_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                has_vector = conn.execute(
                    text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')")
                ).scalar_one()
        except SQLAlchemyError as exc:
            pytest.skip(f"PostgreSQL unavailable: {type(exc).__name__}")
        if not has_vector:
            pytest.skip("PostgreSQL pgvector extension is unavailable")

        with admin_engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))

        test_engine = create_engine(
            settings.database_url,
            connect_args={"options": f"-csearch_path={schema},public"},
            pool_pre_ping=True,
        )
        try:
            # public is on search_path so PostgreSQL can resolve the vector
            # extension type. checkfirst=False prevents public's application
            # tables from satisfying the existence checks for this schema.
            Base.metadata.create_all(test_engine, checkfirst=False)
            yield sessionmaker(
                bind=test_engine,
                autoflush=False,
                autocommit=False,
            )
        finally:
            test_engine.dispose()
    finally:
        if schema.startswith("pytest_reprocess_"):
            try:
                with admin_engine.begin() as conn:
                    conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            finally:
                admin_engine.dispose()


class _FixtureAdapter:
    def __init__(self, video_path: Path):
        self.video_path = video_path

    def resolve(self, canonical, *, work_dir=None):
        return ResolvedMedia(
            canonical=canonical,
            metadata=SourceMetadata(caption=CAPTION_PHRASE),
            media_path=str(self.video_path),
            duration_s=12.0,
        )


class _FixtureSpeech:
    def __init__(self):
        self.calls = 0

    def transcribe(self, audio_path: str):
        self.calls += 1
        return [
            TranscriptSegment(0, 1200, SPEECH_PHRASE, "en"),
            TranscriptSegment(1200, 2400, "second deterministic speech segment", "en"),
        ]


class _FixtureOCR:
    def __init__(self):
        self.calls = 0

    def extract_text(self, frame_paths: list[str]):
        self.calls += 1
        return [
            OCRSegment(0, OCR_PHRASE),
            OCRSegment(5000, "second deterministic screen segment"),
        ]


def _modality_counts(db, memory_id: uuid.UUID) -> dict[str, int]:
    rows = (
        db.query(MemorySegment.modality, func.count(MemorySegment.id))
        .filter(MemorySegment.memory_id == memory_id)
        .group_by(MemorySegment.modality)
        .all()
    )
    return dict(rows)


def _matching_hit(db, user_id: uuid.UUID, query: str, memory_id: uuid.UUID):
    hits = hybrid_search(db, user_id, query, query_embedding=None, limit=10)
    return next((hit for hit in hits if hit.memory_id == memory_id), None), hits


def test_postgres_reprocess_replaces_evidence_and_preserves_fts(
    postgres_session_factory, monkeypatch, tmp_path
):
    Session = postgres_session_factory
    video = tmp_path / "fixture.mp4"
    audio = tmp_path / "fixture.wav"
    frame = tmp_path / "fixture.jpg"
    video.write_bytes(b"deterministic-video-boundary")
    audio.write_bytes(b"deterministic-audio-boundary")
    frame.write_bytes(b"deterministic-frame-boundary")

    db = Session()
    try:
        user = User(email=f"postgres-reprocess-{uuid.uuid4().hex}@example.test")
        target_item = SourceItem(
            user=user,
            platform="fixture",
            platform_item_id="target-memory",
            canonical_url="https://fixture.invalid/reel/target",
            original_url="https://fixture.invalid/reel/target",
            caption=CAPTION_PHRASE,
            source_status="AVAILABLE",
        )
        target = Memory(
            source_item=target_item,
            processing_status=ProcessingStatus.CAPTURED.value,
        )
        first_job = ProcessingJob(
            memory=target,
            status="QUEUED",
            stage=ProcessingStatus.QUEUED.value,
        )

        unrelated_item = SourceItem(
            user=user,
            platform="fixture",
            platform_item_id="unrelated-memory",
            canonical_url="https://fixture.invalid/reel/unrelated",
            original_url="https://fixture.invalid/reel/unrelated",
            caption="ordinary unrelated caption",
            source_status="AVAILABLE",
        )
        unrelated = Memory(
            source_item=unrelated_item,
            title="Unrelated memory",
            processing_status=ProcessingStatus.READY.value,
        )
        unrelated.segments.extend(
            [
                MemorySegment(modality="speech", content="ordinary unrelated speech"),
                MemorySegment(modality="ocr", content="ordinary unrelated screen text"),
                MemorySegment(modality="caption", content="ordinary unrelated caption"),
            ]
        )
        db.add_all([target, first_job, unrelated])
        db.commit()
        target_id, unrelated_id, user_id, first_job_id = (
            target.id,
            unrelated.id,
            user.id,
            first_job.id,
        )
    finally:
        db.close()

    speech = _FixtureSpeech()
    ocr = _FixtureOCR()
    worker = Worker(
        Session,
        Providers(
            speech=speech,
            ocr=ocr,
            embedding=HashingEmbeddingProvider(),
            memory_generator=DeterministicMemoryGenerator(),
        ),
    )
    monkeypatch.setattr(settings, "temp_dir", str(tmp_path))
    monkeypatch.setattr("app.pipeline.worker.get_adapter", lambda _platform: _FixtureAdapter(video))
    monkeypatch.setattr(worker, "_extract_audio", lambda _media, _tmp: str(audio))
    monkeypatch.setattr(worker, "_extract_frames", lambda _media, _tmp: [str(frame)])
    monkeypatch.setattr(worker, "_persist_thumbnail", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "_select_visual_samples", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(worker, "_index_visual_frames", lambda *_args, **_kwargs: None)

    # Run 1: the real worker claims the queued row, persists all evidence,
    # indexes it, and commits terminal job/memory state.
    assert worker.poll_once() is True
    db = Session()
    try:
        first_job = db.get(ProcessingJob, first_job_id)
        target = db.get(Memory, target_id)
        assert first_job.status == "DONE"
        assert first_job.stage == ProcessingStatus.READY.value
        assert first_job.failure_code is None
        assert target.processing_status == ProcessingStatus.READY.value
        run1_counts = _modality_counts(db, target_id)
        assert run1_counts == {"caption": 1, "ocr": 2, "speech": 2, "summary": 1}

        # Queue the same memory through the real endpoint policy.
        response = reprocess_memory(target_id, db=db, user=db.get(User, user_id))
        assert response.processing_status == ProcessingStatus.QUEUED.value
        second_job = (
            db.query(ProcessingJob)
            .filter(
                ProcessingJob.memory_id == target_id,
                ProcessingJob.status == "QUEUED",
            )
            .one()
        )
        second_job_id = second_job.id
    finally:
        db.close()

    # Run 2: the queued-attempt cleanup must replace, not append, Run 1.
    assert worker.poll_once() is True
    db = Session()
    try:
        second_job = db.get(ProcessingJob, second_job_id)
        target = db.get(Memory, target_id)
        assert second_job.status == "DONE"
        assert second_job.stage == ProcessingStatus.READY.value
        assert second_job.failure_code is None
        assert second_job.failure_message is None
        assert target.processing_status == ProcessingStatus.READY.value

        run2_counts = _modality_counts(db, target_id)
        assert run2_counts == run1_counts
        assert run2_counts == {"caption": 1, "ocr": 2, "speech": 2, "summary": 1}
        assert sum(run2_counts.values()) == 6

        duplicate_rows = (
            db.query(
                MemorySegment.modality,
                MemorySegment.content,
                func.count(MemorySegment.id).label("copies"),
            )
            .filter(MemorySegment.memory_id == target_id)
            .group_by(MemorySegment.modality, MemorySegment.content)
            .having(func.count(MemorySegment.id) > 1)
            .all()
        )
        assert duplicate_rows == []
        assert Counter(
            row.content
            for row in db.query(MemorySegment)
            .filter(MemorySegment.memory_id == target_id)
            .all()
        )[SPEECH_PHRASE] == 1
        assert Counter(
            row.content
            for row in db.query(MemorySegment)
            .filter(MemorySegment.memory_id == target_id)
            .all()
        )[OCR_PHRASE] == 1

        for query, expected_type in (
            (SPEECH_PHRASE, "SPEECH"),
            (OCR_PHRASE, "OCR"),
            (CAPTION_PHRASE, "CAPTION"),
        ):
            hit, hits = _matching_hit(db, user_id, query, target_id)
            assert hit is not None
            assert expected_type in {e.type for e in hit.evidence}
            assert unrelated_id not in {result.memory_id for result in hits}

        assert speech.calls == 2
        assert ocr.calls == 2
    finally:
        db.close()
