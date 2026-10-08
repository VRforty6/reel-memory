package dev.reelmemory.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ShareIntakeTest {

    @Test
    fun `instagram reel urls classify as instagram`() {
        val result = ShareIntake.classifyUrls(
            listOf("https://www.instagram.com/reel/DI6vK9mPxQz/?igsh=abc123")
        )
        assertEquals(1, result.instagramUrls.size)
        assertEquals("instagram", result.instagramUrls[0].platform)
        assertTrue(result.webUrls.isEmpty())
    }

    @Test
    fun `plain web urls classify as web captures`() {
        val result = ShareIntake.classifyUrls(
            listOf("https://example.com/articles/how-to-make-sourdough")
        )
        assertTrue(result.instagramUrls.isEmpty())
        assertEquals(
            listOf("https://example.com/articles/how-to-make-sourdough"),
            result.webUrls
        )
    }

    @Test
    fun `mixed text urls split into both buckets`() {
        val result = ShareIntake.classifyUrls(
            listOf(
                "https://instagram.com/reel/AbC_12-xy/",
                "https://www.nytimes.com/2026/09/21/tech/ai.html",
                "not-a-url"
            )
        )
        assertEquals(1, result.instagramUrls.size)
        assertEquals(1, result.webUrls.size)
    }

    @Test
    fun `non http urls are dropped`() {
        val result = ShareIntake.classifyUrls(listOf("ftp://files.example.com/x", "hello world"))
        assertTrue(result.isEmpty)
    }

    @Test
    fun `isWebUrl requires http or https with a host`() {
        assertTrue(ShareIntake.isWebUrl("https://example.com/a"))
        assertTrue(ShareIntake.isWebUrl("http://example.com/a"))
        assertTrue(!ShareIntake.isWebUrl("ftp://example.com/a"))
        assertTrue(!ShareIntake.isWebUrl("https:///no-host"))
        assertTrue(!ShareIntake.isWebUrl("just words"))
    }
}
