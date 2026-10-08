package dev.reelmemory.app.data

import androidx.room.Dao
import androidx.room.Entity
import androidx.room.ForeignKey
import androidx.room.Index
import androidx.room.Insert
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Transaction
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
@Entity(tableName = "album_uploads")
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
    val createdAt: Long = System.currentTimeMillis(),
    val updatedAt: Long = System.currentTimeMillis()
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

    @Query("SELECT * FROM album_uploads WHERE id = :id")
    suspend fun getById(id: Long): AlbumUpload?

    @Query("SELECT * FROM album_files WHERE albumId = :albumId ORDER BY sortOrder ASC")
    suspend fun getFiles(albumId: Long): List<AlbumFile>

    @Query("SELECT * FROM album_uploads ORDER BY createdAt DESC")
    fun observeAll(): Flow<List<AlbumUpload>>

    @Query(
        "SELECT * FROM album_uploads " +
            "WHERE status IN ('PENDING','FAILED') ORDER BY createdAt ASC"
    )
    suspend fun pendingOrFailed(): List<AlbumUpload>

    @Query(
        "SELECT * FROM album_uploads " +
            "WHERE status = 'PROCESSING' ORDER BY createdAt ASC"
    )
    suspend fun processing(): List<AlbumUpload>

    @Query(
        "UPDATE album_uploads SET status = :status, attempts = attempts + 1, " +
            "updatedAt = :now WHERE id = :id"
    )
    suspend fun markAttempt(id: Long, status: String, now: Long)

    @Query("UPDATE album_uploads SET progress = :progress, updatedAt = :now WHERE id = :id")
    suspend fun updateProgress(id: Long, progress: Int, now: Long)

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
}
