package dev.reelmemory.app.data

import androidx.room.Dao
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.PrimaryKey
import androidx.room.Query
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
}

@Entity(tableName = "video_uploads")
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
    val createdAt: Long = System.currentTimeMillis(),
    val updatedAt: Long = System.currentTimeMillis()
)

@Dao
interface VideoUploadDao {
    @Insert
    suspend fun insert(upload: VideoUpload): Long

    @Query("SELECT * FROM video_uploads ORDER BY createdAt DESC")
    fun observeAll(): Flow<List<VideoUpload>>

    @Query("SELECT * FROM video_uploads WHERE id = :id")
    suspend fun getById(id: Long): VideoUpload?

    @Query(
        "SELECT * FROM video_uploads " +
            "WHERE status IN ('PENDING','FAILED') ORDER BY createdAt ASC"
    )
    suspend fun pendingOrFailed(): List<VideoUpload>

    @Query(
        "SELECT * FROM video_uploads " +
            "WHERE status = 'PROCESSING' ORDER BY createdAt ASC"
    )
    suspend fun processing(): List<VideoUpload>

    @Query(
        "UPDATE video_uploads SET status = :status, attempts = attempts + 1, " +
            "updatedAt = :now WHERE id = :id"
    )
    suspend fun markAttempt(id: Long, status: String, now: Long)

    @Query("UPDATE video_uploads SET progress = :progress, updatedAt = :now WHERE id = :id")
    suspend fun updateProgress(id: Long, progress: Int, now: Long)

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
}
