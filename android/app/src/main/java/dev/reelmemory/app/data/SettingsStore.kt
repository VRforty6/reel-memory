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

/**
 * App settings: currently just the backend base URL. Defaults to the Android
 * emulator loopback address (10.0.2.2) hitting the backend's default port.
 */
class SettingsStore(private val context: Context) {

    companion object {
        private val KEY_BACKEND_URL = stringPreferencesKey("backend_base_url")
        private val KEY_GOOGLE_CLIENT_ID = stringPreferencesKey("google_client_id")
        private val KEY_PLAY_PRODUCT_ID = stringPreferencesKey("play_product_id")
        private val KEY_DEBUG_DIAGNOSTICS = booleanPreferencesKey("debug_diagnostics")

        /** Default: backend running on the dev machine, reached from an emulator. */
        const val DEFAULT_BACKEND_URL = "http://10.0.2.2:8000"

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

    val backendUrl: Flow<String> = context.settingsDataStore.data
        .map { prefs -> prefs[KEY_BACKEND_URL] ?: DEFAULT_BACKEND_URL }

    suspend fun getBackendUrl(): String = backendUrl.first()

    suspend fun setBackendUrl(raw: String) {
        val normalized = normalize(raw)
        require(normalized.startsWith("http://") || normalized.startsWith("https://")) {
            "Backend URL must start with http:// or https://"
        }
        context.settingsDataStore.edit { prefs ->
            prefs[KEY_BACKEND_URL] = normalized
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
