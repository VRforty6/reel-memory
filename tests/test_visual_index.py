"""Visual search (frame indexing + visual retrieval branch) tests.

Pure logic / fakes only — no torch, no live DB, no network. The real-model
retrieval quality (Recall@1/@3) is measured by qa/visual_benchmark.py, not
here.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.pipeline import frame_index
from app.pipeline.frame_index import (
    VISUAL_BACKFILL_AVAILABLE,
    VISUAL_BACKFILL_SOURCE_UNAVAILABLE,
    VISUAL_INDEX_READY,
    FrameSample,
    _classify,
    _dedupe_cuts,
    classify_visual_backfill,
    index_visual_frames,
    uniform_timestamps_ms,
)
from app.pipeline import media as media_decode
from app.pipeline.failures import FailureCode
from app.pipeline.state_machine import ProcessingStatus
from app.pipeline.visual import (
    UnconfiguredVisualEmbeddingProvider,
    VisualEmbeddingProvider,
    VisualProviderNotConfiguredError,
    build_visual_provider,
)
from app.pipeline.worker import StageError, Worker
from app.search.hybrid import (
    _fmt_ts,
    _visual_evidence_snippet,
    hybrid_search,
)


# --- fakes ------------------------------------------------------------------


class FakeVisualProvider(VisualEmbeddingProvider):
    """Deterministic joint-space stub: image i -> one-hot-ish vector,
    text query -> vector close to a chosen image."""

    dim_value = 4

    def __init__(self, query_target: int = 0) -> None:
        self.query_target = query_target

    @property
    def dim(self) -> int:
        return self.dim_value

    @property
    def model_id(self) -> str:
        return "fake:test"

    def embed_images(self, image_paths: list[str]) -> list[list[float]]:
        vecs = []
        for i, _ in enumerate(image_paths):
            v = [0.0] * self.dim_value
            v[i % self.dim_value] = 1.0
            vecs.append(v)
        return vecs

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vecs = []
        for _ in texts:
            v = [0.0] * self.dim_value
            v[self.query_target % self.dim_value] = 1.0
            vecs.append(v)
        return vecs


class FakeDB:
    """Minimal stand-in for a SQLAlchemy session (add/commit/query)."""

    def __init__(self, count: int = 0) -> None:
        self.added: list = []
        self.commits = 0
        self._count = count
        self.deleted = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.commits += 1

    def query(self, *args):
        db = self

        class _Q:
            def filter(self, *a, **k):
                return self

            def scalar(self):
                return db._count

            def delete(self):
                db.deleted += 1
                return 0

        return _Q()


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class FakeQuery:
    def __init__(self, items):
        self._items = items

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._items


class FakeSession(FakeDB):
    """FakeSession for hybrid_search: canned rows per branch + ORM objects."""

    def __init__(self, branch_rows, memories, items) -> None:
        super().__init__()
        self.branch_rows = branch_rows
        self._memories = memories
        self._items = items

    def execute(self, stmt, params):
        s = str(stmt)
        if "memory_frame_embeddings" in s:
            key = "visual"
        elif "memory_tags" in s:
            key = "tag"
        elif "to_tsvector" in s:
            key = "fts"
        else:
            key = "vec"
        return FakeResult(self.branch_rows.get(key, []))

    def query(self, model):
        from app.models import Memory

        if model is Memory:
            return FakeQuery(self._memories)
        return FakeQuery(self._items)


def _memory_ns(mid: uuid.UUID, source_item_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=mid,
        title="t",
        summary="s",
        category="c",
        source_item_id=source_item_id,
        processing_status="READY",
    )


def _item_ns(sid: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(id=sid, creator_handle="h", platform="upload",
                           source_status="UNKNOWN")


# --- pure helpers ------------------------------------------------------------


def test_uniform_timestamps_follow_fps_grid():
    # fps filter: count frames over duration -> grid t = i * duration / count.
    assert uniform_timestamps_ms(12.0, 12) == [i * 1000 for i in range(12)]
    assert uniform_timestamps_ms(60.0, 12) == [i * 5000 for i in range(12)]


def test_uniform_timestamps_degenerate():
    assert uniform_timestamps_ms(0.0, 3) == [0, 0, 0]
    assert uniform_timestamps_ms(None, 2) == [0, 0]


def test_dedupe_cuts_keeps_distinct_cuts():
    uniform = [0, 1000, 2000, 3000]
    # 150ms window: 1050 is a near-duplicate of the 1000ms sample; 420 is not.
    assert _dedupe_cuts([1050, 420, 2500], uniform) == [420, 2500]


def test_dedupe_cuts_small_window_preserves_events():
    # A cut 200ms after a uniform sample shows different content (the cut
    # itself) and must be kept — this is the whole point of supplementation.
    assert _dedupe_cuts([4200], [4000, 5000]) == [4200]


def test_fmt_ts():
    assert _fmt_ts(0) == "0:00"
    assert _fmt_ts(18000) == "0:18"
    assert _fmt_ts(125000) == "2:05"


def test_visual_evidence_snippet_never_invents_description():
    # Timestamp-grounded only: no claim about what the frame shows.
    assert _visual_evidence_snippet(18000, None) == "Visual match near 0:18"
    assert "red" not in _visual_evidence_snippet(18000, None)
    assert _visual_evidence_snippet(0, 6) == "Visual match: photo 7"


# --- frame selection ---------------------------------------------------------


def _fake_probe(duration_s=12.0):
    return media_decode.MediaProbe(
        duration_s=duration_s, width=640, height=360,
        has_audio=False, has_video=True,
    )


def test_select_video_frames_uniform_plus_scene(monkeypatch):
    monkeypatch.setattr(
        media_decode, "probe_media", lambda *a, **k: _fake_probe(12.0)
    )
    monkeypatch.setattr(
        media_decode,
        "extract_scene_keyframes",
        lambda *a, **k: [("s1.jpg", 4200), ("s2.jpg", 8700)],
    )
    uniform = [f"u{i}.jpg" for i in range(12)]
    samples = frame_index.select_video_frames("/v.mp4", "/tmp", uniform)
    assert len(samples) == 14
    assert [s.selection for s in samples].count("uniform") == 12
    assert [s.selection for s in samples].count("scene") == 2
    ts = [s.timestamp_ms for s in samples]
    assert ts == sorted(ts)  # time order
    assert 4200 in ts and 8700 in ts  # true scene timestamps kept
    assert all(s.album_index is None for s in samples)


def test_select_video_frames_bounded_and_deduped(monkeypatch):
    monkeypatch.setattr(
        media_decode, "probe_media", lambda *a, **k: _fake_probe(12.0)
    )
    # 30 cuts incl. near-duplicates of uniform samples -> capped at 12 extras.
    cuts = [(f"s{i}.jpg", i * 400) for i in range(30)]
    monkeypatch.setattr(
        media_decode, "extract_scene_keyframes", lambda *a, **k: cuts
    )
    uniform = [f"u{i}.jpg" for i in range(12)]
    samples = frame_index.select_video_frames("/v.mp4", "/tmp", uniform)
    n_scene = sum(1 for s in samples if s.selection == "scene")
    assert n_scene <= 12
    assert len(samples) <= 24
    # near-duplicate of a uniform sample is dropped
    ts = [s.timestamp_ms for s in samples if s.selection == "scene"]
    assert not any(abs(t - 1000) <= 150 for t in ts)


def test_select_video_frames_scene_failure_degrades_to_uniform(monkeypatch):
    monkeypatch.setattr(
        media_decode, "probe_media", lambda *a, **k: _fake_probe(12.0)
    )

    def _boom(*a, **k):
        raise media_decode.MediaDecodeError("scene filter exploded")

    monkeypatch.setattr(media_decode, "extract_scene_keyframes", _boom)
    uniform = [f"u{i}.jpg" for i in range(12)]
    samples = frame_index.select_video_frames("/v.mp4", "/tmp", uniform)
    assert len(samples) == 12
    assert all(s.selection == "uniform" for s in samples)


def test_select_video_frames_propagates_album_index(monkeypatch):
    monkeypatch.setattr(
        media_decode, "probe_media", lambda *a, **k: _fake_probe(12.0)
    )
    monkeypatch.setattr(
        media_decode, "extract_scene_keyframes", lambda *a, **k: []
    )
    samples = frame_index.select_video_frames(
        "/v.mp4", "/tmp", ["u0.jpg"], album_index=3
    )
    assert all(s.album_index == 3 for s in samples)


# --- index_visual_frames -----------------------------------------------------


def test_index_visual_frames_persists_rows():
    db = FakeDB()
    samples = [
        FrameSample(path="a.jpg", timestamp_ms=0, selection="uniform"),
        FrameSample(path="b.jpg", timestamp_ms=4200, selection="scene"),
    ]
    n = index_visual_frames(db, uuid.uuid4(), samples, FakeVisualProvider())
    assert n == 2
    assert len(db.added) == 2
    assert db.commits == 1
    rows = db.added
    assert [r.frame_index for r in rows] == [0, 1]
    assert [r.timestamp_ms for r in rows] == [0, 4200]
    assert [r.selection for r in rows] == ["uniform", "scene"]
    assert all(len(r.embedding) == 4 for r in rows)


def test_index_visual_frames_empty_is_noop():
    db = FakeDB()
    assert index_visual_frames(db, uuid.uuid4(), [], FakeVisualProvider()) == 0
    assert db.added == [] and db.commits == 0


def test_index_visual_frames_start_index_gives_dense_global_indices():
    """Backfill calls index_visual_frames once per album file; start_index
    keeps frame_index dense across the whole memory (no collisions)."""
    db = FakeDB()
    mid = uuid.uuid4()
    s1 = [FrameSample(path="a.jpg", timestamp_ms=0, selection="photo",
                      album_index=0)]
    s2 = [FrameSample(path="b.jpg", timestamp_ms=0, selection="photo",
                      album_index=1),
          FrameSample(path="c.jpg", timestamp_ms=0, selection="photo",
                      album_index=1)]
    n1 = index_visual_frames(db, mid, s1, FakeVisualProvider(), start_index=0)
    n2 = index_visual_frames(db, mid, s2, FakeVisualProvider(), start_index=n1)
    assert (n1, n2) == (1, 2)
    assert [r.frame_index for r in db.added] == [0, 1, 2]


def test_index_visual_frames_count_mismatch_raises():
    class BadProvider(FakeVisualProvider):
        def embed_images(self, paths):
            return [[1.0, 0, 0, 0]]  # wrong count, no matter the input

    from app.pipeline.visual import VisualProviderError

    with pytest.raises(VisualProviderError):
        index_visual_frames(
            FakeDB(),
            uuid.uuid4(),
            [FrameSample(path="a.jpg", timestamp_ms=0, selection="uniform"),
             FrameSample(path="b.jpg", timestamp_ms=1, selection="uniform")],
            BadProvider(),
        )


# --- backfill classification -------------------------------------------------


def test_classify_ready_only_from_real_rows():
    # Even a memory whose GPT vision description mentions the event is NOT
    # ready unless frame embedding rows exist.
    assert _classify(False, "upload", True)[0] == VISUAL_BACKFILL_AVAILABLE
    assert _classify(True, "upload", False)[0] == VISUAL_INDEX_READY


def test_classify_upload_file_gone():
    status, reason = _classify(False, "upload", False)
    assert status == VISUAL_BACKFILL_SOURCE_UNAVAILABLE
    assert "deleted" in reason


def test_classify_instagram_never_available():
    status, _ = _classify(False, "instagram", False)
    assert status == VISUAL_BACKFILL_SOURCE_UNAVAILABLE


def test_classify_web_article():
    status, reason = _classify(False, "web", False)
    assert status == VISUAL_BACKFILL_SOURCE_UNAVAILABLE
    assert "no video frames" in reason


def _mem_item_ns(platform="upload", item_id="abc123"):
    item = SimpleNamespace(platform=platform, platform_item_id=item_id)
    return SimpleNamespace(id=uuid.uuid4(), source_item=item)


def test_classify_visual_backfill_ready(monkeypatch):
    db = FakeDB(count=14)
    status, _ = classify_visual_backfill(db, _mem_item_ns())
    assert status == VISUAL_INDEX_READY


def test_classify_visual_backfill_upload_present(monkeypatch):
    db = FakeDB(count=0)
    monkeypatch.setattr(
        frame_index, "_upload_source_files_present", lambda pid: True
    )
    status, _ = classify_visual_backfill(db, _mem_item_ns("upload"))
    assert status == VISUAL_BACKFILL_AVAILABLE


def test_classify_visual_backfill_upload_gone(monkeypatch):
    db = FakeDB(count=0)
    monkeypatch.setattr(
        frame_index, "_upload_source_files_present", lambda pid: False
    )
    status, reason = classify_visual_backfill(db, _mem_item_ns("upload"))
    assert status == VISUAL_BACKFILL_SOURCE_UNAVAILABLE
    assert "re-upload" in reason


# --- visual provider ---------------------------------------------------------


def test_unconfigured_visual_provider_is_loud():
    p = UnconfiguredVisualEmbeddingProvider()
    with pytest.raises(VisualProviderNotConfiguredError):
        p.embed_images(["x.jpg"])
    with pytest.raises(VisualProviderNotConfiguredError):
        p.embed_texts(["q"])
    with pytest.raises(VisualProviderNotConfiguredError):
        _ = p.dim


def test_build_visual_provider_unknown_name(monkeypatch):
    import app.pipeline.visual as visual_mod
    from app.config import settings

    monkeypatch.setattr(settings, "visual_embedding_provider", "wat")
    with pytest.raises(VisualProviderNotConfiguredError):
        build_visual_provider()


# --- worker hooks ------------------------------------------------------------


def test_worker_skips_visual_when_disabled(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "visual_embedding_provider", "none")
    w = Worker(lambda: None)
    assert w._select_visual_samples("/v.mp4", "/tmp", ["f.jpg"], None) == []
    # no-op on empty: never touches the provider
    w._index_visual_frames(FakeDB(), SimpleNamespace(id=uuid.uuid4()), [])


def test_worker_photo_is_single_sample(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "visual_embedding_provider", "openclip")
    w = Worker(lambda: None)
    samples = w._select_visual_samples(
        "/shot.PNG", "/tmp", ["/shot.PNG"], album_index=2
    )
    assert len(samples) == 1
    assert samples[0].selection == "photo"
    assert samples[0].timestamp_ms == 0
    assert samples[0].album_index == 2


def test_worker_dim_mismatch_fails_loud(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "visual_embedding_provider", "openclip")
    monkeypatch.setattr(settings, "visual_embedding_dim", 512)

    class Dim4Provider(FakeVisualProvider):
        dim_value = 4

    import app.pipeline.worker as worker_mod

    monkeypatch.setattr(
        worker_mod, "build_visual_provider", lambda: Dim4Provider()
    )
    w = Worker(lambda: None)
    with pytest.raises(StageError) as exc:
        w._index_visual_frames(
            FakeDB(),
            SimpleNamespace(id=uuid.uuid4()),
            [FrameSample(path="a.jpg", timestamp_ms=0, selection="uniform")],
        )
    assert exc.value.code == FailureCode.VISUAL_INDEX_FAILED


def test_worker_unconfigured_provider_fails_loud(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "visual_embedding_provider", "openclip")
    import app.pipeline.worker as worker_mod

    monkeypatch.setattr(
        worker_mod,
        "build_visual_provider",
        lambda: UnconfiguredVisualEmbeddingProvider(),
    )
    w = Worker(lambda: None)
    with pytest.raises(StageError) as exc:
        w._index_visual_frames(
            FakeDB(),
            SimpleNamespace(id=uuid.uuid4()),
            [FrameSample(path="a.jpg", timestamp_ms=0, selection="uniform")],
        )
    assert exc.value.code == FailureCode.VISUAL_INDEX_FAILED


# --- hybrid_search visual branch ---------------------------------------------


def _session_with_visual_only():
    mid = uuid.uuid4()
    sid = uuid.uuid4()
    visual_rows = [
        {
            "memory_id": str(mid),
            "distance": 0.1,
            "timestamp_ms": 18000,
            "frame_index": 7,
            "album_index": None,
        }
    ]
    return (
        FakeSession(
            {"visual": visual_rows},
            [_memory_ns(mid, sid)],
            [_item_ns(sid)],
        ),
        mid,
    )


def test_visual_branch_retrieves_without_text_signals():
    """Acceptance plumbing: a reel whose transcript/OCR/caption/vision text
    say nothing relevant is still retrieved through frame embeddings."""
    session, mid = _session_with_visual_only()
    hits = hybrid_search(
        session,
        uuid.uuid4(),
        "guy writing with 2 pens on a red sticky note",
        query_embedding=None,  # no text vector branch
        query_visual_embedding=[0.0, 1.0, 0.0, 0.0],
    )
    assert len(hits) == 1
    assert hits[0].memory_id == mid
    visual_ev = [e for e in hits[0].evidence if e.type == "VISUAL"]
    assert len(visual_ev) == 1
    assert visual_ev[0].snippet == "Visual match near 0:18"


def test_visual_branch_skipped_when_no_visual_embedding():
    session, _ = _session_with_visual_only()
    hits = hybrid_search(
        session, uuid.uuid4(), "q", query_embedding=None,
        query_visual_embedding=None,
    )
    assert hits == []


def test_visual_evidence_dedupes_against_gpt_visual_segment():
    """When a GPT 'visual' text segment already gave VISUAL evidence, the
    frame branch does not add a second VISUAL entry."""
    mid = uuid.uuid4()
    sid = uuid.uuid4()
    session = FakeSession(
        {
            "fts": [
                {
                    "memory_id": str(mid),
                    "modality": "visual",
                    "content": "a person writing at a desk",
                    "start_ms": None,
                    "end_ms": None,
                }
            ],
            "visual": [
                {
                    "memory_id": str(mid),
                    "distance": 0.1,
                    "timestamp_ms": 18000,
                    "frame_index": 7,
                    "album_index": None,
                }
            ],
        },
        [_memory_ns(mid, sid)],
        [_item_ns(sid)],
    )
    hits = hybrid_search(
        session, uuid.uuid4(), "writing",
        query_embedding=None,
        query_visual_embedding=[0.0, 1.0, 0.0, 0.0],
    )
    assert len(hits) == 1
    visual_ev = [e for e in hits[0].evidence if e.type == "VISUAL"]
    assert len(visual_ev) == 1
    # the GPT description wins the single VISUAL slot (first branch wins)
    assert "person writing" in visual_ev[0].snippet


def test_visual_branch_participates_in_rrf():
    """A memory matching ONLY the visual branch still ranks via RRF."""
    mid_a, mid_b = uuid.uuid4(), uuid.uuid4()
    sid = uuid.uuid4()
    session = FakeSession(
        {
            "tag": [{"memory_id": str(mid_a), "tag": "cooking"}],
            "visual": [
                {
                    "memory_id": str(mid_b),
                    "distance": 0.05,
                    "timestamp_ms": 4000,
                    "frame_index": 2,
                    "album_index": None,
                }
            ],
        },
        [_memory_ns(mid_a, sid), _memory_ns(mid_b, sid)],
        [_item_ns(sid)],
    )
    hits = hybrid_search(
        session, uuid.uuid4(), "red sticky note",
        query_embedding=None,
        query_visual_embedding=[0.0, 1.0, 0.0, 0.0],
        limit=10,
    )
    ids = [h.memory_id for h in hits]
    assert mid_b in ids  # visual-only memory is retrievable


def test_visual_album_evidence_names_photo():
    mid = uuid.uuid4()
    sid = uuid.uuid4()
    session = FakeSession(
        {
            "visual": [
                {
                    "memory_id": str(mid),
                    "distance": 0.1,
                    "timestamp_ms": 0,
                    "frame_index": 0,
                    "album_index": 6,
                }
            ]
        },
        [_memory_ns(mid, sid)],
        [_item_ns(sid)],
    )
    hits = hybrid_search(
        session, uuid.uuid4(), "q", query_embedding=None,
        query_visual_embedding=[1.0, 0, 0, 0],
    )
    assert hits[0].evidence[0].snippet == "Visual match: photo 7"


# --- regression: existing behavior untouched ----------------------------------


def test_hybrid_search_without_visual_kwarg_still_works():
    """Old callers (3-branch) keep working: visual branch defaults off."""
    mid = uuid.uuid4()
    sid = uuid.uuid4()
    session = FakeSession(
        {"fts": [{"memory_id": str(mid), "modality": "speech",
                  "content": "hello world",
                  "start_ms": 1000, "end_ms": 2500}]},
        [_memory_ns(mid, sid)],
        [_item_ns(sid)],
    )
    hits = hybrid_search(session, uuid.uuid4(), "hello", query_embedding=None)
    assert len(hits) == 1
    assert hits[0].evidence[0].type == "SPEECH"


# --- regression: search never decodes media / calls paid vision ----------------


def test_search_path_imports_no_media_or_paid_vision():
    """Static guard: the query-time search modules must not import ffmpeg,
    the vision providers, or paid API SDKs. Visual search embeds the query
    text locally and reads stored frame rows only."""
    import ast
    import pathlib

    for mod in ("app/api/search.py", "app/search/hybrid.py"):
        tree = ast.parse(pathlib.Path(mod).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for forbidden in ("ffmpeg", "openai", "anthropic"):
            assert forbidden not in imported, f"{mod} imports {forbidden!r}"
        src = pathlib.Path(mod).read_text()
        assert "app.pipeline.media" not in src
        assert "providers.vision" not in src


def test_search_never_embeds_images_or_touches_media(monkeypatch):
    """Behavioral guard: query-time visual embedding (app/api/search.py)
    must only call embed_texts — image embedding is ingest-only, and no
    media decoding happens. hybrid_search itself takes a precomputed
    vector and cannot reach a provider at all."""
    from app.api import search as search_api
    from app.config import settings
    import app.pipeline.visual as visual_mod

    class StrictProvider(FakeVisualProvider):
        def embed_images(self, paths):
            raise AssertionError("search must never embed images")

    monkeypatch.setattr(settings, "visual_embedding_provider", "openclip")
    monkeypatch.setattr(visual_mod, "build_visual_provider",
                        lambda: StrictProvider())
    vec = search_api._maybe_embed_visual("red motorcycle")
    assert vec == [1.0, 0.0, 0.0, 0.0]  # embed_texts only; no image path taken

    # hybrid_search takes a plain vector — no provider, no media module.
    session, mid = _session_with_visual_only()
    hits = hybrid_search(
        session, uuid.uuid4(), "q", query_embedding=None,
        query_visual_embedding=vec,
    )
    assert len(hits) == 1 and hits[0].memory_id == mid


def test_processing_statuses_unchanged():
    # No new pipeline stage was added; visual indexing rides ANALYZING_VISUALS.
    assert ProcessingStatus.ANALYZING_VISUALS.value == "ANALYZING_VISUALS"


# --- provider singleton cache ------------------------------------------------


def test_build_visual_provider_caches_openclip_singleton(monkeypatch):
    """Repeated build_visual_provider() calls reuse one instance, so the
    ~578MB model is loaded once per process instead of on every search."""
    import app.pipeline.visual as visual_mod
    from app.config import settings

    monkeypatch.setattr(settings, "visual_embedding_provider", "openclip")
    visual_mod._clear_visual_provider_cache()
    try:
        p1 = build_visual_provider()
        p2 = build_visual_provider()
        assert p1 is p2
        assert isinstance(p1, visual_mod.OpenCLIPVisualEmbeddingProvider)
        # The model itself is still lazy: constructing/caching must not load it.
        assert p1._model is None
    finally:
        visual_mod._clear_visual_provider_cache()


def test_build_visual_provider_cache_keyed_on_model_config(monkeypatch):
    import app.pipeline.visual as visual_mod
    from app.config import settings

    monkeypatch.setattr(settings, "visual_embedding_provider", "openclip")
    visual_mod._clear_visual_provider_cache()
    try:
        p1 = build_visual_provider()
        monkeypatch.setattr(settings, "visual_embedding_model", "ViT-L-14")
        p2 = build_visual_provider()
        assert p2 is not p1
        # Switching back resolves to the original cached instance.
        monkeypatch.setattr(settings, "visual_embedding_model", "ViT-B-32")
        assert build_visual_provider() is p1
    finally:
        monkeypatch.setattr(settings, "visual_embedding_model", "ViT-B-32")
        visual_mod._clear_visual_provider_cache()


def test_build_visual_provider_none_stub_not_cached(monkeypatch):
    import app.pipeline.visual as visual_mod
    from app.config import settings

    monkeypatch.setattr(settings, "visual_embedding_provider", "none")
    visual_mod._clear_visual_provider_cache()
    try:
        s1 = build_visual_provider()
        s2 = build_visual_provider()
        assert isinstance(s1, UnconfiguredVisualEmbeddingProvider)
        assert s1 is not s2  # stateless loud stub: never cached
        assert len(visual_mod._provider_cache) == 0
    finally:
        visual_mod._clear_visual_provider_cache()
