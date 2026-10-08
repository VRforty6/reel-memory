package dev.reelmemory.app.net

import dev.reelmemory.app.auth.AuthOutcome
import dev.reelmemory.app.auth.HttpResponse
import dev.reelmemory.app.auth.QuotaExceededInfo
import dev.reelmemory.app.auth.SessionManager
import dev.reelmemory.app.auth.parseAuthOutcome
import dev.reelmemory.app.data.SettingsStore
import dev.reelmemory.app.json.JsonObject
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.InterruptedIOException
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

internal const val SEARCH_CONNECT_TIMEOUT_SECONDS = 10L
internal const val SEARCH_READ_TIMEOUT_SECONDS = 25L
internal const val SEARCH_CALL_TIMEOUT_SECONDS = 30L

/** Search is interactive, so it gets a short hard deadline of its own. */
internal fun buildSearchHttpClient(): OkHttpClient = OkHttpClient.Builder()
    .connectTimeout(SEARCH_CONNECT_TIMEOUT_SECONDS, TimeUnit.SECONDS)
    .readTimeout(SEARCH_READ_TIMEOUT_SECONDS, TimeUnit.SECONDS)
    .callTimeout(SEARCH_CALL_TIMEOUT_SECONDS, TimeUnit.SECONDS)
    .build()

/**
 * One evidence item grounding a search hit.
 *
 * Backend contract: `GET /v1/search?q=&limit=` -> 200
 * `{"results": [{"memory_id", "title", "summary", "category",
 *   "creator_handle", "platform", "processing_status", "source_status",
 *   "score", "evidence": [{"type": "VISUAL|SPEECH|OCR|CAPTION|TAG",
 *   "snippet", "start_ms": int|null, "end_ms": int|null}]}],
 *   "degraded": bool}`.
 *
 * `score` is parsed but never rendered (contract: scores stay internal).
 * Parse helpers are top-level and pure so they are JVM-unit-testable.
 */
data class SearchEvidence(
    val type: String,
    val snippet: String,
    val startMs: Long?,
    val endMs: Long?,
)

data class SearchResultItem(
    val memoryId: String,
    val title: String?,
    val summary: String?,
    val category: String?,
    val creatorHandle: String?,
    val platform: String?,
    val processingStatus: String,
    val sourceStatus: String?,
    val evidence: List<SearchEvidence>,
    /** Backend `media_kind`; null when not sent. */
    val mediaKind: String? = null,
    /** Backend `has_thumbnail`; null when not sent. */
    val hasThumbnail: Boolean? = null,
)

/** Parses one search-evidence item. Never throws. */
fun parseSearchEvidence(obj: JsonObject): SearchEvidence = SearchEvidence(
    type = obj.optString("type", "UNKNOWN").uppercase(),
    snippet = obj.optString("snippet", ""),
    startMs = if (obj.isNull("start_ms")) null else obj.optLong("start_ms"),
    endMs = if (obj.isNull("end_ms")) null else obj.optLong("end_ms"),
)

/** Parses one search result. Never throws. */
fun parseSearchResultItem(obj: JsonObject): SearchResultItem {
    val evidence = mutableListOf<SearchEvidence>()
    obj.optArray("evidence")?.let { arr ->
        for (i in 0 until arr.length()) {
            arr.optObject(i)?.let { evidence += parseSearchEvidence(it) }
        }
    }
    return SearchResultItem(
        memoryId = obj.optString("memory_id", ""),
        title = obj.optStringOrNull("title").takeIf { !obj.isNull("title") },
        summary = obj.optStringOrNull("summary").takeIf { !obj.isNull("summary") },
        category = obj.optStringOrNull("category").takeIf { !obj.isNull("category") },
        creatorHandle = obj.optStringOrNull("creator_handle").takeIf { !obj.isNull("creator_handle") },
        platform = obj.optStringOrNull("platform").takeIf { !obj.isNull("platform") },
        processingStatus = obj.optString("processing_status", "UNKNOWN"),
        sourceStatus = obj.optStringOrNull("source_status").takeIf { !obj.isNull("source_status") },
        evidence = evidence,
        mediaKind = obj.optStringOrNull("media_kind").takeIf { !obj.isNull("media_kind") },
        hasThumbnail = if (obj.isNull("has_thumbnail")) null
            else obj.optBoolean("has_thumbnail", false)
    )
}

/**
 * Parses the GET /v1/search response body into (results, degraded).
 * Returns null when the payload is not valid JSON.
 */
fun parseSearchResponse(payload: String): Pair<List<SearchResultItem>, Boolean>? {
    val obj = try {
        JsonObject(payload)
    } catch (_: Exception) {
        return null
    }
    val out = mutableListOf<SearchResultItem>()
    obj.optArray("results")?.let { arr ->
        for (i in 0 until arr.length()) {
            arr.optObject(i)?.let { out += parseSearchResultItem(it) }
        }
    }
    return out to obj.optBoolean("degraded", false)
}

/**
 * Client for `GET /v1/search`. Mirrors [MemoryApi]'s auth handling: Bearer
 * token via the session, 401 → Unauthenticated, 402 → QuotaExceeded.
 */
class SearchApi(
    private val settings: SettingsStore,
    private val session: SessionManager? = null
) {

    sealed class SearchOutcome {
        data class Ok(val results: List<SearchResultItem>, val degraded: Boolean) : SearchOutcome()
        data class Transient(val message: String) : SearchOutcome()
        data object Unauthenticated : SearchOutcome()
        data class QuotaExceeded(val info: QuotaExceededInfo) : SearchOutcome()
    }

    private val client = buildSearchHttpClient()

    private suspend fun baseUrl(): String? = try {
        settings.getBackendUrl()
    } catch (_: Exception) {
        null
    }

    private suspend fun authed(request: Request): HttpResponse {
        val attempt: suspend (bearer: String?) -> HttpResponse = { bearer ->
            val builder = request.newBuilder()
            if (bearer != null) builder.header("Authorization", "Bearer $bearer")
            client.newCall(builder.build()).execute().use { response ->
                HttpResponse(response.code, response.body?.string().orEmpty())
            }
        }
        return session?.authorizedHttp(attempt) ?: attempt(null)
    }

    private fun quotaOf(payload: String): QuotaExceededInfo =
        (parseAuthOutcome(402, payload) as? AuthOutcome.QuotaExceeded)?.info
            ?: QuotaExceededInfo(
                tier = "free", bucket = "unknown", limit = 0, used = 0, resetsAt = ""
            )

    suspend fun search(query: String, limit: Int = 25): SearchOutcome =
        withContext(Dispatchers.IO) {
            val trimmed = query.trim()
            if (trimmed.isEmpty()) {
                return@withContext SearchOutcome.Ok(emptyList(), degraded = false)
            }
            val base = baseUrl() ?: return@withContext SearchOutcome.Transient("cannot read backend URL")
            val encoded = URLEncoder.encode(trimmed, "UTF-8")
            val request = Request.Builder()
                .url("$base/v1/search?q=$encoded&limit=$limit")
                .get()
                .header("Accept", "application/json")
                .build()
            val resp = try {
                authed(request)
            } catch (e: CancellationException) {
                // A newer search or a closed screen cancelled this call.
                // Cancellation must remain cancellation, not become a stale
                // error result that can overwrite the next query.
                throw e
            } catch (_: InterruptedIOException) {
                return@withContext SearchOutcome.Transient(
                    "search timed out after $SEARCH_CALL_TIMEOUT_SECONDS seconds"
                )
            } catch (e: Exception) {
                return@withContext SearchOutcome.Transient(e.message ?: e.javaClass.simpleName)
            }
            val payload = resp.body
            when (resp.code) {
                200 -> {
                    val parsed = parseSearchResponse(payload)
                    if (parsed != null) SearchOutcome.Ok(parsed.first, parsed.second)
                    else SearchOutcome.Transient("could not parse search response")
                }
                401 -> SearchOutcome.Unauthenticated
                402 -> SearchOutcome.QuotaExceeded(quotaOf(payload))
                else -> SearchOutcome.Transient("backend returned HTTP ${resp.code}")
            }
        }
}
