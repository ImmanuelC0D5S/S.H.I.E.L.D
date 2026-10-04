package com.immanuelc0d5s.shield.alarm

import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.compose.runtime.getValue
import androidx.lifecycle.ViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.viewModelScope
import com.immanuelc0d5s.shield.data.ShieldRepository
import com.immanuelc0d5s.shield.data.Verdict
import com.immanuelc0d5s.shield.ui.AlertScreen
import com.immanuelc0d5s.shield.ui.AlertUiState
import com.immanuelc0d5s.shield.ui.theme.ShieldTheme
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * P0 #2/P0 #3: the full-screen alert. Snapshot, time, confidence, and the three
 * buttons.
 *
 * This Activity is the full-screen-intent target. It owns the looping
 * [AlarmPlayer], which is why it must be torn down on every exit path -
 * `onStop` included, because a screen that is covered by the lock screen still
 * counts as stopped and still needs the alarm to continue, but an Activity that
 * is finishing must not leave a looping player with no UI behind it.
 */
class AlertActivity : ComponentActivity() {

    private var player: AlarmPlayer? = null

    private val viewModel: AlertViewModel by viewModels {
        AlertViewModel.factory(ShieldRepository.get(applicationContext))
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        AlarmChannels.ensure(this)
        val eventId = intent?.getStringExtra(EXTRA_EVENT_ID).orEmpty()

        if (eventId.isBlank()) {
            // A full-screen intent can be replayed by the system after a reboot.
            // Arriving here with nothing to show means finish immediately rather
            // than present an empty alert the user has to dismiss.
            Log.w(TAG, "started with no event id; finishing")
            finish()
            return
        }

        // Keep the display on while the alert is answered. Set as a window flag
        // rather than a theme attribute because no such theme attribute exists,
        // and released automatically when this window goes away.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        viewModel.load(eventId)

        player = AlarmPlayer(this).also { it.start() }

        setContent {
            val state by viewModel.state.collectAsStateWithLifecycle()
            ShieldTheme {
                AlertScreen(
                    state = state,
                    onKnown = { viewModel.submit(Verdict.KNOWN) },
                    onIntruder = { viewModel.submit(Verdict.INTRUDER) },
                    onDismiss = { viewModel.dismiss() },
                )
            }
        }
    }

    override fun onDestroy() {
        // Every path out of this screen goes through here, so this is the single
        // place that has to guarantee the alarm stops.
        player?.stop()
        player = null
        super.onDestroy()
    }

    companion object {
        private const val TAG = "AlertActivity"
        private const val EXTRA_EVENT_ID = "event_id"

        fun intent(context: Context, eventId: String): Intent =
            Intent(context, AlertActivity::class.java).apply {
                putExtra(EXTRA_EVENT_ID, eventId)
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or
                    Intent.FLAG_ACTIVITY_CLEAR_TOP or
                    Intent.FLAG_ACTIVITY_NO_USER_ACTION
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                    addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
                }
            }
    }
}

/**
 * State for one alert.
 *
 * Split out from the Activity so the same logic can be tested without a window.
 * The important behaviour is what happens on a *failed* verdict write: the local
 * database already has the answer (so the log is correct), but the RPi does not
 * (so it will keep escalating), and the UI has to say so rather than pretending
 * it worked.
 */
class AlertViewModel(
    private val repository: ShieldRepository,
    private val onFinished: () -> Unit = {},
) : ViewModel() {

    private val _state = MutableStateFlow(AlertUiState())
    val state: StateFlow<AlertUiState> = _state.asStateFlow()

    fun load(eventId: String) {
        _state.value = _state.value.copy(eventId = eventId, loading = true)

        viewModelScope.launch {
            // Prefer what the push already gave us so the snapshot appears without
            // waiting on the network; the screen is already on the lock screen and
            // must not sit blank.
            val cached = repository.eventById(eventId)
            if (cached != null) {
                _state.value = _state.value.copy(
                    eventId = eventId,
                    imageUrl = cached.imagePath,
                    ts = cached.ts,
                    confidence = cached.confidence,
                    loading = false,
                )
            }

            // Then reconcile with the server, which is the authority on the event.
            repository.refreshEvents(limit = 10)
                .onSuccess { events ->
                    val fresh = events.firstOrNull { it.id == eventId } ?: return@onSuccess
                    _state.value = _state.value.copy(
                        imageUrl = fresh.imagePath.ifBlank { _state.value.imageUrl },
                        ts = fresh.ts,
                        confidence = fresh.confidence,
                        verdict = fresh.verdict,
                        loading = false,
                    )
                }
                .onFailure {
                    Log.w(TAG, "could not refresh the alert; showing the cached copy")
                    _state.value = _state.value.copy(loading = false)
                }
        }
    }

    /**
     * Write the user's answer and close. P0 #3.
     *
     * The screen closes even if the remote write fails, because leaving the user
     * staring at an alarm they have already answered is worse, and the local
     * database already holds their answer. The failure is recorded in the state
     * so the history screen can show it and the RPi's own timeout still ends the
     * escalation.
     */
    fun submit(verdict: Verdict) {
        val eventId = _state.value.eventId
        if (eventId.isBlank() || _state.value.submitting) return

        _state.value = _state.value.copy(submitting = true, error = null)

        viewModelScope.launch {
            repository.submitVerdict(eventId, verdict)
                .onSuccess {
                    _state.value = _state.value.copy(verdict = verdict, submitting = false)
                    onFinished()
                }
                .onFailure { error ->
                    Log.e(TAG, "verdict $verdict for $eventId did not reach the server", error)
                    _state.value = _state.value.copy(
                        verdict = verdict,
                        submitting = false,
                        error = "Saved on this phone, but not sent. The alarm will stop on its own.",
                    )
                    onFinished()
                }
        }
    }

    /**
     * Dismiss without answering.
     *
     * Deliberately does *not* write a verdict. Dismissal means "I am not going to
     * answer right now", and guessing `known` on the user's behalf would stand the
     * alarm down. The RPi keeps escalating, which is the correct outcome for an
     * unanswered alert.
     */
    fun dismiss() {
        onFinished()
    }

    companion object {
        private const val TAG = "AlertViewModel"

        fun factory(repository: ShieldRepository) = object : androidx.lifecycle.ViewModelProvider.Factory {
            @Suppress("UNCHECKED_CAST")
            override fun <T : ViewModel> create(modelClass: Class<T>): T =
                AlertViewModel(repository) as T
        }
    }
}