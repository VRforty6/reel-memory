package dev.reelmemory.app.net

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import okio.Buffer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class UploadApiTest {

    @Test
    fun sizeLimit_boundaries() {
        assertFalse(UploadApi.isWithinSizeLimit(0))
        assertFalse(UploadApi.isWithinSizeLimit(-10))
        assertTrue(UploadApi.isWithinSizeLimit(1))
        assertTrue(UploadApi.isWithinSizeLimit(UploadApi.MAX_UPLOAD_BYTES))
        assertFalse(UploadApi.isWithinSizeLimit(UploadApi.MAX_UPLOAD_BYTES + 1))
        assertEquals(200L * 1024 * 1024, UploadApi.MAX_UPLOAD_BYTES)
    }

    @Test
    fun formatBytes_cases() {
        assertEquals("512 B", UploadApi.formatBytes(512))
        assertEquals("1.5 KB", UploadApi.formatBytes(1536))
        assertEquals("200.0 MB", UploadApi.formatBytes(200L * 1024 * 1024))
    }

    @Test
    fun buildMultipartBody_structure() {
        val tmp = File.createTempFile("reel", ".mp4")
        try {
            tmp.writeBytes(ByteArray(1024) { it.toByte() })
            val body = UploadApi.buildMultipartBody(
                file = tmp,
                fileName = "clip.mp4",
                mimeType = "video/mp4",
                originalUrl = "https://instagram.com/reel/abc123",
                onProgress = { _, _ -> }
            )
            assertEquals(2, body.parts.size)

            val filePart = body.parts[0]
            val disposition = filePart.headers?.get("Content-Disposition") ?: ""
            assertTrue(disposition.contains("name=\"file\""))
            assertTrue(disposition.contains("filename=\"clip.mp4\""))

            val urlPart = body.parts[1]
            val urlDisposition = urlPart.headers?.get("Content-Disposition") ?: ""
            assertTrue(urlDisposition.contains("name=\"original_url\""))

            // Multipart overhead means the body is bigger than the raw file.
            assertTrue(body.contentLength() > tmp.length())
        } finally {
            tmp.delete()
        }
    }

    @Test
    fun buildMultipartBody_noOriginalUrl_singlePart() {
        val tmp = File.createTempFile("reel", ".mp4")
        try {
            tmp.writeBytes(byteArrayOf(1, 2, 3))
            val body = UploadApi.buildMultipartBody(tmp, "c.mp4", "video/mp4", null)
            assertEquals(1, body.parts.size)
        } finally {
            tmp.delete()
        }
    }

    @Test
    fun parseUploadResult_accepted() {
        val payload = """{"id":"c1","memory_id":"m1","status":"QUEUED","duplicate":false}"""
        val r = UploadApi.parseUploadResult(202, payload)
        assertTrue(r is UploadApi.UploadResult.Uploaded)
        assertEquals("m1", (r as UploadApi.UploadResult.Uploaded).memoryId)
        assertEquals(false, r.duplicate)

        val dup = UploadApi.parseUploadResult(
            200, """{"id":"c1","memory_id":"m1","status":"READY","duplicate":true}"""
        ) as UploadApi.UploadResult.Uploaded
        assertEquals(true, dup.duplicate)
    }

    @Test
    fun parseUploadResult_tooLarge() {
        val r = UploadApi.parseUploadResult(413, "")
        assertTrue(r is UploadApi.UploadResult.TooLarge)
    }

    @Test
    fun parseUploadResult_rejected() {
        val r = UploadApi.parseUploadResult(
            422, """{"detail":{"code":"INVALID_FILE","message":"no file part"}}"""
        ) as UploadApi.UploadResult.Rejected
        assertEquals("INVALID_FILE", r.code)
        assertEquals("no file part", r.message)
    }

    @Test
    fun parseUploadResult_transient() {
        val r = UploadApi.parseUploadResult(500, "boom")
        assertTrue(r is UploadApi.UploadResult.Transient)
    }

    @Test
    fun progressRequestBody_reportsCumulativeBytes() {
        val seen = mutableListOf<Pair<Long, Long>>()
        val delegate = "hello world".toRequestBody("text/plain".toMediaType())
        val body = ProgressRequestBody(delegate) { sent, total -> seen.add(sent to total) }
        body.writeTo(Buffer())
        assertTrue(seen.isNotEmpty())
        val (sent, total) = seen.last()
        assertEquals(11L, sent)
        assertEquals(11L, total)
        // Monotonic non-decreasing progress.
        assertTrue(seen.zipWithNext().all { (a, b) -> b.first >= a.first })
    }
}
