package dev.reelmemory.app.auth

import dev.reelmemory.app.json.JsonObject

/**
 * Pure auth/entitlement models and parsers. No Android framework, no network —
 * everything here is JVM-unit-testable.
 *
 * Backend shapes (see backend README "Auth, entitlements & selling Pro"):
 * - TokenPair: {"access_token", "refresh_token", "token_type": "bearer", "expires_in": seconds}
 * - GET /v1/me: {"id", "email", "tier": "free"|"pro", "pro_expires_at"?, "google_linked",
 *   "has_password", "usage": {bucket: {"used", "limit", "resets_at"}}}
 * - 402 quota: {"code": "quota_exceeded", "tier", "bucket", "limit", "used", "resets_at"}
 */

/** What an HTTP status means for the session, before any retry logic. */
sealed class AuthOutcome {
    /** Normal response — classify by status as usual. */
    data object Ok : AuthOutcome()

    /** 401: the access token is dead. The session layer tries one silent
     *  refresh; if that fails the user must sign in again. */
    data object Unauthorized : AuthOutcome()

    /** 402: free quota exhausted. Show the Pro paywall. */
    data class QuotaExceeded(val info: QuotaExceededInfo) : AuthOutcome()
}

/**
 * Pure classifier for the two session-level statuses. Returns null for any
 * other code (the caller classifies those itself).
 */
fun parseAuthOutcome(code: Int, payload: String): AuthOutcome? = when (code) {
    401 -> AuthOutcome.Unauthorized
    402 -> AuthOutcome.QuotaExceeded(
        QuotaExceededInfo.fromJson(payload) ?: QuotaExceededInfo(
            tier = "free",
            bucket = "unknown",
            limit = 0,
            used = 0,
            resetsAt = ""
        )
    )
    else -> null
}

data class TokenPair(
    val accessToken: String,
    val refreshToken: String,
    /** Epoch millis at which the access token should be considered stale. */
    val accessExpiresAtMillis: Long
) {
    /** True when the token is expired or will expire within [marginMillis]. */
    fun isExpired(nowMillis: Long, marginMillis: Long = 60_000): Boolean =
        nowMillis + marginMillis >= accessExpiresAtMillis

    fun toJson(): JsonObject = JsonObject()
        .put("access_token", accessToken)
        .put("refresh_token", refreshToken)
        .put("access_expires_at", accessExpiresAtMillis)

    companion object {
        /**
         * Builds a pair from the backend's TokenPair body. `expires_in` is
         * seconds until the access token expires; we shave a small safety
         * margin so proactive refresh beats the real expiry.
         */
        fun fromJson(obj: JsonObject, nowMillis: Long): TokenPair? {
            val access = obj.optString("access_token").takeIf { it.isNotEmpty() } ?: return null
            val refresh = obj.optString("refresh_token").takeIf { it.isNotEmpty() } ?: return null
            val expiresInSec = obj.optLong("expires_in", 900).coerceAtLeast(60)
            val safetyMarginSec = 30L
            return TokenPair(
                accessToken = access,
                refreshToken = refresh,
                accessExpiresAtMillis = nowMillis + (expiresInSec - safetyMarginSec) * 1000
            )
        }

        fun fromStoredJson(raw: String): TokenPair? = try {
            val obj = JsonObject(raw)
            TokenPair(
                accessToken = obj.getString("access_token"),
                refreshToken = obj.getString("refresh_token"),
                accessExpiresAtMillis = obj.getLong("access_expires_at")
            )
        } catch (_: Exception) {
            null
        }
    }
}

data class QuotaBucket(
    val name: String,
    val used: Int,
    val limit: Int,
    /** ISO-8601 timestamp string from the backend (UTC). Kept raw; the UI formats it. */
    val resetsAt: String
) {
    val fraction: Float =
        if (limit > 0) (used.toFloat() / limit).coerceIn(0f, 1f) else 0f
    val exhausted: Boolean = limit > 0 && used >= limit
}

data class MeProfile(
    val id: String,
    val email: String,
    val tier: String,
    val proExpiresAt: String?,
    val googleLinked: Boolean,
    val hasPassword: Boolean,
    val usage: List<QuotaBucket>
) {
    val isPro: Boolean get() = tier == "pro"

    companion object {
        fun fromJson(obj: JsonObject): MeProfile? = try {
            val usageObj = obj.optObject("usage") ?: JsonObject()
            val buckets = mutableListOf<QuotaBucket>()
            val names = usageObj.keys()
            while (names.hasNext()) {
                val name = names.next()
                val b = usageObj.optObject(name) ?: continue
                buckets.add(
                    QuotaBucket(
                        name = name,
                        used = b.optInt("used", 0),
                        limit = b.optInt("limit", 0),
                        resetsAt = b.optString("resets_at", "")
                    )
                )
            }
            // Stable display order: captures, questions, verifications, then anything else.
            val order = mapOf("captures" to 0, "questions" to 1, "verifications" to 2)
            buckets.sortBy { order[it.name] ?: 99 }
            MeProfile(
                id = obj.optString("id"),
                email = obj.optString("email"),
                tier = obj.optString("tier", "free"),
                proExpiresAt = obj.optString("pro_expires_at").takeIf { it.isNotEmpty() },
                googleLinked = obj.optBoolean("google_linked", false),
                hasPassword = obj.optBoolean("has_password", false),
                usage = buckets
            )
        } catch (_: Exception) {
            null
        }
    }
}

data class QuotaExceededInfo(
    val tier: String,
    val bucket: String,
    val limit: Int,
    val used: Int,
    /** ISO-8601 timestamp string from the backend (UTC). Kept raw; the UI formats it. */
    val resetsAt: String
) {
    companion object {
        fun fromJson(payload: String): QuotaExceededInfo? {
            return try {
                val obj = JsonObject(payload)
                if (obj.optString("code") != "quota_exceeded") return null
                QuotaExceededInfo(
                    tier = obj.optString("tier", "free"),
                    bucket = obj.optString("bucket", "unknown"),
                    limit = obj.optInt("limit", 0),
                    used = obj.optInt("used", 0),
                    resetsAt = obj.optString("resets_at", "")
                )
            } catch (_: Exception) {
                null
            }
        }
    }
}

data class BillingVerification(
    val tier: String,
    val proExpiresAt: String?,
    val autoRenewing: Boolean,
    val orderId: String?
) {
    companion object {
        fun fromJson(obj: JsonObject): BillingVerification? = try {
            BillingVerification(
                tier = obj.optString("tier", "free"),
                proExpiresAt = obj.optString("pro_expires_at").takeIf { it.isNotEmpty() },
                autoRenewing = obj.optBoolean("auto_renewing", false),
                orderId = obj.optString("order_id").takeIf { it.isNotEmpty() }
            )
        } catch (_: Exception) {
            null
        }
    }
}

/**
 * Client-side mirrors of the backend's credential policy (backend
 * `app/auth/passwords.py`: email regex, 10–128 char passwords, max 320-char
 * email). The backend re-validates everything; this just gives instant UI
 * feedback. Returns an error message, or null when the value is acceptable.
 */
object AuthValidation {
    private val EMAIL_RE = Regex("^[A-Za-z0-9._%+\\-]+@[A-Za-z0-9.\\-]+\\.[A-Za-z]{2,}$")
    const val MIN_PASSWORD_LEN = 10
    const val MAX_PASSWORD_LEN = 128
    const val MAX_EMAIL_LEN = 320

    fun validateEmail(raw: String): String? {
        val email = raw.trim()
        if (email.isEmpty()) return "Enter your email address."
        if (email.length > MAX_EMAIL_LEN || !EMAIL_RE.matches(email)) {
            return "That doesn't look like an email address."
        }
        return null
    }

    fun validatePassword(password: String): String? {
        if (password.length < MIN_PASSWORD_LEN) {
            return "Password must be at least $MIN_PASSWORD_LEN characters."
        }
        if (password.length > MAX_PASSWORD_LEN) {
            return "Password must be at most $MAX_PASSWORD_LEN characters."
        }
        return null
    }

    fun validatePasswordConfirm(password: String, confirm: String): String? {
        if (password != confirm) return "Passwords don't match."
        return null
    }
}
