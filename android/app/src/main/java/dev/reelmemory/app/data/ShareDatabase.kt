package dev.reelmemory.app.data

import android.content.Context
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase
import kotlinx.coroutines.flow.Flow

/**
 * Durable local queue for shared reels. The share is persisted FIRST (before
 * any network), so a share is never lost even if the backend is unreachable
 * or the app is killed. This is the client side of PRD FR-CAP-004.
 */
object ShareStatus {
    const val PENDING = "PENDING"   // saved locally, not yet sent
    const val SYNCING = "SYNCING"   // upload in progress
    const val SYNCED = "SYNCED"     // backend accepted (202/200)
    const val FAILED = "FAILED"     // last attempt failed; worker will retry
    const val QUOTA = "QUOTA"       // parked: free quota exhausted (402); user upgrades or retries
}

@Entity(tableName = "queued_shares")
data class QueuedShare(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val platform: String,
    val platformItemId: String,
    val canonicalUrl: String,
    val originalUrl: String,
    val status: String = ShareStatus.PENDING,
    val attempts: Int = 0,
    val lastError: String? = null,
    /** Backend memory id, when the backend reported one. */
    val remoteMemoryId: String? = null,
    val createdAt: Long = System.currentTimeMillis(),
    val syncedAt: Long? = null
)

@Dao
interface ShareDao {
    @Insert(onConflict = OnConflictStrategy.ABORT)
    suspend fun insert(share: QueuedShare): Long

    /** Client-side dedupe: one row per (platform, platform_item_id). */
    @Query(
        "SELECT * FROM queued_shares " +
            "WHERE platform = :platform AND platformItemId = :platformItemId LIMIT 1"
    )
    suspend fun findBySource(platform: String, platformItemId: String): QueuedShare?

    @Query("SELECT * FROM queued_shares ORDER BY createdAt DESC")
    fun observeAll(): Flow<List<QueuedShare>>

    @Query("SELECT * FROM queued_shares WHERE status IN ('PENDING','FAILED') ORDER BY createdAt ASC")
    suspend fun pendingOrFailed(): List<QueuedShare>

    @Query("UPDATE queued_shares SET status = :status, attempts = attempts + 1 WHERE id = :id")
    suspend fun markAttempt(id: Long, status: String)

    @Query(
        "UPDATE queued_shares SET status = :status, lastError = :error, syncedAt = :syncedAt, " +
            "remoteMemoryId = COALESCE(:remoteMemoryId, remoteMemoryId) WHERE id = :id"
    )
    suspend fun markResult(
        id: Long,
        status: String,
        error: String?,
        syncedAt: Long?,
        remoteMemoryId: String?
    )

    @Query("SELECT COUNT(*) FROM queued_shares WHERE status IN ('PENDING','FAILED')")
    suspend fun pendingCount(): Int

    /** After an upgrade (or a manual retry), un-park quota-blocked rows. */
    @Query("UPDATE queued_shares SET status = 'PENDING', lastError = NULL WHERE status = 'QUOTA'")
    suspend fun requeueQuotaBlocked(): Int
}

@Database(
    entities = [QueuedShare::class, VideoUpload::class, WebCapture::class, BriefDecision::class,
        AlbumUpload::class, AlbumFile::class],
    version = 4,
    exportSchema = false
)
abstract class ShareDatabase : RoomDatabase() {
    abstract fun shareDao(): ShareDao
    abstract fun videoUploadDao(): VideoUploadDao
    abstract fun webCaptureDao(): WebCaptureDao
    abstract fun briefDecisionDao(): BriefDecisionDao
    abstract fun albumUploadDao(): AlbumUploadDao

    companion object {
        @Volatile
        private var instance: ShareDatabase? = null

        private val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    "CREATE TABLE IF NOT EXISTS `video_uploads` (" +
                        "`id` INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, " +
                        "`fileName` TEXT NOT NULL, " +
                        "`mimeType` TEXT NOT NULL, " +
                        "`sizeBytes` INTEGER NOT NULL, " +
                        "`localPath` TEXT NOT NULL, " +
                        "`originalUrl` TEXT, " +
                        "`status` TEXT NOT NULL, " +
                        "`progress` INTEGER NOT NULL, " +
                        "`attempts` INTEGER NOT NULL, " +
                        "`lastError` TEXT, " +
                        "`remoteMemoryId` TEXT, " +
                        "`createdAt` INTEGER NOT NULL, " +
                        "`updatedAt` INTEGER NOT NULL)"
                )
            }
        }

        private val MIGRATION_2_3 = object : Migration(2, 3) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    "CREATE TABLE IF NOT EXISTS `web_captures` (" +
                        "`id` INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, " +
                        "`url` TEXT NOT NULL, " +
                        "`status` TEXT NOT NULL, " +
                        "`attempts` INTEGER NOT NULL, " +
                        "`lastError` TEXT, " +
                        "`remoteMemoryId` TEXT, " +
                        "`createdAt` INTEGER NOT NULL, " +
                        "`updatedAt` INTEGER NOT NULL)"
                )
                db.execSQL(
                    "CREATE TABLE IF NOT EXISTS `brief_decisions` (" +
                        "`memoryId` TEXT NOT NULL PRIMARY KEY, " +
                        "`choice` TEXT NOT NULL, " +
                        "`decidedAt` INTEGER NOT NULL)"
                )
            }
        }

        private val MIGRATION_3_4 = object : Migration(3, 4) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    "CREATE TABLE IF NOT EXISTS `album_uploads` (" +
                        "`id` INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, " +
                        "`fileCount` INTEGER NOT NULL, " +
                        "`totalBytes` INTEGER NOT NULL, " +
                        "`status` TEXT NOT NULL, " +
                        "`progress` INTEGER NOT NULL, " +
                        "`attempts` INTEGER NOT NULL, " +
                        "`lastError` TEXT, " +
                        "`remoteMemoryId` TEXT, " +
                        "`createdAt` INTEGER NOT NULL, " +
                        "`updatedAt` INTEGER NOT NULL)"
                )
                db.execSQL(
                    "CREATE TABLE IF NOT EXISTS `album_files` (" +
                        "`id` INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, " +
                        "`albumId` INTEGER NOT NULL, " +
                        "`fileName` TEXT NOT NULL, " +
                        "`mimeType` TEXT NOT NULL, " +
                        "`sizeBytes` INTEGER NOT NULL, " +
                        "`localPath` TEXT NOT NULL, " +
                        "`sortOrder` INTEGER NOT NULL, " +
                        "FOREIGN KEY(`albumId`) REFERENCES `album_uploads`(`id`) " +
                        "ON UPDATE NO ACTION ON DELETE CASCADE)"
                )
                db.execSQL(
                    "CREATE INDEX IF NOT EXISTS `index_album_files_albumId` " +
                        "ON `album_files` (`albumId`)"
                )
            }
        }

        fun get(context: Context): ShareDatabase =
            instance ?: synchronized(this) {
                instance ?: Room.databaseBuilder(
                    context.applicationContext,
                    ShareDatabase::class.java,
                    "reel-memory.db"
                ).addMigrations(MIGRATION_1_2, MIGRATION_2_3, MIGRATION_3_4).build().also { instance = it }
            }
    }
}
