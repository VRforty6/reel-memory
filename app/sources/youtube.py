"""YouTube source adapter using Apify's maintained YouTube Scraper.

The Android app already routes non-Instagram text shares through
POST /v1/captures/url. The API recognizes YouTube video URLs and stores them as
platform="youtube"; this adapter then retrieves public metadata plus existing
YouTube subtitles. No login, cookies, video downloader, or generated cloud
transcription is used. Videos without public subtitles still become searchable
by title/description, but are not falsely claimed to have a transcript.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Optional

from app.capture.canonicalize import CanonicalURL, CanonicalizationError
from app.config import settings
from app.pipeline.failures import FailureCode
from app.sources.base import (
    AuthenticationRequired,
    ResolutionResult,
    RetryableFailure,
    SourceAdapter,
    SourceMetadata,
    TranscriptContent,
    Unavailable,
    Unsupported,
)

_APIFY_API_BASE = "https://api.apify.com/v2"
_YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
)
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,32}$")
_SRT_TIME_RE = re.compile(
    r"^(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s+-->\s+"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


class _ApifyConfigError(Exception):
    pass


class _ApifyUnavailable(Exception):
    pass


class _TransientFetchError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def is_youtube_url(url: str) -> bool:
    raw = (url or "").strip()
    if not raw:
        return False
    try:
        parsed = urllib.parse.urlsplit(raw if "://" in raw else "https://" + raw)
    except ValueError:
        return False
    return (parsed.hostname or "").lower() in _YOUTUBE_HOSTS


def canonicalize_youtube_url(url: str) -> CanonicalURL:
    """Canonicalize one YouTube video/Short/live URL to watch?v=<id>."""
    raw = (url or "").strip()
    if not raw:
        raise CanonicalizationError("INVALID_URL", "empty YouTube URL")
    try:
        parsed = urllib.parse.urlsplit(raw if "://" in raw else "https://" + raw)
    except ValueError as e:
        raise CanonicalizationError("INVALID_URL", f"malformed YouTube URL: {raw!r}") from e
    if parsed.scheme not in ("http", "https"):
        raise CanonicalizationError("INVALID_URL", "YouTube URL must use http(s)")
    host = (parsed.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        raise CanonicalizationError("UNSUPPORTED_SOURCE", f"not a YouTube host: {host!r}")

    video_id: Optional[str] = None
    if host == "youtu.be":
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) == 1:
            video_id = parts[0]
    else:
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.path.rstrip("/") == "/watch":
            video_id = urllib.parse.parse_qs(parsed.query).get("v", [None])[0]
        elif len(parts) == 2 and parts[0].lower() in {"shorts", "live", "embed"}:
            video_id = parts[1]

    if not video_id or not _VIDEO_ID_RE.fullmatch(video_id):
        raise CanonicalizationError(
            "INVALID_URL",
            "expected a YouTube video URL (watch?v=, youtu.be/, /shorts/, or /live/)",
        )
    return CanonicalURL(
        platform="youtube",
        platform_item_id=video_id,
        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
        original_url=raw,
    )


def _run_actor(canonical: CanonicalURL) -> dict:
    token = (settings.apify_api_token or "").strip()
    if not token:
        raise _ApifyConfigError("YouTube acquisition requires APIFY_API_TOKEN")
    actor_id = urllib.parse.quote(settings.youtube_actor_id, safe="~")
    timeout_s = max(1.0, float(settings.apify_timeout_s))
    endpoint = (
        f"{_APIFY_API_BASE}/acts/{actor_id}/run-sync-get-dataset-items"
        f"?timeout={int(timeout_s)}"
    )
    body = json.dumps(
        {
            "startUrls": [{"url": canonical.canonical_url}],
            "maxResults": 1,
            "maxResultsShorts": 0,
            "maxResultStreams": 0,
            # Existing public subtitles only: no paid/generated STT here.
            "transcriptionAndSubtitle": "ALWAYS_SUBTITLES",
            "subtitlesLanguage": settings.youtube_subtitles_language,
            "subtitlesFormat": "srt",
            "saveSubsToKVS": False,
            "aiVideoDescription": False,
            "aiVideoSummary": False,
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
        if e.code in (401, 403):
            raise _ApifyConfigError(f"Apify rejected APIFY_API_TOKEN (HTTP {e.code})") from e
        if e.code == 404:
            raise _ApifyConfigError(f"YouTube actor {settings.youtube_actor_id!r} not found") from e
        if e.code == 429:
            raise _TransientFetchError("Apify YouTube actor rate-limited (HTTP 429)") from e
        raise _TransientFetchError(f"Apify YouTube actor HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise _TransientFetchError(f"Apify YouTube actor request failed: {type(e).__name__}: {e}") from e
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        raise _TransientFetchError(f"invalid Apify YouTube response: {e}") from e

    if not isinstance(payload, list):
        raise _TransientFetchError("Apify YouTube response was not a dataset list")
    if not payload:
        raise _ApifyUnavailable("Apify returned no YouTube video item")
    item = payload[0]
    if not isinstance(item, dict):
        raise _TransientFetchError("Apify returned a non-object YouTube item")
    return item


def _parse_date(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_ms(h: str, m: str, s: str, frac: str) -> int:
    millis = int(frac.ljust(3, "0")[:3])
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + millis


def parse_srt(srt: str) -> list[tuple[int, int, str]]:
    """Parse SRT into timestamped text segments; ignore malformed cue blocks."""
    normalized = (srt or "").replace("\r\n", "\n").replace("\r", "\n")
    out: list[tuple[int, int, str]] = []
    previous: tuple[int, int, str] | None = None
    for block in re.split(r"\n\s*\n", normalized):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        time_idx = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if time_idx is None:
            continue
        match = _SRT_TIME_RE.match(lines[time_idx])
        if not match:
            continue
        start = _to_ms(*match.groups()[:4])
        end = _to_ms(*match.groups()[4:])
        text = " ".join(lines[time_idx + 1 :]).strip()
        text = _HTML_TAG_RE.sub("", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        cue = (start, max(start, end), text)
        if previous is not None and previous[2] == cue[2]:
            continue
        out.append(cue)
        previous = cue
    return out


def _subtitle_srt(item: dict) -> str:
    subtitles = item.get("subtitles")
    if not isinstance(subtitles, list):
        return ""
    preferred = (settings.youtube_subtitles_language or "").lower()
    ordered = sorted(
        (s for s in subtitles if isinstance(s, dict)),
        key=lambda s: 0 if str(s.get("language") or "").lower() == preferred else 1,
    )
    for sub in ordered:
        value = sub.get("srt")
        if isinstance(value, str) and value.strip():
            return value[: max(1, int(settings.youtube_max_subtitle_chars))]
    return ""


class YouTubeAdapter(SourceAdapter):
    platform = "youtube"

    def resolve(
        self, canonical: CanonicalURL, *, work_dir: str | None = None
    ) -> ResolutionResult:
        if canonical.platform != self.platform:
            return Unsupported(detail=f"not a YouTube source: {canonical.original_url!r}")
        try:
            item = _run_actor(canonical)
        except _ApifyConfigError as e:
            return Unsupported(detail=str(e))
        except _ApifyUnavailable as e:
            return Unavailable(canonical=canonical, reason=str(e))
        except _TransientFetchError as e:
            return RetryableFailure(
                canonical=canonical,
                code=FailureCode.SOURCE_RESOLUTION_FAILED.value,
                retryable=True,
                detail=e.detail,
            )

        error = str(item.get("error") or "").strip()
        if error:
            note = str(item.get("note") or error).strip()
            upper = error.upper()
            if upper in {"AGE_RESTRICTED"} or "MEMBER" in upper or "PRIVATE" in upper:
                return AuthenticationRequired(canonical=canonical, detail=note)
            if upper in {"NOT_FOUND", "VIDEO_UNAVAILABLE"}:
                return Unavailable(canonical=canonical, reason=note)
            return Unsupported(detail=f"YouTube actor error {error}: {note}")
        if bool(item.get("isMembersOnly")):
            return AuthenticationRequired(canonical=canonical, detail="YouTube video is members-only")

        metadata = SourceMetadata(
            creator_handle=(str(item.get("channelName"))[:128] if item.get("channelName") else None),
            caption=(str(item.get("text"))[:10000] if item.get("text") else None),
            published_at=_parse_date(item.get("date")),
            thumbnail_url=(str(item.get("thumbnailUrl")) if item.get("thumbnailUrl") else None),
        )
        title = str(item.get("title") or "").strip()[:200] or None
        segments = parse_srt(_subtitle_srt(item))
        return TranscriptContent(
            canonical=canonical,
            metadata=metadata,
            title=title,
            segments=segments,
        )
