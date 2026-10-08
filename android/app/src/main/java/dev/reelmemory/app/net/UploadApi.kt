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
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.asRequestBody
import okio.Buffer
import okio.BufferedSink
import okio.ForwardingSink
import okio.buffer
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Uploads actual video/image files to the backend's POST /v1/captures/upload
 * endpoint (multipart/form-data).
 *
 * Why this exists: unauthenticated Instagram URL fetching yields zero video
 * bytes, so the app must send the media itself when the user shares a video
 * file or a screenshot/image. The URL-share path stays, but its result is labeled
 * honestly as
 * "link saved (preview only — video not readable)".
 *
 * Backend contract (see android/README.md; endpoint does not exist yet):
 *   POST {base}/v1/captures/upload   multipart: file=<video bytes>,
 *       original_url=<optional text>
 *   202 new / 200 duplicate ->
 *       {"id": uuid, "memory_id": uuid, "status": str, "duplicate": bool}
 *   413 -> file too large (client guards first at MAX_UPLOAD_BYTES)
 *   422 -> missing/invalid file: {"detail": {"code": str, "message": str}}
 */
class UploadApi(
    private val settings: SettingsStore,
    private val session: SessionManager? = null
) {

    sealed class UploadResult {
        data class Uploaded(val memoryId: String?, val duplicate: Boolean) : UploadResult()
        data class TooLarge(val sizeBytes: Long) : UploadResult()
        data class Rejected(val code: String, val message: String) : UploadResult()
        data class Transient(val message: String) : UploadResult()
        /** 401 after the session's refresh attempt: sign in again. */
        data object Unauthenticated : UploadResult()
        /** 402: free upload quota exhausted — the session already emitted the paywall event. */
        data class QuotaExceeded(val info: QuotaExceededInfo) : UploadResult()
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .writeTimeout(600, TimeUnit.SECONDS) // large video uploads need room
        .callTimeout(660, TimeUnit.SECONDS)
        .build()

    suspend fun uploadVideo(
        file: File,
        fileName: String,
        mimeType: String,
        originalUrl: String?,
        onProgress: (sentBytes: Long, totalBytes: Long) -> Unit
    ): UploadResult = withContext(Dispatchers.IO) {
        val sizeBytes = file.length()
        if (!isWithinSizeLimit(sizeBytes)) {
            return@withContext UploadResult.TooLarge(sizeBytes)
        }
        val base = try {
            settings.getBackendUrl()
        } catch (e: Exception) {
            return@withContext UploadResult.Transient("cannot read backend URL: ${e.message}")
        }
        val endpoint = "$base/v1/captures/upload"
        val body = buildMultipartBody(file, fileName, mimeType, originalUrl, onProgress)
        // Built per attempt: SessionManager.authorizedHttp may execute this
        // twice (once, then once more after a silent token refresh). The file
        // body is re-readable, so reuse is safe.
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
            parseUploadResult(resp.code, resp.body)
        } catch (e: Exception) {
            UploadResult.Transient(e.message ?: e.javaClass.simpleName)
        }
    }

    companion object {
        /** Client-side guard matching the backend default (PRD: 200 MB). */
        const val MAX_UPLOAD_BYTES = 200L * 1024 * 1024

        fun isWithinSizeLimit(sizeBytes: Long): Boolean =
            sizeBytes in 1..MAX_UPLOAD_BYTES

        fun formatBytes(bytes: Long): String = when {
            bytes < 1024 -> "$bytes B"
            bytes < 1024 * 1024 -> "%.1f KB".format(bytes / 1024.0)
            bytes < 1024 * 1024 * 1024 -> "%.1f MB".format(bytes / (1024.0 * 1024))
            else -> "%.2f GB".format(bytes / (1024.0 * 1024 * 1024))
        }

        /**
         * Builds the multipart body for POST /v1/captures/upload.
         * Pure construction (no network) — JVM-testable.
         */
        fun buildMultipartBody(
            file: File,
            fileName: String,
            mimeType: String,
            originalUrl: String?,
            onProgress: (sentBytes: Long, totalBytes: Long) -> Unit = { _, _ -> }
        ): MultipartBody {
            val mediaType = mimeType.toMediaTypeOrNull()
            val fileBody = ProgressRequestBody(file.asRequestBody(mediaType), onProgress)
            val builder = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("file", fileName, fileBody)
            if (!originalUrl.isNullOrBlank()) {
                builder.addFormDataPart("original_url", originalUrl)
            }
            return builder.build()
        }

        /** Maps (HTTP code, body) onto an UploadResult. Pure — JVM-testable. */
        fun parseUploadResult(code: Int, payload: String): UploadResult {
            parseAuthOutcome(code, payload)?.let { outcome ->
                return when (outcome) {
                    AuthOutcome.Unauthorized -> UploadResult.Unauthenticated
                    is AuthOutcome.QuotaExceeded -> UploadResult.QuotaExceeded(outcome.info)
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
                    UploadResult.Uploaded(
                        memoryId = obj.optString("memory_id").takeIf { it.isNotEmpty() },
                        duplicate = obj.optBoolean("duplicate", false)
                    )
                }
                413 -> UploadResult.TooLarge(-1)
                in 400..499 -> {
                    val obj = try {
                        JsonObject(payload)
                    } catch (_: Exception) {
                        null
                    }
                    val detail = obj?.optObject("detail")
                    UploadResult.Rejected(
                        code = detail?.optString("code") ?: "HTTP_$code",
                        message = detail?.optString("message")
                            ?: payload.ifEmpty { "upload rejected" }
                    )
                }
                else -> UploadResult.Transient("backend returned HTTP $code")
            }
        }
    }
}

/**
 * RequestBody wrapper that reports cumulative bytes written. Used to drive
 * the upload progress indicator. Pure okio — JVM-testable.
 */
class ProgressRequestBody(
    private val delegate: RequestBody,
    private val onProgress: (sentBytes: Long, totalBytes: Long) -> Unit
) : RequestBody() {

    override fun contentType() = delegate.contentType()

    override fun contentLength(): Long = try {
        delegate.contentLength()
    } catch (_: Exception) {
        -1L
    }

    override fun writeTo(sink: BufferedSink) {
        val total = contentLength()
        var sent = 0L
        val counting = object : ForwardingSink(sink) {
            override fun write(source: Buffer, byteCount: Long) {
                super.write(source, byteCount)
                sent += byteCount
                onProgress(sent, total)
            }
        }.buffer()
        delegate.writeTo(counting)
        counting.flush()
        onProgress(sent.coerceAtLeast(0), total)
    }
}
