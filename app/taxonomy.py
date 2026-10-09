"""Hierarchical, evidence-aware taxonomy for personal memory organization.

The flat ``memories.category`` column is kept as a compatibility leaf label, but
canonical organization lives in ``categories`` + ``memory_categories``.  The
classifier is deliberately constrained: it selects from a controlled tree and
prefers a deep leaf only when the evidence is strong enough.  Tags/entities
remain independent cross-cutting signals.

This module is local/keyless.  A future local-LLM classifier can choose among
the same paths without changing the database or API contracts.
"""
from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Category, Memory, MemoryCategory, MemorySegment, MemoryTag, SourceItem


@dataclass(frozen=True)
class TaxonomyRule:
    path: tuple[str, ...]
    strong: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    min_score: float = 8.0


@dataclass(frozen=True)
class TaxonomyDecision:
    primary_path: tuple[str, ...]
    confidence: float
    secondary_paths: tuple[tuple[str, ...], ...] = ()
    score: float = 0.0


# High-precision leaves first.  Rules intentionally use product/domain phrases
# rather than generic words such as "food" or "exercise" that caused the old
# classifier to put AI-learning and motorsport posts into Food/Fitness.
RULES: tuple[TaxonomyRule, ...] = (
    # --- AI & Technology -------------------------------------------------
    TaxonomyRule(
        ("Technology & AI", "Artificial Intelligence", "AI Coding & Agents", "Claude Code"),
        strong=("claude code", "claude-code", "anthropic claude code"),
        keywords=("claudecode", "anthropic", "claude"), min_score=11,
    ),
    TaxonomyRule(
        ("Technology & AI", "Artificial Intelligence", "AI Coding & Agents", "Agent Skills & MCP"),
        strong=("agent skills", "claude skills", "claudeskills", "mcp server", "model context protocol", "book to skill", "skills add"),
        keywords=("mcp", "skills", "skill", "agents"), min_score=10,
    ),
    TaxonomyRule(
        ("Technology & AI", "Artificial Intelligence", "AI Coding & Agents", "AI Coding Tools"),
        strong=("coding agent", "ai coding", "codex", "cursor", "copilot cli", "amp agent", "vibe coding"),
        keywords=("codex", "cursor", "copilot", "coding", "developer"), min_score=10,
    ),
    TaxonomyRule(
        ("Technology & AI", "Artificial Intelligence", "AI Coding & Agents", "AI Developer Resources"),
        strong=("github repos", "repo links", "build code better", "ai gateway", "omniroute", "book to skill", "developer resources"),
        keywords=("github", "repos", "repo", "codex", "cursor", "copilot", "claude", "agents"), min_score=10,
    ),
    TaxonomyRule(
        ("Technology & AI", "Artificial Intelligence", "AI Coding & Agents", "AI Agents & Automation"),
        strong=("ai agent", "agentic", "digital worker", "computer use", "hermes agent", "hermes", "voice agent", "autonomous agent"),
        keywords=("agents", "automation", "hermes", "worker"), min_score=10,
    ),
    TaxonomyRule(
        ("Technology & AI", "Artificial Intelligence", "AI Productivity & Learning", "AI Learning Skills"),
        strong=("learning audit", "use ai every day", "brain sharp", "without ai", "learn with ai", "ai skills", "ai literacy"),
        keywords=("learning", "learn", "chatgpt", "claude", "exercise"), min_score=11,
    ),
    TaxonomyRule(
        ("Technology & AI", "Artificial Intelligence", "AI Productivity & Learning", "AI Writing & Humanization"),
        strong=("humanizer", "ai writing", "ai slop", "anti-ai-slop", "writing patterns", "ai detection"),
        keywords=("humanizer", "writing", "slop", "rewrite"), min_score=9,
    ),
    TaxonomyRule(
        ("Technology & AI", "Artificial Intelligence", "AI Productivity & Learning", "AI Research & Knowledge Tools"),
        strong=("open notebook", "research assistant", "chat with content", "knowledge base", "second brain", "personal knowledge"),
        keywords=("research", "notebook", "knowledge", "sources"), min_score=10,
    ),
    TaxonomyRule(
        ("Technology & AI", "Artificial Intelligence", "AI Tools & Resources", "General AI Tools"),
        strong=("ai tools", "ai services", "tools and services", "artificial intelligence tools"),
        keywords=("ai", "tools", "services", "models"), min_score=10,
    ),
    TaxonomyRule(
        ("Technology & AI", "Artificial Intelligence", "AI Models & Infrastructure", "AI Models & APIs"),
        strong=("llm api", "model api", "free ai models", "gemini api", "openai api", "inference api", "model context"),
        keywords=("llm", "models", "gemini", "inference", "api"), min_score=11,
    ),
    TaxonomyRule(
        ("Technology & AI", "Artificial Intelligence", "AI Models & Infrastructure", "Local AI"),
        strong=("local llm", "run locally", "ollama", "local model", "self hosted ai", "self-hosted ai"),
        keywords=("ollama", "local", "model", "gpu", "vram"), min_score=10,
    ),
    TaxonomyRule(
        ("Technology & AI", "Artificial Intelligence", "AI Models & Infrastructure", "Computer Vision & OCR"),
        strong=("ocr", "optical character recognition", "computer vision", "vision model", "openclip", "image embedding"),
        keywords=("ocr", "vision", "frames", "image", "embedding"), min_score=9,
    ),
    TaxonomyRule(
        ("Technology & AI", "Artificial Intelligence", "AI Models & Infrastructure", "Speech & Transcription"),
        strong=("whisper", "speech to text", "speech-to-text", "transcription", "voice model"),
        keywords=("whisper", "transcribe", "speech", "audio"), min_score=9,
    ),
    TaxonomyRule(
        ("Technology & AI", "Software Development", "Mobile Development"),
        strong=("android app", "mobile app", "mobile interface", "mobile ui", "jetpack compose", "react native", "expo app", "apk"),
        keywords=("android", "mobile", "app", "kotlin", "compose"), min_score=10,
    ),
    TaxonomyRule(
        ("Technology & AI", "Software Development", "Web Development & UI"),
        strong=("next.js", "nextjs", "landing page", "web design", "website", "frontend", "user interface", "interface design"),
        keywords=("nextjs", "website", "frontend", "ui", "css", "interface"), min_score=10,
    ),
    TaxonomyRule(
        ("Technology & AI", "Software Development", "Backend, Data & Databases"),
        strong=("postgresql", "postgres", "database", "data pipeline", "fastapi", "sql server", "backend api", "pgvector"),
        keywords=("database", "postgres", "sql", "backend", "api", "data"), min_score=11,
    ),
    TaxonomyRule(
        ("Technology & AI", "Software Development", "Automation & Integrations"),
        strong=("connector", "webhook", "integration", "api integration", "social media ingestion", "workflow automation"),
        keywords=("connector", "integration", "automation", "workflow", "ingestion"), min_score=10,
    ),
    TaxonomyRule(
        ("Technology & AI", "Hardware & Computing", "PC Setups & Hardware"),
        strong=("pc setup", "computer setup", "desktop setup", "gpu", "graphics card", "monitor setup", "shelving unit"),
        keywords=("computer", "desktop", "monitor", "gpu", "hardware", "cables"), min_score=10,
    ),

    # --- Business / finance ----------------------------------------------
    TaxonomyRule(
        ("Business & Work", "Entrepreneurship", "Business Models & Strategy"),
        strong=("business model", "business strategy", "revenue model", "intellectual property", "ip creation", "go to market", "go-to-market"),
        keywords=("business", "model", "strategy", "revenue", "brands", "entrepreneurship"), min_score=10,
    ),
    TaxonomyRule(
        ("Business & Work", "Entrepreneurship", "Startups & Products"),
        strong=("startup", "product idea", "product market fit", "product-market fit", "saas", "founder"),
        keywords=("startup", "founder", "product", "saas"), min_score=10,
    ),
    TaxonomyRule(
        ("Business & Work", "Operations", "Business Automation"),
        strong=("business automation", "automate admin", "operations automation", "ai receptionist", "workflow"),
        keywords=("automation", "operations", "workflow", "admin"), min_score=11,
    ),
    TaxonomyRule(
        ("Business & Work", "Marketing & Sales", "Client Acquisition"),
        strong=("client acquisition", "lead generation", "sales leads", "cold outreach", "marketing funnel", "book a call"),
        keywords=("leads", "sales", "client", "outreach", "marketing"), min_score=10,
    ),
    TaxonomyRule(
        ("Business & Work", "Careers", "Jobs & Job Search"),
        strong=("job search", "job application", "resume", "interview", "career", "hiring"),
        keywords=("jobs", "career", "resume", "interview", "hiring"), min_score=10,
    ),
    TaxonomyRule(
        ("Finance & Property", "Real Estate", "Property Market"),
        strong=("real estate", "property sales", "housing market", "home prices", "mortgage"),
        keywords=("property", "realestate", "housing", "mortgage", "investment"), min_score=9,
    ),
    TaxonomyRule(
        ("Finance & Property", "Investing", "Markets & Investing"),
        strong=("stock market", "investing", "investment", "portfolio", "etf", "stocks"),
        keywords=("invest", "stocks", "market", "finance", "portfolio"), min_score=10,
    ),

    # --- Health -----------------------------------------------------------
    TaxonomyRule(
        ("Health & Fitness", "Training", "Workouts & Strength"),
        strong=("workout", "strength training", "gym workout", "exercise routine", "muscle building", "cardio workout"),
        keywords=("workout", "gym", "strength", "muscle", "cardio", "sets", "reps"), min_score=10,
    ),
    TaxonomyRule(
        ("Health & Fitness", "Nutrition & Weight", "Fat Loss & Nutrition"),
        strong=("fat loss", "visceral fat", "calorie deficit", "weight loss", "protein intake", "nutrition"),
        keywords=("fat", "calories", "protein", "nutrition", "weight"), min_score=9,
    ),
    TaxonomyRule(
        ("Health & Fitness", "Nutrition & Weight", "Fasting & Metabolic Health"),
        strong=("intermittent fasting", "24 hour fast", "24-hour fast", "autophagy", "fasting"),
        keywords=("fasting", "autophagy", "metabolic", "fast"), min_score=8,
    ),
    TaxonomyRule(
        ("Health & Fitness", "Wellness", "Mindfulness & Mental Wellness"),
        strong=("mindfulness", "breathing exercise", "meditation", "stress relief", "mental health"),
        keywords=("mindfulness", "breathing", "meditation", "wellness", "stress"), min_score=9,
    ),

    # --- Food / travel ----------------------------------------------------
    TaxonomyRule(
        ("Food & Drink", "Cooking", "Recipes & Cooking"),
        strong=("recipe", "how to cook", "ingredients", "bake", "cooking", "chef"),
        keywords=("recipe", "cook", "ingredients", "baking", "kitchen"), min_score=10,
    ),
    TaxonomyRule(
        ("Food & Drink", "Dining", "Restaurants & Food Spots"),
        strong=("restaurant", "food spot", "where to eat", "restaurant menu", "cafe", "street food"),
        keywords=("restaurant", "cafe", "dining"), min_score=10,
    ),
    TaxonomyRule(
        ("Food & Drink", "Nightlife", "Bars & Cocktails"),
        strong=("best bars", "bar in", "cocktail", "nightlife", "50 best bars", "bartender"),
        keywords=("bars", "bar", "cocktail", "nightlife", "whiskey"), min_score=9,
    ),
    TaxonomyRule(
        ("Travel & Places", "Travel Planning", "Flights & Hotels"),
        strong=("flight booking", "cheap flights", "hotel booking", "travel hack", "airfare"),
        keywords=("flight", "hotel", "airfare", "booking", "travel"), min_score=10,
    ),
    TaxonomyRule(
        ("Travel & Places", "Destinations", "Cities & Places"),
        strong=("travel guide", "things to do", "places to visit", "city guide", "destination"),
        keywords=("travel", "city", "destination", "visit", "trip"), min_score=11,
    ),

    # --- Entertainment / sports ------------------------------------------
    TaxonomyRule(
        ("Entertainment & Culture", "Film & TV", "Film Promotions & Celebrities"),
        strong=("promos", "during promos", "share an energetic dance during", "film promotion", "movie promotion", "actors", "actress", "celebrity"),
        keywords=("promos", "actors", "actress", "celebrity", "cinema", "indiancinema"), min_score=10,
    ),
    TaxonomyRule(
        ("Entertainment & Culture", "Film & TV", "Movies & Cinema"),
        strong=("movie", "film", "cinema", "remake", "bollywood", "tollywood", "kollywood", "movie review"),
        keywords=("movie", "films", "cinema", "remakes", "actor", "actress"), min_score=9,
    ),
    TaxonomyRule(
        ("Entertainment & Culture", "Comedy", "Stand-up & Humor"),
        strong=("stand up comedy", "stand-up comedy", "comicstaan", "comedian", "comedy"),
        keywords=("comedy", "comedian", "jokes", "roast", "humor"), min_score=8,
    ),
    TaxonomyRule(
        ("Entertainment & Culture", "Music & Dance", "Music & Dance"),
        strong=("dance", "song", "music", "guitar", "concert", "choreography"),
        keywords=("dance", "music", "song", "guitar", "concert"), min_score=10,
    ),
    TaxonomyRule(
        ("Sports & Motorsports", "Motorsport", "Formula 1 & Grand Prix"),
        strong=("formula 1", "f1", "grand prix", "grandprix", "singapore gp", "singaporegp", "scuderia ferrari", "scuderiaferrari", "ferrari", "macau grand prix", "race week", "raceweek", "street circuit", "streetcircuit"),
        keywords=("ferrari", "grandprix", "racing", "circuit", "scuderia", "sgp"), min_score=9,
    ),
    TaxonomyRule(
        ("Sports & Motorsports", "Gaming", "Racing Games"),
        strong=("racing game", "dlss", "rally looks", "rally game", "sim racing", "forza", "assetto corsa"),
        keywords=("dlss", "rally", "gaming", "game", "simracing"), min_score=9,
    ),
    TaxonomyRule(
        ("Sports & Motorsports", "Football", "Football"),
        strong=("steve coppell", "steve copper", "united legend", "reading manager", "football manager", "premier league", "manchester united", "football club"),
        keywords=("football", "premier", "manchester", "united", "reading"), min_score=10,
    ),
    TaxonomyRule(
        ("Sports & Motorsports", "Basketball", "Basketball"),
        strong=("basketball", "nba", "three pointer", "dunk"),
        keywords=("basketball", "nba", "dunk"), min_score=8,
    ),

    # --- Learning / hobbies / society ------------------------------------
    TaxonomyRule(
        ("Learning & Education", "Learning Methods", "Study & Skill Building"),
        strong=("how to learn", "study method", "learning method", "skill building", "practice drill"),
        keywords=("learn", "study", "practice", "skill"), min_score=12,
    ),
    TaxonomyRule(
        ("Learning & Education", "Languages", "Language Learning"),
        strong=("learn spanish", "learn french", "language lesson", "vocabulary", "pronunciation"),
        keywords=("spanish", "french", "language", "vocabulary"), min_score=9,
    ),
    TaxonomyRule(
        ("Lifestyle & Hobbies", "Games", "Chess"),
        strong=("chess", "opening", "checkmate", "fried liver"),
        keywords=("chess", "checkmate", "bishop", "knight"), min_score=8,
    ),
    TaxonomyRule(
        ("Lifestyle & Hobbies", "Making", "DIY & Crafts"),
        strong=("diy", "woodworking", "crochet", "knitting", "handmade", "customizing", "customizing this entire keyboard", "custom keyboard", "keycap"),
        keywords=("diy", "craft", "crochet", "knitting", "woodworking", "customizing", "keyboard", "keycap"), min_score=9,
    ),
    TaxonomyRule(
        ("Lifestyle & Hobbies", "Photography & Video", "Photography"),
        strong=("photography", "camera composition", "photo tips", "camera settings"),
        keywords=("photography", "camera", "photo", "composition"), min_score=10,
    ),
    TaxonomyRule(
        ("Lifestyle & Hobbies", "Home & Garden", "Gardening"),
        strong=("gardening", "grow tomatoes", "plants", "balcony garden"),
        keywords=("gardening", "plants", "tomatoes", "garden"), min_score=9,
    ),
    TaxonomyRule(
        ("News & Society", "Technology & Society", "Cybersecurity & Account Security"),
        strong=("unauthorised control", "unauthorized control", "gainedunauthorisedcontrolofoursocialmedia", "gainedunauthorizedcontrolofoursocialmedia", "social media channels", "socialmediachannels", "account restored", "full control of our accounts", "fullcontrolofouraccounts", "unofficialstatement"),
        keywords=("unauthorised", "unauthorized", "account", "channels", "security", "hacked", "unofficialstatement"), min_score=9,
    ),
    TaxonomyRule(
        ("News & Society", "Technology & Society", "Privacy & Surveillance"),
        strong=("licence plate recognition", "license plate recognition", "surveillance camera", "police camera", "facial recognition", "privacy"),
        keywords=("surveillance", "police", "camera", "privacy", "tracking"), min_score=10,
    ),
    TaxonomyRule(
        ("News & Society", "Current Events", "News & Incidents"),
        strong=("breaking news", "statement", "incident", "news conference", "unauthorised control", "unauthorized control"),
        keywords=("news", "incident", "statement"), min_score=12,
    ),
)

# Broad fallbacks only run if no specific leaf reaches threshold.  This keeps
# every analyzable memory organized without pretending to know a narrow topic.
FALLBACKS: tuple[TaxonomyRule, ...] = (
    TaxonomyRule(("Technology & AI", "General Technology"), strong=(" ai ", "software", "code", "computer", "technology", "app"), keywords=("ai", "software", "code", "computer", "technology", "app"), min_score=5),
    TaxonomyRule(("Business & Work", "General Business"), strong=("business", "client", "sales", "company"), keywords=("business", "client", "sales", "company"), min_score=5),
    TaxonomyRule(("Sports & Motorsports", "General Sports"), strong=("sport", "racing", "race", "match"), keywords=("sports", "racing", "race", "match"), min_score=5),
    TaxonomyRule(("Entertainment & Culture", "General Entertainment"), strong=("entertainment", "movie", "music", "dance"), keywords=("entertainment", "movie", "music", "dance"), min_score=5),
    TaxonomyRule(("Travel & Places", "General Travel"), strong=("travel", "trip", "destination"), keywords=("travel", "trip", "destination"), min_score=5),
    TaxonomyRule(("Food & Drink", "General Food & Drink"), strong=("restaurant", "recipe", "food", "drink"), keywords=("restaurant", "recipe", "food", "drink"), min_score=5),
)

_FIELD_WEIGHTS = {
    "title": 6.0,
    "caption": 4.0,
    "creator": 2.0,
    "tags": 3.0,
    "ocr": 2.3,
    "speech": 1.5,
    "article": 2.0,
    "summary": 1.0,
    "visual": 1.8,
}

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+.#'_-]*")
_NONWORD_RE = re.compile(r"[^a-z0-9+.#'_-]+")


def _norm(value: str | None) -> str:
    return " ".join(_NONWORD_RE.sub(" ", (value or "").lower()).split())


def _contains_phrase(text: str, phrase: str) -> bool:
    p = _norm(phrase)
    if not p:
        return False
    return f" {p} " in f" {text} " or p in text


def _field_map(
    *,
    title: str | None = None,
    caption: str | None = None,
    creator: str | None = None,
    tags: Iterable[str] = (),
    segments: dict[str, Iterable[str]] | None = None,
) -> dict[str, str]:
    out = {
        "title": _norm(title),
        "caption": _norm(caption),
        "creator": _norm(creator),
        "tags": _norm(" ".join(tags)),
    }
    for modality, values in (segments or {}).items():
        out[modality] = _norm(" ".join(values))
    return out


def _rule_score(rule: TaxonomyRule, fields: dict[str, str]) -> float:
    total = 0.0
    for field, text in fields.items():
        if not text:
            continue
        weight = _FIELD_WEIGHTS.get(field, 1.0)
        tokens = set(_WORD_RE.findall(text))
        # Strong phrases deliberately dominate isolated keywords.
        for phrase in rule.strong:
            if _contains_phrase(text, phrase):
                total += weight * 2.0
        for keyword in rule.keywords:
            key = _norm(keyword)
            if " " in key:
                if _contains_phrase(text, key):
                    total += weight * 1.1
            elif key in tokens:
                total += weight * 0.7
    # Small specificity bonus only after evidence exists.
    if total:
        total += max(0, len(rule.path) - 2) * 0.4
    return round(total, 3)


def classify_fields(
    *,
    title: str | None = None,
    caption: str | None = None,
    creator: str | None = None,
    tags: Iterable[str] = (),
    segments: dict[str, Iterable[str]] | None = None,
) -> TaxonomyDecision:
    """Choose a primary deep path plus optional cross-branch secondary paths."""
    fields = _field_map(
        title=title, caption=caption, creator=creator, tags=tags, segments=segments
    )
    has_content = any(v for v in fields.values())
    if not has_content:
        return TaxonomyDecision(("Other", "Uncategorized"), 0.20, score=0.0)

    scored: list[tuple[float, TaxonomyRule]] = []
    for rule in RULES:
        score = _rule_score(rule, fields)
        if score >= rule.min_score:
            scored.append((score, rule))

    if not scored:
        for rule in FALLBACKS:
            score = _rule_score(rule, fields)
            if score >= rule.min_score:
                scored.append((score, rule))

    if not scored:
        return TaxonomyDecision(("Other", "Uncategorized"), 0.35, score=0.0)

    scored.sort(key=lambda x: (x[0], len(x[1].path)), reverse=True)
    best_score, best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    margin = max(0.0, best_score - second_score)
    confidence = min(0.98, 0.58 + min(best_score, 35.0) / 100.0 + min(margin, 20.0) / 80.0)

    secondary: list[tuple[str, ...]] = []
    best_root = best.path[0]
    for score, rule in scored[1:]:
        if rule.path[0] == best_root:
            continue
        if score < max(rule.min_score, best_score * 0.58):
            continue
        secondary.append(rule.path)
        if len(secondary) >= 2:
            break

    return TaxonomyDecision(
        primary_path=best.path,
        confidence=round(confidence, 3),
        secondary_paths=tuple(secondary),
        score=best_score,
    )


def slugify_category(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "category"


def ensure_category_path(
    db: Session, user_id: uuid.UUID, path: tuple[str, ...]
) -> Category:
    parent: Category | None = None
    slugs: list[str] = []
    for depth, name in enumerate(path):
        slugs.append(slugify_category(name))
        path_key = "/".join(slugs)
        node = (
            db.query(Category)
            .filter(Category.user_id == user_id, Category.path == path_key)
            .first()
        )
        if node is None:
            node = Category(
                user_id=user_id,
                parent_id=parent.id if parent else None,
                name=name[:128],
                slug=slugs[-1][:128],
                path=path_key,
                depth=depth,
            )
            db.add(node)
            db.flush()
        parent = node
    assert parent is not None
    return parent


def classify_memory(db: Session, memory: Memory, item: SourceItem | None = None) -> TaxonomyDecision:
    item = item or memory.source_item
    segments: dict[str, list[str]] = defaultdict(list)
    for segment in (
        db.query(MemorySegment)
        .filter(MemorySegment.memory_id == memory.id)
        .order_by(MemorySegment.id)
        .all()
    ):
        segments[segment.modality].append(segment.content)
    tags = [
        row.tag
        for row in db.query(MemoryTag).filter(MemoryTag.memory_id == memory.id).all()
    ]
    return classify_fields(
        title=memory.title,
        caption=item.caption if item else None,
        creator=item.creator_handle if item else None,
        tags=tags,
        segments=segments,
    )


def assign_memory_categories(
    db: Session,
    memory: Memory,
    user_id: uuid.UUID,
    decision: TaxonomyDecision,
    *,
    source: str = "rules-v1",
    preserve_manual: bool = True,
) -> None:
    existing = (
        db.query(MemoryCategory)
        .filter(MemoryCategory.memory_id == memory.id)
        .all()
    )
    if preserve_manual and any(x.is_primary and x.source == "manual" for x in existing):
        return

    primary = ensure_category_path(db, user_id, decision.primary_path)
    desired: dict[uuid.UUID, tuple[bool, float]] = {
        primary.id: (True, decision.confidence)
    }
    for path in decision.secondary_paths:
        leaf = ensure_category_path(db, user_id, path)
        if leaf.id != primary.id:
            desired[leaf.id] = (False, max(0.40, decision.confidence - 0.12))

    existing_by_id = {assoc.category_id: assoc for assoc in existing}
    # Update surviving assignments and remove obsolete ones first. Flushing
    # before inserting a new primary avoids the partial unique-index race and
    # keeps SQLAlchemy's identity map consistent across repeated reclassifies.
    for category_id, assoc in existing_by_id.items():
        spec = desired.get(category_id)
        if spec is None:
            db.delete(assoc)
            continue
        assoc.is_primary, assoc.confidence = spec
        assoc.source = source
    db.flush()

    for category_id, (is_primary, confidence) in desired.items():
        if category_id in existing_by_id:
            continue
        db.add(
            MemoryCategory(
                memory_id=memory.id,
                category_id=category_id,
                is_primary=is_primary,
                confidence=confidence,
                source=source,
            )
        )

    # Compatibility for current Android/API clients: show the useful leaf,
    # while the full path is available from the category relationship/API.
    memory.category = decision.primary_path[-1][:64]
    db.commit()


def categorize_memory(
    db: Session,
    memory: Memory,
    item: SourceItem | None = None,
    *,
    source: str = "rules-v1",
    preserve_manual: bool = True,
) -> TaxonomyDecision:
    item = item or memory.source_item
    decision = classify_memory(db, memory, item)
    user_id = getattr(item, "user_id", None)

    # No evidence means no category. Do not let failed/private/empty legacy
    # saves dominate the user's tree as a giant "Other" branch. A READY item
    # with some evidence but no confident rule still receives
    # Other/Uncategorized (confidence 0.35), which is useful triage.
    no_evidence = decision.score == 0.0 and decision.confidence <= 0.20
    if user_id is None:
        memory.category = None if no_evidence else decision.primary_path[-1][:64]
        return decision
    if no_evidence:
        existing = (
            db.query(MemoryCategory)
            .filter(MemoryCategory.memory_id == memory.id)
            .all()
        )
        if preserve_manual and any(x.is_primary and x.source == "manual" for x in existing):
            return decision
        for assoc in existing:
            db.delete(assoc)
        memory.category = None
        db.commit()
        return decision

    assign_memory_categories(
        db,
        memory,
        user_id,
        decision,
        source=source,
        preserve_manual=preserve_manual,
    )
    return decision


def category_paths_for_memory(db: Session, memory_id: uuid.UUID) -> list[dict]:
    rows = (
        db.query(MemoryCategory, Category)
        .join(Category, Category.id == MemoryCategory.category_id)
        .filter(MemoryCategory.memory_id == memory_id)
        .order_by(MemoryCategory.is_primary.desc(), Category.path)
        .all()
    )
    return [
        {
            "path": node.path,
            "names": tuple(part.name for part in _ancestor_chain(db, node)),
            "is_primary": assoc.is_primary,
            "confidence": assoc.confidence,
            "source": assoc.source,
            "category_id": node.id,
        }
        for assoc, node in rows
    ]


def _ancestor_chain(db: Session, node: Category) -> list[Category]:
    chain = [node]
    current = node
    while current.parent_id is not None:
        current = db.query(Category).filter(Category.id == current.parent_id).one()
        chain.append(current)
    return list(reversed(chain))


def reclassify_user_memories(
    db: Session, user_id: uuid.UUID, *, preserve_manual: bool = True
) -> dict[str, int]:
    memories = (
        db.query(Memory)
        .join(SourceItem, SourceItem.id == Memory.source_item_id)
        .filter(SourceItem.user_id == user_id, Memory.processing_status != "DELETED")
        .all()
    )
    counts: dict[str, int] = defaultdict(int)
    for memory in memories:
        decision = categorize_memory(
            db,
            memory,
            memory.source_item,
            source="rules-v1",
            preserve_manual=preserve_manual,
        )
        if not (decision.score == 0.0 and decision.confidence <= 0.20):
            counts[" > ".join(decision.primary_path)] += 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def build_category_tree(db: Session, user_id: uuid.UUID) -> list[dict]:
    nodes = db.query(Category).filter(Category.user_id == user_id).order_by(Category.path).all()
    direct_counts = dict(
        db.query(MemoryCategory.category_id, func.count())
        .filter(MemoryCategory.is_primary.is_(True))
        .join(Memory, Memory.id == MemoryCategory.memory_id)
        .join(SourceItem, SourceItem.id == Memory.source_item_id)
        .filter(SourceItem.user_id == user_id)
        .group_by(MemoryCategory.category_id)
        .all()
    )
    by_parent: dict[uuid.UUID | None, list[Category]] = defaultdict(list)
    for node in nodes:
        by_parent[node.parent_id].append(node)

    def emit(node: Category) -> dict:
        children = [emit(child) for child in by_parent.get(node.id, [])]
        children = [child for child in children if child["total_count"] > 0]
        direct = int(direct_counts.get(node.id, 0))
        total = direct + sum(child["total_count"] for child in children)
        return {
            "id": node.id,
            "name": node.name,
            "slug": node.slug,
            "path": node.path,
            "depth": node.depth,
            "direct_count": direct,
            "total_count": total,
            "children": children,
        }

    roots = [emit(node) for node in by_parent.get(None, [])]
    # Hide empty historical nodes if a taxonomy version later stops using them.
    return [root for root in roots if root["total_count"] > 0]
