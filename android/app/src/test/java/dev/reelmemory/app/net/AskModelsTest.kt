package dev.reelmemory.app.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class AskModelsTest {

    private val fullAskJson = """
        {
          "answer": "The reel shows a carbonara recipe using guanciale.",
          "evidence": [
            {"modality": "SPEECH", "start_ms": 4000, "end_ms": 9000,
             "excerpt": "first crisp the guanciale"},
            {"modality": "OCR", "start_ms": null, "end_ms": null,
             "excerpt": "200g spaghetti"}
          ],
          "verification": {
            "status": "supported",
            "findings": [
              {"claim": "Carbonara traditionally uses guanciale",
               "verdict": "supported",
               "sources": [{"title": "Authentic carbonara", "url": "https://example.com/carbonara"}]},
              {"claim": "Carbonara contains cream",
               "verdict": "contradicted",
               "sources": [{"title": "No cream", "url": "https://example.com/no-cream"}]}
            ]
          }
        }
    """.trimIndent()

    @Test
    fun parseAskResponse_full() {
        val r = parseAskResponse(fullAskJson)!!
        assertEquals("The reel shows a carbonara recipe using guanciale.", r.answer)
        assertEquals(2, r.evidence.size)
        assertEquals("SPEECH", r.evidence[0].modality)
        assertEquals(4000L, r.evidence[0].startMs)
        assertEquals("0:04–0:09", r.evidence[0].timestampLabel())
        assertEquals("OCR", r.evidence[1].modality)
        assertNull(r.evidence[1].startMs)
        assertEquals("--", r.evidence[1].timestampLabel())

        val v = r.verification!!
        assertEquals(Verdict.SUPPORTED, v.status)
        assertEquals(2, v.findings.size)
        assertEquals(Verdict.SUPPORTED, v.findings[0].verdict)
        assertEquals("Authentic carbonara", v.findings[0].sources[0].title)
        assertEquals("https://example.com/carbonara", v.findings[0].sources[0].url)
        assertEquals(Verdict.CONTRADICTED, v.findings[1].verdict)
    }

    @Test
    fun parseAskResponse_noVerification() {
        val r = parseAskResponse("""{"answer":"hi","evidence":[]}""")!!
        assertEquals("hi", r.answer)
        assertTrue(r.evidence.isEmpty())
        assertNull(r.verification)
    }

    @Test
    fun parseAskResponse_nullVerification() {
        val r = parseAskResponse("""{"answer":"hi","evidence":[],"verification":null}""")!!
        assertNull(r.verification)
    }

    @Test
    fun parseAskResponse_unknownVerdict() {
        val r = parseAskResponse(
            """{"answer":"x","evidence":[],
                "verification":{"status":"bogus","findings":[
                  {"claim":"c","verdict":"weird","sources":[]}]}}"""
        )!!
        val v = r.verification!!
        assertEquals(Verdict.UNKNOWN, v.status)
        assertEquals(Verdict.UNKNOWN, v.findings[0].verdict)
    }

    @Test
    fun parseAskResponse_malformed_returnsNull() {
        assertNull(parseAskResponse("not json"))
        assertNull(parseAskResponse(""))
    }

    @Test
    fun parseAskResponse_missingEvidence_defaultsEmpty() {
        val r = parseAskResponse("""{"answer":"just text"}""")!!
        assertEquals("just text", r.answer)
        assertTrue(r.evidence.isEmpty())
    }

    @Test
    fun parseMemoryListResponse_basic() {
        val json = """
            {"memories":[
              {"id":"m1","title":"Carbonara","summary":"pasta","category":"Recipes & Cooking",
               "category_path":"food-drink/cooking/recipes-cooking",
               "processing_status":"READY","created_at":"2026-09-22T01:00:00Z"},
              {"id":"m2","title":null,"summary":null,"category":null,
               "processing_status":"PROCESSING","created_at":null}
            ],"limit":50,"offset":0}
        """.trimIndent()
        val list = parseMemoryListResponse(json)
        assertEquals(2, list.size)
        assertEquals("m1", list[0].id)
        assertEquals("Carbonara", list[0].displayTitle)
        assertEquals("food-drink/cooking/recipes-cooking", list[0].categoryPath)
        assertTrue(list[0].isReady)
        assertEquals("Untitled reel", list[1].displayTitle)
        assertTrue(!list[1].isReady)
    }

    @Test
    fun parseMemoryListResponse_malformed_empty() {
        assertTrue(parseMemoryListResponse("nope").isEmpty())
    }

    @Test
    fun parseMemoryStatusResponse_basic() {
        val s = parseMemoryStatusResponse(
            """{"id":"m1","processing_status":"READY","stage":"done",
                "attempt_count":1,"failure_code":null,"failure_message":null}"""
        )!!
        assertEquals("m1", s.id)
        assertTrue(s.isReady)
        assertTrue(!s.isTerminalFailure)
        assertEquals(1, s.attemptCount)
    }

    @Test
    fun parseMemoryStatusResponse_failed() {
        val s = parseMemoryStatusResponse(
            """{"id":"m2","processing_status":"FAILED_PERMANENT","stage":null,
                "attempt_count":3,"failure_code":"NO_MEDIA","failure_message":"no bytes"}"""
        )!!
        assertTrue(!s.isReady)
        assertTrue(s.isTerminalFailure)
        assertEquals("NO_MEDIA", s.failureCode)
        assertNull(s.mediaKind)
        assertNull(s.platform)
    }

    @Test
    fun parseMemoryStatusResponse_autoRetryable() {
        // FAILED_RETRYABLE is the backend's internal retry: not terminal —
        // pollers keep waiting, the UI shows "Retrying automatically…",
        // and no manual Try Again is offered.
        val s = parseMemoryStatusResponse(
            """{"id":"m3","processing_status":"FAILED_RETRYABLE","stage":null,
                "attempt_count":2,"failure_code":null,"failure_message":null,
                "media_kind":"video","platform":"INSTAGRAM"}"""
        )!!
        assertTrue(!s.isReady)
        assertFalse(s.isTerminalFailure)
        assertEquals("video", s.mediaKind)
        assertEquals("INSTAGRAM", s.platform)
    }

    @Test
    fun everyNonReadyTerminalBackendState_isTerminalFailure() {
        val terminal = listOf(
            "METADATA_ONLY",
            "SOURCE_UNAVAILABLE",
            "SOURCE_REQUIRES_ACCESS",
            "FAILED_PERMANENT",
            "DELETED",
        )

        for (processingStatus in terminal) {
            val s = MemoryStatus(
                id = "m-$processingStatus",
                processingStatus = processingStatus,
                stage = null,
                attemptCount = 1,
                failureCode = null,
                failureMessage = null,
            )
            assertTrue("status $processingStatus", s.isTerminalFailure)
        }
    }

    @Test
    fun parseMemoryListResponse_hasThumbnail() {
        val list = parseMemoryListResponse(
            """{"memories":[
              {"id":"a","processing_status":"READY","has_thumbnail":true},
              {"id":"b","processing_status":"READY","has_thumbnail":false},
              {"id":"c","processing_status":"READY"}
            ]}"""
        )
        assertEquals(3, list.size)
        assertEquals(true, list[0].hasThumbnail)
        assertEquals(false, list[1].hasThumbnail)
        assertNull(list[2].hasThumbnail)
    }

    @Test
    fun formatTimestamp_cases() {
        assertEquals("0:00", formatTimestamp(0))
        assertEquals("0:12", formatTimestamp(12_000))
        assertEquals("1:05", formatTimestamp(65_000))
        assertEquals("1:02:03", formatTimestamp(3_723_000))
        assertEquals("--", formatTimestamp(null))
        assertEquals("--", formatTimestamp(-5))
    }

    @Test
    fun parseEvidenceChip_albumIndex() {
        val obj = dev.reelmemory.app.json.JsonObject(
            """{"modality":"VISUAL","start_ms":null,"end_ms":null,
                "excerpt":"a handbag","album_index":2}"""
        )
        val chip = parseEvidenceChip(obj)
        assertEquals(2, chip.albumIndex)
        assertEquals("Photo 3", chip.photoLabel)
    }

    @Test
    fun parseEvidenceChip_noAlbumIndex() {
        val obj = dev.reelmemory.app.json.JsonObject(
            """{"modality":"OCR","start_ms":null,"end_ms":null,"excerpt":"sale"}"""
        )
        val chip = parseEvidenceChip(obj)
        assertNull(chip.albumIndex)
        assertNull(chip.photoLabel)
    }
}
