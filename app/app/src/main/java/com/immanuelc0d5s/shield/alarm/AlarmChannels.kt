package com.immanuelc0d5s.shield.alarm

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.os.Build
import android.util.Log
import androidx.core.app.NotificationCompat
import com.immanuelc0d5s.shield.R
import com.immanuelc0d5s.shield.alarm.AlertActivity

/**
 * The alarm notification. P0 #2.
 *
 * Three properties have to be right and each has its own trap.
 *
 * **`IMPORTANCE_HIGH`.** Without it the notification is silent and heads-up is
 * not offered at all. It still requires a user-visible channel, so this is only
 * half the requirement.
 *
 * **`USAGE_ALARM`, not `USAGE_NOTIFICATION`.** This is the difference between
 * the alarm sounding through Do Not Disturb and being suppressed by it. A user who
 * silenced their phone at night did not mean to silence an intruder detector, and
 * `setBypassDnd` additionally needs a Notification Policy Access grant, which the
 * app requests but which the user may refuse - so the audio usage attribute is the
 * thing that carries the request, and bypass-DND is the bonus.
 *
 * **The sound is looped by the player, not by the channel.** A channel sound
 * loops only if the URI's audio metadata says so, and `RingtoneManager.TYPE_ALARM`
 * does not guarantee that on every device. The notification therefore sets a sound
 * for the notification's own lifetime, and [AlarmPlayer] runs a separate looping
 * player that the alert activity tears down on dismissal. Relying on the channel
 * alone is the single most common way this feature "works in testing" and then
 * plays once on a real phone.
 */
object AlarmChannels {
    private const val TAG = "AlarmChannels"

    const val ALARM_ID = "shield_alarm_v1"
    const val STATUS_ID = "shield_status_v1"

    const val NOTIFICATION_ID_ALERT = 1001

    /**
     * Idempotent, and safe to call from a BroadcastReceiver or a Service.
     *
     * Re-creating a channel with the same id does not reset a user's changes - it
     * is a no-op once the channel exists. That is the behaviour we want: if the
     * owner has muted the alarm channel on purpose, this must not silently undo
     * that every time a push arrives.
     */
    fun ensure(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java) ?: return

        val alarmUri = alarmSound()

        manager.createNotificationChannel(
            NotificationChannel(
                ALARM_ID,
                "Intruder alarms",
                NotificationManager.IMPORTANCE_HIGH,
            ).apply {
                description = "Full-screen alerts with sound and vibration. Do not mute."
                setSound(
                    alarmUri,
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build(),
                )
                enableVibration(true)
                // 0.5 s on, 0.5 s off, repeat. A steady buzz is indistinguishable
                // from a notification; the gap is what makes it read as an alarm.
                vibrationPattern = longArrayOf(0, 500, 500, 500, 500)
                // No explicit amplitude. NotificationChannel has no public
                // setVibrationAmplitude; amplitude control for the channel path
                // is not part of the platform API. The AlarmPlayer's
                // VibrationEffect is what carries amplitude, and even there it is
                // advisory - many devices ignore it and vibrate at full strength.
                setShowBadge(true)
                lockscreenVisibility = Notification.VISIBILITY_PUBLIC
                // Only honoured when the owner has granted Notification Policy
                // Access. Without it, alarms are silenced by Do Not Disturb and the
                // owner is told so from the settings screen rather than finding
                // out at 3am.
                setBypassDnd(canBypassDnd(context))
            },
        )

        // Separate channel for the low-priority notifications the app posts
        // outside an alert (a successful test alert, a token refresh failure).
        // Sharing the alarm channel would make those unmissable, which trains the
        // owner to ignore the channel and with it the real alarm.
        manager.createNotificationChannel(
            NotificationChannel(
                STATUS_ID,
                "Status",
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = "Background status messages."
                setSound(null, null)
                enableVibration(false)
                setShowBadge(false)
            },
        )

        Log.i(TAG, "alarm channels ready")
    }

    /** The default alarm tone, falling back to the ringer, then to null. */
    private fun alarmSound(): android.net.Uri? =
        RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)

    private fun canBypassDnd(context: Context): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return false
        val notificationManager = context.getSystemService(NotificationManager::class.java)
        return notificationManager?.isNotificationPolicyAccessGranted == true
    }

    /**
     * Build the alarm notification for one event.
     *
     * [AlertActivity] is the full-screen intent target, so it is what actually
     * appears; this notification is the fallback for when the full-screen
     * permission is refused, and it is what makes the alert visible in the shade
     * either way.
     */
    fun buildAlertNotification(context: Context, eventId: String, confidence: Int): Notification {
        val fullScreenIntent = PendingIntent.getActivity(
            context,
            eventId.hashCode(),
            AlertActivity.intent(context, eventId),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val contentIntent = PendingIntent.getActivity(
            context,
            eventId.hashCode() + 1,
            AlertActivity.intent(context, eventId),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        return NotificationCompat.Builder(context, ALARM_ID)
            .setSmallIcon(R.drawable.ic_shield_alarm)
            .setContentTitle(context.getString(R.string.alert_title_unknown))
            .setContentText("Confidence $confidence%. Tap to answer.")
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setCategory(NotificationCompat.CATEGORY_ALARM)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setOngoing(true)
            // Not auto-cancelled: the alert owns its own dismissal, so that
            // swiping the notification cannot leave the alarm sounding.
            .setAutoCancel(false)
            .setFullScreenIntent(fullScreenIntent, true)
            .setContentIntent(contentIntent)
            .setWhen(System.currentTimeMillis())
            .setShowWhen(true)
            .setColor(context.getColor(R.color.shield_alarm_red))
            .setColorized(false)
            .setCategory(NotificationCompat.CATEGORY_ALARM)
            .build()
    }

    /** A quiet note on the status channel. */
    fun buildStatusNotification(context: Context, title: String, body: String): Notification =
        NotificationCompat.Builder(context, STATUS_ID)
            .setSmallIcon(R.drawable.ic_shield_alarm)
            .setContentTitle(title)
            .setContentText(body)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setAutoCancel(true)
            .build()

    /** Post the alert. Idempotent per event: re-posting updates rather than stacks. */
    fun postAlert(context: Context, eventId: String, confidence: Int) {
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        manager.notify(NOTIFICATION_ID_ALERT, buildAlertNotification(context, eventId, confidence))
    }

    /** Stop the alarm notification. Called when the alert screen is answered or closed. */
    fun cancelAlert(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        manager.cancel(NOTIFICATION_ID_ALERT)
    }
}

/** Exposed for the settings screen, which needs to know whether to prompt. */
object DndPolicy {
    fun hasAccess(context: Context): Boolean =
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q &&
            context.getSystemService(NotificationManager::class.java)
                ?.isNotificationPolicyAccessGranted == true
}

/** The tone the alarm channel is configured with. Shown on the settings screen so
 *  the owner can tell whether an alarm that failed to play was a silent phone
 *  rather than a broken channel. */
val alarmToneUri: android.net.Uri?
    get() = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)