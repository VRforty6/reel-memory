package dev.reelmemory.app.ui

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.List
import androidx.compose.material.icons.filled.MailOutline
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
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
import androidx.lifecycle.lifecycleScope
import dev.reelmemory.app.auth.AuthState
import dev.reelmemory.app.auth.AuthApi
import dev.reelmemory.app.auth.QuotaExceededInfo
import dev.reelmemory.app.auth.SessionManager
import dev.reelmemory.app.billing.BillingRepository
import dev.reelmemory.app.billing.StubBillingPort
import dev.reelmemory.app.data.AlbumUpload
import dev.reelmemory.app.data.QueuedShare
import dev.reelmemory.app.data.SettingsStore
import dev.reelmemory.app.data.ShareDatabase
import dev.reelmemory.app.data.VideoUpload
import dev.reelmemory.app.data.WebCapture
import dev.reelmemory.app.net.CaptureApi
import dev.reelmemory.app.net.MemoryApi
import dev.reelmemory.app.net.MemoryItem
import dev.reelmemory.app.net.SearchApi
import dev.reelmemory.app.net.SearchResultItem
import dev.reelmemory.app.net.ThumbnailApi
import dev.reelmemory.app.sync.StatusCheckWorker
import dev.reelmemory.app.sync.SyncWorker
import dev.reelmemory.app.sync.UploadWorker
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * Main screen, Milestone A:
 * - Bottom nav: Inbox / Search / Library (Search is the center hero tab).
 * - + FAB opens the add/import sheet (share a video / add images / paste a link).
 * - Settings (top bar): normal user settings; developer settings are hidden
 *   behind a long-press on the app version row.
 */
class MainActivity : ComponentActivity() {

    private val queueState = MutableStateFlow<List<QueuedShare>>(emptyList())
    val queue: StateFlow<List<QueuedShare>> = queueState.asStateFlow()

    private val videosState = MutableStateFlow<List<VideoUpload>>(emptyList())
    val videos: StateFlow<List<VideoUpload>> = videosState.asStateFlow()

    private val albumsState = MutableStateFlow<List<AlbumUpload>>(emptyList())
    val albums: StateFlow<List<AlbumUpload>> = albumsState.asStateFlow()

    private val webState = MutableStateFlow<List<WebCapture>>(emptyList())
    val webCaptures: StateFlow<List<WebCapture>> = webState.asStateFlow()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val session = SessionManager.get(this)
        lifecycleScope.launch {
            ShareDatabase.get(this@MainActivity).shareDao().observeAll()
                .collect { queueState.value = it }
        }
        lifecycleScope.launch {
            ShareDatabase.get(this@MainActivity).videoUploadDao().observeAll()
                .collect { videosState.value = it }
        }
        lifecycleScope.launch {
            ShareDatabase.get(this@MainActivity).albumUploadDao().observeAll()
                .collect { albumsState.value = it }
        }
        lifecycleScope.launch {
            ShareDatabase.get(this@MainActivity).webCaptureDao().observeAll()
                .collect { webState.value = it }
        }
        setContent {
            ReelMemoryTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    ReelMemoryApp(
                        session = session,
                        queue = queue,
                        videos = videos,
                        albums = albums,
                        webCaptures = webCaptures
                    )
                }
            }
        }
    }

    override fun onResume() {
        super.onResume()
        // Reconcile uploads that finished on the backend while the app was
        // stopped. This worker only polls existing backend memory ids; it
        // never sends media again.
        StatusCheckWorker.enqueue(this)
    }
}

private enum class Tab(val label: String) {
    INBOX("Inbox"),
    SEARCH("Search"),
    LIBRARY("Library")
}

/** A memory opened from any tab, routed to its state-appropriate screen. */
private data class DetailTarget(
    val memoryId: String,
    val title: String,
    val knownStatus: String?
)

/**
 * App root: signed-out users get the auth flow, signed-in users get the
 * tabbed app. A 402 `quota_exceeded` from any endpoint raises the Pro
 * paywall as an overlay above both.
 */
@Composable
private fun ReelMemoryApp(
    session: SessionManager,
    queue: StateFlow<List<QueuedShare>>,
    videos: StateFlow<List<VideoUpload>>,
    albums: StateFlow<List<AlbumUpload>>,
    webCaptures: StateFlow<List<WebCapture>>
) {
    val context = LocalContext.current
    val settings = remember { SettingsStore(context) }
    val authApi = remember { AuthApi(baseUrl = { settings.getBackendUrl() }) }
    val billing = remember {
        BillingRepository(
            port = StubBillingPort(),
            verifyApi = object : BillingRepository.VerifyApi {
                override suspend fun verify(
                    accessToken: String,
                    packageName: String,
                    productId: String,
                    purchaseToken: String
                ) = authApi.verifyPurchase(accessToken, packageName, productId, purchaseToken)
            },
            session = session,
            productIdProvider = { settings.getPlayProductId() }
        )
    }

    val authState by session.authState.collectAsState()
    var paywallInfo by remember { mutableStateOf<QuotaExceededInfo?>(null) }
    var manualPaywall by remember { mutableStateOf(false) }

    LaunchedEffect(session) {
        session.quotaEvents.collect { info -> paywallInfo = info }
    }

    if (paywallInfo != null || manualPaywall) {
        val profile = (authState as? AuthState.SignedIn)?.profile
        PaywallScreen(
            quotaInfo = paywallInfo,
            profile = profile,
            billing = billing,
            onDismiss = { paywallInfo = null; manualPaywall = false }
        )
        return
    }

    when (authState) {
        AuthState.SignedOut -> AuthFlow(
            session = session,
            onSignedIn = {
                // Anything queued while signed out can sync now.
                SyncWorker.enqueue(context)
                UploadWorker.enqueue(context)
                StatusCheckWorker.enqueue(context)
            }
        )
        is AuthState.SignedIn -> MainTabs(
            session = session,
            billing = billing,
            settings = settings,
            queue = queue,
            videos = videos,
            albums = albums,
            webCaptures = webCaptures,
            onUpgrade = { manualPaywall = true }
        )
    }
}

/** The tabbed app, only reachable while signed in. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun MainTabs(
    session: SessionManager,
    billing: BillingRepository,
    settings: SettingsStore,
    queue: StateFlow<List<QueuedShare>>,
    videos: StateFlow<List<VideoUpload>>,
    albums: StateFlow<List<AlbumUpload>>,
    webCaptures: StateFlow<List<WebCapture>>,
    onUpgrade: () -> Unit
) {
    var tab by remember { mutableStateOf(Tab.SEARCH) }
    var showSettings by remember { mutableStateOf(false) }
    var showAddSheet by remember { mutableStateOf(false) }
    var detailTarget by remember { mutableStateOf<DetailTarget?>(null) }
    val context = LocalContext.current
    val snackbar = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()
    val memoryApi = remember { MemoryApi(settings, session) }
    val searchApi = remember { SearchApi(settings, session) }
    val thumbnailApi = remember { ThumbnailApi(settings, session) }
    val thumbnailLoader = remember { ThumbnailLoader(thumbnailApi) }
    val captureApi = remember { CaptureApi(settings, session) }
    val devDiagnostics by settings.debugDiagnostics.collectAsState(initial = false)

    var backendMemories by remember { mutableStateOf<List<MemoryItem>?>(null) }
    var memoriesError by remember { mutableStateOf<String?>(null) }
    var memoriesLoading by remember { mutableStateOf(false) }

    fun loadMemories() {
        scope.launch {
            memoriesLoading = true
            memoriesError = null
            when (val r = memoryApi.listMemories()) {
                is MemoryApi.ListResult.Ok -> backendMemories = r.memories
                is MemoryApi.ListResult.Unauthenticated -> {
                    memoriesError = "Signed out — sign in to see your memories."
                    if (backendMemories == null) backendMemories = emptyList()
                }
                is MemoryApi.ListResult.QuotaExceeded ->
                    memoriesError = "Free quota exhausted — upgrade to Pro to keep browsing."
                is MemoryApi.ListResult.Transient -> {
                    memoriesError = r.message
                    if (backendMemories == null) backendMemories = emptyList()
                }
            }
            memoriesLoading = false
        }
    }

    // Load the backend list the first time a list-backed tab is shown.
    LaunchedEffect(tab) {
        if ((tab == Tab.INBOX || tab == Tab.LIBRARY) && backendMemories == null && !memoriesLoading) {
            loadMemories()
        }
    }

    fun openDetail(memoryId: String, title: String, knownStatus: String?) {
        detailTarget = DetailTarget(memoryId, title, knownStatus)
    }

    fun openSearchResult(item: SearchResultItem) {
        openDetail(
            item.memoryId,
            searchResultTitle(item.title, item.creatorHandle, item.platform),
            item.processingStatus
        )
    }

    fun openMemoryItem(item: MemoryItem) {
        openDetail(
            item.id,
            libraryTitleFallback(item.title, item.platform),
            item.processingStatus
        )
    }

    // Full-screen detail takes over the tabs until dismissed.
    detailTarget?.let { target ->
        MemoryDetailRouter(
            memoryId = target.memoryId,
            title = target.title,
            api = memoryApi,
            knownStatus = target.knownStatus,
            devDiagnostics = devDiagnostics,
            onBack = { detailTarget = null },
            onRemoved = {
                detailTarget = null
                loadMemories()
            }
        )
        return
    }

    if (showAddSheet) {
        AddImportSheet(
            onDismiss = { showAddSheet = false },
            onPasteLink = { url ->
                showAddSheet = false
                scope.launch {
                    val msg = when (val r = captureApi.postUrlCapture(url)) {
                        is CaptureApi.SyncResult.Accepted ->
                            if (r.duplicate) "Already in Reel Memory"
                            else "Link saved"
                        is CaptureApi.SyncResult.Rejected -> "Couldn't save: ${r.message}"
                        is CaptureApi.SyncResult.Transient -> "Couldn't reach the backend: ${r.message}"
                        is CaptureApi.SyncResult.Unauthenticated -> "Signed out — sign in and try again."
                        is CaptureApi.SyncResult.QuotaExceeded -> "Free quota exhausted — upgrade to Pro."
                    }
                    snackbar.showSnackbar(msg)
                }
            }
        )
    }

    Scaffold(
        snackbarHost = { SnackbarHost(snackbar) },
        topBar = {
            TopAppBar(
                title = { Text("Reel Memory") },
                actions = {
                    IconButton(onClick = {
                        StatusCheckWorker.enqueue(context)
                        loadMemories()
                        scope.launch { snackbar.showSnackbar("Refreshing status") }
                    }) {
                        Icon(Icons.Filled.Refresh, contentDescription = "Refresh status")
                    }
                    IconButton(onClick = { showSettings = !showSettings }) {
                        Icon(Icons.Filled.Settings, contentDescription = "Settings")
                    }
                }
            )
        },
        bottomBar = {
            NavigationBar {
                NavigationBarItem(
                    selected = tab == Tab.INBOX,
                    onClick = { tab = Tab.INBOX },
                    icon = { Icon(Icons.Filled.MailOutline, contentDescription = null) },
                    label = { Text("Inbox") }
                )
                NavigationBarItem(
                    selected = tab == Tab.SEARCH,
                    onClick = { tab = Tab.SEARCH },
                    icon = {
                        // The hero tab: emphasized with a filled purple disc.
                        Surface(
                            shape = CircleShape,
                            color = if (tab == Tab.SEARCH)
                                androidx.compose.material3.MaterialTheme.colorScheme.primaryContainer
                            else androidx.compose.material3.MaterialTheme.colorScheme.surfaceVariant,
                            modifier = Modifier.size(48.dp)
                        ) {
                            androidx.compose.foundation.layout.Box(
                                contentAlignment = Alignment.Center,
                                modifier = Modifier.fillMaxSize()
                            ) {
                                Icon(
                                    Icons.Filled.Search,
                                    contentDescription = null,
                                    tint = if (tab == Tab.SEARCH)
                                        androidx.compose.material3.MaterialTheme.colorScheme.onPrimaryContainer
                                    else androidx.compose.material3.MaterialTheme.colorScheme.onSurfaceVariant,
                                    modifier = Modifier.size(24.dp)
                                )
                            }
                        }
                    },
                    label = { Text("Search", fontWeight = FontWeight.SemiBold) }
                )
                NavigationBarItem(
                    selected = tab == Tab.LIBRARY,
                    onClick = { tab = Tab.LIBRARY },
                    icon = { Icon(Icons.Filled.List, contentDescription = null) },
                    label = { Text("Library") }
                )
            }
        },
        floatingActionButton = {
            if (!showSettings) {
                FloatingActionButton(onClick = { showAddSheet = true }) {
                    Icon(Icons.Filled.Add, contentDescription = "Add a memory")
                }
            }
        }
    ) { padding ->
        if (showSettings) {
            SettingsRoot(
                session = session,
                store = settings,
                onUpgrade = onUpgrade,
                onClose = { showSettings = false },
                modifier = Modifier.padding(padding)
            )
        } else {
            when (tab) {
                Tab.INBOX -> InboxScreen(
                    modifier = Modifier.padding(padding),
                    queue = queue,
                    videos = videos,
                    albums = albums,
                    webCaptures = webCaptures,
                    backendMemories = backendMemories,
                    onOpenBackend = { openMemoryItem(it) },
                    onAskLocal = { memoryId, title -> openDetail(memoryId, title, "READY") }
                )
                Tab.SEARCH -> SearchScreen(
                    modifier = Modifier.padding(padding),
                    api = searchApi,
                    thumbnailLoader = thumbnailLoader,
                    onOpenResult = { openSearchResult(it) }
                )
                Tab.LIBRARY -> LibraryScreen(
                    modifier = Modifier.padding(padding),
                    memories = backendMemories,
                    error = memoriesError,
                    loading = memoriesLoading,
                    onRefresh = { loadMemories() },
                    thumbnailLoader = thumbnailLoader,
                    onOpenMemory = { openMemoryItem(it) },
                    onAddFirst = { showAddSheet = true }
                )
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Add / import sheet
// ---------------------------------------------------------------------------

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AddImportSheet(
    onDismiss: () -> Unit,
    onPasteLink: (String) -> Unit,
) {
    var dialog by remember { mutableStateOf<AddDialog?>(null) }

    dialog?.let {
        when (it) {
            AddDialog.ShareVideo -> ShareFileHelpDialog(
                kind = "video",
                onDismiss = { dialog = null }
            )
            AddDialog.AddImages -> ShareFileHelpDialog(
                kind = "images",
                onDismiss = { dialog = null }
            )
            AddDialog.PasteLink -> PasteLinkDialog(
                onDismiss = { dialog = null },
                onSubmit = onPasteLink
            )
        }
    }

    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = rememberModalBottomSheetState()
    ) {
        Column(Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 8.dp)) {
            Text(
                "Add to Reel Memory",
                style = androidx.compose.material3.MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.SemiBold,
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp)
            )
            AddSheetRow(
                icon = Icons.Filled.PlayArrow,
                title = "Share a video",
                subtitle = "Send the actual video file from any app",
                onClick = { dialog = AddDialog.ShareVideo }
            )
            AddSheetRow(
                icon = ImageFrame,
                title = "Add images",
                subtitle = "Share photos or screenshots as files",
                onClick = { dialog = AddDialog.AddImages }
            )
            AddSheetRow(
                icon = Icons.Filled.Search,
                title = "Paste a link",
                subtitle = "Save the source for later",
                onClick = { dialog = AddDialog.PasteLink }
            )
            Spacer(Modifier.height(16.dp))
        }
    }
}

private enum class AddDialog { ShareVideo, AddImages, PasteLink }

@Composable
private fun AddSheetRow(
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    title: String,
    subtitle: String,
    onClick: () -> Unit,
) {
    ListItem(
        headlineContent = { Text(title, fontWeight = FontWeight.Medium) },
        supportingContent = { Text(subtitle) },
        leadingContent = { Icon(icon, contentDescription = null) },
        modifier = Modifier.clickable(onClick = onClick)
    )
}

/**
 * In-app entry point for the share-sheet intake: the actual file has to
 * come from another app, so this explains the two taps honestly instead
 * of pretending the app can fetch it.
 */
@Composable
private fun ShareFileHelpDialog(kind: String, onDismiss: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(if (kind == "video") "Share a video" else "Add images") },
        text = {
            Text(
                "Reel Memory understands a ${if (kind == "video") "video" else "photo"} " +
                    "only when it receives the actual file. Two taps:\n\n" +
                    "1. Open the ${if (kind == "video") "video" else "photo(s)"} in your " +
                    "gallery or any other app.\n" +
                    "2. Tap Share → choose Reel Memory.\n\n" +
                    "The file uploads, gets analyzed, and lands in your Inbox."
            )
        },
        confirmButton = {
            TextButton(onClick = onDismiss) { Text("Got it") }
        }
    )
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun PasteLinkDialog(
    onDismiss: () -> Unit,
    onSubmit: (String) -> Unit,
) {
    var url by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Paste a link") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(
                    "Save the source for later. Full analysis depends on " +
                        "whether the media can be accessed.",
                    style = androidx.compose.material3.MaterialTheme.typography.bodyMedium,
                    color = androidx.compose.material3.MaterialTheme.colorScheme.onSurfaceVariant
                )
                OutlinedTextField(
                    value = url,
                    onValueChange = { url = it; error = null },
                    label = { Text("Link") },
                    placeholder = { Text("https://…") },
                    singleLine = true,
                    isError = error != null,
                    supportingText = error?.let { { Text(it) } },
                    modifier = Modifier.fillMaxWidth()
                )
                Text(
                    "Instagram links save as link-only — Instagram doesn't " +
                        "share the video itself. For full analysis, save the " +
                        "video to your phone and share the file.",
                    style = androidx.compose.material3.MaterialTheme.typography.bodySmall,
                    color = androidx.compose.material3.MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        },
        confirmButton = {
            TextButton(onClick = {
                val trimmed = url.trim()
                if (trimmed.isEmpty() ||
                    !(trimmed.startsWith("http://") || trimmed.startsWith("https://"))
                ) {
                    error = "Paste a full http(s) link."
                } else {
                    onSubmit(trimmed)
                }
            }) { Text("Save") }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("Cancel") }
        }
    )
}
