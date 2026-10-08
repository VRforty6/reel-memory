package dev.reelmemory.app

import org.junit.Assert.*
import org.junit.Test

/**
 * JVM unit tests for URL canonicalization. These mirror the backend's
 * `test_canonicalize.py` contract — the two implementations must agree.
 */
class UrlCanonicalizerTest {

    @Test
    fun `reel URL canonicalizes to bare form`() {
        val result = UrlCanonicalizer.canonicalize(
            "https://www.instagram.com/reel/DI6vK9mPxQz/?igsh=abc123&utm_source=ig"
        ) as UrlCanonicalizer.Result.Ok
        assertEquals("instagram", result.platform)
        assertEquals("DI6vK9mPxQz", result.platformItemId)
        assertEquals("https://instagram.com/reel/DI6vK9mPxQz", result.canonicalUrl)
    }

    @Test
    fun `reels plural and p paths accepted`() {
        for (path in listOf("reels", "p")) {
            val result = UrlCanonicalizer.canonicalize(
                "https://instagram.com/$path/AbC_12-xy/"
            ) as UrlCanonicalizer.Result.Ok
            assertEquals("AbC_12-xy", result.platformItemId)
            assertEquals("https://instagram.com/reel/AbC_12-xy", result.canonicalUrl)
        }
    }

    @Test
    fun `mobile host accepted`() {
        val result = UrlCanonicalizer.canonicalize("https://m.instagram.com/reel/abcDEF12")
        assertTrue(result is UrlCanonicalizer.Result.Ok)
    }

    @Test
    fun `unsupported host rejected with UNSUPPORTED_SOURCE`() {
        val result = UrlCanonicalizer.canonicalize("https://www.tiktok.com/@user/video/123")
            as UrlCanonicalizer.Result.Err
        assertEquals("UNSUPPORTED_SOURCE", result.code)
    }

    @Test
    fun `non-reel path rejected with INVALID_URL`() {
        val result = UrlCanonicalizer.canonicalize("https://instagram.com/explore/")
            as UrlCanonicalizer.Result.Err
        assertEquals("INVALID_URL", result.code)
    }

    @Test
    fun `empty URL rejected`() {
        val result = UrlCanonicalizer.canonicalize("  ") as UrlCanonicalizer.Result.Err
        assertEquals("INVALID_URL", result.code)
    }

    @Test
    fun `extractor finds instagram link in share text`() {
        val urls = UrlExtractor.extract(
            "Check this out https://www.instagram.com/reel/DI6vK9mPxQz/?igsh=abc 🔥"
        )
        assertEquals(
            listOf("https://www.instagram.com/reel/DI6vK9mPxQz/?igsh=abc"),
            urls
        )
    }

    @Test
    fun `extractor returns empty for text without urls`() {
        assertTrue(UrlExtractor.extract("just some words").isEmpty())
        assertTrue(UrlExtractor.extract(null).isEmpty())
    }
}
