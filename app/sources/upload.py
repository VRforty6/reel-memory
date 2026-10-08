"""Upload source adapter — direct video/image file uploads (platform "upload").

Unauthenticated Instagram acquisition yields 0% video bytes (see
MILESTONE2-BENCHMARK.md), so the "see the reel" path is a direct file upload:
the client POSTs the video (or a screenshot / post image) to
/v1/captures/upload and this adapter hands the stored file to the normal
worker pipeline as ResolvedMedia. No fetching, no credentials, no URL
resolution — the bytes are already ours.

The upload endpoint names files deterministically as <sha256>.<ext> inside
settings.upload_dir; resolve() re-derives the path from the platform_item_id
(the content hash) so no extra DB columns are needed. Dotfiles and partial
uploads (".part-*") are never matched.

Album/carousel captures (POST /v1/captures/album) store one file per photo
as <combined_hash>.<index:02d>.<file_sha256>.<ext>; resolve() detects that
layout and returns AlbumMedia with the files in album order.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from app.capture.canonicalize import CanonicalURL
from app.config import settings
from app.pipeline.failures import FailureCode
from app.sources.base import (
    AlbumMedia,
    ResolutionResult,
    ResolvedMedia,
    RetryableFailure,
    SourceAdapter,
    SourceMetadata,
)

log = logging.getLogger("reel-memory.sources.upload")

# Album/carousel file layout written by POST /v1/captures/album:
#   <combined_hash>.<index:02d>.<file_sha256>.<ext>
# The combined hash is the SourceItem's platform_item_id, so one glob finds
# the whole album and the index orders the photos ("photo N" = index N-1).
_ALBUM_FILE_RE = re.compile(r"^([0-9a-f]{64})\.(\d{2})\.([0-9a-f]{64})(\.[a-z0-9]+)$")


class UploadAdapter(SourceAdapter):
    platform = "upload"

    def resolve(self, canonical: CanonicalURL) -> ResolutionResult:
        upload_dir = Path(settings.resolve_upload_dir())
        # platform_item_id is a hex sha256 digest — safe for globbing.
        matches = sorted(upload_dir.glob(f"{canonical.platform_item_id}.*"))
        if not matches:
            return RetryableFailure(
                canonical,
                FailureCode.SOURCE_RESOLUTION_FAILED.value,
                True,
                "uploaded file not found in the upload store; "
                "it may have been cleaned up after processing",
            )
        album = self._match_album(canonical.platform_item_id, matches)
        if album is not None:
            return AlbumMedia(
                canonical=canonical,
                metadata=SourceMetadata(),
                files=album,
            )
        return ResolvedMedia(
            canonical=canonical,
            metadata=SourceMetadata(),
            media_path=str(matches[0]),
        )

    @staticmethod
    def _match_album(
        platform_item_id: str, matches: list[Path]
    ) -> list[str] | None:
        """Detect the album file layout among glob matches and return the
        file paths in album order, or None for a single-file upload. Pure
        (unit-testable)."""
        indexed: list[tuple[int, str]] = []
        for m in matches:
            mm = _ALBUM_FILE_RE.match(m.name)
            if mm and mm.group(1) == platform_item_id:
                indexed.append((int(mm.group(2)), str(m)))
        if not indexed:
            return None
        indexed.sort(key=lambda t: t[0])
        return [path for _, path in indexed]
