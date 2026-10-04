package com.immanuelc0d5s.shield.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import com.immanuelc0d5s.shield.data.AlertPayload
import com.immanuelc0d5s.shield.data.ShieldEvent
import com.immanuelc0d5s.shield.data.Verdict
import com.immanuelc0d5s.shield.ui.theme.ShieldAlertColors

/**
 * P0 #4: the event log, newest first, with a thumbnail per row.
 *
 * Reads from the SQLCipher database via a Flow rather than fetching on every
 * resume, so it renders immediately and offline. The refresh on resume is a
 * reconciliation, not the primary source - see `MainViewModel`.
 */
@Composable
fun HistoryScreen(
    events: List<ShieldEvent>,
    nowSeconds: Long,
    modifier: Modifier = Modifier,
) {
    if (events.isEmpty()) {
        Box(modifier = modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Text(
                "No events yet.",
                style = MaterialTheme.typography.bodyLarge,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        return
    }

    LazyColumn(
        modifier = modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        items(events, key = { it.id }) { event ->
            EventRow(event = event, nowSeconds = nowSeconds)
        }
    }
}

@Composable
private fun EventRow(event: ShieldEvent, nowSeconds: Long) {
    Card(
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Row(
            modifier = Modifier.padding(12.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Thumbnail(event)

            Column(
                modifier = Modifier.weight(1f),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                Text(titleFor(event), style = MaterialTheme.typography.titleLarge)
                Text(
                    "${formatEventTs(event.ts)} · ${relativeAge(event.ageSeconds(nowSeconds))}",
                    style = MaterialTheme.typography.bodyLarge,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                VerdictChip(event.verdict)
            }
        }
    }
}

@Composable
private fun Thumbnail(event: ShieldEvent) {
    val hasImage = event.imagePath.isNotBlank() && !event.id.startsWith("test-")

    Box(
        modifier = Modifier
            .size(72.dp)
            .clip(RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant),
        contentAlignment = Alignment.Center,
    ) {
        if (hasImage) {
            AsyncImage(
                model = event.imagePath,
                contentDescription = null,
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize(),
            )
        } else {
            Text(
                if (event.id.startsWith("test-")) "TEST" else "—",
                style = MaterialTheme.typography.labelLarge,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

@Composable
private fun VerdictChip(verdict: Verdict?) {
    val (label, colour) = when (verdict) {
        Verdict.KNOWN -> "Marked known" to ShieldAlertColors.known
        Verdict.INTRUDER -> "Marked intruder" to ShieldAlertColors.intruder
        null -> "Awaiting your answer" to ShieldAlertColors.lowConfidence
    }
    Text(label, style = MaterialTheme.typography.bodyLarge, color = colour)
}

private fun titleFor(event: ShieldEvent): String = when {
    event.id.startsWith("test-") -> "Test alert"
    event.status == AlertPayload.STATUS_KNOWN ->
        "Known: ${event.identity ?: "enrolled"}"
    event.status == AlertPayload.STATUS_CLEAR -> "Clear"
    else -> "Unknown person"
}

private fun formatEventTs(epochSeconds: Long): String {
    val format = java.text.SimpleDateFormat("yyyy-MM-dd HH:mm:ss", java.util.Locale.US)
    format.timeZone = java.util.TimeZone.getDefault()
    return format.format(java.util.Date(epochSeconds * 1000))
}

/**
 * Human age.
 *
 * Deliberately not a timestamp alone: "14 minutes ago" is what tells the owner
 * whether they are looking at the intruder they just heard about or something
 * from this morning.
 */
private fun relativeAge(seconds: Long): String = when {
    seconds < 60 -> "just now"
    seconds < 3600 -> "${seconds / 60} min ago"
    seconds < 86_400 -> "${seconds / 3600} h ago"
    else -> "${seconds / 86_400} d ago"
}
