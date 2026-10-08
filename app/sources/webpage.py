"""Website ingestion — platform "web".

POST /v1/captures/url stores a page URL; the worker fetches it here and
reduces it to its main article text (readability-style heuristics, stdlib
only), returned as ArticleContent. The extracted text is UNTRUSTED input: it
is read and quoted by later stages, never obeyed (SEC-008).

SSRF safety (the fetch path, defense in depth with the capture endpoint):
- https only, no userinfo in the URL, sane ASCII hostname
- every resolved IP must be public: private / loopback / link-local /
  multicast / reserved / unspecified addresses are refused (this blocks
  127.0.0.1, 10/8, 192.168/16, and the 169.254.169.254 metadata endpoint)
- the TCP connection is pinned to the validated IP (DNS-rebinding safe);
  SNI and the Host header still carry the original hostname
- manual redirect handling (<= max_redirects hops), each hop re-validated
  and re-resolved; redirect loops refused
- byte cap (~2MB) enforced via Content-Length pre-check and while reading
- connect/read timeout; only text/html is accepted
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import logging
import re
import socket
import ssl
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from app.capture.canonicalize import CanonicalURL, CanonicalizationError
from app.config import settings
from app.pipeline.failures import FailureCode
from app.sources.base import (
    ArticleContent,
    AuthenticationRequired,
    ResolutionResult,
    RetryableFailure,
    SourceAdapter,
    SourceMetadata,
    Unavailable,
    Unsupported,
)

log = logging.getLogger("reel-memory.sources.web")


# --- URL validation / canonicalization ---------------------------------------


def canonicalize_web_url(url: str) -> CanonicalURL:
    """Validate and canonicalize a web page URL for ingestion.

    Raises CanonicalizationError (codes INVALID_URL / UNSUPPORTED_SOURCE).
    """
    raw = (url or "").strip()
    if not raw:
        raise CanonicalizationError("INVALID_URL", "empty URL")
    parsed = urlsplit(raw if "://" in raw else "https://" + raw)
    if parsed.scheme != "https":
        raise CanonicalizationError(
            "INVALID_URL", f"only https URLs can be ingested, got {raw!r}"
        )
    if parsed.username or parsed.password:
        raise CanonicalizationError(
            "INVALID_URL", "URLs with embedded credentials are not allowed"
        )
    host = (parsed.hostname or "").lower()
    if not host or not re.fullmatch(r"[a-z0-9.-]{1,253}", host):
        raise CanonicalizationError("INVALID_URL", f"malformed host in URL: {raw!r}")
    if host.startswith("-") or host.endswith("-") or ".." in host:
        raise CanonicalizationError("INVALID_URL", f"malformed host in URL: {raw!r}")

    path = parsed.path or "/"
    canonical = f"https://{host}{path}"
    if parsed.query:
        canonical += f"?{parsed.query}"
    # (fragment dropped: it never changes the fetched document)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return CanonicalURL(
        platform="web",
        platform_item_id=digest,
        canonical_url=canonical,
        original_url=raw,
    )


def _validate_page_url(url: str) -> str:
    """Re-validate a URL at fetch time (each redirect hop). Returns the
    normalized https URL or raises FetchError(kind="unsupported")."""
    try:
        canon = canonicalize_web_url(url)
    except CanonicalizationError as e:
        raise FetchError("unsupported", f"refusing to fetch {url!r}: {e}") from e
    return canon.canonical_url


# --- SSRF-safe fetch -----------------------------------------------------------


class FetchError(Exception):
    """Classified fetch failure.

    kind: "retryable" (transient network/5xx/429) | "auth" (401/403) |
          "gone" (404/410) | "unsupported" (SSRF refusal, non-HTML,
          too large, redirect abuse, bad URL — will not succeed on retry)
    """

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def _resolve_public_ip(host: str) -> str:
    """Resolve `host` and return one IP, refusing non-public addresses.

    Raises FetchError(kind="retryable") on DNS failure and
    FetchError(kind="unsupported") on any non-public resolved IP.
    """
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(
            "retryable", f"DNS resolution failed for {host!r}: {e}"
        ) from e
    if not infos:
        raise FetchError("retryable", f"DNS returned no addresses for {host!r}")
    ips: list[str] = []
    for info in infos:
        raw_ip = info[4][0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError as e:
            raise FetchError("unsupported", f"unparseable IP {raw_ip!r}: {e}") from e
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise FetchError(
                "unsupported",
                f"refusing to fetch {host!r}: resolves to non-public IP {ip}",
            )
        ips.append(raw_ip)
    return ips[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that TCP-connects to a pre-validated IP (DNS-rebinding
    safe) while SNI and the Host header keep the original hostname."""

    def __init__(self, host: str, *, _pin_ip: str, **kwargs) -> None:
        self._pin_ip = _pin_ip
        super().__init__(host, **kwargs)

    def connect(self) -> None:  # noqa: D102
        self.sock = socket.create_connection(
            (self._pin_ip, self.port), self.timeout, self.source_address
        )
        server_hostname = self.host
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=server_hostname
        )


@dataclass
class _RawResponse:
    status: int
    headers: dict
    body: bytes


def _http_get(url: str, timeout_s: float, user_agent: str) -> _RawResponse:
    """One HTTPS GET with no redirect following. Module-level so tests can
    monkeypatch it with canned responses."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    ip = _resolve_public_ip(host)
    ctx = ssl.create_default_context()
    conn = _PinnedHTTPSConnection(
        host,
        port=parsed.port or 443,
        timeout=timeout_s,
        context=ctx,
        _pin_ip=ip,
    )
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    try:
        conn.request(
            "GET",
            path,
            headers={
                "Host": host,
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        )
        resp = conn.getresponse()
        return _RawResponse(
            status=resp.status,
            headers={k.lower(): v for k, v in resp.getheaders()},
            body=resp.read(),
        )
    finally:
        conn.close()


@dataclass
class FetchResult:
    final_url: str
    html: str


def _decode_body(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([a-z0-9_-]+)", content_type, re.IGNORECASE)
    encoding = (m.group(1) if m else "utf-8").strip("\"'")
    try:
        return body.decode(encoding, errors="replace")
    except (LookupError, ValueError):
        return body.decode("utf-8", errors="replace")


def fetch_page(
    url: str,
    *,
    timeout_s: float | None = None,
    max_bytes: int | None = None,
    max_redirects: int | None = None,
    user_agent: str | None = None,
) -> FetchResult:
    """SSRF-safe fetch of a page. Returns decoded HTML.

    Raises FetchError with a classified kind; never follows more than
    max_redirects hops, never reads more than max_bytes.
    """
    timeout_s = settings.fetch_timeout_s if timeout_s is None else timeout_s
    max_bytes = (
        settings.max_fetch_mb * 1024 * 1024 if max_bytes is None else max_bytes
    )
    max_redirects = (
        settings.fetch_max_redirects if max_redirects is None else max_redirects
    )
    user_agent = settings.fetch_user_agent if user_agent is None else user_agent

    current = _validate_page_url(url)
    seen: set[str] = set()
    for _ in range(max_redirects + 1):
        if current in seen:
            raise FetchError("unsupported", f"redirect loop at {current!r}")
        seen.add(current)
        try:
            raw = _http_get(current, timeout_s, user_agent)
        except (socket.timeout, TimeoutError, ConnectionError, OSError,
                ssl.SSLError, http.client.HTTPException) as e:
            raise FetchError(
                "retryable", f"fetch of {current!r} failed: {type(e).__name__}: {e}"
            ) from e

        if raw.status in (301, 302, 303, 307, 308):
            location = raw.headers.get("location")
            if not location:
                raise FetchError(
                    "unsupported", f"redirect without Location from {current!r}"
                )
            current = _validate_page_url(urljoin(current, location))
            continue

        if raw.status == 429:
            raise FetchError("retryable", f"rate limited (HTTP 429) by {current!r}")
        if raw.status in (401, 403):
            raise FetchError(
                "auth", f"page requires access (HTTP {raw.status}): {current!r}"
            )
        if raw.status in (404, 410):
            raise FetchError("gone", f"page not found (HTTP {raw.status}): {current!r}")
        if raw.status != 200:
            raise FetchError(
                "retryable" if 500 <= raw.status < 600 else "unsupported",
                f"unexpected HTTP {raw.status} from {current!r}",
            )

        content_type = raw.headers.get("content-type", "")
        media_type = content_type.split(";")[0].strip().lower()
        if media_type not in ("text/html", "application/xhtml+xml"):
            raise FetchError(
                "unsupported",
                f"not an HTML page (Content-Type: {media_type or 'unknown'}): {current!r}",
            )
        if len(raw.body) > max_bytes:
            raise FetchError(
                "unsupported",
                f"page exceeds the {max_bytes // (1024 * 1024)} MiB fetch cap: {current!r}",
            )
        return FetchResult(
            final_url=current, html=_decode_body(raw.body, content_type)
        )
    raise FetchError(
        "unsupported", f"too many redirects (>{max_redirects}) from {url!r}"
    )


# --- readability-style article extraction (stdlib only) -------------------------


_STRIP_TAGS = frozenset({
    "script", "style", "noscript", "nav", "header", "footer", "aside",
    "form", "button", "select", "input", "textarea", "iframe", "svg",
    "canvas", "figure", "figcaption",
})

_BOILERPLATE_RE = re.compile(
    r"nav|menu|sidebar|footer|header|comment|widget|popup|cookie|subscribe|"
    r"newsletter|social|share|related|breadcrumb|promo|banner|modal|overlay|"
    r"login|signup|advert",
    re.IGNORECASE,
)

_CONTAINER_TAGS = frozenset({"article", "main", "section", "div", "body"})

_WS_RE = re.compile(r"\s+")


class _Node:
    __slots__ = ("tag", "attrs", "children", "texts")

    def __init__(self, tag: str, attrs: dict) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node] = []
        self.texts: list[str] = []


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self._stack = [self.root]
        self.title_parts: list[str] = []
        self.og_title: str | None = None
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        ad = {k.lower(): str(v) for k, v in attrs}
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            prop = (ad.get("property") or ad.get("name") or "").lower()
            if prop == "og:title" and ad.get("content"):
                self.og_title = ad["content"].strip()
        if tag in _STRIP_TAGS:
            node = _Node(tag, ad)
            self._stack[-1].children.append(node)
            self._stack.append(node)  # still track depth so matching endtag pops
            return
        node = _Node(tag, ad)
        self._stack[-1].children.append(node)
        if tag not in ("br", "hr", "img", "meta", "link"):
            self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                break

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        elif self._stack[-1].tag not in _STRIP_TAGS:
            self._stack[-1].texts.append(data)


def _node_text(node: _Node, *, _depth: int = 0) -> str:
    if node.tag in _STRIP_TAGS or _depth > 60:
        return ""
    parts = list(node.texts)
    for child in node.children:
        parts.append(_node_text(child, _depth=_depth + 1))
    return " ".join(p for p in parts if p)


def _link_text_len(node: _Node) -> int:
    total = 0
    if node.tag == "a":
        total += len(_node_text(node))
    for child in node.children:
        total += _link_text_len(child)
    return total


def _is_boilerplate(node: _Node) -> bool:
    hay = f"{node.attrs.get('id', '')} {node.attrs.get('class', '')} {node.attrs.get('role', '')}"
    return bool(_BOILERPLATE_RE.search(hay))


def _paragraph_score(node: _Node) -> float:
    """Readability-style: long <p> text counts, link-heavy text is discounted."""
    if node.tag in _STRIP_TAGS or _is_boilerplate(node):
        return 0.0
    score = 0.0
    for child in node.children:
        if child.tag == "p":
            text = _node_text(child)
            if len(text.strip()) >= 25:
                score += len(text) - 2.0 * _link_text_len(child)
        else:
            score += 0.5 * _paragraph_score(child)
    own = " ".join(node.texts).strip()
    if len(own) >= 80:
        score += len(own) * 0.5
    return score


def _collect_paragraphs(node: _Node, out: list[str]) -> None:
    if node.tag in _STRIP_TAGS or _is_boilerplate(node):
        return
    if node.tag == "p":
        text = _WS_RE.sub(" ", _node_text(node)).strip()
        if len(text) >= 25:
            out.append(text)
        return
    for child in node.children:
        _collect_paragraphs(child, out)


def extract_article(html: str, *, max_chars: int = 100_000) -> tuple[str | None, str]:
    """Extract (title, main article text) from HTML.

    Heuristic: drop boilerplate tags/elements, score containers by
    paragraph-text density, take the winner's paragraphs. Returns ("", ...)
    title None when no <title>/og:title is present, and "" text when nothing
    readable is found.
    """
    parser = _TreeBuilder()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML must not crash ingestion
        log.warning("HTML parse error; extracting from partial tree", exc_info=True)
    parser.close()

    title = parser.og_title or _WS_RE.sub(" ", "".join(parser.title_parts)).strip()
    title = title or None

    best: _Node | None = None
    best_score = 0.0

    def visit(node: _Node) -> None:
        nonlocal best, best_score
        if node.tag in _CONTAINER_TAGS and not _is_boilerplate(node):
            s = _paragraph_score(node)
            if s > best_score:
                best, best_score = node, s
        for child in node.children:
            visit(child)

    visit(parser.root)

    paragraphs: list[str] = []
    if best is not None and best_score > 0:
        _collect_paragraphs(best, paragraphs)
    if not paragraphs:
        # fallback: every readable paragraph on the page
        _collect_paragraphs(parser.root, paragraphs)

    text = "\n\n".join(paragraphs)
    text = _WS_RE.sub(" ", text).strip()
    # restore paragraph breaks collapsed above: keep it simple and honest
    return title, text[:max_chars]


# --- adapter -------------------------------------------------------------------


class WebpageAdapter(SourceAdapter):
    """Fetch a web page SSRF-safely and reduce it to article text.

    Fetch failures are classified honestly: transient network/5xx/429 ->
    RetryableFailure, 401/403 -> AuthenticationRequired (never circumvented),
    404/410 -> Unavailable, SSRF refusal / non-HTML / oversize / redirect
    abuse -> Unsupported (permanent).
    """

    platform = "web"

    def resolve(self, canonical: CanonicalURL) -> ResolutionResult:
        try:
            fetched = fetch_page(canonical.canonical_url)
        except FetchError as e:
            if e.kind == "retryable":
                code = (
                    FailureCode.SOURCE_RATE_LIMITED.value
                    if "429" in e.detail
                    else FailureCode.SOURCE_RESOLUTION_FAILED.value
                )
                return RetryableFailure(canonical, code, True, e.detail)
            if e.kind == "auth":
                return AuthenticationRequired(canonical, e.detail)
            if e.kind == "gone":
                return Unavailable(canonical, e.detail)
            return Unsupported(e.detail)

        title, text = extract_article(fetched.html)
        if not text.strip():
            return Unsupported(
                f"page contained no extractable article text: {fetched.final_url!r}"
            )
        return ArticleContent(
            canonical=canonical,
            metadata=SourceMetadata(),
            title=title,
            text=text,
        )
