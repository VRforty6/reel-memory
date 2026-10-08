package dev.reelmemory.app.sync

import dev.reelmemory.app.auth.QuotaExceededInfo
import dev.reelmemory.app.data.VideoUploadStatus
import dev.reelmemory.app.net.MemoryApi
import dev.reelmemory.app.net.MemoryStatus
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Decision logic for the Inbox "Processing…" re-check (A.2).
 *
 * Pure JVM tests: no Android, WorkManager, or database involved.
 */
class StatusCheckDecisionTest {

    private fun status(
        processingStatus: String,
        failureMessage: String? = null,
    ) = MemoryStatus(
        id = "mem-1",
        processingStatus = processingStatus,
        stage = null,
        attemptCount = 1,
        failureCode = null,
        failureMessage = failureMessage,
    )

    private fun resolve(decision: StatusCheckDecision): StatusCheckDecision.Resolve {
        assertTrue("expected Resolve, got $decision", decision is StatusCheckDecision.Resolve)
        return decision as StatusCheckDecision.Resolve
    }

    @Test
    fun `ready backend status resolves the row to READY`() {
        val d = resolve(
            decideStatusCheck(
                MemoryApi.StatusResult.Ok(status("READY")),
                memoryId = "mem-1",
            )
        )
        assertEquals(VideoUploadStatus.READY, d.localStatus)
        assertNull(d.error)
    }

    @Test
    fun `terminal failure with backend message resolves to FAILED with that message`() {
        val d = resolve(
            decideStatusCheck(
                MemoryApi.StatusResult.Ok(status("FAILED_PERMANENT", "quota gone")),
                memoryId = "mem-1",
            )
        )
        assertEquals(VideoUploadStatus.FAILED, d.localStatus)
        assertEquals("quota gone", d.error)
    }

    @Test
    fun `terminal failure without message uses fallback naming the backend status`() {
        val d = resolve(
            decideStatusCheck(
                MemoryApi.StatusResult.Ok(status("FAILED_PERMANENT")),
                memoryId = "mem-1",
            )
        )
        assertEquals(VideoUploadStatus.FAILED, d.localStatus)
        assertTrue(d.error!!.contains("FAILED_PERMANENT"))
    }

    @Test
    fun `all non-ready backend terminal states resolve to FAILED`() {
        for (s in listOf(
            "METADATA_ONLY",
            "SOURCE_UNAVAILABLE",
            "SOURCE_REQUIRES_ACCESS",
            "FAILED_PERMANENT",
            "DELETED",
        )) {
            val d = resolve(
                decideStatusCheck(
                    MemoryApi.StatusResult.Ok(status(s)),
                    memoryId = "mem-1",
                )
            )
            assertEquals("status $s", VideoUploadStatus.FAILED, d.localStatus)
            assertTrue("status $s", d.error!!.contains(s))
        }
    }

    @Test
    fun `retryable backend failure keeps PROCESSING`() {
        // FAILED_RETRYABLE is the backend's internal automatic retry — the
        // UI must keep waiting, not flip the card.
        val d = decideStatusCheck(
            MemoryApi.StatusResult.Ok(status("FAILED_RETRYABLE")),
            memoryId = "mem-1",
        )
        assertEquals(StatusCheckDecision.KeepProcessing, d)
    }

    @Test
    fun `mid-pipeline backend status keeps PROCESSING`() {
        for (s in listOf("QUEUED", "TRANSCRIBING", "ANALYZING_VISUALS")) {
            assertEquals(
                "status $s",
                StatusCheckDecision.KeepProcessing,
                decideStatusCheck(MemoryApi.StatusResult.Ok(status(s)), memoryId = "mem-1"),
            )
        }
    }

    @Test
    fun `not found resolves to FAILED`() {
        val d = resolve(
            decideStatusCheck(
                MemoryApi.StatusResult.NotFound("gone"),
                memoryId = "mem-1",
            )
        )
        assertEquals(VideoUploadStatus.FAILED, d.localStatus)
        assertTrue(d.error!!.contains("disappeared"))
    }

    @Test
    fun `transient errors keep PROCESSING`() {
        assertEquals(
            StatusCheckDecision.KeepProcessing,
            decideStatusCheck(
                MemoryApi.StatusResult.Transient("timeout"),
                memoryId = "mem-1",
            )
        )
    }

    @Test
    fun `unauthenticated keeps PROCESSING`() {
        assertEquals(
            StatusCheckDecision.KeepProcessing,
            decideStatusCheck(
                MemoryApi.StatusResult.Unauthenticated,
                memoryId = "mem-1",
            )
        )
    }

    @Test
    fun `quota exceeded keeps PROCESSING`() {
        val info = QuotaExceededInfo(
            tier = "free", bucket = "uploads", limit = 10, used = 10,
            resetsAt = "2026-10-01T00:00:00Z",
        )
        assertEquals(
            StatusCheckDecision.KeepProcessing,
            decideStatusCheck(
                MemoryApi.StatusResult.QuotaExceeded(info),
                memoryId = "mem-1",
            )
        )
    }

    @Test
    fun `missing memory id fails honestly without polling`() {
        val d = resolve(decideStatusCheck(result = null, memoryId = null))
        assertEquals(VideoUploadStatus.FAILED, d.localStatus)
        assertTrue(d.error!!.contains("re-share the video"))
    }

    @Test
    fun `missing memory id on an album names the album`() {
        val d = resolve(decideStatusCheck(result = null, memoryId = "", isAlbum = true))
        assertEquals(VideoUploadStatus.FAILED, d.localStatus)
        assertTrue(d.error!!.contains("re-share the album"))
    }
}
