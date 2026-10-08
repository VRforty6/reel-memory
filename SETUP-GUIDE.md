# Reel Memory — Setup Guide

Everything you need to go from this zip to a working app on your phone.
Do it in order: **server first, app second, Play Console third.**

---

## What you have

- `reel-memory/` — the full project
  - `app/` — Python backend (API + background worker)
  - `migrations/` — Postgres database setup (incl. auth + billing)
  - `android/` — native Kotlin Android app
  - `README.md` files — deep technical docs per part

Test status when packaged: backend 286/286 tests pass, Android 101/101 tests pass.

---

## STEP 1 — Get a server

You need a machine that's always on (a VPS like Hetzner, DigitalOcean, or
AWS Lightsail is fine; cheapest tier works for personal use).

On that server, install:

```bash
sudo apt-get update && sudo apt-get install -y ffmpeg python3.11 python3-pip postgresql
```

- `ffmpeg` is **required** for video understanding (reels won't process without it).
- Postgres needs the **pgvector** extension for semantic search
  (`sudo apt-get install postgresql-16-pgvector` on Debian/Ubuntu, version
  matching your Postgres).
- **RAM:** the visual-search model (see Step 3A) peaks at ~1.5 GB during
  indexing and keeps ~578 MB of weights resident per process. Use a server
  with **at least 4 GB RAM** (2 GB is the bare minimum — indexing will be
  slower and you risk out-of-memory failures).

---

## STEP 2 — Set up the database

```bash
# Create the database and enable extensions
sudo -u postgres psql -c "CREATE DATABASE reelmemory;"
sudo -u postgres psql -d reelmemory -c "CREATE EXTENSION vector;"

# Run the migrations in order (001 → 006; 006 adds per-memory thumbnails)
for f in migrations/*.sql; do
  sudo -u postgres psql -d reelmemory -f "$f"
done
```

---

## STEP 3 — Configure the backend

```bash
cd reel-memory
cp .env.example .env
```

Edit `.env` and set at minimum:

| Variable | What to put |
|---|---|
| `DATABASE_URL` | `postgresql://...` pointing at your DB |
| `JWT_SECRET` | a long random string (access tokens) |
| AI provider keys | your OpenAI API key (used for transcription, vision, OCR, embeddings) |
| `GOOGLE_CLIENT_ID` | from Google Cloud (Step 6) — needed for Google sign-in |
| Play billing vars | service-account JSON path (Step 7) — needed to verify Pro purchases |

Provider/cost details are in the backend `README.md` ("AI provider wiring & cost").
Rough cost: ~$0.009 per reel processed.

---

## STEP 3A — Visual search (free "search by what you saw")

This is what lets you type *"guy writing with 2 pens on a red sticky note"*
and find the reel even when no transcript, caption, or OCR mentions it.
Each reel's frames are embedded **once** at ingest with a local
open-source model (OpenCLIP ViT-B-32); searches then query the stored
index only — **$0 per search**, no vision API call, no video decoding.

It is **optional**: with the default settings the app works exactly as
before (text search over transcript/OCR/vision descriptions). Enable it
when your server has the RAM (see Step 1).

### 1. Install the visual dependencies (on the server)

```bash
cd reel-memory
pip install -e ".[visual]"
```

This installs PyTorch (CPU wheel — no GPU needed), `open_clip_torch`,
Pillow, and numpy. The torch wheel is large (~200 MB download); the rest
of the app works without these packages.

### 2. Configure (in `.env`)

| Variable | Value | Notes |
|---|---|---|
| `VISUAL_EMBEDDING_PROVIDER` | `openclip` | `none` (default) = visual search off |
| `VISUAL_EMBEDDING_MODEL` | `ViT-B-32` | model architecture |
| `VISUAL_EMBEDDING_PRETRAINED` | `laion2b_s34b_b79k` | weights tag |
| `VISUAL_EMBEDDING_DIM` | `512` | **must** match the model and the `memory_frame_embeddings.embedding vector(512)` column (migration 005) |
| `VISUAL_MODEL_CACHE_DIR` | _(unset)_ | where weights are cached; default is the Hugging Face cache (`~/.cache/huggingface`) |

### 3. Model download & cache location

The first ingest or search after enabling downloads the weights **once**
(~578 MB) from the Hugging Face Hub into `VISUAL_MODEL_CACHE_DIR`
(default `~/.cache/huggingface`). After that it runs fully offline.
The model stays loaded for the process lifetime (~578 MB resident per
process — the API and the worker are separate processes, so each holds
its own copy; that's why Step 1 recommends 4 GB RAM).

To pre-download (so the first real request isn't slow):

```bash
cd reel-memory
VISUAL_EMBEDDING_PROVIDER=openclip \
  .venv/bin/python -c "from app.pipeline.visual import build_visual_provider; print(build_visual_provider().dim)"
```

To use a custom location (e.g. a data disk), set
`VISUAL_MODEL_CACHE_DIR=/var/lib/reel-memory/models` in `.env`.

### 4. What happens if the model can't load (e.g. not enough RAM)

The code **fails loudly, never silently**:

- **During ingest:** the job fails with `VISUAL_INDEX_FAILED` and the
  memory never becomes `READY`. The rest of the pipeline is unaffected —
  nothing is half-indexed or faked.
- **During search:** a misconfigured provider returns HTTP 503; a
  call-time failure (e.g. out-of-memory while embedding the query)
  returns HTTP 502. With `VISUAL_EMBEDDING_PROVIDER=none`, the visual
  branch is simply skipped and search works as text-only.

**Remediation:** add RAM (see Step 1), or set
`VISUAL_EMBEDDING_PROVIDER=none` to run text-only search. Existing
memories keep working either way; visual search just won't be available.
Do **not** change `VISUAL_EMBEDDING_DIM` or the model without
re-migrating — the worker fails fast on a dim mismatch rather than
writing vectors the index can't compare.

### 5. Measured performance (benchmark: `qa/visual_benchmark_results.md`)

- Recall@1 **100%**, Recall@3 **100%** on 6 representative visual queries
- ~2.7 s indexing per reel (one-time), ~170 ms per query (warm),
  ~10 s cold start (first query loads the model), ~1.5 GB RAM peak,
  ~50–100 KB stored per reel.

---

## STEP 4 — Start the backend

Two processes, in two terminals (or use systemd/tmux for permanence):

```bash
pip install -e ".[dev]"
uvicorn app.main:app --host 0.0.0.0 --port 8000   # the API
python -m app.main worker                          # the background worker
```

Sanity check: `curl http://YOUR_SERVER_IP:8000/health` should answer.

> For real use, put the API behind HTTPS (Caddy or nginx + Let's Encrypt).
> The Android app should talk to `https://your-domain`, not raw HTTP.

---

## STEP 5 — Build the Android app

1. Install **Android Studio** on your computer.
2. Open `reel-memory/android/` as a project.
3. In the app's settings / config, set the backend base URL to your server
   (`https://your-domain`).
4. **Two small wiring tasks** (left as clean stubbed seams — the ports are
   defined, you just plug in the official libraries):
   - **Google sign-in**: add `play-services-auth`, implement the account
     picker in `auth/` (interface `GoogleSignInPort`), send the ID token to
     `POST /v1/auth/google`.
   - **Play Billing**: add `billing-ktx`, implement the purchase flow in
     `billing/` (interface `BillingPort`), send the purchase token to
     `POST /v1/billing/verify`.
5. Build → Generate Signed Bundle/APK, and sign it with your keystore.

The Android `README.md` documents every screen, the 401/402 behavior, and
the exact backend contract.

---

## STEP 6 — Google Cloud (for Google sign-in)

1. [Google Cloud Console](https://console.cloud.google.com) → new project.
2. Create an **OAuth 2.0 Client ID** (Android type) with your app's package
   name and SHA-1 of your signing key.
3. Copy the client ID into the backend `.env` as `GOOGLE_CLIENT_ID`.

---

## STEP 7 — Google Play Console (to sell Pro)

1. [Play Console](https://play.google.com/console) → create the app, upload
   your signed AAB.
2. **Monetize → Subscriptions**: create your Pro subscription product.
   Note its product ID — the Android billing code needs it.
3. **Setup → API access**: create a service account, grant it permission to
   check subscriptions, download its JSON key to the server, and point the
   backend config at it. The backend verifies every purchase server-side —
   Pro is only granted after Google confirms payment.
4. Use **license testing** (Play Console → Setup → License testing) to test
   purchases without being charged.

---

## STEP 8 — Test the loop on your phone

1. Install the APK, create an account (email or Google).
2. Open Instagram/TikTok, **Share → Reel Memory** on a reel or carousel.
   - If the app receives the media: it uploads, processes, and appears in
     Memories. Open it → Ask tab → ask questions ("what did he recommend?").
   - If the share only sends a link: save/export the media first, then share
     the saved file to the app. (Links alone can't be fetched — benchmarked.)
3. Try a 15–20 photo carousel: it uploads as **one** album, **one** memory,
   answers cite "photo N".

---

## Where your data lives

- Your reels' understanding (transcripts, frame analysis, OCR, search index)
  lives in **your** Postgres on **your** server. Raw video files are deleted
  after processing.
- Login tokens on the phone live in Android's encrypted Keystore.
- Nothing is stored with any third party except the AI provider calls needed
  to analyze each reel (and Google, only for sign-in/billing if you enable them).

---

## If something breaks

- Backend logs: the `uvicorn` and `worker` terminals show everything;
  failed videos land `FAILED_PERMANENT` with the reason.
- `GET /v1/me` shows your tier and quota usage.
- Quota exhausted → the app shows the paywall (HTTP 402); upgrade via the
  Play subscription you created in Step 7.
- Deep docs: `reel-memory/README.md` (backend), `reel-memory/android/README.md`.
