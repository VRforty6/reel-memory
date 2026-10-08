package dev.reelmemory.app.data

import androidx.room.Dao
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import kotlinx.coroutines.flow.Flow

/**
 * The user's local decision on a decision-brief card.
 *
 * Recorded on-device only for now (no backend call): "yes" = "Yes, do it",
 * "no" = "Not now". One row per memory; re-deciding overwrites.
 */
@Entity(tableName = "brief_decisions")
data class BriefDecision(
    @PrimaryKey val memoryId: String,
    val choice: String,
    val decidedAt: Long = System.currentTimeMillis(),
) {
    companion object {
        const val YES = "yes"
        const val NO = "no"
    }
}

@Dao
interface BriefDecisionDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(decision: BriefDecision)

    @Query("SELECT * FROM brief_decisions WHERE memoryId = :memoryId")
    fun observe(memoryId: String): Flow<BriefDecision?>

    @Query("SELECT * FROM brief_decisions WHERE memoryId = :memoryId")
    suspend fun get(memoryId: String): BriefDecision?

    @Query("DELETE FROM brief_decisions")
    suspend fun clearAll()
}
