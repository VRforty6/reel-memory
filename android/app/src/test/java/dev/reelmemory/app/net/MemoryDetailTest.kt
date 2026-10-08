package dev.reelmemory.app.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class MemoryDetailTest {

    private val fullDetailJson = """
        {
          "id": "m1",
          "title": "Sticky note trick",
          "summary": "A person writes with two pens.",
          "category": "productivity",
          "processing_status": "METADATA_ONLY",
          "media_kind": null,
          "source": {
            "platform": "INSTAGRAM",
            "canonical_url": "https://www.instagram.com/reel/ABC123/",
            "original_url": "https://www.instagram.com/reel/ABC123/?igsh=xyz",
            "creator_handle": "vrfortysix"
          }
        }
    """.trimIndent()

    @Test
    fun parseMemoryDetail_full() {
        val d = parseMemoryDetail(fullDetailJson)!!
        assertEquals("m1", d.id)
        assertEquals("Sticky note trick", d.title)
        assertEquals("METADATA_ONLY", d.processingStatus)
        assertEquals("INSTAGRAM", d.platform)
        assertEquals("https://www.instagram.com/reel/ABC123/", d.canonicalUrl)
        assertEquals("https://www.instagram.com/reel/ABC123/?igsh=xyz", d.originalUrl)
        assertEquals("vrfortysix", d.creatorHandle)
        // Best URL for "Open original": canonical first.
        assertEquals("https://www.instagram.com/reel/ABC123/", d.effectiveUrl)
    }

    @Test
    fun parseMemoryDetail_topLevelPlatformFallback() {
        val d = parseMemoryDetail(
            """{"id":"m2","processing_status":"FAILED","platform":"TIKTOK","source":{}}"""
        )!!
        assertEquals("TIKTOK", d.platform)
        assertNull(d.effectiveUrl)
    }

    @Test
    fun parseMemoryDetail_noSourceObject() {
        val d = parseMemoryDetail("""{"id":"m3","processing_status":"READY"}""")!!
        assertEquals("m3", d.id)
        assertNull(d.platform)
        assertNull(d.creatorHandle)
        assertNull(d.effectiveUrl)
    }

    @Test
    fun parseMemoryDetail_invalidJson() {
        assertNull(parseMemoryDetail("not json"))
    }

    @Test
    fun parseMemoryList_newFields() {
        val items = parseMemoryListResponse(
            """{"memories":[
              {"id":"a","processing_status":"READY","platform":"INSTAGRAM","media_kind":"video"},
              {"id":"b","processing_status":"METADATA_ONLY","platform":"TIKTOK","media_kind":"article"}
            ]}"""
        )
        assertEquals(2, items.size)
        assertEquals("INSTAGRAM", items[0].platform)
        assertEquals("video", items[0].mediaKind)
        assertEquals("TIKTOK", items[1].platform)
        assertEquals("article", items[1].mediaKind)
    }

    @Test
    fun parseMemoryList_oldPayloadNulls() {
        val items = parseMemoryListResponse(
            """{"memories":[{"id":"a","processing_status":"READY"}]}"""
        )
        assertEquals(1, items.size)
        assertNull(items[0].platform)
        assertNull(items[0].mediaKind)
        assertNull(items[0].hasThumbnail)
    }

    @Test
    fun parseMemoryDetail_hasThumbnail() {
        val withFlag = parseMemoryDetail(
            """{"id":"a","processing_status":"READY","has_thumbnail":false}"""
        )!!
        assertEquals(false, withFlag.hasThumbnail)
        val withoutFlag = parseMemoryDetail(
            """{"id":"b","processing_status":"READY"}"""
        )!!
        assertNull(withoutFlag.hasThumbnail)
    }
}
