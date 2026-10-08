package dev.reelmemory.app.auth

import dev.reelmemory.app.billing.BillingFlowResult
import dev.reelmemory.app.billing.BillingPort
import dev.reelmemory.app.billing.BillingRepository
import dev.reelmemory.app.billing.ProProduct
import dev.reelmemory.app.billing.ProductQueryResult
import dev.reelmemory.app.billing.PurchaseResult
import dev.reelmemory.app.billing.StubBillingPort
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import dev.reelmemory.app.json.JsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

// ---------------------------------------------------------------------------
// Fakes
// ---------------------------------------------------------------------------

private class FakeTokenStore : TokenStore {
    var pair: TokenPair? = null
    override fun save(pair: TokenPair) { this.pair = pair }
    override fun load(): TokenPair? = pair
    override fun clear() { pair = null }
}

private class FakeAuthBackend : AuthBackend {
    var signupResult: AuthApi.AuthCallResult<TokenPair> = AuthApi.AuthCallResult.Transient("unset")
    var loginResult: AuthApi.AuthCallResult<TokenPair> = AuthApi.AuthCallResult.Transient("unset")
    var googleResult: AuthApi.AuthCallResult<TokenPair> = AuthApi.AuthCallResult.Transient("unset")
    var refreshResult: AuthApi.AuthCallResult<TokenPair> = AuthApi.AuthCallResult.Transient("unset")
    var profile: MeProfile? = null
    var loggedOutToken: String? = null
    var refreshCalls = 0

    override suspend fun signup(email: String, password: String) = signupResult
    override suspend fun login(email: String, password: String) = loginResult
    override suspend fun googleSignIn(idToken: String) = googleResult
    override suspend fun refreshTokens(refreshToken: String): AuthApi.AuthCallResult<TokenPair> {
        refreshCalls++
        return refreshResult
    }
    override suspend fun loadProfile(accessToken: String): MeProfile? = profile
    override suspend fun logout(refreshToken: String): AuthApi.AuthCallResult<Unit> {
        loggedOutToken = refreshToken
        return AuthApi.AuthCallResult.Ok(Unit)
    }
}

private class FakeBillingPort(
    override val isAvailable: Boolean = true,
    var product: ProductQueryResult =
        ProductQueryResult.Found(ProProduct("pro_monthly", "Reel Memory Pro", "$4.99/mo")),
    var purchaseResult: PurchaseResult = PurchaseResult.Cancelled
) : BillingPort {
    override suspend fun queryProProduct(productId: String): ProductQueryResult = product
    override suspend fun launchPurchase(activity: Any, productId: String): PurchaseResult =
        purchaseResult
}

private class FakeVerifyApi(
    var result: AuthApi.AuthCallResult<BillingVerification> =
        AuthApi.AuthCallResult.Transient("unset")
) : BillingRepository.VerifyApi {
    var calls = 0
    var lastPurchaseToken: String? = null
    override suspend fun verify(
        accessToken: String,
        packageName: String,
        productId: String,
        purchaseToken: String
    ): AuthApi.AuthCallResult<BillingVerification> {
        calls++
        lastPurchaseToken = purchaseToken
        return result
    }
}

private const val NOW = 1_700_000_000_000L

/** Access token valid for ~10 more minutes. */
private fun freshPair() = TokenPair("access-1", "refresh-1", NOW + 600_000)

/** Access token already stale (past the 60 s proactive margin). */
private fun stalePair() = TokenPair("access-old", "refresh-old", NOW - 1)

private fun meProfile(tier: String = "free") = MeProfile(
    id = "u1",
    email = "ram@example.com",
    tier = tier,
    proExpiresAt = if (tier == "pro") "2026-10-22" else null,
    googleLinked = false,
    hasPassword = true,
    usage = listOf(
        QuotaBucket("captures", 3, 20, "2026-10-01T00:00:00+00:00"),
        QuotaBucket("questions", 50, 50, "2026-09-23T00:00:00+00:00")
    )
)

private fun signedInSession(
    store: FakeTokenStore = FakeTokenStore(),
    backend: FakeAuthBackend = FakeAuthBackend()
): SessionManager {
    backend.loginResult = AuthApi.AuthCallResult.Ok(freshPair())
    backend.profile = meProfile()
    val session = SessionManager(store, backend, clock = { NOW })
    runBlocking { session.login("ram@example.com", "password1234") }
    return session
}

// ---------------------------------------------------------------------------
// AuthValidation
// ---------------------------------------------------------------------------

class AuthValidationTest {

    @Test
    fun `valid email passes`() {
        assertNull(AuthValidation.validateEmail("ram@example.com"))
    }

    @Test
    fun `blank and malformed emails fail`() {
        assertTrue(AuthValidation.validateEmail("").toString().isNotEmpty())
        assertTrue(AuthValidation.validateEmail("not-an-email")!!.isNotEmpty())
        assertTrue(AuthValidation.validateEmail("a@b")!!.isNotEmpty())
    }

    @Test
    fun `password must be 10 to 128 chars`() {
        assertTrue(AuthValidation.validatePassword("short9chr")!!.isNotEmpty())
        assertNull(AuthValidation.validatePassword("exactly10!"))
        assertNull(AuthValidation.validatePassword("a".repeat(128)))
        assertTrue(AuthValidation.validatePassword("a".repeat(129))!!.isNotEmpty())
    }

    @Test
    fun `password confirmation must match`() {
        assertNull(AuthValidation.validatePasswordConfirm("password1234", "password1234"))
        assertTrue(
            AuthValidation.validatePasswordConfirm("password1234", "password1235")!!.isNotEmpty()
        )
    }
}

// ---------------------------------------------------------------------------
// Parsers
// ---------------------------------------------------------------------------

class AuthParsersTest {

    @Test
    fun `me profile parses tier and ordered usage`() {
        val json = JsonObject(
            """{
              "id": "u1", "email": "ram@example.com", "tier": "pro",
              "pro_expires_at": "2026-10-22T00:00:00Z",
              "google_linked": true, "has_password": false,
              "usage": {
                "verifications": {"used": 1, "limit": 10, "resets_at": "2026-09-23T00:00:00Z"},
                "captures": {"used": 3, "limit": 200, "resets_at": "2026-10-01T00:00:00Z"},
                "questions": {"used": 7, "limit": 500, "resets_at": "2026-09-23T00:00:00Z"}
              }
            }"""
        )
        val profile = MeProfile.fromJson(json)!!
        assertEquals("ram@example.com", profile.email)
        assertTrue(profile.isPro)
        assertEquals("2026-10-22T00:00:00Z", profile.proExpiresAt)
        assertTrue(profile.googleLinked)
        assertFalse(profile.hasPassword)
        // Stable order: captures, questions, verifications.
        assertEquals(listOf("captures", "questions", "verifications"), profile.usage.map { it.name })
        assertEquals(0.015f, profile.usage[0].fraction, 0.001f)
    }

    @Test
    fun `quota info parses from 402 body`() {
        val outcome = parseAuthOutcome(
            402,
            """{"code":"quota_exceeded","tier":"free","bucket":"questions",
                 "limit":50,"used":50,"resets_at":"2026-09-23T00:00:00+00:00"}"""
        )
        val info = (outcome as AuthOutcome.QuotaExceeded).info
        assertEquals("questions", info.bucket)
        assertEquals(50, info.used)
        assertEquals(50, info.limit)
        assertEquals("2026-09-23T00:00:00+00:00", info.resetsAt)
    }

    @Test
    fun `401 classifies as unauthorized, other codes as null`() {
        assertTrue(parseAuthOutcome(401, "") is AuthOutcome.Unauthorized)
        assertNull(parseAuthOutcome(200, "{}"))
        assertNull(parseAuthOutcome(500, "boom"))
    }

    @Test
    fun `malformed 402 body still yields a quota event with defaults`() {
        val outcome = parseAuthOutcome(402, "not json") as AuthOutcome.QuotaExceeded
        assertEquals("unknown", outcome.info.bucket)
    }
}

class AuthApiClassifyTest {

    @Test
    fun `2xx is ok`() {
        val r = AuthApi.classifyResponse(201, """{"access_token":"a"}""")
        assertTrue(r is AuthApi.AuthCallResult.Ok)
    }

    @Test
    fun `401 carries the backend error code`() {
        val r = AuthApi.classifyResponse(
            401,
            """{"detail":{"code":"invalid_credentials","message":"nope"}}"""
        ) as AuthApi.AuthCallResult.Rejected
        assertEquals("invalid_credentials", r.code)
    }

    @Test
    fun `429 is rate limited and 503 is not configured`() {
        assertTrue(AuthApi.classifyResponse(429, "slow down") is AuthApi.AuthCallResult.RateLimited)
        assertTrue(
            AuthApi.classifyResponse(503, "no billing") is AuthApi.AuthCallResult.NotConfigured
        )
    }

    @Test
    fun `5xx is transient`() {
        assertTrue(AuthApi.classifyResponse(500, "boom") is AuthApi.AuthCallResult.Transient)
    }
}

// ---------------------------------------------------------------------------
// SessionManager
// ---------------------------------------------------------------------------

class SessionManagerTest {

    @Test
    fun `login success stores tokens and signs in with profile`() = runBlocking {
        val store = FakeTokenStore()
        val backend = FakeAuthBackend()
        backend.loginResult = AuthApi.AuthCallResult.Ok(freshPair())
        backend.profile = meProfile()
        val session = SessionManager(store, backend, clock = { NOW })

        val result = session.login("ram@example.com", "password1234")

        assertTrue(result is SignInResult.Ok)
        assertEquals("access-1", store.pair?.accessToken)
        assertEquals(meProfile(), (session.authState.value as AuthState.SignedIn).profile)
    }

    @Test
    fun `login rejection maps to friendly errors`() = runBlocking {
        val backend = FakeAuthBackend()
        val session = SessionManager(FakeTokenStore(), backend, clock = { NOW })

        backend.loginResult =
            AuthApi.AuthCallResult.Rejected("invalid_credentials", "Wrong email or password.")
        assertTrue(
            (session.login("a@b.com", "password1234") as SignInResult.Error)
                .message.contains("Wrong email")
        )

        backend.signupResult = AuthApi.AuthCallResult.Rejected("email_taken", "taken")
        assertTrue(
            (session.signUp("a@b.com", "password1234") as SignInResult.Error)
                .message.contains("already registered")
        )

        backend.loginResult = AuthApi.AuthCallResult.RateLimited("slow")
        assertTrue(
            (session.login("a@b.com", "password1234") as SignInResult.Error)
                .message.contains("Too many attempts")
        )
    }

    @Test
    fun `authorizedHttp attaches the bearer token`() = runBlocking {
        val session = signedInSession()
        var seen: String? = "unset"
        val resp = session.authorizedHttp { bearer ->
            seen = bearer
            HttpResponse(200, "{}")
        }
        assertEquals(200, resp.code)
        assertEquals("access-1", seen)
    }

    @Test
    fun `401 triggers exactly one refresh and a single retry`() = runBlocking {
        val store = FakeTokenStore()
        val backend = FakeAuthBackend()
        val session = signedInSession(store, backend)
        backend.refreshResult =
            AuthApi.AuthCallResult.Ok(TokenPair("access-2", "refresh-2", NOW + 600_000))

        val seen = mutableListOf<String?>()
        var calls = 0
        val resp = session.authorizedHttp { bearer ->
            calls++
            seen += bearer
            if (calls == 1) HttpResponse(401, "expired") else HttpResponse(200, "{}")
        }

        assertEquals(200, resp.code)
        assertEquals(1, backend.refreshCalls)
        assertEquals(listOf("access-1", "access-2"), seen)
        assertEquals("access-2", store.pair?.accessToken)
    }

    @Test
    fun `401 forces a refresh even when the token is not expired`() = runBlocking {
        // Regression test: a 401 on a fresh-looking token (server-side
        // revocation) must still attempt the refresh instead of retrying
        // with the same dead token.
        val store = FakeTokenStore()
        val backend = FakeAuthBackend()
        val session = signedInSession(store, backend)
        backend.refreshResult =
            AuthApi.AuthCallResult.Ok(TokenPair("access-2", "refresh-2", NOW + 600_000))

        val seen = mutableListOf<String?>()
        val resp = session.authorizedHttp { bearer ->
            seen += bearer
            HttpResponse(401, "revoked")
        }

        assertEquals(401, resp.code)
        assertEquals(1, backend.refreshCalls)
        assertEquals(listOf("access-1", "access-2"), seen)
        // Still failing after the refresh: the session is dead, route to sign-in.
        assertTrue(session.authState.value is AuthState.SignedOut)
        assertNull(store.pair)
    }

    @Test
    fun `401 with dead refresh token clears the session`() = runBlocking {
        val store = FakeTokenStore()
        val backend = FakeAuthBackend()
        val session = signedInSession(store, backend)
        backend.refreshResult = AuthApi.AuthCallResult.Rejected("invalid_grant", "dead")

        val resp = session.authorizedHttp { HttpResponse(401, "expired") }

        assertEquals(401, resp.code)
        assertEquals(1, backend.refreshCalls)
        assertTrue(session.authState.value is AuthState.SignedOut)
        assertNull(store.pair)
    }

    @Test
    fun `402 emits a quota event without retrying`() = runBlocking {
        val session = signedInSession()
        val body =
            """{"code":"quota_exceeded","tier":"free","bucket":"captures",
                 "limit":20,"used":20,"resets_at":"2026-10-01T00:00:00+00:00"}"""
        var calls = 0
        val resp = session.authorizedHttp {
            calls++
            HttpResponse(402, body)
        }

        assertEquals(402, resp.code)
        assertEquals(1, calls) // no retry on 402
        val info = session.quotaEvents.first()
        assertEquals("captures", info.bucket)
        assertEquals(20, info.used)
    }

    @Test
    fun `restore refreshes a stale token pair`() = runBlocking {
        val store = FakeTokenStore()
        store.save(stalePair())
        val backend = FakeAuthBackend()
        backend.refreshResult =
            AuthApi.AuthCallResult.Ok(TokenPair("access-9", "refresh-9", NOW + 600_000))
        backend.profile = meProfile()
        val session = SessionManager(store, backend, clock = { NOW })

        assertTrue(session.restore())
        assertEquals(1, backend.refreshCalls)
        assertEquals("access-9", store.pair?.accessToken)
        assertTrue(session.authState.value is AuthState.SignedIn)
    }

    @Test
    fun `restore with no stored tokens returns false and stays signed out`() = runBlocking {
        val session = SessionManager(FakeTokenStore(), FakeAuthBackend(), clock = { NOW })
        assertFalse(session.restore())
        assertTrue(session.authState.value is AuthState.SignedOut)
    }

    @Test
    fun `signOut revokes server-side and clears local state`() = runBlocking {
        val store = FakeTokenStore()
        val backend = FakeAuthBackend()
        val session = signedInSession(store, backend)

        session.signOut()

        assertEquals("refresh-1", backend.loggedOutToken)
        assertNull(store.pair)
        assertTrue(session.authState.value is AuthState.SignedOut)
    }

    @Test
    fun `google sign-in adopts the token pair`() = runBlocking {
        val store = FakeTokenStore()
        val backend = FakeAuthBackend()
        backend.googleResult = AuthApi.AuthCallResult.Ok(freshPair())
        backend.profile = meProfile()
        val session = SessionManager(store, backend, clock = { NOW })

        assertTrue(session.signInWithGoogle("google-id-token") is SignInResult.Ok)
        assertEquals("access-1", store.pair?.accessToken)
    }
}

// ---------------------------------------------------------------------------
// BillingRepository
// ---------------------------------------------------------------------------

class BillingRepositoryTest {

    private fun repo(
        session: SessionManager,
        port: FakeBillingPort = FakeBillingPort(),
        verify: FakeVerifyApi = FakeVerifyApi()
    ) = BillingRepository(
        port = port,
        verifyApi = verify,
        session = session,
        productIdProvider = { "pro_monthly" }
    )

    @Test
    fun `successful purchase verifies with backend and refreshes profile`() = runBlocking {
        val backend = FakeAuthBackend()
        val session = signedInSession(backend = backend)
        backend.profile = meProfile("pro")
        val port = FakeBillingPort(
            purchaseResult = PurchaseResult.Purchased("dev.reelmemory.app", "pro_monthly", "tok123")
        )
        val verify = FakeVerifyApi(
            AuthApi.AuthCallResult.Ok(BillingVerification("pro", "2026-10-22", true, "order1"))
        )

        val result = repo(session, port, verify).purchase(Object())

        val success = result as BillingFlowResult.Success
        assertTrue(success.verification.autoRenewing)
        assertEquals(1, verify.calls)
        assertEquals("tok123", verify.lastPurchaseToken)
        // Profile refresh picked up the new Pro tier.
        assertEquals("pro", (session.authState.value as AuthState.SignedIn).profile?.tier)
    }

    @Test
    fun `pending payment never reaches the backend`() = runBlocking {
        val session = signedInSession()
        val port = FakeBillingPort(purchaseResult = PurchaseResult.Pending)
        val verify = FakeVerifyApi()

        val result = repo(session, port, verify).purchase(Object())

        assertTrue(result is BillingFlowResult.PendingPayment)
        assertEquals(0, verify.calls)
    }

    @Test
    fun `cancelled purchase reports cancelled`() = runBlocking {
        val session = signedInSession()
        val result = repo(session).purchase(Object())
        assertTrue(result is BillingFlowResult.Cancelled)
    }

    @Test
    fun `unavailable billing port reports honestly`() = runBlocking {
        val session = signedInSession()
        val stubResult = repo(session, FakeBillingPort(isAvailable = false)).purchase(Object())
        assertTrue(stubResult is BillingFlowResult.BillingUnavailable)

        val query = repo(session, FakeBillingPort(isAvailable = false)).queryProduct()
        assertTrue(query is ProductQueryResult.Unavailable)
    }

    @Test
    fun `rejected verification surfaces the backend message`() = runBlocking {
        val session = signedInSession()
        val port = FakeBillingPort(
            purchaseResult = PurchaseResult.Purchased("dev.reelmemory.app", "pro_monthly", "tok9")
        )
        val verify = FakeVerifyApi(
            AuthApi.AuthCallResult.Rejected("purchase_not_found", "No such purchase.")
        )

        val result = repo(session, port, verify).purchase(Object())

        assertTrue((result as BillingFlowResult.Failed).message.contains("No such purchase."))
    }

    @Test
    fun `purchase while signed out fails without touching play`() = runBlocking {
        val session = SessionManager(FakeTokenStore(), FakeAuthBackend(), clock = { NOW })
        val port = FakeBillingPort(
            purchaseResult = PurchaseResult.Purchased("dev.reelmemory.app", "pro_monthly", "tok1")
        )
        val verify = FakeVerifyApi()

        val result = repo(session, port, verify).purchase(Object())

        assertTrue((result as BillingFlowResult.Failed).message.contains("signed out"))
        assertEquals(0, verify.calls)
    }

    @Test
    fun `query product returns price when available`() = runBlocking {
        val session = signedInSession()
        val result = repo(session).queryProduct() as ProductQueryResult.Found
        assertEquals("$4.99/mo", result.product.priceText)
    }

    @Test
    fun `stub billing port is unavailable with a helpful reason`() = runBlocking {
        val stub = StubBillingPort()
        assertFalse(stub.isAvailable)
        val q = stub.queryProProduct("pro_monthly") as ProductQueryResult.Unavailable
        assertTrue(q.reason.contains("README"))
    }

    @Test
    fun `stub google sign-in is unavailable with a helpful reason`() = runBlocking {
        val r = StubGoogleSignInProvider().requestIdToken() as GoogleSignInResult.Unavailable
        assertTrue(r.message.contains("README"))
    }
}
