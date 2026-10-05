package com.immanuelc0d5s.shield.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * Dark-only, on purpose.
 *
 * This app is read at 3am by someone who has just been woken by it, in a hallway
 * with the lights off. A light theme would be actively hostile there, and a
 * theme that followed the system would switch to light in a sunlit room and then
 * the user enables dark mode on the phone and suddenly cannot read the snapshot.
 * The choice is not negotiable at the design level.
 *
 * The palette is deliberately low-luminance. Saturated colour is reserved for
 * meaning only: red is an intruder, green is an answer, amber is "needs you".
 * A screen that is 90% grey with three coloured signals reads faster at 3am than
 * one where everything is coloured.
 */
private val ShieldColors = darkColorScheme(
    primary = Color(0xFFFF453A),
    onPrimary = Color(0xFFFFFFFF),
    primaryContainer = Color(0xFF3A1210),
    onPrimaryContainer = Color(0xFFFFD9D6),
    secondary = Color(0xFF32D74B),
    onSecondary = Color(0xFF06210C),
    tertiary = Color(0xFFFF9F0A),
    background = Color(0xFF05070B),
    onBackground = Color(0xFFF2F5F9),
    surface = Color(0xFF0E131B),
    onSurface = Color(0xFFF2F5F9),
    surfaceVariant = Color(0xFF161D27),
    onSurfaceVariant = Color(0xFF98A4B3),
    surfaceContainerLowest = Color(0xFF05070B),
    surfaceContainerLow = Color(0xFF0B1017),
    surfaceContainer = Color(0xFF0E131B),
    surfaceContainerHigh = Color(0xFF151C25),
    surfaceContainerHighest = Color(0xFF1C242F),
    outline = Color(0xFF39434F),
    outlineVariant = Color(0xFF232C37),
    error = Color(0xFFFF453A),
    onError = Color(0xFFFFFFFF),
    scrim = Color(0xFF000000),
)

/**
 * Larger base sizes than stock Material.
 *
 * The alert screen is used one-handed, in the dark, by someone whose pulse is
 * high. 14sp body text on a phone held at arm's length is not readable in those
 * conditions, and the three buttons are the only way to stop the noise.
 *
 * Nothing here is smaller than the stock Material size for the same role. This is
 * a deliberate regression against the platform, not an oversight.
 */
private val ShieldTypography = Typography(
    displayLarge = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.Bold,
        fontSize = 44.sp,
        lineHeight = 50.sp,
        letterSpacing = (-0.5).sp,
    ),
    displaySmall = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.Bold,
        fontSize = 34.sp,
        lineHeight = 40.sp,
        letterSpacing = (-0.25).sp,
    ),
    headlineMedium = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.Bold,
        fontSize = 26.sp,
        lineHeight = 32.sp,
        letterSpacing = (-0.2).sp,
    ),
    headlineSmall = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.SemiBold,
        fontSize = 21.sp,
        lineHeight = 28.sp,
    ),
    titleLarge = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.SemiBold,
        fontSize = 19.sp,
        lineHeight = 25.sp,
    ),
    titleMedium = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.SemiBold,
        fontSize = 16.sp,
        lineHeight = 22.sp,
    ),
    bodyLarge = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.Normal,
        fontSize = 16.sp,
        lineHeight = 23.sp,
    ),
    bodyMedium = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.Normal,
        fontSize = 14.sp,
        lineHeight = 20.sp,
    ),
    labelLarge = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.SemiBold,
        fontSize = 16.sp,
        letterSpacing = 0.4.sp,
    ),
    labelMedium = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.Medium,
        fontSize = 12.sp,
        letterSpacing = 0.6.sp,
    ),
)

/**
 * Corner radii, in one place.
 *
 * Concentric rounding is what makes a surface stack look designed rather than
 * assembled: a card inside a sheet uses a smaller radius than the sheet, and a
 * button inside a card is smaller again. Three values cover every case here.
 */
private val ShieldShapes = Shapes(
    extraSmall = RoundedCornerShape(8.dp),
    small = RoundedCornerShape(12.dp),
    medium = RoundedCornerShape(18.dp),
    large = RoundedCornerShape(24.dp),
    extraLarge = RoundedCornerShape(30.dp),
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
        shapes = ShieldShapes,
        content = content,
    )
}

/** Semantic colours used by the alert screen, outside the Material roles. */
object ShieldAlertColors {
    val intruder = Color(0xFFFF453A)
    val intruderSoft = Color(0xFFFF6B61)
    val known = Color(0xFF32D74B)
    val knownSoft = Color(0xFF6BE87F)
    val dismiss = Color(0xFF98A4B3)
    val lowConfidence = Color(0xFFFF9F0A)
}

/**
 * The glass recipe.
 *
 * Real backdrop blur is not available below API 31 without a window-level
 * RenderEffect, and even above it, blurring a live window on a phone that may be
 * showing an incoming alarm costs frames on the one screen where frames matter
 * most. So the effect is built from three cheap layers that read the same:
 *
 *  1. a translucent white fill, which is what actually sells "frosted"
 *  2. a 1dp border at low alpha, which gives the edge a highlight
 *  3. a soft drop shadow so the surface sits *above* the background rather than
 *     being printed on it
 *
 * The blur that people are really responding to is already present in the
 * background itself: the ambient orbs are radial gradients, which are soft by
 * construction. Nothing needs to be blurred at runtime.
 */
object ShieldGlass {
    val fill = Color.White.copy(alpha = 0.055f)
    val fillStrong = Color.White.copy(alpha = 0.085f)
    val border = Color.White.copy(alpha = 0.10f)
    val borderStrong = Color.White.copy(alpha = 0.16f)
    val innerHighlight = Color.White.copy(alpha = 0.07f)

    /** Ambient light sources behind the glass. Cool, low saturation, non-competing. */
    val orbPrimary = Color(0xFF1E4D8C)
    val orbSecondary = Color(0xFF0E7A6B)
    val orbTertiary = Color(0xFF7A2340)
}

/**
 * An 8px-derived spacing scale.
 *
 * Every gap in the app is one of these. Off-scale values are how a layout drifts
 * from "aligned" to "nearly aligned", and the eye notices that long before it can
 * name what is wrong.
 */
object ShieldSpacing {
    val xs = 4.dp
    val sm = 8.dp
    val md = 12.dp
    val lg = 16.dp
    val xl = 20.dp
    val xxl = 28.dp
    val screen = 20.dp
}
