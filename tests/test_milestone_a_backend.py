"""Unit tests: Milestone A backend additions (thumbnail + media kind +
evidence timestamps). Pure logic — no DB, no network."""

import subprocess
import uuid
from pathlib import Path

import pytest

from app.pipeline import media as media_decode
from app.pipeline.worker import _media_kind_for_media
from app.schemas import MemorySummary, SearchEvidence
from app.search.hybrid import FTS_SQL, VEC_SQL, Evidence


# -- thumbnail generation ---------------------------------------------------


def _make_frame(tmp_path: Path) -> str:
    """Generate a real 640x480 JPEG frame with ffmpeg (lavfi testsrc)."""
    frame = tmp_path / "frame.jpg"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=640x480:rate=1",
            "-frames:v", "1", "-y", str(frame),
        ],
        check=True,
        timeout=30,
    )
    assert frame.exists() and frame.stat().st_size > 0
    return str(frame)


def test_make_thumbnail_jpeg_downscales_to_max_width(tmp_path):
    frame = _make_frame(tmp_path)
    out = media_decode.make_thumbnail_jpeg(frame, tmp_path, max_width_px=320)
    p = Path(out)
    assert p.exists() and p.stat().st_size > 0
    # JPEG magic bytes.
    assert p.read_bytes()[:2] == b"\xff\xd8"
    # Width is capped at 320 (ffprobe the output).
    cp = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width", "-of", "csv=p=0", out],
        capture_output=True, text=True, timeout=15,
    )
    assert int(cp.stdout.strip()) == 320


def test_make_thumbnail_jpeg_failure_raises_media_decode_error(tmp_path):
    with pytest.raises(media_decode.MediaDecodeError):
        media_decode.make_thumbnail_jpeg(
            str(tmp_path / "does-not-exist.jpg"), tmp_path
        )


# -- media kind classification ----------------------------------------------


def test_media_kind_image_suffixes():
    for suffix in (".png", ".jpg", ".jpeg", ".webp", ".JPG"):
        assert _media_kind_for_media(suffix) == "image"


def test_media_kind_video_suffixes():
    for suffix in (".mp4", ".mov", ".webm", ""):
        assert _media_kind_for_media(suffix) == "video"


# -- evidence timestamps ------------------------------------------------------


def test_evidence_defaults_to_no_timestamps():
    e = Evidence(type="SPEECH", snippet="hello")
    assert e.start_ms is None
    assert e.end_ms is None


def test_evidence_carries_timestamps():
    e = Evidence(type="VISUAL", snippet="Visual match near 00:18", start_ms=18000)
    assert e.start_ms == 18000
    assert e.end_ms is None


def test_search_evidence_schema_defaults():
    e = SearchEvidence(type="TAG", snippet="x")
    assert e.start_ms is None
    assert e.end_ms is None
    assert SearchEvidence(type="SPEECH", snippet="x", start_ms=1000, end_ms=2500).start_ms == 1000


def test_segment_sql_selects_timestamps():
    for sql in (FTS_SQL, VEC_SQL):
        assert "s.start_ms" in sql
        assert "s.end_ms" in sql


# -- list response fields -----------------------------------------------------


def test_memory_summary_requires_platform_media_kind_optional():
    from datetime import datetime, timezone
    import uuid

    base = dict(
        id=uuid.uuid4(),
        title="t",
        summary="s",
        category="c",
        platform="instagram",
        processing_status="READY",
        has_thumbnail=False,
        created_at=datetime.now(timezone.utc),
    )
    s = MemorySummary(**base)
    assert s.platform == "instagram"
    assert s.media_kind is None
    assert MemorySummary(**{**base, "media_kind": "video"}).media_kind == "video"


# -- migration 006 --------------------------------------------------------------


def test_migration_006_adds_thumbnail_and_media_kind():
    path = (
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "006_thumbnails.sql"
    )
    sql = path.read_text()
    assert "ALTER TABLE memories ADD COLUMN thumbnail_jpeg BYTEA" in sql
    assert "ALTER TABLE memories ADD COLUMN media_kind VARCHAR(16)" in sql


def test_migration_007_adds_expression_index_used_by_search():
    path = (
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "007_search_fts_index.sql"
    )
    sql = path.read_text()
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in sql
    assert "to_tsvector('english', content)" in sql


# -- has_thumbnail schema field (ITEM 4a) -------------------------------------


def _fake_summary_memory(thumbnail_jpeg):
    from types import SimpleNamespace
    from datetime import datetime, timezone

    return SimpleNamespace(
        id=uuid.uuid4(),
        title="t",
        summary="s",
        category="c",
        source_item=SimpleNamespace(platform="instagram"),
        media_kind="video",
        processing_status="READY",
        thumbnail_jpeg=thumbnail_jpeg,
        created_at=datetime.now(timezone.utc),
    )


def test_has_thumbnail_field_contract():
    # The Android client consumes `has_thumbnail`; it must exist on both
    # schemas as a required bool.
    from app.schemas import MemoryDetail

    for model in (MemorySummary, MemoryDetail):
        assert "has_thumbnail" in model.model_fields, model.__name__
        assert model.model_fields["has_thumbnail"].annotation is bool
        assert model.model_fields["has_thumbnail"].is_required()


def test_summary_has_thumbnail_true_when_jpeg_present():
    from app.api.memories import _summary

    s = _summary(_fake_summary_memory(thumbnail_jpeg=b"\xff\xd8fake-jpeg"))
    assert s.has_thumbnail is True


def test_summary_has_thumbnail_false_when_jpeg_missing():
    # thumbnail_jpeg IS NULL (pre-migration-006 memory, or generation
    # failed) -> clients must not re-request a known-missing thumbnail.
    from app.api.memories import _summary

    s = _summary(_fake_summary_memory(thumbnail_jpeg=None))
    assert s.has_thumbnail is False
