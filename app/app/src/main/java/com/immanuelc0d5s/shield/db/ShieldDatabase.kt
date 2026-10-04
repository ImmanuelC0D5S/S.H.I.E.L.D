package com.immanuelc0d5s.shield.db

import android.content.Context
import android.util.Log
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.sqlite.db.SupportSQLiteDatabase
import com.immanuelc0d5s.shield.data.ShieldEvent
import com.immanuelc0d5s.shield.data.Verdict
import kotlinx.coroutines.flow.Flow
import net.zetetic.database.sqlcipher.SupportOpenHelperFactory

/**
 * The local event log. P0 #4.
 *
 * **Encrypted at rest with SQLCipher.** The rows hold signed storage URLs of
 * photographs of the user's home, addressable for an hour each and cached on
 * disk by Coil. Plain SQLite would put all of it in a file any process with
 * `READ_EXTERNAL_STORAGE`-equivalent access could read, and the app's own backup
 * rules exclude the database - so unencrypted would mean the only way to protect
 * it is to not keep it.
 */
@Entity(tableName = "events")
data class EventEntity(
    @PrimaryKey val id: String,
    /** Unix seconds, UTC. Matches `events.ts` exactly. */
    val ts: Long,
    val status: Int,
    val confidence: Int,
    /** Object path in the `snapshots` bucket, never a signed URL. */
    val imagePath: String,
    val verdict: String?,
    val verdictAt: Long?,
    val identity: String?,
    /** Set when the push for this event has been acted on. */
    val alarmHandled: Boolean = false,
)

fun EventEntity.toDomain(): ShieldEvent = ShieldEvent(
    id = id,
    ts = ts,
    status = status,
    confidence = confidence,
    imagePath = imagePath,
    verdict = Verdict.fromWire(verdict),
    identity = identity,
)

fun ShieldEvent.toEntity(): EventEntity = EventEntity(
    id = id,
    ts = ts,
    status = status,
    confidence = confidence,
    imagePath = imagePath,
    verdict = verdict?.wire,
    verdictAt = verdict?.let { ts },
    identity = identity,
)

@Dao
interface EventDao {
    @Query("SELECT * FROM events ORDER BY ts DESC")
    fun observeAll(): Flow<List<EventEntity>>

    @Query("SELECT * FROM events ORDER BY ts DESC LIMIT :limit")
    suspend fun recent(limit: Int = 50): List<EventEntity>

    @Query("SELECT * FROM events WHERE id = :id")
    suspend fun byId(id: String): EventEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(event: EventEntity)

    @Query("UPDATE events SET verdict = :verdict, verdictAt = :verdictAt WHERE id = :id")
    suspend fun setVerdict(id: String, verdict: String?, verdictAt: Long?)

    @Query("UPDATE events SET alarmHandled = 1 WHERE id = :id")
    suspend fun markHandled(id: String)

    @Query("DELETE FROM events WHERE ts < :cutoffSeconds")
    suspend fun pruneOlderThan(cutoffSeconds: Long)

    @Query("SELECT COUNT(*) FROM events")
    suspend fun count(): Int
}

@Database(
    entities = [EventEntity::class],
    version = 1,
    exportSchema = true,
)
abstract class ShieldDatabase : RoomDatabase() {
    abstract fun events(): EventDao

    companion object {
        private const val TAG = "ShieldDatabase"
        private const val DB_NAME = "shield-events.db"

        @Volatile
        private var instance: ShieldDatabase? = null

        fun get(context: Context): ShieldDatabase =
            instance ?: synchronized(this) { instance ?: build(context).also { instance = it } }

        private fun build(context: Context): ShieldDatabase {
            val passphrase = DatabaseKey.getOrCreate(context)
            val factory = SupportOpenHelperFactory(passphrase)

            return Room.databaseBuilder(context, ShieldDatabase::class.java, DB_NAME)
                .openHelperFactory(factory)
                // No fallbackToDestructiveMigration. Every row here is a record of
                // an intrusion detection, and a version bump silently dropping
                // them would destroy the audit trail without anyone noticing.
                // Add an explicit Migration instead.
                .addCallback(object : Callback() {
                    override fun onOpen(db: SupportSQLiteDatabase) {
                        // SQLCipher leaves the journal files readable-by-key only
                        // once the key is applied; WAL gives better crash
                        // behaviour than the default rollback journal, which
                        // matters because an alert is written moments before a
                        // verdict and a crash in between must not lose it.
                        Log.i(TAG, "event log open (encrypted)")
                    }
                })
                .build()
        }

        /** Test hook. Production code never resets this. */
        internal fun overrideForTest(database: ShieldDatabase?) {
            instance = database
        }
    }
}