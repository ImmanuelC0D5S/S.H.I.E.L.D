package com.immanuelc0d5s.shield.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import com.immanuelc0d5s.shield.data.Verdict
import com.immanuelc0d5s.shield.ui.theme.ShieldAlertColors
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/** State for [AlertScreen]. Mirrors `AlertViewModel.state`. */
data class AlertUiState(
    val eventId: String = "",
    val ts: Long = 0,
    val confidence: Int = 0,
    val imageUrl: String = "",
    val verdict: Verdict? = null,
    val loading: Boolean = false,
    val submitting: Boolean = false,
    val error: String? = null,
)

/**
 * P0 #3: snapshot, time, confidence, Known / Intruder / Dismiss.
 *
 * Layout priority is deliberate: the snapshot is the largest element, the three
 * buttons are the tallest touch targets Material allows, and everything else is
 * secondary. Someone woken at 3am has to answer this with one thumb while
 * persuaded that the person on screen is or is not a stranger.
 *
 * Dismiss is visually de-emphasised *and* last. It is the button most likely to
 * be pressed by accident by someone who is not reading, and an accidental
 * dismiss loses the answer the RPi is waiting for.
 */
@Composable
fun AlertScreen(
    state: AlertUiState,
    onKnown: () -> Unit,
    onIntruder: () -> Unit,
    onDismiss: () -> Unit,
) {
    val busy = state.submitting

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .systemBarsPadding()
            .padding(horizontal = 20.dp, vertical = 16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Header(confidence = state.confidence)

        Box(
            modifier = Modifier
                .fillMaxWidth()
                .weight(1f)
                .clip(RoundedCornerShape(16.dp))
                .background(MaterialTheme.colorScheme.surface),
            contentAlignment = Alignment.Center,
        ) {
            when {
                state.imageUrl.isNotBlank() -> AsyncImage(
                    model = state.imageUrl,
                    // A signed URL, so no auth header is attached and none is
                    // needed: the signature in the query string is the credential.
                    contentDescription = "Camera snapshot at the time of the alert",
                    contentScale = ContentScale.Fit,
                    modifier = Modifier.fillMaxSize(),
                )

                state.loading -> CircularProgressIndicator(color = MaterialTheme.colorScheme.primary)

                else -> MissingSnapshot()
            }
        }

        state.error?.let { message ->
            Text(
                message,
                style = MaterialTheme.typography.bodyLarge,
                color = ShieldAlertColors.lowConfidence,
                textAlign = TextAlign.Center,
                modifier = Modifier.fillMaxWidth(),
            )
        }

        VerdictButtons(
            busy = busy,
            answered = state.verdict,
            onKnown = onKnown,
            onIntruder = onIntruder,
            onDismiss = onDismiss,
        )
    }
}

@Composable
private fun Header(confidence: Int) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(
            "UNKNOWN PERSON DETECTED",
            style = MaterialTheme.typography.displaySmall,
            color = ShieldAlertColors.intruder,
        )
        Text(
            "Confidence $confidence% that this is nobody you enrolled",
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(
            formatUtc(),
            style = MaterialTheme.typography.labelLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

/**
 * UTC, and labelled as such.
 *
 * An intrusion alert is meaningless without its timestamp, and a local-time
 * string on a phone in the wrong timezone is worse than none: it reads as a
 * different hour of the same day. UTC plus an explicit "UTC" suffix removes the
 * ambiguity at the cost of requiring one mental conversion.
 */
private fun formatUtc(): String {
    val format = SimpleDateFormat("yyyy-MM-dd HH:mm:ss 'UTC'", Locale.US)
    format.timeZone = TimeZone.getTimeZone("UTC")
    return format.format(Date())
}

@Composable
private fun MissingSnapshot() {
    Column(
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(8.dp),
        modifier = Modifier.padding(24.dp),
    ) {
        Text(
            "No snapshot available",
            style = MaterialTheme.typography.titleLarge,
            textAlign = TextAlign.Center,
        )
        Text(
            "Answer from the time and confidence above, or open History for detail.",
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
    }
}

@Composable
private fun VerdictButtons(
    busy: Boolean,
    answered: Verdict?,
    onKnown: () -> Unit,
    onIntruder: () -> Unit,
    onDismiss: () -> Unit,
) {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Button(
            onClick = onIntruder,
            enabled = !busy,
            colors = ButtonDefaults.buttonColors(
                containerColor = ShieldAlertColors.intruder,
                contentColor = Color.White,
            ),
            modifier = Modifier
                .fillMaxWidth()
                .height(72.dp),
        ) {
            Text(
                if (answered == Verdict.INTRUDER) "Intruder — recorded" else "INTRUDER",
                style = MaterialTheme.typography.labelLarge,
            )
        }

        Button(
            onClick = onKnown,
            enabled = !busy,
            colors = ButtonDefaults.buttonColors(
                containerColor = ShieldAlertColors.known,
                contentColor = Color.Black,
            ),
            modifier = Modifier
                .fillMaxWidth()
                .height(72.dp),
        ) {
            Text(
                if (answered == Verdict.KNOWN) "Known — recorded" else "KNOWN",
                style = MaterialTheme.typography.labelLarge,
            )
        }

        OutlinedButton(
            onClick = onDismiss,
            enabled = !busy,
            modifier = Modifier
                .fillMaxWidth()
                .height(56.dp),
        ) {
            Text(
                "Dismiss without answering",
                style = MaterialTheme.typography.bodyLarge,
                color = ShieldAlertColors.dismiss,
            )
        }

        Spacer(modifier = Modifier.height(4.dp))
        Text(
            "Dismissing does not stand the alarm down. It keeps sounding until you " +
                "answer or it times out.",
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
            modifier = Modifier.fillMaxWidth(),
        )
    }
}
