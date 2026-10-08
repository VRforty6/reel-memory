package dev.reelmemory.app.ui

import android.app.Activity
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import dev.reelmemory.app.auth.MeProfile
import dev.reelmemory.app.auth.QuotaExceededInfo
import dev.reelmemory.app.billing.BillingFlowResult
import dev.reelmemory.app.billing.BillingRepository
import dev.reelmemory.app.billing.ProductQueryResult
import dev.reelmemory.app.billing.ProProduct
import dev.reelmemory.app.sync.SyncWorker
import kotlinx.coroutines.launch
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale

/**
 * The Pro paywall. Shown when the backend answers 402 `quota_exceeded`
 * (via [dev.reelmemory.app.auth.SessionManager.quotaEvents]) and from the
 * "Upgrade to Pro" entry point in Settings.
 *
 * [quotaInfo] may be null when opened manually — then it shows the generic
 * pitch without a specific exhausted bucket.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PaywallScreen(
    quotaInfo: QuotaExceededInfo?,
    profile: MeProfile?,
    billing: BillingRepository,
    onDismiss: () -> Unit,
    modifier: Modifier = Modifier
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var product by remember { mutableStateOf<ProProduct?>(null) }
    var productError by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    var outcome by remember { mutableStateOf<BillingFlowResult?>(null) }

    LaunchedEffect(Unit) {
        when (val q = billing.queryProduct()) {
            is ProductQueryResult.Found -> product = q.product
            is ProductQueryResult.Unavailable -> productError = q.reason
        }
    }

    Scaffold(
        modifier = modifier,
        topBar = {
            TopAppBar(
                title = { Text("Reel Memory Pro") },
                navigationIcon = {
                    IconButton(onClick = onDismiss) {
                        Icon(Icons.Filled.Close, contentDescription = "Close")
                    }
                }
            )
        }
    ) { padding ->
        Column(
            modifier = Modifier.padding(padding).fillMaxSize().padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            Text(
                "Reel Memory Pro",
                style = MaterialTheme.typography.headlineSmall,
                fontWeight = FontWeight.Bold
            )
            quotaInfo?.let { info ->
                Card(
                    colors = CardDefaults.cardColors(
                        containerColor = MaterialTheme.colorScheme.errorContainer
                    ),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text(
                        "You've used your free ${bucketLabel(info.bucket)} " +
                            "(${info.used}/${info.limit})" +
                            formatResetsAt(info.resetsAt),
                        modifier = Modifier.padding(12.dp),
                        style = MaterialTheme.typography.bodyMedium
                    )
                }
            }
            Text(
                "Pro raises every limit 10× so you can keep saving reels, " +
                    "asking questions, and verifying claims without hitting a wall.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center
            )
            profile?.let {
                Text(
                    "Current plan: ${it.tier.uppercase(Locale.getDefault())}",
                    style = MaterialTheme.typography.labelLarge,
                    fontWeight = FontWeight.SemiBold
                )
            }

            Spacer(Modifier.height(8.dp))

            when {
                busy -> {
                    CircularProgressIndicator()
                    Text("Contacting Google Play…", style = MaterialTheme.typography.bodySmall)
                }
                outcome is BillingFlowResult.Success -> {
                    val v = (outcome as BillingFlowResult.Success).verification
                    Card(
                        colors = CardDefaults.cardColors(
                            containerColor = MaterialTheme.colorScheme.primaryContainer
                        ),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text(
                            "You're Pro now — limits refresh shortly. Happy deciding.",
                            modifier = Modifier.padding(12.dp),
                            style = MaterialTheme.typography.bodyMedium
                        )
                    }
                    if (v.autoRenewing) {
                        Text(
                            "Subscription auto-renews.",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                    Button(
                        onClick = {
                            scope.launch {
                                SyncWorker.retryQuotaBlocked(context)
                                onDismiss()
                            }
                        },
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text("Retry queued items")
                    }
                }
                outcome is BillingFlowResult.PendingPayment -> {
                    Text(
                        "Your payment is pending with Google Play. Pro unlocks " +
                            "automatically once it clears — no need to pay twice.",
                        style = MaterialTheme.typography.bodyMedium,
                        textAlign = TextAlign.Center
                    )
                    OutlinedButton(onClick = { outcome = null }, modifier = Modifier.fillMaxWidth()) {
                        Text("Back")
                    }
                }
                outcome is BillingFlowResult.Cancelled -> {
                    Text("Purchase cancelled — nothing was charged.")
                    OutlinedButton(onClick = { outcome = null }, modifier = Modifier.fillMaxWidth()) {
                        Text("Try again")
                    }
                }
                outcome is BillingFlowResult.BillingUnavailable -> {
                    Text(
                        (outcome as BillingFlowResult.BillingUnavailable).reason,
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.error,
                        textAlign = TextAlign.Center
                    )
                }
                outcome is BillingFlowResult.Failed -> {
                    Text(
                        (outcome as BillingFlowResult.Failed).message,
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.error,
                        textAlign = TextAlign.Center
                    )
                    OutlinedButton(onClick = { outcome = null }, modifier = Modifier.fillMaxWidth()) {
                        Text("Try again")
                    }
                }
                else -> {
                    productError?.let {
                        Text(
                            it,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            textAlign = TextAlign.Center
                        )
                    }
                    Button(
                        onClick = {
                            val activity = context as? Activity ?: return@Button
                            scope.launch {
                                busy = true
                                outcome = billing.purchase(activity)
                                busy = false
                            }
                        },
                        enabled = !busy && product != null,
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text(
                            if (product != null) "Upgrade — ${product!!.priceText}"
                            else "Upgrade to Pro"
                        )
                    }
                    product?.let {
                        Text(
                            it.title,
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                    Spacer(Modifier.height(4.dp))
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        androidx.compose.material3.TextButton(onClick = onDismiss) {
                            Text("Not now")
                        }
                        androidx.compose.material3.TextButton(
                            onClick = {
                                scope.launch {
                                    SyncWorker.retryQuotaBlocked(context)
                                    onDismiss()
                                }
                            }
                        ) {
                            Text("Retry queued items")
                        }
                    }
                }
            }
        }
    }
}

private fun bucketLabel(bucket: String): String = when (bucket) {
    "captures" -> "saves this month"
    "questions" -> "questions today"
    "verifications" -> "web verifications today"
    else -> "$bucket quota"
}

/** "2026-10-01T00:00:00+00:00" -> "Oct 1". Falls back to the raw string. */
fun formatResetDate(resetsAt: String): String {
    if (resetsAt.isBlank()) return ""
    return try {
        val dt = OffsetDateTime.parse(resetsAt)
        val fmt = DateTimeFormatter.ofPattern("MMM d", Locale.getDefault())
        dt.format(fmt)
    } catch (_: Exception) {
        resetsAt
    }
}

/** "2026-10-01T00:00:00+00:00" -> ", resets Oct 1". Empty string on blank input. */
fun formatResetsAt(resetsAt: String): String {
    val date = formatResetDate(resetsAt)
    return if (date.isBlank()) "" else ", resets $date"
}
