"""Hybrid retrieval — PRD §26 (FTS + pgvector, RRF fusion) and §27 (evidence).

Two independent ranked lists are produced per query:
  * full-text rank over memory segments + caption (exact names, OCR, numbers),
  * cosine-similarity rank over pgvector embeddings (semantic / visual recall),
plus tag matches. They are merged with reciprocal-rank fusion so neither
signal needs score normalization. Each hit carries evidence explaining why it
matched — never raw model JSON (PRD §27, §8.9).

The SQL here is real and runs against PostgreSQL + pgvector; only the merge
helper and SQL builders are unit-tested (no live DB in tests).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

RRF_K = 60
READY_STATUSES = ("READY", "METADATA_ONLY")

# PRD §27 evidence types. Summary segments are system-generated descriptors,
# like tags, so summary matches surface as TAG evidence with a "Summary:" prefix.
EVIDENCE_FOR_MODALITY = {
    "speech": "SPEECH",
    "ocr": "OCR",
    "visual": "VISUAL",
    "caption": "CAPTION",
    "summary": "TAG",
}


def rrf_merge(ranked_id_lists: list[list[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    """Reciprocal-rank fusion: score(id) = sum(1 / (k + rank)) over lists.

    Returns (id, score) sorted by score desc. An id appearing in several lists
    outranks single-list ids; smaller k weights top ranks more steeply.
    """
    scores: dict[str, float] = {}
    for ranked in ranked_id_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def _ordered_unique(ids) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


FTS_SQL = """
SELECT m.id::text AS memory_id,
       ts_rank_cd(to_tsvector('english', s.content),
                  plainto_tsquery('english', :q)) AS score,
       s.modality AS modality,
       s.content AS content,
       s.start_ms AS start_ms,
       s.end_ms AS end_ms
FROM memory_segments s
JOIN memories m ON m.id = s.memory_id
JOIN source_items si ON si.id = m.source_item_id
WHERE si.user_id = CAST(:user_id AS uuid)
  AND m.processing_status IN ('READY', 'METADATA_ONLY')
  AND to_tsvector('english', s.content) @@ plainto_tsquery('english', :q)
ORDER BY score DESC
LIMIT :limit
"""

VEC_SQL = """
SELECT m.id::text AS memory_id,
       (s.embedding <=> CAST(:qvec AS vector)) AS distance,
       s.modality AS modality,
       s.content AS content,
       s.start_ms AS start_ms,
       s.end_ms AS end_ms
FROM memory_segments s
JOIN memories m ON m.id = s.memory_id
JOIN source_items si ON si.id = m.source_item_id
WHERE si.user_id = CAST(:user_id AS uuid)
  AND m.processing_status IN ('READY', 'METADATA_ONLY')
  AND s.embedding IS NOT NULL
ORDER BY s.embedding <=> CAST(:qvec AS vector)
LIMIT :limit
"""

TAG_SQL = """
SELECT m.id::text AS memory_id, t.tag AS tag
FROM memory_tags t
JOIN memories m ON m.id = t.memory_id
JOIN source_items si ON si.id = m.source_item_id
WHERE si.user_id = CAST(:user_id AS uuid)
  AND m.processing_status IN ('READY', 'METADATA_ONLY')
  AND t.tag ILIKE '%' || :q_escaped || '%'
LIMIT :limit
"""

# Visual search branch: cosine similarity between the query's joint
# image/text embedding and the stored per-frame visual embeddings.
# DISTINCT ON keeps each memory's best-matching frame (with its true
# timestamp for evidence); the outer ORDER BY ranks memories by that best
# distance. This branch touches the stored index only — no video decoding,
# no vision API call at search time.
VISUAL_SQL = """
SELECT best.memory_id, best.distance, best.timestamp_ms, best.frame_index,
       best.album_index
FROM (
  SELECT DISTINCT ON (sub.memory_id)
    sub.memory_id AS memory_id,
    sub.distance AS distance,
    sub.timestamp_ms AS timestamp_ms,
    sub.frame_index AS frame_index,
    sub.album_index AS album_index
  FROM (
    SELECT
      m.id::text AS memory_id,
      (f.embedding <=> CAST(:qvec_visual AS vector)) AS distance,
      f.timestamp_ms AS timestamp_ms,
      f.frame_index AS frame_index,
      f.album_index AS album_index
    FROM memory_frame_embeddings f
    JOIN memories m ON m.id = f.memory_id
    JOIN source_items si ON si.id = m.source_item_id
    WHERE si.user_id = CAST(:user_id AS uuid)
      AND m.processing_status IN ('READY', 'METADATA_ONLY')
      AND f.embedding IS NOT NULL
    ORDER BY f.embedding <=> CAST(:qvec_visual AS vector)
    LIMIT (:limit * 25)
  ) sub
  ORDER BY sub.memory_id, sub.distance
) best
ORDER BY best.distance
LIMIT :limit
"""


@dataclass
class Evidence:
    type: str  # VISUAL | SPEECH | OCR | CAPTION | TAG
    snippet: str
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None


@dataclass
class SearchHit:
    memory_id: uuid.UUID
    title: str | None
    summary: str | None
    category: str | None
    creator_handle: str | None
    platform: str
    processing_status: str
    source_status: str
    score: float
    evidence: list[Evidence] = field(default_factory=list)


def _snippet(content: str, prefix: str = "", limit: int = 200) -> str:
    text_ = " ".join(content.split())
    if len(text_) > limit:
        text_ = text_[:limit].rstrip() + "…"
    return f"{prefix}{text_}" if prefix else text_


def _escape_like(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fmt_ts(timestamp_ms: int | None) -> str:
    """Format a frame timestamp as M:SS for evidence (e.g. 18_000 -> "0:18").

    Pure (unit-testable). None -> "?:??" is never emitted: callers only pass
    stored timestamps.
    """
    total_s = max(0, int(timestamp_ms or 0) // 1000)
    return f"{total_s // 60}:{total_s % 60:02d}"


def _visual_evidence_snippet(
    timestamp_ms: int | None, album_index: int | None
) -> str:
    """User-facing reason for a visual hit, grounded ONLY in stored evidence
    (the frame's timestamp / photo number). Never invents a description of
    what the frame shows — the similarity score is the match signal.

    Pure (unit-testable).
    """
    if album_index is not None:
        return f"Visual match: photo {album_index + 1}"
    return f"Visual match near {_fmt_ts(timestamp_ms)}"


def hybrid_search(
    session: Session,
    user_id: uuid.UUID,
    query_text: str,
    query_embedding: list[float] | None,
    limit: int = 10,
    per_branch: int = 50,
    query_visual_embedding: list[float] | None = None,
) -> list[SearchHit]:
    """Run FTS + vector + tag + visual branches, fuse with RRF, return top
    hits with evidence.

    query_embedding=None -> text-vector branch skipped. query_visual_embedding=None
    -> visual branch skipped (FTS-only/text-only callers mark degraded).
    The visual branch matches the query's joint image/text embedding against
    stored per-frame visual embeddings — no video decoding at search time.
    """
    from app.models import Memory, SourceItem  # local import: avoid circulars

    params = {"user_id": str(user_id), "q": query_text, "limit": per_branch}
    fts_rows = session.execute(text(FTS_SQL), params).mappings().all()

    vec_rows: list = []
    if query_embedding is not None:
        # Vector literal built only from Python floats -> no injection surface.
        qvec = "[" + ",".join(repr(float(x)) for x in query_embedding) + "]"
        vec_rows = session.execute(
            text(VEC_SQL), {**params, "qvec": qvec}
        ).mappings().all()

    tag_rows = session.execute(
        text(TAG_SQL), {**params, "q_escaped": _escape_like(query_text)}
    ).mappings().all()

    visual_rows: list = []
    if query_visual_embedding is not None:
        qvec_visual = "[" + ",".join(
            repr(float(x)) for x in query_visual_embedding
        ) + "]"
        visual_rows = session.execute(
            text(VISUAL_SQL), {**params, "qvec_visual": qvec_visual}
        ).mappings().all()

    fts_ids = _ordered_unique(r["memory_id"] for r in fts_rows)
    vec_ids = _ordered_unique(r["memory_id"] for r in vec_rows)
    tag_ids = _ordered_unique(r["memory_id"] for r in tag_rows)
    visual_ids = _ordered_unique(r["memory_id"] for r in visual_rows)
    merged = rrf_merge([fts_ids, vec_ids, tag_ids, visual_ids])[:limit]
    if not merged:
        return []

    # Evidence: best segment per branch per memory (max 2 distinct types).
    seg_evidence: dict[str, list[tuple[str, str, Optional[int], Optional[int]]]] = {}
    for rows in (fts_rows, vec_rows):
        for r in rows:
            mid = r["memory_id"]
            if mid in seg_evidence:
                continue
            modality = r["modality"]
            ev_type = EVIDENCE_FOR_MODALITY.get(modality, "TAG")
            prefix = "Summary: " if modality == "summary" else ""
            seg_evidence.setdefault(mid, []).append(
                (ev_type, _snippet(r["content"], prefix), r["start_ms"], r["end_ms"])
            )
    # Visual evidence comes from the stored frame (timestamp/photo), never
    # from an invented description — the similarity IS the match signal.
    visual_evidence: dict[str, tuple[str, Optional[int]]] = {}
    for r in visual_rows:
        visual_evidence.setdefault(
            r["memory_id"],
            (
                _visual_evidence_snippet(r["timestamp_ms"], r["album_index"]),
                r["timestamp_ms"],
            ),
        )
    tag_evidence: dict[str, str] = {}
    for r in tag_rows:
        tag_evidence.setdefault(r["memory_id"], r["tag"])

    wanted = [uuid.UUID(mid) for mid, _ in merged]
    memories = {
        m.id: m
        for m in session.query(Memory).filter(Memory.id.in_(wanted)).all()
    }
    items = {
        i.id: i
        for i in session.query(SourceItem).filter(
            SourceItem.id.in_([m.source_item_id for m in memories.values()])
        ).all()
    }

    hits: list[SearchHit] = []
    for mid_str, score in merged:
        mid = uuid.UUID(mid_str)
        memory = memories.get(mid)
        if memory is None:
            continue
        item = items.get(memory.source_item_id)
        evidence: list[Evidence] = []
        seen_types: set[str] = set()
        for ev_type, snippet, start_ms, end_ms in seg_evidence.get(mid_str, [])[:2]:
            if ev_type not in seen_types:
                evidence.append(
                    Evidence(
                        type=ev_type,
                        snippet=snippet,
                        start_ms=start_ms,
                        end_ms=end_ms,
                    )
                )
                seen_types.add(ev_type)
        if mid_str in visual_evidence and "VISUAL" not in seen_types:
            vis_snippet, vis_ts = visual_evidence[mid_str]
            evidence.append(
                Evidence(type="VISUAL", snippet=vis_snippet, start_ms=vis_ts)
            )
            seen_types.add("VISUAL")
        if mid_str in tag_evidence and "TAG" not in seen_types:
            evidence.append(
                Evidence(type="TAG", snippet=f"Tag: {tag_evidence[mid_str]}")
            )
        hits.append(
            SearchHit(
                memory_id=mid,
                title=memory.title,
                summary=memory.summary,
                category=memory.category,
                creator_handle=item.creator_handle if item else None,
                platform=item.platform if item else "?",
                processing_status=memory.processing_status,
                source_status=item.source_status if item else "UNKNOWN",
                score=score,
                evidence=evidence,
            )
        )
    return hits
