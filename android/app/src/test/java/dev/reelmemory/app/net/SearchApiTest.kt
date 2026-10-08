package dev.reelmemory.app.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SearchApiTest {

    private val fullSearchJson = """
        {
          "results": [
            {
              "memory_id": "m1",
              "title": "Sticky note trick",
              "summary": "A person writes with two pens.",
              "category": "productivity",
              "creator_handle": "vrfortysix",
              "platform": "INSTAGRAM",
              "processing_status": "READY",
              "source_status": "ok",
              "score": 0.87,
              "evidence": [
                {"type": "VISUAL", "snippet": "person writing on a red sticky note",
                 "start_ms": 18000, "end_ms": 21000},
                {"type": "SPEECH", "snippet": "grab two pens",
                 "start_ms": null, "end_ms": null}
              ]
            },
            {
              "memory_id": "m2",
              "title": null,
              "summary": null,
              "category": null,
              "creator_handle": null,
              "platform": "TIKTOK",
              "processing_status": "METADATA_ONLY",
              "source_status": null,
              "score": 0.41,
              "evidence": []
            }
          ],
          "degraded": true
        }
    """.trimIndent()

    @Test
    fun parseSearchResponse_full() {
        val (results, degraded) = parseSearchResponse(fullSearchJson)!!
        assertTrue(degraded)
        assertEquals(2, results.size)

        val first = results[0]
        assertEquals("m1", first.memoryId)
        assertEquals("Sticky note trick", first.title)
        assertEquals("vrfortysix", first.creatorHandle)
        assertEquals("INSTAGRAM", first.platform)
        assertEquals("READY", first.processingStatus)
        assertEquals(2, first.evidence.size)

        val visual = first.evidence[0]
        assertEquals("VISUAL", visual.type)
        assertEquals("person writing on a red sticky note", visual.snippet)
        assertEquals(18_000L, visual.startMs)
        assertEquals(21_000L, visual.endMs)

        val speech = first.evidence[1]
        assertEquals("SPEECH", speech.type)
        assertNull(speech.startMs)
        assertNull(speech.endMs)

        val second = results[1]
        assertEquals("m2", second.memoryId)
        assertNull(second.title)
        assertNull(second.creatorHandle)
        assertEquals("TIKTOK", second.platform)
        assertEquals("METADATA_ONLY", second.processingStatus)
        assertTrue(second.evidence.isEmpty())
    }

    @Test
    fun parseSearchResponse_notDegradedByDefault() {
        val (results, degraded) = parseSearchResponse("""{"results": []}""")!!
        assertTrue(results.isEmpty())
        assertFalse(degraded)
    }

    @Test
    fun parseSearchResponse_missingResults() {
        val (results, degraded) = parseSearchResponse("""{"degraded": false}""")!!
        assertTrue(results.isEmpty())
        assertFalse(degraded)
    }

    @Test
    fun parseSearchResponse_invalidJson() {
        assertNull(parseSearchResponse("not json"))
    }

    @Test
    fun parseSearchResponse_mediaKindAndHasThumbnail() {
        val (results, _) = parseSearchResponse(
            """{"results":[
              {"memory_id":"a","processing_status":"READY","media_kind":"video","has_thumbnail":true},
              {"memory_id":"b","processing_status":"METADATA_ONLY","media_kind":"article","has_thumbnail":false},
              {"memory_id":"c","processing_status":"READY"}
            ]}"""
        )!!
        assertEquals(3, results.size)
        assertEquals("video", results[0].mediaKind)
        assertEquals(true, results[0].hasThumbnail)
        assertEquals("article", results[1].mediaKind)
        assertEquals(false, results[1].hasThumbnail)
        assertNull(results[2].mediaKind)
        assertNull(results[2].hasThumbnail)
    }

    @Test
    fun parseSearchEvidence_defaults() {
        val e = parseSearchEvidence(dev.reelmemory.app.json.JsonObject("""{}"""))
        assertEquals("UNKNOWN", e.type)
        assertEquals("", e.snippet)
        assertNull(e.startMs)
        assertNull(e.endMs)
    }

    @Test
    fun parseSearchEvidence_typeUppercased() {
        val e = parseSearchEvidence(
            dev.reelmemory.app.json.JsonObject(
                """{"type": "visual", "snippet": "x", "start_ms": 1000, "end_ms": 2000}"""
            )
        )
        assertEquals("VISUAL", e.type)
    }

    @Test
    fun searchClient_hasInteractiveHardDeadline() {
        val client = buildSearchHttpClient()

        assertEquals(10_000, client.connectTimeoutMillis)
        assertEquals(25_000, client.readTimeoutMillis)
        assertEquals(30_000, client.callTimeoutMillis)
        assertTrue(client.callTimeoutMillis < client.readTimeoutMillis + client.connectTimeoutMillis)
    }
}
