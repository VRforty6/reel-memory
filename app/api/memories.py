"""Memory API — PRD §39.

GET /v1/memories, GET /v1/memories/{id}, GET /v1/memories/{id}/status,
DELETE /v1/memories/{id} (SEC-007: deletes the record and all derived data),
POST /v1/memories/{id}/reprocess (explicit reprocess from a settled state),
POST /v1/memories/{id}/ask (GPT-style Q&A over the reel's evidence, with
optional web verification of factual claims).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session, selectinload

from app.capture.dedupe import should_reprocess
from app.config import settings
from app.auth import get_current_user
from app.db import get_db
from app.entitlements import (
    BUCKET_QUESTIONS,
    BUCKET_VERIFICATIONS,
    check_and_increment_quota,
)
from app.models import Category, Memory, MemoryCategory, ProcessingJob, SourceItem, User
from app.pipeline.providers import (
    ProviderError,
    ProviderNotConfiguredError,
    UnconfiguredChatProvider,
    build_providers,
)
from app.pipeline.state_machine import (
    ProcessingStatus,
    assert_transition,
    coerce,
    is_terminal,
)
from app.qa import (
    EvidenceRef,
    VerificationOutcome,
    album_index_from_metadata,
    answer_question,
    verify_claims,
)
from app.intel import (
    InferredIntent as InferredIntentModel,
    build_brief,
    classify_intent,
    extract_actions,
    slugify,
)
from app.schemas import (
    ActionEvidence as ActionEvidenceSchema,
    ActionItem as ActionItemSchema,
    CategoryAssignmentOut,
    ActionsResponse,
    AskCitation,
    AskRequest,
    AskResponse,
    BriefRequest,
    BriefResponse,
    InferredIntent as InferredIntentSchema,
    MemoryDetail,
    MemoryListResponse,
    MemorySegmentOut,
    MemorySource,
    MemoryStatusResponse,
    MemorySummary,
    MemoryTagOut,
    VerificationFinding as VerificationFindingSchema,
    VerificationResult,
    VerificationSource,
    VisualBackfillResponse,
    VisualIndexStatusResponse,
)

router = APIRouter()

_REPROCESSABLE = {
    ProcessingStatus.FAILED_PERMANENT,
    ProcessingStatus.READY,
    ProcessingStatus.METADATA_ONLY,
    ProcessingStatus.SOURCE_UNAVAILABLE,
    ProcessingStatus.SOURCE_REQUIRES_ACCESS,
}


def _get_memory(db: Session, user_id: uuid.UUID, memory_id: uuid.UUID) -> Memory:
    memory = (
        db.query(Memory)
        .join(SourceItem, SourceItem.id == Memory.source_item_id)
        .filter(Memory.id == memory_id, SourceItem.user_id == user_id)
        .first()
    )
    if memory is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return memory


def _category_outputs(memory: Memory) -> tuple[str | None, list[CategoryAssignmentOut]]:
    assignments = sorted(
        getattr(memory, "category_assignments", []),
        key=lambda a: (not a.is_primary, a.category.path),
    )
    outputs = [
        CategoryAssignmentOut(
            id=a.category.id,
            name=a.category.name,
            path=a.category.path,
            is_primary=a.is_primary,
            confidence=a.confidence,
            source=a.source,
        )
        for a in assignments
    ]
    primary = next((a for a in assignments if a.is_primary), None)
    return (primary.category.path if primary else None), outputs


def _summary(memory: Memory) -> MemorySummary:
    category_path, category_assignments = _category_outputs(memory)
    return MemorySummary(
        id=memory.id,
        title=memory.title,
        summary=memory.summary,
        category=memory.category,
        category_path=category_path,
        category_assignments=category_assignments,
        platform=memory.source_item.platform,
        media_kind=memory.media_kind,
        processing_status=memory.processing_status,
        has_thumbnail=memory.thumbnail_jpeg is not None,
        created_at=memory.created_at,
    )


@router.get("/v1/memories", response_model=MemoryListResponse)
def list_memories(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    category_path: str | None = Query(None, max_length=800),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MemoryListResponse:
    query = (
        db.query(Memory)
        .options(
            selectinload(Memory.source_item),
            selectinload(Memory.category_assignments).selectinload(MemoryCategory.category),
        )
        .join(SourceItem, SourceItem.id == Memory.source_item_id)
        .filter(SourceItem.user_id == user.id)
    )
    if category_path:
        normalized = category_path.strip().strip("/")
        query = (
            query.join(MemoryCategory, MemoryCategory.memory_id == Memory.id)
            .join(Category, Category.id == MemoryCategory.category_id)
            .filter(
                MemoryCategory.is_primary.is_(True),
                (Category.path == normalized)
                | Category.path.startswith(normalized + "/", autoescape=True),
            )
        )
    memories = (
        query.order_by(Memory.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return MemoryListResponse(
        memories=[_summary(m) for m in memories], limit=limit, offset=offset
    )


@router.get("/v1/memories/{memory_id}", response_model=MemoryDetail)
def get_memory(
    memory_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MemoryDetail:
    memory = _get_memory(db, user.id, memory_id)
    item = memory.source_item
    category_path, category_assignments = _category_outputs(memory)
    return MemoryDetail(
        id=memory.id,
        title=memory.title,
        summary=memory.summary,
        category=memory.category,
        category_path=category_path,
        category_assignments=category_assignments,
        language=memory.language,
        processing_status=memory.processing_status,
        processing_version=memory.processing_version,
        has_thumbnail=memory.thumbnail_jpeg is not None,
        source=MemorySource(
            platform=item.platform,
            canonical_url=item.canonical_url,
            original_url=item.original_url,
            creator_handle=item.creator_handle,
            caption=item.caption,
            published_at=item.published_at,
            source_status=item.source_status,
        ),
        tags=[MemoryTagOut(tag=t.tag, confidence=t.confidence) for t in memory.tags],
        segments=[
            MemorySegmentOut(
                modality=s.modality,
                start_ms=s.start_ms,
                end_ms=s.end_ms,
                content=s.content,
                album_index=album_index_from_metadata(s.metadata_json),
            )
            for s in sorted(
                memory.segments, key=lambda s: (s.start_ms is None, s.start_ms or 0)
            )
        ],
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


@router.get("/v1/memories/{memory_id}/status", response_model=MemoryStatusResponse)
def get_memory_status(
    memory_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MemoryStatusResponse:
    memory = _get_memory(db, user.id, memory_id)
    latest_job = (
        db.query(ProcessingJob)
        .filter(ProcessingJob.memory_id == memory.id)
        .order_by(ProcessingJob.id.desc())
        .first()
    )
    return MemoryStatusResponse(
        id=memory.id,
        processing_status=memory.processing_status,
        stage=latest_job.stage if latest_job else None,
        attempt_count=latest_job.attempt_count if latest_job else 0,
        failure_code=latest_job.failure_code if latest_job else None,
        failure_message=latest_job.failure_message if latest_job else None,
    )


@router.get("/v1/memories/{memory_id}/thumbnail")
def get_memory_thumbnail(
    memory_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """One small JPEG persisted once at ingest (migration 006).

    404 when the memory has no thumbnail (ingested before migration 006,
    or thumbnail generation failed) — the app shows a placeholder.
    Thumbnails are never reprocessed or backfilled.
    """
    memory = _get_memory(db, user.id, memory_id)
    if not memory.thumbnail_jpeg:
        raise HTTPException(status_code=404, detail="no thumbnail for this memory")
    return Response(content=memory.thumbnail_jpeg, media_type="image/jpeg")


@router.delete("/v1/memories/{memory_id}", status_code=204)
def delete_memory(
    memory_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    """SEC-007: delete the memory record and ALL derived data (segments, tags,
    jobs, captures, source item). The original platform URL is not touched."""
    memory = _get_memory(db, user.id, memory_id)
    item = memory.source_item
    db.delete(item)  # cascades to memory, segments, tags, jobs, captures
    db.commit()
    return Response(status_code=204)


@router.post("/v1/memories/{memory_id}/reprocess", status_code=202)
def reprocess_memory(
    memory_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MemoryStatusResponse:
    """Explicit reprocess (PRD §13): allowed only from a settled state.

    Terminal failure states (e.g. FAILED_PERMANENT) and settled data states
    (READY, METADATA_ONLY, ...) may be reprocessed. FAILED_RETRYABLE is
    owned by the worker's automatic retry loop and QUEUED/processing states
    are still in flight — both return 409 here so exactly one owner drives
    the retry.
    """
    memory = _get_memory(db, user.id, memory_id)
    status = coerce(memory.processing_status)
    if status not in _REPROCESSABLE and not is_terminal(status):
        raise HTTPException(
            status_code=409,
            detail=f"memory is still processing ({status.value}); cannot reprocess yet",
        )
    # should_reprocess(explicit=True) is always True; the guard above is the
    # real policy — assert the transition is legal in the state machine.
    assert_transition(status, ProcessingStatus.QUEUED)
    _ = should_reprocess(
        memory.processing_status,
        memory.processing_version,
        settings.processing_version,
        explicit=True,
    )
    memory.processing_status = ProcessingStatus.QUEUED.value
    memory.processing_version = settings.processing_version
    job = ProcessingJob(
        memory_id=memory.id,
        status="QUEUED",
        stage=ProcessingStatus.QUEUED.value,
        attempt_count=0,
        failure_code=None,
        failure_message=None,
    )
    db.add(job)
    db.commit()
    return MemoryStatusResponse(
        id=memory.id,
        processing_status=memory.processing_status,
        stage=job.stage,
        attempt_count=0,
    )


# --- visual search: index status + backfill ----------------------------------


@router.get(
    "/v1/memories/{memory_id}/visual-index",
    response_model=VisualIndexStatusResponse,
)
def visual_index_status(
    memory_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> VisualIndexStatusResponse:
    """Honest visual-backfill classification for one memory.

    VISUAL_INDEX_READY is reported only when frame embedding rows actually
    exist. VISUAL_BACKFILL_AVAILABLE means the source bytes can still be
    obtained (e.g. the upload file has not been cleaned up yet).
    VISUAL_BACKFILL_SOURCE_UNAVAILABLE means the original media is gone
    (deleted after processing per PRD §8.6, or never acquirable without
    authentication) — visual search cannot be enabled for this memory short
    of re-uploading it.
    """
    from sqlalchemy import func

    from app.models import MemoryFrameEmbedding
    from app.pipeline.frame_index import classify_visual_backfill

    memory = _get_memory(db, user.id, memory_id)
    status, reason = classify_visual_backfill(db, memory)
    count = (
        db.query(func.count(MemoryFrameEmbedding.id))
        .filter(MemoryFrameEmbedding.memory_id == memory.id)
        .scalar()
    )
    return VisualIndexStatusResponse(
        id=memory.id,
        status=status,
        reason=reason,
        frame_count=int(count or 0),
    )


@router.post(
    "/v1/memories/{memory_id}/visual-backfill",
    response_model=VisualBackfillResponse,
)
def visual_backfill(
    memory_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> VisualBackfillResponse:
    """Backfill visual frame embeddings for one memory from its source media.

    Only re-runs visual frame indexing (frame selection + local embedding) —
    never transcription, the paid vision/OCR stages, or the text index. 409
    when the source media is unavailable: the endpoint refuses to claim
    success from text/vision-description data alone.
    """
    from app.pipeline.frame_index import (
        VisualBackfillUnavailable,
        run_visual_backfill,
    )

    memory = _get_memory(db, user.id, memory_id)
    try:
        result = run_visual_backfill(db, memory)
    except VisualBackfillUnavailable as e:
        raise HTTPException(status_code=409, detail=f"{e.status}: {e.reason}") from e
    return VisualBackfillResponse(
        id=memory.id,
        status=result["status"],
        reason=result["reason"],
        frames_indexed=result["frames_indexed"],
    )


# --- conversational Q&A ------------------------------------------------------

_ASKABLE = {ProcessingStatus.READY, ProcessingStatus.METADATA_ONLY}
_EVIDENCE_MODALITIES = ("speech", "visual", "ocr", "caption", "article")


def _evidence_from_memory(memory: Memory) -> list[EvidenceRef]:
    refs = [
        EvidenceRef(
            modality=s.modality,
            start_ms=s.start_ms,
            end_ms=s.end_ms,
            content=s.content,
            album_index=album_index_from_metadata(s.metadata_json),
        )
        for s in memory.segments
        if s.modality in _EVIDENCE_MODALITIES and (s.content or "").strip()
    ]
    return sorted(
        refs, key=lambda r: (r.modality, r.start_ms is None, r.start_ms or 0)
    )


def _require_chat_providers():
    """Build providers and enforce a configured chat provider.

    Raises 503 when chat is unavailable. A missing *search* provider does NOT
    raise — verification degrades to status "unavailable", never faked.
    """
    try:
        providers = build_providers()
    except ProviderNotConfiguredError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if isinstance(providers.chat, UnconfiguredChatProvider):
        raise HTTPException(
            status_code=503,
            detail=(
                "chat provider is not configured: set CHAT_PROVIDER=openai and "
                "OPENAI_API_KEY (see .env.example)"
            ),
        )
    return providers


def _get_askable_memory(db: Session, user_id: uuid.UUID, memory_id: uuid.UUID) -> Memory:
    memory = _get_memory(db, user_id, memory_id)
    status = coerce(memory.processing_status)
    if status not in _ASKABLE:
        raise HTTPException(
            status_code=409,
            detail=(
                f"memory is {status.value}; this is available once "
                "processing settles (READY or METADATA_ONLY)"
            ),
        )
    return memory


def _verification_schema(outcome: VerificationOutcome) -> VerificationResult:
    return VerificationResult(
        status=outcome.status,
        message=outcome.message,
        findings=[
            VerificationFindingSchema(
                claim=f.claim,
                verdict=f.verdict,
                sources=[
                    VerificationSource(title=s["title"], url=s["url"])
                    for s in f.sources
                ],
            )
            for f in outcome.findings
        ],
    )


@router.post("/v1/memories/{memory_id}/ask", response_model=AskResponse)
def ask_memory(
    memory_id: uuid.UUID,
    req: AskRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AskResponse:
    """GPT-style Q&A over one reel's evidence.

    The chat model answers ONLY from the memory's modality segments
    (speech/visual/OCR/caption), cites each claim with modality + timestamp,
    and says "The reel doesn't show or say this" when evidence is absent.
    With verify=true, factual claims are additionally cross-checked against
    the web — reported separately, never mixed into "what the reel says".
    """
    memory = _get_askable_memory(db, user.id, memory_id)

    # Quota: the question always counts; verification counts extra.
    check_and_increment_quota(db, user, BUCKET_QUESTIONS)
    if req.verify:
        check_and_increment_quota(db, user, BUCKET_VERIFICATIONS)

    providers = _require_chat_providers()

    evidence = _evidence_from_memory(memory)
    try:
        parsed = answer_question(evidence, req.question, providers.chat)
    except ProviderError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    verification = None
    if req.verify:
        try:
            outcome = verify_claims(parsed.claims, providers.search, providers.chat)
        except ProviderError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        verification = _verification_schema(outcome)

    return AskResponse(
        answer=parsed.answer,
        citations=[
            AskCitation(
                modality=c.modality, timestamp_ms=c.timestamp_ms, quote=c.quote,
                album_index=c.album_index,
            )
            for c in parsed.citations
        ],
        evidence_coverage=parsed.evidence_coverage,
        verification=verification,
    )


# --- intelligence layer: actions + decision brief ------------------------------


@router.post("/v1/memories/{memory_id}/actions", response_model=ActionsResponse)
def memory_actions(
    memory_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ActionsResponse:
    """Extract concrete to-dos the content explicitly recommends.

    Each action carries an honest priority (P0/P1/P2 = impact x effort
    judgment), an effort size, and grounding evidence (modality, optional
    timestamp, exact quote). Content with no actionable advice returns an
    empty list — never filler.
    """
    memory = _get_askable_memory(db, user.id, memory_id)
    check_and_increment_quota(db, user, BUCKET_QUESTIONS)
    providers = _require_chat_providers()

    evidence = _evidence_from_memory(memory)
    try:
        actions = extract_actions(evidence, providers.chat)
    except ProviderError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    return ActionsResponse(
        actions=[
            ActionItemSchema(
                title=a.title,
                detail=a.detail,
                priority=a.priority,
                effort=a.effort,
                evidence=ActionEvidenceSchema(
                    modality=a.evidence.modality,
                    timestamp_ms=a.evidence.timestamp_ms,
                    quote=a.evidence.quote,
                    album_index=a.evidence.album_index,
                ),
            )
            for a in actions
        ]
    )


@router.post("/v1/memories/{memory_id}/brief", response_model=BriefResponse)
def memory_brief(
    memory_id: uuid.UUID,
    req: BriefRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BriefResponse:
    """Decision brief: "should I spend time on this?"

    Pipeline: (1) infer the user's intent from the content + goal (model
    classification over domain/intent, with the user's recent intents as weak
    context; skipped when the client sends a corrected `intent_override`);
    (2) build the brief framed for that intent (purchase -> comparison +
    value-for-money verdict; business validation -> risks + validation steps;
    act/portfolio -> prioritized action list); (3) web-verify the key claims
    ("what the content claims" stays separate from "what the web supports").
    The inferred intent is returned for the client to show ("Looks like
    you're deciding whether to buy this") and recorded in the user's intent
    history (capped, newest last) to improve follow-up classifications.
    """
    memory = _get_askable_memory(db, user.id, memory_id)

    # Quota: the brief always burns a question AND a verification (it
    # web-validates its key claims).
    check_and_increment_quota(db, user, BUCKET_QUESTIONS)
    check_and_increment_quota(db, user, BUCKET_VERIFICATIONS)

    providers = _require_chat_providers()

    evidence = _evidence_from_memory(memory)
    history = _intent_history(user)

    if req.intent_override is not None:
        # User-corrected intent: trust it, skip the model classification.
        intent = InferredIntentModel(
            domain=slugify(req.intent_override.domain, "other"),
            intent=slugify(req.intent_override.intent, "explore"),
            confidence="high",
            label=(
                f"corrected to {slugify(req.intent_override.intent, 'explore')} "
                f"in {slugify(req.intent_override.domain, 'other')}"
            ),
            source="user",
        )
    else:
        try:
            intent = classify_intent(
                evidence, req.goal, providers.chat, recent=history[-5:]
            )
        except ProviderError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

    try:
        brief = build_brief(evidence, req.goal, providers.chat, intent=intent)
    except ProviderError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    try:
        outcome = verify_claims(brief.key_claims, providers.search, providers.chat)
    except ProviderError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    _record_intent(db, user, history, intent, memory.id)

    return BriefResponse(
        summary=brief.summary,
        key_claims=brief.key_claims,
        validation=_verification_schema(outcome),
        usefulness_assessment=brief.usefulness_assessment,
        effort_estimate=brief.effort_estimate,
        open_question=brief.open_question,
        inferred_intent=InferredIntentSchema(
            domain=intent.domain,
            intent=intent.intent,
            confidence=intent.confidence,  # type: ignore[arg-type]
            label=intent.label,
            source=intent.source,  # type: ignore[arg-type]
        ),
    )


_INTENT_HISTORY_CAP = 20


def _intent_history(user: User) -> list[dict]:
    """The user's recent inferred intents, oldest first. Tolerates a missing
    or malformed column value (e.g. pre-migration rows)."""
    raw = user.intent_history or []
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict)][:50]


def _record_intent(
    db: Session,
    user: User,
    history: list[dict],
    intent: InferredIntentModel,
    memory_id: uuid.UUID,
) -> None:
    """Append the intent to the user's history (capped, newest last).

    Reassigns (never mutates in place) so SQLAlchemy tracks the JSON change.
    """
    entry = {
        "domain": intent.domain,
        "intent": intent.intent,
        "label": intent.label,
        "confidence": intent.confidence,
        "source": intent.source,
        "memory_id": str(memory_id),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    user.intent_history = (history + [entry])[-_INTENT_HISTORY_CAP:]
    db.commit()
