"""Deterministic memory assembly — PRD §22/§23/§24 (skeleton).

This is the rule-based fallback used when no MemoryGenerator AI provider is
configured (Milestone 4 wires in the real one). It operates ONLY on structured
provider outputs — never on raw model JSON — and produces a usable
title/summary/category/tags record so the pipeline is end-to-end testable
without AI spend.
"""

from __future__ import annotations

import re
from collections import Counter

from app.pipeline.providers import (
    GeneratedMemory,
    PipelineEvidence,
)

# PRD §23 initial taxonomy (configurable later).
CATEGORIES = (
    "Educational",
    "Projects",
    "Food",
    "Fun",
    "Technology",
    "Fitness",
    "Travel",
    "Products",
    "Entertainment",
    "Other",
)

_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Food": ("recipe", "cook", "restaurant", "food", "dosa", "eat", "kitchen", "chef"),
    "Fitness": ("workout", "gym", "fitness", "exercise", "muscle", "cardio", "yoga", "protein"),
    "Technology": (
        "code", "coding", "python", "database", "server", "api", "software",
        "programming", "algorithm", "sharding", "postgres", "redis", "git",
    ),
    "Travel": ("travel", "flight", "hotel", "beach", "mountain", "city", "trip", "himalaya"),
    "Projects": ("build", "diy", "project", "tutorial", "how to", "make a"),
    "Entertainment": ("movie", "film", "song", "music", "comedy", "meme"),
    "Products": ("buy", "price", "discount", "product", "review", "unboxing"),
}

_STOPWORDS = frozenset(
    "the a an and or of to in on for with is are was were be been it its this that "
    "these those as at by from we you he she they them his her their our your my "
    "so do does did not no yes if then than too very can will just about into".split()
)

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'_-]*")


def _all_text(evidence: PipelineEvidence) -> str:
    parts = [s.text for s in evidence.transcript]
    parts += [o.description for o in evidence.visuals]
    parts += [s.text for s in evidence.ocr]
    if evidence.metadata and evidence.metadata.caption:
        parts.append(evidence.metadata.caption)
    return "\n".join(parts).lower()


def _pick_category(text: str) -> str:
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(k in text for k in keywords):
            return category
    return "Other"


def _pick_tags(text: str, limit: int = 8) -> list[str]:
    words = [w for w in _WORD_RE.findall(text) if w not in _STOPWORDS and len(w) > 2]
    return [w for w, _ in Counter(words).most_common(limit)]


def build_memory(evidence: PipelineEvidence) -> GeneratedMemory:
    """Assemble a GeneratedMemory from structured evidence (deterministic)."""
    text = _all_text(evidence)

    if evidence.visuals:
        title_src = evidence.visuals[0].description
    elif evidence.transcript:
        title_src = evidence.transcript[0].text
    elif evidence.metadata and evidence.metadata.caption:
        title_src = evidence.metadata.caption
    else:
        title_src = "Untitled memory"
    title = title_src.strip().split("\n")[0][:120] or "Untitled memory"

    bits = []
    if evidence.transcript:
        bits.append(f"{len(evidence.transcript)} speech segments")
    if evidence.visuals:
        bits.append(f"{len(evidence.visuals)} visual observations")
    if evidence.ocr:
        bits.append(f"{len(evidence.ocr)} on-screen text segments")
    detail = "; ".join(bits) if bits else "no modality evidence extracted"
    summary = f"Saved reel memory ({detail})."
    if evidence.transcript:
        first = evidence.transcript[0].text.strip()
        if first:
            summary += f" Opens with: {first[:200]}"

    return GeneratedMemory(
        title=title,
        summary=summary,
        category=_pick_category(text),
        tags=_pick_tags(text),
        language=None,
    )
