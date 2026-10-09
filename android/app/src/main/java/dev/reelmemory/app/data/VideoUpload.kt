package dev.reelmemory.app.data

import androidx.room.Dao
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.Index
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Transaction
import androidx.room.Update
import kotlinx.coroutines.flow.Flow

/**
 * Durable local queue for shared VIDEO files.
 *
 * Unlike URL shares (which the backend can only ever treat as metadata —
 * unauthenticated Instagram fetching yields zero video bytes), a shared
 * video file is copied into app-private storage first, then uploaded to
 * POST /v1/captures/upload so the backend can actually see the reel.
 *
 * Lifecycle: PENDING -> UPLOADING -> PROCESSING -> READY | FAILED.
 * READY means the backend reports processing_status=READY and the reel can
 * be opened in the Ask screen.
 */
object VideoUploadStatus {
    const val PENDING = "PENDING"       // saved locally, not yet uploaded
    const val UPLOADING = "UPLOADING"   // multipart upload in flight
    const val PROCESSING = "PROCESSING" // backend accepted; AI pipeline running
    const val READY = "READY"           // backend reports READY; ask away
    const val FAILED = "FAILED"        // last attempt failed; worker retries
    const val QUOTA = "QUOTA"          // parked: free quota exhausted (402); user upgrades or retries
    const val NEEDS_ATTENTION = "NEEDS_ATTENTION" // interrupted before backend accepted it
}

@Entity(tableName = "video_uploads", indices = [Index("contentHash")])
data class VideoUpload(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val fileName: String,
    val mimeType: String,
    val sizeBytes: Long,
    /** Absolute path of the app-private copy. */
    val localPath: String,
    /** Instagram URL the user may have shared alongside (optional). */
    val originalUrl: String? = null,
    val status: String = VideoUploadStatus.PENDING,
    /** 0..100 while UPLOADING. */
    val progress: Int = 0,
    val attempts: Int = 0,
    val lastError: String? = null,
    /** Backend memory id, once the upload is accepted. */
    val remoteMemoryId: String? = null,
    /** SHA-256 of the copied bytes: stable local/server dedupe identity. */
    val contentHash: String? = null,
    val remoteSourceId: String? = null,
    val remoteJobId: String? = null,
    val createdAt: Long = System.currentTimeMillis(),
    val updatedAt: Long = System.currentTimeMillis()
)

fun VideoUpload.reviveForRetry(now: Long): VideoUpload = copy(
    status = VideoUploadStatus.PENDING,
    progress = 0,
    lastError = null,
    remoteMemoryId = null,
    remoteSourceId = null,
    remoteJobId = null,
    updatedAt = now,
)

@Dao
interface VideoUploadDao {
    @Insert
    suspend fun insert(upload: VideoUpload): Long

    @Query(
        "SELECT * FROM video_uploads WHERE contentHash = :contentHash " +
            "AND status IN ('PENDING','UPLOADING','PROCESSING','NEEDS_ATTENTION','FAILED','QUOTA') " +
            "ORDER BY createdAt ASC LIMIT 1"
    )
    suspend fun findActiveByContentHash(contentHash: String): VideoUpload?

    @Query(
        "SELECT * FROM video_uploads WHERE contentHash IS NULL " +
            "AND status IN ('PENDING','UPLOADING','PROCESSING','NEEDS_ATTENTION','FAILED','QUOTA')"
    )
    suspend fun activeWithoutContentHash(): List<VideoUpload>

    @Transaction
    suspend fun insertUnlessActiveDuplicate(upload: VideoUpload): EnqueueResult {
        val hash = upload.contentHash
        val existing = hash?.let { findActiveByContentHash(it) }
        return if (existing != null) EnqueueResult(existing.id, false, existing.status)
        else EnqueueResult(insert(upload), true, upload.status)
    }

    @Query("SELECT * FROM video_uploads ORDER BY createdAt DESC")
    fun observeAll(): Flow<List<VideoUpload>>

    @Query("SELECT * FROM video_uploads WHERE id = :id")
    suspend fun getById(id: Long): VideoUpload?

    @Query("SELECT * FROM video_uploads WHERE remoteMemoryId = :memoryId")
    suspend fun findByRemoteMemoryId(memoryId: String): List<VideoUpload>

    @Query(
        "SELECT * FROM video_uploads " +
            "WHERE status IN ('PENDING','FAILED') AND remoteMemoryId IS NULL " +
            "ORDER BY createdAt ASC"
    )
    suspend fun pendingOrFailed(): List<VideoUpload>

    @Query(
        "SELECT * FROM video_uploads " +
            "WHERE remoteMemoryId IS NOT NULL AND status IN ('UPLOADING','PROCESSING') " +
            "ORDER BY createdAt ASC"
    )
    suspend fun reconciliationCandidates(): List<VideoUpload>

    @Query("SELECT * FROM video_uploads WHERE status = 'PROCESSING' ORDER BY createdAt ASC")
    suspend fun processing(): List<VideoUpload>

    @Query("SELECT * FROM video_uploads WHERE status = 'UPLOADING' AND remoteMemoryId IS NULL")
    suspend fun unlinkedUploading(): List<VideoUpload>

    @Query(
        "SELECT * FROM video_uploads WHERE status = 'UPLOADING' " +
            "AND remoteMemoryId IS NULL AND updatedAt < :before ORDER BY createdAt ASC"
    )
    suspend fun staleUnlinkedUploading(before: Long): List<VideoUpload>

    @Query(
        "UPDATE video_uploads SET status = :status, attempts = attempts + 1, " +
            "updatedAt = :now WHERE id = :id"
    )
    suspend fun markAttempt(id: Long, status: String, now: Long)

    @Query("UPDATE video_uploads SET progress = :progress, updatedAt = :now WHERE id = :id")
    suspend fun updateProgress(id: Long, progress: Int, now: Long)

    @Query("UPDATE video_uploads SET contentHash = :contentHash, updatedAt = :now WHERE id = :id")
    suspend fun updateContentHash(id: Long, contentHash: String, now: Long)

    @Query(
        "UPDATE video_uploads SET status = 'PROCESSING', progress = 100, lastError = NULL, " +
            "remoteMemoryId = :memoryId, remoteSourceId = :sourceId, remoteJobId = :jobId, " +
            "contentHash = COALESCE(:contentHash, contentHash), updatedAt = :now WHERE id = :id"
    )
    suspend fun markAccepted(
        id: Long,
        memoryId: String,
        sourceId: String?,
        jobId: String?,
        contentHash: String?,
        now: Long,
    )

    @Query(
        "UPDATE video_uploads SET status = :status, lastError = :error, " +
            "remoteMemoryId = COALESCE(:remoteMemoryId, remoteMemoryId), " +
            "updatedAt = :now WHERE id = :id"
    )
    suspend fun markResult(
        id: Long,
        status: String,
        error: String?,
        remoteMemoryId: String?,
        now: Long
    )

    @Query("SELECT COUNT(*) FROM video_uploads WHERE status IN ('PENDING','FAILED')")
    suspend fun pendingCount(): Int

    /** After an upgrade (or a manual retry), un-park quota-blocked rows. */
    @Query("UPDATE video_uploads SET status = 'PENDING', lastError = NULL WHERE status = 'QUOTA'")
    suspend fun requeueQuotaBlocked(): Int

    @Update
    suspend fun update(upload: VideoUpload): Int

    @Transaction
    suspend fun retryNeedsAttention(id: Long, now: Long): Int {
        val current = getById(id) ?: return 0
        if (current.status != VideoUploadStatus.NEEDS_ATTENTION) return 0
        return update(current.reviveForRetry(now))
    }

    @Query("DELETE FROM video_uploads WHERE remoteMemoryId = :memoryId")
    suspend fun deleteByRemoteMemoryId(memoryId: String): Int

    @Query("DELETE FROM video_uploads WHERE id = :id")
    suspend fun deleteById(id: Long): Int
}

data class EnqueueResult(
    val rowId: Long,
    val inserted: Boolean,
    val status: String,
)
