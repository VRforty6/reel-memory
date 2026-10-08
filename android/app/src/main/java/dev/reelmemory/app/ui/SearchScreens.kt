package dev.reelmemory.app.ui

import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Share
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.reelmemory.app.net.SearchApi
import dev.reelmemory.app.net.SearchResultItem
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch

private val EXAMPLE_QUERIES = listOf(
    "guy with glasses sitting on a colorful couch",
    "video about creating virtual IP",
    "red motorcycle in a garage",
    "person writing on a sticky note",
    "AI tool that generates videos",
    "the reel that mentioned WhatsApp automation",
)

private sealed interface SearchUiState {
    data object Idle : SearchUiState
    data object Loading : SearchUiState
    data class Results(val items: List<SearchResultItem>, val degraded: Boolean) : SearchUiState
    data class Error(val message: String) : SearchUiState
}

/**
 * The hero tab: "What do you remember?" over the whole saved library.
 * Scores are never rendered; every hit explains itself with a
 * why-it-matched row grounded in its evidence.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun SearchScreen(
    api: SearchApi,
    thumbnailLoader: ThumbnailLoader,
    onOpenResult: (SearchResultItem) -> Unit,
    modifier: Modifier = Modifier,
) {
    var query by remember { mutableStateOf("") }
    var state by remember { mutableStateOf<SearchUiState>(SearchUiState.Idle) }
    var searchJob by remember { mutableStateOf<Job?>(null) }
    val scope = rememberCoroutineScope()

    fun runSearch(q: String) {
        val trimmed = q.trim()
        if (trimmed.isEmpty()) return
        query = trimmed
        state = SearchUiState.Loading
        searchJob?.cancel()
        searchJob = scope.launch {
            state = when (val r = api.search(trimmed)) {
                is SearchApi.SearchOutcome.Ok ->
                    SearchUiState.Results(r.results, r.degraded)
                is SearchApi.SearchOutcome.Unauthenticated ->
                    SearchUiState.Error("Signed out — sign in to search your memories.")
                is SearchApi.SearchOutcome.QuotaExceeded ->
                    SearchUiState.Error("Free quota exhausted — upgrade to Pro to keep searching.")
                is SearchApi.SearchOutcome.Transient ->
                    SearchUiState.Error("Couldn't reach the backend: ${r.message}")
            }
        }
    }

    LazyColumn(
        modifier = modifier.fillMaxSize().padding(horizontal = 16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        item {
            Spacer(Modifier.height(8.dp))
            Text(
                "What do you remember?",
                style = MaterialTheme.typography.headlineMedium,
                fontWeight = FontWeight.Bold
            )
            Spacer(Modifier.height(6.dp))
            Text(
                "Remember a face, object, phrase, idea, screen, place, color, or anything else.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
        item {
            OutlinedTextField(
                value = query,
                onValueChange = { query = it },
                modifier = Modifier.fillMaxWidth(),
                placeholder = { Text("Search everything you've saved") },
                leadingIcon = { Icon(Icons.Filled.Search, contentDescription = null) },
                singleLine = true,
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                keyboardActions = KeyboardActions(onSearch = { runSearch(query) }),
                shape = RoundedCornerShape(28.dp)
            )
        }
        item {
            FlowRow(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                EXAMPLE_QUERIES.forEach { example ->
                    AssistChip(
                        onClick = { runSearch(example) },
                        label = {
                            Text(
                                example,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis
                            )
                        }
                    )
                }
            }
        }

        when (val s = state) {
            SearchUiState.Idle -> {
                item {
                    Box(
                        Modifier.fillMaxWidth().padding(vertical = 32.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Text(
                            "Try one of the examples above, or describe what you remember.",
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                }
            }
            SearchUiState.Loading -> {
                items(4) { SearchSkeletonCard() }
            }
            is SearchUiState.Results -> {
                if (s.items.isEmpty()) {
                    item {
                        Column(
                            Modifier.fillMaxWidth().padding(vertical = 32.dp),
                            horizontalAlignment = Alignment.CenterHorizontally
                        ) {
                            Text(
                                "No matches",
                                style = MaterialTheme.typography.titleMedium,
                                fontWeight = FontWeight.SemiBold
                            )
                            Spacer(Modifier.height(8.dp))
                            Text(
                                "Try different words — a color, an object, a phrase " +
                                    "you remember hearing.",
                                style = MaterialTheme.typography.bodyMedium,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                    }
                } else {
                    if (s.degraded) {
                        item {
                            Text(
                                "Search may be incomplete right now — showing what we found.",
                                style = MaterialTheme.typography.labelMedium,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                    }
                    items(s.items, key = { it.memoryId }) { item ->
                        SearchResultCard(
                            item = item,
                            thumbnailLoader = thumbnailLoader,
                            onOpen = { onOpenResult(item) }
                        )
                    }
                }
            }
            is SearchUiState.Error -> {
                item {
                    Column(
                        Modifier.fillMaxWidth().padding(vertical = 32.dp),
                        horizontalAlignment = Alignment.CenterHorizontally,
                        verticalArrangement = Arrangement.spacedBy(12.dp)
                    ) {
                        Text(
                            "Couldn't search",
                            style = MaterialTheme.typography.titleMedium,
                            fontWeight = FontWeight.SemiBold
                        )
                        Text(
                            s.message,
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                        Button(onClick = { runSearch(query) }) { Text("Try again") }
                    }
                }
            }
        }
        item { Spacer(Modifier.height(12.dp)) }
    }
}

@Composable
private fun SearchResultCard(
    item: SearchResultItem,
    thumbnailLoader: ThumbnailLoader,
    onOpen: () -> Unit,
) {
    val title = searchResultTitle(item.title, item.creatorHandle, item.platform)
    val snippet = searchResultSnippet(item.evidence.firstOrNull()?.snippet, item.summary)
    val topEvidence = item.evidence.firstOrNull()
    val why = topEvidence?.let { whyMatch(it.type) }

    Card(
        modifier = Modifier.fillMaxWidth().clickable(onClick = onOpen),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Row(Modifier.padding(12.dp), verticalAlignment = Alignment.Top) {
            if (isMediaItem(item.mediaKind)) {
                MemoryThumbnail(
                    memoryId = item.memoryId,
                    loader = thumbnailLoader,
                    modifier = Modifier.size(96.dp).clip(RoundedCornerShape(12.dp)),
                    contentDescription = null,
                    hasThumbnail = item.hasThumbnail
                )
            } else {
                // Link-only / non-media hits: compact source card — no
                // thumbnail, no empty placeholder.
                Surface(
                    color = MaterialTheme.colorScheme.tertiaryContainer,
                    shape = RoundedCornerShape(12.dp)
                ) {
                    Icon(
                        Icons.Filled.Share,
                        contentDescription = null,
                        tint = MaterialTheme.colorScheme.onTertiaryContainer,
                        modifier = Modifier.padding(12.dp).size(24.dp)
                    )
                }
            }
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    Text(
                        title,
                        style = MaterialTheme.typography.titleSmall,
                        fontWeight = FontWeight.SemiBold,
                        maxLines = 2,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f, fill = false)
                    )
                    if (item.processingStatus == "METADATA_ONLY") {
                        LinkOnlyMiniChip()
                    }
                }
                if (snippet.isNotBlank()) {
                    Spacer(Modifier.height(4.dp))
                    Text(
                        snippet,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        maxLines = 2,
                        overflow = TextOverflow.Ellipsis
                    )
                }
                if (why != null && topEvidence != null) {
                    Spacer(Modifier.height(8.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            "${why.icon} ${why.label}",
                            style = MaterialTheme.typography.labelMedium,
                            fontWeight = FontWeight.SemiBold,
                            color = MaterialTheme.colorScheme.primary
                        )
                        evidenceTimeSuffix(topEvidence.startMs)?.let { suffix ->
                            Text(
                                " $suffix",
                                style = MaterialTheme.typography.labelMedium,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                        if (item.evidence.size > 1) {
                            Text(
                                "  +${item.evidence.size - 1} more",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun LinkOnlyMiniChip() {
    Surface(
        color = MaterialTheme.colorScheme.tertiaryContainer,
        shape = MaterialTheme.shapes.extraSmall
    ) {
        Text(
            "Link only",
            modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp),
            style = MaterialTheme.typography.labelSmall,
            fontWeight = FontWeight.Bold
        )
    }
}

/** Pulsing placeholder cards while search is in flight. */
@Composable
private fun SearchSkeletonCard() {
    val transition = rememberInfiniteTransition()
    val alpha by transition.animateFloat(
        initialValue = 0.45f,
        targetValue = 0.9f,
        animationSpec = infiniteRepeatable(
            animation = tween(900),
            repeatMode = RepeatMode.Reverse
        )
    )
    Card(
        modifier = Modifier.fillMaxWidth().alpha(alpha),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Row(Modifier.padding(12.dp)) {
            Box(
                Modifier.size(96.dp).clip(RoundedCornerShape(12.dp))
            ) {
                Surface(
                    color = MaterialTheme.colorScheme.surfaceVariant,
                    modifier = Modifier.fillMaxSize()
                ) {}
            }
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Surface(
                    color = MaterialTheme.colorScheme.surfaceVariant,
                    shape = RoundedCornerShape(6.dp),
                    modifier = Modifier.fillMaxWidth(0.7f).height(16.dp)
                ) {}
                Surface(
                    color = MaterialTheme.colorScheme.surfaceVariant,
                    shape = RoundedCornerShape(6.dp),
                    modifier = Modifier.fillMaxWidth().height(12.dp)
                ) {}
                Surface(
                    color = MaterialTheme.colorScheme.surfaceVariant,
                    shape = RoundedCornerShape(6.dp),
                    modifier = Modifier.fillMaxWidth(0.5f).height(12.dp)
                ) {}
            }
        }
    }
}
