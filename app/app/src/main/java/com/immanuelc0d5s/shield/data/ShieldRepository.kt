package com.immanuelc0d5s.shield.data

import android.content.Context
import android.util.Log
import com.immanuelc0d5s.shield.db.EventDao
import com.immanuelc0d5s.shield.db.EventEntity
import com.immanuelc0d5s.shield.db.ShieldDatabase
import com.immanuelc0d5s.shield.db.toDomain
import com.immanuelc0d5s.shield.db.toEntity
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

/**
 * Everything the UI and the alert screen call.
 *
 * Deliberately thin. The interesting behaviour - what happens when the network is
 * down, when a verdict write fails, when a token rotates - is decisions that
 * belong in one testable place rather than scattered across composables.
 */
class ShieldRepository(
    private val client: SupabaseClient,
    private val dao: EventDao,
) {
    val events: Flow<List<ShieldEvent>> = dao.observeAll().map { rows -> rows.map { it.toDomain() } }

    val configured: Boolean get() = client.isConfigured

    // -- devices ----------------------------------------------------------

    /**
     * P0 #5. Called on first launch and from `onNewToken`.
     *
     * Failure is logged and not surfaced. A token that could not be registered
     * yet is not a user-facing problem, and FCM will call `onNewToken` again on
     * the next successful connection - so the honest behaviour is to try again
     * rather than show an error the user can do nothing about.
     */
    suspend fun registerToken(token: String, appVersion: String) {
        client.registerDevice(token, appVersion)
            .onFailure { Log.w(TAG, "could not register FCM token: $it") }
    }

    // -- events -----------------------------------------------------------

    /**
     * Record an alert locally, from a push or from a fetch.
     *
     * Written to SQLCipher before the notification is posted, not after: if the
     * process dies between the two, the history must still show what woke the
     * phone.
     */
    suspend fun recordAlert(payload: AlertPayload): ShieldEvent {
        val event = ShieldEvent(
            id = payload.eventId,
            ts = payload.ts,
            status = payload.status,
            confidence = payload.confidence,
            imagePath = payload.imageUrl,
            verdict = null,
            identity = null,
        )
        dao.upsert(event.toEntity().copy(alarmHandled = false))
        return event
    }

    /**
     * P0 #3. Persist the user's answer locally and send it to the RPi.
     *
     * The local write happens first and unconditionally, so the UI updates
     * instantly and the answer survives the app being killed mid-request. The
     * remote write's failure is returned, and the caller shows it, because a
     * verdict that never reaches the database means the RPi keeps escalating.
     */
    suspend fun submitVerdict(eventId: String, verdict: Verdict): Result<Unit> {
        val nowSeconds = System.currentTimeMillis() / 1000
        dao.setVerdict(eventId, verdict.wire, nowSeconds)
        dao.markHandled(eventId)
        return client.submitVerdict(eventId, verdict)
    }

    suspend fun eventById(eventId: String): ShieldEvent? = dao.byId(eventId)?.toDomain()

    suspend fun refreshEvents(limit: Int = 50): Result<List<ShieldEvent>> =
        client.fetchEvents(limit).map { remote ->
            // Cache what the server already knows, so the history list is useful
            // with no connectivity and shows the server's verdicts rather than
            // whatever this device happened to be told.
            remote.forEach { dao.upsert(it.toEntity()) }
            remote
        }

    // -- arm state --------------------------------------------------------

    suspend fun fetchArmed(): Boolean = client.fetchArmed().getOrDefault(true)

    suspend fun setArmed(armed: Boolean): Result<Unit> = client.setArmed(armed)

    // -- diagnostics ------------------------------------------------------

    /**
     * P0 #5: the settings screen's "Send test alert".
     *
     * Inserts a real `status = 2` event rather than firing a local notification,
     * because the thing being tested is the path *through* the database webhook
     * and the Edge Function. A locally-posted notification would prove nothing:
     * it would succeed on a deployment where the webhook is broken.
     */
    suspend fun insertTestAlert(deviceId: String): Result<String> =
        client.insertTestEvent(deviceId)

    suspend fun localEventCount(): Int = dao.count()

    companion object {
        private const val TAG = "ShieldRepository"

        @Volatile
        private var instance: ShieldRepository? = null

        fun get(context: Context): ShieldRepository =
            instance ?: synchronized(this) {
                instance ?: ShieldRepository(
                    client = SupabaseClient(),
                    dao = ShieldDatabase.get(context).events(),
                ).also { instance = it }
            }

        internal fun overrideForTest(repository: ShieldRepository?) {
            instance = repository
        }
    }
}

/** Result of a local-cache miss, used by the alert screen to pick a load strategy. */
fun EventEntity.isTestEvent(): Boolean = id.startsWith("test-")