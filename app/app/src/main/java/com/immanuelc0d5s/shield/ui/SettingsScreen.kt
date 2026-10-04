package com.immanuelc0d5s.shield.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
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
 * P0 #5: arm/disarm and the permission fixes.
 *
 * The permission rows are here as well as in the first-run gate. First-run only
 * catches the permissions missing at install; a user who declined and dismissed
 * the gate needs somewhere to come back to.
 *
 * `statusMessage` still reports the arm/disarm result. It is not the test-alert
 * status it used to share a card with.
 */
@Composable
fun SettingsScreen(
    armed: Boolean,
    statusMessage: String?,
    localEventCount: Int,
    onArmedChange: (Boolean) -> Unit,
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
            // Arm/disarm feedback. This used to live in the test-alert card; it is
            // the only status the app can report about its own writes, so it
            // belongs with the switch that causes it.
            statusMessage?.let {
                Text(
                    it,
                    style = MaterialTheme.typography.bodyLarge,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(start = 16.dp, end = 16.dp, bottom = 16.dp),
                )
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

        // -- testing -------------------------------------------------------
        //
        // There is deliberately no "Send test alert" button here. It used to be
        // P0 #5, and it could never work: it POSTed to /rest/v1/events with the
        // anon key, and migration 20261003090100 does `revoke insert on public.events
        // from anon`. Every press returned
        //   401 42501 permission denied for table events
        //
        // The grant is the correct side of that conflict, not the bug. `anon` is the
        // project's publishable key, which ships inside every APK, so allowing it to
        // insert would let anyone who unzips the app forge intrusion records into
        // the audit trail and spam this phone with pushes. An app that can write its
        // own evidence is not evidence.
        //
        // So the insert is an operator action, done from the Supabase SQL editor.
        Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Text("Testing the push path", style = MaterialTheme.typography.titleLarge)
                Text(
                    "Test alerts are inserted from the Supabase SQL editor, not from " +
                        "this app. Insert a row with status = 2 into public.events and " +
                        "the whole path runs: database webhook, Edge Function, FCM, " +
                        "and the alarm on this phone.",
                    style = MaterialTheme.typography.bodyLarge,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Text(
                    "This app deliberately cannot insert one. It authenticates as anon, " +
                        "which has read access to events and no write access, so a " +
                        "compromised or reverse-engineered APK cannot fabricate an " +
                        "intrusion.",
                    style = MaterialTheme.typography.bodyLarge,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
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