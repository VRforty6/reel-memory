package dev.reelmemory.app.ui

/**
 * Pure status → UI-state mapping for Milestone A. Dependency-free (no
 * Compose, no Android imports) so every rule here is JVM-unit-testable.
 *
 * The backend contract this is coded against:
 * - processing_status ∈ {READY, METADATA_ONLY, FAILED_*, SOURCE_UNAVAILABLE, …stages…}
 * - stage ∈ {CAPTURED, QUEUED, RESOLVING_SOURCE, MEDIA_READY, TRANSCRIBING,
 *   ANALYZING_VISUALS, RUNNING_OCR, GENERATING_MEMORY, INDEXING, READY}
 * - search evidence type ∈ {VISUAL, SPEECH, OCR, CAPTION, TAG}
 * - memory list media_kind ∈ {video, image, album, article, null}
 */

/** Which mutually-exclusive detail screen a memory opens. */
enum class DetailRoute {
    /** Ask / Actions / Brief tabs (the only route that shows them). */
    READY,
    /** Honest stage list; no percentages, no Ask. */
    PROCESSING,
    /** "Link saved" explainer; never labeled synced. */
    METADATA_ONLY,
    /** Human reason + retry/remove. */
    FAILED,
}

/**
 * Maps a backend `processing_status` to its detail route. Anything
 * unrecognized is treated as still processing (never as failed, never as
 * ready) so new backend stages degrade honestly instead of mislabeling.
 *
 * FAILED_RETRYABLE is the backend's internal automatic retry: it renders
 * the PROCESSING UI (with "Retrying automatically…" copy) and never a
 * manual Try Again button, because the retry is already happening.
 */
fun detailRouteFor(processingStatus: String): DetailRoute = when {
    processingStatus == "READY" -> DetailRoute.READY
    processingStatus == "METADATA_ONLY" -> DetailRoute.METADATA_ONLY
    processingStatus == "FAILED_RETRYABLE" -> DetailRoute.PROCESSING
    processingStatus.startsWith("FAILED") || processingStatus == "SOURCE_UNAVAILABLE" ->
        DetailRoute.FAILED
    else -> DetailRoute.PROCESSING
}

/** True when the backend is retrying on its own: no manual retry offered. */
fun isAutoRetrying(processingStatus: String?): Boolean =
    processingStatus == "FAILED_RETRYABLE"

/**
 * Honest intro copy for the processing screen. Source-aware: a link
 * capture that is still resolving never claims an upload is safe.
 */
fun processingIntroCopy(processingStatus: String?, mediaKind: String?): String {
    if (isAutoRetrying(processingStatus)) {
        return "Retrying automatically… Reel Memory is giving this another " +
            "go on its own. You can leave; it'll be here when it's done."
    }
    return if (mediaKind == "article") {
        "Your link is saved. Checking whether media is available."
    } else {
        "Your upload is safe. Reel Memory is working through it — " +
            "you can leave; it'll be here when it's done."
    }
}

/**
 * True for memories with real media (video / image / album) that deserve
 * the thumbnail-forward card. Link-only, article, and still-processing
 * items with no media render the compact source/link card instead.
 */
fun isMediaItem(mediaKind: String?): Boolean =
    mediaKind == "video" || mediaKind == "image" || mediaKind == "album"

/**
 * Whether the thumbnail fetch can be skipped without hitting the
 * network: the backend reported `has_thumbnail: false` on the summary /
 * detail, or this session already got a 404 for the memory.
 */
fun shouldSkipThumbnail(hasThumbnail: Boolean?, alreadyKnownMissing: Boolean): Boolean =
    hasThumbnail == false || alreadyKnownMissing

/** Ask / Actions / Brief tabs exist only on the READY route. */
fun allowsQuestionTabs(route: DetailRoute): Boolean = route == DetailRoute.READY

// ---------------------------------------------------------------------------
// Processing stages
// ---------------------------------------------------------------------------

/** Backend stage → honest display label. No fake percentages anywhere. */
fun stageLabel(stage: String?): String = when (stage?.uppercase()) {
    "CAPTURED", "QUEUED" -> "Uploaded"
    "RESOLVING_SOURCE" -> "Finding media"
    "MEDIA_READY" -> "Extracting media"
    "TRANSCRIBING" -> "Transcribing speech"
    "ANALYZING_VISUALS" -> "Understanding visuals"
    "RUNNING_OCR" -> "Reading on-screen text"
    "GENERATING_MEMORY" -> "Building memory"
    "INDEXING" -> "Indexing"
    "READY" -> "Ready"
    else -> "Working on it"
}

/** Ordered pipeline shown on the processing screen; stages before the
 * current one render as done, the current one as in progress. */
val PROCESSING_STAGES: List<String> = listOf(
    "CAPTURED",
    "RESOLVING_SOURCE",
    "MEDIA_READY",
    "TRANSCRIBING",
    "ANALYZING_VISUALS",
    "RUNNING_OCR",
    "GENERATING_MEMORY",
    "INDEXING",
)

/**
 * Position of [stage] inside [PROCESSING_STAGES], or -1 when the backend
 * reports something we don't recognize (renders as "working on it" without
 * a fake position).
 */
fun stagePosition(stage: String?): Int {
    val normalized = stage?.uppercase() ?: return -1
    // CAPTURED and QUEUED are the same first step for display purposes.
    val key = if (normalized == "QUEUED") "CAPTURED" else normalized
    return PROCESSING_STAGES.indexOf(key)
}

// ---------------------------------------------------------------------------
// Failure copy + retry classification
// ---------------------------------------------------------------------------

/** Retryable backend failure codes. Everything else is permanent. */
val RETRYABLE_FAILURE_CODES: Set<String> = setOf(
    "SOURCE_RATE_LIMITED",
    "SOURCE_RESOLUTION_FAILED",
    "MEDIA_DOWNLOAD_FAILED",
    "TRANSCRIPTION_FAILED",
    "VISION_FAILED",
    "OCR_FAILED",
    "EMBEDDING_FAILED",
    "VISUAL_INDEX_FAILED",
    "DATABASE_FAILED",
    "INDEXING_FAILED",
)

/** True when "Try again" (POST reprocess) is offered; false → Open original / Remove. */
fun isRetryableFailure(failureCode: String?): Boolean =
    failureCode?.uppercase() in RETRYABLE_FAILURE_CODES

/**
 * Human copy for a backend failure code. Never surfaces raw exception text;
 * raw technical detail lives only in the hidden developer view.
 */
fun failureCopy(failureCode: String?): String = when (failureCode?.uppercase()) {
    "MEDIA_DOWNLOAD_FAILED", "SOURCE_RESOLUTION_FAILED" ->
        "We couldn't download the media."
    "SOURCE_DELETED" ->
        "The original was deleted."
    "SOURCE_PRIVATE", "SOURCE_LOGIN_REQUIRED" ->
        "The original isn't publicly accessible."
    "MEDIA_DECODE_FAILED" ->
        "The file couldn't be read."
    "TRANSCRIPTION_FAILED", "VISION_FAILED", "OCR_FAILED",
    "EMBEDDING_FAILED", "VISUAL_INDEX_FAILED", "INDEXING_FAILED" ->
        "Analysis hit a snag."
    "INVALID_URL", "UNSUPPORTED_SOURCE" ->
        "That link isn't supported yet."
    "DATABASE_FAILED" ->
        "A server hiccup interrupted this."
    else -> "We couldn't analyze this item."
}

// ---------------------------------------------------------------------------
// Search result rendering helpers
// ---------------------------------------------------------------------------

/** Icon + label for the "why it matched" row of a search result. */
data class WhyMatch(val icon: String, val label: String)

/** Search evidence type → why-it-matched presentation. */
fun whyMatch(modality: String): WhyMatch = when (modality.uppercase()) {
    "VISUAL" -> WhyMatch("👁", "Visual match")
    "SPEECH" -> WhyMatch("🎙", "Transcript")
    "OCR" -> WhyMatch("🔤", "On-screen text")
    "CAPTION" -> WhyMatch("✦", "Caption match")
    "TAG" -> WhyMatch("🏷", "Tag")
    else -> WhyMatch("✦", "Match")
}

/**
 * "· 00:18" suffix for the why-it-matched row when the evidence carries a
 * start timestamp; null when there is none (never a fake timestamp).
 */
fun evidenceTimeSuffix(startMs: Long?): String? {
    if (startMs == null || startMs < 0) return null
    val totalSeconds = startMs / 1000
    val hours = totalSeconds / 3600
    val minutes = (totalSeconds % 3600) / 60
    val seconds = totalSeconds % 60
    val clock = if (hours > 0) {
        "%d:%02d:%02d".format(hours, minutes, seconds)
    } else {
        "%02d:%02d".format(minutes, seconds)
    }
    return "· $clock"
}

/** "Instagram", "TikTok", "YouTube"; anything else is shown as-is. */
fun platformDisplayName(platform: String?): String = when (platform?.uppercase()) {
    "INSTAGRAM" -> "Instagram"
    "TIKTOK" -> "TikTok"
    "YOUTUBE" -> "YouTube"
    "X", "TWITTER" -> "X"
    null -> "Unknown source"
    else -> platform
}

/**
 * Search-result title fallback chain: real title → "Post by @handle" →
 * "Saved {platform} post" → "Saved memory". Never a bare "Untitled reel"
 * when evidence exists.
 */
fun searchResultTitle(
    title: String?,
    creatorHandle: String?,
    platform: String?,
): String {
    title?.takeIf { it.isNotBlank() }?.let { return it.trim() }
    creatorHandle?.takeIf { it.isNotBlank() }?.let { return "Post by @${it.trim()}" }
    platform?.takeIf { it.isNotBlank() }?.let { return "Saved ${platformDisplayName(it)} post" }
    return "Saved memory"
}

/** Library-card title fallback (list items carry no creator handle). */
fun libraryTitleFallback(title: String?, platform: String?): String =
    searchResultTitle(title, null, platform)

/** Best 1–2 line context for a search result: top evidence snippet, else the summary. */
fun searchResultSnippet(firstSnippet: String?, summary: String?): String =
    firstSnippet?.takeIf { it.isNotBlank() }
        ?: summary?.takeIf { it.isNotBlank() }
        ?: ""

// ---------------------------------------------------------------------------
// Library filters
// ---------------------------------------------------------------------------

/** Library filter chips. */
enum class LibraryFilter(val label: String) {
    ALL("All"),
    READY("Ready"),
    VIDEOS("Videos"),
    IMAGES("Images"),
    LINKS("Links"),
}

/**
 * Whether a memory belongs under [this] filter.
 * media_kind: Videos → video; Images → image|album; Links → article or
 * METADATA_ONLY status; Ready → READY status; a null media_kind shows under
 * All/Ready only.
 */
fun LibraryFilter.matches(processingStatus: String, mediaKind: String?): Boolean = when (this) {
    LibraryFilter.ALL -> true
    LibraryFilter.READY -> processingStatus == "READY"
    LibraryFilter.VIDEOS -> mediaKind == "video"
    LibraryFilter.IMAGES -> mediaKind == "image" || mediaKind == "album"
    LibraryFilter.LINKS -> mediaKind == "article" || processingStatus == "METADATA_ONLY"
}

/**
 * "Mar 4, 2026" from an ISO-8601 created_at; falls back to the date part or
 * the raw value when the backend sends something unexpected.
 */
fun formatSavedDate(createdAt: String?): String {
    if (createdAt.isNullOrBlank()) return ""
    val datePart = createdAt.trim().take(10) // yyyy-MM-dd
    val parts = datePart.split("-")
    if (parts.size != 3) return createdAt
    val (y, m, d) = parts
    val month = when (m) {
        "01" -> "Jan"; "02" -> "Feb"; "03" -> "Mar"; "04" -> "Apr"
        "05" -> "May"; "06" -> "Jun"; "07" -> "Jul"; "08" -> "Aug"
        "09" -> "Sep"; "10" -> "Oct"; "11" -> "Nov"; "12" -> "Dec"
        else -> return createdAt
    }
    val day = d.toIntOrNull() ?: return createdAt
    val year = y.toIntOrNull() ?: return createdAt
    return "$month $day, $year"
}
