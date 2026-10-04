package com.immanuelc0d5s.shield.alarm

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.util.Log
import androidx.core.content.getSystemService

/**
 * The part of the alarm that actually makes noise.
 *
 * Separate from the notification channel, and that separation is the whole point.
 * A channel sound loops only when the URI's own metadata carries loop
 * information, which `RingtoneManager.TYPE_ALARM` does not guarantee across OEM
 * builds. The result is an alert that plays once and then sits silent looking
 * delivered. So the notification plays its one sound and this runs an explicitly
 * looping player for as long as the alert is on screen.
 *
 * Stream `STREAM_ALARM` rather than `STREAM_RINGTONE`, so the alarm is governed
 * by alarm volume (which the user controls separately and silences when they go
 * to bed) rather than by ringer volume, and is routed by the audio system as an
 * alarm.
 */
class AlarmPlayer(context: Context) {

    private val appContext = context.applicationContext
    private var player: MediaPlayer? = null
    private var vibrator: Vibrator? = null

    private var running = false

    /** True once [start] has completed without throwing. */
    var isPlaying: Boolean = false
        private set

    /**
     * Begin sound and vibration. Idempotent.
     *
     * Every failure is logged and swallowed. An alert screen that crashes
     * because the audio stack is unhappy is strictly worse than a silent one: the
     * user would not see the snapshot or the three buttons at all, so they could
     * not answer, so the RPi would escalate. A silent alert with a working UI is
     * recoverable.
     */
    fun start() {
        if (running) return
        running = true
        startTone()
        startVibration()
    }

    private fun startTone() {
        val uri = alarmToneUri ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
        if (uri == null) {
            Log.w(TAG, "no alarm tone and no ringer tone; the alert will be silent")
            return
        }

        runCatching {
            player = MediaPlayer().apply {
                setDataSource(appContext, uri)
                setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build()
                )
                // Explicit. See the class docstring.
                isLooping = true
                prepare()
                start()
            }
            isPlaying = true
            Log.i(TAG, "alarm tone looping")
        }.onFailure {
            Log.e(TAG, "could not start the alarm tone", it)
            releasePlayer()
        }
    }

    private fun startVibration() {
        val service = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            appContext.getSystemService<VibratorManager>()?.defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            appContext.getSystemService<Vibrator>()
        }
        if (service == null || !service.hasVibrator()) {
            Log.w(TAG, "no vibrator; the alert will be silent only in the hand")
            return
        }

        vibrator = service
        runCatching {
            // 0.5 s on, 0.5 s off, repeat, forever. The gap is what makes this
            // read as an alarm rather than a notification buzz.
            val pattern = longArrayOf(0, 500, 500)
            service.vibrate(
                VibrationEffect.createWaveform(pattern, 0),
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build(),
            )
        }.onFailure { Log.w(TAG, "could not start vibration", it) }
    }

    /**
     * Stop everything and release the native player.
     *
     * Must be called on every dismissal path. A leaked looping MediaPlayer keeps
     * the alarm audible with no UI attached, which is the worst state this app
     * can be left in.
     */
    fun stop() {
        if (!running) return
        running = false
        isPlaying = false

        vibrator?.runCatching { cancel() }
        vibrator = null
        releasePlayer()
        Log.i(TAG, "alarm stopped")
    }

    private fun releasePlayer() {
        player?.runCatching {
            if (isPlaying) stop()
            release()
        }
        player = null
    }

    /** Exposed for the alert screen's "test sound" affordance and for tests. */
    fun isRunning(): Boolean = running

    companion object {
        private const val TAG = "AlarmPlayer"

        /**
         * Whether the device has a working alarm stream at all.
         *
         * Worth checking on the settings screen: a phone with alarm volume at
         * zero produces a completely silent alert that looks like a broken app.
         */
        fun alarmVolume(context: Context): Int {
            val manager = context.getSystemService<AudioManager>() ?: return -1
            return manager.getStreamVolume(AudioManager.STREAM_ALARM)
        }
    }
}