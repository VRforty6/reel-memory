package dev.reelmemory.app.ui

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
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
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.reelmemory.app.net.MemoryApi
import dev.reelmemory.app.net.MemoryDetail
import dev.reelmemory.app.net.MemoryStatus
import kotlinx.coroutines.launch

private sealed interface RouterState {
    data object Loading : RouterState
    data class Routed(val route: DetailRoute, val status: MemoryStatus?, val detail: MemoryDetail?) :
        RouterState
    data class Error(val message: String) : RouterState
}

/**
 * Opens the one correct detail screen for a memory's state. The four
 * routes are mutually exclusive: READY keeps the existing Ask / Actions /
 * Brief tabs; the other three never show them.
 */
@Composable
fun MemoryDetailRouter(
    memoryId: String,
    title: String,
    api: MemoryApi,
    knownStatus: String? = null,
    devDiagnostics: Boolean = false,
    onBack: () -> Unit,
    onRemoved: () -> Unit,
) {
    // Fast path: we already know it is READY (e.g. from the Library list).
    if (knownStatus == "READY") {
        MemoryDetailScreen(
            memoryId = memoryId,
            title = title,
            api = api,
            onBack = onBack
        )
        return
    }

    var state by remember(memoryId) { mutableStateOf<RouterState>(RouterState.Loading) }

    LaunchedEffect(memoryId) {
        val status = when (val s = api.getStatus(memoryId)) {
            is MemoryApi.StatusResult.Ok -> s.status
            is MemoryApi.StatusResult.NotFound -> {
                state = RouterState.Error(s.message); return@LaunchedEffect
            }
            is MemoryApi.StatusResult.Unauthenticated -> {
                state = RouterState.Error("Signed out — sign in to continue."); return@LaunchedEffect
            }
            is MemoryApi.StatusResult.QuotaExceeded -> {
                state = RouterState.Error("Free quota exhausted — upgrade to Pro to continue.")
                return@LaunchedEffect
            }
            is MemoryApi.StatusResult.Transient -> {
                state = RouterState.Error("Couldn't reach the backend: ${s.message}")
                return@LaunchedEffect
            }
        }
        val route = detailRouteFor(status.processingStatus)
        if (route == DetailRoute.READY || route == DetailRoute.PROCESSING) {
            state = RouterState.Routed(route, status, null)
            return@LaunchedEffect
        }
        // Link-only and failed screens need the source URLs.
        val detail = when (val d = api.getDetail(memoryId)) {
            is MemoryApi.DetailResult.Ok -> d.detail
            else -> null // detail is best-effort; the screens cope without it
        }
        state = RouterState.Routed(route, status, detail)
    }

    when (val s = state) {
        RouterState.Loading -> {
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator()
            }
        }
        is RouterState.Error -> {
            RouterError(message = s.message, onBack = onBack)
        }
        is RouterState.Routed -> {
            when (s.route) {
                DetailRoute.READY -> MemoryDetailScreen(
                    memoryId = memoryId, title = title, api = api, onBack = onBack
                )
                DetailRoute.PROCESSING -> ProcessingDetailScreen(
                    memoryId = memoryId,
                    title = title,
                    status = s.status,
                    onBack = onBack
                )
                DetailRoute.METADATA_ONLY -> LinkOnlyDetailScreen(
                    memoryId = memoryId,
                    title = title,
                    detail = s.detail,
                    api = api,
                    onBack = onBack,
                    onRemoved = onRemoved
                )
                DetailRoute.FAILED -> FailedDetailScreen(
                    memoryId = memoryId,
                    title = title,
                    status = s.status,
                    detail = s.detail,
                    devDiagnostics = devDiagnostics,
                    api = api,
                    onBack = onBack,
                    onRemoved = onRemoved
                )
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun RouterError(message: String, onBack: () -> Unit) {
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Memory") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.Filled.ArrowBack, contentDescription = "Back")
                    }
                }
            )
        }
    ) { padding ->
        Column(
            Modifier.padding(padding).fillMaxSize().padding(24.dp),
            verticalArrangement = Arrangement.Center,
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            Text(
                "Couldn't open this memory",
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.SemiBold
            )
            Spacer(Modifier.height(8.dp))
            Text(
                message,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
    }
}

// ---------------------------------------------------------------------------
// PROCESSING: honest stage list, no percentages, no Ask
// ---------------------------------------------------------------------------

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ProcessingDetailScreen(
    memoryId: String,
    title: String,
    status: MemoryStatus?,
    onBack: () -> Unit,
) {
    val rawStage = status?.stage ?: status?.processingStatus
    val position = stagePosition(rawStage)
    val autoRetrying = isAutoRetrying(status?.processingStatus)
    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Text(title, maxLines = 1, overflow = TextOverflow.Ellipsis)
                },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.Filled.ArrowBack, contentDescription = "Back")
                    }
                }
            )
        }
    ) { padding ->
        Column(
            Modifier.padding(padding).fillMaxSize().padding(20.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            Text(
                "Getting this ready",
                style = MaterialTheme.typography.headlineSmall,
                fontWeight = FontWeight.Bold
            )
            Text(
                // Source-aware: a link capture never claims an upload is
                // safe, and an automatic backend retry says so honestly.
                processingIntroCopy(status?.processingStatus, status?.mediaKind),
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
            if (status == null) {
                LinearProgressIndicator(Modifier.fillMaxWidth())
            } else if (autoRetrying) {
                // Backend-side automatic retry: no stage list (the stage is
                // unknown during a retry) and deliberately no manual
                // "Try again" button — the retry is already running.
                LinearProgressIndicator(Modifier.fillMaxWidth())
            } else {
                Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    PROCESSING_STAGES.forEachIndexed { index, stage ->
                        val done = position >= 0 && index < position
                        val current = position >= 0 && index == position
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            modifier = Modifier.fillMaxWidth().padding(vertical = 6.dp)
                        ) {
                            Icon(
                                imageVector = if (done) Icons.Filled.CheckCircle
                                else CircleOutline,
                                contentDescription = null,
                                tint = when {
                                    done -> MaterialTheme.colorScheme.primary
                                    current -> MaterialTheme.colorScheme.primary
                                    else -> MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.4f)
                                },
                                modifier = Modifier.size(22.dp)
                            )
                            Spacer(Modifier.width(12.dp))
                            Text(
                                stageLabel(stage),
                                style = MaterialTheme.typography.bodyLarge,
                                fontWeight = if (current) FontWeight.SemiBold else FontWeight.Normal,
                                color = if (current || done) MaterialTheme.colorScheme.onSurface
                                else MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.6f)
                            )
                            if (current) {
                                Spacer(Modifier.width(8.dp))
                                Text(
                                    "in progress",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.primary,
                                    fontWeight = FontWeight.SemiBold
                                )
                            }
                        }
                    }
                    if (position < 0) {
                        Text(
                            stageLabel(rawStage),
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// METADATA_ONLY: "Link saved" — never labeled synced, no Ask/Actions/Brief
// ---------------------------------------------------------------------------

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun LinkOnlyDetailScreen(
    memoryId: String,
    title: String,
    detail: MemoryDetail?,
    api: MemoryApi,
    onBack: () -> Unit,
    onRemoved: () -> Unit,
) {
    val context = LocalContext.current
    var showImportHelp by remember { mutableStateOf(false) }
    var confirmRemove by remember { mutableStateOf(false) }
    var removing by remember { mutableStateOf(false) }
    var removeError by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    if (showImportHelp) {
        ImportMediaHelpDialog(onDismiss = { showImportHelp = false })
    }
    if (confirmRemove) {
        ConfirmRemoveDialog(
            onConfirm = {
                confirmRemove = false
                removing = true
                removeError = null
                scope.launch {
                    when (val r = api.deleteMemory(memoryId)) {
                        is MemoryApi.DeleteResult.Deleted -> onRemoved()
                        is MemoryApi.DeleteResult.NotFound -> onRemoved()
                        is MemoryApi.DeleteResult.Unauthenticated ->
                            removeError = "Signed out — sign in and try again."
                        is MemoryApi.DeleteResult.QuotaExceeded ->
                            removeError = "Free quota exhausted — upgrade to Pro."
                        is MemoryApi.DeleteResult.Transient ->
                            removeError = "Couldn't reach the backend: ${r.message}"
                    }
                    removing = false
                }
            },
            onDismiss = { confirmRemove = false }
        )
    }

    val platformName = platformDisplayName(detail?.platform)
    val copy = if (detail?.platform.equals("instagram", ignoreCase = true)) {
        "Instagram shared the link but not the actual Reel video, so Reel " +
            "Memory cannot yet see or hear this Reel."
    } else {
        "$platformName shared the link but not the actual video, so Reel " +
            "Memory cannot yet see or hear it."
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Text(title, maxLines = 1, overflow = TextOverflow.Ellipsis)
                },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.Filled.ArrowBack, contentDescription = "Back")
                    }
                }
            )
        }
    ) { padding ->
        Column(
            Modifier.padding(padding).fillMaxSize().padding(20.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            LinkOnlyBadge()
            Text(
                "Link saved",
                style = MaterialTheme.typography.headlineSmall,
                fontWeight = FontWeight.Bold
            )
            Text(
                copy,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
            detail?.creatorHandle?.let {
                Text(
                    "Shared by @$it on $platformName",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            Spacer(Modifier.height(8.dp))
            detail?.effectiveUrl?.let { url ->
                Button(
                    onClick = { openUrl(context, url) },
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text("Open original")
                }
            }
            OutlinedButton(
                onClick = { showImportHelp = true },
                modifier = Modifier.fillMaxWidth()
            ) {
                Text("Learn how to import usable media")
            }
            TextButton(
                onClick = { confirmRemove = true },
                enabled = !removing,
                modifier = Modifier.fillMaxWidth()
            ) {
                Text(if (removing) "Removing…" else "Remove")
            }
            removeError?.let {
                Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
            }
        }
    }
}

@Composable
private fun LinkOnlyBadge() {
    androidx.compose.material3.Surface(
        color = MaterialTheme.colorScheme.tertiaryContainer,
        shape = MaterialTheme.shapes.small
    ) {
        Text(
            "Link only — not analyzed",
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp),
            style = MaterialTheme.typography.labelMedium,
            fontWeight = FontWeight.SemiBold
        )
    }
}

/** How to turn a link-only save into a fully analyzable memory. */
@Composable
fun ImportMediaHelpDialog(onDismiss: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Import usable media") },
        text = {
            Text(
                "Reel Memory can only understand a video it actually receives " +
                    "as a file. To fix a link-only save:\n\n" +
                    "1. Save the video to your phone — download it from the " +
                    "app, or screen-record it while it plays.\n" +
                    "2. Open your gallery, tap Share on the saved video.\n" +
                    "3. Choose Reel Memory. The file uploads and gets fully " +
                    "analyzed: speech, visuals, and on-screen text."
            )
        },
        confirmButton = {
            TextButton(onClick = onDismiss) { Text("Got it") }
        }
    )
}

@Composable
private fun ConfirmRemoveDialog(onConfirm: () -> Unit, onDismiss: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Remove this memory?") },
        text = { Text("It will be deleted from your library. This can't be undone.") },
        confirmButton = {
            TextButton(onClick = onConfirm) { Text("Remove") }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("Keep") }
        }
    )
}

private fun openUrl(context: android.content.Context, url: String) {
    try {
        context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
    } catch (_: Exception) {
        // No browser available; nothing sensible to do.
    }
}

// ---------------------------------------------------------------------------
// FAILED: human reason + Try again (retryable) or Open original / Remove
// ---------------------------------------------------------------------------

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun FailedDetailScreen(
    memoryId: String,
    title: String,
    status: MemoryStatus?,
    detail: MemoryDetail?,
    devDiagnostics: Boolean,
    api: MemoryApi,
    onBack: () -> Unit,
    onRemoved: () -> Unit,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val retryable = isRetryableFailure(status?.failureCode)
    var retryState by remember { mutableStateOf<RetryUi>(RetryUi.Idle) }
    var confirmRemove by remember { mutableStateOf(false) }
    var removing by remember { mutableStateOf(false) }
    var removeError by remember { mutableStateOf<String?>(null) }

    if (confirmRemove) {
        ConfirmRemoveDialog(
            onConfirm = {
                confirmRemove = false
                removing = true
                removeError = null
                scope.launch {
                    when (val r = api.deleteMemory(memoryId)) {
                        is MemoryApi.DeleteResult.Deleted -> onRemoved()
                        is MemoryApi.DeleteResult.NotFound -> onRemoved()
                        is MemoryApi.DeleteResult.Unauthenticated ->
                            removeError = "Signed out — sign in and try again."
                        is MemoryApi.DeleteResult.QuotaExceeded ->
                            removeError = "Free quota exhausted — upgrade to Pro."
                        is MemoryApi.DeleteResult.Transient ->
                            removeError = "Couldn't reach the backend: ${r.message}"
                    }
                    removing = false
                }
            },
            onDismiss = { confirmRemove = false }
        )
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Text(title, maxLines = 1, overflow = TextOverflow.Ellipsis)
                },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.Filled.ArrowBack, contentDescription = "Back")
                    }
                }
            )
        }
    ) { padding ->
        Column(
            Modifier.padding(padding).fillMaxSize().padding(20.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            Text(
                "We couldn't analyze this item",
                style = MaterialTheme.typography.headlineSmall,
                fontWeight = FontWeight.Bold
            )
            Text(
                failureCopy(status?.failureCode),
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )

            when (val rs = retryState) {
                RetryUi.Idle -> {
                    if (retryable) {
                        Button(
                            onClick = {
                                retryState = RetryUi.Working
                                scope.launch {
                                    when (val r = api.reprocessMemory(memoryId)) {
                                        is MemoryApi.ReprocessResult.Accepted -> {
                                            retryState = RetryUi.Accepted
                                        }
                                        is MemoryApi.ReprocessResult.NotFound ->
                                            retryState = RetryUi.Error("It's no longer on the backend.")
                                        is MemoryApi.ReprocessResult.Unauthenticated ->
                                            retryState = RetryUi.Error("Signed out — sign in and try again.")
                                        is MemoryApi.ReprocessResult.QuotaExceeded ->
                                            retryState = RetryUi.Error("Free quota exhausted — upgrade to Pro.")
                                        is MemoryApi.ReprocessResult.Transient ->
                                            retryState = RetryUi.Error("Couldn't reach the backend: ${r.message}")
                                    }
                                }
                            },
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Text("Try again")
                        }
                    } else {
                        detail?.effectiveUrl?.let { url ->
                            Button(
                                onClick = { openUrl(context, url) },
                                modifier = Modifier.fillMaxWidth()
                            ) {
                                Text("Open original")
                            }
                        }
                    }
                }
                RetryUi.Working -> {
                    LinearProgressIndicator(Modifier.fillMaxWidth())
                    Text(
                        "Trying again…",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                RetryUi.Accepted -> {
                    Text(
                        "It's back in the queue — check the Inbox for progress.",
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Button(onClick = onBack, modifier = Modifier.fillMaxWidth()) {
                        Text("Back to Inbox")
                    }
                }
                is RetryUi.Error -> {
                    Text(rs.message, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
                    TextButton(onClick = { retryState = RetryUi.Idle }) { Text("Dismiss") }
                }
            }

            TextButton(
                onClick = { confirmRemove = true },
                enabled = !removing,
                modifier = Modifier.fillMaxWidth()
            ) {
                Text(if (removing) "Removing…" else "Remove")
            }
            removeError?.let {
                Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
            }

            // Raw technical detail lives ONLY in the hidden developer view.
            if (devDiagnostics) {
                Text(
                    "Developer detail\ncode: ${status?.failureCode ?: "—"}\n" +
                        "attempts: ${status?.attemptCount ?: 0}\n" +
                        (status?.failureMessage ?: "no message"),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }
    }
}

private sealed interface RetryUi {
    data object Idle : RetryUi
    data object Working : RetryUi
    data object Accepted : RetryUi
    data class Error(val message: String) : RetryUi
}
