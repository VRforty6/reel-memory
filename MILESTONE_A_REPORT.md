# Reel Memory — Milestone A Implementation Report

**Date:** 2026-09-23
**Scope:** Milestone A only — UX Stabilization + Search-First Android Redesign (implementation brief §§3–19).
Milestone B (Ask This Reel) was not started. No feature work beyond Milestone A.

## What changed — Android

- Bottom navigation is now **Inbox / Search / Library** (was Queue / Media / Memories). Search is the
  emphasized center tab: headline "What do you remember?", placeholder "Search everything you've saved",
  supporting copy, and 6 example queries.
- Search results show why they matched (Visual match / Transcript / On-screen text / Caption / Tag) with
  timestamps (e.g. "near 00:18"). Raw vector/RRF scores are never exposed. Wired to the existing backend
  `GET /v1/search`; the search backend was not rebuilt.
- Detail screens are now mutually exclusive per content state:
  - READY → thumbnail, title, summary, Ask / Actions / Brief (existing behavior preserved)
  - PROCESSING → honest stage labels, no fake percentages
  - LINK_ONLY (backend METADATA_ONLY) → "Link saved" + "Instagram shared the link but not the actual Reel
    video, so Reel Memory cannot yet see or hear this Reel." + Open original / import help / Remove.
    Never labeled "Synced". No Ask / Actions / Brief tabs.
  - FAILED → human-friendly copy from `failure_code`; retryable vs permanent visually distinct;
    raw backend exception text removed from normal UI (still available in developer view).
- The contradictory "Processing failed" + "Waiting for the backend…" detail state can no longer render.
- Google Sign-In hidden from normal users (still stubbed internally); email/password is the working auth.
- Settings split: normal Settings = Account / Plan / Usage / Privacy / Data / Appearance / Help / Sign out.
  Backend URL, OAuth client ID, Play product ID, and diagnostics moved to hidden Developer Settings
  (long-press the app-version row).
- Library: filters All / Ready / Videos / Images / Links; thumbnail-forward cards (thumbnail, title,
  category, one-line summary, source, saved date, quick Ask); new empty state ("Your memory starts here",
  "Add your first memory" CTA, "How sharing works" with the honest Instagram URL-only limitation).
- Add/import: prominent `+` action (Share a video / Add images / Paste a link); link flow carries honest
  copy that full analysis depends on media accessibility.

## What changed — backend (additive only)

- New migration `006_thumbnails.sql`: adds `thumbnail_jpeg BYTEA` and `media_kind VARCHAR(16)` to
  `memories`. Old rows stay NULL and are never backfilled. Migrations 001→006 apply cleanly.
- Thumbnail persisted at ingest (single JPEG, ≤320px, middle frame, best-effort — never fails the job).
- New `GET /v1/memories/{id}/thumbnail` (JPEG or 404).
- `MemorySummary` list responses now include `platform` and `media_kind` (no N+1).
- Search evidence now carries `start_ms`/`end_ms` so clients can show timestamp citations.
- Untouched: text embeddings (1536), visual embeddings (512), HNSW indexes, RRF ranking, OpenCLIP model,
  GPT vision descriptions, worker architecture, cleanup behavior, quotas, auth backend.

## Tests

- Backend: **298/298 pass** (288 baseline + 10 new in `tests/test_milestone_a_backend.py`), 11 pre-existing warnings.
- Android unit tests: **137/137 pass** (101 baseline + 36 new), run via kotlinc + JUnitCore harness.
- Gradle `assembleDebug`: **could not complete in this sandbox** (all TCP from Java processes is denied, so
  the wrapper cannot download Gradle/dependencies). No APK produced here; requires a networked machine.
- Migrations 001–006 verified on real PostgreSQL 16 + pgvector.

## Acceptance status (§19)

All code- and test-verifiable criteria pass. Milestone A is **not declared fully accepted**: the real-device
criteria (Samsung device test, end-to-end signup, real MP4 share → READY lifecycle, visual queries on real
user data, no-regression confirmation on real MP4 processing) cannot run in this sandbox and must be
validated on a real device after building the APK on a networked machine.

## Remaining stubs / unsupported flows (explicit)

1. Google Sign-In still stubbed internally; now hidden from normal users.
2. Play Billing purchase UI still unwired (out of scope for this milestone).
3. Instagram URL-only shares still yield no media bytes (0% unauthenticated benchmark stands); the LINK_ONLY
   path represents this honestly but does not fix it.
4. Ask This Reel conversational chat is Milestone B — not implemented. Existing Ask / Actions / Brief on
   READY items preserved as-is.
5. Memories created before this build have no thumbnails (placeholder shown); thumbnails are never backfilled.
6. Import picker / FAB actions reuse existing intake code; full Activity-Result wiring needs on-device confirmation.

## Files changed

Backend: `migrations/006_thumbnails.sql` (new), `app/models.py`, `app/pipeline/media.py`,
`app/pipeline/worker.py`, `app/api/memories.py`, `app/api/search.py`, `app/search/hybrid.py`,
`app/schemas.py`, `SETUP-GUIDE.md`, `tests/test_milestone_a_backend.py` (new).
Android: `ui/StateMapping.kt`, `ui/SearchScreens.kt`, `ui/LibraryScreens.kt`, `ui/InboxScreens.kt`,
`ui/DetailScreens.kt`, `ui/SettingsScreens.kt`, `ui/ThumbnailLoader.kt`, `net/SearchApi.kt` (new);
`ui/MainActivity.kt`, `ui/AuthScreens.kt`, `ui/AskScreens.kt` (reworked);
`net/SearchApiTest.kt`, `net/MemoryDetailTest.kt`, `ui/StateMappingTest.kt` (new tests).

---

# A.1 corrective patch (2026-09-23)

Real-device acceptance of Milestone A failed on six verified issues. All six fixed below. Milestone B not started.

## 1. Instagram gated-shell → METADATA_ONLY
`app/sources/instagram.py`: an HTTP-200 gated-shell page with no usable OG metadata now returns
`MetadataOnly(canonical_url, SourceMetadata())` — all fields null, nothing fabricated. The worker already
maps `MetadataOnly` → terminal `METADATA_ONLY`, so this is a single attempt with no retry loop.
Transient network errors, 5xx, and HTTP 429 keep their retryable classification.

## 2. FAILED_RETRYABLE / reprocess concurrency
- `app/pipeline/state_machine.py`: `QUEUED → QUEUED` is now an idempotent no-op (tolerated, never raises
  `IllegalTransitionError`); all other illegal transitions still raise.
- `app/capture/dedupe.py`: `FAILED_RETRYABLE` removed from `REPROCESSABLE_STATUSES` (worker-owned).
- `app/api/memories.py`: `FAILED_RETRYABLE` removed from `_REPROCESSABLE`; `POST /reprocess` returns 409
  for any non-terminal state — manual reprocess is terminal-failure-states only.
- Android (`ui/StateMapping.kt`, `ui/DetailScreens.kt`): `FAILED_RETRYABLE` maps to the Processing UI with
  "Retrying automatically…" copy and no Try Again button; `AskScreens.kt` no longer leaks the raw status code.

## 3. URL-share processing copy
Processing intro is now source-aware (`processingIntroCopy`): link captures show "Your link is saved.
Checking whether media is available." File uploads keep the upload wording. METADATA_ONLY still switches
cleanly to the existing Link Only UI.

## 4. Thumbnails: no repeated 404s; compact link cards
- Backend: `has_thumbnail: bool` added to `MemorySummary` and `MemoryDetail` (`thumbnail_jpeg IS NOT NULL`).
- Android (`ui/ThumbnailLoader.kt`): in-memory `LruCache` negative cache — a 404 is never re-requested
  in-session; requests are skipped entirely when `has_thumbnail=false`; transport errors don't poison the cache.
- Android (`ui/LibraryScreens.kt`, `ui/SearchScreens.kt`): new `CompactLinkCard` (platform icon, title/source,
  state chip) for link-only and non-media items — no huge empty thumbnail placeholders. Thumbnail-forward
  cards kept for real video/image/album media.

## 5. Gradle icon dependency fixed without material-icons-extended
Verified by direct AAR inspection (Compose BOM 2024.09.00 → material-icons-core 1.7.0): all six referenced
icons (Inbox, Movie, VideoLibrary, Image, ChevronRight, RadioButtonUnchecked) are extended-only in 1.7.0
(core ships 49 filled icons; extended ships 2,083). AAR sizes: core **823,516 bytes** vs extended
**35,720,998 bytes** (~43.4× — the dex-heap killer). Replacements, all core-verified or local:
Inbox→`MailOutline`, VideoLibrary→`List`, Movie→`PlayArrow`, ChevronRight→`KeyboardArrowRight`,
RadioButtonUnchecked→local `CircleOutline`, Image→local `ImageFrame` (`ui/AppIcons.kt`, simple primitives).
`build.gradle.kts` keeps only `material-icons-core`. Gradle could not be run in the sandbox (Java TCP blocked);
the user must run `./gradlew clean testDebugUnitTest` and `./gradlew assembleDebug` from `android/` on a
networked machine.

## 6. Hermetic backend tests
New `tests/conftest.py` pins stub providers (`*_PROVIDER=none`, deterministic memory generator, pops
`OPENAI_API_KEY`/`SEARCH_API_KEY`) at import time, before `app.config.settings` is built. Root causes of the
3 failures with a real `.env`: one test assumed ambient stub defaults; two album tests triggered a real
OpenCLIP weights download via `VISUAL_EMBEDDING_PROVIDER=openclip`.

## A.1 test results
- Backend: **311/311 pass** both with provider env vars set (simulated real `.env`: OpenAI + OpenCLIP
  selected) and without — zero manual intervention. (Milestone A baseline was 298.)
- Android (kotlinc + JUnitCore harness): **148/148 pass** (was 137).
- New tests: `test_reprocess.py` (409 on FAILED_RETRYABLE, allowed on FAILED_PERMANENT, race test with 50
  threaded iterations — no IllegalTransitionError, exactly one owner), gated-shell adapter tests (METADATA_ONLY,
  single attempt), QUEUED→QUEUED idempotence tests, `has_thumbnail` contract tests, 12 Android tests
  (FAILED_RETRYABLE routing, copy, negative cache, compact cards, model parsing).

## Remaining unsupported behavior (A.1)
- Gradle build/APK still unverified — must run on the user's networked machine (commands above).
- `POST /reprocess` on DELETED still 500s (pre-existing: state machine forbids DELETED→QUEUED); left untouched.
- Instagram URL-only media still unfetchable (0% benchmark stands); now represented honestly as Link Only.
- Google Sign-In still stubbed/hidden; Play Billing unwired; Ask This Reel chat still Milestone B.

---

# A.2 corrective patch (2026-09-28) — stuck "Processing…" fix

Root cause of the real-device report (video sat in "Processing…" ~1 hour,
nothing ever resolved): two compounding bugs.

1. **Backend: orphaned worker jobs were never recovered.** `poll_once()`
   claims a job (status → RUNNING, commit) and *then* processes it. A
   worker crash / OOM / reboot / redeploy between those two points left
   `processing_jobs.status = RUNNING` with the memory parked mid-pipeline
   forever — the normal poll loop only selects QUEUED jobs. There was no
   startup recovery and no stale-job reaper.
2. **Android: the 90-second poll budget was the only re-check.**
   `UploadWorker` polls `GET /v1/memories/{id}/status` 6× at 15s intervals
   after upload and then stops; nothing ever asked the backend again, so
   the Inbox card froze on "Processing…" even after the backend had moved
   on (or after the server-side recovery below fixed the job).

## What changed — backend

- `app/pipeline/worker.py`
  - `_recover_at_startup()`: on worker start, requeues (or honestly
    closes) every job left in RUNNING with no owner.
  - `_reap_stale_jobs()`: every `worker_reap_interval_s` (default 300s)
    inside `run_forever()`, recovers RUNNING jobs older than
    `worker_stuck_after_s` (default 3600s), excluding jobs claimed by this
    process (`self._claimed`, populated in `poll_once()` with
    try/finally so a crash while processing can't be reaped mid-flight).
  - `recover_orphaned_jobs(db, only_older_than_s, exclude_job_ids)`:
    per-job rules — (1) memory already terminal → close the job as
    DONE/FAILED to match, never reprocess; (2) attempt budget
    (`MAX_ATTEMPTS`, crashes included) spent → FAILED_PERMANENT with code
    `WORKER_LOST_JOB`; (3) otherwise → delete partial segments, visual
    frame embeddings, and tags from the dead attempt, walk the memory back
    to QUEUED through assert_transition-checked transitions only
    (mid-pipeline stages step through FAILED_RETRYABLE, which reads
    honestly in the audit trail), reset the job row to status QUEUED with
    `stage`/`started_at`/`finished_at`/`failure_*` cleared, and consume one
    attempt so a worker that dies before its first attempt increment still
    terminates instead of requeueing forever.
- `app/config.py`: `worker_stuck_after_s` (default 3600.0),
  `worker_reap_interval_s` (default 300.0).

## What changed — Android

- New `sync/StatusCheckWorker.kt`: one-shot WorkManager worker that polls
  `GET /v1/memories/{id}/status` once per PROCESSING row (single uploads
  and albums) and flips the local row to READY/FAILED when the backend
  has moved on; still-processing rows stay PROCESSING. Never uploads.
- New `sync/StatusCheckDecision.kt`: the pure, JVM-testable decision
  function (`decideStatusCheck`) the worker applies.
- `data/VideoUpload.kt`, `data/AlbumUpload.kt`: new `processing()` DAO
  queries (no schema change, no migration).
- `ui/InboxScreens.kt`: "Check status" button on both video and album
  Processing cards; `InboxScreen` auto-enqueues a status check via
  `LaunchedEffect` whenever it opens with PROCESSING rows present.

## A.2 test results
- Backend: **324/324 pass** (311 baseline + 13 new in
  `tests/test_worker_recovery.py`: requeue via legal transitions, partial
  evidence cleared, terminal memory closes job without reprocessing,
  exhausted attempts → FAILED_PERMANENT/WORKER_LOST_JOB, age gate skips
  fresh jobs, claimed-job exclusion, attempt-consumption crash-loop
  termination, original claim behavior untouched).
- Android: 12 new JUnit tests (`sync/StatusCheckDecisionTest.kt`) written
  for READY/FAILED/FAILED_RETRYABLE/mid-pipeline/NotFound/Transient/
  Unauthenticated/QuotaExceeded/missing-memory-id decisions — **not run
  in the sandbox** (no JVM/kotlinc available here); the user must run
  `./gradlew clean testDebugUnitTest` and `./gradlew assembleDebug` from
  `android/` on a networked machine. No APK/device claim is made.

## Remaining unsupported behavior (A.2)
- Gradle build/APK still unverified — must run on the user's networked machine.
- Instagram URL-only shares remain Link Only (0% video-byte benchmark stands).
- `POST /reprocess` on DELETED still 500s (pre-existing).
- Google Sign-In still hidden/stubbed; Play Billing unwired; Milestone B parked
  until the user accepts A/A.1/A.2 on his phone.
