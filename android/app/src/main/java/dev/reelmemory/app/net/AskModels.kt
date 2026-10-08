package dev.reelmemory.app.net

import dev.reelmemory.app.json.JsonArray
import dev.reelmemory.app.json.JsonObject

/**
 * Data model + JSON parsing for the Reel Memory Q&A and memory-browsing API.
 *
 * Backend contract (see android/README.md "Backend contract"):
 *
 *   GET  /v1/memories?limit=&offset=
 *     -> {"memories":[{"id","title","summary","category","processing_status","created_at"}],
 *         "limit":50,"offset":0}
 *   GET  /v1/memories/{id}/status
 *     -> {"id","processing_status","stage","attempt_count","failure_code","failure_message"}
 *   POST /v1/memories/{id}/ask   {"question": str, "verify": bool}
 *     -> {"answer": str,
 *         "evidence": [{"modality":"VISUAL|SPEECH|OCR|CAPTION|TAG",
 *                       "start_ms": int|null, "end_ms": int|null, "excerpt": str}],
 *         "verification": {"status":"supported|contradicted|uncertain",
 *                          "findings": [{"claim": str,
 *                                        "verdict":"supported|contradicted|uncertain",
 *                                        "sources":[{"title":str,"url":str}]}]}
 *          | null}
 *
 * NOTE: POST /v1/memories/{id}/ask does not exist on the backend yet — the
 * client is coded against the contract above. Until the backend implements
 * it, asking a question surfaces a clear "not available" message.
 *
 * Everything in this file is pure JVM logic (no Android APIs) so it is
 * covered by JVM unit tests.
 */

/** One modality-aware evidence citation grounding an answer. */
data class EvidenceChip(
    val modality: String,
    val startMs: Long?,
    val endMs: Long?,
    val excerpt: String,
    /**
     * Which photo of a carousel/album share the evidence came from
     * (backend `album_index`: photo N = index N-1). Null for single-file
     * memories, where there is only one photo to point at.
     */
    val albumIndex: Int? = null
) {
    /** "Photo 3" when the evidence is pinned to one carousel photo. */
    val photoLabel: String? get() = albumIndex?.let { "Photo ${it + 1}" }
}

data class WebSource(val title: String, val url: String)

enum class Verdict { SUPPORTED, CONTRADICTED, UNCERTAIN, UNKNOWN }

fun parseVerdict(raw: String?): Verdict = when (raw?.lowercase()) {
    "supported" -> Verdict.SUPPORTED
    "contradicted" -> Verdict.CONTRADICTED
    "uncertain" -> Verdict.UNCERTAIN
    else -> Verdict.UNKNOWN
}

data class VerificationFinding(
    val claim: String,
    val verdict: Verdict,
    val sources: List<WebSource>
)

data class AskVerification(val status: Verdict, val findings: List<VerificationFinding>)

data class AskResponse(
    val answer: String,
    val evidence: List<EvidenceChip>,
    val verification: AskVerification?
)

data class MemoryItem(
    val id: String,
    val title: String?,
    val summary: String?,
    val category: String?,
    val processingStatus: String,
    val createdAt: String?,
    /** Backend `platform` on list items (e.g. "INSTAGRAM"); null on old payloads. */
    val platform: String? = null,
    /** Backend `media_kind`: video|image|album|article|null. */
    val mediaKind: String? = null,
    /**
     * Backend `has_thumbnail`: false when the memory has no thumbnail so
     * the client can skip the thumbnail request entirely. Null on old
     * payloads (fall back to requesting; a 404 is cached in-session).
     */
    val hasThumbnail: Boolean? = null,
) {
    val isReady: Boolean get() = processingStatus == "READY"
    val displayTitle: String get() = title?.takeIf { it.isNotBlank() } ?: "Untitled reel"
}

/**
 * Full memory detail for the state-specific screens.
 *
 * Backend contract: `GET /v1/memories/{id}` -> 200
 * `{"id","title","summary","category","processing_status","media_kind",
 *   "source": {"platform","canonical_url","original_url","creator_handle",...}}`;
 * 404 unknown memory.
 */
data class MemoryDetail(
    val id: String,
    val title: String?,
    val summary: String?,
    val category: String?,
    val processingStatus: String,
    val mediaKind: String?,
    val platform: String?,
    val canonicalUrl: String?,
    val originalUrl: String?,
    val creatorHandle: String?,
    /** Backend `has_thumbnail`; null on old payloads (see [MemoryItem]). */
    val hasThumbnail: Boolean? = null,
) {
    /** Best URL for "Open original". */
    val effectiveUrl: String? get() = canonicalUrl ?: originalUrl
}

/** Parses the GET /v1/memories/{id} detail body. Never throws; null on bad JSON. */
fun parseMemoryDetail(payload: String): MemoryDetail? {
    val obj = try {
        JsonObject(payload)
    } catch (_: Exception) {
        return null
    }
    val source = obj.optObject("source")
    fun JsonObject?.str(name: String): String? =
        this?.optStringOrNull(name)?.takeIf { !this.isNull(name) }
    return MemoryDetail(
        id = obj.optString("id", ""),
        title = obj.str("title"),
        summary = obj.str("summary"),
        category = obj.str("category"),
        processingStatus = obj.optString("processing_status", "UNKNOWN"),
        mediaKind = obj.str("media_kind"),
        platform = obj.str("platform") ?: source.str("platform"),
        canonicalUrl = source.str("canonical_url"),
        originalUrl = source.str("original_url"),
        creatorHandle = source.str("creator_handle"),
        hasThumbnail = if (obj.isNull("has_thumbnail")) null
            else obj.optBoolean("has_thumbnail", false),
    )
}

data class MemoryStatus(
    val id: String,
    val processingStatus: String,
    val stage: String?,
    val attemptCount: Int,
    val failureCode: String?,
    val failureMessage: String?,
    /** Backend `media_kind` on the status payload; null when not sent. */
    val mediaKind: String? = null,
    /** Backend `platform` on the status payload; null when not sent. */
    val platform: String? = null,
) {
    val isReady: Boolean get() = processingStatus == "READY"

    /**
     * Terminal only when nothing will happen on its own. FAILED_RETRYABLE
     * is the backend's internal automatic retry — it can still become
     * READY, so pollers must keep waiting and the UI must not offer a
     * manual Try Again.
     */
    val isTerminalFailure: Boolean get() = processingStatus in setOf(
        "METADATA_ONLY",
        "SOURCE_UNAVAILABLE",
        "SOURCE_REQUIRES_ACCESS",
        "FAILED_PERMANENT",
        "DELETED",
    )
}

/** "0:12", "1:05", "1:02:03"; "--" when unknown. */
fun formatTimestamp(ms: Long?): String {
    if (ms == null || ms < 0) return "--"
    val totalSeconds = ms / 1000
    val hours = totalSeconds / 3600
    val minutes = (totalSeconds % 3600) / 60
    val seconds = totalSeconds % 60
    return if (hours > 0) {
        "%d:%02d:%02d".format(hours, minutes, seconds)
    } else {
        "%d:%02d".format(minutes, seconds)
    }
}

fun EvidenceChip.timestampLabel(): String = when {
    startMs != null && endMs != null && endMs != startMs ->
        "${formatTimestamp(startMs)}–${formatTimestamp(endMs)}"
    startMs != null -> formatTimestamp(startMs)
    else -> "--"
}

private fun JsonObject.optLongOrNull(name: String): Long? =
    if (isNull(name)) null else optLong(name)

fun parseEvidenceChip(obj: JsonObject): EvidenceChip = EvidenceChip(
    modality = obj.optString("modality", "UNKNOWN").uppercase(),
    startMs = obj.optLongOrNull("start_ms"),
    endMs = obj.optLongOrNull("end_ms"),
    excerpt = obj.optString("excerpt", ""),
    albumIndex = if (obj.isNull("album_index")) null else obj.optInt("album_index")
)

fun parseWebSource(obj: JsonObject): WebSource = WebSource(
    title = obj.optString("title", "").takeIf { it.isNotBlank() } ?: obj.optString("url", ""),
    url = obj.optString("url", "")
)

fun parseVerificationFinding(obj: JsonObject): VerificationFinding {
    val sources = mutableListOf<WebSource>()
    obj.optArray("sources")?.let { arr ->
        for (i in 0 until arr.length()) {
            arr.optObject(i)?.let { sources += parseWebSource(it) }
        }
    }
    return VerificationFinding(
        claim = obj.optString("claim", ""),
        verdict = parseVerdict(obj.optStringOrNull("verdict")),
        sources = sources
    )
}

fun parseAskVerification(obj: JsonObject): AskVerification {
    val findings = mutableListOf<VerificationFinding>()
    obj.optArray("findings")?.let { arr ->
        for (i in 0 until arr.length()) {
            arr.optObject(i)?.let { findings += parseVerificationFinding(it) }
        }
    }
    return AskVerification(
        status = parseVerdict(obj.optStringOrNull("status")),
        findings = findings
    )
}

/** Parses the POST /v1/memories/{id}/ask response body. Never throws. */
fun parseAskResponse(payload: String): AskResponse? {
    val obj = try {
        JsonObject(payload)
    } catch (_: Exception) {
        return null
    }
    val evidence = mutableListOf<EvidenceChip>()
    obj.optArray("evidence")?.let { arr ->
        for (i in 0 until arr.length()) {
            arr.optObject(i)?.let { evidence += parseEvidenceChip(it) }
        }
    }
    val verification = if (obj.isNull("verification")) {
        null
    } else {
        obj.optObject("verification")?.let { parseAskVerification(it) }
    }
    return AskResponse(
        answer = obj.optString("answer", ""),
        evidence = evidence,
        verification = verification
    )
}

/** Parses the GET /v1/memories response body. Never throws. */
fun parseMemoryListResponse(payload: String): List<MemoryItem> {
    val obj = try {
        JsonObject(payload)
    } catch (_: Exception) {
        return emptyList()
    }
    val out = mutableListOf<MemoryItem>()
    val arr: JsonArray = obj.optArray("memories") ?: return out
    for (i in 0 until arr.length()) {
        val m = arr.optObject(i) ?: continue
        out += MemoryItem(
            id = m.optString("id", ""),
            title = m.optStringOrNull("title").takeIf { !m.isNull("title") },
            summary = m.optStringOrNull("summary").takeIf { !m.isNull("summary") },
            category = m.optStringOrNull("category").takeIf { !m.isNull("category") },
            processingStatus = m.optString("processing_status", "UNKNOWN"),
            createdAt = m.optStringOrNull("created_at").takeIf { !m.isNull("created_at") },
            platform = m.optStringOrNull("platform").takeIf { !m.isNull("platform") },
            mediaKind = m.optStringOrNull("media_kind").takeIf { !m.isNull("media_kind") },
            hasThumbnail = if (m.isNull("has_thumbnail")) null
                else m.optBoolean("has_thumbnail", false)
        )
    }
    return out
}

/** Parses the GET /v1/memories/{id}/status response body. Never throws. */
fun parseMemoryStatusResponse(payload: String): MemoryStatus? {
    val obj = try {
        JsonObject(payload)
    } catch (_: Exception) {
        return null
    }
    return MemoryStatus(
        id = obj.optString("id", ""),
        processingStatus = obj.optString("processing_status", "UNKNOWN"),
        stage = obj.optStringOrNull("stage").takeIf { !obj.isNull("stage") },
        attemptCount = obj.optInt("attempt_count", 0),
        failureCode = obj.optStringOrNull("failure_code").takeIf { !obj.isNull("failure_code") },
        failureMessage = obj.optStringOrNull("failure_message").takeIf { !obj.isNull("failure_message") },
        mediaKind = obj.optStringOrNull("media_kind").takeIf { !obj.isNull("media_kind") },
        platform = obj.optStringOrNull("platform").takeIf { !obj.isNull("platform") }
    )
}
