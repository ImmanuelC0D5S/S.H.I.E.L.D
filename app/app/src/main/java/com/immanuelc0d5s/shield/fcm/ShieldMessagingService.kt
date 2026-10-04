package com.immanuelc0d5s.shield.fcm

import android.content.Context
import android.util.Log
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import com.immanuelc0d5s.shield.data.AlertPayload
import com.immanuelc0d5s.shield.data.ShieldRepository
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

/**
 * P0 #1: receive the data-only FCM message with the app killed or in Doze.
 *
 * Three things about this callback are load-bearing.
 *
 * **It must return fast.** Android gives a [BroadcastReceiver] about 10 seconds
 * before ANR-killing the app, and an ANR here means the process dies before the
 * notification is posted - so the alarm never appears. Everything expensive (the
 * database write, the repository call) goes onto [scope] and the method returns
 * immediately. The alarm is posted synchronously *before* the coroutine starts,
 * because that is the only part that must not be deferred.
 *
 * **The payload is data-only.** shield-app-prompt.md is explicit that there is no
 * `notification` block. That is what makes FCM deliver to
 * `onMessageReceived` at all: with a `notification` block, FCM displays its own
 * notification and this method is never called when the app is in the background.
 * Losing that block would mean the alarm looked fine in the foreground and did
 * nothing with the app killed - exactly the Done-when case.
 *
 * **The scope outlives the callback.** [scope] is a process-lifetime
 * [CoroutineScope] rather than one tied to this service, because the service is
 * destroyed as soon as `onMessageReceived` returns and the work must not be
 * cancelled with it.
 */
class ShieldMessagingService : FirebaseMessagingService() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    override fun onMessageReceived(message: RemoteMessage) {
        val payload = AlertPayload.fromMap(message.data)
        if (payload == null) {
            Log.w(TAG, "push with no usable event_id; ignoring: ${message.data.keys}")
            return
        }

        if (!payload.requiresVerdict) {
            // The Edge Function already filters status 0 and 1. Reaching here
            // means the payload and the function disagree, which is worth a log
            // but not an alarm: waking someone at 3am for a status 1 "known" is
            // how an alarm app loses its credibility.
            Log.w(TAG, "push status ${payload.status} needsVerdict=${payload.needsVerdict}; not alarming")
            return
        }

        Log.i(TAG, "alert push ${payload.eventId} confidence=${payload.confidence}")

        // Synchronous and first. Everything below this line may be deferred; this
        // must not be.
        AlarmPoster.post(this, payload)

        scope.launch {
            runCatching {
                ShieldRepository.get(applicationContext).recordAlert(payload)
            }.onFailure {
                Log.e(TAG, "could not cache the alert locally", it)
            }
        }
    }

    /**
     * P0 #5: the FCM token rotated. Re-register immediately.
     *
     * The old token's row is overwritten by the upsert on `fcm_token`, so there
     * is no cleanup to do - but only if this runs. A token that goes
     * unregistered silently is the most common cause of "the app worked for a
     * month and then stopped receiving alerts".
     */
    override fun onNewToken(token: String) {
        Log.i(TAG, "FCM token rotated; re-registering")
        scope.launch {
            runCatching {
                ShieldRepository.get(applicationContext).registerToken(token, appVersion())
            }.onFailure {
                Log.e(TAG, "could not re-register the rotated token", it)
            }
        }
    }

    private fun appVersion(): String =
        runCatching {
            packageManager.getPackageInfo(packageName, 0).versionName ?: "unknown"
        }.getOrDefault("unknown")

    private companion object {
        const val TAG = "ShieldMessagingService"
    }
}

/** Thin seam so [ShieldMessagingService] can be exercised without a Firebase instance. */
internal object AlarmPoster {
    fun post(context: Context, payload: AlertPayload) {
        com.immanuelc0d5s.shield.alarm.AlarmChannels.ensure(context)
        com.immanuelc0d5s.shield.alarm.AlarmChannels.postAlert(
            context, payload.eventId, payload.confidence,
        )
    }
}