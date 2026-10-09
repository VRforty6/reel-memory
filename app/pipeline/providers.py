"""AI provider interfaces — PRD §35.

All AI services sit behind these ABCs so models can be compared and swapped
(cloud vs local, cheap vs quality) without touching business logic (PRD §36).

Concrete implementations:
  * app.pipeline.providers_http — OpenAI-compatible cloud providers
    (transcription, vision, OCR, embeddings, memory generation), selected by
    *_PROVIDER env settings via build_providers() below.
  * HashingEmbeddingProvider (here) — deterministic, keyless, offline-safe
    embedding for development and retrieval QA. NOT semantic-grade; document
    any QA run that uses it.

SECURITY (SEC-008): transcript, OCR, captions and video content are UNTRUSTED
DATA. They must never be interpolated into control paths, system prompts, tool
calls, or permission decisions. Implementations must treat strings like
"Ignore previous instructions..." as video content, not instructions. The
structured evidence types below are the boundary: providers consume and produce
data, never commands.
"""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from app.pipeline.failures import FailureCode
from app.sources.base import SourceMetadata


# --- Structured evidence types (the provider boundary) ---


@dataclass
class TranscriptSegment:
    start_ms: int
    end_ms: int
    text: str  # untrusted data (SEC-008)
    lang: Optional[str] = None


@dataclass
class VisualObservation:
    timestamp_ms: int
    description: str  # untrusted data (SEC-008)
    confidence: Optional[float] = None


@dataclass
class OCRSegment:
    timestamp_ms: int
    text: str  # untrusted data (SEC-008)


@dataclass
class GeneratedMemory:
    title: str
    summary: str
    category: str
    tags: list[str] = field(default_factory=list)
    language: Optional[str] = None


@dataclass
class PipelineEvidence:
    """Everything the memory generator may use. All text fields are untrusted."""

    transcript: list[TranscriptSegment] = field(default_factory=list)
    visuals: list[VisualObservation] = field(default_factory=list)
    ocr: list[OCRSegment] = field(default_factory=list)
    metadata: Optional[SourceMetadata] = None


# --- Provider interfaces ---


class ProviderNotConfiguredError(RuntimeError):
    """Raised by stub providers. Message names what is missing and how to fix it."""


class ProviderError(RuntimeError):
    """A configured provider failed at call time (network, auth, rate limit,
    malformed response). Carries an optional FailureCode hint; the worker maps
    it to the stage's classified code when absent."""

    def __init__(
        self, message: str, failure_code: Optional[FailureCode] = None
    ) -> None:
        super().__init__(message)
        self.failure_code = failure_code


class SpeechProvider(ABC):
    @abstractmethod
    def transcribe(self, audio_path: str) -> list[TranscriptSegment]:
        """Transcribe normalized audio -> timestamped segments."""
        raise NotImplementedError


class VisionProvider(ABC):
    @abstractmethod
    def analyze_frames(self, frame_paths: list[str]) -> list[VisualObservation]:
        """Describe representative frames -> visual observations."""
        raise NotImplementedError


class OCRProvider(ABC):
    @abstractmethod
    def extract_text(self, frame_paths: list[str]) -> list[OCRSegment]:
        """Extract on-screen text -> timestamped OCR segments."""
        raise NotImplementedError


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts -> one vector per input, in order."""
        raise NotImplementedError

    @property
    @abstractmethod
    def dim(self) -> int:
        raise NotImplementedError


class MemoryGenerator(ABC):
    @abstractmethod
    def generate(self, evidence: PipelineEvidence) -> GeneratedMemory:
        """Assemble title/summary/category/tags from multimodal evidence."""
        raise NotImplementedError


class ChatProvider(ABC):
    @abstractmethod
    def complete(self, system: str, user: str) -> str:
        """Single-turn chat completion -> assistant text. Used for Q&A over a
        memory's evidence. The system prompt carries the SEC-008 framing; the
        user message carries untrusted evidence, clearly delimited."""
        raise NotImplementedError


@dataclass
class SearchResultItem:
    title: str
    url: str
    snippet: str  # untrusted third-party content (SEC-008)


class SearchProvider(ABC):
    @abstractmethod
    def search(self, query: str, max_results: int = 5) -> list[SearchResultItem]:
        """Web search -> ranked results. Used only to cross-check factual
        claims in verify mode; never presented as what the reel says."""
        raise NotImplementedError


# --- Unconfigured stubs: fail loudly, never silently ---


def _not_configured(what: str) -> ProviderNotConfiguredError:
    return ProviderNotConfiguredError(
        f"{what} provider is not configured. Wire a real implementation of the "
        f"corresponding ABC in app.pipeline.providers (Milestone 4) and pass it "
        f"to the worker; see README 'What's stubbed and why'."
    )


class UnconfiguredSpeechProvider(SpeechProvider):
    def transcribe(self, audio_path: str) -> list[TranscriptSegment]:
        raise _not_configured("Speech")


class UnconfiguredVisionProvider(VisionProvider):
    def analyze_frames(self, frame_paths: list[str]) -> list[VisualObservation]:
        raise _not_configured("Vision")


class UnconfiguredOCRProvider(OCRProvider):
    def extract_text(self, frame_paths: list[str]) -> list[OCRSegment]:
        raise _not_configured("OCR")


class UnconfiguredEmbeddingProvider(EmbeddingProvider):
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise _not_configured("Embedding")

    @property
    def dim(self) -> int:
        raise _not_configured("Embedding")


class UnconfiguredMemoryGenerator(MemoryGenerator):
    def generate(self, evidence: PipelineEvidence) -> GeneratedMemory:
        raise _not_configured("MemoryGenerator")


class UnconfiguredChatProvider(ChatProvider):
    def complete(self, system: str, user: str) -> str:
        raise _not_configured(
            "Chat (set CHAT_PROVIDER=openai and OPENAI_API_KEY; see .env.example)"
        )


class UnconfiguredSearchProvider(SearchProvider):
    def search(self, query: str, max_results: int = 5) -> list[SearchResultItem]:
        raise _not_configured(
            "Search (set SEARCH_PROVIDER=tavily and SEARCH_API_KEY; see .env.example)"
        )


@dataclass
class Providers:
    """Bundle passed to the worker. Defaults are loud stubs (never silent no-ops)."""

    speech: SpeechProvider = field(default_factory=UnconfiguredSpeechProvider)
    vision: VisionProvider = field(default_factory=UnconfiguredVisionProvider)
    ocr: OCRProvider = field(default_factory=UnconfiguredOCRProvider)
    embedding: EmbeddingProvider = field(default_factory=UnconfiguredEmbeddingProvider)
    memory_generator: MemoryGenerator = field(
        default_factory=UnconfiguredMemoryGenerator
    )
    chat: ChatProvider = field(default_factory=UnconfiguredChatProvider)
    search: SearchProvider = field(default_factory=UnconfiguredSearchProvider)


# --- Keyless local providers ------------------------------------------------


class HashingEmbeddingProvider(EmbeddingProvider):
    """Deterministic char n-gram hashing embedding (offline / QA use only).

    Buckets hashed 3-5-grams into `dim` dimensions with raw-count weighting and
    L2 normalization. Deterministic across processes (md5, not hash()) so QA
    corpora are reproducible. Captures lexical overlap, NOT semantics — any
    benchmark using it must say so (see RETRIEVAL-QA.md).
    """

    def __init__(self, dim: Optional[int] = None) -> None:
        from app.config import settings  # local import: avoid import cycle

        self._dim = dim or settings.embedding_dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        lowered = f" {text.lower()} "
        for n in (3, 4, 5):
            for i in range(len(lowered) - n + 1):
                gram = lowered[i : i + n]
                bucket = int(hashlib.md5(gram.encode()).hexdigest(), 16) % self._dim
                vec[bucket] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


class DeterministicMemoryGenerator(MemoryGenerator):
    """Wraps the deterministic build_memory() fallback as a real provider."""

    def generate(self, evidence: PipelineEvidence) -> GeneratedMemory:
        from app.pipeline.memory_builder import build_memory  # local: no cycle

        return build_memory(evidence)


# --- Factory: env names -> provider bundle ----------------------------------

_VALID_CHOICES = {
    "speech": ("none", "openai", "faster-whisper"),
    "vision": ("none", "openai"),
    "ocr": ("none", "openai", "rapidocr"),
    "embedding": ("none", "openai", "hash"),
    "memory_generator": ("none", "openai", "deterministic"),
    "chat": ("none", "openai"),
    "search": ("none", "tavily"),
}


def build_providers() -> Providers:
    """Build the provider bundle from settings (*_PROVIDER env vars).

    "none" (default) keeps the loud Unconfigured* stubs so nothing silently
    no-ops. "openai" requires OPENAI_API_KEY (any OpenAI-compatible base URL
    works via OPENAI_BASE_URL). Raises ProviderNotConfiguredError naming the
    missing setting for unknown or incomplete configuration.
    """
    from app.config import settings  # local import: avoid import cycle
    from app.pipeline import providers_http, providers_local

    def pick(stage: str, name: str) -> str:
        choices = _VALID_CHOICES[stage]
        if name not in choices:
            raise _not_configured(
                f"{stage} provider {name!r} is unknown; choose one of {choices}"
            )
        return name

    speech_name = pick("speech", settings.speech_provider)
    vision_name = pick("vision", settings.vision_provider)
    ocr_name = pick("ocr", settings.ocr_provider)
    embedding_name = pick("embedding", settings.embedding_provider)
    memory_name = pick("memory_generator", settings.memory_generator_provider)
    chat_name = pick("chat", settings.chat_provider)
    search_name = pick("search", settings.search_provider)

    if "openai" in (speech_name, vision_name, ocr_name, embedding_name, memory_name, chat_name):
        if not settings.openai_api_key:
            raise _not_configured(
                "an 'openai' provider is selected but OPENAI_API_KEY is not set; "
                "see .env.example"
            )
    if search_name == "tavily" and not settings.search_api_key:
        raise _not_configured(
            "SEARCH_PROVIDER=tavily but SEARCH_API_KEY is not set; see .env.example"
        )

    return Providers(
        speech=(
            providers_http.OpenAISpeechProvider()
            if speech_name == "openai"
            else (
                providers_local.FasterWhisperSpeechProvider()
                if speech_name == "faster-whisper"
                else UnconfiguredSpeechProvider()
            )
        ),
        vision=(
            providers_http.OpenAIVisionProvider()
            if vision_name == "openai"
            else UnconfiguredVisionProvider()
        ),
        ocr=(
            providers_http.OpenAIOCRProvider()
            if ocr_name == "openai"
            else (
                providers_local.RapidOCRProvider()
                if ocr_name == "rapidocr"
                else UnconfiguredOCRProvider()
            )
        ),
        embedding=(
            providers_http.OpenAIEmbeddingProvider()
            if embedding_name == "openai"
            else (
                HashingEmbeddingProvider()
                if embedding_name == "hash"
                else UnconfiguredEmbeddingProvider()
            )
        ),
        memory_generator=(
            providers_http.OpenAIMemoryGenerator()
            if memory_name == "openai"
            else (
                DeterministicMemoryGenerator()
                if memory_name == "deterministic"
                else UnconfiguredMemoryGenerator()
            )
        ),
        chat=(
            providers_http.OpenAIChatProvider()
            if chat_name == "openai"
            else UnconfiguredChatProvider()
        ),
        search=(
            providers_http.TavilySearchProvider()
            if search_name == "tavily"
            else UnconfiguredSearchProvider()
        ),
    )
