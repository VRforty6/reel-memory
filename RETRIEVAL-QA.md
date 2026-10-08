# Retrieval QA — 2026-09-22

**Runner:** `qa/retrieval_qa.py` · **Corpus:** `qa/corpus.py` (24 synthetic reels, 22 query→reel pairs across visual/speech/OCR modalities, incl. paraphrase queries)

## Method
- Loaded corpus into PostgreSQL 16 + pgvector (`reel_memory_qa` DB).
- Hybrid search: PostgreSQL full-text search + pgvector semantic retrieval + reciprocal rank fusion (RRF, K=60), as implemented in `app/search/hybrid.py`.
- Embeddings: `HashingEmbeddingProvider` — deterministic, keyless, **lexical** (token-hash based), not true semantic embeddings. Run is labeled accordingly.
- Each query measured for expected-reel rank and per-query latency.

## Results

| Metric | Measured | PRD target | Pass |
|---|---|---|---|
| Recall@1 | 100.0% | ≥ 80% | ✅ |
| Recall@3 | 100.0% | ≥ 90% | ✅ |
| Latency p50 | 6.0 ms | — | — |
| Latency p95 | 8.3 ms | < 1500 ms | ✅ |

**RESULT: PASS — all PRD targets met.**

Per-query detail (all 22 queries ranked expected reel #1):

| query | expected | rank | ms | note |
|---|---|---|---|---|
| red motorcycle number 46 | reel-01 | 1 | 13.0 | visual + OCR exact |
| how to make carbonara | reel-02 | 1 | 7.0 | speech paraphrase |
| neon tokyo rain night | reel-03 | 1 | 6.6 | visual paraphrase |
| plank workout no rest | reel-04 | 1 | 8.3 | speech |
| phone camera grid composition | reel-05 | 1 | 7.7 | speech paraphrase |
| scoring sourdough before baking | reel-06 | 1 | 8.1 | visual |
| golden retriever puppy snow | reel-07 | 1 | 6.4 | visual |
| cheapest day to book flights | reel-08 | 1 | 6.2 | speech paraphrase |
| travis picking guitar lesson | reel-09 | 1 | 6.1 | speech |
| vitamin c serum routine | reel-10 | 1 | 6.3 | speech |
| fried liver chess trap | reel-11 | 1 | 6.5 | speech |
| rosetta latte art pour | reel-12 | 1 | 5.6 | visual |
| cherry tomatoes balcony pots | reel-13 | 1 | 5.6 | speech + visual |
| python breakpoint debugging | reel-14 | 1 | 5.8 | speech + OCR |
| surfer big turquoise wave | reel-15 | 1 | 5.4 | visual |
| granny square crochet chain three | reel-16 | 1 | 5.5 | speech + visual |
| electric car 420 mile range | reel-17 | 1 | 5.4 | OCR + speech |
| breathing meditation four counts | reel-18 | 1 | 5.3 | speech |
| basketball rooftop trick shot | reel-19 | 1 | 5.8 | visual |
| dovetail joints chisel | reel-24 | 1 | 5.4 | visual + speech |
| 450°F | reel-06 | 1 | 5.2 | OCR exact token |
| la biblioteca | reel-23 | 1 | 5.2 | OCR exact token |

No tuning was required — targets met on first run.

## Caveats
- **Embeddings are lexical, not semantic.** The deterministic hashing provider tests the retrieval *machinery* (FTS + vector + fusion plumbing), not real semantic understanding. Re-run this QA with production embeddings (e.g. `text-embedding-3-small`, wired behind `EMBEDDING_PROVIDER`) once an API key is configured to measure true semantic recall.
- **Synthetic corpus.** 24 hand-built reels with clean, distinctive text. Real reels have noisier transcripts and overlapping vocabulary; expect lower numbers on real data. Re-run against a labeled set of real captures before trusting the targets in production.
- **No retrieval changes were needed**, so no re-measurement loop was exercised. Per PRD, re-run this benchmark before accepting any retrieval or model change.
