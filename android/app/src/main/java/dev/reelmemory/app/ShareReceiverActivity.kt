package dev.reelmemory.app

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.Parcelable
import android.provider.OpenableColumns
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.lifecycle.lifecycleScope
import dev.reelmemory.app.data.QueuedShare
import dev.reelmemory.app.data.ShareDatabase
import dev.reelmemory.app.data.ShareStatus
import dev.reelmemory.app.data.VideoUpload
import dev.reelmemory.app.data.WebCapture
import dev.reelmemory.app.data.WebCaptureStatus
import dev.reelmemory.app.net.AlbumApi
import dev.reelmemory.app.net.UploadApi
import dev.reelmemory.app.sync.SyncWorker
import dev.reelmemory.app.sync.UploadWorker
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.util.UUID

/**
 * Share target shown in the Android share sheet. Two intake paths:
 *
 * 1. text/plain (what Instagram usually sends: a message containing the reel
 *    URL). The URL is saved to the local queue and synced to
 *    POST /v1/captures. Honest limitation: the backend cannot fetch video
 *    bytes from an unauthenticated URL, so these are labeled "preview only".
 *
 * 2. video files (when the sending app shares an actual video file). The stream
 *    is copied into app-private storage immediately (durable before any
 *    network), then a WorkManager worker uploads it via multipart to
 *    POST /v1/captures/upload. This is the path that lets the backend
 *    actually *see* the reel — required for the Ask screen.
 *
 * 3. carousel/album shares (ACTION_SEND_MULTIPLE with 2+ image/video files).
 *    All streams are copied into app-private storage immediately, then ONE
 *    AlbumUpload row is queued and a WorkManager worker uploads everything
 *    in ONE multipart call to POST /v1/captures/album — one memory, one
 *    quota unit, no matter the photo count. This is the "send a post like
 *    in an Instagram DM" path: every photo is read (vision + OCR per photo,
 *    `album_index` tags the evidence so citations say "photo N").
 *
 * PRD FR-CAP-004 capture budget: the share must be acknowledged in under one
 * second and be durable before any network. The video copy happens on IO;
 * for very large files the toast may lag slightly, but the copy (not the
 * upload) is what blocks the return to the sending app.
 *
 * The activity has no window (transparent theme, noHistory) so the user is
 * returned straight to the sending app after the toast.
 */
class ShareReceiverActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        handleShare(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShare(intent)
    }

    private fun handleShare(intent: Intent?) {
        if (intent == null) {
            toastAndFinish(getString(R.string.share_nothing))
            return
        }
        val mime = intent.type.orEmpty()
        lifecycleScope.launch {
            if (intent.action == Intent.ACTION_SEND && (mime.startsWith("video/") || mime.startsWith("image/"))) {
                val uri: Uri? = intent.getParcelableExtra<Parcelable>(Intent.EXTRA_STREAM) as? Uri
                handleMediaShares(listOfNotNull(uri), mime)
            } else if (intent.action == Intent.ACTION_SEND_MULTIPLE &&
                (mime.startsWith("video/") || mime.startsWith("image/") || mime == "*/*")
            ) {
                // "*/*" covers mixed image+video carousel shares whose
                // intent-level type isn't image/* or video/*. Each URI's real
                // content type is validated during persistAlbum; non-media
                // shares are rejected with a clear message, never uploaded.
                val uris = intent.getParcelableArrayListExtra<Parcelable>(Intent.EXTRA_STREAM)
                    ?.mapNotNull { it as? Uri }.orEmpty()
                handleMediaShares(uris, mime)
            } else {
                handleTextShares(intent)
            }
        }
    }

    // ---------- video/* and image/* path ----------

    private suspend fun handleMediaShares(uris: List<Uri>, mime: String) {
        if (uris.isEmpty()) {
            toastAndFinish(getString(R.string.share_no_video))
            return
        }
        if (uris.size >= 2) {
            // Carousel/album share: 2-30 photos (+ at most one video) become
            // ONE memory via POST /v1/captures/album — one upload, one quota
            // unit, no matter the photo count.
            val result = withContext(Dispatchers.IO) { persistAlbum(uris, mime) }
            toastAndFinish(result.userMessage)
            if (result.savedNew > 0) {
                UploadWorker.enqueue(this@ShareReceiverActivity)
            }
            return
        }
        val result = withContext(Dispatchers.IO) { persistMedia(uris, mime) }
        toastAndFinish(result.userMessage)
        if (result.savedNew > 0) {
            UploadWorker.enqueue(this@ShareReceiverActivity)
        }
    }

    private data class AlbumPersistResult(
        val savedNew: Int,
        val tooLarge: Int,
        val failed: Int,
        val problem: String?
    ) {
        val userMessage: String
            get() = when {
                savedNew > 0 ->
                    if (savedNew == 1) "Album saved — uploading to Reel Memory"
                    else "$savedNew photos saved — uploading as one album"
                problem != null -> problem
                tooLarge > 0 -> "Photos too large (album over 400 MB) — not saved"
                else -> "Couldn't save the shared photos"
            }
    }

    /**
     * Copies a carousel/album share into app-private storage and queues ONE
     * [dev.reelmemory.app.data.AlbumUpload] row with one
     * [dev.reelmemory.app.data.AlbumFile] child per photo, in share order.
     *
     * Validation mirrors the backend's album rules: 2-30 files, at most one
     * video, image or video files only, 200 MB per file, 400 MB total. The
     * backend dedupes by the combined content hash, so re-sharing the same
     * carousel is cheap — but we still guard locally first.
     */
    /**
     * Copies a carousel/album share into app-private storage and queues ONE
     * [dev.reelmemory.app.data.AlbumUpload] row with one
     * [dev.reelmemory.app.data.AlbumFile] child per photo, in share order.
     *
     * All-or-nothing: if ANY member can't be read or breaks a limit, the whole
     * album is rejected and the partial copies are deleted — a carousel is
     * only useful with every photo. Validation mirrors the backend's album
     * rules: 2-30 files, at most one video, image or video files only,
     * 200 MB per file, 400 MB total. Sizes are enforced DURING the copy
     * (bounded stream), so a huge file can't fill storage before we notice.
     * The album row and its file rows are queued in one Room transaction.
     */
    private suspend fun persistAlbum(uris: List<Uri>, intentMime: String): AlbumPersistResult {
        if (uris.size > AlbumApi.MAX_ALBUM_FILES) {
            return AlbumPersistResult(
                0, 0, 0,
                "Too many photos (${uris.size}) — an album holds at most 30"
            )
        }
        val albumDao = ShareDatabase.get(this).albumUploadDao()
        val albumDir = File(File(filesDir, "uploads"), "album-${UUID.randomUUID()}").apply { mkdirs() }

        val copied = mutableListOf<Triple<File, String, Long>>() // file, mime, size
        var totalBytes = 0L
        var problem: String? = null

        for ((index, uri) in uris.withIndex()) {
            // Per-URI mime matters: the share can mix images and one video,
            // and the intent-level type may just be "image/*".
            val mime = (contentResolver.getType(uri) ?: intentMime)
                .substringBefore(";").trim().lowercase()
            if (!mime.startsWith("image/") && !mime.startsWith("video/")) {
                problem = "Couldn't read photo ${index + 1} (unsupported type) — album not saved"
                break
            }
            val declaredSize = querySize(uri)
            if (declaredSize != null && !UploadApi.isWithinSizeLimit(declaredSize)) {
                problem = "Photo ${index + 1} is over 200 MB — album not saved"
                break
            }
            if (totalBytes + (declaredSize ?: 0L) > AlbumApi.MAX_ALBUM_BYTES) {
                problem = "Album is over 400 MB — not saved"
                break
            }
            val defaultExt = if (mime.startsWith("image/")) "jpg" else "mp4"
            val ext = mime.substringAfter("/", defaultExt)
                .takeIf { it.matches(Regex("[a-zA-Z0-9]{1,5}")) } ?: defaultExt
            val dest = File(albumDir, "photo-${index + 1}.$ext")
            // Bounded copy: abort mid-stream past the per-file cap so an
            // undeclared huge file can't fill storage before we notice.
            when (val r = copyBounded(uri, dest, UploadApi.MAX_UPLOAD_BYTES)) {
                is CopyResult.TooLarge -> {
                    problem = "Photo ${index + 1} is over 200 MB — album not saved"
                    break
                }
                is CopyResult.Failed -> {
                    problem = "Couldn't read photo ${index + 1} — album not saved"
                    break
                }
                is CopyResult.Ok -> {
                    totalBytes += r.bytes
                    if (totalBytes > AlbumApi.MAX_ALBUM_BYTES) {
                        problem = "Album is over 400 MB — not saved"
                        break
                    }
                    copied += Triple(dest, mime, r.bytes)
                }
            }
        }

        if (problem == null) {
            // Final rule check (count, at most one video) on what we copied.
            val infos = copied.map {
                AlbumApi.Companion.AlbumFileInfo(
                    fileName = it.first.name,
                    mimeType = it.second,
                    sizeBytes = it.third
                )
            }
            problem = when (val v = AlbumApi.classifyAlbum(infos)) {
                is AlbumApi.Companion.AlbumValidity.Valid -> null
                is AlbumApi.Companion.AlbumValidity.TooFew ->
                    "Only ${v.count} photo(s) saved — an album needs at least 2"
                is AlbumApi.Companion.AlbumValidity.TooMany ->
                    "Too many photos (${v.count}) — an album holds at most 30"
                is AlbumApi.Companion.AlbumValidity.TooManyVideos ->
                    "An album can hold at most one video — not saved"
                is AlbumApi.Companion.AlbumValidity.UnsupportedType ->
                    "Unsupported file type (${v.mimeType}) — not saved"
                is AlbumApi.Companion.AlbumValidity.PerFileTooLarge ->
                    "A photo is over 200 MB — not saved"
                is AlbumApi.Companion.AlbumValidity.TooLargeTotal ->
                    "Album is ${UploadApi.formatBytes(v.totalBytes)} — the 400 MB album limit was exceeded"
            }
        }

        if (problem != null) {
            albumDir.deleteRecursively()
            return AlbumPersistResult(0, 0, 0, problem)
        }

        // Atomic queueing: album row + file rows in one Room transaction.
        albumDao.insertAlbumWithFiles(
            dev.reelmemory.app.data.AlbumUpload(
                fileCount = copied.size,
                totalBytes = totalBytes
            ),
            copied.mapIndexed { sortOrder, (file, mime, size) ->
                dev.reelmemory.app.data.AlbumFile(
                    albumId = 0, // replaced by insertAlbumWithFiles
                    fileName = file.name,
                    mimeType = mime,
                    sizeBytes = size,
                    localPath = file.absolutePath,
                    sortOrder = sortOrder
                )
            }
        )
        return AlbumPersistResult(copied.size, 0, 0, null)
    }

    /**
     * Copies [uri] to [dest], stopping after [maxBytes]. Dest is deleted when
     * the stream can't be read or exceeds the cap.
     */
    private sealed class CopyResult {
        data class Ok(val bytes: Long) : CopyResult()
        data object TooLarge : CopyResult()
        data object Failed : CopyResult()
    }

    private fun copyBounded(uri: Uri, dest: File, maxBytes: Long): CopyResult {
        return try {
            val stream = contentResolver.openInputStream(uri)
            if (stream == null) {
                dest.delete()
                return CopyResult.Failed
            }
            stream.use { input ->
                dest.outputStream().use { output ->
                    val buf = ByteArray(64 * 1024)
                    var total = 0L
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        total += n
                        if (total > maxBytes) {
                            dest.delete()
                            return CopyResult.TooLarge
                        }
                        output.write(buf, 0, n)
                    }
                    CopyResult.Ok(total)
                }
            }
        } catch (_: Exception) {
            dest.delete()
            CopyResult.Failed
        }
    }

    private data class VideoPersistResult(val savedNew: Int, val tooLarge: Int, val failed: Int, val kind: String) {
        val userMessage: String
            get() = when {
                savedNew > 0 -> if (savedNew == 1) "$kind saved — uploading to Reel Memory"
                    else "$savedNew ${kind.lowercase()}s saved — uploading to Reel Memory"
                tooLarge > 0 -> "$kind too large (over 200 MB) — not saved"
                else -> "Couldn't save the shared $kind"
            }
    }

    /**
     * Copies each shared video/image stream into app-private storage and queues a
     * VideoUpload row. Enforces the client-side size guard (200 MB) with a
     * friendly outcome instead of a crash or a doomed upload.
     */
    private suspend fun persistMedia(uris: List<Uri>, mime: String): VideoPersistResult {
        val kind = if (mime.startsWith("image/")) "Image" else "Video"
        val dao = ShareDatabase.get(this).videoUploadDao()
        val uploadsDir = File(filesDir, "uploads").apply { mkdirs() }
        var savedNew = 0
        var tooLarge = 0
        var failed = 0

        for (uri in uris) {
            try {
                val declaredSize = querySize(uri)
                if (declaredSize != null && !UploadApi.isWithinSizeLimit(declaredSize)) {
                    tooLarge++
                    continue
                }
                val defaultExt = if (mime.startsWith("image/")) "jpg" else "mp4"
                val ext = mime.substringAfter("/", defaultExt).substringBefore(";")
                    .takeIf { it.matches(Regex("[a-zA-Z0-9]{1,5}")) } ?: defaultExt
                val fileName = "reel-${UUID.randomUUID()}.$ext"
                val dest = File(uploadsDir, fileName)
                val copied = try {
                    contentResolver.openInputStream(uri)?.use { input ->
                        dest.outputStream().use { output -> input.copyTo(output) }
                    } != null
                } catch (_: Exception) {
                    false
                }
                if (!copied) {
                    dest.delete()
                    failed++
                    continue
                }

                if (!UploadApi.isWithinSizeLimit(dest.length())) {
                    dest.delete()
                    tooLarge++
                    continue
                }
                dao.insert(
                    VideoUpload(
                        fileName = fileName,
                        mimeType = mime.substringBefore(";"),
                        sizeBytes = dest.length(),
                        localPath = dest.absolutePath
                    )
                )
                savedNew++
            } catch (_: Exception) {
                failed++
            }
        }
        return VideoPersistResult(savedNew, tooLarge, failed, kind)
    }

    /** Declared size from the content provider; null when unknown. */
    private fun querySize(uri: Uri): Long? {
        return try {
            contentResolver.query(uri, arrayOf(OpenableColumns.SIZE), null, null, null)
                ?.use { cursor ->
                    if (cursor.moveToFirst()) {
                        val idx = cursor.getColumnIndex(OpenableColumns.SIZE)
                        if (idx >= 0) cursor.getLong(idx).takeIf { it > 0 } else null
                    } else null
                }
        } catch (_: Exception) {
            null
        }
    }

    // ---------- text/plain path (unchanged behavior, honest labeling) ----------

    private suspend fun handleTextShares(intent: Intent) {
        val texts = extractSharedTexts(intent)
        if (texts.isEmpty()) {
            toastAndFinish(getString(R.string.share_no_text))
            return
        }
        val result = withContext(Dispatchers.IO) { persistShares(texts) }
        toastAndFinish(result.userMessage)
        if (result.savedNew > 0 || result.savedWeb > 0) {
            // Kick the sync worker; it no-ops if there's nothing to send.
            SyncWorker.enqueue(this@ShareReceiverActivity)
        }
    }

    /** Pulls every text payload out of SEND / SEND_MULTIPLE intents. */
    private fun extractSharedTexts(intent: Intent?): List<String> {
        if (intent == null) return emptyList()
        return when (intent.action) {
            Intent.ACTION_SEND -> {
                listOfNotNull(intent.getCharSequenceExtra(Intent.EXTRA_TEXT)?.toString())
                    .filter { it.isNotBlank() }
            }
            Intent.ACTION_SEND_MULTIPLE -> {
                intent.getCharSequenceArrayListExtra(Intent.EXTRA_TEXT)
                    ?.mapNotNull { it?.toString() }
                    ?.filter { it.isNotBlank() }
                    .orEmpty()
            }
            else -> emptyList()
        }
    }

    private data class PersistResult(
        val savedNew: Int,
        val duplicates: Int,
        val rejected: Int,
        val savedWeb: Int = 0,
    ) {
        val userMessage: String
            get() = when {
                savedWeb > 0 && savedNew == 0 ->
                    if (savedWeb == 1) "Web link saved — capturing article"
                    else "$savedWeb web links saved — capturing articles"
                savedNew > 0 && duplicates == 0 && rejected == 0 ->
                    if (savedNew == 1) "Link saved (preview only — video not readable)"
                    else "$savedNew links saved (preview only — video not readable)"
                savedNew > 0 ->
                    "Saved $savedNew link(s), preview only ($duplicates already saved, $rejected skipped)"
                duplicates > 0 && rejected == 0 -> "Already in Reel Memory"
                else -> "Couldn't save: no link found to capture"
            }
    }

    /**
     * Extract URLs from the shared text, split Instagram reels/posts from plain
     * web URLs, dedupe against the local queues, and insert new rows.
     * Instagram shares keep the existing flow; plain web URLs queue for
     * `POST /v1/captures/url`. All on IO; local DB ops only.
     */
    private suspend fun persistShares(texts: List<String>): PersistResult {
        val db = ShareDatabase.get(this)
        val shareDao = db.shareDao()
        val webDao = db.webCaptureDao()
        var savedNew = 0
        var duplicates = 0
        var rejected = 0
        var savedWeb = 0

        for (text in texts) {
            val classified = ShareIntake.classifyUrls(UrlExtractor.extract(text))
            if (classified.isEmpty) {
                rejected++
                continue
            }
            for (url in classified.webUrls) {
                if (webDao.countByUrl(url) == 0) {
                    webDao.insert(WebCapture(url = url, status = WebCaptureStatus.PENDING))
                    savedWeb++
                } else {
                    duplicates++
                }
            }
            for (canon in classified.instagramUrls) {
                val existing = shareDao.findBySource(canon.platform, canon.platformItemId)
                if (existing != null) {
                    duplicates++
                } else {
                    shareDao.insert(
                        QueuedShare(
                            platform = canon.platform,
                            platformItemId = canon.platformItemId,
                            canonicalUrl = canon.canonicalUrl,
                            originalUrl = canon.originalUrl,
                            status = ShareStatus.PENDING
                        )
                    )
                    savedNew++
                }
            }
        }
        return PersistResult(savedNew, duplicates, rejected, savedWeb)
    }

    private fun toastAndFinish(message: String) {
        Toast.makeText(this, "Reel Memory: $message", Toast.LENGTH_LONG).show()
        finish()
    }
}
