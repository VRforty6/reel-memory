"""Retrieval QA runner — measures Recall@1 / Recall@3 and latency of the
hybrid search (FTS + pgvector + RRF) against the synthetic benchmark corpus
in qa/corpus.py.

Usage:
    QA_DATABASE_URL=postgresql+psycopg://reel_memory:changeme@localhost:5432/reel_memory_qa \\
        .venv/bin/python qa/retrieval_qa.py [--rrf-k 60] [--per-branch 50]

The script creates the QA database if missing, loads the corpus with
deterministic hashing embeddings (keyless; lexical, not semantic — any run is
labeled accordingly), runs every query, and prints per-query ranks plus
aggregate Recall@1 / Recall@3 / p50 / p95 latency. Exit code 0 only if the
PRD targets are met (Recall@1 >= 80%, Recall@3 >= 90%, p95 < 1.5s).
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.search.hybrid as hybrid_mod
from app.models import Base, Memory, MemorySegment, MemoryTag, SourceItem, User
from app.pipeline.providers import HashingEmbeddingProvider
from qa.corpus import QUERIES, REELS

TARGET_R1 = 0.80
TARGET_R3 = 0.90
TARGET_P95_S = 1.5


def _qa_url() -> str:
    return os.environ.get(
        "QA_DATABASE_URL",
        "postgresql+psycopg://reel_memory:changeme@localhost:5432/reel_memory_qa",
    )


def _ensure_database(url: str) -> None:
    from sqlalchemy import create_engine, text

    # Connect to the maintenance DB to create the QA database if needed.
    maint = url.rsplit("/", 1)[0] + "/postgres"
    dbname = url.rsplit("/", 1)[1]
    eng = create_engine(maint, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": dbname}
        ).first()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{dbname}"'))
            print(f"created database {dbname}")
    eng.dispose()


def load_corpus(url: str) -> uuid.UUID:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    _ensure_database(url)
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.drop_all(bind=eng)
    Base.metadata.create_all(bind=eng)

    embedder = HashingEmbeddingProvider()
    with Session(eng) as s:
        user = User(email="qa@reel-memory")
        s.add(user)
        s.flush()

        all_texts: list[str] = []
        seg_refs: list[tuple[uuid.UUID, str, int | None, int | None]] = []
        for reel in REELS:
            item = SourceItem(
                user_id=user.id,
                platform="instagram",
                platform_item_id=reel["id"],
                canonical_url=f"https://instagram.com/reel/{reel['id']}",
                original_url=f"https://instagram.com/reel/{reel['id']}",
                creator_handle=f"creator_{reel['id']}",
                caption=reel.get("caption"),
                source_status="READY",
            )
            s.add(item)
            s.flush()
            summary_text = next(
                (c for m, c in reel["segments"] if m == "summary"), ""
            )
            memory = Memory(
                source_item_id=item.id,
                title=reel["title"],
                summary=summary_text,
                category=reel["category"],
                processing_status="READY",
            )
            s.add(memory)
            s.flush()
            for modality, content in reel["segments"]:
                seg = MemorySegment(
                    memory_id=memory.id,
                    modality=modality,
                    content=content,
                )
                s.add(seg)
                s.flush()
                seg_refs.append(seg.id)
                all_texts.append(content)
            for tag in reel["tags"]:
                s.add(MemoryTag(memory_id=memory.id, tag=tag))
        vectors = embedder.embed(all_texts)
        for seg_id, vec in zip(seg_refs, vectors):
            seg = s.get(MemorySegment, seg_id)
            seg.embedding = vec
        s.commit()
        user_id = user.id
    eng.dispose()
    return user_id


def run_eval(url: str, user_id: uuid.UUID, rrf_k: int, per_branch: int):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    hybrid_mod.RRF_K = rrf_k
    eng = create_engine(url)
    embedder = HashingEmbeddingProvider()
    rows = []
    with Session(eng) as s:
        # map expected reel platform_item_id -> memory_id
        mem_by_item = {
            item.platform_item_id: item.memory.id
            for item in s.query(SourceItem).all()
        }
        for query, expected_item, note in QUERIES:
            qvec = embedder.embed([query])[0]
            t0 = time.perf_counter()
            hits = hybrid_mod.hybrid_search(
                s, user_id, query, qvec, limit=10, per_branch=per_branch
            )
            dt = time.perf_counter() - t0
            expected_mid = mem_by_item[expected_item]
            rank = next(
                (i + 1 for i, h in enumerate(hits) if h.memory_id == expected_mid),
                None,
            )
            rows.append(
                {
                    "query": query,
                    "expected": expected_item,
                    "note": note,
                    "rank": rank,
                    "latency_ms": dt * 1000,
                    "top1": (
                        s.get(Memory, hits[0].memory_id).source_item.platform_item_id
                        if hits
                        else None
                    ),
                }
            )
    eng.dispose()
    return rows


def summarize(rows) -> dict:
    n = len(rows)
    r1 = sum(1 for r in rows if r["rank"] == 1) / n
    r3 = sum(1 for r in rows if r["rank"] is not None and r["rank"] <= 3) / n
    lats = sorted(r["latency_ms"] for r in rows)
    p50 = statistics.median(lats)
    p95 = lats[min(len(lats) - 1, int(len(lats) * 0.95))]
    return {"n": n, "recall@1": r1, "recall@3": r3, "p50_ms": p50, "p95_ms": p95}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rrf-k", type=int, default=60)
    ap.add_argument("--per-branch", type=int, default=50)
    ap.add_argument("--no-reload", action="store_true",
                    help="skip corpus reload (DB already loaded)")
    args = ap.parse_args()

    url = _qa_url()
    user_id = load_corpus(url) if not args.no_reload else None
    if user_id is None:
        # resolve the QA user id from the existing DB
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        eng = create_engine(url)
        with Session(eng) as s:
            user_id = s.query(User).filter(User.email == "qa@reel-memory").one().id
        eng.dispose()

    rows = run_eval(url, user_id, args.rrf_k, args.per_branch)
    print(f"\n{'query':38s} {'expected':10s} {'rank':>4s} {'ms':>7s}  note")
    for r in rows:
        rank = str(r["rank"]) if r["rank"] else "MISS"
        print(f"{r['query'][:38]:38s} {r['expected']:10s} {rank:>4s} {r['latency_ms']:7.1f}  {r['note']}")
        if r["rank"] not in (1,) and r["rank"] is not None and r["rank"] > 1:
            pass

    s = summarize(rows)
    print(
        f"\ncorpus={s['n']} queries  RRF_K={args.rrf_k} per_branch={args.per_branch} "
        f"embeddings=hashing(deterministic, lexical)"
    )
    print(f"Recall@1: {s['recall@1']:.1%}  (target >= {TARGET_R1:.0%})")
    print(f"Recall@3: {s['recall@3']:.1%}  (target >= {TARGET_R3:.0%})")
    print(f"latency:  p50 {s['p50_ms']:.1f}ms  p95 {s['p95_ms']:.1f}ms  (target p95 < {TARGET_P95_S*1000:.0f}ms)")
    misses = [r for r in rows if r["rank"] is None or r["rank"] > 3]
    if misses:
        print("\nmisses (rank > 3 or not found):")
        for r in misses:
            print(f"  - {r['query']!r} -> expected {r['expected']} (got rank {r['rank']}, top1={r['top1']})")

    ok = (
        s["recall@1"] >= TARGET_R1
        and s["recall@3"] >= TARGET_R3
        and s["p95_ms"] / 1000 < TARGET_P95_S
    )
    print("\nRESULT:", "PASS — all PRD targets met" if ok else "FAIL — targets missed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
