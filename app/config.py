"""Application configuration (pydantic-settings). PRD §34."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+psycopg://reel_memory:changeme@localhost:5432/reel_memory"
    )
    embedding_dim: int = 1536
    embedding_provider: str = "none"  # "none" | "openai" | "hash"
    # Milestone 4 — AI provider selection. "none" (default) keeps the loud
    # Unconfigured* stubs so missing config fails fast, never silently.
    # "openai" needs OPENAI_API_KEY (+ optional OPENAI_BASE_URL for any
    # OpenAI-compatible endpoint). "hash" = deterministic keyless embedding
    # for dev/QA (lexical, not semantic). "deterministic" = the rule-based
    # memory builder (no AI spend).
    speech_provider: str = "none"  # "none" | "openai" | "faster-whisper"
    vision_provider: str = "none"  # "none" | "openai"; none = skip semantic vision stage
    ocr_provider: str = "none"  # "none" | "openai" | "rapidocr"
    # Local/offline providers. Start CPU-first for reliability on Windows;
    # switch LOCAL_WHISPER_DEVICE=cuda after a successful CUDA benchmark.
    local_whisper_model: str = "base"
    local_whisper_device: str = "cpu"
    local_whisper_compute_type: str = "int8"
    local_whisper_beam_size: int = 1
    local_whisper_cpu_threads: int = 8
    local_model_cache_dir: str | None = None
    # OCR is CPU-local. Bound both frame count and resolution so a short Reel
    # cannot monopolize the single worker for minutes.
    local_ocr_max_frames: int = 6
    local_ocr_max_dimension_px: int = 640
    memory_generator_provider: str = "deterministic"  # "deterministic" | "openai" | "none"
    # Conversational Q&A (ask): single-turn chat over a memory's evidence.
    chat_provider: str = "none"  # "none" | "openai"
    # Verification mode (ask with verify=true): web cross-check of claims.
    search_provider: str = "none"  # "none" | "tavily"
    search_api_key: str | None = None
    tavily_api_url: str = "https://api.tavily.com"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_transcription_model: str = "whisper-1"
    openai_vision_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_chat_model: str = "gpt-4o-mini"
    provider_timeout_s: float = 120.0
    vision_max_frames: int = 6
    worker_poll_interval_s: float = 5.0
    # Orphaned-job recovery (A.2): a dead worker (crash, OOM, reboot, deploy)
    # leaves processing_jobs rows in RUNNING with the memory stuck forever.
    # A PostgreSQL advisory lock enforces one live worker per database. Only
    # that lock owner may recover RUNNING rows, so startup recovery cannot
    # steal work from a healthy process. The periodic age gate is defense in
    # depth for legacy/orphan rows; the in-process claimed set is also skipped.
    worker_stuck_after_s: float = 3600.0
    worker_reap_interval_s: float = 300.0
    max_attempts: int = 3  # PRD §32: bounded retries, 3 attempts
    temp_dir: str = "/tmp/reel-memory"
    # Direct video uploads (the "see the reel" path): stored under temp_dir,
    # never served over HTTP; the worker deletes them after processing.
    upload_dir: str | None = None  # None -> <temp_dir>/uploads
    max_upload_mb: int = 200  # per-file upload cap for POST /v1/captures/upload
    # --- Media decoding (Milestone 3: ffmpeg) ----------------------------------
    # Video uploads and album videos are decoded locally: audio -> 16 kHz mono
    # WAV for transcription, frames -> JPEGs for vision/OCR. ffmpeg + ffprobe
    # must be installed and on PATH (or pointed at via FFMPEG_BIN/FFPROBE_BIN);
    # when absent, video jobs fail honestly with MEDIA_DECODE_FAILED (the
    # error names the install step) — success is never faked.
    ffmpeg_bin: str = "ffmpeg"  # FFMPEG_BIN
    ffprobe_bin: str = "ffprobe"  # FFPROBE_BIN
    media_frame_count: int = 12  # MEDIA_FRAME_COUNT: JPEGs sampled per video
    media_max_dimension_px: int = 1280  # MEDIA_MAX_DIMENSION_PX: frames scaled to this width
    max_media_duration_s: float = 600.0  # MAX_MEDIA_DURATION_S: longer videos fail honestly
    media_probe_timeout_s: float = 15.0  # MEDIA_PROBE_TIMEOUT_S
    media_decode_timeout_s: float = 180.0  # MEDIA_DECODE_TIMEOUT_S
    # Album/carousel capture (POST /v1/captures/album): one share of 2-30
    # photos (at most one video) becomes ONE memory. An album counts as ONE
    # capture against the free-tier quota; the caps below bound processing.
    min_album_files: int = 2
    max_album_files: int = 30  # also the per-file processing cap
    max_album_mb: int = 400  # total-bytes cap across all files in one album
    # Website ingestion (POST /v1/captures/url, platform "web"): SSRF-safe
    # fetch — https only, public IPs only, redirect limit, byte cap, timeout.
    max_fetch_mb: int = 2  # byte cap for a fetched page (~2 MiB)
    fetch_timeout_s: float = 10.0
    fetch_max_redirects: int = 5
    fetch_user_agent: str = "ReelMemory/1.0 (+website-ingest)"

    # Instagram source acquisition. ``direct`` keeps the anonymous metadata-only
    # path; ``apify`` resolves public Reel video bytes through Apify and stores
    # them only in the worker's temporary directory.
    instagram_acquisition_provider: str = "direct"  # direct | apify
    apify_api_token: str | None = None
    apify_actor_id: str = "apify~instagram-reel-scraper"
    instagram_post_actor_id: str = "apify~instagram-post-scraper"
    youtube_actor_id: str = "streamers~youtube-scraper"
    youtube_subtitles_language: str = "en"
    youtube_max_subtitle_chars: int = 250000
    apify_timeout_s: float = 120.0
    apify_max_media_mb: int = 200
    local_user_email: str = "local@reel-memory"  # single-user local mode (SEC-005)
    # Set true only for throwaway local dev: seeds local_user_email on boot.
    # Production must leave this false and create users via /v1/auth/signup.
    dev_seed_local_user: bool = False
    log_level: str = "INFO"
    processing_version: str = "v0.1.0"

    # --- Visual frame indexing (visual search) --------------------------------
    # One-time, at ingest: representative frames (uniform samples + bounded
    # scene-cut supplementation) are embedded with a local joint image/text
    # model (no API cost) and stored in memory_frame_embeddings. Search
    # embeds the text query with the same model and matches stored frames —
    # no video decoding and no vision API call at search time.
    # "none" (default) skips visual indexing; "openclip" uses OpenCLIP
    # (torch + open_clip_torch; install with `pip install -e ".[visual]"`).
    visual_embedding_provider: str = "none"  # "none" | "openclip"
    visual_embedding_model: str = "ViT-B-32"  # open_clip model architecture
    visual_embedding_pretrained: str = "laion2b_s34b_b79k"  # weights tag
    visual_embedding_dim: int = 512  # MUST match the model AND the
    # memory_frame_embeddings.embedding vector(N) column (see migrations).
    # Changing the model to one with a different dim requires a migration
    # and a full visual re-index — the worker fails fast on mismatch.
    visual_max_extra_frames: int = 12  # scene-cut supplementation cap;
    # total indexed frames per video <= media_frame_count + this (<= ~24).
    visual_scene_threshold: float = 0.10  # ffmpeg select filter threshold
    visual_scene_max_cuts: int = 120  # upper bound on detected cuts before
    # even-spacing selection (the null-pass decode is cheap at 320p).
    visual_model_cache_dir: str | None = None  # None -> HF default cache
    visual_embed_batch_size: int = 16

    # --- auth (Milestone 5: commercialization) --------------------------------
    # JWT_SECRET must be set in production (a long random value). If unset,
    # auth endpoints fail fast with a clear error instead of signing with a
    # weak default.
    jwt_secret: str | None = None
    jwt_access_ttl_s: int = 15 * 60  # 15 minutes
    jwt_refresh_ttl_s: int = 30 * 24 * 3600  # 30 days
    # Google Sign-In: the OAuth client ID of the app (Android client ID from
    # Google Cloud Console). Unset -> /v1/auth/google returns 503.
    google_client_id: str | None = None
    # Login brute-force protection (in-memory, per process — see README for
    # the multi-worker caveat).
    login_max_attempts_ip: int = 20  # per window, per client IP
    login_max_attempts_account: int = 8  # per window, per email
    login_window_s: int = 600  # 10 minutes

    # --- freemium entitlements -------------------------------------------------
    free_captures_monthly: int = 20
    free_questions_daily: int = 50  # ask + actions + brief
    free_verifications_daily: int = 10  # verify=true + brief validation
    pro_quota_multiplier: int = 10  # pro abuse caps = free limit x this

    # --- Google Play Billing ---------------------------------------------------
    # Path to the Play Console service-account JSON key, or the JSON itself.
    # Unset -> /v1/billing/verify returns 503 billing_not_configured.
    google_play_service_account_json: str | None = None

    # --- Android self-update (development / self-hosted distribution) ---------
    # The APK stays server-side. Android checks metadata, downloads the APK,
    # verifies its sha256, then hands it to the OS package installer. Existing
    # installs can only be replaced by an APK signed with the same certificate.
    update_apk_path: str | None = None
    update_version_code: int = 0
    update_version_name: str = ""
    update_release_notes: str = ""

    def resolve_upload_dir(self) -> str:
        """Upload storage dir; defaults under temp_dir, never served over HTTP."""
        import os

        return self.upload_dir or os.path.join(self.temp_dir, "uploads")


settings = Settings()
