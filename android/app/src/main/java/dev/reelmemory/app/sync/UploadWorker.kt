package dev.reelmemory.app.sync

import android.content.Context
import android.util.Log
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import dev.reelmemory.app.auth.SessionManager
import dev.reelmemory.app.data.AlbumUploadDao
import dev.reelmemory.app.data.SettingsStore
import dev.reelmemory.app.data.ShareDatabase
import dev.reelmemory.app.data.VideoUploadStatus
import dev.reelmemory.app.net.AlbumApi
import dev.reelmemory.app.net.AlbumPart
import dev.reelmemory.app.net.MemoryApi
import dev.reelmemory.app.net.UploadApi
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Uploads saved video files to the backend's POST /v1/captures/upload
 * endpoint and carousel/album shares to POST /v1/captures/album (one
 * multipart call per album, one quota unit each), then polls the memory
 * status until it is READY.
 *
 * - Only runs with network connectivity; exponential backoff (30s base).
 * - Progress (0..100) is written to the local DB while UPLOADING so the
 *   Videos tab can show a progress bar.
 * - After the backend accepts the upload, the row becomes PROCESSING and the
 *   worker polls GET /v1/memories/{id}/status a bounded number of times.
 *   If the backend is still working when the poll budget runs out, the row
 *   stays PROCESSING and the Memories/Ask UI continues polling on open.
 * - A row stays in the local DB forever as the audit trail.
 */
class UploadWorker(
    appContext: Context,
    params: WorkerParameters
) : CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result = coroutineScope {
        val db = ShareDatabase.get(applicationContext)
        val dao = db.videoUploadDao()
        val settings = SettingsStore(applicationContext)
        val session = SessionManager.get(applicationContext)
        if (!session.hasSession()) {
            Log.i(TAG, "signed out; leaving uploads queued until sign-in")
            return@coroutineScope Result.success()
        }
        val uploadApi = UploadApi(settings, session)
        val albumApi = AlbumApi(settings, session)
        val memoryApi = MemoryApi(settings, session)

        val pending = dao.pendingOrFailed()
        var transientFailures = 0
        var stopEarly = false
        for (upload in pending) {
            val now = System.currentTimeMillis()
            val file = File(upload.localPath)
            if (!file.exists()) {
                dao.markResult(
                    upload.id, VideoUploadStatus.FAILED,
                    "local video file is gone — re-share the video", null, now
                )
                continue
            }
            if (!UploadApi.isWithinSizeLimit(file.length())) {
                dao.markResult(
                    upload.id, VideoUploadStatus.FAILED,
                    "video is ${UploadApi.formatBytes(file.length())}; " +
                        "limit is ${UploadApi.formatBytes(UploadApi.MAX_UPLOAD_BYTES)}",
                    null, now
                )
                continue
            }

            dao.markAttempt(upload.id, VideoUploadStatus.UPLOADING, now)
            var lastReported = -1
            when (
                val result = uploadApi.uploadVideo(
                    file = file,
                    fileName = upload.fileName,
                    mimeType = upload.mimeType,
                    originalUrl = upload.originalUrl,
                    onProgress = { sent, total ->
                        val pct = if (total > 0) {
                            ((sent * 100) / total).toInt().coerceIn(0, 100)
                        } else 0
                        // Throttle DB writes to actual percentage changes;
                        // fire-and-forget into this worker's scope.
                        if (pct != lastReported) {
                            lastReported = pct
                            val id = upload.id
                            launch {
                                dao.updateProgress(id, pct, System.currentTimeMillis())
                            }
                        }
                    }
                )
            ) {
                is UploadApi.UploadResult.Uploaded -> {
                    dao.markResult(
                        upload.id, VideoUploadStatus.PROCESSING, null,
                        result.memoryId, System.currentTimeMillis()
                    )
                    Log.i(TAG, "uploaded ${upload.id} (duplicate=${result.duplicate})")
                    result.memoryId?.let { memoryId ->
                        pollUntilReady(
                            memoryApi, upload.id, memoryId
                        ) { id, status, error, memId, now2 ->
                            dao.markResult(id, status, error, memId, now2)
                        }
                    }
                }
                is UploadApi.UploadResult.TooLarge -> {
                    dao.markResult(
                        upload.id, VideoUploadStatus.FAILED,
                        "video exceeds the ${UploadApi.formatBytes(UploadApi.MAX_UPLOAD_BYTES)} limit",
                        null, System.currentTimeMillis()
                    )
                }
                is UploadApi.UploadResult.Rejected -> {
                    dao.markResult(
                        upload.id, VideoUploadStatus.FAILED,
                        "${result.code}: ${result.message}",
                        null, System.currentTimeMillis()
                    )
                    Log.w(TAG, "upload ${upload.id} rejected: ${result.code}")
                }
                is UploadApi.UploadResult.Unauthenticated -> {
                    // Session is dead; stop this run, leave the row for later.
                    dao.markResult(
                        upload.id, VideoUploadStatus.PENDING,
                        "signed out — sign in to upload",
                        null, System.currentTimeMillis()
                    )
                    Log.w(TAG, "upload ${upload.id} unauthenticated; stopping run")
                    stopEarly = true
                    break
                }
                is UploadApi.UploadResult.QuotaExceeded -> {
                    // Park it: the paywall event was already emitted.
                    dao.markResult(
                        upload.id, VideoUploadStatus.QUOTA,
                        "Free quota exhausted (${result.info.bucket}: " +
                            "${result.info.used}/${result.info.limit}). Upgrade to Pro to upload.",
                        null, System.currentTimeMillis()
                    )
                    Log.w(TAG, "upload ${upload.id} quota-blocked; stopping run")
                    stopEarly = true
                    break
                }
                is UploadApi.UploadResult.Transient -> {
                    dao.markResult(
                        upload.id, VideoUploadStatus.FAILED, result.message,
                        null, System.currentTimeMillis()
                    )
                    transientFailures++
                    Log.w(TAG, "upload ${upload.id} transient: ${result.message}")
                }
            }
        }

        if (!stopEarly) {
            // Album/carousel uploads ride the same worker, constraints and
            // backoff: one multipart call per album, one quota unit each.
            transientFailures += processAlbumUploads(db.albumUploadDao(), albumApi, memoryApi)
                .also { stopEarly = it.first }
                .second
        }

        if (stopEarly) {
            return@coroutineScope Result.success()
        }
        if (transientFailures > 0) {
            Log.i(TAG, "$transientFailures transient failures; will retry with backoff")
            Result.retry()
        } else {
            Result.success()
        }
    }

    /**
     * Uploads pending album rows (one multipart POST /v1/captures/album per
     * album). Returns (stopEarly, transientFailures): stopEarly when the
     * session died or quota was exhausted — nothing more should be attempted
     * this run.
     */
    private suspend fun kotlinx.coroutines.CoroutineScope.processAlbumUploads(
        albumDao: AlbumUploadDao,
        albumApi: AlbumApi,
        memoryApi: MemoryApi
    ): Pair<Boolean, Int> {
        val pending = albumDao.pendingOrFailed()
        if (pending.isEmpty()) {
            Log.d(TAG, "no albums to upload")
            return false to 0
        }
        var transientFailures = 0
        for (album in pending) {
            val files = albumDao.getFiles(album.id)
            if (files.isEmpty()) {
                albumDao.markResult(
                    album.id, VideoUploadStatus.FAILED,
                    "album has no saved photos — re-share the post",
                    null, System.currentTimeMillis()
                )
                continue
            }
            val missing = files.firstOrNull { !File(it.localPath).exists() }
            if (missing != null) {
                albumDao.markResult(
                    album.id, VideoUploadStatus.FAILED,
                    "photo “${missing.fileName}” is gone locally — re-share the album",
                    null, System.currentTimeMillis()
                )
                continue
            }
            val oversize = files.firstOrNull {
                !UploadApi.isWithinSizeLimit(File(it.localPath).length())
            }
            if (oversize != null) {
                albumDao.markResult(
                    album.id, VideoUploadStatus.FAILED,
                    "photo “${oversize.fileName}” exceeds the " +
                        "${UploadApi.formatBytes(UploadApi.MAX_UPLOAD_BYTES)} per-file limit",
                    null, System.currentTimeMillis()
                )
                continue
            }
            val total = files.sumOf { File(it.localPath).length() }
            if (total > AlbumApi.MAX_ALBUM_BYTES) {
                albumDao.markResult(
                    album.id, VideoUploadStatus.FAILED,
                    "album is ${UploadApi.formatBytes(total)} — over the " +
                        "${UploadApi.formatBytes(AlbumApi.MAX_ALBUM_BYTES)} album limit",
                    null, System.currentTimeMillis()
                )
                continue
            }

            albumDao.markAttempt(album.id, VideoUploadStatus.UPLOADING, System.currentTimeMillis())
            val parts = files.map { AlbumPart(File(it.localPath), it.fileName, it.mimeType) }
            var lastReported = -1
            when (
                val result = albumApi.uploadAlbum(
                    parts = parts,
                    originalUrl = null,
                    onProgress = { sent, totalBytes ->
                        val pct = if (totalBytes > 0) {
                            ((sent * 100) / totalBytes).toInt().coerceIn(0, 100)
                        } else 0
                        if (pct != lastReported) {
                            lastReported = pct
                            val id = album.id
                            launch {
                                albumDao.updateProgress(id, pct, System.currentTimeMillis())
                            }
                        }
                    }
                )
            ) {
                is AlbumApi.AlbumResult.Uploaded -> {
                    albumDao.markResult(
                        album.id, VideoUploadStatus.PROCESSING, null,
                        result.memoryId, System.currentTimeMillis()
                    )
                    Log.i(
                        TAG,
                        "uploaded album ${album.id} (${result.fileCount} photos, " +
                            "duplicate=${result.duplicate})"
                    )
                    result.memoryId?.let { memoryId ->
                        pollUntilReady(
                            memoryApi, album.id, memoryId
                        ) { id, status, error, memId, now ->
                            albumDao.markResult(id, status, error, memId, now)
                        }
                    }
                }
                is AlbumApi.AlbumResult.TooLarge -> {
                    albumDao.markResult(
                        album.id, VideoUploadStatus.FAILED,
                        "album exceeds the ${UploadApi.formatBytes(AlbumApi.MAX_ALBUM_BYTES)} limit",
                        null, System.currentTimeMillis()
                    )
                }
                is AlbumApi.AlbumResult.Rejected -> {
                    albumDao.markResult(
                        album.id, VideoUploadStatus.FAILED,
                        "${result.code}: ${result.message}",
                        null, System.currentTimeMillis()
                    )
                    Log.w(TAG, "album ${album.id} rejected: ${result.code}")
                }
                is AlbumApi.AlbumResult.Unauthenticated -> {
                    albumDao.markResult(
                        album.id, VideoUploadStatus.PENDING,
                        "signed out — sign in to upload",
                        null, System.currentTimeMillis()
                    )
                    Log.w(TAG, "album ${album.id} unauthenticated; stopping run")
                    return true to transientFailures
                }
                is AlbumApi.AlbumResult.QuotaExceeded -> {
                    albumDao.markResult(
                        album.id, VideoUploadStatus.QUOTA,
                        "Free quota exhausted (${result.info.bucket}: " +
                            "${result.info.used}/${result.info.limit}). Upgrade to Pro to upload.",
                        null, System.currentTimeMillis()
                    )
                    Log.w(TAG, "album ${album.id} quota-blocked; stopping run")
                    return true to transientFailures
                }
                is AlbumApi.AlbumResult.Transient -> {
                    albumDao.markResult(
                        album.id, VideoUploadStatus.FAILED, result.message,
                        null, System.currentTimeMillis()
                    )
                    transientFailures++
                    Log.w(TAG, "album ${album.id} transient: ${result.message}")
                }
            }
        }
        return false to transientFailures
    }

    /**
     * Bounded readiness poll after a successful upload: a few checks spaced
     * out so quick jobs flip to READY without the user opening the app.
     * Long AI jobs stay PROCESSING; the Ask UI polls on open.
     *
     * `markResult` writes the outcome to whichever queue table owns the row
     * (single uploads or albums) — keeps one poll implementation.
     */
    private suspend fun pollUntilReady(
        memoryApi: MemoryApi,
        rowId: Long,
        memoryId: String,
        markResult: suspend (id: Long, status: String, error: String?, memoryId: String?, now: Long) -> Unit
    ) {
        repeat(POLL_ATTEMPTS) { attempt ->
            delay(POLL_INTERVAL_MS)
            when (val s = memoryApi.getStatus(memoryId)) {
                is MemoryApi.StatusResult.Ok -> {
                    if (s.status.isReady) {
                        markResult(
                            rowId, VideoUploadStatus.READY, null,
                            memoryId, System.currentTimeMillis()
                        )
                        Log.i(TAG, "row $rowId READY after ${attempt + 1} polls")
                        return
                    }
                    if (s.status.isTerminalFailure) {
                        markResult(
                            rowId, VideoUploadStatus.FAILED,
                            s.status.failureMessage
                                ?: "backend failed to process the media (${s.status.processingStatus})",
                            memoryId, System.currentTimeMillis()
                        )
                        return
                    }
                }
                is MemoryApi.StatusResult.NotFound -> {
                    markResult(
                        rowId, VideoUploadStatus.FAILED,
                        "memory disappeared on the backend", memoryId,
                        System.currentTimeMillis()
                    )
                    return
                }
                is MemoryApi.StatusResult.Unauthenticated -> {
                    Log.w(TAG, "status poll unauthenticated; leaving PROCESSING")
                    return
                }
                is MemoryApi.StatusResult.QuotaExceeded -> {
                    // Status polls don't consume quota; treat like transient.
                    Log.d(TAG, "status poll quota-blocked (unexpected): ${s.info.bucket}")
                }
                is MemoryApi.StatusResult.Transient -> {
                    Log.d(TAG, "status poll transient: ${s.message}")
                }
            }
        }
        Log.i(TAG, "row $rowId still processing after $POLL_ATTEMPTS polls; leaving PROCESSING")
    }

    companion object {
        private const val TAG = "UploadWorker"
        private const val UNIQUE_WORK = "reel-memory-upload"
        private const val POLL_ATTEMPTS = 6
        private const val POLL_INTERVAL_MS = 15_000L

        private val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()

        /** Enqueue an upload run, coalescing bursts into one. */
        fun enqueue(context: Context) {
            val request = OneTimeWorkRequestBuilder<UploadWorker>()
                .setConstraints(constraints)
                .setBackoffCriteria(
                    BackoffPolicy.EXPONENTIAL,
                    30,
                    TimeUnit.SECONDS
                )
                .addTag("upload")
                .build()
            WorkManager.getInstance(context.applicationContext)
                .enqueueUniqueWork(UNIQUE_WORK, ExistingWorkPolicy.APPEND_OR_REPLACE, request)
        }
    }
}
