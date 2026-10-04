package com.immanuelc0d5s.shield.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

/**
 * Dark-only, on purpose.
 *
 * This app is read at 3am by someone who has just been woken by it, in a hallway
 * with the lights off. A light theme would be actively hostile there, and a
 * theme that followed the system would switch to light in a sunlit room and then
 * the user enables dark mode on the phone and suddenly cannot read the snapshot.
 * The choice is not negotiable at the design level.
 */
private val ShieldColors = darkColorScheme(
    primary = Color(0xFFFF3B30),
    onPrimary = Color(0xFFFFFFFF),
    secondary = Color(0xFF34C759),
    onSecondary = Color(0xFF06210C),
    tertiary = Color(0xFFFF9F0A),
    background = Color(0xFF0B0F14),
    onBackground = Color(0xFFF5F7FA),
    surface = Color(0xFF141A22),
    onSurface = Color(0xFFF5F7FA),
    surfaceVariant = Color(0xFF1D252F),
    onSurfaceVariant = Color(0xFF9AA5B1),
    outline = Color(0xFF3A4553),
    error = Color(0xFFFF453A),
)

/**
 * Larger base sizes than stock Material.
 *
 * The alert screen is used one-handed, in the dark, by someone whose pulse is
 * high. 14sp body text on a phone held at arm's length is not readable in those
 * conditions, and the three buttons are the only way to stop the noise.
 */
private val ShieldTypography = Typography(
    displaySmall = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.Bold,
        fontSize = 36.sp,
        lineHeight = 42.sp,
    ),
    headlineMedium = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.Bold,
        fontSize = 26.sp,
        lineHeight = 32.sp,
    ),
    titleLarge = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.SemiBold,
        fontSize = 20.sp,
        lineHeight = 26.sp,
    ),
    bodyLarge = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.Normal,
        fontSize = 17.sp,
        lineHeight = 24.sp,
    ),
    labelLarge = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.SemiBold,
        fontSize = 17.sp,
        letterSpacing = 0.4.sp,
    ),
)

@Composable
fun ShieldTheme(
    @Suppress("UNUSED_PARAMETER") darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    // `darkTheme` is accepted and ignored. See the file comment: the scheme is
    // fixed because the use case fixes it.
    MaterialTheme(
        colorScheme = ShieldColors,
        typography = ShieldTypography,
        content = content,
    )
}

/** Semantic colours used by the alert screen, outside the Material roles. */
object ShieldAlertColors {
    val intruder = Color(0xFFFF3B30)
    val known = Color(0xFF34C759)
    val dismiss = Color(0xFF9AA5B1)
    val lowConfidence = Color(0xFFFF9F0A)
}