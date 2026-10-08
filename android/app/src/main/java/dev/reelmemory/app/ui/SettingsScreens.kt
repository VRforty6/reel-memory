package dev.reelmemory.app.ui

import android.annotation.SuppressLint
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.KeyboardArrowRight
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import dev.reelmemory.app.auth.AuthState
import dev.reelmemory.app.auth.QuotaBucket
import dev.reelmemory.app.auth.SessionManager
import dev.reelmemory.app.data.SettingsStore
import dev.reelmemory.app.sync.SyncWorker
import kotlinx.coroutines.launch
import java.util.Locale

private sealed interface SettingsPage {
    data object Root : SettingsPage
    data object Account : SettingsPage
    data object Plan : SettingsPage
    data object Usage : SettingsPage
    data object Privacy : SettingsPage
    data object Data : SettingsPage
    data object Appearance : SettingsPage
    data object Help : SettingsPage
    data object Developer : SettingsPage
}

/**
 * Settings, split into two worlds:
 * - Normal settings: Account / Plan / Usage / Privacy / Data / Appearance /
 *   Help / Sign out. Simple informational screens — no dev-looking forms.
 * - Hidden Developer Settings (backend URL, OAuth client ID, Play product
 *   ID, debug diagnostics): revealed only via long-press on the app
 *   version row. Normal users never see backend URL / OAuth / product ID
 *   inputs.
 */
@OptIn(ExperimentalMaterial3Api::class, ExperimentalFoundationApi::class)
@Composable
fun SettingsRoot(
    session: SessionManager,
    store: SettingsStore,
    onUpgrade: () -> Unit,
    onClose: () -> Unit,
    modifier: Modifier = Modifier,
) {
    var page by remember { mutableStateOf<SettingsPage>(SettingsPage.Root) }
    val snackbar = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()

    Scaffold(
        modifier = modifier,
        snackbarHost = { SnackbarHost(snackbar) },
        topBar = {
            TopAppBar(
                title = {
                    Text(
                        when (page) {
                            SettingsPage.Root -> "Settings"
                            SettingsPage.Account -> "Account"
                            SettingsPage.Plan -> "Plan"
                            SettingsPage.Usage -> "Usage"
                            SettingsPage.Privacy -> "Privacy"
                            SettingsPage.Data -> "Your data"
                            SettingsPage.Appearance -> "Appearance"
                            SettingsPage.Help -> "Help"
                            SettingsPage.Developer -> "Developer settings"
                        }
                    )
                },
                navigationIcon = {
                    IconButton(onClick = {
                        page = when (page) {
                            SettingsPage.Root -> return@IconButton onClose()
                            else -> SettingsPage.Root
                        }
                    }) {
                        Icon(Icons.Filled.ArrowBack, contentDescription = "Back")
                    }
                }
            )
        }
    ) { padding ->
        when (val p = page) {
            SettingsPage.Root -> SettingsList(
                session = session,
                store = store,
                onOpen = { page = it },
                onUpgrade = onUpgrade,
                onUnlockDeveloper = {
                    page = SettingsPage.Developer
                    scope.launch { snackbar.showSnackbar("Developer settings unlocked") }
                },
                modifier = Modifier.padding(padding)
            )
            SettingsPage.Account -> AccountPage(session, onUpgrade, Modifier.padding(padding))
            SettingsPage.Plan -> PlanPage(session, onUpgrade, Modifier.padding(padding))
            SettingsPage.Usage -> UsagePage(session, Modifier.padding(padding))
            SettingsPage.Privacy -> InfoPage(
                body = "Reel Memory analyzes the videos and images you share " +
                    "so you can search and ask about them later. Your library " +
                    "is yours: memories live in your own database on your " +
                    "server, not with us. We never sell your data, and we " +
                    "never show your shared content to other users.",
                modifier = Modifier.padding(padding)
            )
            SettingsPage.Data -> InfoPage(
                body = "Everything you save belongs to you.\n\n" +
                    "• Reels, transcripts, and memories live in your own " +
                    "Postgres database on your server.\n" +
                    "• Raw video files are deleted right after processing — " +
                    "only the understanding (transcript, frame analysis, " +
                    "on-screen text, search data) is kept.\n" +
                    "• Sign-in tokens stay in your phone's encrypted storage.\n\n" +
                    "Removing a memory from the app deletes it from your library.",
                modifier = Modifier.padding(padding)
            )
            SettingsPage.Appearance -> InfoPage(
                body = "Reel Memory uses a calm light theme with a purple " +
                    "accent, tuned for reading and browsing your library.",
                modifier = Modifier.padding(padding)
            )
            SettingsPage.Help -> HelpPage(Modifier.padding(padding))
            SettingsPage.Developer -> DeveloperPage(
                store = store,
                onSaved = { scope.launch { snackbar.showSnackbar("Developer settings saved") } },
                modifier = Modifier.padding(padding)
            )
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun SettingsList(
    session: SessionManager,
    store: SettingsStore,
    onOpen: (SettingsPage) -> Unit,
    onUpgrade: () -> Unit,
    onUnlockDeveloper: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    val authState by session.authState.collectAsState()
    val profile = (authState as? AuthState.SignedIn)?.profile
    val scope = rememberCoroutineScope()
    var signingOut by remember { mutableStateOf(false) }

    LazyColumn(modifier = modifier.fillMaxSize()) {
        item {
            SettingRow(
                title = "Account",
                subtitle = profile?.email ?: "…",
                onClick = { onOpen(SettingsPage.Account) }
            )
        }
        item {
            SettingRow(
                title = "Plan",
                subtitle = if (profile?.isPro == true) "Pro" else "Free",
                onClick = { onOpen(SettingsPage.Plan) }
            )
        }
        item {
            SettingRow(
                title = "Usage",
                subtitle = "See what you've used this period",
                onClick = { onOpen(SettingsPage.Usage) }
            )
        }
        item { HorizontalDivider(Modifier.padding(horizontal = 16.dp)) }
        item {
            SettingRow(
                title = "Privacy",
                subtitle = "How your content is handled",
                onClick = { onOpen(SettingsPage.Privacy) }
            )
        }
        item {
            SettingRow(
                title = "Your data",
                subtitle = "Where your memories live",
                onClick = { onOpen(SettingsPage.Data) }
            )
        }
        item {
            SettingRow(
                title = "Appearance",
                subtitle = "Theme",
                onClick = { onOpen(SettingsPage.Appearance) }
            )
        }
        item {
            SettingRow(
                title = "Help",
                subtitle = "Sharing, importing, troubleshooting",
                onClick = { onOpen(SettingsPage.Help) }
            )
        }
        item { HorizontalDivider(Modifier.padding(horizontal = 16.dp)) }
        item {
            Row(
                modifier = Modifier.fillMaxWidth()
                    .combinedClickable(
                        onClick = {},
                        onLongClick = onUnlockDeveloper
                    )
                    .padding(horizontal = 16.dp, vertical = 14.dp),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Column {
                    Text(
                        "Reel Memory",
                        style = MaterialTheme.typography.bodyLarge,
                        fontWeight = FontWeight.Medium
                    )
                    Text(
                        "Version ${appVersionName()}",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
        }
        item {
            TextButton(
                onClick = {
                    scope.launch {
                        signingOut = true
                        session.signOut()
                    }
                },
                enabled = !signingOut,
                modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp)
            ) {
                Text(
                    if (signingOut) "Signing out…" else "Sign out",
                    color = MaterialTheme.colorScheme.error
                )
            }
            Spacer(Modifier.height(24.dp))
        }
    }
}

@SuppressLint("PackageManagerGetSignatures")
@Composable
private fun appVersionName(): String {
    val context = LocalContext.current
    return remember {
        try {
            @Suppress("DEPRECATION")
            context.packageManager.getPackageInfo(context.packageName, 0).versionName
                ?: "0.1.0"
        } catch (_: Exception) {
            "0.1.0"
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun SettingRow(title: String, subtitle: String, onClick: () -> Unit) {
    Row(
        modifier = Modifier.fillMaxWidth()
            .combinedClickable(onClick = onClick)
            .padding(horizontal = 16.dp, vertical = 14.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Column(Modifier.weight(1f)) {
            Text(
                title,
                style = MaterialTheme.typography.bodyLarge,
                fontWeight = FontWeight.Medium
            )
            Text(
                subtitle,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
        Icon(
            Icons.Filled.KeyboardArrowRight,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.onSurfaceVariant
        )
    }
}

@Composable
private fun AccountPage(
    session: SessionManager,
    onUpgrade: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val authState by session.authState.collectAsState()
    val profile = (authState as? AuthState.SignedIn)?.profile
    val scope = rememberCoroutineScope()
    var signingOut by remember { mutableStateOf(false) }

    LazyColumn(
        modifier = modifier.fillMaxSize().padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        item {
            if (profile != null) {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(
                        profile.email,
                        style = MaterialTheme.typography.bodyLarge,
                        fontWeight = FontWeight.SemiBold
                    )
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        TierBadge(profile.tier)
                        if (profile.isPro && profile.proExpiresAt != null) {
                            Spacer(Modifier.width(8.dp))
                            Text(
                                "Pro through ${profile.proExpiresAt.take(10)}",
                                style = MaterialTheme.typography.labelMedium,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                    }
                    if (!profile.isPro) {
                        Spacer(Modifier.height(4.dp))
                        Button(onClick = onUpgrade, modifier = Modifier.fillMaxWidth()) {
                            Text("Upgrade to Pro")
                        }
                    }
                    Spacer(Modifier.height(4.dp))
                    TextButton(
                        onClick = {
                            scope.launch {
                                signingOut = true
                                session.signOut()
                            }
                        },
                        enabled = !signingOut
                    ) {
                        Text(
                            if (signingOut) "Signing out…" else "Sign out",
                            color = MaterialTheme.colorScheme.error
                        )
                    }
                }
            } else {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    LinearProgressIndicator(Modifier.fillMaxWidth())
                    Text(
                        "Loading profile…",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    TextButton(onClick = { scope.launch { session.refreshProfile() } }) {
                        Text("Retry")
                    }
                }
            }
        }
    }
}

@Composable
private fun PlanPage(
    session: SessionManager,
    onUpgrade: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val authState by session.authState.collectAsState()
    val profile = (authState as? AuthState.SignedIn)?.profile
    Column(
        modifier = modifier.fillMaxSize().padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Text(
            if (profile?.isPro == true) "You're on Pro" else "You're on the Free plan",
            style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.SemiBold
        )
        Text(
            "Free covers everyday saving and searching. Pro raises your " +
                "monthly saves and daily questions so a heavy research day " +
                "never stalls.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        if (profile?.isPro != true) {
            Button(onClick = onUpgrade, modifier = Modifier.fillMaxWidth()) {
                Text("Upgrade to Pro")
            }
        }
    }
}

@Composable
private fun UsagePage(session: SessionManager, modifier: Modifier = Modifier) {
    val authState by session.authState.collectAsState()
    val profile = (authState as? AuthState.SignedIn)?.profile
    val scope = rememberCoroutineScope()
    LazyColumn(
        modifier = modifier.fillMaxSize().padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        item {
            if (profile != null) {
                Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    profile.usage.forEach { QuotaBar(it) }
                    TextButton(onClick = { scope.launch { session.refreshProfile() } }) {
                        Text("Refresh")
                    }
                }
            } else {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    LinearProgressIndicator(Modifier.fillMaxWidth())
                    TextButton(onClick = { scope.launch { session.refreshProfile() } }) {
                        Text("Retry")
                    }
                }
            }
        }
    }
}

@Composable
private fun InfoPage(body: String, modifier: Modifier = Modifier) {
    Column(modifier = modifier.fillMaxSize().padding(20.dp)) {
        Text(
            body,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
    }
}

@Composable
private fun HelpPage(modifier: Modifier = Modifier) {
    var showHowSharingWorks by remember { mutableStateOf(false) }
    if (showHowSharingWorks) {
        HowSharingWorksDialog(onDismiss = { showHowSharingWorks = false })
    }
    LazyColumn(
        modifier = modifier.fillMaxSize().padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        item {
            Text(
                "Getting the best results",
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.SemiBold
            )
        }
        item {
            Text(
                "• Share the actual video or photo file — not just the link — " +
                    "so Reel Memory can see, hear, and read it.\n" +
                    "• For carousels, share all photos at once: they become " +
                    "one memory, and search can point at the exact photo.\n" +
                    "• Link-only saves are kept too, but they can't be " +
                    "analyzed until you import the media file.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
        item {
            TextButton(onClick = { showHowSharingWorks = true }) {
                Text("How sharing works")
            }
        }
        item { HorizontalDivider() }
        item {
            Text(
                "Something stuck?",
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.SemiBold
            )
        }
        item {
            Text(
                "Items that are still processing appear in the Inbox. If an " +
                    "item failed, open it: retryable hiccups offer “Try again”, " +
                    "and anything else can be opened in its original app or removed.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
    }
}

// ---------------------------------------------------------------------------
// Hidden developer settings
// ---------------------------------------------------------------------------

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun DeveloperPage(
    store: SettingsStore,
    onSaved: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    val currentUrl by store.backendUrl.collectAsState(initial = SettingsStore.DEFAULT_BACKEND_URL)
    var draftUrl by remember(currentUrl) { mutableStateOf(currentUrl) }
    val currentClientId by store.googleClientId.collectAsState(initial = null)
    var draftClientId by remember(currentClientId) { mutableStateOf(currentClientId ?: "") }
    val currentProductId by store.playProductId.collectAsState(
        initial = SettingsStore.DEFAULT_PLAY_PRODUCT_ID
    )
    var draftProductId by remember(currentProductId) { mutableStateOf(currentProductId) }
    val debugDiagnostics by store.debugDiagnostics.collectAsState(initial = false)
    var error by remember { mutableStateOf<String?>(null) }

    LazyColumn(
        modifier = modifier.fillMaxSize().padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        item {
            Text(
                "These are for development and self-hosting. Normal users " +
                    "should never need to touch them.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
        item {
            Text(
                "Backend",
                style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.SemiBold
            )
        }
        item {
            OutlinedTextField(
                value = draftUrl,
                onValueChange = { draftUrl = it; error = null },
                label = { Text("Backend base URL") },
                singleLine = true,
                isError = error != null,
                supportingText = error?.let { { Text(it) } },
                modifier = Modifier.fillMaxWidth()
            )
        }
        item {
            Text(
                "On an emulator use http://10.0.2.2:8000. On a physical phone " +
                    "on the same Wi-Fi, use your computer's LAN IP.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
        item { HorizontalDivider() }
        item {
            Text(
                "Google Sign-In",
                style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.SemiBold
            )
        }
        item {
            OutlinedTextField(
                value = draftClientId,
                onValueChange = { draftClientId = it },
                label = { Text("Google OAuth client ID") },
                singleLine = true,
                placeholder = { Text("….apps.googleusercontent.com") },
                modifier = Modifier.fillMaxWidth()
            )
        }
        item { HorizontalDivider() }
        item {
            Text(
                "Google Play Billing",
                style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.SemiBold
            )
        }
        item {
            OutlinedTextField(
                value = draftProductId,
                onValueChange = { draftProductId = it },
                label = { Text("Pro product ID") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth()
            )
        }
        item { HorizontalDivider() }
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Column(Modifier.weight(1f)) {
                    Text(
                        "Debug diagnostics",
                        style = MaterialTheme.typography.bodyLarge,
                        fontWeight = FontWeight.Medium
                    )
                    Text(
                        "Show raw backend failure details on failed memories.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                Switch(
                    checked = debugDiagnostics,
                    onCheckedChange = { scope.launch { store.setDebugDiagnostics(it) } }
                )
            }
        }
        item { HorizontalDivider() }
        item {
            Row {
                Button(onClick = {
                    scope.launch {
                        try {
                            store.setBackendUrl(draftUrl)
                            store.setGoogleClientId(draftClientId)
                            store.setPlayProductId(draftProductId)
                            error = null
                            onSaved()
                            SyncWorker.enqueue(context)
                        } catch (e: IllegalArgumentException) {
                            error = e.message
                        }
                    }
                }) {
                    Text("Save")
                }
                Spacer(Modifier.width(8.dp))
                TextButton(onClick = {
                    draftUrl = SettingsStore.DEFAULT_BACKEND_URL
                    draftClientId = ""
                    draftProductId = SettingsStore.DEFAULT_PLAY_PRODUCT_ID
                    error = null
                }) {
                    Text("Reset to defaults")
                }
            }
            Spacer(Modifier.height(24.dp))
        }
    }
}

// ---------------------------------------------------------------------------
// Shared bits (moved from the old Settings screen)
// ---------------------------------------------------------------------------

@Composable
internal fun TierBadge(tier: String) {
    val pro = tier.equals("pro", ignoreCase = true)
    Card(
        colors = CardDefaults.cardColors(
            containerColor = if (pro) MaterialTheme.colorScheme.primaryContainer
            else MaterialTheme.colorScheme.surfaceVariant
        )
    ) {
        Text(
            tier.uppercase(Locale.getDefault()),
            modifier = Modifier.padding(horizontal = 10.dp, vertical = 4.dp),
            style = MaterialTheme.typography.labelSmall,
            fontWeight = FontWeight.Bold,
            color = if (pro) MaterialTheme.colorScheme.onPrimaryContainer
            else MaterialTheme.colorScheme.onSurfaceVariant
        )
    }
}

@Composable
internal fun QuotaBar(bucket: QuotaBucket) {
    Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Text(
                bucketDisplayName(bucket.name),
                style = MaterialTheme.typography.labelMedium
            )
            Text(
                "${bucket.used}/${bucket.limit}",
                style = MaterialTheme.typography.labelMedium,
                fontWeight = FontWeight.SemiBold
            )
        }
        LinearProgressIndicator(
            progress = { bucket.fraction },
            modifier = Modifier.fillMaxWidth()
        )
        if (bucket.resetsAt.isNotBlank()) {
            Text(
                "Resets ${formatResetDate(bucket.resetsAt)}",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
    }
}

private fun bucketDisplayName(bucket: String): String = when (bucket) {
    "captures" -> "Saves this month"
    "questions" -> "Questions today"
    "verifications" -> "Web verifications today"
    else -> bucket.replaceFirstChar { it.uppercaseChar() }
}
