"""Instagram source adapter.

Two acquisition modes are supported behind the same SourceAdapter boundary:

* ``direct`` (default) preserves the Milestone-2 behavior: one anonymous GET
  of the public Instagram page and Open Graph metadata only. It never logs in,
  uses cookies, or attempts to bypass private-content controls.
* ``apify`` sends the public Reel URL to Apify's maintained Instagram Reel
  Scraper, reads the returned ``videoUrl``, and downloads those public media
  bytes into the worker's per-job temporary directory. The Apify token is sent
  only in an Authorization header and is never exposed to the Android client.

The Apify path is deliberately narrow: no transcript add-on, no downloaded
video add-on, no shares add-on. Reel Memory still performs its own ffmpeg,
transcription, vision, OCR, embeddings, and memory-generation stages.

All returned media URLs are treated as untrusted. Only HTTPS URLs on known
Instagram CDN suffixes are accepted, downloads are byte-capped, and the file
is written under the worker-owned temporary directory so the worker's existing
finally-block removes raw media on success or failure.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.capture.canonicalize import ALLOWED_HOSTS, CanonicalURL
from app.config import settings
from app.pipeline.failures import FailureCode
from app.sources.base import (
    AuthenticationRequired,
    MetadataOnly,
    ResolutionResult,
    ResolvedMedia,
    RetryableFailure,
    SourceAdapter,
    SourceMetadata,
    Unavailable,
    Unsupported,
)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
_FETCH_TIMEOUT_S = 10
_HEAD_BYTES = 262_144

_OG_RE = re.compile(
    r'<meta\s+[^>]*property=["\'](og:[a-zA-Z_:]+)["\'][^>]*content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_OG_RE_ALT = re.compile(
    r'<meta\s+[^>]*content=["\']([^"\']*)["\'][^>]*property=["\'](og:[a-zA-Z_:]+)["\']',
    re.IGNORECASE,
)

_CREATOR_MAX = 128
_CAPTION_MAX = 10_000
_APIFY_API_BASE = "https://api.apify.com/v2"
_APIFY_MEDIA_HOST_SUFFIXES = ("cdninstagram.com", "fbcdn.net")
_DOWNLOAD_CHUNK = 1024 * 1024


class _RateLimited(Exception):
    """HTTP 429 — upstream is throttling us."""


class _NotFound(Exception):
    """HTTP 404 — the source is gone."""


class _TransientFetchError(Exception):
    """Timeouts, connection errors, and transient HTTP failures."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class _ApifyConfigError(Exception):
    """Apify mode is selected but its server-side configuration is invalid."""


class _ApifyUnavailable(Exception):
    """Apify positively reports that the Reel is unavailable."""


def _http_get(url: str) -> tuple[int, bytes]:
    """Single unauthenticated Instagram GET used by ``direct`` mode."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        resp = urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S)
        return resp.status, resp.read(_HEAD_BYTES)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise _RateLimited(f"HTTP 429 from {url}") from e
        if e.code == 404:
            raise _NotFound(f"HTTP 404 from {url}") from e
        raise _TransientFetchError(
            f"HTTP {e.code} fetching {url}: no usable response"
        ) from e
    except urllib.error.URLError as e:
        raise _TransientFetchError(
            f"network error fetching {url}: {e.reason}"
        ) from e
    except Exception as e:
        raise _TransientFetchError(
            f"{type(e).__name__} fetching {url}: {e}"
        ) from e


def _extract_open_graph(head: bytes) -> dict[str, str]:
    html = head.decode("utf-8", "replace")
    og: dict[str, str] = {}
    for prop, content in _OG_RE.findall(html):
        og.setdefault(prop.lower(), content)
    for content, prop in _OG_RE_ALT.findall(html):
        og.setdefault(prop.lower(), content)
    return og


def _metadata_from_og(og: dict[str, str]) -> Optional[SourceMetadata]:
    creator = og.get("og:title") or None
    caption = og.get("og:description") or None
    thumbnail = og.get("og:image") or og.get("og:image:secure_url") or None
    if not any([creator, caption, thumbnail]):
        return None
    return SourceMetadata(
        creator_handle=creator[:_CREATOR_MAX] if creator else None,
        caption=caption[:_CAPTION_MAX] if caption else None,
        published_at=None,
        thumbnail_url=thumbnail,
    )


def _parse_apify_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _metadata_from_apify(item: dict) -> SourceMetadata:
    creator = item.get("ownerUsername")
    caption = item.get("caption")
    thumbnail = item.get("displayUrl")
    return SourceMetadata(
        creator_handle=(str(creator)[:_CREATOR_MAX] if creator else None),
        caption=(str(caption)[:_CAPTION_MAX] if caption else None),
        published_at=_parse_apify_timestamp(item.get("timestamp")),
        thumbnail_url=(str(thumbnail) if thumbnail else None),
    )


def _apify_actor_item(canonical: CanonicalURL) -> dict:
    """Run the official Apify Reel actor synchronously for one public URL."""
    token = (settings.apify_api_token or "").strip()
    if not token:
        raise _ApifyConfigError(
            "INSTAGRAM_ACQUISITION_PROVIDER=apify but APIFY_API_TOKEN is missing"
        )
    actor_id = urllib.parse.quote(settings.apify_actor_id, safe="~")
    timeout_s = max(1.0, float(settings.apify_timeout_s))
    endpoint = (
        f"{_APIFY_API_BASE}/actors/{actor_id}/run-sync-get-dataset-items"
        f"?timeout={int(timeout_s)}"
    )
    body = json.dumps(
        {
            "username": [canonical.canonical_url],
            "resultsLimit": 1,
            "includeTranscript": False,
            "includeDownloadedVideo": False,
            "includeSharesCount": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s + 30.0) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise _RateLimited("Apify returned HTTP 429") from e
        if e.code in (401, 403):
            raise _ApifyConfigError(
                f"Apify rejected APIFY_API_TOKEN (HTTP {e.code})"
            ) from e
        if e.code == 404:
            raise _ApifyConfigError(
                f"Apify actor {settings.apify_actor_id!r} was not found"
            ) from e
        raise _TransientFetchError(
            f"Apify actor HTTP {e.code} while resolving the Reel"
        ) from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise _TransientFetchError(
            f"Apify actor request failed: {type(e).__name__}: {e}"
        ) from e
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        raise _TransientFetchError(f"invalid Apify actor response: {e}") from e

    if not isinstance(payload, list):
        raise _TransientFetchError("Apify actor response was not a dataset item list")
    if not payload:
        raise _ApifyUnavailable("Apify returned no item for this Reel")
    item = payload[0]
    if not isinstance(item, dict):
        raise _TransientFetchError("Apify returned a non-object dataset item")
    return item


def _is_allowed_media_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return False
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _APIFY_MEDIA_HOST_SUFFIXES
    )


def _download_apify_video(video_url: str, work_dir: str, shortcode: str) -> str:
    """Download an Apify-returned Instagram CDN URL into worker temp storage."""
    if not _is_allowed_media_url(video_url):
        host = urllib.parse.urlparse(video_url).hostname
        raise _ApifyConfigError(
            f"refusing unexpected media URL returned by Apify (host={host!r})"
        )

    dest_dir = Path(work_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / f"instagram-{shortcode}.mp4"
    part = dest_dir / f"instagram-{shortcode}.part"
    max_bytes = max(1, int(settings.apify_max_media_mb)) * 1024 * 1024
    req = urllib.request.Request(
        video_url,
        headers={
            "User-Agent": _USER_AGENT,
            "Referer": "https://www.instagram.com/",
            "Accept": "video/*,*/*;q=0.8",
        },
    )
    written = 0
    try:
        with urllib.request.urlopen(req, timeout=float(settings.apify_timeout_s)) as resp:
            length = resp.headers.get("Content-Length")
            if length:
                try:
                    if int(length) > max_bytes:
                        raise _ApifyConfigError(
                            f"Reel media exceeds APIFY_MAX_MEDIA_MB={settings.apify_max_media_mb}"
                        )
                except ValueError:
                    pass
            with part.open("wb") as f:
                while True:
                    chunk = resp.read(_DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise _ApifyConfigError(
                            f"Reel media exceeds APIFY_MAX_MEDIA_MB={settings.apify_max_media_mb}"
                        )
                    f.write(chunk)
        if written <= 0:
            raise _TransientFetchError("Instagram CDN returned an empty video")
        part.replace(final)
        return str(final)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise _RateLimited("Instagram CDN returned HTTP 429") from e
        raise _TransientFetchError(
            f"Instagram CDN HTTP {e.code} while downloading Apify media"
        ) from e
    except urllib.error.URLError as e:
        raise _TransientFetchError(
            f"Instagram CDN download failed: {e.reason}"
        ) from e
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass


class InstagramAdapter(SourceAdapter):
    platform = "instagram"

    def resolve(
        self, canonical: CanonicalURL, *, work_dir: str | None = None
    ) -> ResolutionResult:
        if canonical.platform != self.platform:
            return Unsupported(detail=f"not an instagram URL: {canonical.original_url!r}")

        host = (urllib.parse.urlparse(canonical.canonical_url).hostname or "").lower()
        if host not in ALLOWED_HOSTS:
            return Unsupported(detail=f"host not allowlisted: {host!r}")

        mode = (settings.instagram_acquisition_provider or "direct").strip().lower()
        if mode == "apify":
            return self._resolve_apify(canonical, work_dir=work_dir)
        if mode != "direct":
            return Unsupported(
                detail=(
                    f"unknown INSTAGRAM_ACQUISITION_PROVIDER={mode!r}; "
                    "expected 'direct' or 'apify'"
                )
            )
        return self._resolve_direct(canonical)

    def _resolve_apify(
        self, canonical: CanonicalURL, *, work_dir: str | None
    ) -> ResolutionResult:
        if work_dir is None:
            return Unsupported(
                detail="Apify media acquisition requires a worker temporary directory"
            )
        try:
            item = _apify_actor_item(canonical)
        except _RateLimited as e:
            return RetryableFailure(
                canonical=canonical,
                code=FailureCode.SOURCE_RATE_LIMITED.value,
                retryable=FailureCode.SOURCE_RATE_LIMITED.retryable,
                detail=str(e),
            )
        except _ApifyUnavailable as e:
            return Unavailable(canonical=canonical, reason=str(e))
        except _ApifyConfigError as e:
            return Unsupported(detail=str(e))
        except _TransientFetchError as e:
            return RetryableFailure(
                canonical=canonical,
                code=FailureCode.SOURCE_RESOLUTION_FAILED.value,
                retryable=FailureCode.SOURCE_RESOLUTION_FAILED.retryable,
                detail=e.detail,
            )

        error = str(item.get("error") or "").strip()
        description = str(item.get("errorDescription") or error or "").strip()
        if error:
            lowered = f"{error} {description}".lower()
            if "not_found" in lowered or "not found" in lowered or "deleted" in lowered:
                return Unavailable(canonical=canonical, reason=description or error)
            if "private" in lowered or "login" in lowered or "access" in lowered:
                return AuthenticationRequired(canonical=canonical, detail=description or error)
            return RetryableFailure(
                canonical=canonical,
                code=FailureCode.SOURCE_RESOLUTION_FAILED.value,
                retryable=True,
                detail=f"Apify could not fully scrape the Reel: {description or error}",
            )

        metadata = _metadata_from_apify(item)
        video_url = item.get("videoUrl")
        if not video_url:
            return MetadataOnly(canonical=canonical, metadata=metadata)

        try:
            media_path = _download_apify_video(
                str(video_url), work_dir, canonical.platform_item_id
            )
        except _RateLimited as e:
            return RetryableFailure(
                canonical=canonical,
                code=FailureCode.SOURCE_RATE_LIMITED.value,
                retryable=True,
                detail=str(e),
            )
        except _ApifyConfigError as e:
            return Unsupported(detail=str(e))
        except _TransientFetchError as e:
            return RetryableFailure(
                canonical=canonical,
                code=FailureCode.MEDIA_DOWNLOAD_FAILED.value,
                retryable=FailureCode.MEDIA_DOWNLOAD_FAILED.retryable,
                detail=e.detail,
            )

        duration = item.get("videoDuration")
        try:
            duration_s = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_s = None
        return ResolvedMedia(
            canonical=canonical,
            metadata=metadata,
            media_path=media_path,
            duration_s=duration_s,
        )

    def _resolve_direct(self, canonical: CanonicalURL) -> ResolutionResult:
        try:
            _status, head = _http_get(canonical.canonical_url)
        except _RateLimited as e:
            return RetryableFailure(
                canonical=canonical,
                code=FailureCode.SOURCE_RATE_LIMITED.value,
                retryable=FailureCode.SOURCE_RATE_LIMITED.retryable,
                detail=f"Instagram rate-limited the metadata fetch: {e}",
            )
        except _NotFound:
            return Unavailable(
                canonical=canonical,
                reason="Instagram returned 404 for the post (deleted or invalid shortcode)",
            )
        except _TransientFetchError as e:
            return RetryableFailure(
                canonical=canonical,
                code=FailureCode.SOURCE_RESOLUTION_FAILED.value,
                retryable=FailureCode.SOURCE_RESOLUTION_FAILED.retryable,
                detail=e.detail,
            )

        metadata = _metadata_from_og(_extract_open_graph(head))
        if metadata is None:
            return MetadataOnly(canonical=canonical, metadata=SourceMetadata())
        return MetadataOnly(canonical=canonical, metadata=metadata)
