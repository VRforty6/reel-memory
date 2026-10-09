package dev.reelmemory.app.data

import android.content.Context
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

private val Context.settingsDataStore by preferencesDataStore(name = "reel_memory_settings")

/** App settings, including the single source of truth for backend selection. */
class SettingsStore(
    private val context: Context,
    isEmulator: Boolean = AndroidDeviceEnvironment.isEmulator(),
) {

    private val emulator = isEmulator

    companion object {
        private val KEY_BACKEND_URL = stringPreferencesKey("backend_base_url")
        private val KEY_TAILSCALE_DEFAULT_MIGRATED =
            booleanPreferencesKey("backend_tailscale_default_migrated")
        private val KEY_BACKEND_URL_USER_CONFIGURED =
            booleanPreferencesKey("backend_url_user_configured")
        private val KEY_GOOGLE_CLIENT_ID = stringPreferencesKey("google_client_id")
        private val KEY_PLAY_PRODUCT_ID = stringPreferencesKey("play_product_id")
        private val KEY_DEBUG_DIAGNOSTICS = booleanPreferencesKey("debug_diagnostics")

        const val EMULATOR_BACKEND_URL = "http://10.0.2.2:8000"
        const val USB_DEBUG_BACKEND_URL = "http://127.0.0.1:8000"
        const val PHYSICAL_DEVICE_BACKEND_URL = "https://vrforty6.tail434ddf.ts.net"

        internal fun defaultBackendUrl(isEmulator: Boolean): String =
            if (isEmulator) EMULATOR_BACKEND_URL else PHYSICAL_DEVICE_BACKEND_URL

        internal fun resolveBackendUrl(
            persistedUrl: String?,
            isEmulator: Boolean,
            legacyMigrationComplete: Boolean = false,
            userConfigured: Boolean = false,
        ): String =
            if (
                shouldRepairPhysicalDeviceUrl(
                    persistedUrl = persistedUrl,
                    isEmulator = isEmulator,
                    userConfigured = userConfigured,
                ) ||
                shouldMigrateLegacyUrl(
                    persistedUrl,
                    isEmulator,
                    legacyMigrationComplete,
                    userConfigured,
                )
            ) {
                PHYSICAL_DEVICE_BACKEND_URL
            } else {
                persistedUrl ?: defaultBackendUrl(isEmulator)
            }

        internal fun shouldRepairPhysicalDeviceUrl(
            persistedUrl: String?,
            isEmulator: Boolean,
            userConfigured: Boolean = false,
        ): Boolean {
            if (userConfigured || isEmulator || persistedUrl.isNullOrBlank()) return false
            val normalized = normalize(persistedUrl)
            val host = runCatching { java.net.URI(normalized).host?.lowercase() }.getOrNull()
            return host == "10.0.2.2" || host == "127.0.0.1" || host == "localhost"
        }

        internal fun shouldMigrateLegacyUrl(
            persistedUrl: String?,
            isEmulator: Boolean,
            legacyMigrationComplete: Boolean = false,
            userConfigured: Boolean = false,
        ): Boolean = !userConfigured &&
            !legacyMigrationComplete &&
            !isEmulator &&
            (persistedUrl == EMULATOR_BACKEND_URL || persistedUrl == USB_DEBUG_BACKEND_URL)

        /**
         * Google OAuth client ID for Sign-In (Google Cloud Console -> APIs &
         * Services -> Credentials -> OAuth client ID for Android). The backend
         * verifies ID tokens against its own GOOGLE_CLIENT_ID, so this must
         * belong to the same Cloud project. Empty = not configured.
         */
        const val DEFAULT_PLAY_PRODUCT_ID = "pro_monthly"

        fun normalize(raw: String): String {
            var url = raw.trim().trimEnd('/')
            if (!url.contains("://")) url = "http://$url"
            return url
        }
    }

    val defaultBackendUrl: String = defaultBackendUrl(emulator)

    val backendUrl: Flow<String> = context.settingsDataStore.data
        .map { prefs ->
            resolveBackendUrl(
                persistedUrl = prefs[KEY_BACKEND_URL],
                isEmulator = emulator,
                legacyMigrationComplete =
                    prefs[KEY_TAILSCALE_DEFAULT_MIGRATED] ?: false,
                userConfigured = prefs[KEY_BACKEND_URL_USER_CONFIGURED] ?: false,
            )
        }

    suspend fun getBackendUrl(): String {
        val initialPreferences = context.settingsDataStore.data.first()
        val migrationComplete =
            initialPreferences[KEY_TAILSCALE_DEFAULT_MIGRATED] ?: false
        val userConfigured = initialPreferences[KEY_BACKEND_URL_USER_CONFIGURED] ?: false
        val repairNeeded = shouldRepairPhysicalDeviceUrl(
            persistedUrl = initialPreferences[KEY_BACKEND_URL],
            isEmulator = emulator,
            userConfigured = userConfigured,
        )
        if (emulator || (migrationComplete && !repairNeeded)) {
            return resolveBackendUrl(
                persistedUrl = initialPreferences[KEY_BACKEND_URL],
                isEmulator = emulator,
                legacyMigrationComplete = migrationComplete,
                userConfigured = userConfigured,
            )
        }

        // Complete the migration atomically so a concurrent custom URL is never overwritten.
        // Also repair stale emulator/loopback defaults left behind by older physical-device builds,
        // even when the one-time migration flag was already set by an earlier release.
        val updatedPreferences = context.settingsDataStore.edit { prefs ->
            val migrated = prefs[KEY_TAILSCALE_DEFAULT_MIGRATED] ?: false
            val configured = prefs[KEY_BACKEND_URL_USER_CONFIGURED] ?: false
            val persistedUrl = prefs[KEY_BACKEND_URL]
            if (
                shouldRepairPhysicalDeviceUrl(
                    persistedUrl = persistedUrl,
                    isEmulator = emulator,
                    userConfigured = configured,
                ) ||
                (!migrated && shouldMigrateLegacyUrl(
                    persistedUrl = persistedUrl,
                    isEmulator = emulator,
                    userConfigured = configured,
                ))
            ) {
                prefs[KEY_BACKEND_URL] = PHYSICAL_DEVICE_BACKEND_URL
            }
            if (!migrated) prefs[KEY_TAILSCALE_DEFAULT_MIGRATED] = true
        }
        return resolveBackendUrl(
            persistedUrl = updatedPreferences[KEY_BACKEND_URL],
            isEmulator = emulator,
            legacyMigrationComplete =
                updatedPreferences[KEY_TAILSCALE_DEFAULT_MIGRATED] ?: false,
            userConfigured = updatedPreferences[KEY_BACKEND_URL_USER_CONFIGURED] ?: false,
        )
    }

    suspend fun setBackendUrl(raw: String) {
        val normalized = normalize(raw)
        require(normalized.startsWith("http://") || normalized.startsWith("https://")) {
            "Backend URL must start with http:// or https://"
        }
        context.settingsDataStore.edit { prefs ->
            prefs[KEY_BACKEND_URL] = normalized
            // Anything saved through Settings is an explicit user choice.
            prefs[KEY_BACKEND_URL_USER_CONFIGURED] = true
            prefs[KEY_TAILSCALE_DEFAULT_MIGRATED] = true
        }
    }

    // --- Commercialization settings --------------------------------------

    /** Google OAuth client ID for Sign-In. Null/blank = not configured. */
    val googleClientId: Flow<String?> = context.settingsDataStore.data
        .map { prefs -> prefs[KEY_GOOGLE_CLIENT_ID]?.takeIf { it.isNotBlank() } }

    suspend fun getGoogleClientId(): String? = googleClientId.first()

    suspend fun setGoogleClientId(clientId: String?) {
        context.settingsDataStore.edit { prefs ->
            val trimmed = clientId?.trim().orEmpty()
            if (trimmed.isEmpty()) prefs.remove(KEY_GOOGLE_CLIENT_ID)
            else prefs[KEY_GOOGLE_CLIENT_ID] = trimmed
        }
    }

    /** Play Console subscription product ID for Pro. */
    val playProductId: Flow<String> = context.settingsDataStore.data
        .map { prefs ->
            prefs[KEY_PLAY_PRODUCT_ID]?.takeIf { it.isNotBlank() }
                ?: DEFAULT_PLAY_PRODUCT_ID
        }

    suspend fun getPlayProductId(): String = playProductId.first()

    suspend fun setPlayProductId(productId: String) {
        val trimmed = productId.trim()
        require(trimmed.isNotEmpty()) { "Product ID must not be empty." }
        context.settingsDataStore.edit { prefs ->
            prefs[KEY_PLAY_PRODUCT_ID] = trimmed
        }
    }

    // --- Developer settings (hidden behind a long-press on the version row) ---

    /**
     * When true, raw backend failure details are shown on the failed-memory
     * screen. Normal users never see this toggle.
     */
    val debugDiagnostics: Flow<Boolean> = context.settingsDataStore.data
        .map { prefs -> prefs[KEY_DEBUG_DIAGNOSTICS] ?: false }

    suspend fun setDebugDiagnostics(enabled: Boolean) {
        context.settingsDataStore.edit { prefs ->
            prefs[KEY_DEBUG_DIAGNOSTICS] = enabled
        }
    }
}
