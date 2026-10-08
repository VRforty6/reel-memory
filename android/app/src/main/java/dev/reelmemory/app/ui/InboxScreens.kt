package dev.reelmemory.app.ui

import androidx.compose.foundation.clickable
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
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.reelmemory.app.data.AlbumUpload
import dev.reelmemory.app.data.QueuedShare
import dev.reelmemory.app.data.ShareStatus
import dev.reelmemory.app.data.VideoUpload
import dev.reelmemory.app.data.VideoUploadStatus
import dev.reelmemory.app.data.WebCapture
import dev.reelmemory.app.data.WebCaptureStatus
import dev.reelmemory.app.net.MemoryItem
import dev.reelmemory.app.net.UploadApi
import dev.reelmemory.app.sync.StatusCheckWorker
import dev.reelmemory.app.sync.UploadWorker
import kotlinx.coroutines.flow.StateFlow
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Inbox: everything still on its way to becoming a memory.
 *
 * - Local rows that are not ready yet (queued links, web captures, video /
 *   album uploads), reusing the existing ShareCard / WebCaptureCard /
 *   VideoUploadCard / AlbumUploadCard.
 * - Backend memories whose status is not READY (processing, link-only,
 *   failed). Tapping one opens the state-appropriate detail screen.
 */
@Composable
fun InboxScreen(
    queue: StateFlow<List<QueuedShare>>,
    videos: StateFlow<List<VideoUpload>>,
    albums: StateFlow<List<AlbumUpload>>,
    webCaptures: StateFlow<List<WebCapture>>,
    backendMemories: List<MemoryItem>?,
    onOpenBackend: (MemoryItem) -> Unit,
    onAskLocal: (memoryId: String, title: String) -> Unit,
    modifier: Modifier = Modifier,
) {
    val shares by queue.collectAsState()
    val uploads by videos.collectAsState()
    val albumUploads by albums.collectAsState()
    val webLinks by webCaptures.collectAsState()

    val pendingShares = shares.filter { it.status != ShareStatus.SYNCED }
    val pendingWeb = webLinks.filter { it.status != WebCaptureStatus.SYNCED }
    val pendingUploads = uploads.filter { it.status != VideoUploadStatus.READY }
    val pendingAlbums = albumUploads.filter { it.status != VideoUploadStatus.READY }
    val backendPending = backendMemories?.filter { it.processingStatus != "READY" }.orEmpty()

    // UploadWorker only polls the backend for ~90s after an upload. If the
    // backend needed longer (or a dead worker orphaned the job and the
    // server later recovered it), the card would sit on "Processing…"
    // forever — nothing ever asked again. Re-check on open instead.
    val context = LocalContext.current
    val hasProcessing = uploads.any { it.status == VideoUploadStatus.PROCESSING } ||
        albumUploads.any { it.status == VideoUploadStatus.PROCESSING }
    LaunchedEffect(hasProcessing) {
        if (hasProcessing) StatusCheckWorker.enqueue(context)
    }

    val nothingPending = pendingShares.isEmpty() && pendingWeb.isEmpty() &&
        pendingUploads.isEmpty() && pendingAlbums.isEmpty() && backendPending.isEmpty()

    if (backendMemories != null && nothingPending) {
        Column(
            modifier = modifier.fillMaxSize().padding(24.dp),
            verticalArrangement = Arrangement.Center,
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            Text(
                "All caught up",
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.SemiBold
            )
            Spacer(Modifier.height(8.dp))
            Text(
                "Nothing is waiting to be remembered. Share something new with " +
                    "the + button below.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
        return
    }

    LazyColumn(
        modifier = modifier.fillMaxSize().padding(horizontal = 12.dp, vertical = 8.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        if (pendingUploads.isNotEmpty() || pendingAlbums.isNotEmpty()) {
            item { SectionHeader("Uploading from this phone") }
            items(pendingAlbums, key = { "album-${it.id}" }) { album ->
                AlbumUploadCard(album = album, onAsk = onAskLocal)
            }
            items(pendingUploads, key = { "upload-${it.id}" }) { upload ->
                VideoUploadCard(upload = upload, onAsk = onAskLocal)
            }
        }
        if (pendingShares.isNotEmpty() || pendingWeb.isNotEmpty()) {
            item { SectionHeader("Links waiting to sync") }
            items(pendingWeb, key = { "web-${it.id}" }) { capture ->
                WebCaptureCard(capture)
            }
            items(pendingShares, key = { it.id }) { share ->
                ShareCard(share)
            }
        }
        if (backendPending.isNotEmpty()) {
            item { SectionHeader("Being remembered") }
            items(backendPending, key = { it.id }) { memory ->
                BackendInboxCard(memory = memory, onOpen = { onOpenBackend(memory) })
            }
        }
        if (backendMemories == null) {
            item {
                LinearProgressIndicator(Modifier.fillMaxWidth().padding(vertical = 8.dp))
            }
        }
    }
}

@Composable
private fun SectionHeader(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.titleSmall,
        fontWeight = FontWeight.SemiBold,
        modifier = Modifier.padding(top = 4.dp)
    )
}

/** One backend memory that is not READY yet: processing, link-only, failed. */
@Composable
private fun BackendInboxCard(memory: MemoryItem, onOpen: () -> Unit) {
    Card(
        modifier = Modifier.fillMaxWidth().clickable(onClick = onOpen),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Column(Modifier.padding(12.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                InboxStatusChip(memory.processingStatus)
                memory.category?.let {
                    Text(
                        it,
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
            Spacer(Modifier.height(6.dp))
            Text(
                libraryTitleFallback(memory.title, memory.platform),
                style = MaterialTheme.typography.bodyLarge,
                fontWeight = FontWeight.SemiBold,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis
            )
            memory.summary?.let {
                Spacer(Modifier.height(4.dp))
                Text(
                    it,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis
                )
            }
        }
    }
}

@Composable
private fun InboxStatusChip(status: String) {
    val (label, container) = when (detailRouteFor(status)) {
        DetailRoute.READY -> "Ready" to MaterialTheme.colorScheme.primaryContainer
        DetailRoute.METADATA_ONLY -> "Link only" to MaterialTheme.colorScheme.tertiaryContainer
        DetailRoute.FAILED -> "Couldn't analyze" to MaterialTheme.colorScheme.errorContainer
        DetailRoute.PROCESSING -> "Processing…" to MaterialTheme.colorScheme.secondaryContainer
    }
    Surface(color = container, shape = MaterialTheme.shapes.small) {
        Text(
            label,
            modifier = Modifier.padding(horizontal = 10.dp, vertical = 4.dp),
            style = MaterialTheme.typography.labelMedium,
            fontWeight = FontWeight.SemiBold
        )
    }
}

@Composable
private fun rememberDateFmt() =
    remember { SimpleDateFormat("MMM d, HH:mm", Locale.getDefault()) }

// ---------------------------------------------------------------------------
// Local intake cards (moved from the old Queue/Media tabs, unchanged behavior)
// ---------------------------------------------------------------------------

@Composable
internal fun WebCaptureCard(capture: WebCapture) {
    val dateFmt = rememberDateFmt()
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceVariant
        )
    ) {
        Column(Modifier.padding(12.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                WebCaptureStatusChip(capture.status)
                Text(
                    dateFmt.format(Date(capture.createdAt)),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            Spacer(Modifier.height(6.dp))
            Text(
                capture.url,
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.Medium,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis
            )
            Spacer(Modifier.height(4.dp))
            Text(
                "Captured from the live web page — the backend reads the article " +
                    "so you can ask about it.",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
            capture.lastError?.let { err ->
                Spacer(Modifier.height(4.dp))
                Text(
                    err,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.error,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis
                )
            }
        }
    }
}

@Composable
internal fun WebCaptureStatusChip(status: String) {
    val (label, container) = when (status) {
        WebCaptureStatus.SYNCED -> "Synced" to MaterialTheme.colorScheme.primaryContainer
        WebCaptureStatus.SYNCING -> "Syncing…" to MaterialTheme.colorScheme.secondaryContainer
        WebCaptureStatus.FAILED -> "Retrying" to MaterialTheme.colorScheme.errorContainer
        WebCaptureStatus.QUOTA -> "Over quota" to MaterialTheme.colorScheme.errorContainer
        else -> "Queued" to MaterialTheme.colorScheme.tertiaryContainer
    }
    Surface(
        color = container,
        shape = MaterialTheme.shapes.small
    ) {
        Text(
            label,
            modifier = Modifier.padding(horizontal = 10.dp, vertical = 4.dp),
            style = MaterialTheme.typography.labelMedium,
            fontWeight = FontWeight.SemiBold
        )
    }
}

@Composable
internal fun ShareCard(share: QueuedShare) {
    val dateFmt = rememberDateFmt()
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceVariant
        )
    ) {
        Column(Modifier.padding(12.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                ShareStatusChip(share.status)
                Text(
                    dateFmt.format(Date(share.createdAt)),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            Spacer(Modifier.height(6.dp))
            Text(
                share.canonicalUrl,
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.Medium,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis
            )
            if (share.originalUrl != share.canonicalUrl) {
                Text(
                    "shared as: ${share.originalUrl}",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
            }
            Spacer(Modifier.height(4.dp))
            Text(
                "Link saved (preview only — video not readable). " +
                    "Share the actual video file to let Reel Memory see it.",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
            share.lastError?.let { err ->
                Spacer(Modifier.height(4.dp))
                Text(
                    err,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.error,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis
                )
            }
        }
    }
}

@Composable
internal fun ShareStatusChip(status: String) {
    val (label, container) = when (status) {
        ShareStatus.SYNCED -> "Synced" to MaterialTheme.colorScheme.primaryContainer
        ShareStatus.SYNCING -> "Syncing…" to MaterialTheme.colorScheme.secondaryContainer
        ShareStatus.FAILED -> "Retrying" to MaterialTheme.colorScheme.errorContainer
        ShareStatus.QUOTA -> "Over quota" to MaterialTheme.colorScheme.errorContainer
        else -> "Queued" to MaterialTheme.colorScheme.tertiaryContainer
    }
    Surface(
        color = container,
        shape = MaterialTheme.shapes.small
    ) {
        Text(
            label,
            modifier = Modifier.padding(horizontal = 10.dp, vertical = 4.dp),
            style = MaterialTheme.typography.labelMedium,
            fontWeight = FontWeight.SemiBold
        )
    }
}

/**
 * One carousel/album share: photo-count badge, whole-batch progress while
 * UPLOADING, then processing → ready. One share = one upload = one quota
 * unit, no matter the photo count.
 */
@Composable
internal fun AlbumUploadCard(
    album: AlbumUpload,
    onAsk: (memoryId: String, title: String) -> Unit
) {
    val context = LocalContext.current
    val dateFmt = rememberDateFmt()
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceVariant
        )
    ) {
        Column(Modifier.padding(12.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    VideoStatusChip(album.status)
                    Spacer(Modifier.width(6.dp))
                    Surface(
                        color = MaterialTheme.colorScheme.tertiaryContainer,
                        shape = MaterialTheme.shapes.extraSmall
                    ) {
                        Text(
                            "${album.fileCount} photos",
                            modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp),
                            style = MaterialTheme.typography.labelSmall,
                            fontWeight = FontWeight.Bold
                        )
                    }
                }
                Text(
                    dateFmt.format(Date(album.createdAt)),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            Spacer(Modifier.height(6.dp))
            Text(
                "Album (${album.fileCount} photos)",
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.Medium,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )
            Text(
                UploadApi.formatBytes(album.totalBytes),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
            when (album.status) {
                VideoUploadStatus.UPLOADING -> {
                    Spacer(Modifier.height(8.dp))
                    LinearProgressIndicator(
                        progress = { album.progress / 100f },
                        modifier = Modifier.fillMaxWidth()
                    )
                    Spacer(Modifier.height(4.dp))
                    Text(
                        "Uploading album… ${album.progress}%",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                VideoUploadStatus.PROCESSING -> {
                    Spacer(Modifier.height(8.dp))
                    LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                    Spacer(Modifier.height(4.dp))
                    Text(
                        "Backend is reading all ${album.fileCount} photos…",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Spacer(Modifier.height(4.dp))
                    TextButton(onClick = { StatusCheckWorker.enqueue(context) }) {
                        Text("Check status")
                    }
                }
                VideoUploadStatus.READY -> {
                    Spacer(Modifier.height(8.dp))
                    Button(
                        onClick = {
                            album.remoteMemoryId?.let {
                                onAsk(it, "Album (${album.fileCount} photos)")
                            }
                        },
                        enabled = album.remoteMemoryId != null
                    ) {
                        Text("Ask about this album")
                    }
                }
                VideoUploadStatus.FAILED -> {
                    album.lastError?.let { err ->
                        Spacer(Modifier.height(4.dp))
                        Text(
                            err,
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.error,
                            maxLines = 3,
                            overflow = TextOverflow.Ellipsis
                        )
                    }
                    Spacer(Modifier.height(4.dp))
                    TextButton(onClick = { UploadWorker.enqueue(context) }) {
                        Text("Retry")
                    }
                }
                VideoUploadStatus.QUOTA -> {
                    Spacer(Modifier.height(4.dp))
                    Text(
                        album.lastError ?: "Over quota — upgrade to Pro",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.error,
                        maxLines = 3,
                        overflow = TextOverflow.Ellipsis
                    )
                }
            }
        }
    }
}

@Composable
internal fun VideoUploadCard(
    upload: VideoUpload,
    onAsk: (memoryId: String, title: String) -> Unit
) {
    val context = LocalContext.current
    val dateFmt = rememberDateFmt()
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceVariant
        )
    ) {
        Column(Modifier.padding(12.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                VideoStatusChip(upload.status)
                Text(
                    dateFmt.format(Date(upload.createdAt)),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            Spacer(Modifier.height(6.dp))
            Text(
                upload.fileName,
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.Medium,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )
            Text(
                UploadApi.formatBytes(upload.sizeBytes),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
            when (upload.status) {
                VideoUploadStatus.UPLOADING -> {
                    Spacer(Modifier.height(8.dp))
                    LinearProgressIndicator(
                        progress = { upload.progress / 100f },
                        modifier = Modifier.fillMaxWidth()
                    )
                    Spacer(Modifier.height(4.dp))
                    Text(
                        "Uploading… ${upload.progress}%",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                VideoUploadStatus.PROCESSING -> {
                    Spacer(Modifier.height(8.dp))
                    LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                    Spacer(Modifier.height(4.dp))
                    Text(
                        "Backend is watching the video…",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Spacer(Modifier.height(4.dp))
                    TextButton(onClick = { StatusCheckWorker.enqueue(context) }) {
                        Text("Check status")
                    }
                }
                VideoUploadStatus.READY -> {
                    Spacer(Modifier.height(8.dp))
                    Button(
                        onClick = {
                            upload.remoteMemoryId?.let { onAsk(it, upload.fileName) }
                        },
                        enabled = upload.remoteMemoryId != null
                    ) {
                        Text("Ask about this reel")
                    }
                }
                VideoUploadStatus.FAILED -> {
                    upload.lastError?.let { err ->
                        Spacer(Modifier.height(4.dp))
                        Text(
                            err,
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.error,
                            maxLines = 3,
                            overflow = TextOverflow.Ellipsis
                        )
                    }
                    Spacer(Modifier.height(4.dp))
                    TextButton(onClick = { UploadWorker.enqueue(context) }) {
                        Text("Retry")
                    }
                }
            }
        }
    }
}

@Composable
internal fun VideoStatusChip(status: String) {
    val (label, container) = when (status) {
        VideoUploadStatus.READY -> "Ready — ask" to MaterialTheme.colorScheme.primaryContainer
        VideoUploadStatus.UPLOADING -> "Uploading…" to MaterialTheme.colorScheme.secondaryContainer
        VideoUploadStatus.PROCESSING -> "Processing…" to MaterialTheme.colorScheme.secondaryContainer
        VideoUploadStatus.FAILED -> "Failed" to MaterialTheme.colorScheme.errorContainer
        VideoUploadStatus.QUOTA -> "Over quota" to MaterialTheme.colorScheme.errorContainer
        else -> "Queued" to MaterialTheme.colorScheme.tertiaryContainer
    }
    Surface(
        color = container,
        shape = MaterialTheme.shapes.small
    ) {
        Text(
            label,
            modifier = Modifier.padding(horizontal = 10.dp, vertical = 4.dp),
            style = MaterialTheme.typography.labelMedium,
            fontWeight = FontWeight.SemiBold
        )
    }
}
