"""Pydantic request/response schemas (API surface, PRD §39)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

EvidenceType = Literal["VISUAL", "SPEECH", "OCR", "CAPTION", "TAG"]


# --- Captures ---


class CaptureRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2000)


class UrlCaptureRequest(BaseModel):
    """Website ingestion: fetch the page SSRF-safely and store its main
    article text as a memory (platform "web")."""

    url: str = Field(..., min_length=1, max_length=2000)


class CaptureResponse(BaseModel):
    id: uuid.UUID
    memory_id: uuid.UUID
    status: str
    duplicate: bool = False


class AlbumCaptureResponse(BaseModel):
    """Response for POST /v1/captures/album.

    One share = one memory = one capture against the quota, no matter how
    many photos the album holds. `content_hashes` are the per-file sha256
    digests in album order (photo N = index N-1); the album's dedup identity
    is the sha256 of the ':'-joined *sorted* hashes, so the same photos in a
    different order dedup to the same memory.
    """

    id: uuid.UUID
    memory_id: uuid.UUID
    status: str
    duplicate: bool = False
    file_count: int
    content_hashes: list[str]


# --- Memories ---


class MemorySummary(BaseModel):
    id: uuid.UUID
    title: str | None
    summary: str | None
    category: str | None
    platform: str
    media_kind: str | None = None
    processing_status: str
    # Migration 006 persisted one small JPEG per memory at ingest;
    # thumbnail_jpeg IS NULL for older memories or failed generation.
    # Clients use this to skip re-requesting known-missing thumbnails.
    has_thumbnail: bool
    created_at: datetime


class MemorySource(BaseModel):
    platform: str
    canonical_url: str
    original_url: str
    creator_handle: str | None
    caption: str | None
    published_at: datetime | None
    source_status: str


class MemorySegmentOut(BaseModel):
    modality: str
    start_ms: int | None
    end_ms: int | None
    content: str
    # Carousel/album memories: which photo this segment came from
    # (photo N = index N-1). None for single-file memories.
    album_index: int | None = None


class MemoryTagOut(BaseModel):
    tag: str
    confidence: float | None


class MemoryDetail(BaseModel):
    id: uuid.UUID
    title: str | None
    summary: str | None
    category: str | None
    language: str | None
    processing_status: str
    processing_version: str | None
    # See MemorySummary.has_thumbnail: thumbnail_jpeg IS NOT NULL.
    has_thumbnail: bool
    source: MemorySource
    tags: list[MemoryTagOut]
    segments: list[MemorySegmentOut]
    created_at: datetime
    updated_at: datetime


class MemoryStatusResponse(BaseModel):
    id: uuid.UUID
    processing_status: str
    stage: str | None = None
    attempt_count: int = 0
    failure_code: str | None = None
    failure_message: str | None = None


class VisualIndexStatusResponse(BaseModel):
    """Honest visual-backfill classification for one memory.

    status is one of VISUAL_INDEX_READY | VISUAL_BACKFILL_AVAILABLE |
    VISUAL_BACKFILL_SOURCE_UNAVAILABLE. `reason` explains why in plain
    language. VISUAL_INDEX_READY is reported ONLY when frame embedding rows
    actually exist — never from text/vision-description data.
    """

    id: uuid.UUID
    status: str
    reason: str
    frame_count: int = 0


class VisualBackfillResponse(BaseModel):
    id: uuid.UUID
    status: str
    reason: str
    frames_indexed: int = 0


class MemoryListResponse(BaseModel):
    memories: list[MemorySummary]
    limit: int
    offset: int


# --- Q&A (ask a reel) ---


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    verify: bool = False  # cross-check factual claims against the web


class AskCitation(BaseModel):
    modality: str  # speech | visual | ocr | caption
    timestamp_ms: int | None = None
    quote: str
    # Carousel/album memories: which photo the quote came from
    # (photo N = index N-1). None for single-file memories.
    album_index: int | None = None


class VerificationSource(BaseModel):
    title: str
    url: str


class VerificationFinding(BaseModel):
    claim: str
    verdict: Literal["supported", "contradicted", "uncertain"]
    sources: list[VerificationSource] = []


class VerificationResult(BaseModel):
    status: Literal["verified", "unavailable"]
    findings: list[VerificationFinding] = []
    message: str | None = None


class AskResponse(BaseModel):
    answer: str
    citations: list[AskCitation]
    evidence_coverage: Literal["full", "partial", "none"]
    # Present only when verify=true. "what the reel says" (above) is always
    # kept separate from "what the web supports" (below).
    verification: VerificationResult | None = None


# --- Intelligence layer: actions + decision brief ---


class ActionEvidence(BaseModel):
    modality: str  # speech | visual | ocr | caption | article
    timestamp_ms: int | None = None
    quote: str
    # Carousel/album memories: which photo the quote came from
    # (photo N = index N-1). None for single-file memories.
    album_index: int | None = None


class ActionItem(BaseModel):
    title: str
    detail: str = ""
    priority: Literal["P0", "P1", "P2"]
    effort: Literal["small", "medium", "large"]
    evidence: ActionEvidence


class ActionsResponse(BaseModel):
    actions: list[ActionItem]  # [] when the content has no actionable advice


class BriefRequest(BaseModel):
    goal: str = Field(..., min_length=1, max_length=500)
    # User-corrected intent: when the client shows the inferred intent and the
    # user corrects it, re-request the brief with the correction — the model
    # classification step is skipped and the correction is recorded.
    intent_override: "IntentOverride | None" = None


class IntentOverride(BaseModel):
    domain: str = Field(..., min_length=1, max_length=40)
    intent: str = Field(..., min_length=1, max_length=40)


class InferredIntent(BaseModel):
    """What the brief inferred the user is trying to do. The client shows
    `label` ("Looks like you're deciding whether to buy this — here's the
    breakdown") and can send a correction back via `intent_override`."""

    domain: str  # e.g. career, software, purchase, business, learning, health
    intent: str  # e.g. decide, compare, learn, act, validate
    confidence: Literal["high", "medium", "low"]
    label: str
    source: Literal["model", "user"] = "model"


class BriefResponse(BaseModel):
    summary: str
    key_claims: list[str]
    # Reuses the ask verification machinery over key_claims: "what the
    # content claims" kept separate from "what the web supports".
    validation: VerificationResult | None = None
    usefulness_assessment: str
    effort_estimate: Literal["small", "medium", "large"]
    # Always ends with a direct question ("Do you want to do this?") so the
    # client can render a decision card with Yes / Not now buttons.
    open_question: str
    inferred_intent: InferredIntent


# --- Auth / accounts ---


class SignupRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=10, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


class GoogleSignInRequest(BaseModel):
    id_token: str = Field(..., min_length=1, description="Google ID token from the app")


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until the access token expires


class LogoutRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


# --- Me / entitlements ---


class BucketUsage(BaseModel):
    used: int
    limit: int
    resets_at: datetime


class MeResponse(BaseModel):
    id: uuid.UUID
    email: str
    tier: str  # effective tier: "free" | "pro"
    pro_expires_at: datetime | None = None
    google_linked: bool = False
    has_password: bool = False
    usage: dict[str, BucketUsage]


# --- Google Play Billing ---


class BillingVerifyRequest(BaseModel):
    package_name: str = Field(..., min_length=1, max_length=256)
    product_id: str = Field(..., min_length=1, max_length=256)
    purchase_token: str = Field(..., min_length=1, max_length=1024)


class BillingVerifyResponse(BaseModel):
    tier: str  # effective tier after verification
    pro_expires_at: datetime | None = None
    auto_renewing: bool = False
    order_id: str | None = None


# --- Search ---


class SearchEvidence(BaseModel):
    type: EvidenceType
    snippet: str
    start_ms: int | None = None
    end_ms: int | None = None


class SearchResult(BaseModel):
    memory_id: uuid.UUID
    title: str | None
    summary: str | None
    category: str | None
    creator_handle: str | None
    platform: str
    processing_status: str
    source_status: str
    score: float
    evidence: list[SearchEvidence]


class SearchResponse(BaseModel):
    results: list[SearchResult]
    degraded: bool = False  # True when vector branch unavailable (FTS-only)
