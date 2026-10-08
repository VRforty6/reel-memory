"""Album/carousel capture tests — pure logic, no network, no DB.

Covers: the combined dedup hash (order-independent), the per-photo storage
layout and its detection by the upload adapter, endpoint validation helpers,
"photo N" citation labels, and the worker's per-file album processing
(each photo gets vision + OCR; every segment carries its album_index).
"""

from __future__ import annotations

import uuid
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

from app.api.captures import (
    album_combined_hash,
    album_file_name,
    validate_album_files,
)
from app.capture.canonicalize import CanonicalURL
from app.config import settings
from app.pipeline.providers import (
    OCRSegment,
    UnconfiguredMemoryGenerator,
    VisualObservation,
)
from app.pipeline.state_machine import ProcessingStatus
from app.pipeline.worker import Worker
from app.qa import EvidenceRef, album_index_from_metadata, parse_qa_response
from app.schemas import AlbumCaptureResponse
from app.sources.base import AlbumMedia, ResolvedMedia, RetryableFailure, SourceMetadata
from app.sources.upload import UploadAdapter


def _upload_file(name: str, content_type: str) -> UploadFile:
    return UploadFile(
        file=BytesIO(b"fake-bytes-" + name.encode()),
        filename=name,
        headers=Headers({"content-type": content_type}),
    )


# --- combined hash -----------------------------------------------------------


def test_album_combined_hash_is_order_independent():
    hashes = ["a" * 64, "b" * 64, "c" * 64]
    assert album_combined_hash(hashes) == album_combined_hash(list(reversed(hashes)))


def test_album_combined_hash_is_deterministic_and_sensitive():
    hashes = ["a" * 64, "b" * 64]
    assert album_combined_hash(hashes) == album_combined_hash(hashes)
    assert album_combined_hash(hashes) != album_combined_hash(["a" * 64, "d" * 64])
    assert len(album_combined_hash(hashes)) == 64


# --- storage layout ----------------------------------------------------------


def test_album_file_name_round_trips_through_adapter_regex():
    name = album_file_name("f" * 64, 6, "e" * 64, ".jpg")
    assert name == f"{'f' * 64}.06.{'e' * 64}.jpg"
    matches = UploadAdapter._match_album("f" * 64, [])
    assert matches is None  # no files -> not an album


def test_match_album_orders_by_index_and_ignores_others(tmp_path):
    combined = "c" * 64
    names = [
        album_file_name(combined, 2, "a" * 64, ".png"),
        album_file_name(combined, 0, "b" * 64, ".jpg"),
        album_file_name(combined, 1, "d" * 64, ".webp"),
        "unrelated.txt",
    ]
    paths = [tmp_path / n for n in names]
    ordered = UploadAdapter._match_album(combined, paths)
    assert ordered == [str(paths[1]), str(paths[2]), str(paths[0])]


def test_match_album_returns_none_for_single_upload(tmp_path):
    path = tmp_path / f"{'a' * 64}.mp4"
    assert UploadAdapter._match_album("a" * 64, [path]) is None


def test_resolve_album_returns_album_media_in_order(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    combined = "9" * 64
    for idx, ext in ((0, ".jpg"), (1, ".png"), (2, ".webp")):
        (tmp_path / album_file_name(combined, idx, f"{idx}" * 64, ext)).write_bytes(b"x")
    canon = CanonicalURL(
        platform="upload", platform_item_id=combined,
        canonical_url=f"upload://album/{combined}", original_url="album",
    )
    result = UploadAdapter().resolve(canon)
    assert isinstance(result, AlbumMedia)
    assert len(result.files) == 3
    assert result.files[0].endswith(".00." + "0" * 64 + ".jpg")
    assert result.files[2].endswith(".02." + "2" * 64 + ".webp")


def test_resolve_single_upload_still_works(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    digest = "a" * 64
    (tmp_path / f"{digest}.mp4").write_bytes(b"x")
    canon = CanonicalURL(
        platform="upload", platform_item_id=digest,
        canonical_url=f"upload://{digest}", original_url="reel.mp4",
    )
    result = UploadAdapter().resolve(canon)
    assert isinstance(result, ResolvedMedia)
    assert result.media_path.endswith(f"{digest}.mp4")


def test_resolve_missing_still_retryable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    canon = CanonicalURL(
        platform="upload", platform_item_id="b" * 64,
        canonical_url="upload://album/" + "b" * 64, original_url="album",
    )
    result = UploadAdapter().resolve(canon)
    assert isinstance(result, RetryableFailure)


# --- endpoint validation -----------------------------------------------------


def test_validate_album_files_rejects_too_few_and_too_many():
    with pytest.raises(HTTPException) as e1:
        validate_album_files([_upload_file("a.png", "image/png")])
    assert e1.value.status_code == 422
    assert e1.value.detail["code"] == "ALBUM_FILE_COUNT"

    with pytest.raises(HTTPException) as e2:
        validate_album_files(
            [_upload_file(f"{i}.png", "image/png") for i in range(31)]
        )
    assert e2.value.status_code == 422


def test_validate_album_files_accepts_bounds():
    validate_album_files(
        [_upload_file("a.png", "image/png"), _upload_file("b.jpg", "image/jpeg")]
    )
    validate_album_files(
        [_upload_file(f"{i}.png", "image/png") for i in range(30)]
    )


def test_validate_album_files_rejects_two_videos():
    with pytest.raises(HTTPException) as exc:
        validate_album_files(
            [
                _upload_file("a.mp4", "video/mp4"),
                _upload_file("b.mp4", "video/mp4"),
                _upload_file("c.png", "image/png"),
            ]
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "ALBUM_TOO_MANY_VIDEOS"


def test_validate_album_files_allows_single_video():
    validate_album_files(
        [
            _upload_file("a.mp4", "video/mp4"),
            _upload_file("b.png", "image/png"),
        ]
    )


# --- citations ---------------------------------------------------------------


def test_evidence_label_names_the_photo():
    ref = EvidenceRef(
        modality="visual", start_ms=12000, end_ms=None,
        content="a cat", album_index=2,
    )
    assert ref.label() == "[visual photo 3 @ 12s]"
    ref2 = EvidenceRef(
        modality="ocr", start_ms=None, end_ms=None,
        content="SALE", album_index=6,
    )
    assert ref2.label() == "[ocr photo 7]"


def test_evidence_label_without_album_is_unchanged():
    ref = EvidenceRef(modality="speech", start_ms=5000, end_ms=9000, content="hi")
    assert ref.label() == "[speech @ 5s-9s]"


def test_album_index_from_metadata():
    assert album_index_from_metadata({"album_index": 4}) == 4
    assert album_index_from_metadata({"album_index": 0}) == 0
    assert album_index_from_metadata({}) is None
    assert album_index_from_metadata(None) is None
    assert album_index_from_metadata({"album_index": -1}) is None
    assert album_index_from_metadata({"album_index": "3"}) is None


def test_parse_qa_response_carries_album_index():
    parsed = parse_qa_response(
        '{"answer": "a cat", "citations": ['
        '{"modality": "visual", "timestamp_ms": 12000, "album_index": 2, '
        '"quote": "a cat"}], "evidence_coverage": "full", "claims": []}'
    )
    assert parsed.citations[0].album_index == 2


def test_parse_qa_response_rejects_bad_album_index():
    parsed = parse_qa_response(
        '{"answer": "a cat", "citations": ['
        '{"modality": "visual", "timestamp_ms": null, "album_index": -5, '
        '"quote": "a cat"}], "evidence_coverage": "full", "claims": []}'
    )
    assert parsed.citations[0].album_index is None


def test_album_capture_response_schema():
    resp = AlbumCaptureResponse(
        id=uuid.uuid4(), memory_id=uuid.uuid4(), status="QUEUED",
        duplicate=False, file_count=3,
        content_hashes=["a" * 64, "b" * 64, "c" * 64],
    )
    assert resp.file_count == 3
    assert len(resp.content_hashes) == 3


# --- worker album processing -------------------------------------------------


class _FakeQuery:
    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def delete(self):
        return 0

    def all(self):
        return []


class _FakeDB:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def query(self, *a, **k):
        return _FakeQuery()


class _StubVision:
    def __init__(self):
        self.calls: list[list[str]] = []

    def analyze_frames(self, frame_paths):
        self.calls.append(list(frame_paths))
        return [
            VisualObservation(
                timestamp_ms=0, description=f"seen {frame_paths[0]}"
            )
        ]


class _StubOCR:
    def __init__(self):
        self.calls: list[list[str]] = []

    def extract_text(self, frame_paths):
        self.calls.append(list(frame_paths))
        return [OCRSegment(timestamp_ms=0, text=f"text in {frame_paths[0]}")]


class _StubEmbedding:
    def embed(self, texts):
        return [[0.0] * settings.embedding_dim for _ in texts]


def test_process_album_runs_vision_and_ocr_per_photo(tmp_path):
    photos = []
    for i, ext in enumerate((".jpg", ".png", ".webp")):
        p = tmp_path / f"photo{i}{ext}"
        p.write_bytes(b"fake-image-bytes")
        photos.append(str(p))

    db = _FakeDB()
    memory = SimpleNamespace(
        id=uuid.uuid4(), processing_status=ProcessingStatus.RESOLVING_SOURCE.value,
        title=None, summary=None, category=None, language=None,
        processing_version=None,
    )
    job = SimpleNamespace(
        status="QUEUED", stage=None, attempt_count=0,
        failure_code=None, failure_message=None,
        started_at=None, finished_at=None,
    )
    item = SimpleNamespace(
        platform="upload", caption=None, creator_handle=None, published_at=None,
    )
    vision, ocr = _StubVision(), _StubOCR()
    providers = SimpleNamespace(
        speech=SimpleNamespace(), vision=vision, ocr=ocr,
        embedding=_StubEmbedding(),
        memory_generator=UnconfiguredMemoryGenerator(),  # -> deterministic fallback
        chat=SimpleNamespace(), search=SimpleNamespace(),
    )
    worker = Worker(session_factory=lambda: db, providers=providers)
    album = AlbumMedia(
        canonical=CanonicalURL(
            platform="upload", platform_item_id="c" * 64,
            canonical_url="upload://album/" + "c" * 64, original_url="album",
        ),
        metadata=SourceMetadata(),
        files=photos,
    )

    worker._process_album(db, memory, item, job, album, tmp_path)

    # Every photo got its own vision + OCR pass (the image itself is the frame).
    assert len(vision.calls) == 3
    assert len(ocr.calls) == 3
    assert vision.calls[1] == [photos[1]]

    # Segments carry their album_index in metadata_json.
    by_modality: dict[str, list] = {}
    for obj in db.added:
        if hasattr(obj, "modality"):
            by_modality.setdefault(obj.modality, []).append(obj)
    assert len(by_modality["visual"]) == 3
    assert len(by_modality["ocr"]) == 3
    for idx, seg in enumerate(by_modality["visual"]):
        assert seg.metadata_json == {"album_index": idx}
    for idx, seg in enumerate(by_modality["ocr"]):
        assert seg.metadata_json == {"album_index": idx}
    # No speech segments: images have no audio track.
    assert "speech" not in by_modality

    # The shared tail ran: memory generated across all photos, job READY.
    assert memory.processing_status == ProcessingStatus.READY.value
    assert memory.summary  # deterministic builder summarized the photos
