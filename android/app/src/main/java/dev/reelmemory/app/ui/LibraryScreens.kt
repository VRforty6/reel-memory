package dev.reelmemory.app.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Share
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.reelmemory.app.net.MemoryItem

/**
 * Library: every saved memory as thumbnail-forward cards, filterable by
 * All / Ready / Videos / Images / Links. Quick "Ask" for READY memories;
 * anything else opens its state-appropriate detail screen.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun LibraryScreen(
    memories: List<MemoryItem>?,
    error: String?,
    loading: Boolean,
    onRefresh: () -> Unit,
    thumbnailLoader: ThumbnailLoader,
    onOpenMemory: (MemoryItem) -> Unit,
    onAddFirst: () -> Unit,
    modifier: Modifier = Modifier,
) {
    var filter by remember { mutableStateOf(LibraryFilter.ALL) }
    var showHowSharingWorks by remember { mutableStateOf(false) }

    if (showHowSharingWorks) {
        HowSharingWorksDialog(onDismiss = { showHowSharingWorks = false })
    }

    when {
        loading && memories == null -> {
            Column(
                modifier.fillMaxSize(),
                verticalArrangement = Arrangement.Center,
                horizontalAlignment = Alignment.CenterHorizontally
            ) {
                CircularProgressIndicator()
            }
        }
        error != null && memories.isNullOrEmpty() -> {
            LibraryMessage(
                title = "Couldn't load your library",
                body = error,
                actionLabel = "Try again",
                onAction = onRefresh,
                modifier = modifier
            )
        }
        memories.isNullOrEmpty() -> {
            // The true empty state: no memories at all.
            Column(
                modifier = modifier.fillMaxSize().padding(32.dp),
                verticalArrangement = Arrangement.Center,
                horizontalAlignment = Alignment.CenterHorizontally
            ) {
                Text(
                    "Your memory starts here",
                    style = MaterialTheme.typography.headlineSmall,
                    fontWeight = FontWeight.Bold
                )
                Spacer(Modifier.height(12.dp))
                Text(
                    "Save videos, screenshots and ideas. Reel Memory understands " +
                        "what was said, shown and written so you can find it later.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                Spacer(Modifier.height(24.dp))
                Button(
                    onClick = onAddFirst,
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text("Add your first memory")
                }
                Spacer(Modifier.height(8.dp))
                TextButton(onClick = { showHowSharingWorks = true }) {
                    Text("How sharing works")
                }
            }
        }
        else -> {
            val filtered = memories.filter { filter.matches(it.processingStatus, it.mediaKind) }
            LazyColumn(
                modifier = modifier.fillMaxSize().padding(horizontal = 12.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                item {
                    FlowRow(
                        modifier = Modifier.padding(vertical = 8.dp),
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        LibraryFilter.values().forEach { f ->
                            FilterChip(
                                selected = filter == f,
                                onClick = { filter = f },
                                label = { Text(f.label) }
                            )
                        }
                    }
                }
                if (filtered.isEmpty()) {
                    item {
                        LibraryMessage(
                            title = "Nothing in this view yet",
                            body = "Try a different filter — or save something new with the + button.",
                            actionLabel = null,
                            onAction = null,
                            modifier = Modifier.fillMaxWidth()
                        )
                    }
                } else {
                    items(filtered, key = { it.id }) { memory ->
                        LibraryCard(
                            memory = memory,
                            thumbnailLoader = thumbnailLoader,
                            onOpen = { onOpenMemory(memory) }
                        )
                    }
                }
                item { Spacer(Modifier.height(12.dp)) }
            }
        }
    }
}

@Composable
private fun LibraryMessage(
    title: String,
    body: String,
    actionLabel: String?,
    onAction: (() -> Unit)?,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier.padding(24.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Text(title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.height(8.dp))
        Text(
            body,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        if (actionLabel != null && onAction != null) {
            Spacer(Modifier.height(16.dp))
            Button(onClick = onAction) { Text(actionLabel) }
        }
    }
}

/** Library card: thumbnail-forward for real media, compact source/link card otherwise. */
@Composable
private fun LibraryCard(
    memory: MemoryItem,
    thumbnailLoader: ThumbnailLoader,
    onOpen: () -> Unit,
) {
    if (isMediaItem(memory.mediaKind)) {
        MediaLibraryCard(memory = memory, thumbnailLoader = thumbnailLoader, onOpen = onOpen)
    } else {
        // Link-only and non-media items: no thumbnail, no huge empty 16:9
        // placeholder — a compact card with platform icon, title/source,
        // and state chip.
        CompactLinkCard(memory = memory, onOpen = onOpen)
    }
}

/** Thumbnail-forward card: image, title, category, summary, source, date, quick Ask. */
@Composable
private fun MediaLibraryCard(
    memory: MemoryItem,
    thumbnailLoader: ThumbnailLoader,
    onOpen: () -> Unit,
) {
    val route = detailRouteFor(memory.processingStatus)
    Card(
        modifier = Modifier.fillMaxWidth().clickable(onClick = onOpen),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Column {
            MemoryThumbnail(
                memoryId = memory.id,
                loader = thumbnailLoader,
                modifier = Modifier.fillMaxWidth().aspectRatio(16f / 9f),
                contentDescription = null,
                hasThumbnail = memory.hasThumbnail
            )
            Column(Modifier.padding(12.dp)) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    LibraryStatusChip(route)
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
                    style = MaterialTheme.typography.titleSmall,
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
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis
                    )
                }
                Spacer(Modifier.height(8.dp))
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        memory.platform?.let {
                            PlatformLabel(it)
                        }
                        val saved = formatSavedDate(memory.createdAt)
                        if (saved.isNotBlank()) {
                            Text(
                                saved,
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                    }
                    if (route == DetailRoute.READY) {
                        TextButton(onClick = onOpen) { Text("Ask") }
                    }
                }
            }
        }
    }
}

@Composable
private fun CompactLinkCard(memory: MemoryItem, onOpen: () -> Unit) {
    val route = detailRouteFor(memory.processingStatus)
    Card(
        modifier = Modifier.fillMaxWidth().clickable(onClick = onOpen),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Row(
            modifier = Modifier.padding(12.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Surface(
                color = MaterialTheme.colorScheme.tertiaryContainer,
                shape = RoundedCornerShape(12.dp)
            ) {
                Icon(
                    Icons.Filled.Share,
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.onTertiaryContainer,
                    modifier = Modifier.padding(10.dp).size(20.dp)
                )
            }
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    LibraryStatusChip(route)
                    memory.platform?.let { PlatformLabel(it) }
                }
                Spacer(Modifier.height(6.dp))
                Text(
                    libraryTitleFallback(memory.title, memory.platform),
                    style = MaterialTheme.typography.titleSmall,
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
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis
                    )
                }
                val saved = formatSavedDate(memory.createdAt)
                if (saved.isNotBlank()) {
                    Spacer(Modifier.height(4.dp))
                    Text(
                        saved,
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
        }
    }
}

@Composable
private fun LibraryStatusChip(route: DetailRoute) {
    val (label, container) = when (route) {
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
private fun PlatformLabel(platform: String) {
    Surface(
        color = MaterialTheme.colorScheme.surfaceVariant,
        shape = MaterialTheme.shapes.extraSmall
    ) {
        Text(
            platformDisplayName(platform),
            modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp),
            style = MaterialTheme.typography.labelSmall,
            fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
    }
}

/**
 * The honest Instagram limitation, shown from the Library empty state and
 * the Help screen.
 */
@Composable
fun HowSharingWorksDialog(onDismiss: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("How sharing works") },
        text = {
            Text(
                "Reel Memory understands a video only when it receives the " +
                    "actual video file — shared from your gallery or another " +
                    "app through the Android share sheet.\n\n" +
                    "A plain link (for example, an Instagram reel URL) saves " +
                    "as link-only: Instagram shares the link but not the " +
                    "video itself, so Reel Memory can't see or hear it yet. " +
                    "To make a link analyzable, save the video to your phone " +
                    "first, then share the saved file."
            )
        },
        confirmButton = {
            TextButton(onClick = onDismiss) { Text("Got it") }
        }
    )
}
