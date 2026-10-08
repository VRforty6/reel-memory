package dev.reelmemory.app.net

import dev.reelmemory.app.auth.AuthOutcome
import dev.reelmemory.app.auth.HttpResponse
import dev.reelmemory.app.auth.QuotaExceededInfo
import dev.reelmemory.app.auth.SessionManager
import dev.reelmemory.app.auth.parseAuthOutcome
import dev.reelmemory.app.data.SettingsStore
import dev.reelmemory.app.json.JsonObject
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Uploads carousel/album shares (2-30 photos, at most one video) to the
 * backend's POST /v1/captures/album endpoint (multipart/form-data).
 *
 * One share = ONE multipart call with every photo, ONE memory, and ONE
 * capture against the free quota — the 20-photo carousel costs the same as
 * a single reel. The backend tags every segment with `album_index`, so
 * citations say "photo N".
 *
 * Backend contract (live):
 *   POST {base}/v1/captures/album   multipart: files=<photo bytes> (repeat),
 *       original_url=<optional text>
 *   202 new / 200 duplicate ->
 *       {"id": uuid, "memory_id": uuid, "status": str, "duplicate": bool,
 *        "file_count": int, "content_hashes": [str]}
 *   413 -> album over the total cap (ALBUM_TOO_LARGE)
 *   422 -> {"detail": {"code": str, "message": str}} — e.g. ALBUM_TOO_MANY_VIDEOS
 *   402 -> quota exhausted (one capture unit)
 *
 * Single-file shares keep using POST /v1/captures/upload (UploadApi).
 */

/** One album member, already copied into app-private storage. */
data class AlbumPart(
    val file: File,
    val fileName: String,
    val mimeType: String
)

class AlbumApi(
    private val settings: SettingsStore,
    private val session: SessionManager? = null
) {

    sealed class AlbumResult {
        data class Uploaded(
            val memoryId: String?,
            val duplicate: Boolean,
            val fileCount: Int,
            val contentHashes: List<String>
        ) : AlbumResult()

        data class TooLarge(val totalBytes: Long) : AlbumResult()
        data class Rejected(val code: String, val message: String) : AlbumResult()
        data class Transient(val message: String) : AlbumResult()
        /** 401 after the session's refresh attempt: sign in again. */
        data object Unauthenticated : AlbumResult()
        /** 402: free capture quota exhausted — the session already emitted the paywall event. */
        data class QuotaExceeded(val info: QuotaExceededInfo) : AlbumResult()
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(180, TimeUnit.SECONDS)
        .writeTimeout(900, TimeUnit.SECONDS) // 400 MB albums need room
        .callTimeout(960, TimeUnit.SECONDS)
        .build()

    suspend fun uploadAlbum(
        parts: List<AlbumPart>,
        originalUrl: String?,
        onProgress: (sentBytes: Long, totalBytes: Long) -> Unit
    ): AlbumResult = withContext(Dispatchers.IO) {
        val base = try {
            settings.getBackendUrl()
        } catch (e: Exception) {
            return@withContext AlbumResult.Transient("cannot read backend URL: ${e.message}")
        }
        val endpoint = "$base/v1/captures/album"
        val body = buildAlbumMultipartBody(parts, originalUrl, onProgress)
        // Built per attempt: SessionManager.authorizedHttp may execute this
        // twice (once, then once more after a silent token refresh). The file
        // bodies are re-readable, so reuse is safe.
        val attempt: suspend (bearer: String?) -> HttpResponse = { bearer ->
            val builder = Request.Builder()
                .url(endpoint)
                .post(body)
                .header("Accept", "application/json")
            if (bearer != null) builder.header("Authorization", "Bearer $bearer")
            client.newCall(builder.build()).execute().use { response ->
                HttpResponse(response.code, response.body?.string().orEmpty())
            }
        }
        try {
            val resp = session?.authorizedHttp(attempt) ?: attempt(null)
            parseAlbumResult(resp.code, resp.body)
        } catch (e: Exception) {
            AlbumResult.Transient(e.message ?: e.javaClass.simpleName)
        }
    }

    companion object {
        /** Backend rule: an album holds 2-30 files (MAX_ALBUM_FILES, default 30). */
        const val MIN_ALBUM_FILES = 2
        const val MAX_ALBUM_FILES = 30

        /** Backend rule: whole album capped at MAX_ALBUM_MB (default 400). */
        const val MAX_ALBUM_BYTES = 400L * 1024 * 1024

        /** Pure view of one album member for validation (no Android APIs). */
        data class AlbumFileInfo(
            val fileName: String,
            val mimeType: String,
            val sizeBytes: Long
        )

        /** Result of client-side album validation. */
        sealed class AlbumValidity {
            data object Valid : AlbumValidity()
            data class TooFew(val count: Int) : AlbumValidity()
            data class TooMany(val count: Int) : AlbumValidity()
            data object TooManyVideos : AlbumValidity()
            data class UnsupportedType(val mimeType: String) : AlbumValidity()
            data class PerFileTooLarge(val fileName: String, val sizeBytes: Long) :
                AlbumValidity()

            data class TooLargeTotal(val totalBytes: Long) : AlbumValidity()
        }

        /**
         * Validates an album against the backend's rules before any bytes are
         * copied: 2-30 files, at most one video, image or video MIME types
         * only, each
         * file within the per-file 200 MB guard, total within 400 MB.
         * Pure — JVM-testable.
         */
        fun classifyAlbum(files: List<AlbumFileInfo>): AlbumValidity {
            if (files.size < MIN_ALBUM_FILES) return AlbumValidity.TooFew(files.size)
            if (files.size > MAX_ALBUM_FILES) return AlbumValidity.TooMany(files.size)
            var videos = 0
            for (f in files) {
                val mime = f.mimeType.substringBefore(";").trim().lowercase()
                when {
                    mime.startsWith("video/") -> {
                        videos++
                        if (videos > 1) return AlbumValidity.TooManyVideos
                    }
                    !mime.startsWith("image/") -> return AlbumValidity.UnsupportedType(f.mimeType)
                }
            }
            for (f in files) {
                if (!UploadApi.isWithinSizeLimit(f.sizeBytes)) {
                    return AlbumValidity.PerFileTooLarge(f.fileName, f.sizeBytes)
                }
            }
            val total = files.sumOf { it.sizeBytes }
            if (total > MAX_ALBUM_BYTES) return AlbumValidity.TooLargeTotal(total)
            return AlbumValidity.Valid
        }

        /**
         * Builds the multipart body for POST /v1/captures/album: one `files`
         * part per photo in share order. Progress is aggregated across all
         * parts so the UI shows one batch percentage. Pure construction (no
         * network) — JVM-testable.
         */
        fun buildAlbumMultipartBody(
            parts: List<AlbumPart>,
            originalUrl: String?,
            onProgress: (sentBytes: Long, totalBytes: Long) -> Unit = { _, _ -> }
        ): MultipartBody {
            val total = parts.sumOf {
                try {
                    it.file.length()
                } catch (_: Exception) {
                    0L
                }
            }
            // Per-part cumulative bytes; aggregated under one lock.
            val sent = LongArray(parts.size)
            val lock = Any()
            val builder = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
            parts.forEachIndexed { index, part ->
                val fileBody = ProgressRequestBody(
                    part.file.asRequestBody(part.mimeType.toMediaTypeOrNull()),
                    onProgress = { partSent, _ ->
                        synchronized(lock) {
                            sent[index] = partSent
                            onProgress(sent.sum(), total)
                        }
                    }
                )
                builder.addFormDataPart("files", part.fileName, fileBody)
            }
            if (!originalUrl.isNullOrBlank()) {
                builder.addFormDataPart("original_url", originalUrl)
            }
            return builder.build()
        }

        /** Maps (HTTP code, body) onto an AlbumResult. Pure — JVM-testable. */
        fun parseAlbumResult(code: Int, payload: String): AlbumResult {
            parseAuthOutcome(code, payload)?.let { outcome ->
                return when (outcome) {
                    AuthOutcome.Unauthorized -> AlbumResult.Unauthenticated
                    is AuthOutcome.QuotaExceeded -> AlbumResult.QuotaExceeded(outcome.info)
                    AuthOutcome.Ok -> throw AssertionError("unreachable")
                }
            }
            return when (code) {
                200, 202 -> {
                    val obj = try {
                        JsonObject(payload)
                    } catch (_: Exception) {
                        JsonObject()
                    }
                    val hashes = mutableListOf<String>()
                    obj.optArray("content_hashes")?.let { arr ->
                        for (i in 0 until arr.length()) {
                            arr.optString(i)?.takeIf { it.isNotEmpty() }?.let { hashes += it }
                        }
                    }
                    AlbumResult.Uploaded(
                        memoryId = obj.optString("memory_id").takeIf { it.isNotEmpty() },
                        duplicate = obj.optBoolean("duplicate", false),
                        fileCount = obj.optInt("file_count", hashes.size),
                        contentHashes = hashes
                    )
                }
                413 -> AlbumResult.TooLarge(-1)
                in 400..499 -> {
                    val obj = try {
                        JsonObject(payload)
                    } catch (_: Exception) {
                        null
                    }
                    val detail = obj?.optObject("detail")
                    AlbumResult.Rejected(
                        code = detail?.optString("code") ?: "HTTP_$code",
                        message = detail?.optString("message")
                            ?: payload.ifEmpty { "album rejected" }
                    )
                }
                else -> AlbumResult.Transient("backend returned HTTP $code")
            }
        }
    }
}
