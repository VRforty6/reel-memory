"""YouTube URL + adapter tests. All Apify calls are mocked."""

import pytest

from app.capture.canonicalize import CanonicalizationError
from app.sources import youtube as yt
from app.sources.base import AuthenticationRequired, TranscriptContent


def test_youtube_video_url_canonicalization():
    cases = [
        "https://www.youtube.com/watch?v=YmVqWiFEohY&t=15s",
        "https://youtu.be/YmVqWiFEohY?si=abc",
        "https://www.youtube.com/shorts/YmVqWiFEohY",
        "https://m.youtube.com/live/YmVqWiFEohY?feature=share",
    ]
    for raw in cases:
        c = yt.canonicalize_youtube_url(raw)
        assert c.platform == "youtube"
        assert c.platform_item_id == "YmVqWiFEohY"
        assert c.canonical_url == "https://www.youtube.com/watch?v=YmVqWiFEohY"
        assert c.original_url == raw


def test_youtube_channel_not_accepted_as_video():
    with pytest.raises(CanonicalizationError):
        yt.canonicalize_youtube_url("https://www.youtube.com/@OpenAI")


def test_is_youtube_url_host_detection():
    assert yt.is_youtube_url("https://youtu.be/YmVqWiFEohY")
    assert yt.is_youtube_url("https://www.youtube.com/watch?v=YmVqWiFEohY")
    assert not yt.is_youtube_url("https://youtube.com.evil.example/watch?v=YmVqWiFEohY")
    assert not yt.is_youtube_url("https://example.com/video")


def test_parse_srt_timestamped_segments():
    srt = """1
00:00:00,320 --> 00:00:04,960
hello world

2
00:00:05,000 --> 00:00:07,250
screen memory search
"""
    assert yt.parse_srt(srt) == [
        (320, 4960, "hello world"),
        (5000, 7250, "screen memory search"),
    ]


def test_youtube_adapter_returns_transcript_content(monkeypatch):
    monkeypatch.setattr(
        yt,
        "_run_actor",
        lambda canonical: {
            "id": "YmVqWiFEohY",
            "title": "How I would learn AI",
            "channelName": "Example Channel",
            "date": "2026-10-01",
            "text": "A useful description",
            "thumbnailUrl": "https://i.ytimg.com/vi/YmVqWiFEohY/hqdefault.jpg",
            "subtitles": [
                {
                    "language": "en",
                    "srt": "1\n00:00:01,000 --> 00:00:03,000\nlearn AI from scratch\n",
                }
            ],
        },
    )
    result = yt.YouTubeAdapter().resolve(
        yt.canonicalize_youtube_url("https://youtu.be/YmVqWiFEohY")
    )
    assert isinstance(result, TranscriptContent)
    assert result.title == "How I would learn AI"
    assert result.metadata.creator_handle == "Example Channel"
    assert result.metadata.caption == "A useful description"
    assert result.segments == [(1000, 3000, "learn AI from scratch")]


def test_youtube_without_subtitles_is_honest_empty_transcript(monkeypatch):
    monkeypatch.setattr(
        yt,
        "_run_actor",
        lambda canonical: {
            "id": "YmVqWiFEohY",
            "title": "No captions video",
            "channelName": "Example",
            "subtitles": None,
        },
    )
    result = yt.YouTubeAdapter().resolve(
        yt.canonicalize_youtube_url("https://www.youtube.com/watch?v=YmVqWiFEohY")
    )
    assert isinstance(result, TranscriptContent)
    assert result.segments == []


def test_youtube_age_restricted_stops_without_bypass(monkeypatch):
    monkeypatch.setattr(
        yt,
        "_run_actor",
        lambda canonical: {"error": "AGE_RESTRICTED", "note": "requires login"},
    )
    result = yt.YouTubeAdapter().resolve(
        yt.canonicalize_youtube_url("https://www.youtube.com/watch?v=YmVqWiFEohY")
    )
    assert isinstance(result, AuthenticationRequired)
    assert "login" in result.detail



def test_capture_url_endpoint_routes_youtube_to_youtube_platform(monkeypatch):
    from types import SimpleNamespace
    from fastapi import Response
    from app.api import captures
    from app.schemas import UrlCaptureRequest

    seen = {}
    monkeypatch.setattr(captures, "check_and_increment_quota", lambda *a, **k: None)

    def fake_get_or_create(db, user, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(id="item")

    monkeypatch.setattr(captures, "_get_or_create_item", fake_get_or_create)
    monkeypatch.setattr(
        captures,
        "_record_capture",
        lambda db, user, item, capture_source: ("accepted", False),
    )
    response = Response()
    result = captures.capture_url(
        UrlCaptureRequest(url="https://youtu.be/YmVqWiFEohY?si=shared"),
        response,
        db=SimpleNamespace(),
        user=SimpleNamespace(id="user"),
    )
    assert result == "accepted"
    assert response.status_code == 202
    assert seen["platform"] == "youtube"
    assert seen["platform_item_id"] == "YmVqWiFEohY"
    assert seen["canonical_url"] == "https://www.youtube.com/watch?v=YmVqWiFEohY"
