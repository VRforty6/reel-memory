"""SQLAlchemy models — mirrors migrations/001_initial.sql (PRD §38)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import settings


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    # Lightweight per-user intent history for the decision brief: a list of
    # {"domain","intent","label","source","memory_id","at"} dicts, newest last,
    # capped by the API layer. No new tables — just this JSON column.
    intent_history: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    # --- auth (commercialization) -------------------------------------------
    # NULL for Google-only accounts. Argon2id hash, never plaintext.
    password_hash: Mapped[Optional[str]] = mapped_column(String(256))
    # Google "sub" claim for Sign-In-with-Google; NULL until linked.
    google_sub: Mapped[Optional[str]] = mapped_column(String(128), unique=True)
    # 'free' | 'pro'. effective_tier() in app/entitlements.py also honors
    # pro_expires_at, so an expired subscription reads as free.
    tier: Mapped[str] = mapped_column(String(16), nullable=False, default="free")
    pro_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    source_items: Mapped[list["SourceItem"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    usage_counters: Mapped[list["UsageCounter"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class SourceItem(Base):
    __tablename__ = "source_items"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "platform", "platform_item_id",
            name="uq_source_items_user_platform_item",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    creator_handle: Mapped[Optional[str]] = mapped_column(String(128))
    caption: Mapped[Optional[str]] = mapped_column(Text)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    source_status: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    # PRD §13 duplicate bookkeeping
    first_saved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_saved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    save_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="source_items")
    captures: Mapped[list["Capture"]] = relationship(
        back_populates="source_item", cascade="all, delete-orphan"
    )
    memory: Mapped[Optional["Memory"]] = relationship(
        back_populates="source_item", cascade="all, delete-orphan", uselist=False
    )


class Capture(Base):
    __tablename__ = "captures"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_items.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    capture_source: Mapped[str] = mapped_column(String(64), nullable=False, default="api")

    source_item: Mapped[SourceItem] = relationship(back_populates="captures")


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_items.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )
    title: Mapped[Optional[str]] = mapped_column(Text)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[Optional[str]] = mapped_column(String(64))
    language: Mapped[Optional[str]] = mapped_column(String(16))
    processing_version: Mapped[Optional[str]] = mapped_column(String(32))
    processing_status: Mapped[str] = mapped_column(String(32), nullable=False, default="CAPTURED")
    # Milestone A UX: one small downscaled JPEG persisted once at ingest
    # (migration 006). NULL for memories ingested before it existed.
    thumbnail_jpeg: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    # 'video' | 'image' | 'album' | 'article', set once at ingest; NULL when
    # unknown (e.g. METADATA_ONLY). Used only for honest library filtering.
    media_kind: Mapped[Optional[str]] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    source_item: Mapped[SourceItem] = relationship(back_populates="memory")
    segments: Mapped[list["MemorySegment"]] = relationship(
        back_populates="memory", cascade="all, delete-orphan"
    )
    frame_embeddings: Mapped[list["MemoryFrameEmbedding"]] = relationship(
        back_populates="memory", cascade="all, delete-orphan"
    )
    tags: Mapped[list["MemoryTag"]] = relationship(
        back_populates="memory", cascade="all, delete-orphan"
    )
    jobs: Mapped[list["ProcessingJob"]] = relationship(
        back_populates="memory", cascade="all, delete-orphan"
    )


class MemorySegment(Base):
    __tablename__ = "memory_segments"
    __table_args__ = (
        CheckConstraint(
            "modality IN ('speech','ocr','visual','caption','summary','article')",
            name="ck_memory_segments_modality",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    memory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    modality: Mapped[str] = mapped_column(String(16), nullable=False)
    start_ms: Mapped[Optional[int]] = mapped_column(Integer)
    end_ms: Mapped[Optional[int]] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[Optional[list[float]]] = mapped_column(Vector(settings.embedding_dim))
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    memory: Mapped[Memory] = relationship(back_populates="segments")


class MemoryFrameEmbedding(Base):
    """One-time visual index: joint image/text embeddings of representative
    frames, for visual search ("find the reel by what was seen").

    Written once at ingest (uniform frames + bounded scene-cut extras) or by
    an explicit visual backfill; queried by cosine similarity against the
    query's text embedding. The 1536-dim text segment embedding column is
    untouched — this table carries the visual model's own dim
    (settings.visual_embedding_dim; the vector(N) column must match).
    """

    __tablename__ = "memory_frame_embeddings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    memory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    frame_index: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # "uniform" | "scene" | "photo": how the frame was selected.
    selection: Mapped[str] = mapped_column(String(16), nullable=False)
    # Album videos/photos: which file ("photo N" = album_index N-1).
    album_index: Mapped[Optional[int]] = mapped_column(Integer)
    embedding: Mapped[Optional[list[float]]] = mapped_column(
        Vector(settings.visual_embedding_dim)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    memory: Mapped[Memory] = relationship(back_populates="frame_embeddings")


class MemoryTag(Base):
    __tablename__ = "memory_tags"
    __table_args__ = (
        PrimaryKeyConstraint("memory_id", "tag", name="pk_memory_tags"),
    )

    memory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )
    tag: Mapped[str] = mapped_column(String(128), nullable=False)
    confidence: Mapped[Optional[float]] = mapped_column(Float)

    memory: Mapped[Memory] = relationship(back_populates="tags")


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    memory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="QUEUED")  # QUEUED|RUNNING|DONE|FAILED
    stage: Mapped[Optional[str]] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_code: Mapped[Optional[str]] = mapped_column(String(64))
    failure_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    memory: Mapped[Memory] = relationship(back_populates="jobs")


class RefreshToken(Base):
    """Server-side record for one issued refresh token.

    Only the sha256 of the token is stored — a DB read alone never yields a
    usable token. Rotation: each refresh revokes the presented token and
    issues a new one, so a stolen token is useful at most once (and its reuse
    is detectable: presenting a revoked token fails closed).
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    jti: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="refresh_tokens")


class UsageCounter(Base):
    """Freemium quota accounting: one row per (user, bucket, period).

    Buckets: "captures" (period "YYYY-MM"), "questions" / "verifications"
    (period "YYYY-MM-DD"). Periods are UTC calendar periods; resets_at is the
    start of the next period.
    """

    __tablename__ = "usage_counters"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "bucket", "period", name="pk_usage_counters"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    bucket: Mapped[str] = mapped_column(String(32), nullable=False)
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
        nullable=False,
    )

    user: Mapped[User] = relationship(back_populates="usage_counters")
