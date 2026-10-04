package com.immanuelc0d5s.shield

import android.util.Log
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.immanuelc0d5s.shield.data.ShieldEvent
import com.immanuelc0d5s.shield.data.ShieldRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class MainUiState(
    val events: List<ShieldEvent> = emptyList(),
    val nowSeconds: Long = System.currentTimeMillis() / 1000,
    /** Defaults to armed. A failure to read the flag must not present a disarmed
     *  system while the RPi is in fact alerting. */
    val armed: Boolean = true,
    val sendingTest: Boolean = false,
    val statusMessage: String? = null,
    val localEventCount: Int = 0,
)

/**
 * State for History and Settings.
 *
 * The event list comes from the SQLCipher database rather than from the network,
 * so the app opens instantly and works with no connectivity. The network fetch on
 * resume is a reconciliation, which is also how the app picks up verdicts written
 * from anywhere else.
 */
class MainViewModel(private val repository: ShieldRepository) : ViewModel() {

    private val _state = MutableStateFlow(MainUiState())
    val state: StateFlow<MainUiState> = _state.asStateFlow()

    init {
        // Collect the local log for the lifetime of the screen.
        viewModelScope.launch {
            repository.events.collect { events ->
                _state.value = _state.value.copy(events = events)
            }
        }
        refresh()
    }

    /**
     * Reconcile with the server and re-read the permission-sensitive state.
     *
     * Safe to call often. Both operations are read-only apart from caching
     * server-known verdicts, and the arm flag is deliberately defaulted to armed
     * on failure.
     */
    fun refresh() {
        viewModelScope.launch {
            _state.value = _state.value.copy(nowSeconds = System.currentTimeMillis() / 1000)

            repository.refreshEvents()
                .onFailure { Log.i(TAG, "event refresh failed, showing the local log: $it") }

            val armed = repository.fetchArmed()
            val localCount = repository.localEventCount()
            _state.value = _state.value.copy(armed = armed, localEventCount = localCount)
        }
    }

    /**
     * P0 #5: arm/disarm.
     *
     * Optimistic: the switch moves immediately and rolls back if the write fails.
     * Making the user wait on a network round trip to flip a safety switch is
     * worse, and rolling back is the honest response to a failure.
     */
    fun setArmed(armed: Boolean) {
        val previous = _state.value.armed
        _state.value = _state.value.copy(armed = armed, statusMessage = null)

        viewModelScope.launch {
            repository.setArmed(armed)
                .onSuccess {
                    _state.value = _state.value.copy(
                        statusMessage = if (armed) "Armed." else "Disarmed. The sensor will reply clear.",
                    )
                }
                .onFailure {
                    _state.value = _state.value.copy(
                        armed = previous,
                        statusMessage = "Could not change the arm state. Check your connection.",
                    )
                }
        }
    }

    /**
     * P0 #5: "Send test alert".
     *
     * Inserts a real `status = 2` row and reports what happened, including the
     * event id so the row can be found in the Supabase editor. A test button that
     * swallows failures is worse than none: it turns "is the push working?" into
     * an unanswerable question.
     */
    fun sendTestAlert() {
        if (_state.value.sendingTest) return
        _state.value = _state.value.copy(sendingTest = true, statusMessage = "Sending…")

        viewModelScope.launch {
            repository.insertTestAlert(deviceId = "app-test")
                .onSuccess { eventId ->
                    _state.value = _state.value.copy(
                        sendingTest = false,
                        statusMessage = "Inserted $eventId. The alarm should arrive in a few seconds.",
                    )
                    refresh()
                }
                .onFailure { error ->
                    Log.e(TAG, "test alert failed", error)
                    _state.value = _state.value.copy(
                        sendingTest = false,
                        statusMessage = "Could not insert the test event: ${error.message}",
                    )
                }
        }
    }

    companion object {
        private const val TAG = "MainViewModel"

        fun factory(repository: ShieldRepository) = object : ViewModelProvider.Factory {
            @Suppress("UNCHECKED_CAST")
            override fun <T : ViewModel> create(modelClass: Class<T>): T =
                MainViewModel(repository) as T
        }
    }
}