package dev.reelmemory.app.auth

/**
 * Google Sign-In seam.
 *
 * The production implementation needs `com.google.android.gms:play-services-auth`
 * (see the Android README's "Publisher setup" for the drop-in implementation
 * and the Gradle line). It requests an **ID token** for the backend's
 * `GOOGLE_CLIENT_ID` (configurable in Settings); the app then POSTs that
 * token to `/v1/auth/google`, which verifies it server-side and returns the
 * app's own token pair. The raw Google token is never stored.
 */
sealed class GoogleSignInResult {
    /** The Google ID token to send to POST /v1/auth/google. */
    data class IdToken(val idToken: String) : GoogleSignInResult()
    data object Cancelled : GoogleSignInResult()
    data class Error(val message: String) : GoogleSignInResult()
    /** This build has no Google sign-in wired. */
    data class Unavailable(val message: String) : GoogleSignInResult()
}

interface GoogleIdTokenProvider {
    suspend fun requestIdToken(): GoogleSignInResult
}

/**
 * Placeholder used until the publisher wires play-services-auth. Returns
 * [GoogleSignInResult.Unavailable] with a pointer to the README so the UI
 * can explain honestly instead of failing mysteriously.
 */
class StubGoogleSignInProvider : GoogleIdTokenProvider {
    override suspend fun requestIdToken(): GoogleSignInResult =
        GoogleSignInResult.Unavailable(
            "Google sign-in isn't wired in this build yet — " +
                "see the README's publisher setup to enable it."
        )
}
