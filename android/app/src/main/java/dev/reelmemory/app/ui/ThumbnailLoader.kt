package dev.reelmemory.app.ui

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.LruCache
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import dev.reelmemory.app.net.ThumbnailApi
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Small image loader for backend thumbnails: OkHttp + Bearer (inside
 * [ThumbnailApi]), an in-memory LRU of decoded bitmaps, and
 * [BitmapFactory] decoding. No new Gradle dependencies (no Coil).
 */
class ThumbnailLoader(private val api: ThumbnailApi) {

    private val cache: LruCache<String, Bitmap> =
        object : LruCache<String, Bitmap>(maxCacheKb()) {
            override fun sizeOf(key: String, value: Bitmap): Int =
                value.byteCount / 1024
        }

    /**
     * Negative cache: memory IDs the backend 404'd in this session (or
     * which reported `has_thumbnail: false`). A 404 is never re-requested
     * in-session.
     */
    private val knownMissing: LruCache<String, Boolean> =
        LruCache<String, Boolean>(1024)

    private fun maxCacheKb(): Int =
        (Runtime.getRuntime().maxMemory() / 1024 / 8).toInt().coerceAtLeast(4 * 1024)

    /**
     * Decoded bitmap, or null when the backend has none / can't be reached.
     *
     * [hasThumbnail] is the backend's `has_thumbnail` flag when the memory
     * summary/detail carried it: false skips the request entirely. When the
     * flag is absent, this session's 404 negative cache avoids repeat
     * requests.
     */
    suspend fun load(memoryId: String, hasThumbnail: Boolean? = null): Bitmap? =
        withContext(Dispatchers.IO) {
            cache.get(memoryId)?.let { return@withContext it }
            if (shouldSkipThumbnail(hasThumbnail, knownMissing.get(memoryId) == true)) {
                return@withContext null
            }
            val bytes = try {
                api.fetchThumbnail(memoryId)
            } catch (_: Exception) {
                // Transport error: retryable next time, don't poison the
                // negative cache.
                return@withContext null
            } ?: run {
                knownMissing.put(memoryId, true)
                return@withContext null
            }
            val bitmap = try {
                BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
            } catch (_: Exception) {
                null
            } ?: run {
                knownMissing.put(memoryId, true)
                return@withContext null
            }
            cache.put(memoryId, bitmap)
            bitmap
        }

    /** Pre-seed the negative cache (e.g. from `has_thumbnail: false`). */
    fun markMissing(memoryId: String) {
        knownMissing.put(memoryId, true)
    }

    fun isKnownMissing(memoryId: String): Boolean = knownMissing.get(memoryId) == true

    fun evict(memoryId: String) {
        cache.remove(memoryId)
        knownMissing.remove(memoryId)
    }
}

/**
 * Backend thumbnail for [memoryId], or a calm placeholder while loading /
 * when the memory has none (old memories return 404).
 *
 * [hasThumbnail] is the backend's `has_thumbnail` flag when the calling
 * model carried it: false means the request is skipped outright instead of
 * rendering a placeholder for a thumbnail that can never arrive.
 */
@Composable
fun MemoryThumbnail(
    memoryId: String,
    loader: ThumbnailLoader,
    modifier: Modifier = Modifier,
    contentDescription: String? = null,
    contentScale: ContentScale = ContentScale.Crop,
    hasThumbnail: Boolean? = null,
) {
    var bitmap by remember(memoryId) { mutableStateOf<Bitmap?>(null) }
    var attempted by remember(memoryId) { mutableStateOf(false) }

    LaunchedEffect(memoryId, loader) {
        bitmap = loader.load(memoryId, hasThumbnail)
        attempted = true
    }

    val loaded = bitmap
    if (loaded != null) {
        Image(
            bitmap = loaded.asImageBitmap(),
            contentDescription = contentDescription,
            modifier = modifier,
            contentScale = contentScale,
        )
    } else {
        Box(
            modifier = modifier.background(MaterialTheme.colorScheme.surfaceVariant),
            contentAlignment = Alignment.Center,
        ) {
            if (attempted) {
                Icon(
                    ImageFrame,
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.5f),
                )
            }
        }
    }
}
