package dev.reelmemory.app.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class AlbumApiTest {

    private fun img(name: String, size: Long = 1_000_000) =
        AlbumApi.Companion.AlbumFileInfo(name, "image/jpeg", size)

    @Test
    fun classifyAlbum_validImages() {
        val r = AlbumApi.classifyAlbum(listOf(img("a.jpg"), img("b.jpg"), img("c.png", 500_000)))
        assertTrue(r is AlbumApi.Companion.AlbumValidity.Valid)
    }

    @Test
    fun classifyAlbum_validOneVideoPlusImages() {
        val r = AlbumApi.classifyAlbum(
            listOf(
                img("a.jpg"),
                AlbumApi.Companion.AlbumFileInfo("clip.mp4", "video/mp4", 5_000_000),
                img("c.webp")
            )
        )
        assertTrue(r is AlbumApi.Companion.AlbumValidity.Valid)
    }

    @Test
    fun classifyAlbum_tooFew() {
        val r = AlbumApi.classifyAlbum(listOf(img("a.jpg")))
        assertTrue(r is AlbumApi.Companion.AlbumValidity.TooFew)
        assertEquals(1, (r as AlbumApi.Companion.AlbumValidity.TooFew).count)
        val empty = AlbumApi.classifyAlbum(emptyList())
        assertTrue(empty is AlbumApi.Companion.AlbumValidity.TooFew)
    }

    @Test
    fun classifyAlbum_tooMany() {
        val files = (1..31).map { img("p$it.jpg") }
        val r = AlbumApi.classifyAlbum(files)
        assertTrue(r is AlbumApi.Companion.AlbumValidity.TooMany)
        assertEquals(31, (r as AlbumApi.Companion.AlbumValidity.TooMany).count)
    }

    @Test
    fun classifyAlbum_maxAllowedIsValid() {
        val files = (1..30).map { img("p$it.jpg") }
        assertTrue(AlbumApi.classifyAlbum(files) is AlbumApi.Companion.AlbumValidity.Valid)
    }

    @Test
    fun classifyAlbum_tooManyVideos() {
        val r = AlbumApi.classifyAlbum(
            listOf(
                img("a.jpg"),
                AlbumApi.Companion.AlbumFileInfo("v1.mp4", "video/mp4", 1_000_000),
                AlbumApi.Companion.AlbumFileInfo("v2.mp4", "video/mp4", 1_000_000)
            )
        )
        assertTrue(r is AlbumApi.Companion.AlbumValidity.TooManyVideos)
    }

    @Test
    fun classifyAlbum_unsupportedType() {
        val r = AlbumApi.classifyAlbum(
            listOf(img("a.jpg"), AlbumApi.Companion.AlbumFileInfo("doc.pdf", "application/pdf", 100))
        )
        assertTrue(r is AlbumApi.Companion.AlbumValidity.UnsupportedType)
        assertEquals(
            "application/pdf",
            (r as AlbumApi.Companion.AlbumValidity.UnsupportedType).mimeType
        )
    }

    @Test
    fun classifyAlbum_perFileTooLarge() {
        val big = UploadApi.MAX_UPLOAD_BYTES + 1
        val r = AlbumApi.classifyAlbum(listOf(img("a.jpg"), img("big.jpg", big)))
        assertTrue(r is AlbumApi.Companion.AlbumValidity.PerFileTooLarge)
        assertEquals("big.jpg", (r as AlbumApi.Companion.AlbumValidity.PerFileTooLarge).fileName)
    }

    @Test
    fun classifyAlbum_tooLargeTotal() {
        // 3 files x 200 MB each = 600 MB > 400 MB album cap.
        val files = listOf(
            img("a.jpg", 200L * 1024 * 1024),
            img("b.jpg", 200L * 1024 * 1024),
            img("c.jpg", 200L * 1024 * 1024)
        )
        val r = AlbumApi.classifyAlbum(files)
        assertTrue(r is AlbumApi.Companion.AlbumValidity.TooLargeTotal)
    }

    @Test
    fun classifyAlbum_mimeWithParameters() {
        val r = AlbumApi.classifyAlbum(
            listOf(
                AlbumApi.Companion.AlbumFileInfo("a.jpg", "image/jpeg; charset=binary", 100),
                AlbumApi.Companion.AlbumFileInfo("b.jpg", "IMAGE/PNG", 100)
            )
        )
        assertTrue(r is AlbumApi.Companion.AlbumValidity.Valid)
    }

    @Test
    fun constants_matchBackend() {
        assertEquals(2, AlbumApi.MIN_ALBUM_FILES)
        assertEquals(30, AlbumApi.MAX_ALBUM_FILES)
        assertEquals(400L * 1024 * 1024, AlbumApi.MAX_ALBUM_BYTES)
    }

    @Test
    fun buildAlbumMultipartBody_structureAndOrder() {
        val files = (1..3).map { n ->
            val tmp = File.createTempFile("photo$n", ".jpg")
            tmp.writeBytes(ByteArray(512) { it.toByte() })
            AlbumPart(tmp, "photo-$n.jpg", "image/jpeg")
        }
        try {
            val body = AlbumApi.buildAlbumMultipartBody(files, null)
            assertEquals(3, body.parts.size)
            body.parts.forEachIndexed { i, part ->
                val disposition = part.headers?.get("Content-Disposition") ?: ""
                assertTrue("part $i must use the files field", disposition.contains("name=\"files\""))
                assertTrue(
                    "part $i must keep share order",
                    disposition.contains("filename=\"photo-${i + 1}.jpg\"")
                )
            }
            // Multipart overhead means the body is bigger than the raw files.
            assertTrue(body.contentLength() > 3 * 512)
        } finally {
            files.forEach { it.file.delete() }
        }
    }

    @Test
    fun buildAlbumMultipartBody_includesOriginalUrl() {
        val tmp = File.createTempFile("photo", ".jpg")
        try {
            tmp.writeBytes(byteArrayOf(1, 2, 3))
            val body = AlbumApi.buildAlbumMultipartBody(
                listOf(AlbumPart(tmp, "a.jpg", "image/jpeg")),
                "https://instagram.com/p/xyz",
                { _, _ -> }
            )
            assertEquals(2, body.parts.size)
            val urlDisposition =
                body.parts[1].headers?.get("Content-Disposition") ?: ""
            assertTrue(urlDisposition.contains("name=\"original_url\""))
        } finally {
            tmp.delete()
        }
    }

    @Test
    fun parseAlbumResult_accepted() {
        val payload = """
            {"id":"c1","memory_id":"m9","status":"QUEUED","duplicate":false,
             "file_count":3,"content_hashes":["h1","h2","h3"]}
        """.trimIndent()
        val r = AlbumApi.parseAlbumResult(202, payload)
        assertTrue(r is AlbumApi.AlbumResult.Uploaded)
        r as AlbumApi.AlbumResult.Uploaded
        assertEquals("m9", r.memoryId)
        assertEquals(false, r.duplicate)
        assertEquals(3, r.fileCount)
        assertEquals(listOf("h1", "h2", "h3"), r.contentHashes)
    }

    @Test
    fun parseAlbumResult_duplicate() {
        val payload = """
            {"id":"c1","memory_id":"m9","status":"READY","duplicate":true,
             "file_count":2,"content_hashes":["h1","h2"]}
        """.trimIndent()
        val r = AlbumApi.parseAlbumResult(200, payload)
        assertTrue(r is AlbumApi.AlbumResult.Uploaded)
        assertEquals(true, (r as AlbumApi.AlbumResult.Uploaded).duplicate)
    }

    @Test
    fun parseAlbumResult_unauthenticated() {
        val r = AlbumApi.parseAlbumResult(401, "Unauthorized")
        assertTrue(r is AlbumApi.AlbumResult.Unauthenticated)
    }

    @Test
    fun parseAlbumResult_quotaExceeded() {
        val payload = """
            {"code":"quota_exceeded","tier":"free","bucket":"captures",
             "limit":20,"used":20,"resets_at":"2026-10-01T00:00:00Z"}
        """.trimIndent()
        val r = AlbumApi.parseAlbumResult(402, payload)
        assertTrue(r is AlbumApi.AlbumResult.QuotaExceeded)
        val info = (r as AlbumApi.AlbumResult.QuotaExceeded).info
        assertEquals("captures", info.bucket)
        assertEquals(20, info.limit)
    }

    @Test
    fun parseAlbumResult_rejectedWithCode() {
        val payload = """
            {"detail":{"code":"ALBUM_TOO_MANY_VIDEOS",
                       "message":"an album may contain at most one video"}}
        """.trimIndent()
        val r = AlbumApi.parseAlbumResult(422, payload)
        assertTrue(r is AlbumApi.AlbumResult.Rejected)
        r as AlbumApi.AlbumResult.Rejected
        assertEquals("ALBUM_TOO_MANY_VIDEOS", r.code)
        assertTrue(r.message.contains("at most one video"))
    }

    @Test
    fun parseAlbumResult_tooLarge() {
        val r = AlbumApi.parseAlbumResult(413, "album too large")
        assertTrue(r is AlbumApi.AlbumResult.TooLarge)
    }

    @Test
    fun parseAlbumResult_serverErrorIsTransient() {
        val r = AlbumApi.parseAlbumResult(500, "boom")
        assertTrue(r is AlbumApi.AlbumResult.Transient)
    }
}
