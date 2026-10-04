package com.immanuelc0d5s.shield.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.immanuelc0d5s.shield.alarm.AlarmPlayer
import com.immanuelc0d5s.shield.ui.theme.ShieldAlertColors

/**
 * P0 #5: arm/disarm, "Send test alert", and the permission fixes.
 *
 * The permission rows are here as well as in the first-run gate. First-run only
 * catches the permissions missing at install; a user who declined and dismissed
 * the gate needs somewhere to come back to.
 */
@Composable
fun SettingsScreen(
    armed: Boolean,
    sendingTest: Boolean,
    statusMessage: String?,
    localEventCount: Int,
    onArmedChange: (Boolean) -> Unit,
    onSendTestAlert: () -> Unit,
    onRefresh: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    val permissions = readPermissionState(context)

    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("Settings", style = MaterialTheme.typography.headlineMedium)

        // -- arm state ----------------------------------------------------
        Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(16.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Column(modifier = Modifier.weight(1f)) {
                    Text("System armed", style = MaterialTheme.typography.titleLarge)
                    Text(
                        "When disarmed, the sensor replies clear and nothing is raised. " +
                            "The sensor node always reports regardless - arming is decided here.",
                        style = MaterialTheme.typography.bodyLarge,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                Switch(checked = armed, onCheckedChange = onArmedChange)
            }
        }

        if (!armed) {
            Card(
                colors = CardDefaults.cardColors(
                    containerColor = ShieldAlertColors.lowConfidence.copy(alpha = 0.18f),
                ),
            ) {
                Text(
                    "SHIELD is disarmed. Nothing will alert you until you arm it again.",
                    style = MaterialTheme.typography.bodyLarge,
                    modifier = Modifier.padding(16.dp),
                    color = ShieldAlertColors.lowConfidence,
                )
            }
        }

        // -- test alert ---------------------------------------------------
        Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                Text("Send test alert", style = MaterialTheme.typography.titleLarge)
                Text(
                    "Inserts a status 2 event, which makes the RPi's whole push path " +
                        "run: database webhook, Edge Function, FCM, alarm on this phone. " +
                        "The only honest end-to-end check is a real insert.",
                    style = MaterialTheme.typography.bodyLarge,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Button(
                    onClick = onSendTestAlert,
                    enabled = !sendingTest,
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MaterialTheme.colorScheme.primary,
                    ),
                ) {
                    Text(if (sendingTest) "Sending…" else "Send")
                }
                statusMessage?.let {
                    Text(it, style = MaterialTheme.typography.bodyLarge)
                }
            }
        }

        // -- permissions --------------------------------------------------
        PermissionCard(
            title = "Notifications",
            granted = permissions.notificationsGranted,
            why = "Without this the alarm is silent and invisible on Android 13 and newer.",
            onFix = { openAppNotificationSettings(context) },
        )

        PermissionCard(
            title = "Lock-screen alerts",
            granted = permissions.canUseFullScreenIntent,
            why = "Without this the alert appears in the shade but the phone stays locked.",
            onFix = { openFullScreenIntentSettings(context) },
        )

        PermissionCard(
            title = "Ignore battery optimisation",
            granted = permissions.batteryExempt,
            why = "Doze defers push delivery. Exempt this app so alerts are not hours late.",
            onFix = { openBatterySettings(context) },
        )

        PermissionCard(
            title = "Alarm volume",
            granted = AlarmPlayer.alarmVolume(context) > 0,
            why = "The alarm plays on the alarm stream, which is separate from ringer volume. " +
                "A phone with alarm volume at zero produces a completely silent alert.",
            onFix = { openAlarmVolumeSettings(context) },
        )

        // -- diagnostics -------------------------------------------------
        Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Text("Local event log", style = MaterialTheme.typography.titleLarge)
                Text(
                    "$localEventCount events stored on this phone, encrypted with SQLCipher.",
                    style = MaterialTheme.typography.bodyLarge,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                OutlinedButton(onClick = onRefresh) { Text("Refresh from server") }
            }
        }
    }
}

@Composable
private fun PermissionCard(
    title: String,
    granted: Boolean,
    why: String,
    onFix: () -> Unit,
) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(title, style = MaterialTheme.typography.titleLarge)
                Text(
                    if (granted) "Granted" else "Needs attention",
                    style = MaterialTheme.typography.labelLarge,
                    color = if (granted) {
                        ShieldAlertColors.known
                    } else {
                        ShieldAlertColors.lowConfidence
                    },
                )
            }
            Text(
                why,
                style = MaterialTheme.typography.bodyLarge,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            if (!granted) {
                OutlinedButton(onClick = onFix) { Text("Open settings") }
            }
        }
    }
}

private fun openAlarmVolumeSettings(context: android.content.Context) {
    context.startActivity(
        android.content.Intent(android.provider.Settings.ACTION_SOUND_SETTINGS),
    )
}