"""URL canonicalization — PRD §12 FR-CAP-002/003 and SEC-001 (URL allowlist).

Real logic, no network calls: parses the shared URL, enforces the Instagram host
allowlist, extracts the shortcode from /reel/, /reels/ and /p/ paths, strips ALL
tracking/query parameters for the canonical URL, and keeps the original URL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

# SEC-001: backend must not fetch arbitrary URLs. Only these hosts are accepted.
ALLOWED_HOSTS = frozenset({"instagram.com", "www.instagram.com", "m.instagram.com"})

# Supported path kinds: instagram.com/reel/<sc>, /reels/<sc>, /p/<sc>
PATH_KINDS = frozenset({"reel", "reels", "p"})

# Instagram shortcodes are URL-safe base64-ish tokens.
SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{2,128}$")


class CanonicalizationError(ValueError):
    """Raised when a shared URL is invalid or from an unsupported source."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code  # maps to FailureCode names: INVALID_URL / UNSUPPORTED_SOURCE


@dataclass(frozen=True)
class CanonicalURL:
    platform: str  # "instagram"
    platform_item_id: str  # shortcode — the canonical source identity (FR-CAP-003)
    canonical_url: str  # query params stripped
    original_url: str  # exactly what the user shared


def canonicalize_url(url: str) -> CanonicalURL:
    """Validate and canonicalize a shared Instagram URL.

    Raises:
        CanonicalizationError: code INVALID_URL or UNSUPPORTED_SOURCE.
    """
    raw = (url or "").strip()
    if not raw:
        raise CanonicalizationError("INVALID_URL", "empty URL")

    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    if parsed.scheme not in ("http", "https"):
        raise CanonicalizationError("INVALID_URL", f"unsupported scheme in URL: {raw!r}")

    host = (parsed.hostname or "").lower()
    if not host or not re.fullmatch(r"[a-z0-9.-]+", host):
        raise CanonicalizationError("INVALID_URL", f"malformed host in URL: {raw!r}")
    if host not in ALLOWED_HOSTS:
        # SEC-001: never fetch non-allowlisted hosts.
        raise CanonicalizationError(
            "UNSUPPORTED_SOURCE", f"host not allowlisted: {host!r}"
        )

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) != 2 or parts[0].lower() not in PATH_KINDS:
        raise CanonicalizationError(
            "INVALID_URL",
            f"expected /reel/<shortcode>, /reels/<shortcode> or /p/<shortcode>: {raw!r}",
        )
    shortcode = parts[1]
    if not SHORTCODE_RE.match(shortcode):
        raise CanonicalizationError("INVALID_URL", f"invalid shortcode: {shortcode!r}")

    # Canonical form preserves content kind: reels normalize to /reel/, posts to /p/.
    # The shortcode remains the dedupe identity, but acquisition must know whether
    # to call the Reel Scraper or Post Scraper.
    path_kind = "p" if parts[0].lower() == "p" else "reel"
    canonical = f"https://instagram.com/{path_kind}/{shortcode}"
    return CanonicalURL(
        platform="instagram",
        platform_item_id=shortcode,
        canonical_url=canonical,
        original_url=raw,
    )
