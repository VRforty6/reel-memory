"""Tests for InstagramAdapter — M2 policy-compliant acquisition path.

All network is mocked: _http_get is monkeypatched to return fixture bytes or
raise the internal fetch exceptions. No live Instagram calls in unit tests.
"""

import urllib.error
import uuid
from types import SimpleNamespace

import pytest

from app.capture.canonicalize import canonicalize_url
from app.config import settings
from app.pipeline.state_machine import ProcessingStatus
from app.pipeline.worker import Worker
from app.sources import instagram as ig
from app.sources.base import MetadataOnly, RetryableFailure, Unavailable, Unsupported

OG_HTML = b"""<!DOCTYPE html><html><head>
<meta property="og:title" content="somehandle on Instagram: &quot;great reel&quot;" />
<meta property="og:description" content="A lovely caption with unicode \xc3\xa9" />
<meta property="og:image" content="https://scontent.cdninstagram.com/thumb.jpg" />
<meta property="og:video" content="https://scontent-lax.fbcdn.net/video.mp4" />
</head><body></body></html>"""

# Attribute order flipped (content before property) — still must parse.
OG_HTML_ALT_ORDER = b"""<html><head>
<meta content="otherhandle" property="og:title" />
<meta content="thumb2.jpg" property="og:image:secure_url" />
</head></html>"""

# The M2-measured reality for unauthenticated clients: generic shell page.
GATED_SHELL_HTML = b"""<!DOCTYPE html><html class="_9dls _ar44"><head>
<meta charset="utf-8" /><title>Instagram</title>
</head><body><div id="splash"></div></body></html>"""


def canon(url="https://www.instagram.com/reel/DdfevZCMhco/"):
    return canonicalize_url(url)


def adapter():
    return ig.InstagramAdapter()


def test_wrong_platform_returns_unsupported():
    c = canon("https://www.instagram.com/reel/DdfevZCMhco/")
    object.__setattr__(c, "platform", "tiktok")  # frozen dataclass; bypass for test
    result = adapter().resolve(c)
    assert isinstance(result, Unsupported)


def test_non_allowlisted_host_returns_unsupported(monkeypatch):
    c = canon()
    monkeypatch.setattr(ig, "_http_get", lambda url: (_ for _ in ()).throw(AssertionError("must not fetch")))
    object.__setattr__(c, "canonical_url", "https://evil.example/reel/DdfevZCMhco")
    result = adapter().resolve(c)
    assert isinstance(result, Unsupported)
    assert "allowlist" in result.detail


def test_og_metadata_returns_metadata_only(monkeypatch):
    monkeypatch.setattr(ig, "_http_get", lambda url: (200, OG_HTML))
    result = adapter().resolve(canon())
    assert isinstance(result, MetadataOnly)
    md = result.metadata
    assert md.creator_handle.startswith("somehandle")
    assert "lovely caption" in md.caption
    assert md.thumbnail_url == "https://scontent.cdninstagram.com/thumb.jpg"
    assert md.published_at is None  # never fabricated


def test_og_alt_attribute_order_parses(monkeypatch):
    monkeypatch.setattr(ig, "_http_get", lambda url: (200, OG_HTML_ALT_ORDER))
    result = adapter().resolve(canon())
    assert isinstance(result, MetadataOnly)
    assert result.metadata.creator_handle == "otherhandle"
    assert result.metadata.thumbnail_url == "thumb2.jpg"


def test_gated_shell_page_is_metadata_only(monkeypatch):
    # M2 benchmark outcome: shell page, no OG tags -> MetadataOnly with an
    # all-null SourceMetadata (nothing fabricated). The fetch succeeded and
    # the outcome is understood, so this is terminal — NOT a retry loop.
    monkeypatch.setattr(ig, "_http_get", lambda url: (200, GATED_SHELL_HTML))
    result = adapter().resolve(canon())
    assert isinstance(result, MetadataOnly)
    assert not isinstance(result, RetryableFailure)
    assert result.metadata.creator_handle is None
    assert result.metadata.caption is None
    assert result.metadata.thumbnail_url is None
    assert result.metadata.published_at is None  # never fabricated
    assert result.canonical.canonical_url == canon().canonical_url  # URL preserved


def test_http_429_is_rate_limited(monkeypatch):
    def boom(url):
        raise ig._RateLimited("HTTP 429")
    monkeypatch.setattr(ig, "_http_get", boom)
    result = adapter().resolve(canon())
    assert isinstance(result, RetryableFailure)
    assert result.code == "SOURCE_RATE_LIMITED"
    assert result.retryable is True


def test_http_404_is_unavailable(monkeypatch):
    def boom(url):
        raise ig._NotFound("HTTP 404")
    monkeypatch.setattr(ig, "_http_get", boom)
    result = adapter().resolve(canon())
    assert isinstance(result, Unavailable)
    assert "404" in result.reason


def test_timeout_is_retryable_failure(monkeypatch):
    def boom(url):
        raise ig._TransientFetchError("TimeoutError fetching https://instagram.com/reel/x: timed out")
    monkeypatch.setattr(ig, "_http_get", boom)
    result = adapter().resolve(canon())
    assert isinstance(result, RetryableFailure)
    assert result.code == "SOURCE_RESOLUTION_FAILED"
    assert result.retryable is True


def test_other_http_error_is_retryable_failure(monkeypatch):
    def boom(url):
        raise ig._TransientFetchError("HTTP 403 fetching https://instagram.com/reel/x: no usable response")
    monkeypatch.setattr(ig, "_http_get", boom)
    result = adapter().resolve(canon())
    assert isinstance(result, RetryableFailure)
    assert result.code == "SOURCE_RESOLUTION_FAILED"


def test_fetch_sends_no_cookies_and_uses_timeout(monkeypatch):
    # The policy-compliant fetch must not install a cookie jar and must pass
    # a bounded timeout.
    seen = {}

    class FakeResp:
        status = 200

        def read(self, n):
            seen["max_bytes"] = n
            return GATED_SHELL_HTML

    def fake_urlopen(req, timeout=None):
        hdrs = {k.lower(): v for k, v in req.header_items()}
        seen["timeout"] = timeout
        seen["cookie"] = hdrs.get("cookie")
        seen["ua"] = hdrs.get("user-agent")
        seen["url"] = req.full_url
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    status, head = ig._http_get("https://instagram.com/reel/DdfevZCMhco")
    assert status == 200 and head == GATED_SHELL_HTML
    assert seen["cookie"] is None  # no cookies, ever
    assert seen["timeout"] is not None and seen["timeout"] <= 10
    assert seen["max_bytes"] <= 262_144 + 1
    assert "Mozilla" in seen["ua"]
    assert seen["url"].startswith("https://instagram.com/")


def test_creator_truncated_to_column_width():
    og = {"og:title": "x" * 500}
    md = ig._metadata_from_og(og)
    assert md is not None
    assert len(md.creator_handle) == 128


# --- worker-level regression: gated shell -> METADATA_ONLY, single attempt ---


class _FakeDB:
    def add(self, obj):
        pass

    def commit(self):
        pass


def _run_gated_shell_job(monkeypatch, tmp_path):
    """Drive Worker._process_job for an Instagram URL share whose fetch
    returns the M2 gated shell page. Returns (memory, job, item)."""
    monkeypatch.setattr(ig, "_http_get", lambda url: (200, GATED_SHELL_HTML))
    monkeypatch.setattr(settings, "temp_dir", str(tmp_path))
    c = canon()
    memory = SimpleNamespace(
        id=uuid.uuid4(),
        processing_status=ProcessingStatus.CAPTURED.value,
        title=None, summary=None, category=None, language=None,
        processing_version=None, media_kind=None, thumbnail_jpeg=None,
    )
    job = SimpleNamespace(
        id=uuid.uuid4(),
        status="QUEUED", stage=None, attempt_count=0,
        failure_code=None, failure_message=None,
        started_at=None, finished_at=None,
    )
    item = SimpleNamespace(
        platform="instagram",
        platform_item_id=c.platform_item_id,
        canonical_url=c.canonical_url,
        original_url=c.original_url,
        caption=None, creator_handle=None, published_at=None,
        source_status=None,
    )
    db = _FakeDB()
    Worker(session_factory=lambda: db)._process_job(db, job, memory, item)
    return memory, job, item


def test_gated_shell_job_lands_in_metadata_only(monkeypatch, tmp_path):
    # (a) end-to-end through the worker: HTTP-200 gated shell -> memory
    # reaches METADATA_ONLY, the URL is preserved, nothing is fabricated.
    memory, job, item = _run_gated_shell_job(monkeypatch, tmp_path)
    assert memory.processing_status == ProcessingStatus.METADATA_ONLY.value
    assert job.status == "DONE"
    assert item.source_status == "METADATA_ONLY"
    assert item.creator_handle is None
    assert item.caption is None


def test_gated_shell_job_is_a_single_attempt_no_retries(monkeypatch, tmp_path):
    # (b) the gated-shell path must not schedule retries: the retry loop in
    # _process_job only repeats on StageError; MetadataOnly raises
    # TerminalState, so exactly one attempt is recorded.
    sleeps = []
    monkeypatch.setattr("app.pipeline.worker.time.sleep", lambda s: sleeps.append(s))
    memory, job, _item = _run_gated_shell_job(monkeypatch, tmp_path)
    assert memory.processing_status == ProcessingStatus.METADATA_ONLY.value
    assert job.attempt_count == 1  # no three-attempt loop
    assert sleeps == []  # no backoff scheduled
    assert job.failure_code is None  # not a failure at all
