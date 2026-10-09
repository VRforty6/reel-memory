"""Adapter registry — one adapter per platform (PRD §53: SourceAdapter)."""

from __future__ import annotations

from app.sources.base import SourceAdapter
from app.sources.instagram import InstagramAdapter
from app.sources.upload import UploadAdapter
from app.sources.webpage import WebpageAdapter
from app.sources.youtube import YouTubeAdapter


class UnsupportedSourceError(ValueError):
    def __init__(self, platform: str) -> None:
        super().__init__(f"unsupported platform: {platform!r}")
        self.code = "UNSUPPORTED_SOURCE"


_ADAPTERS: dict[str, SourceAdapter] = {
    "instagram": InstagramAdapter(),
    "upload": UploadAdapter(),  # direct video file uploads (no URL resolution)
    "web": WebpageAdapter(),  # website ingestion: SSRF-safe fetch -> article text
    "youtube": YouTubeAdapter(),  # public metadata + existing subtitles via Apify
    # Future: "tiktok": TikTokAdapter() (PRD §53)
}


def get_adapter(platform: str) -> SourceAdapter:
    try:
        return _ADAPTERS[platform]
    except KeyError:
        raise UnsupportedSourceError(platform) from None


def supported_platforms() -> list[str]:
    return sorted(_ADAPTERS)
