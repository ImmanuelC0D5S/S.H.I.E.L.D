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
import com.immanuelc0d5s.shield.data.AlertPayload
import com.immanuelc0d5s.shield.data.ShieldEvent
import com.immanuelc0d5s.shield.data.Verdict
import com.immanuelc0d5s.shield.ui.theme.ShieldAlertColors
import com.immanuelc0d5s.shield.ui.theme.ShieldGlass
import com.immanuelc0d5s.shield.ui.theme.ShieldSpacing

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
    Box(modifier = modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)) {
        AmbientBackground()

        if (events.isEmpty()) {
            EmptyState()
            return@Box
        }

        LazyColumn(
            modifier = Modifier.fillMaxSize(),
            contentPadding = PaddingValues(
                start = ShieldSpacing.screen,
                end = ShieldSpacing.screen,
                top = ShieldSpacing.lg,
                bottom = ShieldSpacing.xxl,
            ),
            verticalArrangement = Arrangement.spacedBy(ShieldSpacing.md),
        ) {
            item { SectionLabel("Recent activity") }
            items(events, key = { it.id }) { event ->
                EventRow(event = event, nowSeconds = nowSeconds)
            }
        }
    }
}

@Composable
private fun EmptyState() {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(ShieldSpacing.screen),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Box(
            modifier = Modifier
                .size(72.dp)
                .clip(RoundedCornerShape(999.dp))
                .background(Color.White.copy(alpha = 0.05f)),
            contentAlignment = Alignment.Center,
        ) {
            Box(
                modifier = Modifier
                    .size(26.dp)
                    .clip(RoundedCornerShape(999.dp))
                    .background(ShieldAlertColors.known.copy(alpha = 0.5f)),
            )
        }
        Text(
            "No events yet",
            style = MaterialTheme.typography.titleLarge,
            modifier = Modifier.padding(top = ShieldSpacing.lg),
        )
        Text(
            "Nothing has been detected since the log was last cleared.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
            modifier = Modifier.padding(top = ShieldSpacing.xs),
        )
    }
}

@Composable
private fun EventRow(event: ShieldEvent, nowSeconds: Long) {
    val accent = when (event.verdict) {
        Verdict.KNOWN -> ShieldAlertColors.known
        Verdict.INTRUDER -> ShieldAlertColors.intruder
        null -> ShieldAlertColors.lowConfidence
    }

    GlassCard(contentPadding = ShieldSpacing.md) {
        Row(
            horizontalArrangement = Arrangement.spacedBy(ShieldSpacing.md),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Thumbnail(event)

            Column(
                modifier = Modifier.weight(1f),
                verticalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                Text(
                    titleFor(event),
                    style = MaterialTheme.typography.titleMedium,
                    color = MaterialTheme.colorScheme.onSurface,
                )
                Text(
                    "${formatEventTs(event.ts)} · ${relativeAge(event.ageSeconds(nowSeconds))}",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                StatusChip(label = verdictLabel(event.verdict), color = accent)
            }
        }
    }
}

@Composable
private fun Thumbnail(event: ShieldEvent) {
    val hasImage = event.imagePath.isNotBlank()

    Box(
        modifier = Modifier
            .size(64.dp)
            .clip(RoundedCornerShape(14.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .then(Modifier),
        contentAlignment = Alignment.Center,
    ) {
        if (hasImage) {
            AsyncImage(
                model = event.imagePath,
                contentDescription = null,
                contentScale = ContentScale.Crop,
                modifier = Modifier
                    .fillMaxSize()
                    .clip(RoundedCornerShape(14.dp)),
            )
        } else {
            Text(
                "—",
                style = MaterialTheme.typography.titleMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

private fun verdictLabel(verdict: Verdict?): String = when (verdict) {
    Verdict.KNOWN -> "Marked known"
    Verdict.INTRUDER -> "Marked intruder"
    null -> "Awaiting your answer"
}

private fun titleFor(event: ShieldEvent): String = when {
    event.status == AlertPayload.STATUS_KNOWN -> "Known: ${event.identity ?: "enrolled"}"
    event.status == AlertPayload.STATUS_CLEAR -> "All clear"
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
