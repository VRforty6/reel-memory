package dev.reelmemory.app.data

import androidx.room.Dao
import androidx.room.Entity
import androidx.room.ForeignKey
import androidx.room.Index
import androidx.room.Insert
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Transaction
import androidx.room.Update
import kotlinx.coroutines.flow.Flow

/**
 * Durable local queue for carousel/album shares (2-30 photos, at most one
 * video) — the "send a post like in an Instagram DM" path.
 *
 * One share = ONE album row, uploaded in ONE multipart call to
 * POST /v1/captures/album, and counted as ONE capture against the free
 * quota no matter how many photos it holds. The individual files live in
 * [AlbumFile] rows so the worker can stream them in share order.
 *
 * Lifecycle reuses [VideoUploadStatus]: PENDING -> UPLOADING -> PROCESSING
 * -> READY | FAILED (plus QUOTA when the paywall parks it).
 */
@Entity(tableName = "album_uploads", indices = [Index("contentHash")])
data class AlbumUpload(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    /** Number of photos/videos in the album. */
    val fileCount: Int,
    /** Sum of the copied file sizes in bytes. */
    val totalBytes: Long,
    val status: String = VideoUploadStatus.PENDING,
    /** 0..100 while UPLOADING (whole batch). */
    val progress: Int = 0,
    val attempts: Int = 0,
    val lastError: String? = null,
    /** Backend memory id, once the album is accepted. */
    val remoteMemoryId: String? = null,
    val contentHash: String? = null,
    val remoteSourceId: String? = null,
    val remoteJobId: String? = null,
    val createdAt: Long = System.currentTimeMillis(),
    val updatedAt: Long = System.currentTimeMillis()
)

fun AlbumUpload.reviveForRetry(now: Long): AlbumUpload = copy(
    status = VideoUploadStatus.PENDING,
    progress = 0,
    lastError = null,
    remoteMemoryId = null,
    remoteSourceId = null,
    remoteJobId = null,
    updatedAt = now,
)

@Entity(
    tableName = "album_files",
    foreignKeys = [
        ForeignKey(
            entity = AlbumUpload::class,
            parentColumns = ["id"],
            childColumns = ["albumId"],
            onDelete = ForeignKey.CASCADE
        )
    ],
    indices = [Index("albumId")]
)
data class AlbumFile(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val albumId: Long,
    val fileName: String,
    val mimeType: String,
    val sizeBytes: Long,
    /** Absolute path of the app-private copy. */
    val localPath: String,
    /** Position in the album (photo N = sortOrder N-1). */
    val sortOrder: Int
)

@Dao
interface AlbumUploadDao {
    @Insert
    suspend fun insertAlbum(album: AlbumUpload): Long

    @Insert
    suspend fun insertFiles(files: List<AlbumFile>)

    @Query(
        "SELECT * FROM album_uploads WHERE contentHash = :contentHash " +
            "AND status IN ('PENDING','UPLOADING','PROCESSING','NEEDS_ATTENTION','FAILED','QUOTA') " +
            "ORDER BY createdAt ASC LIMIT 1"
    )
    suspend fun findActiveByContentHash(contentHash: String): AlbumUpload?

    @Query(
        "SELECT * FROM album_uploads WHERE contentHash IS NULL " +
            "AND status IN ('PENDING','UPLOADING','PROCESSING','NEEDS_ATTENTION','FAILED','QUOTA')"
    )
    suspend fun activeWithoutContentHash(): List<AlbumUpload>

    /**
     * All-or-nothing album creation: the album row and its file rows land in
     * one transaction, so a share is never half-queued.
     */
    @Transaction
    suspend fun insertAlbumWithFiles(album: AlbumUpload, files: List<AlbumFile>): Long {
        val id = insertAlbum(album)
        insertFiles(files.map { it.copy(albumId = id) })
        return id
    }

    @Transaction
    suspend fun insertUnlessActiveDuplicate(
        album: AlbumUpload,
        files: List<AlbumFile>,
    ): EnqueueResult {
        val hash = album.contentHash
        val existing = hash?.let { findActiveByContentHash(it) }
        return if (existing != null) EnqueueResult(existing.id, false, existing.status)
        else EnqueueResult(insertAlbumWithFiles(album, files), true, album.status)
    }

    @Query("SELECT * FROM album_uploads WHERE id = :id")
    suspend fun getById(id: Long): AlbumUpload?

    @Query("SELECT * FROM album_files WHERE albumId = :albumId ORDER BY sortOrder ASC")
    suspend fun getFiles(albumId: Long): List<AlbumFile>

    @Query("SELECT * FROM album_uploads WHERE remoteMemoryId = :memoryId")
    suspend fun findByRemoteMemoryId(memoryId: String): List<AlbumUpload>

    @Query("SELECT * FROM album_uploads ORDER BY createdAt DESC")
    fun observeAll(): Flow<List<AlbumUpload>>

    @Query(
        "SELECT * FROM album_uploads " +
            "WHERE status IN ('PENDING','FAILED') AND remoteMemoryId IS NULL " +
            "ORDER BY createdAt ASC"
    )
    suspend fun pendingOrFailed(): List<AlbumUpload>

    @Query(
        "SELECT * FROM album_uploads " +
            "WHERE remoteMemoryId IS NOT NULL AND status IN ('UPLOADING','PROCESSING') " +
            "ORDER BY createdAt ASC"
    )
    suspend fun reconciliationCandidates(): List<AlbumUpload>

    @Query("SELECT * FROM album_uploads WHERE status = 'PROCESSING' ORDER BY createdAt ASC")
    suspend fun processing(): List<AlbumUpload>

    @Query(
        "SELECT * FROM album_uploads WHERE status = 'UPLOADING' " +
            "AND remoteMemoryId IS NULL AND updatedAt < :before ORDER BY createdAt ASC"
    )
    suspend fun staleUnlinkedUploading(before: Long): List<AlbumUpload>

    @Query(
        "UPDATE album_uploads SET status = :status, attempts = attempts + 1, " +
            "updatedAt = :now WHERE id = :id"
    )
    suspend fun markAttempt(id: Long, status: String, now: Long)

    @Query("UPDATE album_uploads SET progress = :progress, updatedAt = :now WHERE id = :id")
    suspend fun updateProgress(id: Long, progress: Int, now: Long)

    @Query("UPDATE album_uploads SET contentHash = :contentHash, updatedAt = :now WHERE id = :id")
    suspend fun updateContentHash(id: Long, contentHash: String, now: Long)

    @Query(
        "UPDATE album_uploads SET status = 'PROCESSING', progress = 100, lastError = NULL, " +
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
        "UPDATE album_uploads SET status = :status, lastError = :error, " +
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

    @Query("SELECT COUNT(*) FROM album_uploads WHERE status IN ('PENDING','FAILED')")
    suspend fun pendingCount(): Int

    /** After an upgrade (or a manual retry), un-park quota-blocked albums. */
    @Query("UPDATE album_uploads SET status = 'PENDING', lastError = NULL WHERE status = 'QUOTA'")
    suspend fun requeueQuotaBlocked(): Int

    @Update
    suspend fun update(album: AlbumUpload): Int

    @Transaction
    suspend fun retryNeedsAttention(id: Long, now: Long): Int {
        val current = getById(id) ?: return 0
        if (current.status != VideoUploadStatus.NEEDS_ATTENTION) return 0
        return update(current.reviveForRetry(now))
    }

    @Query("DELETE FROM album_uploads WHERE remoteMemoryId = :memoryId")
    suspend fun deleteByRemoteMemoryId(memoryId: String): Int

    @Query("DELETE FROM album_uploads WHERE id = :id")
    suspend fun deleteById(id: Long): Int
}
