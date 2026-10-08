package dev.reelmemory.app.auth

import android.content.Context
import dev.reelmemory.app.data.SettingsStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

/** Raw HTTP outcome for [SessionManager.authorizedHttp]. */
data class HttpResponse(val code: Int, val body: String)

sealed class AuthState {
    data object SignedOut : AuthState()
    data class SignedIn(val profile: MeProfile?) : AuthState()
}

/** UI-friendly outcome of a sign-up / sign-in attempt. */
sealed class SignInResult {
    data object Ok : SignInResult()
    data class Error(val message: String) : SignInResult()
}

/**
 * Everything the session layer needs from the network, as one seam so tests
 * can fake it. [AuthApi] implements this for production.
 */
interface AuthBackend : TokenRefresher, ProfileLoader {
    suspend fun signup(email: String, password: String): AuthApi.AuthCallResult<TokenPair>
    suspend fun login(email: String, password: String): AuthApi.AuthCallResult<TokenPair>
    suspend fun googleSignIn(idToken: String): AuthApi.AuthCallResult<TokenPair>
    suspend fun logout(refreshToken: String): AuthApi.AuthCallResult<Unit>
}

/**
 * Owns the login session:
 *
 * - Tokens live in [TokenStore] (Keystore-encrypted at rest).
 * - [validAccessToken] refreshes proactively (60 s margin) so most calls
 *   never see a 401.
 * - [authorizedHttp] attaches the Bearer token, and on a 401 performs ONE
 *   silent refresh + single retry; if the refresh fails the local session is
 *   cleared (refresh tokens rotate server-side, so a failed refresh means
 *   the credentials are dead) and the UI routes to sign-in via [authState].
 * - On a 402 the parsed [QuotaExceededInfo] is emitted on [quotaEvents] so
 *   the UI can show the paywall immediately, wherever the call came from.
 *
 * All network goes through [Dispatchers.IO]; state is exposed as flows.
 */
class SessionManager(
    private val tokenStore: TokenStore,
    private val backend: AuthBackend,
    private val clock: () -> Long = System::currentTimeMillis,
    private val scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
) {

    private val _authState = MutableStateFlow<AuthState>(AuthState.SignedOut)
    val authState: StateFlow<AuthState> = _authState.asStateFlow()

    private val _quotaEvents = Channel<QuotaExceededInfo>(capacity = 8)
    /**
     * Quota-exceeded events, one per 402. A [Channel] (not a SharedFlow):
     * SharedFlow only retains values for *active* collectors, so a 402 that
     * fires while the UI isn't collecting yet (e.g. a background sync) would
     * be silently dropped and the paywall would never show. The channel
     * buffers each event until the UI consumes it, exactly once.
     */
    val quotaEvents: Flow<QuotaExceededInfo> = _quotaEvents.receiveAsFlow()

    private val refreshMutex = Mutex()

    /** True when a token pair is stored (regardless of expiry). Cheap; safe to call anywhere. */
    fun hasSession(): Boolean = try {
        tokenStore.load() != null
    } catch (_: Exception) {
        false
    }

    /**
     * Restores a previous session from disk. Call once at app start.
     * Returns true if tokens were found (profile load is best-effort).
     */
    suspend fun restore(): Boolean {
        val pair = withContext(Dispatchers.IO) { tokenStore.load() } ?: return false
        _authState.value = AuthState.SignedIn(null)
        // Proactively refresh if the stored access token is stale.
        if (pair.isExpired(clock())) {
            if (!refreshNow()) return false
        } else {
            refreshProfile()
        }
        return true
    }

    /** A usable access token, refreshing proactively when it is near expiry. Null when signed out. */
    suspend fun validAccessToken(): String? {
        val pair = withContext(Dispatchers.IO) { tokenStore.load() } ?: return null
        if (!pair.isExpired(clock())) return pair.accessToken
        return if (refreshNow()) {
            withContext(Dispatchers.IO) { tokenStore.load() }?.accessToken
        } else null
    }

    /**
     * Runs [call] with the current Bearer token. On 401: one silent refresh
     * and a single retry. A still-failing 401 clears the local session. A
     * 402 emits the quota info on [quotaEvents].
     */
    suspend fun authorizedHttp(call: suspend (bearer: String?) -> HttpResponse): HttpResponse {
        var token = validAccessToken()
        var resp = call(token)
        if (resp.code == 401 && token != null) {
            // Force a real refresh: the token the server just rejected may not
            // be expired yet (e.g. server-side revocation).
            token = if (refreshNow(force = true)) validAccessToken() else null
            resp = if (token != null) call(token) else resp
        }
        when (resp.code) {
            401 -> clearSession()
            402 -> (parseAuthOutcome(402, resp.body) as? AuthOutcome.QuotaExceeded)
                ?.info?.let { _quotaEvents.trySend(it) }
        }
        return resp
    }

    /**
     * Refresh-token rotation, mutex-guarded so concurrent 401s trigger a
     * single refresh. Returns true when a fresh pair is stored.
     *
     * [force] skips the "already fresh" shortcut — used after a 401, where
     * the rejected token may not be expired yet.
     */
    suspend fun refreshNow(force: Boolean = false): Boolean = refreshMutex.withLock {
        val current = withContext(Dispatchers.IO) { tokenStore.load() } ?: return false
        // Another caller may have refreshed while we waited for the lock.
        if (!force && !current.isExpired(clock())) return true
        return when (val r = backend.refreshTokens(current.refreshToken)) {
            is AuthApi.AuthCallResult.Ok -> {
                withContext(Dispatchers.IO) { tokenStore.save(r.value) }
                try {
                    refreshProfile()
                } catch (_: Exception) {
                    // Profile is nice-to-have; the tokens are what matter.
                }
                true
            }
            else -> {
                clearSession()
                false
            }
        }
    }

    suspend fun signUp(email: String, password: String): SignInResult =
        adopt(backend.signup(email, password))

    suspend fun login(email: String, password: String): SignInResult =
        adopt(backend.login(email, password))

    suspend fun signInWithGoogle(idToken: String): SignInResult =
        adopt(backend.googleSignIn(idToken))

    private suspend fun adopt(
        result: AuthApi.AuthCallResult<TokenPair>
    ): SignInResult = when (result) {
        is AuthApi.AuthCallResult.Ok -> {
            withContext(Dispatchers.IO) { tokenStore.save(result.value) }
            try {
                refreshProfile()
            } catch (_: Exception) {
                _authState.value = AuthState.SignedIn(null)
            }
            SignInResult.Ok
        }
        is AuthApi.AuthCallResult.Rejected -> SignInResult.Error(
            when (result.code) {
                "email_taken" -> "That email is already registered — try signing in instead."
                "invalid_credentials" -> result.message.ifEmpty { "Wrong email or password." }
                else -> result.message.ifEmpty { "Sign-in failed (${result.code})." }
            }
        )
        is AuthApi.AuthCallResult.RateLimited ->
            SignInResult.Error("Too many attempts — wait a few minutes and try again.")
        is AuthApi.AuthCallResult.NotConfigured ->
            SignInResult.Error(result.message.ifEmpty { "Google sign-in isn't set up on the server yet." })
        is AuthApi.AuthCallResult.Transient ->
            SignInResult.Error(result.message.ifEmpty { "Couldn't reach the server." })
    }

    /** Best-effort server revocation, then the local session is cleared regardless. */
    suspend fun signOut() {
        val refreshToken = withContext(Dispatchers.IO) { tokenStore.load() }?.refreshToken
        if (refreshToken != null) {
            try {
                backend.logout(refreshToken)
            } catch (_: Exception) {
                // Local state is authoritative for sign-out.
            }
        }
        clearSession()
    }

    /** Reloads GET /v1/me into [authState]. Returns the profile, or null on failure. */
    suspend fun refreshProfile(): MeProfile? {
        val token = withContext(Dispatchers.IO) { tokenStore.load() }?.accessToken
            ?: return null
        val profile = try {
            backend.loadProfile(token)
        } catch (_: Exception) {
            null
        }
        _authState.value = AuthState.SignedIn(profile)
        return profile
    }

    private fun clearSession() {
        try {
            tokenStore.clear()
        } catch (_: Exception) {
            // Keep going: in-memory state is what the UI reads.
        }
        _authState.value = AuthState.SignedOut
    }

    companion object {
        @Volatile
        private var instance: SessionManager? = null

        fun get(context: Context): SessionManager =
            instance ?: synchronized(this) {
                instance ?: run {
                    val app = context.applicationContext
                    val settings = SettingsStore(app)
                    val api = AuthApi(baseUrl = { settings.getBackendUrl() })
                    SessionManager(EncryptedTokenStore(app), api).also { manager ->
                        manager.scope.launch { manager.restore() }
                        instance = manager
                    }
                }
            }

        /** Test-only: replace the singleton (e.g. with fakes). */
        fun setForTests(manager: SessionManager?) {
            instance = manager
        }
    }
}
