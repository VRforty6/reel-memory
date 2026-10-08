package dev.reelmemory.app.net

import dev.reelmemory.app.auth.HttpResponse
import dev.reelmemory.app.auth.SessionManager
import dev.reelmemory.app.data.SettingsStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

/**
 * Fetches per-memory thumbnails.
 *
 * Backend contract: `GET /v1/memories/{id}/thumbnail` -> 200 `image/jpeg`
 * bytes, or 404 when the memory has no thumbnail (old memories). Callers
 * show a placeholder on null.
 */
class ThumbnailApi(
    private val settings: SettingsStore,
    private val session: SessionManager? = null
) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .callTimeout(45, TimeUnit.SECONDS)
        .build()

    /**
     * Raw JPEG bytes, or null when the backend has no thumbnail (404).
     * Throws on transport errors and unexpected HTTP codes.
     */
    suspend fun fetchThumbnail(memoryId: String): ByteArray? = withContext(Dispatchers.IO) {
        val base = try {
            settings.getBackendUrl()
        } catch (e: Exception) {
            throw IllegalStateException("cannot read backend URL")
        }
        var code = 0
        var bytes: ByteArray? = null
        val attempt: suspend (bearer: String?) -> HttpResponse = { bearer ->
            val builder = Request.Builder()
                .url("$base/v1/memories/$memoryId/thumbnail")
                .get()
                .header("Accept", "image/jpeg")
            if (bearer != null) builder.header("Authorization", "Bearer $bearer")
            client.newCall(builder.build()).execute().use { response ->
                code = response.code
                bytes = if (response.code == 200) response.body?.bytes() else null
                HttpResponse(response.code, "")
            }
        }
        session?.authorizedHttp(attempt) ?: attempt(null)
        when (code) {
            200 -> bytes
            404 -> null
            else -> throw IllegalStateException("backend returned HTTP $code")
        }
    }
}
