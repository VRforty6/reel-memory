package dev.reelmemory.app.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class CaptureApiTest {

    @Test
    fun `202 with memory id classifies as accepted`() {
        val result = CaptureApi.classifyResponse(
            202,
            """{"id":"c1","memory_id":"m-123","status":"PROCESSING","duplicate":false}"""
        )
        assertTrue(result is CaptureApi.SyncResult.Accepted)
        result as CaptureApi.SyncResult.Accepted
        assertEquals("m-123", result.memoryId)
        assertEquals(false, result.duplicate)
    }

    @Test
    fun `200 duplicate classifies as accepted duplicate`() {
        val result = CaptureApi.classifyResponse(
            200,
            """{"id":"c1","memory_id":"m-9","status":"READY","duplicate":true}"""
        ) as CaptureApi.SyncResult.Accepted
        assertEquals(true, result.duplicate)
    }

    @Test
    fun `422 with detail classifies as rejected with code and message`() {
        val result = CaptureApi.classifyResponse(
            422,
            """{"detail":{"code":"INVALID_URL","message":"not a fetchable URL"}}"""
        )
        assertTrue(result is CaptureApi.SyncResult.Rejected)
        result as CaptureApi.SyncResult.Rejected
        assertEquals("INVALID_URL", result.code)
        assertEquals("not a fetchable URL", result.message)
    }

    @Test
    fun `500 classifies as transient`() {
        val result = CaptureApi.classifyResponse(500, "boom")
        assertTrue(result is CaptureApi.SyncResult.Transient)
    }

    @Test
    fun `4xx without detail falls back to http code`() {
        val result = CaptureApi.classifyResponse(429, "")
        assertTrue(result is CaptureApi.SyncResult.Rejected)
        result as CaptureApi.SyncResult.Rejected
        assertEquals("HTTP_429", result.code)
    }
}
