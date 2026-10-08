package dev.reelmemory.app.net

import dev.reelmemory.app.json.JsonArray
import dev.reelmemory.app.json.JsonObject

// ---------- action items (POST /v1/memories/{id}/actions) ----------

/**
 * One to-do derived from a memory.
 *
 * Backend contract: `POST /v1/memories/{id}/actions` -> 200
 * `{"actions": [{"title": str, "detail": str, "priority": "P0"|"P1"|"P2",
 *   "effort": "small"|"medium"|"large",
 *   "evidence": {"modality": "speech"|"visual"|"ocr"|"caption"|"article",
 *                "timestamp_ms": int|null, "quote": str,
 *                "album_index": int|null}}]}`;
 * 404 unknown memory; 409 not READY.
 * Older flat `"evidence_quote"` payloads are still accepted as a fallback.
 */
data class ActionItem(
    val id: String,
    val title: String,
    val detail: String?,
    val priority: String,
    val effort: String?,
    val evidenceModality: String?,
    val evidenceTimestampMs: Long?,
    val evidenceQuote: String?,
    val evidenceAlbumIndex: Int?,
) {
    /** "Photo N" for album evidence, mirroring the ask-evidence chip. */
    val evidencePhotoLabel: String? get() = evidenceAlbumIndex?.let { "Photo ${it + 1}" }
}

/** Sort key so P0 renders before P1 before P2; unknown priorities last. */
fun ActionItem.priorityRank(): Int = when (priority.uppercase()) {
    "P0" -> 0
    "P1" -> 1
    "P2" -> 2
    else -> 3
}

/** Parses one action item from its JSON object. Never throws. */
fun parseActionItem(obj: JsonObject): ActionItem {
    val evidence = obj.optObject("evidence")
    val (modality, timestampMs, quote, albumIndex) = if (evidence != null) {
        Quad(
            evidence.optStringOrNull("modality").takeIf { !evidence.isNull("modality") },
            if (evidence.isNull("timestamp_ms")) null else evidence.optLong("timestamp_ms"),
            evidence.optStringOrNull("quote").takeIf { !evidence.isNull("quote") },
            if (evidence.isNull("album_index")) null else evidence.optInt("album_index"),
        )
    } else {
        // Backwards compatibility with the older flat contract.
        Quad(
            null, null,
            obj.optStringOrNull("evidence_quote").takeIf { !obj.isNull("evidence_quote") },
            null,
        )
    }
    return ActionItem(
        id = obj.optString("id", ""),
        title = obj.optString("title", "").ifEmpty { "(untitled)" },
        detail = obj.optStringOrNull("detail").takeIf { !obj.isNull("detail") },
        priority = obj.optString("priority", "P2").uppercase().ifEmpty { "P2" },
        effort = obj.optStringOrNull("effort").takeIf { !obj.isNull("effort") },
        evidenceModality = modality,
        evidenceTimestampMs = timestampMs,
        evidenceQuote = quote,
        evidenceAlbumIndex = albumIndex,
    )
}

private data class Quad<A, B, C, D>(val first: A, val second: B, val third: C, val fourth: D)

/**
 * Parses the actions response body into priority-sorted items.
 * Returns null when the payload is not valid JSON.
 */
fun parseActionsResponse(payload: String): List<ActionItem>? {
    val obj = try {
        JsonObject(payload)
    } catch (_: Exception) {
        return null
    }
    val arr = obj.optArray("actions") ?: JsonArray()
    val out = mutableListOf<ActionItem>()
    for (i in 0 until arr.length()) {
        out += parseActionItem(arr.optObject(i) ?: continue)
    }
    return out.sortedBy { it.priorityRank() }
}

// ---------- decision brief (POST /v1/memories/{id}/brief) ----------

/**
 * A decision card for one memory, generated against the user's stated goal.
 *
 * Backend contract (to be implemented): `POST /v1/memories/{id}/brief {"goal": str}` -> 200
 * `{"summary": str, "verdicts": [{"claim": str,
 *   "verdict": "supported"|"contradicted"|"uncertain", "note": str|null}],
 *   "usefulness": "high"|"medium"|"low", "effort": "small"|"medium"|"large",
 *   "closing_question": str}`; 404 unknown memory; 409 not READY; 422 empty goal.
 */
data class BriefVerdict(
    val claim: String,
    val verdict: Verdict,
    val note: String?,
)

data class DecisionBrief(
    val summary: String,
    val verdicts: List<BriefVerdict>,
    val usefulness: String?,
    val effort: String?,
    val closingQuestion: String,
)

/** Parses one brief validation verdict. Never throws. */
fun parseBriefVerdict(obj: JsonObject): BriefVerdict = BriefVerdict(
    claim = obj.optString("claim", "").ifEmpty { "(no claim)" },
    verdict = parseVerdict(obj.optStringOrNull("verdict")),
    note = obj.optStringOrNull("note").takeIf { !obj.isNull("note") },
)

/**
 * Parses the brief response body. Returns null when the payload is not valid
 * JSON or has no usable content.
 */
fun parseDecisionBriefResponse(payload: String): DecisionBrief? {
    val obj = try {
        JsonObject(payload)
    } catch (_: Exception) {
        return null
    }
    val summary = obj.optString("summary", "")
    val closing = obj.optString("closing_question", "")
    if (summary.isEmpty() && closing.isEmpty()) return null
    val arr = obj.optArray("verdicts") ?: JsonArray()
    val verdicts = mutableListOf<BriefVerdict>()
    for (i in 0 until arr.length()) {
        verdicts += parseBriefVerdict(arr.optObject(i) ?: continue)
    }
    return DecisionBrief(
        summary = summary,
        verdicts = verdicts,
        usefulness = obj.optStringOrNull("usefulness").takeIf { !obj.isNull("usefulness") },
        effort = obj.optStringOrNull("effort").takeIf { !obj.isNull("effort") },
        closingQuestion = closing,
    )
}
