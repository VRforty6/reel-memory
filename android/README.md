# Reel Memory — Android client

Native Android (Kotlin) share-target client for the Reel Memory backend
(`../` — FastAPI service). Android is the P0 client platform; Instagram is
the first source adapter.

## What it does

**Share → understand → find → act.** Five flows:

1. **Link share (text/plain).** In Instagram, share any reel → the Android
   share sheet lists **Reel Memory**. The link is saved to a local Room
   database immediately and synced to `POST /v1/captures`. **Honest
   limitation:** the backend cannot fetch video bytes from an unauthenticated
   Instagram URL (benchmarked at 0%), so link shares are labeled in the app
   as *"preview only — video not readable"* and can at best become
   metadata-only memories. Plain (non-Instagram) URLs shared as text — a
   website, article, doc — are instead queued for `POST /v1/captures/url`
   so the backend reads the live page and it becomes a full memory.
2. **Media share (video/\* and image/\*).** When the sending app shares an
   actual video file or a screenshot/image, the app copies it to app-private
   storage immediately, then uploads it via multipart to
   `POST /v1/captures/upload` with a progress bar (Queued → Uploading →
   Processing → Ready). This is the path that lets the backend truly *see*
   the reel — and the only one that unlocks Q&A, Actions, and Briefs.
3. **Ask (GPT-style Q&A).** The **Memories** tab lists backend memories; the
   **Media** tab shows an "Ask about this reel" button once an upload is
   ready. The memory detail screen polls the memory status until READY, then
   its **Ask** tab answers questions grounded in reel evidence (VISUAL /
   SPEECH / OCR / CAPTION chips with timestamps). A per-question **"Verify
   with web"** toggle cross-checks claims: the answer renders *"What the
   reel says"* separately from *"Web check"* findings (supported /
   contradicted / uncertain, with source links).
4. **Actions (to-dos).** The memory detail **Actions** tab calls
   `POST /v1/memories/{id}/actions` and renders the derived to-dos with
   priority chips (**P0**/**P1**/**P2**), effort labels, and the evidence
   quote backing each one.
5. **Brief (decision card).** The memory detail **Brief** tab asks for your
   goal, calls `POST /v1/memories/{id}/brief {"goal"}`, and renders a
   decision card: summary, validation verdicts (supported / contradicted /
   uncertain), usefulness + effort assessment, and the closing question with
   **"Yes, do it"** / **"Not now"** buttons. The choice is recorded
   **on-device only** (no backend call yet) and shown as a confirmation.

## Backend contract

The client is coded against these endpoint shapes. The intelligence
endpoints below **exist on the backend** (auth + intelligence milestones
are implemented server-side); the client handles their absence or
misconfiguration with clear messages.

Auth & billing (all under `/v1`):

```
POST /v1/auth/signup   {"email","password"}   -> 201 TokenPair
POST /v1/auth/login    {"email","password"}   -> 200 TokenPair (429 when throttled)
POST /v1/auth/refresh  {"refresh_token"}      -> 200 TokenPair (rotates; old one dies)
POST /v1/auth/logout   {"refresh_token"}      -> 204
POST /v1/auth/google   {"id_token"}           -> 200 TokenPair (503 if not configured)
GET  /v1/me                                   -> 200 MeResponse (Bearer)
POST /v1/billing/verify {"package_name","product_id","purchase_token"}
                                              -> 200 (Bearer; 503 if not configured)
```

Quota exhaustion on any product endpoint returns **402** with a
machine-readable body:

```
{"code":"quota_exceeded","tier":"free","bucket":"questions",
 "limit":50,"used":50,"resets_at":"2026-09-23T00:00:00+00:00"}
```

```
POST /v1/captures/upload                      (exists)
  multipart/form-data:
    file=<video or image bytes>   (required; proper filename + media type)
    original_url=<text>           (optional; the Instagram URL if shared too)
  202 new / 200 duplicate ->
    {"id": uuid, "memory_id": uuid, "status": str, "duplicate": bool}
  413 file too large · 422 missing/invalid file {"detail":{"code","message"}}

POST /v1/captures/url                         (exists)
  {"url": str}                    (a plain website/article URL, not Instagram)
  202 new / 200 duplicate ->
    {"id": uuid, "memory_id": uuid, "status": str, "duplicate": bool}
  422 invalid URL {"detail":{"code","message"}}

POST /v1/captures/album                         (exists)
  multipart/form-data: ONE call per album share —
    files=<photo bytes>           (repeat 2-30 times, share order;
                                   image/* photos, at most one video/*;
                                   proper filename + media type each)
    original_url=<text>           (optional; the Instagram post URL if shared too)
  202 new / 200 duplicate ->
    {"id": uuid, "memory_id": uuid, "status": str, "duplicate": bool,
     "file_count": int, "content_hashes": [str]}
  One share = ONE memory = ONE capture against the free quota, no matter
  the photo count. Dedup is by the combined content hash (order-independent).
  413 album over the total cap · 422 rule violation
  {"detail":{"code":"ALBUM_TOO_MANY_VIDEOS", ...}}

GET /v1/memories?limit=50&offset=0             (exists)
  {"memories":[{"id","title","summary","category","processing_status",
                "created_at"}],"limit":50,"offset":0}

GET /v1/memories/{id}/status                  (exists)
  {"id","processing_status","stage","attempt_count",
   "failure_code","failure_message"}

POST /v1/memories/{id}/ask                    (exists)
  {"question": str, "verify": bool}
  200 ->
    {"answer": str,
     "evidence": [{"modality": "VISUAL"|"SPEECH"|"OCR"|"CAPTION"|"TAG",
                   "start_ms": int|null, "end_ms": int|null,
                   "excerpt": str}],
     "verification": {"status": "supported"|"contradicted"|"uncertain",
                      "findings": [{"claim": str,
                                    "verdict": "supported"|"contradicted"|"uncertain",
                                    "sources": [{"title": str, "url": str}]}]}
      | null}                                  (null when verify=false)
  404 unknown memory · 409 memory not READY yet · 422 empty question

POST /v1/memories/{id}/actions                (exists)
  (no body)
  200 ->
    {"actions": [{"title": str, "detail": str,
                 "priority": "P0"|"P1"|"P2",
                 "effort": "small"|"medium"|"large",
                 "evidence": {"modality": "speech"|"visual"|"ocr"|"caption"|"article",
                              "timestamp_ms": int|null, "quote": str,
                              "album_index": int|null}}]}
  404 unknown memory · 409 memory not READY yet

POST /v1/memories/{id}/brief                  (exists)
  {"goal": str}
  200 ->
    {"summary": str,
     "verdicts": [{"claim": str,
                   "verdict": "supported"|"contradicted"|"uncertain",
                   "note": str|null}],
     "usefulness": "high"|"medium"|"low",
     "effort": "small"|"medium"|"large",
     "closing_question": str}
  404 unknown memory · 409 memory not READY yet · 422 empty goal
```

Client-side upload guards: **200 MB per file** (`UploadApi.MAX_UPLOAD_BYTES`),
matching the backend default, and **400 MB per album**
(`AlbumApi.MAX_ALBUM_BYTES`), matching the backend's `MAX_ALBUM_MB`. Albums
are validated locally before any bytes are copied: 2-30 files, at most one
video, image/video types only.

## Project layout

```
android/
├── settings.gradle.kts            # root project config
├── build.gradle.kts               # root build file
├── gradle/libs.versions.toml       # version catalog (AGP, Kotlin, Room, …)
└── app/
    ├── build.gradle.kts
    ├── proguard-rules.pro
    └── src/main/
        ├── AndroidManifest.xml    # launcher + share-target intent filters
        ├── java/dev/reelmemory/app/
        │   ├── ShareReceiverActivity.kt   # ACTION_SEND/_MULTIPLE handler (text + video + image)
        │   ├── UrlCanonicalizer.kt        # URL rules, mirrors backend
        │   ├── ShareIntake.kt             # pure URL classifier: Instagram vs web (JVM-tested)
        │   ├── data/
        │   │   ├── ShareDatabase.kt       # Room: queues (v4: + album_uploads, album_files)
        │   │   ├── VideoUpload.kt         # video/image upload queue entity/DAO
        │   │   ├── WebCapture.kt          # plain web-URL capture queue entity/DAO
        │   │   ├── BriefDecision.kt       # local decision-brief choices entity/DAO
        │   │   └── SettingsStore.kt       # DataStore: backend base URL
        │   ├── net/CaptureApi.kt          # POST /v1/captures + /v1/captures/url clients
        │   ├── net/UploadApi.kt           # POST /v1/captures/upload (multipart + progress)
        │   ├── net/MemoryApi.kt           # memories list/status/ask/actions/brief client
        │   ├── net/AskModels.kt           # Q&A JSON models + parsing (pure JVM)
        │   ├── net/IntelligenceModels.kt  # actions + decision-brief models/parsing (pure JVM)
        │   ├── auth/
        │   │   ├── AuthApi.kt             # auth/profile/billing HTTP client (implements AuthBackend)
        │   │   ├── AuthModels.kt          # TokenPair, MeProfile, quota + billing models (pure JVM)
        │   │   ├── SessionManager.kt      # session owner: Bearer attach, 401 refresh, 402 paywall events
        │   │   ├── TokenStore.kt          # Keystore AES-256-GCM token storage
        │   │   └── GoogleSignIn.kt        # Google ID-token seam (stub until play-services-auth is wired)
        │   ├── billing/
        │   │   └── Billing.kt             # Play Billing seam + purchase→verify→refresh orchestration
        │   ├── sync/SyncWorker.kt         # WorkManager retry/backoff sync (URL + web shares)
        │   ├── sync/UploadWorker.kt       # WorkManager media upload + readiness poll
        │   └── ui/
        │       ├── MainActivity.kt        # auth gate, Queue / Media / Memories tabs, account settings
        │       ├── AuthScreens.kt         # welcome / email sign-in / sign-up / Google screens
        │       ├── PaywallScreen.kt       # Pro paywall (402 quota_exceeded + manual Upgrade entry)
        │       └── AskScreens.kt          # memory detail: Ask chat + Actions + Brief tabs
        └── res/                   # themes, strings, launcher icon
```

### Key design points

- **URL canonicalization** (`UrlCanonicalizer.kt`) mirrors the backend's
  `app/capture/canonicalize.py` exactly: host allowlist
  (`instagram.com`, `www.instagram.com`, `m.instagram.com`), paths
  `/reel/`, `/reels/`, `/p/`, shortcode regex `^[A-Za-z0-9_-]{2,128}$`,
  canonical form `https://instagram.com/reel/<shortcode>`, query params
  stripped. Failure codes (`INVALID_URL`, `UNSUPPORTED_SOURCE`) match the
  backend's failure taxonomy.
- **Client-side dedupe** on `(platform, platform_item_id)`: re-sharing the
  same reel is detected locally and skipped (the toast reports
  "Already in Reel Memory" instead of queueing a duplicate row); the backend
  dedupes again server-side.
- **Sync payload** is the original shared URL (`{"url": ...}`); the backend
  is the source of truth for canonicalization.
- **Backend auth**: every product endpoint needs a Bearer access token
  (see "Accounts, quotas & Pro" below). Tokens are AES-256-GCM encrypted
  with a key that never leaves the Android Keystore; only ciphertext touches
  disk. A 401 triggers one silent refresh-token rotation and a single retry;
  if that fails the session is cleared and the app routes to sign-in. A 402
  `quota_exhausted` raises the Pro paywall wherever it happens.
- **Cleartext HTTP** is allowed (`usesCleartextTraffic`) so a dev backend on
  `http://…:8000` works; use HTTPS in any real deployment.

## Accounts, quotas & Pro

Every backend call goes through `SessionManager.authorizedHttp`, which
attaches the access token and enforces the session contract:

| Situation | App behavior |
|---|---|
| Signed out | The **auth flow** shows instead of the app: welcome → email sign-in / create-account (backend-validated: email format, 10–128 char password), or Continue with Google. |
| Share while signed out | Captures stay **queued locally**; signing in triggers a sync automatically. |
| 401 (token dead) | One silent refresh + one retry. Refresh tokens rotate server-side, so a failed refresh means the credentials are dead: the local session is cleared and the app routes to sign-in. |
| 402 `quota_exceeded` | The **Pro paywall** appears as an overlay, naming the exhausted bucket, usage vs limit, and reset time. Quota-blocked queue items are parked with an "Over quota" chip and can be retried after upgrading. |
| Sign out | Server-side refresh-token revocation (best-effort), then local state is cleared. |

**Account screen** (Settings gear → top of the screen): email, Free/Pro tier
badge, per-bucket usage bars with reset times, an **Upgrade to Pro** entry
point, and sign-out. Free defaults (backend): 20 captures/month, 50
questions/day, 10 web verifications/day; Pro is 10×.

**Token security.** `EncryptedTokenStore` encrypts the token pair with
AES-256-GCM using a key generated in the Android Keystore
(`AndroidKeyStore`, randomized IV per save); only ciphertext is kept in
SharedPreferences. If the Keystore is unavailable, the store degrades to
"no session" rather than plaintext. Short-lived access JWTs, rotating
refresh tokens, only refresh-token hashes on the server.

### Wiring Google Sign-In (publisher)

The app ships with a `GoogleIdTokenProvider` seam; the default
`StubGoogleSignInProvider` reports honestly that it isn't wired. To enable:

1. **Google Cloud**: create a project at
   <https://console.cloud.google.com> → APIs & Services → Credentials →
   Create **OAuth client ID** (Web application). Copy the client ID
   (`…​.apps.googleusercontent.com`).
2. **Backend**: set `GOOGLE_CLIENT_ID` to that client ID and restart — the
   server verifies the ID token's signature, issuer, audience and expiry.
3. **App**: paste it on the "Continue with Google" screen (signed-out users
   can set it right there — no Settings visit needed) or in Settings →
   Google client ID → Save.
4. Add `com.google.android.gms:play-services-auth` and a production
   `GoogleIdTokenProvider` that requests an ID token for that client ID
   (the README's stub file documents the seam). The app POSTs the ID token
   to `/v1/auth/google` and adopts the returned token pair; the raw Google
   token is never stored.

### Wiring Pro purchases (publisher)

Same seam pattern: `BillingPort` with a default `StubBillingPort` that
reports honestly. The `BillingRepository` already orchestrates the full
flow — product query → Play purchase → `POST /v1/billing/verify`
`{package_name, product_id, purchase_token}` → profile refresh — and
handles pending payments (never grants Pro), cancellations, and errors.

1. **Backend**: follow `../README.md` "Selling Pro with Google Play":
   enable the Google Play Android Developer API, create the service
   account + JSON key, grant it Play Console access, set
   `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON`, restart. Without it the backend
   returns 503 `billing_not_configured` and Pro is never granted.
2. **Play Console**: Monetize → Subscriptions → create the subscription;
   note the **Product ID** (e.g. `pro_monthly`).
3. **App**: Settings → Pro product ID → enter it → Save (default
   `pro_monthly`).
4. Add `com.android.billingclient:billing-ktx` and a production
   `BillingPort` implementation (query the subscription product, launch
   `BillingFlow` for it, forward `PURCHASED` purchases; acknowledge them).
5. **Test**: Play Console → License testing → add your Gmail as a license
   tester; purchases are then sandbox purchases (no charge) and verify
   through the same backend path.

### What's wired vs stubbed (honest status)

- **Wired**: email/password sign-up, login, refresh rotation, logout,
  Google-ID-token *exchange* (`POST /v1/auth/google`), profile/quotas,
  401/402 handling, offline queue + 402 parking, purchase *verification*
  orchestration.
- **Stubs (publisher wires)**: the actual Google account picker
  (`play-services-auth`) and the actual Play purchase UI (`billing-ktx`).
  Both stubs say so in-app instead of failing mysteriously.

## Build

Requirements: JDK 17, Android SDK with platform 34 + build-tools.

```bash
cd android
./gradlew assembleDebug          # APK at app/build/outputs/apk/debug/
./gradlew testDebugUnitTest      # JVM unit tests (URL canonicalization, share
                                 # intake, capture/ask/actions/brief parsing,
                                 # multipart upload building, auth validation +
                                 # parsers, session/401/402 behavior, Google +
                                 # billing orchestration)
./gradlew installDebug           # install on a connected device/emulator
```

**Sandbox verification note:** in this environment Gradle's daemon can't
communicate, so verification runs the standalone toolchain instead:
`aapt2 compile`/`link` for resources + manifest (link succeeds against a
temp manifest copy — the source manifest intentionally omits `package`,
which the Gradle namespace provides), `kotlinc` frontend analysis over
all main sources (clean — the only failure is Compose IR codegen, which
needs the Compose compiler plugin that isn't CLI-usable here), JVM
codegen of the non-UI sources (clean, warnings only), and the JUnit suite
(**101 tests, all passing**). Two sandbox gotchas the suite depends on:
the real `org.json` jar must precede `android.jar` on the classpath
(android.jar's `org.json` is throwing stubs), and the Kotlin stdlib on the
classpath must be deduplicated to a single version.

A Gradle wrapper is included; the first run downloads the Gradle
distribution automatically. CI/dev machines need `ANDROID_HOME`
(or `ANDROID_SDK_ROOT`) pointing at the SDK, or a `local.properties`
with `sdk.dir=…`.

## Pointing at the backend

In the app: **Settings (gear icon) → Backend base URL → Save**.

- **Emulator:** `http://10.0.2.2:8000` (default — reaches your dev machine).
- **Physical phone, same Wi-Fi:** your computer's LAN IP, e.g.
  `http://192.168.1.10:8000`.
- The backend must be reachable: from `reel-memory/`,
  `docker compose up` then `uvicorn app.main:app --host 0.0.0.0 --port 8000`
  (bind `0.0.0.0`, not just localhost, for a physical phone).

Changing the URL re-triggers a sync of anything still queued.

## Share flow (detail)

### Link share (text/plain)

1. `ShareReceiverActivity` (transparent, `noHistory`) receives
   `ACTION_SEND`/`ACTION_SEND_MULTIPLE` with `text/plain`.
2. `UrlExtractor` pulls URLs from the shared text; `ShareIntake.classifyUrls`
   splits them: Instagram URLs are canonicalized into the reel queue;
   any other http(s) URL becomes a `WebCapture` queued for
   `POST /v1/captures/url`. Unsupported hosts / malformed URLs are counted
   as rejected, never crash.
3. New rows are inserted into Room; duplicates are recognized locally.
4. A toast confirms ("Link saved (preview only — video not readable)" /
   "Web link saved — capturing article" / "Already in Reel Memory" /
   "Couldn't save: no link found to capture") and the activity finishes —
   the user lands back in the source app.
5. `SyncWorker.enqueue()` runs the upload with `NetworkType.CONNECTED`,
   exponential backoff, and per-share status updates (reel queue via
   `POST /v1/captures`, web queue via `POST /v1/captures/url`):
   - `202`/`200` → **Synced** (backend memory id recorded when present)
   - `4xx` → parked as failed with the backend's reason (won't retry blindly)
   - network/`5xx` → retried with backoff; the queue shows **Retrying**

### Media share (video/* and image/*)

1. The share receiver detects `video/*` and `image/*` and copies each
   content-URI stream into app-private storage (`filesDir/uploads/`) —
   durable before any network. A client-side **200 MB** guard rejects
   oversized files at share time with a friendly toast.
2. A `VideoUpload` row (PENDING) is inserted; `UploadWorker.enqueue()`
   uploads it via multipart `POST /v1/captures/upload`, writing 0–100%
   progress into the row so the **Media** tab shows a live progress bar.
3. After acceptance the row becomes PROCESSING; the worker polls
   `GET /v1/memories/{id}/status` a bounded number of times. Long AI jobs
   stay PROCESSING and the memory detail screen keeps polling on open.
4. When READY, the Media tab offers **"Ask about this reel"**.

### Album share (ACTION_SEND_MULTIPLE, 2+ photos)

1. The share receiver detects a multi-file `image/*` share and validates it
   against the backend's album rules *before* copying: 2–30 files, at most
   one video, image/video types only, 200 MB per file, 400 MB total
   (`AlbumApi.classifyAlbum`). Violations get an honest toast and nothing
   is queued. Mixed image+video shares that arrive as `*/*` are also routed
   to the album path (manifest has a `*/*` SEND_MULTIPLE filter); each URI's
   real content type is validated during the copy and non-media shares are
   rejected with a clear message, never uploaded.
2. Each stream is copied into app-private storage (`filesDir/uploads/album-<uuid>/`),
   using the *per-URI* content type (a share can mix photos and one video).
   Intake is **all-or-nothing**: if any photo can't be read or breaks a
   limit, the whole album is rejected and the partial copies are deleted —
   a carousel is only useful with every photo. Sizes are enforced *during*
   the copy (bounded stream: 200 MB per file, 400 MB cumulative), so a
   huge file can't fill storage before we notice.
3. ONE `AlbumUpload` row (PENDING) plus one `AlbumFile` child row per photo
   (share order) is inserted in a single Room transaction
   (`insertAlbumWithFiles`); `UploadWorker` uploads the whole album in ONE
   multipart `POST /v1/captures/album` call and writes 0–100% *batch*
   progress into the row. The backend reads every photo (vision + OCR) and
   tags each segment with `album_index`.
4. One share = **one memory = one capture against the free quota**, no
   matter the photo count. The Media tab shows album cards with a
   **"N photos"** badge, and "Ask about this album" when READY.
5. Evidence citations from album memories carry `album_index`; the Ask
   screen renders a **"Photo N"** badge on each evidence chip so you can
   see exactly which photo the answer is grounded in. Action items carry
   the same nested evidence — the Actions tab shows the **"Photo N"**
   badge on action cards too.

### Ask flow

1. **Memories** tab lists `GET /v1/memories` (title, status chip, summary).
2. Tapping a memory opens the memory detail screen (tabs: **Ask** /
   **Actions** / **Brief**), which polls
   `GET /v1/memories/{id}/status` every 3s until READY (or terminal failure).
3. The user types a question and optionally ticks **Verify with web**, then
   sends → `POST /v1/memories/{id}/ask {"question","verify"}`.
4. The answer renders in two visually separate sections:
   - **What the reel says** — the grounded answer plus evidence chips
     (modality + timestamp + excerpt).
   - **Web check** — verification status badge (supported / contradicted /
     uncertain) and per-claim findings with source titles + URLs.
5. Backend errors surface as inline chat messages (not crashes): 404 →
   "no longer on the backend, or Q&A isn't available on this backend yet";
   409 → "still processing"; network failures → retryable message.

### Actions flow

1. The **Actions** tab auto-loads once the memory is READY →
   `POST /v1/memories/{id}/actions`.
2. Each to-do renders as a card: priority chip (**P0** red / **P1**
   amber / **P2** neutral, sorted P0-first), effort label, title, detail,
   and the evidence quote that backs it.
3. 404 explains the endpoint isn't on this backend yet; 409 shows
   "still processing" with a Retry button.

### Brief flow

1. The **Brief** tab asks for the user's goal → `POST
   /v1/memories/{id}/brief {"goal"}`.
2. The decision card renders: summary, validation verdicts (reusing the
   supported / contradicted / uncertain badges, with per-claim notes),
   usefulness + effort assessment chips, and the closing question.
3. **"Yes, do it"** / **"Not now"** records the choice in the local
   `brief_decisions` table only — no backend call — and shows a
   confirmation banner ("Noted — you'll do it" / "Noted — not now").
4. "Ask about a different goal" regenerates with a new goal.

## What's left for you (shipping is yours)

- **Signing & Play publishing**: generate a release keystore, add signing
  config, build `assembleRelease`, and publish — per your call, that's your part.
- **Backend reachability in production**: the app points at whatever URL you
  configure; hosting the backend with TLS is outside this repo.
- **iOS**: explicitly later per the PRD.
- Possible follow-ups (not in v1): in-app search UI,
  per-share retry/delete actions, additional SourceAdapters (TikTok, Shorts).
