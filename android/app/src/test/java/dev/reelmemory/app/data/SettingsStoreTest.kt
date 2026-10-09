package dev.reelmemory.app.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SettingsStoreTest {
    @Test
    fun `emulator uses emulator default`() {
        assertEquals(
            SettingsStore.EMULATOR_BACKEND_URL,
            SettingsStore.resolveBackendUrl(persistedUrl = null, isEmulator = true),
        )
    }

    @Test
    fun `physical device uses Tailscale HTTPS default`() {
        assertEquals(
            SettingsStore.PHYSICAL_DEVICE_BACKEND_URL,
            SettingsStore.resolveBackendUrl(persistedUrl = null, isEmulator = false),
        )
    }

    @Test
    fun `legacy emulator URL migrates on physical device`() {
        assertTrue(
            SettingsStore.shouldMigrateLegacyUrl(
                persistedUrl = SettingsStore.EMULATOR_BACKEND_URL,
                isEmulator = false,
            ),
        )
        assertEquals(
            SettingsStore.PHYSICAL_DEVICE_BACKEND_URL,
            SettingsStore.resolveBackendUrl(
                persistedUrl = SettingsStore.EMULATOR_BACKEND_URL,
                isEmulator = false,
            ),
        )
    }

    @Test
    fun `legacy emulator URL does not migrate on emulator`() {
        assertFalse(
            SettingsStore.shouldMigrateLegacyUrl(
                persistedUrl = SettingsStore.EMULATOR_BACKEND_URL,
                isEmulator = true,
            ),
        )
        assertEquals(
            SettingsStore.EMULATOR_BACKEND_URL,
            SettingsStore.resolveBackendUrl(
                persistedUrl = SettingsStore.EMULATOR_BACKEND_URL,
                isEmulator = true,
            ),
        )
    }

    @Test
    fun `custom URL is preserved on physical device`() {
        val customUrl = "https://reel-memory.example.com"

        assertFalse(SettingsStore.shouldMigrateLegacyUrl(customUrl, isEmulator = false))
        assertEquals(customUrl, SettingsStore.resolveBackendUrl(customUrl, isEmulator = false))
    }

    @Test
    fun `explicit USB debug URL is preserved on physical device`() {
        assertFalse(
            SettingsStore.shouldMigrateLegacyUrl(
                persistedUrl = SettingsStore.USB_DEBUG_BACKEND_URL,
                isEmulator = false,
                userConfigured = true,
            ),
        )
        assertEquals(
            SettingsStore.USB_DEBUG_BACKEND_URL,
            SettingsStore.resolveBackendUrl(
                persistedUrl = SettingsStore.USB_DEBUG_BACKEND_URL,
                isEmulator = false,
                userConfigured = true,
            ),
        )
    }

    @Test
    fun `legacy physical USB default migrates to Tailscale`() {
        assertTrue(
            SettingsStore.shouldMigrateLegacyUrl(
                persistedUrl = SettingsStore.USB_DEBUG_BACKEND_URL,
                isEmulator = false,
            ),
        )
        assertEquals(
            SettingsStore.PHYSICAL_DEVICE_BACKEND_URL,
            SettingsStore.resolveBackendUrl(
                persistedUrl = SettingsStore.USB_DEBUG_BACKEND_URL,
                isEmulator = false,
            ),
        )
    }

    @Test
    fun `completed migration still repairs stale physical-device local URLs`() {
        val staleUrls = listOf(
            "http://10.0.2.2:8001",
            "http://127.0.0.1:9999",
            "http://localhost:8000",
        )

        staleUrls.forEach { staleUrl ->
            assertTrue(
                SettingsStore.shouldRepairPhysicalDeviceUrl(
                    persistedUrl = staleUrl,
                    isEmulator = false,
                ),
            )
            assertEquals(
                SettingsStore.PHYSICAL_DEVICE_BACKEND_URL,
                SettingsStore.resolveBackendUrl(
                    persistedUrl = staleUrl,
                    isEmulator = false,
                    legacyMigrationComplete = true,
                ),
            )
        }
    }

    @Test
    fun `backend URL normalization trims and adds scheme`() {
        assertEquals("http://192.168.1.20:8000", SettingsStore.normalize(" 192.168.1.20:8000/ "))
        assertEquals("https://example.com/api", SettingsStore.normalize("https://example.com/api///"))
    }

    @Test
    fun `runtime detector distinguishes emulator and physical device`() {
        assertTrue(
            AndroidDeviceEnvironment.isEmulator(
                fingerprint = "google/sdk_gphone64_x86_64/emu64xa:14/UE1A/test-keys",
                model = "sdk_gphone64_x86_64",
                manufacturer = "Google",
                brand = "google",
                device = "emu64xa",
                product = "sdk_gphone64_x86_64",
            ),
        )
        assertFalse(
            AndroidDeviceEnvironment.isEmulator(
                fingerprint = "samsung/dm3q/dm3q:14/UP1A/release-keys",
                model = "SM-S918W",
                manufacturer = "samsung",
                brand = "samsung",
                device = "dm3q",
                product = "dm3qcsx",
            ),
        )
    }
}
