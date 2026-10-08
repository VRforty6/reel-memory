package dev.reelmemory.app.sync

import android.content.Context
import android.util.Log
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
import dev.reelmemory.app.data.VideoUploadDao
import dev.reelmemory.app.net.MemoryApi

/**
 * Re-polls the backend for uploads stuck in PROCESSING.
 *
 * UploadWorker only polls GET /v1/memories/{id}/status for ~90s after an
 * upload; a backend job that takes longer (or that was orphaned by a dead
 * worker and later recovered server-side) used to leave the Inbox card on
 * "Processing…" forever — nothing ever asked the backend again.
 *
 * This worker asks once per PROCESSING row (single uploads and albums) and
 * flips the local row to READY/FAILED when the backend has moved on. Rows
 * still processing are left alone. It never uploads anything.
 */
class StatusCheckWorker(
    appContext: Context,
    params: WorkerParameters
) : CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result {
        val db = ShareDatabase.get(applicationContext)
        val settings = SettingsStore(applicationContext)
        val session = SessionManager.get(applicationContext)
        if (!session.hasSession()) {
            Log.i(TAG, "signed out; skipping status check")
            return Result.success()
        }
        val memoryApi = MemoryApi(settings, session)
        val now = System.currentTimeMillis()

        var checked = 0
        for (upload in db.videoUploadDao().processing()) {
            if (checkRow(memoryApi, db.videoUploadDao(), upload.id, upload.remoteMemoryId, now)) {
                checked++
            }
        }
        for (album in db.albumUploadDao().processing()) {
            if (checkRow(memoryApi, db.albumUploadDao(), album.id, album.remoteMemoryId, now)) {
                checked++
            }
        }
        Log.i(TAG, "status check done: $checked row(s) resolved")
        return Result.success()
    }

    /**
     * Polls one row's backend memory status. Returns true when the row
     * reached a terminal outcome (local DB updated); false when it is still
     * processing or the check was inconclusive (transient network, signed
     * out mid-run) — those rows stay PROCESSING for the next check.
     * Never uploads anything.
     */
    private suspend fun checkRow(
        memoryApi: MemoryApi,
        dao: VideoUploadDao,
        rowId: Long,
        memoryId: String?,
        now: Long
    ): Boolean {
        val result = if (!memoryId.isNullOrBlank()) memoryApi.getStatus(memoryId) else null
        return when (val d = decideStatusCheck(result, memoryId)) {
            is StatusCheckDecision.Resolve -> {
                dao.markResult(rowId, d.localStatus, d.error, memoryId, now)
                true
            }
            StatusCheckDecision.KeepProcessing -> false
        }
    }

    private suspend fun checkRow(
        memoryApi: MemoryApi,
        dao: AlbumUploadDao,
        rowId: Long,
        memoryId: String?,
        now: Long
    ): Boolean {
        val result = if (!memoryId.isNullOrBlank()) memoryApi.getStatus(memoryId) else null
        return when (val d = decideStatusCheck(result, memoryId, isAlbum = true)) {
            is StatusCheckDecision.Resolve -> {
                dao.markResult(rowId, d.localStatus, d.error, memoryId, now)
                true
            }
            StatusCheckDecision.KeepProcessing -> false
        }
    }

    companion object {
        private const val TAG = "StatusCheckWorker"
        private const val UNIQUE_WORK = "reel-memory-status-check"

        private val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()

        /**
         * Enqueue a one-off status re-check. Coalesced: an already-running
         * or enqueued check is kept instead of stacking duplicates.
         */
        fun enqueue(context: Context) {
            val request = OneTimeWorkRequestBuilder<StatusCheckWorker>()
                .setConstraints(constraints)
                .addTag("status-check")
                .build()
            WorkManager.getInstance(context.applicationContext)
                .enqueueUniqueWork(UNIQUE_WORK, ExistingWorkPolicy.KEEP, request)
        }
    }
}
