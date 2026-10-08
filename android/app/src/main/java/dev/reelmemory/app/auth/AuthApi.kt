package dev.reelmemory.app.auth

import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import dev.reelmemory.app.json.JsonObject
import java.util.concurrent.TimeUnit

/**
 * HTTP client for the backend's auth, profile, and billing endpoints.
 *
 *   POST /v1/auth/signup  {"email","password"}        -> 201 TokenPair
 *   POST /v1/auth/login   {"email","password"}        -> 200 TokenPair (429 when throttled)
 *   POST /v1/auth/refresh {"refresh_token"}          -> 200 TokenPair (rotates; old one dies)
 *   POST /v1/auth/logout  {"refresh_token"}          -> 204
 *   POST /v1/auth/google  {"id_token"}               -> 200 TokenPair (503 if not configured)
 *   GET  /v1/me                                       -> 200 MeResponse (Bearer)
 *   POST /v1/billing/verify {"package_name","product_id","purchase_token"} -> 200 (Bearer; 503 if not configured)
 *
 * Login failures intentionally say "wrong email or password" whether or not
 * the account exists — the client must not try to distinguish either.
 */
class AuthApi(
    /** Resolves the backend base URL; kept injectable so tests can point anywhere. */
    private val baseUrl: suspend () -> String
) : TokenRefresher, ProfileLoader, AuthBackend {

    sealed class AuthCallResult<out T> {
        data class Ok<T>(val value: T) : AuthCallResult<T>()
        /** The request was understood but refused (bad credentials, taken email, ...). */
        data class Rejected(val code: String, val message: String) : AuthCallResult<Nothing>()
        /** 429 — the backend is throttling login attempts. */
        data class RateLimited(val message: String) : AuthCallResult<Nothing>()
        /** 503 — Google sign-in or Play billing isn't configured server-side. */
        data class NotConfigured(val message: String) : AuthCallResult<Nothing>()
        /** Network/5xx/unparseable — retryable. */
        data class Transient(val message: String) : AuthCallResult<Nothing>()
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .callTimeout(45, TimeUnit.SECONDS)
        .build()

    private val jsonMediaType = "application/json; charset=utf-8".toMediaType()

    /** Implements [AuthBackend.signup]. */
    override suspend fun signup(email: String, password: String): AuthCallResult<TokenPair> =
        withContext(Dispatchers.IO) {
            val body = JsonObject()
                .put("email", email.trim())
                .put("password", password)
                .toString()
            when (val r = post("/v1/auth/signup", body, bearer = null)) {
                is AuthCallResult.Ok -> parseTokenPair(r.value, "signup")
                else -> r as AuthCallResult<TokenPair>
            }
        }

    /** Implements [AuthBackend.login]. */
    override suspend fun login(email: String, password: String): AuthCallResult<TokenPair> =
        withContext(Dispatchers.IO) {
            val body = JsonObject()
                .put("email", email.trim())
                .put("password", password)
                .toString()
            when (val r = post("/v1/auth/login", body, bearer = null)) {
                is AuthCallResult.Ok -> parseTokenPair(r.value, "login")
                else -> r as AuthCallResult<TokenPair>
            }
        }

    /** Refresh-token rotation. Implements [TokenRefresher] for [SessionManager]. */
    override suspend fun refreshTokens(refreshToken: String): AuthCallResult<TokenPair> =
        withContext(Dispatchers.IO) {
            val body = JsonObject().put("refresh_token", refreshToken).toString()
            when (val r = post("/v1/auth/refresh", body, bearer = null)) {
                is AuthCallResult.Ok -> parseTokenPair(r.value, "refresh")
                else -> r as AuthCallResult<TokenPair>
            }
        }

    /** Best-effort server-side revocation; the local session is cleared regardless. */
    override suspend fun logout(refreshToken: String): AuthCallResult<Unit> =
        withContext(Dispatchers.IO) {
            val body = JsonObject().put("refresh_token", refreshToken).toString()
            when (val r = post("/v1/auth/logout", body, bearer = null)) {
                is AuthCallResult.Ok -> AuthCallResult.Ok(Unit)
                else -> r as AuthCallResult<Unit>
            }
        }

    suspend fun google(idToken: String): AuthCallResult<TokenPair> =
        withContext(Dispatchers.IO) {
            val body = JsonObject().put("id_token", idToken).toString()
            when (val r = post("/v1/auth/google", body, bearer = null)) {
                is AuthCallResult.Ok -> parseTokenPair(r.value, "google")
                else -> r as AuthCallResult<TokenPair>
            }
        }

    /** Implements [AuthBackend.googleSignIn]. */
    override suspend fun googleSignIn(idToken: String): AuthCallResult<TokenPair> =
        google(idToken)

    /** Implements [ProfileLoader] for [SessionManager]. */
    override suspend fun loadProfile(accessToken: String): MeProfile? =
        withContext(Dispatchers.IO) {
            when (val r = get("/v1/me", accessToken)) {
                is AuthCallResult.Ok -> MeProfile.fromJson(r.value)
                else -> null
            }
        }

    suspend fun verifyPurchase(
        accessToken: String,
        packageName: String,
        productId: String,
        purchaseToken: String
    ): AuthCallResult<BillingVerification> = withContext(Dispatchers.IO) {
        val body = JsonObject()
            .put("package_name", packageName)
            .put("product_id", productId)
            .put("purchase_token", purchaseToken)
            .toString()
        when (val r = post("/v1/billing/verify", body, bearer = accessToken)) {
            is AuthCallResult.Ok -> {
                val parsed = BillingVerification.fromJson(r.value)
                if (parsed != null) AuthCallResult.Ok(parsed)
                else AuthCallResult.Transient("could not parse billing response")
            }
            else -> r as AuthCallResult<BillingVerification>
        }
    }

    // ------------------------------------------------------------------
    // Transport
    // ------------------------------------------------------------------

    private suspend fun post(
        path: String,
        jsonBody: String,
        bearer: String?
    ): AuthCallResult<JsonObject> {
        val base = try {
            baseUrl()
        } catch (e: Exception) {
            return AuthCallResult.Transient("cannot read backend URL: ${e.message}")
        }
        val requestBuilder = Request.Builder()
            .url("$base$path")
            .post(jsonBody.toRequestBody(jsonMediaType))
            .header("Accept", "application/json")
        if (bearer != null) requestBuilder.header("Authorization", "Bearer $bearer")
        return try {
            client.newCall(requestBuilder.build()).execute().use { response ->
                val payload = response.body?.string().orEmpty()
                Log.d(TAG, "POST $path -> ${response.code}")
                classifyResponse(response.code, payload)
            }
        } catch (e: Exception) {
            AuthCallResult.Transient(e.message ?: e.javaClass.simpleName)
        }
    }

    private suspend fun get(path: String, bearer: String): AuthCallResult<JsonObject> {
        val base = try {
            baseUrl()
        } catch (e: Exception) {
            return AuthCallResult.Transient("cannot read backend URL: ${e.message}")
        }
        val request = Request.Builder()
            .url("$base$path")
            .get()
            .header("Accept", "application/json")
            .header("Authorization", "Bearer $bearer")
            .build()
        return try {
            client.newCall(request).execute().use { response ->
                val payload = response.body?.string().orEmpty()
                Log.d(TAG, "GET $path -> ${response.code}")
                classifyResponse(response.code, payload)
            }
        } catch (e: Exception) {
            AuthCallResult.Transient(e.message ?: e.javaClass.simpleName)
        }
    }

    private fun parseTokenPair(
        obj: JsonObject,
        op: String
    ): AuthCallResult<TokenPair> {
        val pair = TokenPair.fromJson(obj, System.currentTimeMillis())
        return if (pair != null) AuthCallResult.Ok(pair)
        else AuthCallResult.Transient("could not parse $op response")
    }

    companion object {
        private const val TAG = "AuthApi"

        /**
         * Pure (code, body) -> AuthCallResult<JsonObject>. JVM-testable.
         * 2xx (incl. 201/204) -> Ok; 401/409/422 -> Rejected with the
         * backend's machine-readable code; 429 -> RateLimited; 503 ->
         * NotConfigured (Google/billing not set up server-side); anything
         * else -> Transient.
         */
        fun classifyResponse(code: Int, payload: String): AuthCallResult<JsonObject> {
            if (code in 200..299) {
                val obj = try {
                    JsonObject(payload.ifEmpty { "{}" })
                } catch (_: Exception) {
                    JsonObject()
                }
                return AuthCallResult.Ok(obj)
            }
            val obj = try {
                JsonObject(payload)
            } catch (_: Exception) {
                null
            }
            val detail = obj?.optObject("detail")
            val message = detail?.optString("message")
                ?: obj?.optString("message")
                ?: obj?.optString("detail")
                ?: payload.ifEmpty { "request failed" }
            // Backend error bodies are {"detail": {"code": ..., "message": ...}};
            // some are {"detail": "plain string"}.
            val codeStr = detail?.optString("code") ?: "HTTP_$code"
            return when (code) {
                429 -> AuthCallResult.RateLimited(message)
                503 -> AuthCallResult.NotConfigured(message)
                in 400..499 -> AuthCallResult.Rejected(codeStr, message)
                else -> AuthCallResult.Transient("backend returned HTTP $code: $message")
            }
        }
    }
}

/** Supplies a fresh token pair from a refresh token. Split out so [SessionManager] is unit-testable. */
interface TokenRefresher {
    suspend fun refreshTokens(refreshToken: String): AuthApi.AuthCallResult<TokenPair>
}

/** Loads the profile for an access token. Split out so [SessionManager] is unit-testable. */
interface ProfileLoader {
    suspend fun loadProfile(accessToken: String): MeProfile?
}
