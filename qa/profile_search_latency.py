"""Read-only latency profile for the SQL branches behind GET /v1/search.

Usage:
    PROFILE_DB_URL=postgresql://... python qa/profile_search_latency.py

The script never writes data. It reuses one stored text/visual vector as a
dimension-correct query fixture, runs warmups, and prints JSON with p50/p95.
Provider latency is deliberately excluded; the endpoint emits a separate
structured log line for text embedding, visual embedding, retrieval, and
response serialization.
"""

from __future__ import annotations

import json
import os
import statistics
import time

import psycopg


QUERY = os.getenv("PROFILE_QUERY", "car")
ITERATIONS = int(os.getenv("PROFILE_ITERATIONS", "30"))
LIMIT = int(os.getenv("PROFILE_LIMIT", "50"))

FTS = """
SELECT m.id::text
FROM memory_segments s
JOIN memories m ON m.id = s.memory_id
JOIN source_items si ON si.id = m.source_item_id
WHERE si.user_id = %s::uuid
  AND m.processing_status IN ('READY', 'METADATA_ONLY')
  AND to_tsvector('english', s.content) @@ plainto_tsquery('english', %s)
ORDER BY ts_rank_cd(to_tsvector('english', s.content),
                    plainto_tsquery('english', %s)) DESC
LIMIT %s
"""

VECTOR = """
SELECT m.id::text
FROM memory_segments s
JOIN memories m ON m.id = s.memory_id
JOIN source_items si ON si.id = m.source_item_id
WHERE si.user_id = %s::uuid
  AND m.processing_status IN ('READY', 'METADATA_ONLY')
  AND s.embedding IS NOT NULL
ORDER BY s.embedding <=> %s::vector
LIMIT %s
"""

TAG = """
SELECT m.id::text
FROM memory_tags t
JOIN memories m ON m.id = t.memory_id
JOIN source_items si ON si.id = m.source_item_id
WHERE si.user_id = %s::uuid
  AND m.processing_status IN ('READY', 'METADATA_ONLY')
  AND t.tag ILIKE '%%' || %s || '%%'
LIMIT %s
"""

VISUAL = """
SELECT m.id::text
FROM memory_frame_embeddings f
JOIN memories m ON m.id = f.memory_id
JOIN source_items si ON si.id = m.source_item_id
WHERE si.user_id = %s::uuid
  AND m.processing_status IN ('READY', 'METADATA_ONLY')
  AND f.embedding IS NOT NULL
ORDER BY f.embedding <=> %s::vector
LIMIT %s
"""


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def measure(conn, sql: str, params: tuple) -> dict:
    conn.execute(sql, params).fetchall()  # warm the connection and page cache
    samples: list[float] = []
    rows = 0
    for _ in range(ITERATIONS):
        started = time.perf_counter()
        rows = len(conn.execute(sql, params).fetchall())
        samples.append((time.perf_counter() - started) * 1000)
    return {
        "rows": rows,
        "p50_ms": round(statistics.median(samples), 3),
        "p95_ms": round(percentile(samples, 0.95), 3),
        "max_ms": round(max(samples), 3),
    }


def main() -> None:
    url = os.getenv("PROFILE_DB_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("set PROFILE_DB_URL or DATABASE_URL")
    url = url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        user_id = conn.execute(
            "SELECT user_id FROM source_items ORDER BY created_at LIMIT 1"
        ).fetchone()[0]
        text_vector_row = conn.execute(
            "SELECT embedding::text FROM memory_segments "
            "WHERE embedding IS NOT NULL LIMIT 1"
        ).fetchone()
        visual_vector_row = conn.execute(
            "SELECT embedding::text FROM memory_frame_embeddings "
            "WHERE embedding IS NOT NULL LIMIT 1"
        ).fetchone()

        branches = {
            "fts": (FTS, (user_id, QUERY, QUERY, LIMIT)),
            "tag": (TAG, (user_id, QUERY, LIMIT)),
        }
        if text_vector_row:
            branches["text_vector"] = (
                VECTOR,
                (user_id, text_vector_row[0], LIMIT),
            )
        if visual_vector_row:
            branches["visual_vector"] = (
                VISUAL,
                (user_id, visual_vector_row[0], LIMIT),
            )

        counts = {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "memories",
                "memory_segments",
                "memory_tags",
                "memory_frame_embeddings",
            )
        }
        output = {
            "database": conn.info.dbname,
            "query": QUERY,
            "iterations": ITERATIONS,
            "counts": counts,
            "branches": {
                name: measure(conn, sql, params)
                for name, (sql, params) in branches.items()
            },
        }
        print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
