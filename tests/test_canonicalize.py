"""Unit tests: URL canonicalization (PRD §12 FR-CAP-002/003, SEC-001). Pure logic."""

import pytest

from app.capture.canonicalize import CanonicalizationError, canonicalize_url


def test_reel_url_basic():
    c = canonicalize_url("https://www.instagram.com/reel/AbC123xYz_-/")
    assert c.platform == "instagram"
    assert c.platform_item_id == "AbC123xYz_-"
    assert c.canonical_url == "https://instagram.com/reel/AbC123xYz_-"
    assert c.original_url == "https://www.instagram.com/reel/AbC123xYz_-/"


def test_tracking_params_stripped():
    c = canonicalize_url(
        "https://www.instagram.com/reel/AbC123xYz_-/?utm_source=ig_web_copy_link&igsh=abc123"
    )
    assert c.canonical_url == "https://instagram.com/reel/AbC123xYz_-"
    # original retained verbatim
    assert "utm_source" in c.original_url


def test_reels_plural_and_post_paths():
    assert canonicalize_url("https://instagram.com/reels/AbC123").platform_item_id == "AbC123"
    assert canonicalize_url("https://instagram.com/p/AbC123").platform_item_id == "AbC123"
    # /reels/ normalizes to /reel/, while /p/ is preserved so acquisition
    # can select the Instagram Post Scraper for photos/carousels.
    assert canonicalize_url("https://instagram.com/reels/AbC123").canonical_url == "https://instagram.com/reel/AbC123"
    assert canonicalize_url("https://m.instagram.com/p/AbC123").canonical_url == "https://instagram.com/p/AbC123"


def test_mobile_host_and_missing_scheme():
    c = canonicalize_url("m.instagram.com/reel/AbC123")
    assert c.canonical_url == "https://instagram.com/reel/AbC123"
    c2 = canonicalize_url("HTTP://WWW.INSTAGRAM.COM/REEL/AbC123")
    assert c2.canonical_url == "https://instagram.com/reel/AbC123"


def test_non_allowlisted_host_rejected():
    with pytest.raises(CanonicalizationError) as exc:
        canonicalize_url("https://www.tiktok.com/@user/video/123")
    assert exc.value.code == "UNSUPPORTED_SOURCE"


def test_lookalike_host_rejected():
    with pytest.raises(CanonicalizationError) as exc:
        canonicalize_url("https://instagram.com.evil.com/reel/AbC123")
    assert exc.value.code == "UNSUPPORTED_SOURCE"


def test_wrong_path_kind_rejected():
    for url in [
        "https://instagram.com/tv/AbC123",
        "https://instagram.com/stories/user/123",
        "https://instagram.com/reel/",
        "https://instagram.com/reel/AbC123/extra",
        "https://instagram.com/",
    ]:
        with pytest.raises(CanonicalizationError) as exc:
            canonicalize_url(url)
        assert exc.value.code == "INVALID_URL", url


def test_bad_shortcode_rejected():
    with pytest.raises(CanonicalizationError) as exc:
        canonicalize_url("https://instagram.com/reel/ab%20c")
    assert exc.value.code == "INVALID_URL"


def test_empty_and_garbage_rejected():
    for url in ["", "   ", "not a url", "://missing-host"]:
        with pytest.raises(CanonicalizationError) as exc:
            canonicalize_url(url)
        assert exc.value.code == "INVALID_URL"


def test_non_http_scheme_rejected():
    with pytest.raises(CanonicalizationError) as exc:
        canonicalize_url("ftp://instagram.com/reel/AbC123")
    assert exc.value.code == "INVALID_URL"
