package dev.reelmemory.app.billing

import dev.reelmemory.app.auth.AuthApi
import dev.reelmemory.app.auth.BillingVerification
import dev.reelmemory.app.auth.SessionManager
import dev.reelmemory.app.data.SettingsStore

/**
 * Google Play Billing seam.
 *
 * The production implementation needs `com.android.billingclient:billing-ktx`
 * (see the Android README's "Publisher setup" for the drop-in implementation
 * and the Gradle line). The flow this repository orchestrates:
 *
 * 1. Query the Pro subscription product (price shown on the paywall).
 * 2. Launch the Play purchase flow.
 * 3. On a PURCHASED purchase, POST {package_name, product_id, purchase_token}
 *    to /v1/billing/verify. The backend checks the purchase with the Play
 *    Developer API and grants Pro until expiryTimeMillis. A PENDING payment
 *    never grants Pro — the user is told honestly to wait for it to clear.
 * 4. Refresh /v1/me so the UI reflects the new tier immediately.
 *
 * Renewals/cancellations/expiry are handled by re-verification: the client
 * re-sends the purchase token on app start when the profile says Pro.
 */
data class ProProduct(
    val productId: String,
    val title: String,
    /** Localized price text from Play, e.g. "$4.99/mo". */
    val priceText: String
)

sealed class ProductQueryResult {
    data class Found(val product: ProProduct) : ProductQueryResult()
    data class Unavailable(val reason: String) : ProductQueryResult()
}

sealed class PurchaseResult {
    /** A completed purchase. Only this state goes to /v1/billing/verify. */
    data class Purchased(
        val packageName: String,
        val productId: String,
        val purchaseToken: String
    ) : PurchaseResult()

    /** Play reports the payment as pending — never sent to the backend. */
    data object Pending : PurchaseResult()
    data object Cancelled : PurchaseResult()
    data class Error(val message: String) : PurchaseResult()
    data class Unavailable(val reason: String) : PurchaseResult()
}

interface BillingPort {
    val isAvailable: Boolean
    suspend fun queryProProduct(productId: String): ProductQueryResult
    /**
     * Launches the Play purchase UI. The activity is typed as [Any] (an
     * `android.app.Activity` in production; the real Play implementation
     * casts it back) so the purchase orchestration stays JVM-testable —
     * instantiating an Activity is impossible on a plain JVM.
     */
    suspend fun launchPurchase(activity: Any, productId: String): PurchaseResult
}

/** Placeholder until the publisher wires billingclient. Every call reports honestly. */
class StubBillingPort : BillingPort {
    override val isAvailable: Boolean = false

    private fun unavailable() =
        "Play Billing isn't wired in this build yet — " +
            "see the README's publisher setup to enable Pro purchases."

    override suspend fun queryProProduct(productId: String): ProductQueryResult =
        ProductQueryResult.Unavailable(unavailable())

    override suspend fun launchPurchase(activity: Any, productId: String): PurchaseResult =
        PurchaseResult.Unavailable(unavailable())
}

sealed class BillingFlowResult {
    data class Success(val verification: BillingVerification) : BillingFlowResult()
    /** The payment is pending with Google Play — Pro unlocks once it clears. */
    data object PendingPayment : BillingFlowResult()
    data object Cancelled : BillingFlowResult()
    data class BillingUnavailable(val reason: String) : BillingFlowResult()
    data class Failed(val message: String) : BillingFlowResult()
}

/**
 * Orchestrates the purchase → server verification → profile refresh flow.
 * Pure orchestration over the [BillingPort] / [VerifyApi] seams, so it is
 * JVM-unit-testable with fakes.
 */
class BillingRepository(
    private val port: BillingPort,
    private val verifyApi: VerifyApi,
    private val session: SessionManager,
    /**
     * Resolves the Play product ID. Defaults to the constant; production
     * passes `{ settings.getPlayProductId() }`. A lambda (not SettingsStore)
     * so this stays JVM-testable without a Context.
     */
    private val productIdProvider: suspend () -> String = { SettingsStore.DEFAULT_PLAY_PRODUCT_ID }
) {
    /** Verifies a Play purchase with the backend. Split out for testability. */
    interface VerifyApi {
        suspend fun verify(
            accessToken: String,
            packageName: String,
            productId: String,
            purchaseToken: String
        ): AuthApi.AuthCallResult<BillingVerification>
    }

    suspend fun productId(): String = try {
        productIdProvider()
    } catch (_: Exception) {
        SettingsStore.DEFAULT_PLAY_PRODUCT_ID
    }

    suspend fun queryProduct(): ProductQueryResult {
        if (!port.isAvailable) {
            return ProductQueryResult.Unavailable(
                "Play Billing isn't available on this device or build."
            )
        }
        return port.queryProProduct(productId())
    }

    /**
     * Full Pro purchase flow. Must be called from an Activity context
     * (Play launches its own UI). Typed as [Any] — see
     * [BillingPort.launchPurchase]; production passes the real Activity.
     */
    suspend fun purchase(activity: Any): BillingFlowResult {
        if (!port.isAvailable) {
            return BillingFlowResult.BillingUnavailable(
                "Play Billing isn't available on this device or build."
            )
        }
        val productId = productId()
        return when (val purchase = port.launchPurchase(activity, productId)) {
            is PurchaseResult.Purchased -> verifyWithBackend(purchase)
            PurchaseResult.Pending -> BillingFlowResult.PendingPayment
            PurchaseResult.Cancelled -> BillingFlowResult.Cancelled
            is PurchaseResult.Error -> BillingFlowResult.Failed(purchase.message)
            is PurchaseResult.Unavailable -> BillingFlowResult.BillingUnavailable(purchase.reason)
        }
    }

    /**
     * Re-sends a known purchase token (e.g. on app start for Pro users) so
     * renewals, cancellations and expiry refresh the backend entitlement.
     */
    suspend fun reverify(purchase: PurchaseResult.Purchased): BillingFlowResult =
        verifyWithBackend(purchase)

    private suspend fun verifyWithBackend(
        purchase: PurchaseResult.Purchased
    ): BillingFlowResult {
        val token = session.validAccessToken()
            ?: return BillingFlowResult.Failed("You're signed out — sign in and try again.")
        return when (
            val r = verifyApi.verify(
                accessToken = token,
                packageName = purchase.packageName,
                productId = purchase.productId,
                purchaseToken = purchase.purchaseToken
            )
        ) {
            is AuthApi.AuthCallResult.Ok -> {
                session.refreshProfile()
                BillingFlowResult.Success(r.value)
            }
            is AuthApi.AuthCallResult.NotConfigured -> BillingFlowResult.Failed(
                "Pro purchases aren't set up on the server yet (billing_not_configured). " +
                    "See the backend README's publisher setup."
            )
            is AuthApi.AuthCallResult.Rejected ->
                BillingFlowResult.Failed(r.message.ifEmpty { "The purchase wasn't accepted (${r.code})." })
            is AuthApi.AuthCallResult.RateLimited ->
                BillingFlowResult.Failed("Too many attempts — try again in a few minutes.")
            is AuthApi.AuthCallResult.Transient ->
                BillingFlowResult.Failed(r.message.ifEmpty { "Couldn't reach the server." })
        }
    }
}
