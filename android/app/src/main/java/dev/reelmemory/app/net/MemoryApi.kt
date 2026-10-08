package dev.reelmemory.app.net

import dev.reelmemory.app.auth.AuthOutcome
import dev.reelmemory.app.auth.HttpResponse
import dev.reelmemory.app.auth.QuotaExceededInfo
import dev.reelmemory.app.auth.SessionManager
import dev.reelmemory.app.auth.parseAuthOutcome
import dev.reelmemory.app.data.SettingsStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import dev.reelmemory.app.json.JsonObject
import java.util.concurrent.TimeUnit

/**
 * Client for the memory browsing + Q&A endpoints:
 *
 *   GET  {base}/v1/memories?limit=&offset=
 *   GET  {base}/v1/memories/{id}
 *   GET  {base}/v1/memories/{id}/status
 *   DELETE {base}/v1/memories/{id}
 *   POST {base}/v1/memories/{id}/reprocess
 *   POST {base}/v1/memories/{id}/ask          (body {"question": str, "verify": bool})
 *   POST {base}/v1/memories/{id}/actions
 *   POST {base}/v1/memories/{id}/brief        (body {"goal": str})
 *
 * Every call goes through [SessionManager.authorizedHttp]: the Bearer token
 * is attached, a 401 triggers one silent refresh + retry, and a 402 (free
 * quota exhausted) is reported as [AuthOutcome.QuotaExceeded] — the session
 * emits the paywall event and each result type carries a QuotaExceeded
 * variant so the UI can react inline too.
 */
class MemoryApi(
    private val settings: SettingsStore,
    private val session: SessionManager? = null
) {

    sealed class ListResult {
        data class Ok(val memories: List<MemoryItem>) : ListResult()
        data class Transient(val message: String) : ListResult()
        data object Unauthenticated : ListResult()
        data class QuotaExceeded(val info: QuotaExceededInfo) : ListResult()
    }

    sealed class StatusResult {
        data class Ok(val status: MemoryStatus) : StatusResult()
        data class NotFound(val message: String) : StatusResult()
        data class Transient(val message: String) : StatusResult()
        data object Unauthenticated : StatusResult()
        data class QuotaExceeded(val info: QuotaExceededInfo) : StatusResult()
    }

    sealed class ActionsResult {
        data class Loaded(val actions: List<ActionItem>) : ActionsResult()
        data class NotReady(val processingStatus: String) : ActionsResult()
        data class Rejected(val message: String) : ActionsResult()
        data class Transient(val message: String) : ActionsResult()
        data object Unauthenticated : ActionsResult()
        data class QuotaExceeded(val info: QuotaExceededInfo) : ActionsResult()
    }

    sealed class BriefResult {
        data class Loaded(val brief: DecisionBrief) : BriefResult()
        data class NotReady(val processingStatus: String) : BriefResult()
        data class Rejected(val message: String) : BriefResult()
        data class Transient(val message: String) : BriefResult()
        data object Unauthenticated : BriefResult()
        data class QuotaExceeded(val info: QuotaExceededInfo) : BriefResult()
    }

    sealed class AskResult {
        data class Answer(val response: AskResponse) : AskResult()
        data class NotReady(val status: String) : AskResult()
        data class Rejected(val message: String) : AskResult()
        data class Transient(val message: String) : AskResult()
        data object Unauthenticated : AskResult()
        data class QuotaExceeded(val info: QuotaExceededInfo) : AskResult()
    }

    sealed class DetailResult {
        data class Ok(val detail: MemoryDetail) : DetailResult()
        data class NotFound(val message: String) : DetailResult()
        data class Transient(val message: String) : DetailResult()
        data object Unauthenticated : DetailResult()
        data class QuotaExceeded(val info: QuotaExceededInfo) : DetailResult()
    }

    sealed class DeleteResult {
        data object Deleted : DeleteResult()
        data class NotFound(val message: String) : DeleteResult()
        data class Transient(val message: String) : DeleteResult()
        data object Unauthenticated : DeleteResult()
        data class QuotaExceeded(val info: QuotaExceededInfo) : DeleteResult()
    }

    sealed class ReprocessResult {
        data object Accepted : ReprocessResult()
        data class NotFound(val message: String) : ReprocessResult()
        data class Transient(val message: String) : ReprocessResult()
        data object Unauthenticated : ReprocessResult()
        data class QuotaExceeded(val info: QuotaExceededInfo) : ReprocessResult()
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS) // answers can take a while
        .callTimeout(150, TimeUnit.SECONDS)
        .build()

    private val jsonMediaType = "application/json; charset=utf-8".toMediaType()

    private suspend fun baseUrl(): String? = try {
        settings.getBackendUrl()
    } catch (e: Exception) {
        null
    }

    /**
     * Executes [request] with the session's Bearer token when a session is
     * wired. A 401 triggers one silent refresh + retry inside the session;
     * a 402 emits the paywall event there. Callers still see the final 401 /
     * 402 and map them to explicit result variants for the UI.
     */
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

    suspend fun listMemories(limit: Int = 50, offset: Int = 0): ListResult =
        withContext(Dispatchers.IO) {
            val base = baseUrl() ?: return@withContext ListResult.Transient("cannot read backend URL")
            val request = Request.Builder()
                .url("$base/v1/memories?limit=$limit&offset=$offset")
                .get()
                .header("Accept", "application/json")
                .build()
            val resp = try {
                authed(request)
            } catch (e: Exception) {
                return@withContext ListResult.Transient(e.message ?: e.javaClass.simpleName)
            }
            val payload = resp.body
            when (resp.code) {
                200 -> ListResult.Ok(parseMemoryListResponse(payload))
                401 -> ListResult.Unauthenticated
                402 -> ListResult.QuotaExceeded(quotaOf(payload))
                else -> ListResult.Transient("backend returned HTTP ${resp.code}")
            }
        }

    suspend fun getStatus(memoryId: String): StatusResult =
        withContext(Dispatchers.IO) {
            val base = baseUrl() ?: return@withContext StatusResult.Transient("cannot read backend URL")
            val request = Request.Builder()
                .url("$base/v1/memories/$memoryId/status")
                .get()
                .header("Accept", "application/json")
                .build()
            val resp = try {
                authed(request)
            } catch (e: Exception) {
                return@withContext StatusResult.Transient(e.message ?: e.javaClass.simpleName)
            }
            val payload = resp.body
            when (resp.code) {
                200 -> {
                    val status = parseMemoryStatusResponse(payload)
                    if (status != null) StatusResult.Ok(status)
                    else StatusResult.Transient("could not parse status response")
                }
                401 -> StatusResult.Unauthenticated
                402 -> StatusResult.QuotaExceeded(quotaOf(payload))
                404 -> StatusResult.NotFound("memory not found on backend")
                else -> StatusResult.Transient("backend returned HTTP ${resp.code}")
            }
        }

    /**
     * Full memory detail (source URLs for the Open-original / Remove
     * actions on the link-only and failed screens).
     * Contract: `GET /v1/memories/{id}` -> 200 detail with `source:{...}`;
     * 404 unknown memory.
     */
    suspend fun getDetail(memoryId: String): DetailResult =
        withContext(Dispatchers.IO) {
            val base = baseUrl() ?: return@withContext DetailResult.Transient("cannot read backend URL")
            val request = Request.Builder()
                .url("$base/v1/memories/$memoryId")
                .get()
                .header("Accept", "application/json")
                .build()
            val resp = try {
                authed(request)
            } catch (e: Exception) {
                return@withContext DetailResult.Transient(e.message ?: e.javaClass.simpleName)
            }
            val payload = resp.body
            when (resp.code) {
                200 -> {
                    val detail = parseMemoryDetail(payload)
                    if (detail != null) DetailResult.Ok(detail)
                    else DetailResult.Transient("could not parse memory detail")
                }
                401 -> DetailResult.Unauthenticated
                402 -> DetailResult.QuotaExceeded(quotaOf(payload))
                404 -> DetailResult.NotFound("memory not found on backend")
                else -> DetailResult.Transient("backend returned HTTP ${resp.code}")
            }
        }

    /**
     * Deletes a memory. Contract: `DELETE /v1/memories/{id}` -> 204;
     * 404 unknown memory.
     */
    suspend fun deleteMemory(memoryId: String): DeleteResult =
        withContext(Dispatchers.IO) {
            val base = baseUrl() ?: return@withContext DeleteResult.Transient("cannot read backend URL")
            val request = Request.Builder()
                .url("$base/v1/memories/$memoryId")
                .delete()
                .build()
            val resp = try {
                authed(request)
            } catch (e: Exception) {
                return@withContext DeleteResult.Transient(e.message ?: e.javaClass.simpleName)
            }
            when (resp.code) {
                204 -> DeleteResult.Deleted
                401 -> DeleteResult.Unauthenticated
                402 -> DeleteResult.QuotaExceeded(quotaOf(resp.body))
                404 -> DeleteResult.NotFound("memory not found on backend")
                else -> DeleteResult.Transient("backend returned HTTP ${resp.code}")
            }
        }

    /**
     * Retries a failed memory. Contract: `POST /v1/memories/{id}/reprocess`
     * -> 202; 404 unknown memory.
     */
    suspend fun reprocessMemory(memoryId: String): ReprocessResult =
        withContext(Dispatchers.IO) {
            val base = baseUrl() ?: return@withContext ReprocessResult.Transient("cannot read backend URL")
            val request = Request.Builder()
                .url("$base/v1/memories/$memoryId/reprocess")
                .post(ByteArray(0).toRequestBody())
                .header("Accept", "application/json")
                .build()
            val resp = try {
                authed(request)
            } catch (e: Exception) {
                return@withContext ReprocessResult.Transient(e.message ?: e.javaClass.simpleName)
            }
            when (resp.code) {
                200, 202 -> ReprocessResult.Accepted
                401 -> ReprocessResult.Unauthenticated
                402 -> ReprocessResult.QuotaExceeded(quotaOf(resp.body))
                404 -> ReprocessResult.NotFound("memory not found on backend")
                else -> ReprocessResult.Transient("backend returned HTTP ${resp.code}")
            }
        }

    suspend fun ask(memoryId: String, question: String, verify: Boolean): AskResult =
        withContext(Dispatchers.IO) {
            val trimmed = question.trim()
            if (trimmed.isEmpty()) {
                return@withContext AskResult.Rejected("Type a question first.")
            }
            val base = baseUrl() ?: return@withContext AskResult.Transient("cannot read backend URL")
            val body = JsonObject()
                .put("question", trimmed)
                .put("verify", verify)
                .toString()
                .toRequestBody(jsonMediaType)
            val request = Request.Builder()
                .url("$base/v1/memories/$memoryId/ask")
                .post(body)
                .header("Accept", "application/json")
                .build()
            val resp = try {
                authed(request)
            } catch (e: Exception) {
                return@withContext AskResult.Transient(e.message ?: e.javaClass.simpleName)
            }
            val payload = resp.body
            when (resp.code) {
                200 -> {
                    val parsed = parseAskResponse(payload)
                    if (parsed != null) AskResult.Answer(parsed)
                    else AskResult.Transient("could not parse answer")
                }
                401 -> AskResult.Unauthenticated
                402 -> AskResult.QuotaExceeded(quotaOf(payload))
                404 -> AskResult.Rejected(
                    "This reel is no longer on the backend."
                )
                409 -> AskResult.NotReady(
                    parseMemoryStatusResponse(payload)?.processingStatus ?: "PROCESSING"
                )
                422 -> AskResult.Rejected("The backend rejected that question.")
                in 400..499 -> AskResult.Rejected("Backend refused the question (HTTP ${resp.code}).")
                else -> AskResult.Transient("backend returned HTTP ${resp.code}")
            }
        }

    /**
     * Loads the to-do actions derived from a memory.
     * Contract: `POST /v1/memories/{id}/actions` (no body) -> 200 `{"actions": [...]}`;
     * 404 unknown memory; 409 not READY.
     */
    suspend fun getActions(memoryId: String): ActionsResult =
        withContext(Dispatchers.IO) {
            val base = baseUrl() ?: return@withContext ActionsResult.Transient("cannot read backend URL")
            val request = Request.Builder()
                .url("$base/v1/memories/$memoryId/actions")
                .post(ByteArray(0).toRequestBody())
                .header("Accept", "application/json")
                .build()
            val resp = try {
                authed(request)
            } catch (e: Exception) {
                return@withContext ActionsResult.Transient(e.message ?: e.javaClass.simpleName)
            }
            val payload = resp.body
            when (resp.code) {
                200 -> {
                    val parsed = parseActionsResponse(payload)
                    if (parsed != null) ActionsResult.Loaded(parsed)
                    else ActionsResult.Transient("could not parse actions")
                }
                401 -> ActionsResult.Unauthenticated
                402 -> ActionsResult.QuotaExceeded(quotaOf(payload))
                404 -> ActionsResult.Rejected(
                    "This memory is no longer on the backend."
                )
                409 -> ActionsResult.NotReady(
                    parseMemoryStatusResponse(payload)?.processingStatus ?: "PROCESSING"
                )
                in 400..499 -> ActionsResult.Rejected(
                    "Backend refused the actions request (HTTP ${resp.code})."
                )
                else -> ActionsResult.Transient("backend returned HTTP ${resp.code}")
            }
        }

    /**
     * Generates a decision brief for a memory against the user's [goal].
     * Contract: `POST /v1/memories/{id}/brief {"goal": ...}` -> 200 decision card;
     * 404 unknown memory; 409 not READY yet; 422 empty goal.
     */
    suspend fun getBrief(memoryId: String, goal: String): BriefResult =
        withContext(Dispatchers.IO) {
            val trimmed = goal.trim()
            if (trimmed.isEmpty()) {
                return@withContext BriefResult.Rejected("Describe your goal first.")
            }
            val base = baseUrl() ?: return@withContext BriefResult.Transient("cannot read backend URL")
            val body = JsonObject()
                .put("goal", trimmed)
                .toString()
                .toRequestBody(jsonMediaType)
            val request = Request.Builder()
                .url("$base/v1/memories/$memoryId/brief")
                .post(body)
                .header("Accept", "application/json")
                .build()
            val resp = try {
                authed(request)
            } catch (e: Exception) {
                return@withContext BriefResult.Transient(e.message ?: e.javaClass.simpleName)
            }
            val payload = resp.body
            when (resp.code) {
                200 -> {
                    val parsed = parseDecisionBriefResponse(payload)
                    if (parsed != null) BriefResult.Loaded(parsed)
                    else BriefResult.Transient("could not parse decision brief")
                }
                401 -> BriefResult.Unauthenticated
                402 -> BriefResult.QuotaExceeded(quotaOf(payload))
                404 -> BriefResult.Rejected(
                    "This memory is no longer on the backend."
                )
                409 -> BriefResult.NotReady(
                    parseMemoryStatusResponse(payload)?.processingStatus ?: "PROCESSING"
                )
                422 -> BriefResult.Rejected("The backend rejected that goal.")
                in 400..499 -> BriefResult.Rejected(
                    "Backend refused the brief request (HTTP ${resp.code})."
                )
                else -> BriefResult.Transient("backend returned HTTP ${resp.code}")
            }
        }
}
