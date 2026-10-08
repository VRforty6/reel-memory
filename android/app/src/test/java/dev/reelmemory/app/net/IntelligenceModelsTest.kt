package dev.reelmemory.app.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class IntelligenceModelsTest {

    @Test
    fun `actions parse and sort P0 before P1 before P2`() {
        val payload = """
            {"actions": [
              {"id":"a2","title":"Low priority","priority":"P2","effort":"large","evidence_quote":null},
              {"id":"a1","title":"Do this now","priority":"P0","effort":"small","detail":"Today","evidence_quote":"buy milk"},
              {"id":"a3","title":"Mid","priority":"p1","effort":"medium"}
            ]}
        """.trimIndent()
        val actions = parseActionsResponse(payload)
        assertNotNull(actions)
        assertEquals(listOf("P0", "P1", "P2"), actions!!.map { it.priority })
        assertEquals("buy milk", actions[0].evidenceQuote)
        assertEquals("Today", actions[0].detail)
        assertEquals("small", actions[0].effort)
    }

    @Test
    fun `actions with invalid json return null`() {
        assertNull(parseActionsResponse("not json"))
    }

    @Test
    fun `actions missing array returns empty list`() {
        assertEquals(emptyList<ActionItem>(), parseActionsResponse("""{"ok":true}"""))
    }

    @Test
    fun `brief parses summary verdicts assessments and closing question`() {
        val payload = """
            {"summary":"The reel recommends X for beginners.",
             "verdicts":[
               {"claim":"X is beginner-friendly","verdict":"supported","note":"matches docs"},
               {"claim":"X is free","verdict":"contradicted","note":null}
             ],
             "usefulness":"high","effort":"small",
             "closing_question":"Want to try X this week?"}
        """.trimIndent()
        val brief = parseDecisionBriefResponse(payload)
        assertNotNull(brief)
        assertEquals("The reel recommends X for beginners.", brief!!.summary)
        assertEquals(2, brief.verdicts.size)
        assertEquals(Verdict.SUPPORTED, brief.verdicts[0].verdict)
        assertEquals(Verdict.CONTRADICTED, brief.verdicts[1].verdict)
        assertEquals("matches docs", brief.verdicts[0].note)
        assertEquals("high", brief.usefulness)
        assertEquals("small", brief.effort)
        assertEquals("Want to try X this week?", brief.closingQuestion)
    }

    @Test
    fun `brief with no content returns null`() {
        assertNull(parseDecisionBriefResponse("""{"verdicts":[]}"""))
        assertNull(parseDecisionBriefResponse("garbage"))
    }

    @Test
    fun `priority rank puts unknown last`() {
        val items = listOf(
            ActionItem("1", "t", null, "P9", null, null, null, null, null),
            ActionItem("2", "t", null, "P0", null, null, null, null, null)
        )
        assertTrue(items[1].priorityRank() < items[0].priorityRank())
    }

    @Test
    fun `actions parse nested evidence with album index`() {
        val payload = """
            {"actions": [
              {"title":"Buy the lens","priority":"P0","effort":"small",
               "evidence":{"modality":"speech","timestamp_ms":45000,
                           "quote":"this 35mm is a steal","album_index":2}}
            ]}
        """.trimIndent()
        val actions = parseActionsResponse(payload)
        assertNotNull(actions)
        val a = actions!!.single()
        assertEquals("speech", a.evidenceModality)
        assertEquals(45000L, a.evidenceTimestampMs)
        assertEquals("this 35mm is a steal", a.evidenceQuote)
        assertEquals(2, a.evidenceAlbumIndex)
        assertEquals("Photo 3", a.evidencePhotoLabel)
    }

    @Test
    fun `actions nested evidence without album index has no photo label`() {
        val payload = """
            {"actions": [
              {"title":"Do it","priority":"P1",
               "evidence":{"modality":"ocr","quote":"50% off"}}
            ]}
        """.trimIndent()
        val a = parseActionsResponse(payload)!!.single()
        assertEquals("ocr", a.evidenceModality)
        assertNull(a.evidenceTimestampMs)
        assertNull(a.evidenceAlbumIndex)
        assertNull(a.evidencePhotoLabel)
    }

    @Test
    fun `actions fall back to flat evidence quote`() {
        val payload = """
            {"actions": [
              {"title":"Old style","priority":"P2","evidence_quote":"buy milk"}
            ]}
        """.trimIndent()
        val a = parseActionsResponse(payload)!!.single()
        assertEquals("buy milk", a.evidenceQuote)
        assertNull(a.evidenceModality)
        assertNull(a.evidencePhotoLabel)
    }
}
