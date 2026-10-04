package com.immanuelc0d5s.shield.data

import android.util.Log
import com.immanuelc0d5s.shield.BuildConfig
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

/**
 * Supabase REST and Storage access.
 *
 * **No Supabase SDK.** The whole surface this app needs is four requests -
 * upsert a device token, read events, update one verdict, read/write the arm
 * flag - plus one authenticated fetch of a signed image URL. `supabase-kt` would
 * add a Kotlin wrapper over `postgrest-js` over OkHttp to do that, in an app that
 * must start and reach the alarm path fast and has no other HTTP need.
 *
 * All calls use the **publishable** key (`anon`), never `SUPABASE_SERVICE_KEY`.
 * That is not a style preference: the service key bypasses RLS, and RLS is the
 * only thing keeping this client from being able to insert events, delete them,
 * or read anyone else's row. See DECISIONS.md D-040.
 */
class SupabaseClient(
    private val baseUrl: String = BuildConfig.SUPABASE_URL,
    private val anonKey: String = BuildConfig.SUPABASE_ANON_KEY,
) {
    private val rest get() = "$baseUrl/rest/v1"
    private val storage get() = "$baseUrl/storage/v1"

    val isConfigured: Boolean get() = baseUrl.isNotBlank() && anonKey.isNotBlank()

    class SupabaseException(message: String, cause: Throwable? = null) :
        IOException(message, cause)

    // -- transport --------------------------------------------------------

    private fun open(path: String, method: String, accept: String = "application/json"): HttpURLConnection {
        val connection = URL("$baseUrl$path").openConnection() as HttpURLConnection
        connection.requestMethod = method
        connection.connectTimeout = CONNECT_TIMEOUT_MS
        connection.readTimeout = READ_TIMEOUT_MS
        connection.setRequestProperty("apikey", anonKey)
        connection.setRequestProperty("Authorization", "Bearer $anonKey")
        connection.setRequestProperty("Accept", accept)
        return connection
    }

    private fun HttpURLConnection.readBody(): String =
        (if (responseCode in 200..299) inputStream else errorStream)
            ?.bufferedReader()?.use { it.readText() }.orEmpty()

    private fun requireConfigured() {
        if (!isConfigured) {
            throw SupabaseException(
                "Supabase is not configured. Set SHIELD_SUPABASE_URL and " +
                    "SHIELD_SUPABASE_ANON_KEY in app/local.properties and rebuild."
            )
        }
    }

    // -- devices ----------------------------------------------------------

    /**
     * Register or refresh this phone's FCM token. P0 #5.
     *
     * Upserted on `fcm_token`, so the row is replaced rather than duplicated.
     * That matters more than it looks: the Edge Function pushes to every row in
     * `devices`, and a reinstall generates a new token, so without an upsert the
     * old token would linger and the phone would eventually receive pushes for a
     * registration that no longer exists.
     */
    suspend fun registerDevice(token: String, appVersion: String): Result<Unit> =
        withContext(Dispatchers.IO) {
            runCatching {
                requireConfigured()
                val body = JSONObject().apply {
                    put("fcm_token", token)
                    put("platform", "android")
                    put("app_version", appVersion)
                }.toString().toByteArray()

                val connection = open("/rest/v1/devices?on_conflict=fcm_token", "POST").apply {
                    doOutput = true
                    setRequestProperty("Content-Type", "application/json")
                    // resolution=merge-duplicates is what makes this an upsert
                    // rather than an insert that fails on the second launch.
                    setRequestProperty("Prefer", "resolution=merge-duplicates,return=minimal")
                }
                connection.outputStream.use { it.write(body) }

                val status = connection.responseCode
                if (status !in 200..299) {
                    throw SupabaseException("device registration failed: $status ${connection.readBody()}")
                }
                connection.disconnect()
            }
        }

    // -- events -----------------------------------------------------------

    suspend fun fetchEvents(limit: Int = 50): Result<List<ShieldEvent>> = withContext(Dispatchers.IO) {
        runCatching {
            requireConfigured()
            val connection = open("/rest/v1/events?select=*&order=ts.desc&limit=$limit", "GET")
            val payload = connection.readBody()
            if (connection.responseCode !in 200..299) {
                throw SupabaseException("event fetch failed: ${connection.responseCode} $payload")
            }
            connection.disconnect()
            parseEvents(payload)
        }
    }

    /**
     * Write the user's answer to `events.verdict`. P0 #3.
     *
     * The RPi is polling for exactly this value and stops when it appears, so a
     * silent failure here means the buzzer keeps sounding until the escalation
     * timeout. Errors are returned rather than swallowed.
     */
    suspend fun submitVerdict(eventId: String, verdict: Verdict): Result<Unit> =
        withContext(Dispatchers.IO) {
            runCatching {
                requireConfigured()
                val body = JSONObject().apply {
                    put("verdict", verdict.wire)
                    put("verdict_at", System.currentTimeMillis() / 1000)
                }.toString().toByteArray()

                val connection =
                    open("/rest/v1/events?id=eq.${eventId.urlEncoded()}", "PATCH").apply {
                        doOutput = true
                        setRequestProperty("Content-Type", "application/json")
                        setRequestProperty("Prefer", "return=minimal")
                    }
                connection.outputStream.use { it.write(body) }

                val status = connection.responseCode
                if (status !in 200..299) {
                    throw SupabaseException("verdict write failed: $status ${connection.readBody()}")
                }
                connection.disconnect()
            }
        }

    /**
     * Insert a `status = 2` event to test the whole push path.
     *
     * P0 #5's "Send test alert". It inserts a real row because the only honest
     * way to prove the database webhook and the Edge Function work is to make
     * the same insert the RPi makes. The row is marked by its event id prefix so
     * it is identifiable in the log.
     */
    suspend fun insertTestEvent(deviceId: String): Result<String> = withContext(Dispatchers.IO) {
        runCatching {
            requireConfigured()
            val eventId = "test-" + java.util.UUID.randomUUID()
            val row = JSONObject().apply {
                put("id", eventId)
                put("ts", System.currentTimeMillis() / 1000)
                put("status", AlertPayload.STATUS_ALERT)
                put("confidence", 100)
                // No image. A test alert that claimed a snapshot would 404 in the
                // alert screen and look like the signed-URL path is broken.
                put("image_path", "")
                put("needs_verdict", true)
                put("device_id", deviceId)
                put("identity", JSONObject.NULL)
            }

            val connection = open("/rest/v1/events", "POST").apply {
                doOutput = true
                setRequestProperty("Content-Type", "application/json")
                setRequestProperty("Prefer", "return=minimal")
            }
            connection.outputStream.use { it.write(JSONArray().put(row).toString().toByteArray()) }

            val status = connection.responseCode
            if (status !in 200..299) {
                throw SupabaseException("test alert insert failed: $status ${connection.readBody()}")
            }
            connection.disconnect()
            eventId
        }
    }

    // -- system state -----------------------------------------------------

    /**
     * Read `system_state.armed`. Defaults to armed on any failure.
     *
     * Failing open is deliberate: an unreachable Supabase must not leave the app
     * showing "disarmed" when the RPi is in fact armed and alerting. The RPi
     * makes the same call for the same reason - see `alerts.py::is_armed`.
     */
    suspend fun fetchArmed(): Result<Boolean> = withContext(Dispatchers.IO) {
        runCatching {
            requireConfigured()
            val connection = open("/rest/v1/system_state?id=eq.1&select=armed", "GET")
            val payload = connection.readBody()
            if (connection.responseCode !in 200..299) {
                Log.w(TAG, "could not read system_state: ${connection.responseCode}")
                return@runCatching true
            }
            connection.disconnect()
            JSONArray(payload).optJSONObject(0)?.optBoolean("armed", true) ?: true
        }.recoverCatching { error ->
            Log.w(TAG, "system_state unreadable, assuming armed: $error")
            true
        }
    }

    suspend fun setArmed(armed: Boolean): Result<Unit> = withContext(Dispatchers.IO) {
        runCatching {
            requireConfigured()
            val body = JSONObject().apply {
                put("armed", armed)
                put("updated_by", "app")
            }.toString().toByteArray()

            val connection = open("/rest/v1/system_state?id=eq.1", "PATCH").apply {
                doOutput = true
                setRequestProperty("Content-Type", "application/json")
                setRequestProperty("Prefer", "return=minimal")
            }
            connection.outputStream.use { it.write(body) }

            val status = connection.responseCode
            if (status !in 200..299) {
                throw SupabaseException("arm write failed: $status ${connection.readBody()}")
            }
            connection.disconnect()
        }
    }

    // -- parsing ----------------------------------------------------------

    private fun parseEvents(payload: String): List<ShieldEvent> {
        val array = JSONArray(payload)
        return buildList(array.length()) {
            for (index in 0 until array.length()) {
                val row = array.optJSONObject(index) ?: continue
                val id = row.optString("id").takeIf { it.isNotBlank() } ?: continue
                add(
                    ShieldEvent(
                        id = id,
                        ts = row.optLong("ts", 0L),
                        status = row.optInt("status", AlertPayload.STATUS_ALERT),
                        confidence = row.optInt("confidence", 0),
                        imagePath = row.optString("image_path"),
                        verdict = Verdict.fromWire(row.optString("verdict").takeIf { it.isNotBlank() }),
                        identity = row.optString("identity").takeIf { it.isNotBlank() },
                    )
                )
            }
        }
    }

    private fun String.urlEncoded(): String = java.net.URLEncoder.encode(this, "UTF-8")

    companion object {
        private const val TAG = "SupabaseClient"
        private const val CONNECT_TIMEOUT_MS = 10_000

        /**
         * Read timeout.
         *
         * Generous on purpose. This read happens on the alert screen, where the
         * user is deciding whether a person in their doorway is a stranger, and a
         * timeout that fires while the snapshot is loading produces a blank
         * screen exactly when it matters most.
         */
        private const val READ_TIMEOUT_MS = 20_000
    }
}