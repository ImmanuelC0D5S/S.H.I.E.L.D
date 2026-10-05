package com.immanuelc0d5s.shield.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
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
import com.immanuelc0d5s.shield.ui.theme.ShieldGlass
import com.immanuelc0d5s.shield.ui.theme.ShieldSpacing
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
 * Layout priority is deliberate and unchanged by the restyle: the snapshot is the
 * largest element, the three buttons are the tallest touch targets Material
 * allows, and everything else is secondary. Someone woken at 3am has to answer
 * this with one thumb while persuaded that the person on screen is or is not a
 * stranger. None of the sizes here were reduced to make room for the glass.
 *
 * Dismiss stays visually de-emphasised *and* last. It is the button most likely
 * to be pressed by accident by someone who is not reading, and an accidental
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

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background),
    ) {
        AmbientBackground()

        Column(
            modifier = Modifier
                .fillMaxSize()
                .systemBarsPadding()
                .padding(
                    horizontal = ShieldSpacing.screen,
                    vertical = ShieldSpacing.lg,
                ),
            verticalArrangement = Arrangement.spacedBy(ShieldSpacing.lg),
        ) {
            Header(confidence = state.confidence)

            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .weight(1f),
                contentAlignment = Alignment.Center,
            ) {
                GlassSurface(
                    modifier = Modifier.fillMaxSize(),
                    shape = RoundedCornerShape(22.dp),
                    fill = ShieldGlass.fillStrong,
                    borderColor = ShieldGlass.borderStrong,
                ) {
                    when {
                        state.imageUrl.isNotBlank() -> AsyncImage(
                            model = state.imageUrl,
                            // A signed URL, so no auth header is attached and none is
                            // needed: the signature in the query string is the credential.
                            contentDescription = "Camera snapshot at the time of the alert",
                            contentScale = ContentScale.Fit,
                            modifier = Modifier
                                .fillMaxSize()
                                .clip(RoundedCornerShape(22.dp)),
                        )

                        state.loading -> CircularProgressIndicator(
                            color = MaterialTheme.colorScheme.primary,
                        )

                        else -> MissingSnapshot()
                    }
                }
            }

            state.error?.let { message ->
                GlassSurface(
                    modifier = Modifier.fillMaxWidth(),
                    shape = RoundedCornerShape(14.dp),
                    fill = ShieldAlertColors.lowConfidence.copy(alpha = 0.12f),
                    borderColor = ShieldAlertColors.lowConfidence.copy(alpha = 0.34f),
                ) {
                    Text(
                        message,
                        style = MaterialTheme.typography.bodyMedium,
                        color = ShieldAlertColors.lowConfidence,
                        textAlign = TextAlign.Center,
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(ShieldSpacing.md),
                    )
                }
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
}

@Composable
private fun Header(confidence: Int) {
    Column(verticalArrangement = Arrangement.spacedBy(ShieldSpacing.xs)) {
        Text(
            "UNKNOWN PERSON DETECTED",
            style = MaterialTheme.typography.displaySmall,
            color = ShieldAlertColors.intruderSoft,
        )
        Text(
            "Confidence $confidence% that this is nobody you enrolled",
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(
            formatUtc(),
            style = MaterialTheme.typography.labelMedium,
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
        verticalArrangement = Arrangement.spacedBy(ShieldSpacing.sm),
        modifier = Modifier.padding(ShieldSpacing.xl),
    ) {
        Box(
            modifier = Modifier
                .size(44.dp)
                .clip(RoundedCornerShape(999.dp))
                .background(Color.White.copy(alpha = 0.06f)),
            contentAlignment = Alignment.Center,
        ) {
            Text("—", style = MaterialTheme.typography.titleLarge, color = Color.White.copy(alpha = 0.4f))
        }
        Text(
            "No snapshot available",
            style = MaterialTheme.typography.titleLarge,
            textAlign = TextAlign.Center,
        )
        Text(
            "Answer from the time and confidence above, or open History for detail.",
            style = MaterialTheme.typography.bodyMedium,
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
    Column(verticalArrangement = Arrangement.spacedBy(ShieldSpacing.md)) {
        ShieldButton(
            label = if (answered == Verdict.INTRUDER) "Intruder — recorded" else "INTRUDER",
            onClick = onIntruder,
            enabled = !busy,
            containerColor = ShieldAlertColors.intruder,
            contentColor = Color.White,
            height = 72.dp,
            shape = RoundedCornerShape(18.dp),
        )

        ShieldButton(
            label = if (answered == Verdict.KNOWN) "Known — recorded" else "KNOWN",
            onClick = onKnown,
            enabled = !busy,
            containerColor = ShieldAlertColors.known,
            contentColor = Color(0xFF04180A),
            height = 72.dp,
            shape = RoundedCornerShape(18.dp),
        )

        // Dismiss is a glass outline, not a filled button. It must be reachable in
        // under a thumb without looking like an answer.
        ShieldButton(
            label = "Dismiss without answering",
            onClick = onDismiss,
            enabled = !busy,
            containerColor = Color.White.copy(alpha = 0.06f),
            contentColor = ShieldAlertColors.dismiss,
            borderColor = ShieldGlass.borderStrong,
            height = 56.dp,
            shape = RoundedCornerShape(16.dp),
        )

        Text(
            "Dismissing does not stand the alarm down. It keeps sounding until you " +
                "answer or it times out.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
            modifier = Modifier
                .fillMaxWidth()
                .padding(top = ShieldSpacing.xs),
        )
    }
}
