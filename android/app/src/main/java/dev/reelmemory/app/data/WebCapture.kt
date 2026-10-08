package dev.reelmemory.app.data

import androidx.room.Dao
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import kotlinx.coroutines.flow.Flow

/** Sync lifecycle states for a web-URL capture queued from a share sheet. */
object WebCaptureStatus {
    const val PENDING = "PENDING"
    const val SYNCING = "SYNCING"
    const val SYNCED = "SYNCED"
    const val FAILED = "FAILED"
    const val QUOTA = "QUOTA" // parked: free quota exhausted (402); user upgrades or retries
}

/**
 * A plain (non-Instagram) URL shared from any app — a website, article, doc, etc.
 * Syncs to the backend via `POST /v1/captures/url {url}`.
 */
@Entity(tableName = "web_captures")
data class WebCapture(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val url: String,
    val status: String = WebCaptureStatus.PENDING,
    val attempts: Int = 0,
    val lastError: String? = null,
    val remoteMemoryId: String? = null,
    val createdAt: Long = System.currentTimeMillis(),
    val updatedAt: Long = System.currentTimeMillis(),
)

@Dao
interface WebCaptureDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun insert(capture: WebCapture): Long

    @Query("SELECT COUNT(*) FROM web_captures WHERE url = :url")
    suspend fun countByUrl(url: String): Int

    @Query("SELECT * FROM web_captures WHERE status = :status LIMIT 50")
    suspend fun pendingByStatus(status: String): List<WebCapture>

    @Query("SELECT * FROM web_captures ORDER BY createdAt DESC")
    fun observeAll(): Flow<List<WebCapture>>

    @Query(
        "UPDATE web_captures SET status = :status, attempts = attempts + 1, " +
            "lastError = :error, remoteMemoryId = :memoryId, " +
            "updatedAt = :now WHERE id = :id"
    )
    suspend fun updateAfterAttempt(
        id: Long,
        status: String,
        error: String?,
        memoryId: String?,
        now: Long,
    )

    @Query("DELETE FROM web_captures")
    suspend fun clearAll()

    /** After an upgrade (or a manual retry), un-park quota-blocked rows. */
    @Query("UPDATE web_captures SET status = 'PENDING', lastError = NULL WHERE status = 'QUOTA'")
    suspend fun requeueQuotaBlocked(): Int
}
