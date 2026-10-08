"""Tests for Milestone 4 provider wiring: factory selection, hashing embedder,
and OpenAI-compatible HTTP providers. All network access is mocked — zero live
calls."""

from __future__ import annotations

import io
import json
import math
import urllib.error

import pytest

from app.config import settings
from app.pipeline import providers_http
from app.pipeline.providers import (
    DeterministicMemoryGenerator,
    GeneratedMemory,
    HashingEmbeddingProvider,
    PipelineEvidence,
    ProviderError,
    ProviderNotConfiguredError,
    Providers,
    TranscriptSegment,
    build_providers,
)


# --- hashing embedder --------------------------------------------------------


def test_hashing_embedder_deterministic_and_normalized():
    p = HashingEmbeddingProvider(dim=64)
    a = p.embed(["red motorcycle number 46"])
    b = p.embed(["red motorcycle number 46"])
    assert a == b
    assert p.dim == 64
    norm = math.sqrt(sum(v * v for v in a[0]))
    assert norm == pytest.approx(1.0)
    c = p.embed(["completely different cooking recipe"])[0]
    assert a[0] != c  # distinct inputs -> distinct vectors


def test_hashing_embedder_blank_text_deterministic():
    # Blank-ish inputs are deterministic per input (padding means "" and "   "
    # legitimately differ); empty string yields the zero vector.
    p = HashingEmbeddingProvider(dim=32)
    assert p.embed([""])[0] == [0.0] * 32
    assert p.embed(["   "])[0] == p.embed(["   "])[0]


def test_hashing_embedder_default_dim_matches_settings():
    p = HashingEmbeddingProvider()
    assert p.dim == settings.embedding_dim


# --- factory -----------------------------------------------------------------


def _defaults(**overrides):
    """Snapshot + override settings attributes, restoring afterwards."""
    saved = {}
    for key, value in overrides.items():
        saved[key] = getattr(settings, key)
        setattr(settings, key, value)
    return saved


def _restore(saved):
    for key, value in saved.items():
        setattr(settings, key, value)


def test_factory_defaults_are_stubs():
    saved = _defaults()
    try:
        p = build_providers()
        assert isinstance(p, Providers)
        assert type(p.speech).__name__ == "UnconfiguredSpeechProvider"
        assert type(p.embedding).__name__ == "UnconfiguredEmbeddingProvider"
        assert isinstance(p.memory_generator, DeterministicMemoryGenerator)
    finally:
        _restore(saved)


def test_factory_hash_embedding():
    saved = _defaults(embedding_provider="hash")
    try:
        p = build_providers()
        assert isinstance(p.embedding, HashingEmbeddingProvider)
    finally:
        _restore(saved)


def test_factory_unknown_name_fails_loudly():
    saved = _defaults(speech_provider="whisperx")
    try:
        with pytest.raises(ProviderNotConfiguredError):
            build_providers()
    finally:
        _restore(saved)


def test_factory_openai_without_key_fails_loudly():
    saved = _defaults(speech_provider="openai", openai_api_key=None)
    try:
        with pytest.raises(ProviderNotConfiguredError) as exc:
            build_providers()
        assert "OPENAI_API_KEY" in str(exc.value)
    finally:
        _restore(saved)


def test_factory_openai_with_key_selects_http_providers():
    saved = _defaults(
        speech_provider="openai",
        vision_provider="openai",
        ocr_provider="openai",
        embedding_provider="openai",
        memory_generator_provider="openai",
        openai_api_key="test-key",
    )
    try:
        p = build_providers()
        assert isinstance(p.speech, providers_http.OpenAISpeechProvider)
        assert isinstance(p.vision, providers_http.OpenAIVisionProvider)
        assert isinstance(p.ocr, providers_http.OpenAIOCRProvider)
        assert isinstance(p.embedding, providers_http.OpenAIEmbeddingProvider)
        assert isinstance(p.memory_generator, providers_http.OpenAIMemoryGenerator)
        assert p.embedding.dim == settings.embedding_dim
    finally:
        _restore(saved)


def test_deterministic_memory_generator_wraps_builder():
    gen = DeterministicMemoryGenerator()
    ev = PipelineEvidence(
        transcript=[TranscriptSegment(0, 1000, "how to cook perfect pasta")]
    )
    out = gen.generate(ev)
    assert isinstance(out, GeneratedMemory)
    assert out.title  # non-empty deterministic output


# --- HTTP client --------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _mock_urlopen(monkeypatch, payload_obj=None, raw=None, error=None):
    def fake(req, timeout=None):
        if error is not None:
            raise error
        body = raw if raw is not None else json.dumps(payload_obj).encode()
        return _FakeResponse(body)

    monkeypatch.setattr(urllib.request, "urlopen", fake)


def _with_key(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "openai_base_url", "https://example.test/v1")


def test_client_rate_limit_classified(monkeypatch):
    _with_key(monkeypatch)
    err = urllib.error.HTTPError(
        "https://example.test/v1/x", 429, "Too Many", {}, io.BytesIO(b"slow down")
    )
    _mock_urlopen(monkeypatch, error=err)
    with pytest.raises(ProviderError) as exc:
        providers_http.OpenAICompatClient("https://example.test/v1", "k").post_json(
            "/x", {}
        )
    assert "rate-limited" in str(exc.value)


def test_client_auth_rejection_names_key(monkeypatch):
    _with_key(monkeypatch)
    err = urllib.error.HTTPError(
        "https://example.test/v1/x", 401, "Unauthorized", {}, io.BytesIO(b"bad key")
    )
    _mock_urlopen(monkeypatch, error=err)
    with pytest.raises(ProviderError) as exc:
        providers_http.OpenAICompatClient("https://example.test/v1", "k").post_json(
            "/x", {}
        )
    assert "OPENAI_API_KEY" in str(exc.value)


def test_client_unreachable(monkeypatch):
    _with_key(monkeypatch)
    _mock_urlopen(monkeypatch, error=urllib.error.URLError("no route"))
    with pytest.raises(ProviderError) as exc:
        providers_http.OpenAICompatClient("https://example.test/v1", "k").post_json(
            "/x", {}
        )
    assert "unreachable" in str(exc.value)


# --- speech provider -----------------------------------------------------------


def test_speech_parses_verbose_segments(monkeypatch, tmp_path):
    _with_key(monkeypatch)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF....")
    _mock_urlopen(
        monkeypatch,
        {
            "text": "hello world",
            "segments": [
                {"start": 0.0, "end": 2.5, "text": "hello"},
                {"start": 2.5, "end": 5.0, "text": "world"},
            ],
        },
    )
    out = providers_http.OpenAISpeechProvider().transcribe(str(audio))
    assert [(s.start_ms, s.end_ms, s.text) for s in out] == [
        (0, 2500, "hello"),
        (2500, 5000, "world"),
    ]


def test_speech_falls_back_to_plain_text(monkeypatch, tmp_path):
    _with_key(monkeypatch)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF....")
    _mock_urlopen(monkeypatch, {"text": "just talking"})
    out = providers_http.OpenAISpeechProvider().transcribe(str(audio))
    assert len(out) == 1 and out[0].text == "just talking"


# --- vision / OCR ---------------------------------------------------------------


def _chat_payload(content: str):
    return {"choices": [{"message": {"content": content}}]}


def test_vision_parses_frame_lines(monkeypatch, tmp_path):
    _with_key(monkeypatch)
    frames = []
    for i in range(2):
        f = tmp_path / f"f{i}.jpg"
        f.write_bytes(b"\xff\xd8fake")
        frames.append(str(f))
    _mock_urlopen(
        monkeypatch,
        _chat_payload("Frame 0: a red motorcycle\nFrame 1: a rider waving\n"),
    )
    out = providers_http.OpenAIVisionProvider().analyze_frames(frames)
    assert [o.description for o in out] == ["a red motorcycle", "a rider waving"]
    assert out[0].timestamp_ms == 0 and out[1].timestamp_ms == 5000


def test_vision_empty_frames_no_call(monkeypatch):
    _with_key(monkeypatch)
    assert providers_http.OpenAIVisionProvider().analyze_frames([]) == []


def test_ocr_skips_empty_frames(monkeypatch, tmp_path):
    _with_key(monkeypatch)
    f = tmp_path / "f0.jpg"
    f.write_bytes(b"\xff\xd8fake")
    _mock_urlopen(
        monkeypatch,
        _chat_payload("Frame 0: 50% OFF SALE\nFrame 1: (no text)\n"),
    )
    out = providers_http.OpenAIOCRProvider().extract_text([str(f)])
    assert len(out) == 1 and out[0].text == "50% OFF SALE"


# --- embeddings ------------------------------------------------------------------


def test_embedding_order_and_dimensions_param(monkeypatch):
    _with_key(monkeypatch)
    monkeypatch.setattr(settings, "openai_embedding_model", "text-embedding-3-small")
    monkeypatch.setattr(settings, "embedding_dim", 4)
    seen_payloads = []

    def fake(req, timeout=None):
        payload = json.loads(req.data.decode())
        seen_payloads.append(payload)
        data = [
            {"index": i, "embedding": [float(i)] * 4}
            for i in range(len(payload["input"]))
        ]
        return _FakeResponse(json.dumps({"data": data}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    out = providers_http.OpenAIEmbeddingProvider().embed(["a", "b", "c"])
    assert out == [[0.0] * 4, [1.0] * 4, [2.0] * 4]
    assert seen_payloads[0]["dimensions"] == 4  # requested to match pgvector column


def test_embedding_count_mismatch_raises(monkeypatch):
    _with_key(monkeypatch)
    _mock_urlopen(monkeypatch, {"data": [{"index": 0, "embedding": [0.1]}]})
    with pytest.raises(ProviderError):
        providers_http.OpenAIEmbeddingProvider().embed(["a", "b"])


# --- memory generator -------------------------------------------------------------


def test_memory_generator_parses_json(monkeypatch):
    _with_key(monkeypatch)
    _mock_urlopen(
        monkeypatch,
        _chat_payload(
            json.dumps(
                {
                    "title": "Pasta masterclass",
                    "summary": "A chef shows a quick pasta recipe.",
                    "category": "food",
                    "tags": ["pasta", "recipe"],
                    "language": "en",
                }
            )
        ),
    )
    out = providers_http.OpenAIMemoryGenerator().generate(PipelineEvidence())
    assert out.title == "Pasta masterclass"
    assert out.category == "food"
    assert out.tags == ["pasta", "recipe"]
    assert out.language == "en"


def test_memory_generator_unknown_category_defaults_other(monkeypatch):
    _with_key(monkeypatch)
    _mock_urlopen(
        monkeypatch,
        _chat_payload(
            json.dumps(
                {
                    "title": "x",
                    "summary": "y",
                    "category": "not-a-category",
                    "tags": [],
                }
            )
        ),
    )
    out = providers_http.OpenAIMemoryGenerator().generate(PipelineEvidence())
    assert out.category == "other"


def test_memory_generator_non_json_raises(monkeypatch):
    _with_key(monkeypatch)
    _mock_urlopen(monkeypatch, _chat_payload("sorry, no json here"))
    with pytest.raises(ProviderError):
        providers_http.OpenAIMemoryGenerator().generate(PipelineEvidence())


# --- worker classification honours provider hint ----------------------------------


def test_worker_call_honours_provider_failure_code():
    from app.pipeline.failures import FailureCode
    from app.pipeline.providers import ProviderError
    from app.pipeline.worker import StageError, Worker

    def boom():
        raise ProviderError("rate limited", FailureCode.TRANSCRIPTION_FAILED)

    worker = Worker.__new__(Worker)  # no DB needed: _call is pure
    with pytest.raises(StageError) as exc:
        worker._call(FailureCode.VISION_FAILED, boom)
    assert exc.value.code == FailureCode.TRANSCRIPTION_FAILED


def test_worker_call_provider_error_defaults_to_stage_code():
    from app.pipeline.failures import FailureCode
    from app.pipeline.providers import ProviderError
    from app.pipeline.worker import StageError, Worker

    def boom():
        raise ProviderError("boom")

    worker = Worker.__new__(Worker)
    with pytest.raises(StageError) as exc:
        worker._call(FailureCode.OCR_FAILED, boom)
    assert exc.value.code == FailureCode.OCR_FAILED
