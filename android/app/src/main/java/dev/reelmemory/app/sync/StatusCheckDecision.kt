package dev.reelmemory.app.sync

import dev.reelmemory.app.data.VideoUploadStatus
import dev.reelmemory.app.net.MemoryApi

/**
 * Pure decision logic for status re-checks.
 *
 * JVM-testable: no Android, WorkManager, or database dependencies. The
 * [StatusCheckWorker] applies the decision to the local row; this function
 * only decides what the decision is.
 */
sealed interface StatusCheckDecision {
    /** The row reached a terminal outcome: write [localStatus]/[error]. */
    data class Resolve(val localStatus: String, val error: String?) : StatusCheckDecision

    /** Still working or the check was inconclusive: keep PROCESSING. */
    data object KeepProcessing : StatusCheckDecision
}

/**
 * Maps one backend status poll to a local-row decision.
 *
 * @param result what GET /v1/memories/{id}/status returned, or null when the
 *               local row has no backend memory id (nothing to poll)
 * @param memoryId the backend memory id, or null/blank when the row never
 *                 got one (can never resolve — fail honestly)
 * @param isAlbum picks the album wording for the no-memory-id error
 */
fun decideStatusCheck(
    result: MemoryApi.StatusResult?,
    memoryId: String?,
    isAlbum: Boolean = false,
): StatusCheckDecision {
    if (memoryId.isNullOrBlank()) {
        return StatusCheckDecision.Resolve(
            VideoUploadStatus.FAILED,
            if (isAlbum) "upload has no backend memory id — re-share the album"
            else "upload has no backend memory id — re-share the video",
        )
    }
    return when (val r = requireNotNull(result)) {
        is MemoryApi.StatusResult.Ok -> when {
            r.status.isReady ->
                StatusCheckDecision.Resolve(VideoUploadStatus.READY, null)

            r.status.isTerminalFailure ->
                StatusCheckDecision.Resolve(
                    VideoUploadStatus.FAILED,
                    r.status.failureMessage
                        ?: "backend failed to process the media (${r.status.processingStatus})",
                )

            else -> StatusCheckDecision.KeepProcessing
        }

        is MemoryApi.StatusResult.NotFound ->
            StatusCheckDecision.Resolve(
                VideoUploadStatus.FAILED,
                "memory disappeared on the backend",
            )

        // Transient / Unauthenticated / QuotaExceeded: say nothing, keep
        // PROCESSING; the next check (or the next Inbox open) retries.
        else -> StatusCheckDecision.KeepProcessing
    }
}
