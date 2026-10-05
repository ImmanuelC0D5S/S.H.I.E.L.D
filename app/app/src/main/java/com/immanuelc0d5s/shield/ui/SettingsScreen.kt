package com.immanuelc0d5s.shield.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.immanuelc0d5s.shield.alarm.AlarmPlayer
import com.immanuelc0d5s.shield.ui.theme.ShieldAlertColors
import com.immanuelc0d5s.shield.ui.theme.ShieldGlass
import com.immanuelc0d5s.shield.ui.theme.ShieldSpacing

/**
 * P0 #5: arm/disarm and the permission fixes.
 *
 * The permission rows are here as well as in the first-run gate. First-run only
 * catches the permissions missing at install; a user who declined and dismissed
 * the gate needs somewhere to come back to.
 *
 * `statusMessage` reports the arm/disarm result. It is not the test-alert status
 * it used to share a card with.
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

    Box(
        modifier = modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background),
    ) {
        AmbientBackground()

        Column(
            modifier = Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = ShieldSpacing.screen)
                .padding(top = ShieldSpacing.xl, bottom = ShieldSpacing.xxl),
            verticalArrangement = Arrangement.spacedBy(ShieldSpacing.lg),
        ) {
            Text("Settings", style = MaterialTheme.typography.headlineMedium)

            // -- arm state ----------------------------------------------------
            GlassCard {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(ShieldSpacing.lg),
                ) {
                    Column(modifier = Modifier.weight(1f)) {
                        Text("System armed", style = MaterialTheme.typography.titleLarge)
                        Text(
                            "When disarmed, the sensor replies clear and nothing is raised. " +
                                "The sensor node always reports regardless - arming is decided here.",
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    Switch(
                        checked = armed,
                        onCheckedChange = onArmedChange,
                        colors = SwitchDefaults.colors(
                            checkedThumbColor = Color(0xFF04180A),
                            checkedTrackColor = ShieldAlertColors.known,
                            uncheckedThumbColor = Color.White.copy(alpha = 0.7f),
                            uncheckedTrackColor = Color.White.copy(alpha = 0.10f),
                            uncheckedBorderColor = ShieldGlass.borderStrong,
                        ),
                    )
                }

                // Arm/disarm feedback. This used to live in the test-alert card; it is
                // the only status the app can report about its own writes, so it
                // belongs with the switch that causes it.
                statusMessage?.let {
                    Text(
                        it,
                        style = MaterialTheme.typography.bodyMedium,
                        color = if (armed) ShieldAlertColors.known else ShieldAlertColors.lowConfidence,
                    )
                }
            }

            if (!armed) {
                GlassCard(
                    fill = ShieldAlertColors.lowConfidence.copy(alpha = 0.10f),
                    borderColor = ShieldAlertColors.lowConfidence.copy(alpha = 0.30f),
                    contentPadding = ShieldSpacing.md,
                ) {
                    Text(
                        "SHIELD is disarmed. Nothing will alert you until you arm it again.",
                        style = MaterialTheme.typography.bodyMedium,
                        color = ShieldAlertColors.lowConfidence,
                    )
                }
            }

            SectionLabel("Permissions")

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

            SectionLabel("Testing")

            // There is deliberately no "Send test alert" button here. It used to be
            // P0 #5, and it could never work: it POSTed to /rest/v1/events with the
            // anon key, and migration 20261003090100 does `revoke insert on public.events
            // from anon`. Every press returned
            //   401 42501 permission denied for table events
            //
            // The grant is the correct side of that conflict, not the bug. `anon` is
            // the project's publishable key, which ships inside every APK, so allowing
            // it to insert would let anyone who unzips the app forge intrusion records
            // into the audit trail and spam this phone with pushes. An app that can
            // write its own evidence is not evidence.
            //
            // So the insert is an operator action, from the SQL editor.
            GlassCard {
                Text("Testing the push path", style = MaterialTheme.typography.titleLarge)
                Text(
                    "Test alerts are inserted from the Supabase SQL editor, not from this " +
                        "app. Insert a row with status = 2 into public.events and the whole " +
                        "path runs: database webhook, Edge Function, FCM, and the alarm here.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Text(
                    "This app deliberately cannot insert one. It authenticates as anon, " +
                        "which can read events but not write them, so a compromised or " +
                        "reverse-engineered APK cannot fabricate an intrusion.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            SectionLabel("Storage")

            GlassCard {
                Text("Local event log", style = MaterialTheme.typography.titleLarge)
                Text(
                    "$localEventCount events on this phone, encrypted at rest with SQLCipher. " +
                        "The key is generated on first launch and wrapped by the Android " +
                        "Keystore, so it never exists on disk in plain form.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                ShieldButton(
                    label = "Refresh from server",
                    onClick = onRefresh,
                    containerColor = Color.White.copy(alpha = 0.07f),
                    contentColor = MaterialTheme.colorScheme.onSurface,
                    borderColor = ShieldGlass.borderStrong,
                    height = 48.dp,
                    shape = RoundedCornerShape(14.dp),
                )
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
    GlassCard(contentPadding = ShieldSpacing.md) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(ShieldSpacing.md),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                title,
                style = MaterialTheme.typography.titleMedium,
                modifier = Modifier.weight(1f),
            )
            StatusChip(
                label = if (granted) "Granted" else "Needs attention",
                color = if (granted) ShieldAlertColors.known else ShieldAlertColors.lowConfidence,
            )
        }

        Text(
            why,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        if (!granted) {
            ShieldButton(
                label = "Open settings",
                onClick = onFix,
                containerColor = Color.White.copy(alpha = 0.07f),
                contentColor = MaterialTheme.colorScheme.onSurface,
                borderColor = ShieldGlass.borderStrong,
                height = 48.dp,
                shape = RoundedCornerShape(14.dp),
            )
        }
    }
}

private fun openAlarmVolumeSettings(context: android.content.Context) {
    context.startActivity(
        android.content.Intent(android.provider.Settings.ACTION_SOUND_SETTINGS),
    )
}
