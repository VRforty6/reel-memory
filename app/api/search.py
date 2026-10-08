"""Search API — PRD §26/§27. GET /v1/search?q=...

Hybrid FTS + pgvector retrieval with RRF fusion and per-result evidence.
If no embedding provider is configured the vector branch is skipped and the
response is marked degraded (FTS-only) rather than failing outright.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import settings
from app.auth import get_current_user
from app.db import get_db
from app.models import User
from app.schemas import SearchEvidence, SearchResponse, SearchResult
from app.search.hybrid import hybrid_search

router = APIRouter()
log = logging.getLogger("reel-memory.search")


def _maybe_embed(query: str) -> list[float] | None:
    """Embed the query with the configured provider (Milestone 4).

    EMBEDDING_PROVIDER=none -> None (FTS-only, response marked degraded).
    "openai"/"hash" -> real vector. Misconfiguration raises 503; a provider
    failing at call time raises 502. Never silently degrades to a zero vector.
    """
    from app.pipeline.providers import ProviderError, ProviderNotConfiguredError
    from app.pipeline.providers import build_providers

    name = settings.embedding_provider
    if name == "none":
        return None
    try:
        provider = build_providers().embedding
    except ProviderNotConfiguredError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    try:
        vectors = provider.embed([query])
    except ProviderError as e:
        raise HTTPException(
            status_code=502, detail=f"embedding provider failed: {e}"
        ) from e
    return vectors[0] if vectors else None


def _maybe_embed_visual(query: str) -> list[float] | None:
    """Embed the query with the joint image/text visual model.

    VISUAL_EMBEDDING_PROVIDER=none -> None (visual branch skipped). The
    visual model maps the text query into the same space as the stored
    frame embeddings. Misconfiguration raises 503; call-time failure
    raises 502. Search itself never decodes video or calls a vision API —
    it only queries stored indexes.
    """
    from app.pipeline.visual import (
        VisualProviderError,
        VisualProviderNotConfiguredError,
        build_visual_provider,
    )

    if settings.visual_embedding_provider == "none":
        return None
    try:
        provider = build_visual_provider()
    except VisualProviderNotConfiguredError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    try:
        vectors = provider.embed_texts([query])
    except VisualProviderError as e:
        raise HTTPException(
            status_code=502, detail=f"visual embedding provider failed: {e}"
        ) from e
    return vectors[0] if vectors else None


@router.get("/v1/search", response_model=SearchResponse)
def search(
    q: str = Query(..., min_length=1, max_length=500),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SearchResponse:
    started = time.perf_counter()
    embedding = _maybe_embed(q)
    text_embed_done = time.perf_counter()
    visual_embedding = _maybe_embed_visual(q)
    visual_embed_done = time.perf_counter()
    hits = hybrid_search(
        db, user.id, q, embedding, limit=limit,
        query_visual_embedding=visual_embedding,
    )
    retrieval_done = time.perf_counter()
    response = SearchResponse(
        results=[
            SearchResult(
                memory_id=h.memory_id,
                title=h.title,
                summary=h.summary,
                category=h.category,
                creator_handle=h.creator_handle,
                platform=h.platform,
                processing_status=h.processing_status,
                source_status=h.source_status,
                score=h.score,
                evidence=[
                    SearchEvidence(
                        type=e.type,
                        snippet=e.snippet,
                        start_ms=e.start_ms,
                        end_ms=e.end_ms,
                    )
                    for e in h.evidence
                ],
            )
            for h in hits
        ],
        degraded=embedding is None and visual_embedding is None,
    )
    finished = time.perf_counter()
    log.info(
        "search latency total_ms=%.1f text_embed_ms=%.1f visual_embed_ms=%.1f "
        "retrieval_ms=%.1f response_ms=%.1f query_chars=%d limit=%d hits=%d "
        "degraded=%s",
        (finished - started) * 1000,
        (text_embed_done - started) * 1000,
        (visual_embed_done - text_embed_done) * 1000,
        (retrieval_done - visual_embed_done) * 1000,
        (finished - retrieval_done) * 1000,
        len(q),
        limit,
        len(hits),
        response.degraded,
    )
    return response
