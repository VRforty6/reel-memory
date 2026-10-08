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
import dev.reelmemory.app.data.SettingsStore
import dev.reelmemory.app.data.ShareDatabase
import dev.reelmemory.app.data.ShareStatus
import dev.reelmemory.app.data.WebCaptureStatus
import dev.reelmemory.app.net.CaptureApi
import java.util.concurrent.TimeUnit

/**
 * Uploads queued shares to the backend's POST /v1/captures endpoint and
 * queued web URLs to POST /v1/captures/url.
 *
 * - Only runs with network connectivity.
 * - Requires a signed-in session: shares captured while signed out stay
 *   PENDING and sync after sign-in (the app enqueues a sync on sign-in).
 * - Exponential backoff via WorkManager (10s base) plus manual backoff for
 *   transient failures: the work retries while anything is PENDING/FAILED.
 * - A 402 (free quota exhausted) parks the item with status QUOTA and the
 *   session emits the paywall event — the item is NOT retried blindly.
 *   [retryQuotaBlocked] un-parks them after an upgrade or on manual retry.
 * - A share stays in the local DB forever as the audit trail; SYNCED means
 *   the backend durably accepted it (its own pipeline takes over from there).
 */
class SyncWorker(
    appContext: Context,
    params: WorkerParameters
) : CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result {
        val db = ShareDatabase.get(applicationContext)
        val dao = db.shareDao()
        val session = SessionManager.get(applicationContext)
        if (!session.hasSession()) {
            Log.i(TAG, "signed out; leaving ${dao.pendingCount()} shares queued until sign-in")
            return Result.success()
        }
        val api = CaptureApi(SettingsStore(applicationContext), session)

        val pending = dao.pendingOrFailed()

        var transientFailures = 0
        var stoppedEarly = false
        for (share in pending) {
            if (stoppedEarly) break
            dao.markAttempt(share.id, ShareStatus.SYNCING)
            when (val result = api.postCapture(share.originalUrl)) {
                is CaptureApi.SyncResult.Accepted -> {
                    dao.markResult(
                        share.id,
                        ShareStatus.SYNCED,
                        error = null,
                        syncedAt = System.currentTimeMillis(),
                        remoteMemoryId = result.memoryId
                    )
                    Log.i(TAG, "synced share ${share.id} (duplicate=${result.duplicate})")
                }
                is CaptureApi.SyncResult.Rejected -> {
                    // 4xx: the backend will never accept this payload; park it
                    // as FAILED with the reason instead of retrying forever.
                    dao.markResult(
                        share.id,
                        ShareStatus.FAILED,
                        error = "${result.code}: ${result.message}",
                        syncedAt = null,
                        remoteMemoryId = null
                    )
                    Log.w(TAG, "share ${share.id} rejected: ${result.code}")
                }
                is CaptureApi.SyncResult.Unauthenticated -> {
                    // Session is dead (the session already routed to sign-in);
                    // stop this run; the row stays PENDING/SYNCING for later.
                    dao.markResult(
                        share.id,
                        ShareStatus.PENDING,
                        error = "signed out — sign in to sync",
                        syncedAt = null,
                        remoteMemoryId = null
                    )
                    stoppedEarly = true
                }
                is CaptureApi.SyncResult.QuotaExceeded -> {
                    // Park it: retrying won't help until the quota resets or
                    // the user upgrades. The paywall event was already emitted.
                    dao.markResult(
                        share.id,
                        ShareStatus.QUOTA,
                        error = "Free quota exhausted (${result.info.bucket}: " +
                            "${result.info.used}/${result.info.limit}). Upgrade to Pro to sync.",
                        syncedAt = null,
                        remoteMemoryId = null
                    )
                    Log.w(TAG, "share ${share.id} quota-blocked: ${result.info.bucket}")
                    stoppedEarly = true
                }
                is CaptureApi.SyncResult.Transient -> {
                    dao.markResult(
                        share.id,
                        ShareStatus.FAILED,
                        error = result.message,
                        syncedAt = null,
                        remoteMemoryId = null
                    )
                    transientFailures++
                    Log.w(TAG, "share ${share.id} transient failure: ${result.message}")
                }
            }
        }

        return if (transientFailures + syncWebCaptures(session) > 0) {
            Log.i(TAG, "$transientFailures transient failures; will retry with backoff")
            Result.retry()
        } else {
            Result.success()
        }
    }

    /**
     * Syncs pending plain-web URLs to POST /v1/captures/url.
     * Returns the number of transient failures (to fold into the retry decision).
     */
    private suspend fun syncWebCaptures(session: SessionManager): Int {
        val db = ShareDatabase.get(applicationContext)
        val webDao = db.webCaptureDao()
        val api = CaptureApi(SettingsStore(applicationContext), session)
        var transientFailures = 0

        val pending = webDao.pendingByStatus(WebCaptureStatus.PENDING) +
            webDao.pendingByStatus(WebCaptureStatus.FAILED)
        for (capture in pending) {
            webDao.updateAfterAttempt(
                capture.id, WebCaptureStatus.SYNCING, null, capture.remoteMemoryId,
                System.currentTimeMillis()
            )
            var stopped = false
            when (val result = api.postUrlCapture(capture.url)) {
                is CaptureApi.SyncResult.Accepted -> {
                    webDao.updateAfterAttempt(
                        capture.id, WebCaptureStatus.SYNCED, null, result.memoryId,
                        System.currentTimeMillis()
                    )
                    Log.i(TAG, "synced web capture ${capture.id} (duplicate=${result.duplicate})")
                }
                is CaptureApi.SyncResult.Rejected -> {
                    webDao.updateAfterAttempt(
                        capture.id, WebCaptureStatus.FAILED,
                        "${result.code}: ${result.message}", null, System.currentTimeMillis()
                    )
                    Log.w(TAG, "web capture ${capture.id} rejected: ${result.code}")
                }
                is CaptureApi.SyncResult.Unauthenticated -> {
                    webDao.updateAfterAttempt(
                        capture.id, WebCaptureStatus.PENDING,
                        "signed out — sign in to sync", null, System.currentTimeMillis()
                    )
                    stopped = true
                }
                is CaptureApi.SyncResult.QuotaExceeded -> {
                    webDao.updateAfterAttempt(
                        capture.id, WebCaptureStatus.QUOTA,
                        "Free quota exhausted (${result.info.bucket}: " +
                            "${result.info.used}/${result.info.limit}). Upgrade to Pro to sync.",
                        null, System.currentTimeMillis()
                    )
                    Log.w(TAG, "web capture ${capture.id} quota-blocked")
                    stopped = true
                }
                is CaptureApi.SyncResult.Transient -> {
                    webDao.updateAfterAttempt(
                        capture.id, WebCaptureStatus.FAILED, result.message, null,
                        System.currentTimeMillis()
                    )
                    transientFailures++
                    Log.w(TAG, "web capture ${capture.id} transient failure: ${result.message}")
                }
            }
            if (stopped) break
        }
        return transientFailures
    }

    companion object {
        private const val TAG = "SyncWorker"
        private const val UNIQUE_WORK = "reel-memory-sync"

        private val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()

        /** Enqueue a sync run, coalescing rapid-fire share bursts into one. */
        fun enqueue(context: Context) {
            val request = OneTimeWorkRequestBuilder<SyncWorker>()
                .setConstraints(constraints)
                .setBackoffCriteria(
                    BackoffPolicy.EXPONENTIAL,
                    10,
                    TimeUnit.SECONDS
                )
                .addTag("sync")
                .build()
            WorkManager.getInstance(context.applicationContext)
                .enqueueUniqueWork(UNIQUE_WORK, ExistingWorkPolicy.APPEND_OR_REPLACE, request)
        }

        /**
         * Un-parks quota-blocked rows (QUOTA -> PENDING) across all queues and
         * starts a sync. Call after a Pro purchase or when the user taps
         * "retry" on the paywall.
         */
        suspend fun retryQuotaBlocked(context: Context) {
            val db = ShareDatabase.get(context.applicationContext)
            db.shareDao().requeueQuotaBlocked()
            db.webCaptureDao().requeueQuotaBlocked()
            db.videoUploadDao().requeueQuotaBlocked()
            enqueue(context)
            UploadWorker.enqueue(context)
        }

        /**
         * Bounded safety net for pathological retry loops: WorkManager's own
         * exponential backoff already caps this, but we stop marking attempts
         * beyond this count so the queue UI can show "needs attention".
         */
        const val MAX_ATTEMPTS_BEFORE_ATTENTION = 25
    }
}
