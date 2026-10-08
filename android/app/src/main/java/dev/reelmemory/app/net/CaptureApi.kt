package dev.reelmemory.app.net

import android.util.Log
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
 * Minimal HTTP client for the Reel Memory backend.
 *
 * Single endpoint used by the client: POST {base}/v1/captures with
 * {"url": "<original shared URL>"}. The backend canonicalizes, dedupes
 * (200 + duplicate=true) and queues processing (202) itself, so the client
 * stays thin.
 *
 * All product endpoints require a Bearer token now ([SessionManager]
 * attaches it; a 401 triggers one silent refresh + retry inside
 * [SessionManager.authorizedHttp], a 402 surfaces the paywall).
 */
class CaptureApi(
    private val settings: SettingsStore,
    private val session: SessionManager? = null
) {

    sealed class SyncResult {
        data class Accepted(val memoryId: String?, val duplicate: Boolean) : SyncResult()
        data class Rejected(val code: String, val message: String) : SyncResult() // 4xx: don't retry the same way
        data class Transient(val message: String) : SyncResult() // 5xx/network: retry with backoff
        /** 401 after the session's refresh attempt: sign in again. */
        data object Unauthenticated : SyncResult()
        /** 402: free quota exhausted — the session already emitted the paywall event. */
        data class QuotaExceeded(val info: QuotaExceededInfo) : SyncResult()
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .callTimeout(45, TimeUnit.SECONDS)
        .build()

    private val jsonMediaType = "application/json; charset=utf-8".toMediaType()

    suspend fun postCapture(originalUrl: String): SyncResult = withContext(Dispatchers.IO) {
        postJson("/v1/captures", JsonObject().put("url", originalUrl).toString())
    }

    /**
     * Submits a plain web URL (website, article, doc) for capture.
     * Backend contract: `POST /v1/captures/url {"url": ...}` ->
     * 200/202 `{"id","memory_id","status","duplicate"}`; 422 invalid URL.
     */
    suspend fun postUrlCapture(url: String): SyncResult = withContext(Dispatchers.IO) {
        postJson("/v1/captures/url", JsonObject().put("url", url).toString())
    }

    private suspend fun baseOf(): String {
        val base = try {
            settings.getBackendUrl()
        } catch (e: Exception) {
            throw BackendUrlException(e.message ?: "cannot read backend URL")
        }
        return base
    }

    private class BackendUrlException(message: String) : Exception(message)

    private suspend fun postJson(path: String, jsonBody: String): SyncResult {
        val endpoint = try {
            "${baseOf()}$path"
        } catch (e: BackendUrlException) {
            return SyncResult.Transient(e.message ?: "cannot read backend URL")
        }
        val body = jsonBody.toRequestBody(jsonMediaType)
        // Built per attempt: SessionManager.authorizedHttp may execute this
        // twice (once, then once more after a silent token refresh).
        val attempt: suspend (bearer: String?) -> HttpResponse = { bearer ->
            val builder = Request.Builder()
                .url(endpoint)
                .post(body)
                .header("Accept", "application/json")
            if (bearer != null) builder.header("Authorization", "Bearer $bearer")
            client.newCall(builder.build()).execute().use { response ->
                Log.d(TAG, "POST $endpoint -> ${response.code}")
                HttpResponse(response.code, response.body?.string().orEmpty())
            }
        }
        return try {
            val resp = session?.authorizedHttp(attempt) ?: attempt(null)
            classifyResponse(resp.code, resp.body)
        } catch (e: Exception) {
            // DNS failure, refused connection, timeout, TLS error: all retryable.
            SyncResult.Transient(e.message ?: e.javaClass.simpleName)
        }
    }

    companion object {
        private const val TAG = "CaptureApi"

        /**
         * Pure classifier shared by the capture endpoints; JVM-testable.
         * 2xx -> Accepted (memory id + duplicate flag), 401 -> Unauthenticated,
         * 402 -> QuotaExceeded, other 4xx -> Rejected (no retry),
         * anything else -> Transient (retry with backoff).
         */
        fun classifyResponse(code: Int, payload: String): SyncResult {
            parseAuthOutcome(code, payload)?.let { outcome ->
                return when (outcome) {
                    AuthOutcome.Unauthorized -> SyncResult.Unauthenticated
                    is AuthOutcome.QuotaExceeded -> SyncResult.QuotaExceeded(outcome.info)
                    AuthOutcome.Ok -> throw AssertionError("unreachable")
                }
            }
            return when (code) {
                200, 202 -> {
                    val obj = try { JsonObject(payload) } catch (_: Exception) { JsonObject() }
                    SyncResult.Accepted(
                        memoryId = obj.optString("memory_id").takeIf { it.isNotEmpty() },
                        duplicate = obj.optBoolean("duplicate", false)
                    )
                }
                in 400..499 -> {
                    // Validation / unsupported source: retrying won't help.
                    val obj = try { JsonObject(payload) } catch (_: Exception) { null }
                    val detail = obj?.optObject("detail")
                    val detailCode = detail?.optString("code") ?: "HTTP_$code"
                    val msg = detail?.optString("message") ?: payload.ifEmpty { "request rejected" }
                    SyncResult.Rejected(detailCode, msg)
                }
                else -> SyncResult.Transient("backend returned HTTP $code")
            }
        }
    }
}
