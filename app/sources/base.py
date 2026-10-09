"""SourceAdapter interface — PRD §14.

Instagram-specific acquisition logic must stay behind this boundary; the rest of
Reel Memory operates on the normalized result types and never knows how a given
platform works (PRD §8.7, §53).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Union

from app.capture.canonicalize import CanonicalURL


@dataclass(frozen=True)
class SourceMetadata:
    creator_handle: Optional[str] = None
    caption: Optional[str] = None
    published_at: Optional[datetime] = None
    thumbnail_url: Optional[str] = None


@dataclass(frozen=True)
class ResolvedMedia:
    """Source resolved AND processable bytes are available."""

    canonical: CanonicalURL
    metadata: SourceMetadata
    media_path: Optional[str] = None  # local temp path of the downloaded media
    duration_s: Optional[float] = None


@dataclass(frozen=True)
class MetadataOnly:
    """Source identified but media bytes are not (yet) available."""

    canonical: CanonicalURL
    metadata: SourceMetadata


@dataclass(frozen=True)
class ArticleContent:
    """A web page fetched and reduced to its main article text.

    Returned by the "web" adapter for website ingestion: the page's readable
    text is the content itself (no audio/video stages apply). The worker
    stores it as an "article"-modality segment and builds the memory from it.
    """

    canonical: CanonicalURL
    metadata: SourceMetadata
    title: Optional[str]
    text: str  # extracted main article text (readability-style, untrusted)


@dataclass(frozen=True)
class TranscriptContent:
    """Public video metadata plus timestamped subtitle/transcript text.

    Used for sources such as YouTube where public captions are available even
    when Reel Memory does not acquire raw video bytes. Segment tuples are
    ``(start_ms, end_ms, text)`` and remain untrusted source data.
    """

    canonical: CanonicalURL
    metadata: SourceMetadata
    title: Optional[str]
    segments: list[tuple[int, int, str]]


@dataclass(frozen=True)
class AlbumMedia:
    """A carousel/album: one memory over multiple uploaded files.

    Returned by the upload adapter when the stored files for a SourceItem
    are the per-photo files of an album capture (see POST
    /v1/captures/album). `files` are local paths in album order — index 0
    is "photo 1" for citations. The worker processes every file and tags
    each segment with its album_index.
    """

    canonical: CanonicalURL
    metadata: SourceMetadata
    files: list[str]


@dataclass(frozen=True)
class Unavailable:
    """Source is gone (deleted / private / removed)."""

    canonical: CanonicalURL
    reason: str


@dataclass(frozen=True)
class AuthenticationRequired:
    """Media needs access the backend must not circumvent (PRD §15, §8.8)."""

    canonical: CanonicalURL
    detail: str


@dataclass(frozen=True)
class Unsupported:
    detail: str


@dataclass(frozen=True)
class RetryableFailure:
    """Transient failure — the worker may retry with backoff (PRD §32)."""

    canonical: Optional[CanonicalURL]
    code: str  # FailureCode name, e.g. SOURCE_RESOLUTION_FAILED
    retryable: bool
    detail: str


ResolutionResult = Union[
    ResolvedMedia, AlbumMedia, MetadataOnly, ArticleContent, TranscriptContent, Unavailable, AuthenticationRequired, Unsupported, RetryableFailure
]


class SourceAdapter(ABC):
    """Per-platform acquisition behind a stable interface."""

    platform: str

    @abstractmethod
    def resolve(
        self, canonical: CanonicalURL, *, work_dir: str | None = None
    ) -> ResolutionResult:
        """Resolve a source. ``work_dir`` is worker-owned temporary storage."""
        raise NotImplementedError
