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
from app.sources.base import AlbumMedia, MetadataOnly, ResolvedMedia, RetryableFailure, Unavailable, Unsupported

@pytest.fixture(autouse=True)
def _default_to_direct_instagram_mode(monkeypatch):
    # Unit tests must not depend on the developer's local .env.
    monkeypatch.setattr(settings, "instagram_acquisition_provider", "direct")


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


def test_apify_mode_returns_processable_media(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "instagram_acquisition_provider", "apify")
    item = {
        "shortCode": "DdfevZCMhco",
        "type": "Video",
        "ownerUsername": "creator",
        "caption": "caption from Apify",
        "displayUrl": "https://example.invalid/thumb.jpg",
        "timestamp": "2026-10-08T12:00:00.000Z",
        "videoDuration": 52.6,
        "videoUrl": "https://scontent-test.cdninstagram.com/video.mp4",
    }
    monkeypatch.setattr(ig, "_apify_actor_item", lambda canonical: item)

    media_path = tmp_path / "instagram-DdfevZCMhco.mp4"
    media_path.write_bytes(b"fake-mp4")
    monkeypatch.setattr(
        ig,
        "_download_apify_video",
        lambda video_url, work_dir, shortcode: str(media_path),
    )

    result = adapter().resolve(canon(), work_dir=str(tmp_path))
    assert isinstance(result, ResolvedMedia)
    assert result.media_path == str(media_path)
    assert result.duration_s == 52.6
    assert result.metadata.creator_handle == "creator"
    assert result.metadata.caption == "caption from Apify"
    assert result.metadata.published_at is not None


def test_apify_mode_requires_worker_temp_dir(monkeypatch):
    monkeypatch.setattr(settings, "instagram_acquisition_provider", "apify")
    result = adapter().resolve(canon())
    assert isinstance(result, Unsupported)
    assert "temporary directory" in result.detail


def test_apify_media_url_allowlist():
    assert ig._is_allowed_media_url("https://scontent-abc.cdninstagram.com/video.mp4")
    assert ig._is_allowed_media_url("https://video.xx.fbcdn.net/file.mp4")
    assert not ig._is_allowed_media_url("http://scontent-abc.cdninstagram.com/video.mp4")
    assert not ig._is_allowed_media_url("https://cdninstagram.com.evil.example/video.mp4")


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



def test_apify_post_image_returns_processable_media(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "instagram_acquisition_provider", "apify")
    item = {
        "shortCode": "Post123",
        "type": "Image",
        "ownerUsername": "post_creator",
        "caption": "nine github repos",
        "displayUrl": "https://scontent-test.cdninstagram.com/photo.jpg",
        "timestamp": "2026-10-08T12:00:00.000Z",
    }
    monkeypatch.setattr(ig, "_apify_post_actor_item", lambda canonical: item)
    image_path = tmp_path / "instagram-Post123-00.jpg"
    image_path.write_bytes(b"fake-jpg")
    monkeypatch.setattr(
        ig, "_download_apify_image",
        lambda image_url, work_dir, shortcode, index=0: str(image_path),
    )
    result = adapter().resolve(
        canon("https://www.instagram.com/p/Post123/"), work_dir=str(tmp_path)
    )
    assert isinstance(result, ResolvedMedia)
    assert result.media_path == str(image_path)
    assert result.metadata.creator_handle == "post_creator"
    assert result.metadata.caption == "nine github repos"


def test_apify_post_carousel_returns_album_media(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "instagram_acquisition_provider", "apify")
    item = {
        "shortCode": "Side123",
        "type": "Sidecar",
        "ownerUsername": "carousel_creator",
        "caption": "carousel caption",
        "displayUrl": "https://scontent-test.cdninstagram.com/cover.jpg",
        "childPosts": [
            {"type": "Image", "displayUrl": "https://scontent-test.cdninstagram.com/1.jpg"},
            {"type": "Image", "displayUrl": "https://scontent-test.cdninstagram.com/2.jpg"},
        ],
    }
    monkeypatch.setattr(ig, "_apify_post_actor_item", lambda canonical: item)
    def fake_image(url, work_dir, shortcode, index=0):
        path = tmp_path / f"instagram-{shortcode}-{index:02d}.jpg"
        path.write_bytes((f"img-{index}").encode())
        return str(path)
    monkeypatch.setattr(ig, "_download_apify_image", fake_image)
    result = adapter().resolve(
        canon("https://www.instagram.com/p/Side123/"), work_dir=str(tmp_path)
    )
    assert isinstance(result, AlbumMedia)
    assert len(result.files) == 2
    assert result.metadata.creator_handle == "carousel_creator"
    assert result.metadata.caption == "carousel caption"
