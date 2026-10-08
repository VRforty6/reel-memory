# Reel Memory — Backend (MVP scaffold)

Personal multimodal memory/search system for short-form video. Product one-liner:

> *Reel Memory lets you save a short video once and find it later by describing
> anything you remember seeing, hearing, or reading in it.*

This repo is the **backend scaffold**: capture API, processing pipeline skeleton,
and hybrid search. See the PRD for the full product spec.

## What it is

- **Capture-first API** — `POST /v1/captures {"url"}` validates + canonicalizes an
  Instagram URL, durably stores it, and returns `202` immediately. AI processing
  happens asynchronously in the worker.
- **Source adapters** behind a stable interface (Instagram first; TikTok/YouTube
  later). No credential scraping, no cookie extraction, no private-content
  bypass — ever.
- **Pipeline worker** — advances memories through the PRD §30 state machine with
  classified failures (§31), bounded retries (§32), and guaranteed temp-media
  cleanup (§40).
- **Hybrid search** — PostgreSQL FTS + pgvector cosine similarity merged with
  reciprocal-rank fusion; every result carries evidence (VISUAL/SPEECH/OCR/
  CAPTION/TAG).

## Quickstart

```bash
docker compose up -d            # pgvector/pgvector:pg16 on :5432
cp .env.example .env            # adjust DATABASE_URL if needed
# production-style: apply the migrations in order
psql postgresql://reel_memory:changeme@localhost:5432/reel_memory \
  -f migrations/001_initial.sql
psql postgresql://reel_memory:changeme@localhost:5432/reel_memory \
  -f migrations/002_article_modality.sql
psql postgresql://reel_memory:changeme@localhost:5432/reel_memory \
  -f migrations/003_intent_history.sql
psql postgresql://reel_memory:changeme@localhost:5432/reel_memory \
  -f migrations/004_auth_billing.sql
psql postgresql://reel_memory:changeme@localhost:5432/reel_memory \
  -f migrations/005_visual_search.sql
psql postgresql://reel_memory:changeme@localhost:5432/reel_memory \
  -f migrations/006_thumbnails.sql
# 007 uses CREATE INDEX CONCURRENTLY; do not wrap it in a transaction.
psql postgresql://reel_memory:changeme@localhost:5432/reel_memory \
  -f migrations/007_search_fts_index.sql

python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

uvicorn app.main:app --reload   # API on :8000 (dev init_db creates tables)
python -m app.main worker       # exactly one worker per database; a second exits
pytest                          # pure-logic unit tests
```

## API examples

All `/v1/*` endpoints except `/v1/auth/*` and `/health` require a bearer
token: sign up (or log in), then pass
`-H "Authorization: Bearer <access_token>"`. A 401 means "show the sign-in
screen"; a 402 `quota_exceeded` means "show the Pro paywall".

```bash
# Sign up (201) — also the dev-seed flow: create your account, then use it
curl -X POST localhost:8000/v1/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"a-long-password-here"}'
# -> {"access_token":"...","refresh_token":"...","token_type":"bearer","expires_in":900}

export TOKEN=<access_token from above>
AUTH=(-H "Authorization: Bearer $TOKEN")

# Log in / refresh / log out
curl -X POST localhost:8000/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"a-long-password-here"}'
curl -X POST localhost:8000/v1/auth/refresh \
  -H 'Content-Type: application/json' \
  -d '{"refresh_token":"<refresh_token>"}'   # rotates: old token is revoked
curl -X POST localhost:8000/v1/auth/logout \
  -H 'Content-Type: application/json' \
  -d '{"refresh_token":"<refresh_token>"}'  # 204, idempotent

# Sign in with Google (Android app sends the Google ID token it obtained)
curl -X POST localhost:8000/v1/auth/google \
  -H 'Content-Type: application/json' \
  -d '{"id_token":"<google-id-token>"}'     # create-or-link by verified email

# Who am I: profile, effective tier, quota usage
curl "${AUTH[@]}" localhost:8000/v1/me

# Capture a reel (202, or 200 + duplicate:true for a READY re-share)
curl -X POST localhost:8000/v1/captures "${AUTH[@]}" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.instagram.com/reel/AbC123xYz_-/?utm_source=ig"}'

# Check processing state (polling is fine for MVP)
curl "${AUTH[@]}" localhost:8000/v1/memories/<memory_id>/status

# Search by vague memory
curl "${AUTH[@]}" 'localhost:8000/v1/search?q=red%20motorcycle%20number%2046'

# Memory detail / delete / explicit reprocess
curl "${AUTH[@]}" localhost:8000/v1/memories/<memory_id>
curl -X DELETE "${AUTH[@]}" localhost:8000/v1/memories/<memory_id>
curl -X POST "${AUTH[@]}" localhost:8000/v1/memories/<memory_id>/reprocess

# Upload a video file directly (the "see the reel" path — Instagram URLs
# carry no video bytes; 202, or 200 + duplicate:true for an identical re-upload)
curl -X POST localhost:8000/v1/captures/upload "${AUTH[@]}" -F file=@reel.mp4

# Upload a screenshot / post image (vision + OCR run on it; no audio stage)
curl -X POST localhost:8000/v1/captures/upload "${AUTH[@]}" -F file=@screenshot.png

# Capture a carousel/album as ONE memory (2-30 photos, at most one video;
# 202, or 200 + duplicate:true for an identical re-share). One share = ONE
# capture against the quota, no matter the photo count.
curl -X POST localhost:8000/v1/captures/album "${AUTH[@]}" \
  -F files=@photo1.jpg -F files=@photo2.jpg -F files=@photo3.jpg

# Ingest a web page as a memory (SSRF-safe fetch; 202, or 200 + duplicate:true)
curl -X POST localhost:8000/v1/captures/url "${AUTH[@]}" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/article"}'

# Ask a question about a reel's contents (memory must be READY)
curl -X POST localhost:8000/v1/memories/<memory_id>/ask "${AUTH[@]}" \
  -H 'Content-Type: application/json' \
  -d '{"question":"What recipe is shown?"}'

# Ask with web verification of factual claims
curl -X POST localhost:8000/v1/memories/<memory_id>/ask "${AUTH[@]}" \
  -H 'Content-Type: application/json' \
  -d '{"question":"Is this claim true?","verify":true}'

# Extract concrete to-dos the content recommends (empty list if none — never filler)
curl -X POST localhost:8000/v1/memories/<memory_id>/actions "${AUTH[@]}"

# Decision brief: "should I spend time on this?" — ends with a direct question
curl -X POST localhost:8000/v1/memories/<memory_id>/brief "${AUTH[@]}" \
  -H 'Content-Type: application/json' \
  -d '{"goal":"stay pain-free at my desk job"}'
```

## Website ingestion

`POST /v1/captures/url {"url"}` ingests a web page as a memory
(platform `"web"`). The worker fetches the page and stores the extracted main
article text as an `"article"`-modality segment (migration
`002_article_modality.sql` widens the segment check constraint), then builds
the memory deterministically (page title + lead summary — honest truncation,
nothing invented), embeds it, and lands `READY`.

**SSRF safety** (all in `app/sources/webpage.py`, stdlib only):
- `https` only, no embedded credentials, sane ASCII hostname
- every resolved IP must be public — private / loopback / link-local /
  multicast / reserved / unspecified are refused (blocks `127.0.0.1`,
  `10/8`, `192.168/16`, and the `169.254.169.254` metadata endpoint)
- the TCP connection is pinned to the validated IP (DNS-rebinding safe);
  SNI and `Host` still carry the original hostname; certificates verified
- manual redirect handling (≤ `FETCH_MAX_REDIRECTS` hops, default 5), each hop
  re-validated and re-resolved; loops refused
- byte cap (`MAX_FETCH_MB`, default 2 MiB), connect/read timeout
  (`FETCH_TIMEOUT_S`, default 10 s), HTML-only

**Honest failure classification:** transient network / 5xx / 429 → retryable
`SOURCE_RESOLUTION_FAILED` / `SOURCE_RATE_LIMITED`; 401/403 →
`AuthenticationRequired` (terminal — never circumvented); 404/410 →
`Unavailable`; SSRF refusal / non-HTML / oversize / redirect abuse / no
extractable text → `Unsupported` (permanent `UNSUPPORTED_SOURCE`).

Article extraction is readability-style heuristics (drop nav/script/boilerplate
by tag and class patterns, score containers by paragraph-text density) — good
enough for prose articles, not a full Readability port.

## Intelligence layer: actions + decision brief

`POST /v1/memories/{id}/actions` extracts concrete to-dos the content
**explicitly** recommends: `{actions: [{title, detail, priority: "P0"|"P1"|"P2",
effort: "small"|"medium"|"large", evidence: {modality, timestamp_ms|null,
quote}}]}`. Priority is the model's honest impact × effort judgment (most
content deserves P1/P2, not P0). Every action must carry an exact evidence
quote — uncited actions are dropped, and content with no actionable advice
returns `[]`, never filler.

`POST /v1/memories/{id}/brief {"goal": "..."}` answers "should I spend time
on this?": `{summary, key_claims[], validation, usefulness_assessment,
effort_estimate, open_question, inferred_intent}`. `validation` reuses the ask
verification machinery over `key_claims` (degrades to `"unavailable"` without
a search key). `usefulness_assessment` is an honest assessment against *your
goal* — including "not very". `open_question` always ends with a direct
question (e.g. "Do you want to do this?"; a fallback is appended if the model
forgets) so the client can render a decision card with **Yes / Not now**
buttons. SEC-008: the content supplies the question's *topic*, never its
wording.

**Intent understanding:** you never declare a goal — the brief first
classifies what you're trying to do, model-side (no manual tags), from the
content + your goal: `inferred_intent = {domain, intent, confidence, label,
source}` (e.g. domain `purchase`, intent `decide`, label "deciding whether
to buy this"). The brief is then framed for that intent: a purchase question
gets comparison + a value-for-money verdict; a business idea gets risks +
concrete validation steps; an act/portfolio item gets the prioritized action
list; learning gets what the content teaches well vs. what it skips. The
client shows the label ("Looks like you're deciding whether to buy this —
here's the breakdown"); if it's wrong, re-request the brief with
`{"goal": "...", "intent_override": {"domain": "purchase", "intent":
"compare"}}` — the correction is trusted, the classification step is skipped,
and it's recorded. Recent inferred intents are kept per user (a capped JSON
list on the user record, migration `003_intent_history.sql`) as weak context
for follow-up classifications — no new tables.

## Conversational Q&A (ask a reel)

`POST /v1/memories/{memory_id}/ask` answers a question **only from the reel's
own evidence** (speech transcript, visual frame descriptions, OCR text,
caption). The flow:

1. The memory's modality segments are loaded and passed to the chat model
   (`CHAT_PROVIDER`) inside a grounded prompt.
2. The model must cite every claim with modality + timestamp
   (`[speech @ 12s]`), and must answer "The reel doesn't show or say this."
   when the evidence doesn't contain the answer — never guessing.
3. Response: `{answer, citations: [{modality, timestamp_ms, quote}],
   evidence_coverage: "full"|"partial"|"none"}`.

**Untrusted-content framing (SEC-008):** transcript, OCR, captions and frame
descriptions are treated as hostile data — read, never obeyed. Injected
instructions inside the evidence (e.g. "ignore previous instructions") cannot
steer the answer; only the user's question is trusted input.

**Verify mode** (`{"verify": true}`) additionally cross-checks the answer's
factual claims against the web via the `SearchProvider` (`SEARCH_PROVIDER`,
Tavily-compatible). The response gains a `verification` block:
`{status: "verified"|"unavailable", findings: [{claim, verdict:
"supported"|"contradicted"|"uncertain", sources: [{title, url}]}]}`.
"What the reel says" and "what the web supports" are kept strictly separate —
verification never rewrites the grounded answer. If no search key is
configured, status is `"unavailable"` with a clear message; verification is
never faked.

## Auth, entitlements & selling Pro (commercialization)

Every product endpoint requires `Authorization: Bearer <access_token>`
(`get_current_user` in `app/auth/deps.py`); unauthenticated requests get
401 `{"code": "unauthorized", ...}`. Data is fully per-user — every query
filters by `user_id` (SEC-005).

### Accounts

| Endpoint | Purpose |
|---|---|
| `POST /v1/auth/signup` `{email, password}` | Create account → 201 + token pair |
| `POST /v1/auth/login` `{email, password}` | Token pair (rate-limited per IP + per account) |
| `POST /v1/auth/refresh` `{refresh_token}` | New pair; the presented refresh token is **revoked** (rotation) |
| `POST /v1/auth/logout` `{refresh_token}` | Revoke (204, idempotent) |
| `POST /v1/auth/google` `{id_token}` | Sign in with Google: the ID token is verified server-side (RS256 via Google's JWKS, issuer, audience = `GOOGLE_CLIENT_ID`, expiry); the account is created or linked by verified email |
| `GET /v1/me` | Profile + effective tier + per-bucket quota usage |

Passwords are Argon2id hashes (never plaintext); login failures return the
same "wrong email or password" whether the account exists or not, and an
unknown email is verified against a dummy hash so timing doesn't leak
registration. Access tokens live 15 min (`JWT_ACCESS_TTL_S`), refresh tokens
30 days (`JWT_REFRESH_TTL_S`); only the sha256 of a refresh token is stored.
`JWT_SECRET` is required — auth refuses to run without it.

Login brute-force protection is in-memory per process
(`app/auth/ratelimit.py`: 20 attempts/10 min per IP, 8 per account by
default). Behind multiple API workers an attacker gets workers × limit —
for a Redis/DB-backed limiter later, the `LoginRateLimiter` boundary is the
swap point.

### Freemium quotas

| Bucket | Free | Counts |
|---|---|---|
| `captures` | 20 / month | every `POST /v1/captures`, `/upload`, `/url`, **`/album`** |
| `questions` | 50 / day | `ask`, `actions`, `brief` |
| `verifications` | 10 / day | `ask` with `verify=true`, `brief` (always validates) |

An album share counts as **one** capture — a 20-photo carousel costs the
same quota as a single screenshot. The per-file processing cap
(`MAX_ALBUM_FILES`, default 30) and the total-bytes cap (`MAX_ALBUM_MB`,
default 400) bound the work instead. Album dedup is by the combined
content hash (order-independent), so re-sharing the same photos in a
different order is a duplicate, not a new capture.

Pro multiplies every limit by `PRO_QUOTA_MULTIPLIER` (default 10 — abuse
caps, not infinite). Exhaustion returns **402**:

```json
{"code": "quota_exceeded", "tier": "free", "bucket": "captures",
 "limit": 20, "used": 20, "resets_at": "2026-10-01T00:00:00+00:00"}
```

The Android client turns this into the Pro paywall. Periods are UTC calendar
months/days. Quota rows live in `usage_counters` (migration `004`).

### Selling Pro with Google Play (publisher setup)

`POST /v1/billing/verify {package_name, product_id, purchase_token}` checks
the purchase with the **Google Play Developer API** and grants Pro until the
subscription's `expiryTimeMillis`. The client re-sends the token (e.g. on app
start) — re-verification refreshes the entitlement, which is how renewals,
cancellations and expiry are handled. Without a configured service account
the endpoint returns 503 `billing_not_configured`; Pro is never granted
without a successful Google verification. A pending payment
(`paymentState: 0`) never grants Pro.

Do this once in Play Console / Google Cloud (about 20 minutes):

1. **Google Cloud project**: create (or pick) a project at
   <https://console.cloud.google.com>. Enable the **Google Play Android
   Developer API** (APIs & Services → Library → search it → Enable).
2. **Service account**: IAM & Admin → Service Accounts → Create. Name it
   e.g. `reel-memory-play-verifier`. Create a **JSON key** and download it —
   keep this file private (it's a credential).
3. **Play Console access**: Play Console → Users and permissions → Invite
   new user → paste the service account's email
   (`...@....iam.gserviceaccount.com`). Grant **View app information and
   download bulk reports** at minimum, plus **Manage orders and
   subscriptions** (needed to read purchases). Apply to your app.
4. **Subscription product**: Play Console → your app → Monetize →
   Subscriptions → Create subscription. Note the **Product ID**
   (e.g. `pro_monthly`) — the Android app sends this as `product_id`, and
   `package_name` is your app's application ID.
5. **Backend config**: set `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON` to the JSON
   key's contents (or a path to the file) and restart the API.
6. **Test**: in Play Console → License testing, add your Gmail as a license
   tester; purchases you make are then sandbox purchases (no charge) and
   verify the same way.

Android side (separate client repo): use Play Billing Library to launch the
purchase flow for the subscription, then POST the returned `purchaseToken`
with your `packageName` and the subscription's `product_id` to
`/v1/billing/verify`. Handle 402 `quota_exceeded` anywhere by showing the
paywall.

### Dev seed flow

For throwaway local development only: set `DEV_SEED_LOCAL_USER=true` and the
legacy `local@reel-memory` account is created on boot. Production must leave
it `false` and create real accounts via `POST /v1/auth/signup`.

## Milestone map (PRD §66) — what's scaffolded vs stubbed

| Milestone | Status in this scaffold |
|---|---|
| M0 Preserve prototype | This repo *is* the starting point; no prior repo existed |
| M1 Reliable capture | **Scaffolded**: canonicalization, dedupe, capture API, state machine. The Android share target is a separate client and posts to `POST /v1/captures`; direct video/image uploads go to `POST /v1/captures/upload` (multipart, `video/*` + `image/*`, `MAX_UPLOAD_MB`, deduped by content hash, processed by the normal pipeline via the `upload` source adapter; images skip audio extraction — the image is the frame set); carousel/album shares go to `POST /v1/captures/album` (multipart `files`, 2–30 photos, at most one video, one memory, one quota unit, order-independent dedup; every segment carries `album_index` so citations say "photo N"); web pages go to `POST /v1/captures/url` (platform `web`, SSRF-safe fetch → `article` segment, migration `002_article_modality.sql`) |
| M2 Source acquisition benchmark | **Measured + implemented**: benchmarked 24 real public reels × 3 unauthenticated approaches (`MILESTONE2-BENCHMARK.md`) — Instagram serves a gated shell page to non-logged-in clients, so metadata yield was 0% and media-byte yield was 0% (no 429s observed; p50 latency ~1.5s/approach). `InstagramAdapter.resolve()` now implements the best policy-compliant path: a single no-cookie direct-page GET (≤10s, head-only byte cap) extracting Open Graph metadata → `MetadataOnly` (worker persists it, memory lands `METADATA_ONLY`); gated/timeout/HTTP errors → honestly classified failures (`SOURCE_RESOLUTION_FAILED` retryable, `SOURCE_RATE_LIMITED` on 429, `Unavailable` on 404). Video bytes are not obtainable without authentication — the backend will never log in — so memories stay metadata-only until an authenticated, consent-based path is approved. SEC-001 host allowlist re-enforced in the adapter. 44 unit tests green |
| M3 Deterministic media pipeline | **Implemented (ffmpeg)**: `_extract_audio` decodes to 16 kHz mono WAV, `_extract_frames` samples up to `MEDIA_FRAME_COUNT` (default 12) JPEGs evenly across the video, scaled to `MEDIA_MAX_DIMENSION_PX` (default 1280px). ffprobe-first probing, `MAX_MEDIA_DURATION_S` cap (default 600s — longer videos fail honestly), hard timeouts, no shell, temp outputs deleted on every path; failures stay `MEDIA_DECODE_FAILED`. Single uploads and album videos share the path; silent videos skip transcription instead of crashing. Without ffmpeg installed, video jobs fail with an install hint — success is never faked. See "Media decoding" below |
| M4 Multimodal memory | **Wired**: OpenAI-compatible providers (stdlib urllib client, no new deps) for speech/vision/OCR/embeddings/memory generation, selected by `*_PROVIDER` env vars; `hash` keyless embedding for dev/QA; `deterministic` rule-based memory builder. See "AI provider wiring & cost" below |
| M5 Retrieval engine | **Implemented**: FTS + pgvector + RRF + evidence; needs a real DB + embeddings to run end-to-end |
| M6 Benchmark & tune | Pending (eval dataset format TBD under `tests/`) |
| M7 Product UX | API ready; UI is out of scope for this repo |
| M8 Private beta infra | **Implemented**: email+password auth (Argon2id, JWT access/refresh with rotation), Sign in with Google (server-side ID-token verification), per-user data isolation, freemium quotas (`usage_counters`, 402 `quota_exceeded`), Google Play subscription verification (`POST /v1/billing/verify`, migration `004_auth_billing.sql`). See "Auth, entitlements & selling Pro" above |

## Media decoding (Milestone 3: ffmpeg)

Video uploads (`POST /v1/captures/upload`) and album videos
(`POST /v1/captures/album`) are decoded locally by the worker before the AI
stages run. The implementation lives in `app/pipeline/media.py`:

| Step | What happens |
|---|---|
| Probe | `ffprobe` reads the container first (duration, dimensions, audio/video streams). Garbage, truncated, or unsupported files fail here as `MEDIA_DECODE_FAILED`. |
| Policy | Videos longer than `MAX_MEDIA_DURATION_S` (default 600s) fail honestly — they are **not** silently truncated, so a transcript can never misrepresent a cut-off video. |
| Audio | `ffmpeg` extracts the audio track to **16 kHz mono WAV** (what the speech provider expects). A video with no audio stream returns "no audio" and the worker **skips transcription** instead of producing an empty transcript. |
| Frames | Up to `MEDIA_FRAME_COUNT` (default 12) JPEGs are sampled evenly across the duration and scaled so the width never exceeds `MEDIA_MAX_DIMENSION_PX` (default 1280px) — keeps vision costs sane. The vision/OCR providers subsample these to `VISION_MAX_FRAMES` themselves. |
| Cleanup | Decoded audio/frames live in the worker's per-job temp dir, deleted on every path (success or failure); partial outputs are removed on failure too. |

**Hostile-input posture:** no shell invocation, `-nostdin`, hard timeouts on
every subprocess (`MEDIA_PROBE_TIMEOUT_S`, `MEDIA_DECODE_TIMEOUT_S`),
container metadata is treated as advisory (a `-t` cap is passed to ffmpeg
regardless), and outputs are existence/size-checked — an empty frame list or
missing WAV is a failure, never a silent pass.

### Installing ffmpeg (required for video)

The API and worker start fine without ffmpeg — image uploads, article
ingestion, Q&A, and actions all work — but **video jobs fail** with
`MEDIA_DECODE_FAILED` and an install hint until it is present. Install both
`ffmpeg` and `ffprobe`:

- **Debian/Ubuntu:** `sudo apt-get update && sudo apt-get install -y ffmpeg`
- **macOS:** `brew install ffmpeg`
- **Docker:** add to your image, e.g. `RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*`
  (this repo's `docker-compose.yml` only runs Postgres; the API/worker are
  expected to run on a host or image where you install ffmpeg yourself)
- **Custom paths:** set `FFMPEG_BIN` / `FFPROBE_BIN` if the binaries aren't on `PATH`.

### Config knobs

| Env var | Default | Meaning |
|---|---|---|
| `FFMPEG_BIN` / `FFPROBE_BIN` | `ffmpeg` / `ffprobe` | Binary paths |
| `MEDIA_FRAME_COUNT` | `12` | JPEG frames sampled per video |
| `MEDIA_MAX_DIMENSION_PX` | `1280` | Frames scaled down to this width |
| `MAX_MEDIA_DURATION_S` | `600` | Videos longer than this fail honestly |
| `MEDIA_PROBE_TIMEOUT_S` | `15` | ffprobe hard timeout |
| `MEDIA_DECODE_TIMEOUT_S` | `180` | ffmpeg hard timeout per decode |

**What "video ready" means now:** an uploaded video (or the one video in an
album) flows through the full stage machine — `TRANSCRIBING →
ANALYZING_VISUALS → RUNNING_OCR → GENERATING_MEMORY → INDEXING → READY` —
exactly like before, except the audio/frames are real instead of stubbed.
Corrupt or over-long videos land in `FAILED_PERMANENT` with the
`MEDIA_DECODE_FAILED` code and a human-readable reason; the uploaded bytes
are still deleted per PRD §8.6.

## Visual search (find reels by what you saw)

Text search (transcript, OCR, captions, GPT vision descriptions) cannot find
a moment nobody described in words — e.g. "guy writing with 2 pens on a red
sticky note" when the narration never mentions it. Visual search closes that
gap with **frame embeddings** from a local joint image/text model.

### How it works

| Stage | What happens |
|---|---|
| Ingest (once) | During `ANALYZING_VISUALS`, the worker selects up to 12 uniform frames (the existing ones) plus up to 12 **scene-cut keyframes** (ffmpeg `select='gt(scene,0.10)'`, true `pts_time` timestamps, capped and evenly spaced — total ≤ 24/reel). Each frame is embedded locally with OpenCLIP ViT-B-32 into a shared 512-d image/text space (L2-normalized). Album photos are indexed as single `photo` samples. |
| Search | The query text is embedded with the **same local model** (no video is decoded, no paid vision API is called — the search path imports neither ffmpeg nor the GPT vision provider). A fourth hybrid-search branch ranks stored frames by cosine distance and merges via RRF with the existing FTS / text-vector / tag branches. |
| Evidence | Visual hits are cited honestly: `Visual match near 0:18` (video timestamp) or `Visual match: photo 7` (album). No description of *what* the frame shows is invented — the timestamp/photo number is the evidence. |

The GPT vision descriptions are unchanged: embeddings are for retrieval,
descriptions remain the readable evidence for Q&A.

### Model setup (one-time download)

The model weights (~578 MB) are **not** in the repo. Download once and point
the app at them:

```sh
pip install -e ".[visual]"   # torch (CPU), open_clip_torch, Pillow, numpy
mkdir -p .model-cache && cd .model-cache
curl -L -o openclip-vit-b32.bin \
  https://huggingface.co/laion/CLIP-ViT-B-32-laion2B-s34B-b79K/resolve/main/open_clip_pytorch_model.bin
```

```env
VISUAL_EMBEDDING_PROVIDER=openclip
VISUAL_EMBEDDING_MODEL=ViT-B-32
VISUAL_EMBEDDING_PRETRAINED=laion2b_s34b_b79k   # or absolute path to the .bin
VISUAL_EMBEDDING_DIM=512
VISUAL_MODEL_CACHE_DIR=.model-cache
```

Set `VISUAL_EMBEDDING_PROVIDER=none` to disable visual indexing entirely
(search then uses only the three text branches). If the provider is
misconfigured or the model dim does not match `VISUAL_EMBEDDING_DIM`,
ingestion fails loudly with `VISUAL_INDEX_FAILED` — visual coverage is
never silently faked.

**Why ViT-B-32:** benchmarked against ViT-L/14 on the six representative
queries (`qa/visual_benchmark.py`, real-photo fixtures with 0.6s events
placed between uniform-sample grid points). Both reached Recall@1 100% /
Recall@3 100% — at the ceiling, so the larger model adds nothing on this
task while costing 19× indexing latency and 2.4× RAM. (SigLIP-B/16 weights
were fetched but its HF-hub tokenizer would not load in the bench sandbox;
moot for the decision.) Full numbers in `qa/visual_benchmark_results.md`.

**Cost/latency (measured, 2-CPU bench machine):** ~2.7s indexing per reel
(one-time, local), ~170ms per query warm (local text embed + pgvector
lookup; the 578MB model loads once per process — ~10s cold start on first
use, then a process-level singleton cache reuses it, so repeat searches
never reload). ~14 frames × 512 × 4 bytes ≈ 29 KB storage per reel,
~1.3 GB RAM for the model process. **$0.00 API cost** — no network calls
at ingest or search.

### Backfill for old memories

`GET /v1/memories/{id}/visual-index` reports one of:

| Status | Meaning |
|---|---|
| `VISUAL_INDEX_READY` | Frame embeddings exist — visual search covers this memory. |
| `VISUAL_BACKFILL_AVAILABLE` | Source media is still present; `POST /v1/memories/{id}/visual-backfill` will index it (local only, no paid APIs). |
| `VISUAL_BACKFILL_SOURCE_UNAVAILABLE` | Source is gone (uploads are deleted after processing; Instagram URLs never carried video bytes) — visual search cannot cover this memory. The text branches still work. |

Backfill never claims success from old text/GPT descriptions — it only
succeeds by actually embedding frames.

### Migration

Apply `migrations/005_visual_search.sql` (after 001–004). It creates
`memory_frame_embeddings` (with a `vector(512)` column and HNSW cosine
index) and adds the missing HNSW index on the existing `vector(1536)` text
embeddings. HNSW was chosen over IVFFlat: no training step, no reindex
cadence as the library grows — the right default for a continuously
ingesting personal library of 10³–10⁵ memories.

## What's stubbed and why

- **Media acquisition** — the load-bearing unknown. Per PRD Risk 1, public-reel
  resolution must be *measured* (success rate, failure types, latency,
  rate limits) before the architecture commits to an acquisition approach.
  The stub preserves the URL and keeps the capture durable meanwhile.
- **AI providers** — behind ABCs so models are chosen by benchmark (§36), not
  reputation. Stubs raise `ProviderNotConfiguredError` naming exactly what's
  missing; they never silently no-op.
- **Android client** — separate repo; URL shares post to `POST /v1/captures`,
  video/image files to `POST /v1/captures/upload` (multipart), web pages to
  `POST /v1/captures/url`, questions to `POST /v1/memories/{id}/ask`, to-dos
  to `POST /v1/memories/{id}/actions`, decision briefs to
  `POST /v1/memories/{id}/brief`.

## AI provider wiring & cost (Milestone 4, PRD §35/§36)

Providers are selected by environment (see `.env.example`); keys are never
hardcoded. The worker and the search API both build the bundle through
`app.pipeline.providers.build_providers()`:

| Stage | `*_PROVIDER` | Choices |
|---|---|---|
| Transcription | `SPEECH_PROVIDER` | `none` (stub), `openai` (whisper) |
| Frame analysis | `VISION_PROVIDER` | `none` (stub), `openai` (vision chat) |
| On-screen text | `OCR_PROVIDER` | `none` (stub), `openai` (vision OCR pass) |
| Embeddings | `EMBEDDING_PROVIDER` | `none` (FTS-only search), `openai` (text-embedding-3-small), `hash` (keyless, lexical) |
| Memory card | `MEMORY_GENERATOR_PROVIDER` | `deterministic` (rule-based, default), `openai` (chat JSON), `none` |
| Q&A chat | `CHAT_PROVIDER` | `none` (ask returns 503), `openai` (chat completions) |
| Web verification | `SEARCH_PROVIDER` | `none` (verify mode returns `"unavailable"`), `tavily` (Tavily-compatible search API) |

`openai` means any OpenAI-compatible HTTP endpoint (`OPENAI_BASE_URL`,
default `https://api.openai.com/v1`) and requires `OPENAI_API_KEY`. The client
is stdlib `urllib` — no new dependencies. Provider failures are classified
into the `FailureCode` taxonomy by the worker (`_call` honours a provider's
`failure_code` hint): HTTP 429 → retryable, auth rejection → stage failure
with a "check key" message, timeouts/unreachable → stage failure.

**Estimated cost per 60-second reel** (defaults, ~Sep 2026 list pricing):

| Stage | Model | Estimate |
|---|---|---|
| Transcription | whisper-1 @ $0.006/min | $0.0060 |
| Vision (6 frames) | gpt-4o-mini | $0.0010 |
| OCR pass (6 frames) | gpt-4o-mini | $0.0010 |
| Embeddings (~2k tokens) | text-embedding-3-small @ $0.02/1M | $0.00004 |
| Memory generation (~4k tokens) | gpt-4o-mini | $0.0008 |
| **Total** | | **≈ $0.009 / reel** |

Under the PRD target of **≤ $0.02/Reel**. Longer reels scale mostly with
transcription ($0.006/min) and frame count. The worker logs per-stage
durations (`memory <id> stage X -> Y took Ns`, §48 hook) — correlate with
`processing_jobs` rows to derive real per-reel cost.

## Cost-tracking hook (PRD §48)

The worker logs every stage transition with its duration
(`memory <id> stage X -> Y took Ns`). Correlate these with attempt counts and
`processing_jobs` rows to derive per-stage cost once providers are priced.
A future migration will add a `processing_costs(memory_id, stage, cost_usd)`
table; the hook points are already in `Worker._transition`.

## Security notes (PRD §41)

- **SEC-001**: `canonicalize_url()` enforces an explicit host allowlist; the
  backend never fetches arbitrary URLs (SSRF guard).
- **SEC-004**: no Instagram credentials are collected, stored, or used.
- **SEC-008**: transcript/OCR/caption/video content is **untrusted data**.
  Provider interfaces pass structured evidence, never control strings; do not
  interpolate extracted text into prompts, tool calls, or permission checks.
- **SEC-007**: `DELETE /v1/memories/{id}` hard-deletes the record and all
  derived data (segments, tags, jobs, captures, source item).
- **Auth**: Argon2id password hashes (never plaintext); JWT access (15 min)
  + rotating refresh tokens (only sha256 stored); login throttled per-IP and
  per-account with identical failure messages (no enumeration oracle);
  Google ID tokens verified server-side (RS256 JWKS, issuer, audience,
  expiry — `alg` confusion rejected); `JWT_SECRET` required, never defaulted.
- **Billing**: Pro is granted only after a successful Google Play Developer
  API verification; pending payments never grant Pro; unconfigured billing
  fails closed with `billing_not_configured`.

## Layout

```
app/
  main.py                 FastAPI app + worker entrypoint
  config.py / db.py       settings, engine, local-user bootstrap
  models.py / schemas.py  SQLAlchemy models (PRD §38) / Pydantic schemas (§39)
  api/                    auth.py, me.py, billing.py, captures.py, memories.py, search.py
  auth/                   passwords.py (Argon2id), tokens.py (JWT), google.py
                          (ID-token verification), ratelimit.py, deps.py
  billing/                play.py (Play Developer API subscription verification)
  entitlements.py         tiers, freemium quotas, usage accounting
  capture/                canonicalize.py (FR-CAP-002/003), dedupe.py (§13)
  sources/                base.py (SourceAdapter), instagram.py, registry.py
  pipeline/               state_machine.py (§30), failures.py (§31/§32),
                          providers.py (§35), worker.py, memory_builder.py (§22)
  search/                 hybrid.py (FTS + pgvector + RRF, §26/§27)
migrations/001_initial.sql .. 004_auth_billing.sql
tests/                    pure-logic unit tests (no live DB/Instagram)
```
