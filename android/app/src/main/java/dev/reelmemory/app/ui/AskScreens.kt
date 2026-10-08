package dev.reelmemory.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
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
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Send
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateListOf
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
import dev.reelmemory.app.data.BriefDecision
import dev.reelmemory.app.data.ShareDatabase
import dev.reelmemory.app.net.ActionItem
import dev.reelmemory.app.net.AskResponse
import dev.reelmemory.app.net.AskVerification
import dev.reelmemory.app.net.DecisionBrief
import dev.reelmemory.app.net.EvidenceChip
import dev.reelmemory.app.net.MemoryApi
import dev.reelmemory.app.net.MemoryStatus
import dev.reelmemory.app.net.Verdict
import dev.reelmemory.app.net.formatTimestamp
import dev.reelmemory.app.net.timestampLabel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

// ---------------------------------------------------------------------------
// Ask screen — chat about one reel
// ---------------------------------------------------------------------------

private sealed interface ChatMsg {
    data class User(val text: String) : ChatMsg
    data class Assistant(val response: AskResponse) : ChatMsg
    data class Error(val text: String) : ChatMsg
}

// ---------------------------------------------------------------------------
// Memory detail: Ask / Actions / Brief tabs
// ---------------------------------------------------------------------------

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MemoryDetailScreen(
    memoryId: String,
    title: String,
    api: MemoryApi,
    onBack: () -> Unit,
    modifier: Modifier = Modifier
) {
    var memStatus by remember { mutableStateOf<MemoryStatus?>(null) }
    var statusError by remember { mutableStateOf<String?>(null) }
    var tab by remember { mutableIntStateOf(0) }

    // Poll the backend until the memory is READY (or terminally failed).
    LaunchedEffect(memoryId) {
        repeat(60) {
            when (val s = api.getStatus(memoryId)) {
                is MemoryApi.StatusResult.Ok -> {
                    memStatus = s.status
                    statusError = null
                    if (s.status.isReady || s.status.isTerminalFailure) return@LaunchedEffect
                }
                is MemoryApi.StatusResult.NotFound -> {
                    statusError = s.message
                    return@LaunchedEffect
                }
                is MemoryApi.StatusResult.Unauthenticated -> {
                    statusError = "Signed out — sign in to continue."
                    return@LaunchedEffect
                }
                is MemoryApi.StatusResult.QuotaExceeded -> {
                    statusError = "Free quota exhausted — upgrade to Pro to continue."
                    return@LaunchedEffect
                }
                is MemoryApi.StatusResult.Transient -> {
                    if (memStatus == null) statusError = s.message
                }
            }
            delay(3_000)
        }
    }

    Scaffold(
        modifier = modifier,
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
        Column(Modifier.padding(padding).fillMaxSize()) {
            TabRow(selectedTabIndex = tab) {
                Tab(selected = tab == 0, onClick = { tab = 0 }, text = { Text("Ask") })
                Tab(selected = tab == 1, onClick = { tab = 1 }, text = { Text("Actions") })
                Tab(selected = tab == 2, onClick = { tab = 2 }, text = { Text("Brief") })
            }
            when (tab) {
                0 -> AskTab(
                    memoryId = memoryId,
                    api = api,
                    memStatus = memStatus,
                    statusError = statusError,
                    modifier = Modifier.fillMaxSize()
                )
                1 -> ActionsTab(
                    memoryId = memoryId,
                    api = api,
                    memStatus = memStatus,
                    statusError = statusError,
                    modifier = Modifier.fillMaxSize()
                )
                else -> BriefTab(
                    memoryId = memoryId,
                    api = api,
                    memStatus = memStatus,
                    statusError = statusError,
                    modifier = Modifier.fillMaxSize()
                )
            }
        }
    }
}

/** GPT-style Q&A over one memory's evidence. */
@Composable
private fun AskTab(
    memoryId: String,
    api: MemoryApi,
    memStatus: MemoryStatus?,
    statusError: String?,
    modifier: Modifier = Modifier
) {
    val messages = remember { mutableStateListOf<ChatMsg>() }
    var draft by remember { mutableStateOf("") }
    var verify by remember { mutableStateOf(false) }
    var sending by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    val ready = memStatus?.isReady == true

    fun send() {
        val q = draft.trim()
        if (q.isEmpty() || sending || !ready) return
        messages += ChatMsg.User(q)
        draft = ""
        sending = true
        val wantVerify = verify
        scope.launch {
            when (val r = api.ask(memoryId, q, wantVerify)) {
                is MemoryApi.AskResult.Answer -> messages += ChatMsg.Assistant(r.response)
                is MemoryApi.AskResult.NotReady ->
                    messages += ChatMsg.Error("Still processing — try again in a bit.")
                is MemoryApi.AskResult.Rejected ->
                    messages += ChatMsg.Error(r.message)
                is MemoryApi.AskResult.Unauthenticated ->
                    messages += ChatMsg.Error("Signed out — sign in and try again.")
                is MemoryApi.AskResult.QuotaExceeded ->
                    messages += ChatMsg.Error(
                        "You've hit your free question limit — " +
                            "upgrade to Pro to keep asking."
                    )
                is MemoryApi.AskResult.Transient ->
                    messages += ChatMsg.Error("Couldn't reach the backend: ${r.message}")
            }
            sending = false
        }
    }

    Scaffold(
        modifier = modifier,
        bottomBar = {
            AskInputBar(
                draft = draft,
                onDraftChange = { draft = it },
                verify = verify,
                onVerifyChange = { verify = it },
                sending = sending,
                enabled = ready,
                onSend = { send() }
            )
        }
    ) { padding ->
        Column(Modifier.padding(padding).fillMaxSize()) {
            StatusBanner(memStatus, statusError)
            if (messages.isEmpty()) {
                Box(Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
                    Text(
                        if (ready) "Ask anything about this reel — what was said, " +
                            "shown, or written on screen."
                        else "Waiting for the backend to finish watching this reel…",
                        modifier = Modifier.padding(24.dp),
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            } else {
                LazyColumn(
                    modifier = Modifier.weight(1f).fillMaxWidth()
                        .padding(horizontal = 12.dp, vertical = 8.dp),
                    verticalArrangement = Arrangement.spacedBy(10.dp)
                ) {
                    items(messages, key = { it.hashCode() }) { msg ->
                        when (msg) {
                            is ChatMsg.User -> UserBubble(msg.text)
                            is ChatMsg.Assistant -> AnswerCard(msg.response)
                            is ChatMsg.Error -> ErrorBubble(msg.text)
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun StatusBanner(status: MemoryStatus?, error: String?) {
    when {
        error != null && status == null -> {
            Row(
                Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Icon(
                    Icons.Filled.Warning,
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.error
                )
                Spacer(Modifier.width(8.dp))
                Text(
                    error,
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.error
                )
            }
        }
        status == null -> {
            LinearProgressIndicator(Modifier.fillMaxWidth())
        }
        !status.isReady && !status.isTerminalFailure -> {
            Column(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp)) {
                Text(
                    // A backend auto-retry never leaks its raw status code.
                    if (isAutoRetrying(status.processingStatus)) "Retrying automatically…"
                    else "Watching the reel… ${status.stage ?: status.processingStatus}",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                Spacer(Modifier.height(4.dp))
                LinearProgressIndicator(Modifier.fillMaxWidth())
            }
        }
        status.isTerminalFailure -> {
            Text(
                "Processing failed: ${status.failureMessage ?: status.processingStatus}",
                modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp),
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.error
            )
        }
    }
}

@Composable
private fun AskInputBar(
    draft: String,
    onDraftChange: (String) -> Unit,
    verify: Boolean,
    onVerifyChange: (Boolean) -> Unit,
    sending: Boolean,
    enabled: Boolean,
    onSend: () -> Unit
) {
    Surface(shadowElevation = 4.dp) {
        Column(Modifier.fillMaxWidth().padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Checkbox(checked = verify, onCheckedChange = onVerifyChange, enabled = enabled)
                Text(
                    "Verify with web",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(
                    value = draft,
                    onValueChange = onDraftChange,
                    modifier = Modifier.weight(1f),
                    placeholder = { Text("Ask about this reel…") },
                    singleLine = false,
                    maxLines = 4,
                    enabled = enabled && !sending
                )
                Spacer(Modifier.width(8.dp))
                IconButton(
                    onClick = onSend,
                    enabled = enabled && !sending && draft.isNotBlank()
                ) {
                    if (sending) {
                        CircularProgressIndicator(
                            modifier = Modifier.padding(8.dp),
                            strokeWidth = 2.dp
                        )
                    } else {
                        Icon(Icons.Filled.Send, contentDescription = "Send question")
                    }
                }
            }
        }
    }
}

@Composable
private fun UserBubble(text: String) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
        Surface(
            color = MaterialTheme.colorScheme.primaryContainer,
            shape = MaterialTheme.shapes.medium
        ) {
            Text(
                text,
                modifier = Modifier.padding(12.dp),
                style = MaterialTheme.typography.bodyMedium
            )
        }
    }
}

@Composable
private fun ErrorBubble(text: String) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
        Surface(
            color = MaterialTheme.colorScheme.errorContainer,
            shape = MaterialTheme.shapes.small
        ) {
            Text(
                text,
                modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                style = MaterialTheme.typography.labelMedium
            )
        }
    }
}

/**
 * One assistant answer: "What the reel says" (grounded answer + evidence
 * chips) kept visually separate from "Web check" (verification findings).
 */
@Composable
private fun AnswerCard(response: AskResponse) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)
    ) {
        Column(Modifier.padding(12.dp)) {
            // ---- what the reel says ----
            Text(
                "What the reel says",
                style = MaterialTheme.typography.labelMedium,
                fontWeight = FontWeight.Bold,
                color = MaterialTheme.colorScheme.primary
            )
            Spacer(Modifier.height(4.dp))
            Text(response.answer, style = MaterialTheme.typography.bodyMedium)
            if (response.evidence.isNotEmpty()) {
                Spacer(Modifier.height(8.dp))
                Text(
                    "Evidence",
                    style = MaterialTheme.typography.labelSmall,
                    fontWeight = FontWeight.SemiBold,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                Spacer(Modifier.height(4.dp))
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    response.evidence.forEach { chip ->
                        EvidenceRow(chip)
                    }
                }
            }
            // ---- what the web confirms ----
            response.verification?.let { verification ->
                Spacer(Modifier.height(10.dp))
                HorizontalDivider()
                Spacer(Modifier.height(8.dp))
                VerificationSection(verification)
            }
        }
    }
}

@Composable
private fun EvidenceRow(chip: EvidenceChip) {
    Surface(
        color = MaterialTheme.colorScheme.surface,
        shape = MaterialTheme.shapes.small,
        modifier = Modifier.fillMaxWidth()
    ) {
        Column(Modifier.padding(horizontal = 10.dp, vertical = 6.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Surface(
                    color = MaterialTheme.colorScheme.secondaryContainer,
                    shape = MaterialTheme.shapes.extraSmall
                ) {
                    Text(
                        chip.modality,
                        modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp),
                        style = MaterialTheme.typography.labelSmall,
                        fontWeight = FontWeight.Bold
                    )
                }
                Spacer(Modifier.width(8.dp))
                chip.photoLabel?.let { photo ->
                    Surface(
                        color = MaterialTheme.colorScheme.tertiaryContainer,
                        shape = MaterialTheme.shapes.extraSmall
                    ) {
                        Text(
                            photo,
                            modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp),
                            style = MaterialTheme.typography.labelSmall,
                            fontWeight = FontWeight.Bold
                        )
                    }
                    Spacer(Modifier.width(8.dp))
                }
                Text(
                    chip.timestampLabel(),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            if (chip.excerpt.isNotBlank()) {
                Spacer(Modifier.height(2.dp))
                Text(
                    chip.excerpt,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 4,
                    overflow = TextOverflow.Ellipsis
                )
            }
        }
    }
}

@Composable
private fun VerificationSection(verification: AskVerification) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Icon(
            Icons.Filled.Search,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.onSurfaceVariant
        )
        Spacer(Modifier.width(6.dp))
        Text(
            "Web check",
            style = MaterialTheme.typography.labelMedium,
            fontWeight = FontWeight.Bold
        )
        Spacer(Modifier.width(8.dp))
        VerdictBadge(verification.status)
    }
    Spacer(Modifier.height(6.dp))
    if (verification.findings.isEmpty()) {
        Text(
            "No specific claims were checked.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
    } else {
        Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
            verification.findings.forEach { finding ->
                FindingRow(finding)
            }
        }
    }
}

@Composable
private fun VerdictBadge(verdict: Verdict) {
    val (label, container) = when (verdict) {
        Verdict.SUPPORTED -> "Supported" to MaterialTheme.colorScheme.primaryContainer
        Verdict.CONTRADICTED -> "Contradicted" to MaterialTheme.colorScheme.errorContainer
        Verdict.UNCERTAIN -> "Uncertain" to MaterialTheme.colorScheme.tertiaryContainer
        Verdict.UNKNOWN -> "Unknown" to MaterialTheme.colorScheme.surfaceVariant
    }
    val icon = when (verdict) {
        Verdict.SUPPORTED -> Icons.Filled.CheckCircle
        Verdict.CONTRADICTED, Verdict.UNCERTAIN -> Icons.Filled.Warning
        Verdict.UNKNOWN -> null
    }
    Surface(color = container, shape = MaterialTheme.shapes.small) {
        Row(
            Modifier.padding(horizontal = 10.dp, vertical = 4.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            icon?.let {
                Icon(it, contentDescription = null, modifier = Modifier.padding(end = 4.dp))
            }
            Text(
                label,
                style = MaterialTheme.typography.labelMedium,
                fontWeight = FontWeight.SemiBold
            )
        }
    }
}

@Composable
private fun FindingRow(finding: dev.reelmemory.app.net.VerificationFinding) {
    Column(Modifier.fillMaxWidth()) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            VerdictBadge(finding.verdict)
        }
        Spacer(Modifier.height(2.dp))
        Text(finding.claim, style = MaterialTheme.typography.bodySmall)
        finding.sources.forEach { source ->
            Text(
                "↳ ${source.title.ifBlank { source.url }}",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.primary,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis
            )
            if (source.url.isNotBlank()) {
                Text(
                    source.url,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Actions tab: to-dos derived from a memory
// ---------------------------------------------------------------------------

/** To-do list view; loads once the memory is READY. */
@Composable
private fun ActionsTab(
    memoryId: String,
    api: MemoryApi,
    memStatus: MemoryStatus?,
    statusError: String?,
    modifier: Modifier = Modifier
) {
    val scope = rememberCoroutineScope()
    var actions by remember { mutableStateOf<List<ActionItem>?>(null) }
    var loading by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val ready = memStatus?.isReady == true

    fun load() {
        if (loading) return
        loading = true
        error = null
        scope.launch {
            when (val r = api.getActions(memoryId)) {
                is MemoryApi.ActionsResult.Loaded -> actions = r.actions
                is MemoryApi.ActionsResult.NotReady ->
                    error = "Still processing — try again in a bit."
                is MemoryApi.ActionsResult.Rejected -> error = r.message
                is MemoryApi.ActionsResult.Unauthenticated ->
                    error = "Signed out — sign in and try again."
                is MemoryApi.ActionsResult.QuotaExceeded ->
                    error = "You've hit your free question limit — upgrade to Pro to keep going."
                is MemoryApi.ActionsResult.Transient ->
                    error = "Couldn't reach the backend: ${r.message}"
            }
            loading = false
        }
    }

    // Auto-load the first time the memory is ready.
    LaunchedEffect(ready) {
        if (ready && actions == null && error == null && !loading) load()
    }

    Column(modifier) {
        StatusBanner(memStatus, statusError)
        when {
            loading -> Box(Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator()
            }
            error != null -> Column(
                Modifier.weight(1f).fillMaxWidth().padding(24.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.Center
            ) {
                ErrorBubble(error!!)
                Spacer(Modifier.height(12.dp))
                Button(onClick = { load() }, enabled = ready) { Text("Retry") }
            }
            !ready -> Box(Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
                Text(
                    "Waiting for the backend to finish watching this reel…",
                    modifier = Modifier.padding(24.dp),
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            actions.isNullOrEmpty() -> Box(
                Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center
            ) {
                Text(
                    "No action items found in this reel.",
                    modifier = Modifier.padding(24.dp),
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            else -> LazyColumn(
                modifier = Modifier.weight(1f).fillMaxWidth()
                    .padding(horizontal = 12.dp, vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                items(actions!!, key = { it.id.ifEmpty { it.title } }) { action ->
                    ActionItemCard(action)
                }
            }
        }
    }
}

@Composable
private fun ActionItemCard(action: ActionItem) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                PriorityChip(action.priority)
                action.effort?.let { effort ->
                    Surface(
                        color = MaterialTheme.colorScheme.surfaceVariant,
                        shape = MaterialTheme.shapes.small
                    ) {
                        Text(
                            "effort: $effort",
                            modifier = Modifier.padding(horizontal = 10.dp, vertical = 4.dp),
                            style = MaterialTheme.typography.labelMedium
                        )
                    }
                }
            }
            Spacer(Modifier.height(8.dp))
            Text(
                action.title,
                style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.SemiBold
            )
            action.detail?.let { detail ->
                Spacer(Modifier.height(4.dp))
                Text(
                    detail,
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            action.evidenceQuote?.let { quote ->
                Spacer(Modifier.height(8.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    action.evidencePhotoLabel?.let { photo ->
                        Surface(
                            color = MaterialTheme.colorScheme.tertiaryContainer,
                            shape = MaterialTheme.shapes.extraSmall
                        ) {
                            Text(
                                photo,
                                modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp),
                                style = MaterialTheme.typography.labelSmall,
                                fontWeight = FontWeight.Bold
                            )
                        }
                        Spacer(Modifier.width(8.dp))
                    }
                    action.evidenceModality?.let { modality ->
                        Text(
                            modality,
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                }
                Spacer(Modifier.height(4.dp))
                Surface(
                    color = MaterialTheme.colorScheme.surfaceVariant,
                    shape = MaterialTheme.shapes.small,
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text(
                        "“$quote”",
                        modifier = Modifier.padding(10.dp),
                        style = MaterialTheme.typography.bodySmall,
                        fontWeight = FontWeight.Medium
                    )
                }
            }
        }
    }
}

@Composable
private fun PriorityChip(priority: String) {
    val container = when (priority.uppercase()) {
        "P0" -> MaterialTheme.colorScheme.errorContainer
        "P1" -> MaterialTheme.colorScheme.tertiaryContainer
        else -> MaterialTheme.colorScheme.secondaryContainer
    }
    Surface(color = container, shape = MaterialTheme.shapes.small) {
        Text(
            priority.uppercase(),
            modifier = Modifier.padding(horizontal = 10.dp, vertical = 4.dp),
            style = MaterialTheme.typography.labelMedium,
            fontWeight = FontWeight.Bold
        )
    }
}

// ---------------------------------------------------------------------------
// Brief tab: decision card for a memory + goal
// ---------------------------------------------------------------------------

/** Decision-brief view: generates a card against the user's goal, records the choice locally. */
@Composable
private fun BriefTab(
    memoryId: String,
    api: MemoryApi,
    memStatus: MemoryStatus?,
    statusError: String?,
    modifier: Modifier = Modifier
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val db = remember { ShareDatabase.get(context) }
    val decision by db.briefDecisionDao().observe(memoryId).collectAsState(initial = null)

    var goal by remember { mutableStateOf("") }
    var brief by remember { mutableStateOf<DecisionBrief?>(null) }
    var loading by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val ready = memStatus?.isReady == true

    fun generate() {
        if (loading || !ready) return
        loading = true
        error = null
        scope.launch {
            when (val r = api.getBrief(memoryId, goal)) {
                is MemoryApi.BriefResult.Loaded -> brief = r.brief
                is MemoryApi.BriefResult.NotReady ->
                    error = "Still processing — try again in a bit."
                is MemoryApi.BriefResult.Rejected -> error = r.message
                is MemoryApi.BriefResult.Unauthenticated ->
                    error = "Signed out — sign in and try again."
                is MemoryApi.BriefResult.QuotaExceeded ->
                    error = "You've hit your free limit — upgrade to Pro to keep going."
                is MemoryApi.BriefResult.Transient ->
                    error = "Couldn't reach the backend: ${r.message}"
            }
            loading = false
        }
    }

    fun decide(choice: String) {
        scope.launch {
            db.briefDecisionDao().upsert(BriefDecision(memoryId = memoryId, choice = choice))
        }
    }

    Column(modifier) {
        StatusBanner(memStatus, statusError)
        if (brief == null) {
            Column(
                Modifier.fillMaxWidth().padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                Text(
                    "Decision brief",
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.SemiBold
                )
                Text(
                    "Describe what you're deciding, and Reel Memory will check " +
                        "this reel against your goal.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                OutlinedTextField(
                    value = goal,
                    onValueChange = { goal = it },
                    label = { Text("Your goal") },
                    modifier = Modifier.fillMaxWidth(),
                    minLines = 2
                )
                if (loading) {
                    LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                }
                error?.let { ErrorBubble(it) }
                Button(
                    onClick = { generate() },
                    enabled = ready && goal.isNotBlank() && !loading,
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text("Generate decision brief")
                }
                if (!ready) {
                    Text(
                        "Waiting for the backend to finish watching this reel…",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
        } else {
            LazyColumn(
                modifier = Modifier.weight(1f).fillMaxWidth()
                    .padding(horizontal = 12.dp, vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                item {
                    DecisionBriefCard(
                        brief = brief!!,
                        goal = goal,
                        decision = decision?.choice,
                        onDecide = { decide(it) },
                        onNewGoal = { brief = null; error = null }
                    )
                }
            }
        }
    }
}

@Composable
private fun DecisionBriefCard(
    brief: DecisionBrief,
    goal: String,
    decision: String?,
    onDecide: (String) -> Unit,
    onNewGoal: () -> Unit
) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(
                "Decision brief",
                style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.SemiBold,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
            if (goal.isNotBlank()) {
                Text(
                    "Goal: $goal",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            Text(brief.summary, style = MaterialTheme.typography.bodyMedium)

            if (brief.verdicts.isNotEmpty()) {
                HorizontalDivider()
                Text(
                    "Validation",
                    style = MaterialTheme.typography.labelLarge,
                    fontWeight = FontWeight.SemiBold
                )
                brief.verdicts.forEach { v ->
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalAlignment = Alignment.Top
                    ) {
                        VerdictBadge(v.verdict)
                        Column(Modifier.weight(1f)) {
                            Text(v.claim, style = MaterialTheme.typography.bodySmall)
                            v.note?.let { note ->
                                Text(
                                    note,
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant
                                )
                            }
                        }
                    }
                }
            }

            HorizontalDivider()
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                brief.usefulness?.let { usefulness ->
                    AssessmentChip("Usefulness: $usefulness")
                }
                brief.effort?.let { effort ->
                    AssessmentChip("Effort: $effort")
                }
            }

            if (brief.closingQuestion.isNotBlank()) {
                Text(
                    brief.closingQuestion,
                    style = MaterialTheme.typography.titleSmall,
                    fontWeight = FontWeight.SemiBold
                )
            }

            when (decision) {
                BriefDecision.YES -> ConfirmationBanner("Noted — you'll do it. Good luck ✓")
                BriefDecision.NO -> ConfirmationBanner("Noted — not now. It's saved if you change your mind.")
                else -> Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(10.dp)
                ) {
                    Button(
                        onClick = { onDecide(BriefDecision.YES) },
                        modifier = Modifier.weight(1f)
                    ) { Text("Yes, do it") }
                    androidx.compose.material3.OutlinedButton(
                        onClick = { onDecide(BriefDecision.NO) },
                        modifier = Modifier.weight(1f)
                    ) { Text("Not now") }
                }
            }

            androidx.compose.material3.TextButton(onClick = onNewGoal) {
                Text("Ask about a different goal")
            }
        }
    }
}

@Composable
private fun AssessmentChip(label: String) {
    Surface(
        color = MaterialTheme.colorScheme.secondaryContainer,
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
private fun ConfirmationBanner(text: String) {
    Surface(
        color = MaterialTheme.colorScheme.primaryContainer,
        shape = MaterialTheme.shapes.small,
        modifier = Modifier.fillMaxWidth()
    ) {
        Text(
            text,
            modifier = Modifier.padding(12.dp),
            style = MaterialTheme.typography.bodyMedium,
            fontWeight = FontWeight.Medium
        )
    }
}
