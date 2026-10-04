package com.immanuelc0d5s.shield

import android.app.Application
import android.util.Log
import com.google.firebase.messaging.FirebaseMessaging
import com.immanuelc0d5s.shield.alarm.AlarmChannels
import com.immanuelc0d5s.shield.data.ShieldRepository
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

/**
 * Registers the FCM token and creates the notification channels at process start.
 *
 * The token registration here and in
 * [com.immanuelc0d5s.shield.fcm.ShieldMessagingService.onNewToken] are both
 * needed and neither subsumes the other: `onNewToken` fires on a rotation but not
 * on a fresh install, and this fires on a fresh install but not on a rotation.
 * Missing either is a way to receive no alerts at all, silently.
 */
class ShieldApp : Application() {

    /**
     * A scope owned by the application rather than by a call site, because
     * `getToken` completes after `onCreate` returns and there is no lifecycle left
     * to attach the continuation to.
     */
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onCreate() {
        super.onCreate()

        // SQLCipher's native library, before anything can open the database.
        //
        // net.zetetic:sqlcipher-android ships libsqlcipher.so for all four ABIs and
        // does NOT load it for you. SupportOpenHelperFactory reaches
        // SQLiteConnection.nativeOpen through JNI, so until this call the method
        // exists in the Java class and has no implementation behind it, and the
        // first database open dies with
        //   UnsatisfiedLinkError: No implementation found for
        //   SQLiteConnection.nativeOpen (net.zetetic.database.sqlcipher)
        //
        // onCreate is the only place early enough. Application.onCreate runs before
        // any Activity, Service or ContentProvider in this process, and
        // ShieldRepository.get() below opens the event log -- so a load placed in
        // MainActivity, or lazily on first query, loses the race against the
        // FCM service, which can start this process with no Activity at all when a
        // push arrives. That path is the one that matters most here: it is the
        // alarm arriving, and it would crash before the alarm could play.
        //
        // Idempotent and cheap after the first call, so this is safe on every start.
        try {
            System.loadLibrary("sqlcipher")
        } catch (error: UnsatisfiedLinkError) {
            // Fail here, loudly, rather than at the first database open with an
            // error that names a JNI method and not the cause. A missing or
            // unloadable .so almost always means the ABI is not in the APK.
            throw IllegalStateException(
                "libsqlcipher.so could not be loaded. Check that the APK contains " +
                    "jni/<abi>/libsqlcipher.so for this device's ABI.",
                error,
            )
        }

        // Channels must exist before any notification is posted. On Android 8+ a
        // post to an unknown channel id is dropped silently: no notification and
        // no error, which reads as "the app received the push and did nothing".
        AlarmChannels.ensure(this)

        val repository = ShieldRepository.get(this)
        if (!repository.configured) {
            Log.e(
                TAG,
                "Supabase is not configured; the app cannot register a device token or " +
                    "write verdicts. Set SHIELD_SUPABASE_URL and SHIELD_SUPABASE_ANON_KEY " +
                    "in app/local.properties and rebuild.",
            )
            return
        }

        // Deliberately not awaited: Application.onCreate runs on the main thread
        // before any Activity exists, and blocking it delays the first frame for a
        // value that is only needed when an alert eventually arrives.
        FirebaseMessaging.getInstance().token
            .addOnSuccessListener { token ->
                Log.i(TAG, "FCM token acquired; registering with Supabase")
                scope.launch { repository.registerToken(token, appVersion()) }
            }
            .addOnFailureListener { error ->
                // Play Services unavailable or broken. Nothing the user can do, but
                // it must be loud in logcat because the symptom otherwise appears
                // weeks later as "the app never gets alerts".
                Log.e(TAG, "could not obtain an FCM token; this device will receive no alerts", error)
            }
    }

    private fun appVersion(): String =
        runCatching {
            packageManager.getPackageInfo(packageName, 0).versionName ?: "unknown"
        }.getOrDefault("unknown")

    private companion object {
        const val TAG = "ShieldApp"
    }
}