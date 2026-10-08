"""Visual frame indexing — one-time per reel, at ingest.

Pipeline (all local, no API cost):
  video -> uniform frames (existing media.extract_frames, kept as-is)
        + bounded scene-cut supplementation (media.extract_scene_keyframes)
        -> joint image/text embeddings (app.pipeline.visual provider)
        -> memory_frame_embeddings rows (frame_index, true timestamp_ms,
           selection, album_index, embedding)

Search later embeds only the query text and matches the stored vectors —
no video decoding, no vision API call at search time.

Backfill honesty: raw media is deleted after processing (PRD §8.6) and the
Instagram adapter cannot reacquire video bytes, so old memories are
classified before any backfill attempt:
  VISUAL_INDEX_READY                  — frame embeddings already stored
  VISUAL_BACKFILL_AVAILABLE           — source bytes still obtainable
  VISUAL_BACKFILL_SOURCE_UNAVAILABLE  — media gone; never claim success
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.capture.canonicalize import CanonicalURL
from app.config import settings
from app.pipeline import media as media_decode
from app.pipeline.visual import (
    VisualEmbeddingProvider,
    VisualProviderError,
    VisualProviderNotConfiguredError,
    build_visual_provider,
)
from app.sources.base import AlbumMedia, ResolvedMedia
from app.sources.registry import get_adapter

log = logging.getLogger("reel-memory.frame_index")

# Backfill classification states (user-facing strings, stable API contract).
VISUAL_INDEX_READY = "VISUAL_INDEX_READY"
VISUAL_BACKFILL_AVAILABLE = "VISUAL_BACKFILL_AVAILABLE"
VISUAL_BACKFILL_SOURCE_UNAVAILABLE = "VISUAL_BACKFILL_SOURCE_UNAVAILABLE"


@dataclass(frozen=True)
class FrameSample:
    """One frame selected for visual indexing."""

    path: str
    timestamp_ms: int  # true media timestamp (uniform: nominal fps-grid time)
    selection: str  # "uniform" | "scene" | "photo"
    album_index: Optional[int] = None


def uniform_timestamps_ms(duration_s: float, count: int) -> list[int]:
    """Nominal true timestamps of uniformly sampled frames.

    media.extract_frames uses ``fps={count/duration}``, whose output frames
    land on the grid t = i * duration / count. Pure (unit-testable).
    """
    count = max(1, int(count))
    if not duration_s or duration_s <= 0:
        return [0] * count
    return [int(i * duration_s / count * 1000) for i in range(count)]


def _dedupe_cuts(
    cuts_ms: list[int], uniform_ms: list[int], window_ms: int = 150
) -> list[int]:
    """Drop scene cuts within `window_ms` of any uniform sample — the uniform
    frame already represents that decoded moment.

    The window is intentionally small (default 150ms): a cut even a few
    hundred ms from a uniform sample usually shows different content (the
    uniform frame caught the pre-cut scene), and dropping it could hide the
    very event scene detection is for. Pure (unit-testable).
    """
    kept: list[int] = []
    for c in cuts_ms:
        if all(abs(c - u) > window_ms for u in uniform_ms):
            kept.append(c)
    return kept


def select_video_frames(
    media_path: str,
    tmp_dir: str | Path,
    uniform_frame_paths: list[str],
    *,
    album_index: Optional[int] = None,
    uniform_count: Optional[int] = None,
    max_extra: Optional[int] = None,
    scene_threshold: Optional[float] = None,
) -> list[FrameSample]:
    """Combine the existing uniform frames with bounded scene-cut extras.

    Returns FrameSamples sorted by timestamp with dense frame_index order
    (assigned by the caller/persister). Total <= uniform_count + max_extra.
    Scene detection failure degrades to uniform-only with a loud warning —
    the uniform baseline is the guaranteed index, extras are opportunistic.
    """
    uniform_count = uniform_count or settings.media_frame_count
    max_extra = settings.visual_max_extra_frames if max_extra is None else max_extra
    scene_threshold = (
        settings.visual_scene_threshold if scene_threshold is None else scene_threshold
    )
    n_uniform = len(uniform_frame_paths)
    if n_uniform == 0:
        raise media_decode.MediaDecodeError(
            f"no uniform frames provided for {media_path!r}; refusing to index"
        )

    probe = media_decode.probe_media(
        media_path,
        ffprobe_bin=settings.ffprobe_bin,
        timeout_s=settings.media_probe_timeout_s,
    )
    duration_s = probe.duration_s or 0.0
    uniform_ms = uniform_timestamps_ms(duration_s, n_uniform)
    samples = [
        FrameSample(path=p, timestamp_ms=ts, selection="uniform", album_index=album_index)
        for p, ts in zip(uniform_frame_paths, uniform_ms)
    ]

    extras: list[tuple[str, int]] = []
    try:
        extras = media_decode.extract_scene_keyframes(
            media_path,
            tmp_dir,
            max_extra=max_extra,
            scene_threshold=scene_threshold,
            max_dimension_px=settings.media_max_dimension_px,
            ffmpeg_bin=settings.ffmpeg_bin,
            ffprobe_bin=settings.ffprobe_bin,
            probe_timeout_s=settings.media_probe_timeout_s,
            decode_timeout_s=settings.media_decode_timeout_s,
        )
    except media_decode.MediaDecodeError as e:
        # Best-effort supplementation: the uniform frames are the guaranteed
        # baseline, so log loudly and continue rather than failing the job.
        log.warning("scene-cut supplementation failed for %r: %s; "
                    "indexing %d uniform frames only", media_path, e, n_uniform)

    if extras:
        kept_set = set(
            _dedupe_cuts([ts for _, ts in extras], uniform_ms)
        )
        kept = [(p, ts) for p, ts in extras if ts in kept_set]
        # Defense in depth: the extractor caps its output, but the bound
        # (<= max_extra scene frames) is guaranteed here too so processing
        # cost can never explode regardless of the extractor's behavior.
        if len(kept) > max_extra:
            step = len(kept) / max_extra
            kept = [kept[int(i * step)] for i in range(max_extra)]
        for p, ts in kept:
            samples.append(
                FrameSample(path=p, timestamp_ms=ts, selection="scene",
                            album_index=album_index)
            )
        log.info("visual indexing: %d uniform + %d scene frames for %r",
                 n_uniform, len(kept), media_path)

    samples.sort(key=lambda s: (s.timestamp_ms, s.selection))
    return samples


def index_visual_frames(
    db: Session,
    memory_id,
    samples: list[FrameSample],
    provider: VisualEmbeddingProvider,
    *,
    start_index: int = 0,
) -> int:
    """Embed frame samples and persist memory_frame_embeddings rows.

    `start_index` offsets the dense frame_index (backfill passes a running
    total so multi-file albums get globally dense indices).

    Raises VisualProviderError / VisualProviderNotConfiguredError on provider
    problems (the worker maps these to VISUAL_INDEX_FAILED); raises
    VisualProviderError on count/dim mismatch. Never invents vectors.
    """
    from app.models import MemoryFrameEmbedding  # local import: avoid circulars

    if not samples:
        return 0
    vectors = provider.embed_images([s.path for s in samples])
    if len(vectors) != len(samples):
        raise VisualProviderError(
            f"visual embedding count mismatch: {len(vectors)} vectors for "
            f"{len(samples)} frames"
        )
    expected_dim = provider.dim
    for i, (s, v) in enumerate(zip(samples, vectors)):
        if len(v) != expected_dim:
            raise VisualProviderError(
                f"visual embedding dim mismatch at frame {i}: got {len(v)}, "
                f"provider dim is {expected_dim}"
            )
        db.add(
            MemoryFrameEmbedding(
                memory_id=memory_id,
                frame_index=start_index + i,
                timestamp_ms=s.timestamp_ms,
                selection=s.selection,
                album_index=s.album_index,
                embedding=v,
            )
        )
    db.commit()
    return len(samples)


# -- backfill classification ------------------------------------------------


def _classify(
    has_frame_embeddings: bool,
    platform: str,
    source_files_present: bool,
) -> tuple[str, str]:
    """Pure backfill classifier (unit-testable). Never claims READY from
    text/vision-description data — only from actual frame embedding rows."""
    if has_frame_embeddings:
        return (
            VISUAL_INDEX_READY,
            "frame embeddings are already stored for this memory",
        )
    if platform == "upload":
        if source_files_present:
            return (
                VISUAL_BACKFILL_AVAILABLE,
                "uploaded source file(s) are still present in the upload store",
            )
        return (
            VISUAL_BACKFILL_SOURCE_UNAVAILABLE,
            "uploaded source file was deleted after processing (PRD §8.6); "
            "re-upload the media to enable visual search",
        )
    if platform == "web":
        return (
            VISUAL_BACKFILL_SOURCE_UNAVAILABLE,
            "article captures have no video frames to index",
        )
    return (
        VISUAL_BACKFILL_SOURCE_UNAVAILABLE,
        f"platform '{platform}' source media cannot be reacquired without "
        "authentication (unauthenticated acquisition yields no video bytes; "
        "see MILESTONE2-BENCHMARK.md)",
    )


def _upload_source_files_present(platform_item_id: str) -> bool:
    """True when the upload store still holds file(s) for this item.

    Mirrors the UploadAdapter's glob (platform_item_id is a hex digest, safe
    to glob); dotfiles and partial uploads never count.
    """
    upload_dir = Path(settings.resolve_upload_dir())
    try:
        matches = sorted(upload_dir.glob(f"{platform_item_id}.*"))
    except OSError:
        return False
    return any(not p.name.startswith(".") for p in matches)


def classify_visual_backfill(db: Session, memory) -> tuple[str, str]:
    """Classify a memory's visual-backfill state. `memory` is a Memory ORM
    object (source_item relationship must be loadable)."""
    from app.models import MemoryFrameEmbedding  # local import: avoid circulars

    has = (
        db.query(func.count(MemoryFrameEmbedding.id))
        .filter(MemoryFrameEmbedding.memory_id == memory.id)
        .scalar()
    )
    item = memory.source_item
    platform = item.platform if item is not None else "unknown"
    files_present = (
        _upload_source_files_present(item.platform_item_id)
        if item is not None and platform == "upload"
        else False
    )
    return _classify(bool(has), platform, files_present)


class VisualBackfillUnavailable(Exception):
    """Raised when a backfill is requested but no source media exists."""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(f"{status}: {reason}")
        self.status = status
        self.reason = reason


def run_visual_backfill(db: Session, memory) -> dict:
    """Backfill visual frame embeddings for one memory from its source media.

    Only re-runs the visual indexing (frame selection + embedding) — never
    the transcription/vision/OCR stages and never the text index. Raises
    VisualBackfillUnavailable when no source media exists; the caller turns
    that into an honest 409. Returns a summary dict on success.
    """
    from app.models import Memory  # noqa: F401  (type clarity)

    status, reason = classify_visual_backfill(db, memory)
    if status == VISUAL_INDEX_READY:
        from app.models import MemoryFrameEmbedding

        count = (
            db.query(func.count(MemoryFrameEmbedding.id))
            .filter(MemoryFrameEmbedding.memory_id == memory.id)
            .scalar()
        )
        return {"status": status, "reason": reason, "frames_indexed": int(count or 0)}
    if status == VISUAL_BACKFILL_SOURCE_UNAVAILABLE:
        raise VisualBackfillUnavailable(status, reason)

    item = memory.source_item
    adapter = get_adapter(item.platform)
    canon = CanonicalURL(
        platform=item.platform,
        platform_item_id=item.platform_item_id,
        canonical_url=item.canonical_url,
        original_url=item.original_url,
    )
    result = adapter.resolve(canon)
    if isinstance(result, ResolvedMedia) and result.media_path:
        file_samples: list[tuple[Optional[int], str]] = [(None, result.media_path)]
    elif isinstance(result, AlbumMedia):
        file_samples = [(idx, path) for idx, path in enumerate(result.files)]
    else:
        raise VisualBackfillUnavailable(
            VISUAL_BACKFILL_SOURCE_UNAVAILABLE,
            f"adapter could not reacquire source media for backfill "
            f"(got {type(result).__name__}); refusing to claim success from "
            "text-only data",
        )

    provider = build_visual_provider()  # loud when unconfigured
    if provider.dim != settings.visual_embedding_dim:
        raise VisualProviderError(
            f"visual model dim {provider.dim} != VISUAL_EMBEDDING_DIM "
            f"{settings.visual_embedding_dim}; refusing to write mixed-dim vectors"
        )

    from app.models import MemoryFrameEmbedding  # local import: avoid circulars

    # Remove any partial rows from a previous failed backfill before writing.
    db.query(MemoryFrameEmbedding).filter(
        MemoryFrameEmbedding.memory_id == memory.id
    ).delete()
    db.commit()

    tmp = Path(tempfile.mkdtemp(prefix="reel-memory-backfill-", dir=settings.temp_dir))
    total = 0
    try:
        for album_index, media_path in file_samples:
            suffix = Path(media_path).suffix.lower()
            if suffix in (".png", ".jpg", ".jpeg", ".webp"):
                samples = [
                    FrameSample(path=media_path, timestamp_ms=0,
                                selection="photo", album_index=album_index)
                ]
            else:
                frame_paths = media_decode.extract_frames(
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
                samples = select_video_frames(
                    media_path, tmp, frame_paths, album_index=album_index
                )
            total += index_visual_frames(
                db, memory.id, samples, provider, start_index=total
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {
        "status": VISUAL_INDEX_READY,
        "reason": f"backfilled {total} frame embeddings from source media",
        "frames_indexed": total,
    }
