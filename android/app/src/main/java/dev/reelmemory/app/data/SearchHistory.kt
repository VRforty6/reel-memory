package dev.reelmemory.app.data

import androidx.room.Dao
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.Index
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Transaction
import kotlinx.coroutines.flow.Flow
import java.util.Locale

const val RECENT_SEARCH_LIMIT = 20

@Entity(tableName = "recent_searches", indices = [Index("searchedAt")])
data class RecentSearch(
    @PrimaryKey val normalizedQuery: String,
    val query: String,
    val searchedAt: Long,
)

fun normalizeSearchQuery(query: String): String =
    query.trim().replace(Regex("\\s+"), " ").lowercase(Locale.ROOT)

@Dao
interface SearchHistoryDao {
    @Query("SELECT * FROM recent_searches ORDER BY searchedAt DESC LIMIT :limit")
    fun observeRecent(limit: Int = RECENT_SEARCH_LIMIT): Flow<List<RecentSearch>>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(item: RecentSearch)

    @Query(
        "DELETE FROM recent_searches WHERE normalizedQuery NOT IN " +
            "(SELECT normalizedQuery FROM recent_searches ORDER BY searchedAt DESC LIMIT :limit)"
    )
    suspend fun trimTo(limit: Int)

    @Transaction
    suspend fun record(query: String, searchedAt: Long = System.currentTimeMillis()) {
        val normalized = normalizeSearchQuery(query)
        if (normalized.isBlank()) return
        upsert(RecentSearch(normalized, query.trim().replace(Regex("\\s+"), " "), searchedAt))
        trimTo(RECENT_SEARCH_LIMIT)
    }

    @Query("DELETE FROM recent_searches WHERE normalizedQuery = :normalizedQuery")
    suspend fun delete(normalizedQuery: String)

    @Query("DELETE FROM recent_searches")
    suspend fun clear()
}
