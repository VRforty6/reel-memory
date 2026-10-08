"""Instagram source adapter — PRD §14, §8.8, §15, SEC-004.

M2 benchmark result (see MILESTONE2-BENCHMARK.md): with unauthenticated HTTP
only, Instagram serves a generic gated shell page on every public endpoint we
tried (oEmbed, /embed/, direct page) — 0% metadata yield and 0% media-byte
yield across 24 real public reels. There is therefore NO policy-compliant
acquisition path that produces video bytes:

  * video bytes would require an Instagram login/cookies, which this backend
    will never use (§8.8, SEC-004);
  * even a direct video URL (e.g. og:video) would point at *.fbcdn.net, which
    is outside the SEC-001 host allowlist and is un-fetchable by design.

So this adapter implements the best policy-compliant fallback measured in M2:
a single unauthenticated GET of the canonical post URL, parsing Open Graph
metadata tags. When OG metadata is present, the adapter returns MetadataOnly
and the worker persists it and lands the memory in METADATA_ONLY. When the
response is the known gated shell page (HTTP 200, no usable OG metadata —
the M2-measured outcome for unauthenticated clients), the adapter also
returns MetadataOnly, with an all-null SourceMetadata: the fetch succeeded
and the outcome is understood, so retrying would not change anything. The
memory lands in METADATA_ONLY on this single attempt — no retry loop — with
the shared URL preserved and nothing fabricated. Genuine transient failures
(timeouts, 5xx, network errors) and HTTP 429 rate limiting remain classified
retryable failures.

What this adapter will NEVER do (hard product constraints):
  * collect Instagram passwords or credentials (SEC-004),
  * extract browser cookies or maintain hidden Instagram sessions (§8.8),
  * perform authenticated scraping or circumvent private-content restrictions
    (§15),
  * fetch any host outside ALLOWED_HOSTS (SEC-001; re-checked here before the
    single fetch).

Downloaded page content is hostile/untrusted data (SEC-008): it is parsed with
byte-capped regexes over the <head> region only, never executed or
interpolated into anything trusted.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from app.capture.canonicalize import ALLOWED_HOSTS, CanonicalURL
from app.pipeline.failures import FailureCode
from app.sources.base import (
    MetadataOnly,
    ResolutionResult,
    RetryableFailure,
    SourceAdapter,
    SourceMetadata,
    Unavailable,
    Unsupported,
)

# Policy-compliant fetch parameters: neutral desktop UA, no cookies (urllib
# ships no CookieJar unless one is installed), tight timeout, head-only byte
# cap so a slow-drip response cannot hang the worker (M2 saw one 62s read).
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
_FETCH_TIMEOUT_S = 10
_HEAD_BYTES = 262_144  # OG tags always live in <head>; this caps a slow read.

# og: meta tags, attribute order either way.
_OG_RE = re.compile(
    r'<meta\s+[^>]*property=["\'](og:[a-zA-Z_:]+)["\'][^>]*content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_OG_RE_ALT = re.compile(
    r'<meta\s+[^>]*content=["\']([^"\']*)["\'][^>]*property=["\'](og:[a-zA-Z_:]+)["\']',
    re.IGNORECASE,
)

# Defensive truncation so extracted strings fit the DB columns
# (source_items.creator_handle is VARCHAR(128); caption is TEXT).
_CREATOR_MAX = 128
_CAPTION_MAX = 10_000


class _RateLimited(Exception):
    """HTTP 429 — Instagram is throttling us."""


class _NotFound(Exception):
    """HTTP 404 — the post is gone (or the shortcode never existed)."""


class _TransientFetchError(Exception):
    """Timeouts, connection errors, 5xx, gated responses — retryable."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _http_get(url: str) -> tuple[int, bytes]:
    """Single unauthenticated GET. Returns (status, head_bytes).

    Raises _RateLimited / _NotFound / _TransientFetchError. Kept as a
    module-level function so tests can monkeypatch it (no live network in
    unit tests).
    """
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
    except urllib.error.URLError as e:  # DNS, refused, reset, ...
        raise _TransientFetchError(
            f"network error fetching {url}: {e.reason}"
        ) from e
    except Exception as e:  # timeouts (TimeoutError) and anything else
        raise _TransientFetchError(
            f"{type(e).__name__} fetching {url}: {e}"
        ) from e


def _extract_open_graph(head: bytes) -> dict[str, str]:
    """Parse og:* meta tags from a page head. Untrusted input -> plain dict."""
    html = head.decode("utf-8", "replace")
    og: dict[str, str] = {}
    for prop, content in _OG_RE.findall(html):
        og.setdefault(prop.lower(), content)
    for content, prop in _OG_RE_ALT.findall(html):
        og.setdefault(prop.lower(), content)
    return og


def _metadata_from_og(og: dict[str, str]) -> Optional[SourceMetadata]:
    """Build SourceMetadata from OG tags, or None if no usable metadata."""
    creator = og.get("og:title") or None
    caption = og.get("og:description") or None
    thumbnail = og.get("og:image") or og.get("og:image:secure_url") or None
    if not any([creator, caption, thumbnail]):
        return None
    # og:title for reels is usually "<handle> on Instagram: ..."; keep it
    # verbatim rather than guessing at a handle parse.
    return SourceMetadata(
        creator_handle=creator[:_CREATOR_MAX] if creator else None,
        caption=caption[:_CAPTION_MAX] if caption else None,
        published_at=None,  # OG tags carry no publish date; not fabricated.
        thumbnail_url=thumbnail,
    )


class InstagramAdapter(SourceAdapter):
    platform = "instagram"

    def resolve(self, canonical: CanonicalURL) -> ResolutionResult:
        if canonical.platform != self.platform:
            return Unsupported(detail=f"not an instagram URL: {canonical.original_url!r}")

        # SEC-001: re-validate the allowlist inside the adapter, immediately
        # before any network fetch, even though canonicalize_url() already
        # checked it at the API boundary.
        host = (urllib.parse.urlparse(canonical.canonical_url).hostname or "").lower()
        if host not in ALLOWED_HOSTS:
            return Unsupported(detail=f"host not allowlisted: {host!r}")

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
            # M2 measured outcome for unauthenticated clients: Instagram
            # returns HTTP 200 with a generic gated shell page carrying no
            # OG metadata. This is a *successful* fetch of a known,
            # unauthenticated outcome — not a transient failure — so it is
            # terminal: MetadataOnly with an all-null SourceMetadata (there
            # is nothing genuine to report; the canonical URL itself is
            # carried on the result). The worker lands the memory in
            # METADATA_ONLY on this single attempt with no retry loop. No
            # metadata is fabricated. Genuine transient errors (timeouts,
            # 5xx, network failures) and HTTP 429 stay retryable above.
            return MetadataOnly(canonical=canonical, metadata=SourceMetadata())
        return MetadataOnly(canonical=canonical, metadata=metadata)
