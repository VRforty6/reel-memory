"""Visual (joint image/text) embedding providers — visual search.

The text EmbeddingProvider in app.pipeline.providers is text-only. Visual
search needs a model that maps BOTH images and text queries into the SAME
embedding space (e.g. CLIP / OpenCLIP / SigLIP), so a natural-language query
like "red sticky note with two pens" can match a stored video frame even
when no transcript, OCR, caption, or vision description mentions it.

Embeddings are computed ONCE at ingest (frames -> vectors stored in
memory_frame_embeddings); search embeds only the query text and hits the
stored index. No video decoding and no vision API call happen at search
time, and the 1536-dim text embedding column is untouched.

Implementations must L2-normalize their outputs: pgvector cosine distance
is then a true cosine similarity and HNSW behaves well.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod

log = logging.getLogger("reel-memory.visual")


class VisualEmbeddingProvider(ABC):
    """Joint image/text embedding. Image and text vectors share one space."""

    @abstractmethod
    def embed_images(self, image_paths: list[str]) -> list[list[float]]:
        """Embed image files -> one L2-normalized vector per path, in order."""
        raise NotImplementedError

    @abstractmethod
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed query texts -> one L2-normalized vector per text, in order."""
        raise NotImplementedError

    @property
    @abstractmethod
    def dim(self) -> int:
        """Embedding dimension. Must equal settings.visual_embedding_dim and
        the memory_frame_embeddings.embedding vector(N) column."""
        raise NotImplementedError

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Human-readable model identity, e.g. 'openclip:ViT-B-32:laion2b_s34b_b79k'."""
        raise NotImplementedError


class VisualProviderNotConfiguredError(RuntimeError):
    """Raised by the stub when visual indexing is requested but no visual
    embedding provider is configured (VISUAL_EMBEDDING_PROVIDER=none)."""


class VisualProviderError(RuntimeError):
    """A configured visual provider failed at call time (model load, corrupt
    image, OOM)."""


class UnconfiguredVisualEmbeddingProvider(VisualEmbeddingProvider):
    """Loud stub: visual indexing/search requested without configuration."""

    def _boom(self) -> VisualProviderNotConfiguredError:
        return VisualProviderNotConfiguredError(
            "visual embedding provider is not configured "
            "(VISUAL_EMBEDDING_PROVIDER=none). Set "
            "VISUAL_EMBEDDING_PROVIDER=openclip and install the visual "
            "extras (`pip install -e \".[visual]\"`); see .env.example and "
            "README 'Visual search'."
        )

    def embed_images(self, image_paths: list[str]) -> list[list[float]]:
        raise self._boom()

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise self._boom()

    @property
    def dim(self) -> int:
        raise self._boom()

    @property
    def model_id(self) -> str:
        raise self._boom()


class OpenCLIPVisualEmbeddingProvider(VisualEmbeddingProvider):
    """Local OpenCLIP model (torch, CPU). Free, offline after the one-time
    weights download.

    The model is loaded lazily on first use and cached for the process
    lifetime (see build_visual_provider's process-level singleton cache),
    so a long-lived worker/API process pays the load cost once.
    Both image and text embeddings are L2-normalized.
    """

    def __init__(
        self,
        model_name: str | None = None,
        pretrained: str | None = None,
        cache_dir: str | None = None,
        batch_size: int = 16,
    ) -> None:
        from app.config import settings  # local import: avoid import cycle

        self._model_name = model_name or settings.visual_embedding_model
        self._pretrained = pretrained or settings.visual_embedding_pretrained
        self._cache_dir = cache_dir or settings.visual_model_cache_dir
        self._batch_size = max(1, int(batch_size or settings.visual_embed_batch_size))
        self._model = None
        self._preprocess = None
        self._tokenizer = None
        self._device = None
        self._dim: int | None = None

    @property
    def model_id(self) -> str:
        return f"openclip:{self._model_name}:{self._pretrained}"

    def _load(self) -> None:
        """Lazy model load. Raises VisualProviderError with a clear reason."""
        if self._model is not None:
            return
        try:
            import torch
            import open_clip
        except ImportError as e:
            raise VisualProviderError(
                "open_clip_torch/torch are not installed; install the visual "
                f"extras with `pip install -e \".[visual]\"` ({e})"
            ) from e
        try:
            log.info("loading visual model %s (first use; may download weights)",
                     self.model_id)
            model, _, preprocess = open_clip.create_model_and_transforms(
                self._model_name,
                pretrained=self._pretrained,
                cache_dir=self._cache_dir,
            )
            tokenizer = open_clip.get_tokenizer(self._model_name)
        except Exception as e:  # noqa: BLE001 - report whatever broke the load
            raise VisualProviderError(
                f"could not load visual model {self.model_id}: {e}"
            ) from e
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        model.to(self._device).eval()
        # Visual output dim (e.g. 512 for ViT-B-32, 768 for ViT-L-14/SigLIP).
        self._dim = int(model.visual.output_dim)
        self._model = model
        self._preprocess = preprocess
        self._tokenizer = tokenizer
        log.info("visual model %s ready on %s (dim=%d)",
                 self.model_id, self._device, self._dim)

    @property
    def dim(self) -> int:
        self._load()
        assert self._dim is not None
        return self._dim

    def _normalize(self, tensor):  # torch.Tensor -> L2-normalized
        import torch

        return tensor / tensor.norm(dim=-1, keepdim=True).clamp(min=1e-12)

    def embed_images(self, image_paths: list[str]) -> list[list[float]]:
        self._load()
        import torch
        from PIL import Image

        assert self._model is not None
        out: list[list[float]] = []
        with torch.no_grad():
            for i in range(0, len(image_paths), self._batch_size):
                batch_paths = image_paths[i : i + self._batch_size]
                images = []
                for p in batch_paths:
                    try:
                        images.append(
                            self._preprocess(
                                Image.open(p).convert("RGB")
                            )
                        )
                    except Exception as e:  # noqa: BLE001 - corrupt frame file
                        raise VisualProviderError(
                            f"could not read frame image {p!r}: {e}"
                        ) from e
                batch = torch.stack(images).to(self._device)
                feats = self._normalize(self._model.encode_image(batch))
                out.extend(feats.cpu().tolist())
        if len(out) != len(image_paths):
            raise VisualProviderError(
                f"visual embedding count mismatch: {len(out)} for {len(image_paths)} images"
            )
        return out

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self._load()
        import torch

        assert self._model is not None
        out: list[list[float]] = []
        with torch.no_grad():
            for i in range(0, len(texts), self._batch_size):
                batch_texts = texts[i : i + self._batch_size]
                tokens = self._tokenizer(batch_texts).to(self._device)
                feats = self._normalize(self._model.encode_text(tokens))
                out.extend(feats.cpu().tolist())
        if len(out) != len(texts):
            raise VisualProviderError(
                f"visual text-embedding count mismatch: {len(out)} for {len(texts)} texts"
            )
        return out


# Process-level singleton cache for configured providers. build_visual_provider
# is called on every search request and every visual-indexing run; without
# this, each call would construct a new OpenCLIPVisualEmbeddingProvider and
# reload the ~578MB model (~5s, ~1.5GB RAM churn) on every search. The cache
# is keyed on the resolved model config so a settings change picks up a new
# instance. The "none" stub is intentionally NOT cached (it is stateless and
# loud by design).
_provider_cache: dict[str, VisualEmbeddingProvider] = {}
_provider_cache_lock = threading.Lock()


def _clear_visual_provider_cache() -> None:
    """Drop cached provider instances. Test helper only — production code
    never calls this; the cache lives for the process lifetime."""
    with _provider_cache_lock:
        _provider_cache.clear()


def build_visual_provider() -> VisualEmbeddingProvider:
    """Build the visual embedding provider from VISUAL_EMBEDDING_PROVIDER.

    "none" (default) -> loud stub (visual indexing is skipped by the worker
    before the stub is ever called; the stub exists so direct misuse fails
    fast). "openclip" -> local OpenCLIP model, cached as a process-level
    singleton so repeated search/indexing calls reuse the loaded model
    instead of reloading ~578MB of weights on every call.
    """
    from app.config import settings  # local import: avoid import cycle

    name = settings.visual_embedding_provider
    if name == "none":
        return UnconfiguredVisualEmbeddingProvider()
    if name == "openclip":
        key = (f"openclip:{settings.visual_embedding_model}:"
               f"{settings.visual_embedding_pretrained}")
        with _provider_cache_lock:
            provider = _provider_cache.get(key)
            if provider is None:
                provider = OpenCLIPVisualEmbeddingProvider()
                _provider_cache[key] = provider
            return provider
    raise VisualProviderNotConfiguredError(
        f"visual embedding provider {name!r} is unknown; choose 'none' or 'openclip'"
    )
