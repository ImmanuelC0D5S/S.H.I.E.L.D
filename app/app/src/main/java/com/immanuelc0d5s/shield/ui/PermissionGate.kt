package com.immanuelc0d5s.shield.ui

import android.Manifest
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp

/**
 * P0 #6: the runtime permissions this app cannot work without.
 *
 * Each is requested because it is genuinely required, and each is explained
 * rather than demanded. On Android 13+ `POST_NOTIFICATIONS` is a hard gate: with
 * it denied, the alarm is silent and invisible, so the app is useless and the
 * user deserves to be told that rather than left wondering.
 *
 * The prompts are deliberately **not** fired from `AlertActivity`. An Activity
 * that appears over the lock screen to shout about an intruder is the worst
 * possible moment to also ask for a battery-exemption dialog; the owner would
 * dismiss it without reading and the exemption would never be granted.
 */
data class PermissionState(
    val notificationsGranted: Boolean,
    val canUseFullScreenIntent: Boolean,
    val batteryExempt: Boolean,
) {
    /** What the app genuinely cannot do without. */
    val isCritical: Boolean get() = !notificationsGranted

    val everythingGranted: Boolean
        get() = notificationsGranted && canUseFullScreenIntent && batteryExempt
}

fun readPermissionState(context: Context): PermissionState = PermissionState(
    notificationsGranted = hasNotificationPermission(context),
    canUseFullScreenIntent = canUseFullScreenIntent(context),
    batteryExempt = isBatteryExempt(context),
)

fun hasNotificationPermission(context: Context): Boolean {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
        // Pre-13 there is no runtime permission; the channel's importance decides
        // whether anything appears.
        return NotificationManagerCompatLite.areEnabled(context)
    }
    return NotificationManagerCompatLite.areEnabled(context)
}

fun canUseFullScreenIntent(context: Context): Boolean {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
        // The permission exists and is granted at install time before API 34.
        return context.getSystemService(android.app.NotificationManager::class.java)
            ?.canUseFullScreenIntent() != false
    }
    return context.getSystemService(NotificationManager::class.java)
        ?.canUseFullScreenIntent() == true
}

fun isBatteryExempt(context: Context): Boolean {
    val power = context.getSystemService(PowerManager::class.java) ?: return true
    return power.isIgnoringBatteryOptimizations(context.packageName)
}

/**
 * Shown on first launch, and reachable from Settings afterwards.
 *
 * Re-checked on every resume rather than cached, because the user leaves to the
 * system settings screen to grant these and comes back. A one-shot check would
 * show the prompts as permanently unfixed.
 */
@Composable
fun PermissionGate(onAllGranted: () -> Unit = {}) {
    val context = LocalContext.current
    var state by remember { mutableStateOf(readPermissionState(context)) }

    val notificationLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        state = readPermissionState(context).copy(notificationsGranted = granted)
    }

    // Re-read on every resume: the user may have granted something in Settings
    // while this was in the back stack.
    LaunchedEffect(Unit) {
        state = readPermissionState(context)
    }

    if (state.everythingGranted) {
        onAllGranted()
        return
    }

    if (state.isCritical && !state.notificationsGranted) {
        // Fire immediately on first launch. On Android 13+ the app cannot do
        // anything at all without this, and a settings-only path here would look
        // like a broken app.
        LaunchedEffect(Unit) {
            notificationLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    Column(
        modifier = Modifier.padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("S.H.I.E.L.D needs a few permissions", style = MaterialTheme.typography.headlineMedium)

        Text(
            "Without them the alarm cannot wake you. Nothing is sent anywhere else " +
                "and no camera access is requested - this app only receives alerts.",
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        PermissionRow(
            title = "Notifications",
            why = "Required. Without it the alarm is silent and invisible.",
            granted = state.notificationsGranted,
        ) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                notificationLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
            } else {
                openAppNotificationSettings(context)
            }
        }

        PermissionRow(
            title = "Lock-screen alerts",
            why = "Required to show the alert over the lock screen.",
            granted = state.canUseFullScreenIntent,
        ) { openFullScreenIntentSettings(context) }

        PermissionRow(
            title = "Ignore battery optimisation",
            why = "Doze delays push delivery. Exempt this app so alerts arrive promptly.",
            granted = state.batteryExempt,
        ) { openBatterySettings(context) }
    }
}

@Composable
private fun PermissionRow(
    title: String,
    why: String,
    granted: Boolean,
    onFix: () -> Unit,
) {
    Card(
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(title, style = MaterialTheme.typography.titleLarge)
            Text(
                why,
                style = MaterialTheme.typography.bodyLarge,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            if (granted) {
                Text(
                    "Granted",
                    style = MaterialTheme.typography.labelLarge,
                    color = MaterialTheme.colorScheme.secondary,
                )
            } else {
                Button(
                    onClick = onFix,
                    colors = ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.primary),
                ) { Text("Grant") }
            }
        }
    }
}

// -- deep links ---------------------------------------------------------------

fun openFullScreenIntentSettings(context: Context) {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.UPSIDE_DOWN_CAKE) return
    val perApp = Intent(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT).apply {
        data = Uri.parse("package:${context.packageName}")
    }
    // Fall back to the global list: several OEM builds ship without the per-app
    // screen even on API 34, and throwing here would leave the row dead with no
    // way to grant the permission.
    runCatching { context.startActivity(perApp) }
        .onFailure {
            context.startActivity(
                Intent(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT),
            )
        }
}

fun openBatterySettings(context: Context) {
    val direct = Intent(
        Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
        Uri.parse("package:${context.packageName}"),
    )
    runCatching { context.startActivity(direct) }
        .onFailure {
            context.startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
}

fun openAppNotificationSettings(context: Context) {
    context.startActivity(
        Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
            .putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName),
    )
}

/**
 * Thin wrapper.
 *
 * `NotificationManagerCompat.areNotificationsEnabled()` would need
 * `androidx.core`, which is already a dependency, so this exists only to keep the
 * single call site in one place.
 */
private object NotificationManagerCompatLite {
    fun areEnabled(context: Context): Boolean =
        androidx.core.app.NotificationManagerCompat.from(context).areNotificationsEnabled()
}