package dev.reelmemory.app.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class UpdateApiTest {
    private val sha = "a".repeat(64)

    @Test
    fun parseUpdateMetadata_valid() {
        val info = parseAppUpdateInfo(
            """{"update_available":true,"latest_version_code":3,"latest_version_name":"0.1.2","release_notes":"Category tree","size_bytes":10485760,"sha256":"$sha","download_path":"/v1/app/update/apk"}"""
        )!!
        assertTrue(info.updateAvailable)
        assertEquals(3, info.latestVersionCode)
        assertEquals("0.1.2", info.latestVersionName)
        assertEquals("Category tree", info.releaseNotes)
        assertEquals(10_485_760L, info.sizeBytes)
    }

    @Test
    fun parseUpdateMetadata_currentVersion() {
        val info = parseAppUpdateInfo(
            """{"update_available":false,"latest_version_code":3,"latest_version_name":"0.1.2","release_notes":null,"size_bytes":10,"sha256":"$sha","download_path":"/v1/app/update/apk"}"""
        )!!
        assertFalse(info.updateAvailable)
        assertNull(info.releaseNotes)
    }

    @Test
    fun parseUpdateMetadata_rejectsAbsoluteOrCorruptMetadata() {
        assertNull(parseAppUpdateInfo("not json"))
        assertNull(parseAppUpdateInfo(
            """{"update_available":true,"latest_version_code":3,"latest_version_name":"0.1.2","size_bytes":10,"sha256":"$sha","download_path":"https://evil.invalid/app.apk"}"""
        ))
        assertNull(parseAppUpdateInfo(
            """{"update_available":true,"latest_version_code":3,"latest_version_name":"0.1.2","size_bytes":10,"sha256":"bad","download_path":"/v1/app/update/apk"}"""
        ))
    }
}
