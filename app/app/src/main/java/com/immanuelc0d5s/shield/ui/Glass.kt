package com.immanuelc0d5s.shield.ui

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.immanuelc0d5s.shield.ui.theme.ShieldGlass
import com.immanuelc0d5s.shield.ui.theme.ShieldSpacing

/**
 * The ambient background every screen sits on.
 *
 * Radial gradients rather than blurred shapes. `Modifier.blur` over an orb is the
 * usual way to build this, but it costs a render pass per frame and buys nothing
 * visible: `Brush.radialGradient` already falls off smoothly, so the orbs are
 * soft for free. Drawn once on a Canvas, it never re-renders.
 *
 * Positions are deliberately off-centre and asymmetric. A centred glow reads as a
 * vignette; an off-centre one reads as light in a room.
 */
@Composable
fun AmbientBackground(modifier: Modifier = Modifier) {
    Canvas(modifier = modifier.fillMaxSize()) {
        val w = size.width
        val h = size.height
        val r = maxOf(w, h) * 0.75f

        listOf(
            Triple(Offset(w * 0.86f, h * 0.06f), r, ShieldGlass.orbPrimary to 0.34f),
            Triple(Offset(w * 0.04f, h * 0.52f), r * 0.92f, ShieldGlass.orbSecondary to 0.26f),
            Triple(Offset(w * 0.62f, h * 1.02f), r * 0.80f, ShieldGlass.orbTertiary to 0.20f),
        ).forEach { (center, radius, spec) ->
            val (colour, alpha) = spec
            drawCircle(
                brush = Brush.radialGradient(
                    colors = listOf(colour.copy(alpha = alpha), Color.Transparent),
                    center = center,
                    radius = radius,
                ),
                radius = radius,
                center = center,
            )
        }
    }
}

/**
 * A frosted surface. See [ShieldGlass] for why this is three cheap layers rather
 * than an actual blur.
 */
@Composable
fun GlassSurface(
    modifier: Modifier = Modifier,
    shape: Shape = MaterialTheme.shapes.medium,
    fill: Color = ShieldGlass.fill,
    borderColor: Color = ShieldGlass.border,
    elevation: Dp = 0.dp,
    content: @Composable () -> Unit,
) {
    Box(
        modifier = modifier
            .shadow(
                elevation = elevation,
                shape = shape,
                ambientColor = Color.Black,
                spotColor = Color.Black,
            )
            .clip(shape)
            .background(fill)
            .border(BorderStroke(1.dp, borderColor), shape),
    ) {
        content()
    }
}

/** The standard content card: glass, medium radius, evenly padded. */
@Composable
fun GlassCard(
    modifier: Modifier = Modifier,
    shape: Shape = MaterialTheme.shapes.medium,
    fill: Color = ShieldGlass.fill,
    borderColor: Color = ShieldGlass.border,
    contentPadding: Dp = ShieldSpacing.lg,
    content: @Composable ColumnScope.() -> Unit,
) {
    GlassSurface(
        modifier = modifier.fillMaxWidth(),
        shape = shape,
        fill = fill,
        borderColor = borderColor,
    ) {
        Column(
            modifier = Modifier.padding(contentPadding),
            verticalArrangement = Arrangement.spacedBy(ShieldSpacing.sm),
            content = content,
        )
    }
}

/**
 * A button that reacts to being pressed.
 *
 * The scale is the whole interaction. Nothing here is slower than ~200ms: on the
 * alert screen this is pressed by someone who wants the noise to stop, and an
 * animation that lags the touch is perceived as lag in the app itself.
 *
 * The pressed state also darkens rather than only shrinking, because a scale
 * change alone is easy to miss on a large target in peripheral vision at 3am.
 */
@Composable
fun ShieldButton(
    label: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    containerColor: Color = MaterialTheme.colorScheme.primary,
    contentColor: Color = MaterialTheme.colorScheme.onPrimary,
    borderColor: Color? = null,
    height: Dp = 56.dp,
    shape: Shape = RoundedCornerShape(16.dp),
) {
    val interactionSource = remember { MutableInteractionSource() }
    val pressed by interactionSource.collectIsPressedAsState()

    val scale by animateFloatAsState(
        targetValue = if (pressed && enabled) 0.965f else 1f,
        animationSpec = spring(
            dampingRatio = Spring.DampingRatioMediumBouncy,
            stiffness = Spring.StiffnessMediumLow,
        ),
        label = "pressScale",
    )
    val alpha by animateFloatAsState(
        targetValue = if (enabled) 1f else 0.38f,
        animationSpec = tween(160),
        label = "enabledAlpha",
    )
    val resolvedContent by animateColorAsState(
        targetValue = if (enabled) contentColor else contentColor.copy(alpha = 0.65f),
        animationSpec = tween(160),
        label = "contentColor",
    )

    Box(
        modifier = modifier
            .fillMaxWidth()
            .height(height)
            .scale(scale)
            .clip(shape)
            .background(containerColor.copy(alpha = alpha))
            .then(
                if (borderColor != null) Modifier.border(BorderStroke(1.dp, borderColor), shape)
                else Modifier
            )
            .clickable(
                interactionSource = interactionSource,
                indication = null,
                enabled = enabled,
                role = Role.Button,
                onClick = onClick,
            )
            .padding(horizontal = ShieldSpacing.lg),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = label,
            style = MaterialTheme.typography.labelLarge,
            color = resolvedContent,
            textAlign = TextAlign.Center,
        )
    }
}

/**
 * A small status pill: a tinted dot plus a label.
 *
 * Replaces coloured text, which was the previous approach and had two problems. A
 * reader scanning for "is this answered?" had to parse a sentence fragment to
 * find the state, and colour alone is not an accessible signal — the dot plus the
 * word carries it in two channels.
 */
@Composable
fun StatusChip(
    label: String,
    color: Color,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier
            .clip(RoundedCornerShape(999.dp))
            .background(color.copy(alpha = 0.14f))
            .border(1.dp, color.copy(alpha = 0.32f), RoundedCornerShape(999.dp))
            .padding(horizontal = ShieldSpacing.md, vertical = 6.dp),
        horizontalArrangement = Arrangement.spacedBy(ShieldSpacing.sm),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            modifier = Modifier
                .size(7.dp)
                .clip(RoundedCornerShape(999.dp))
                .background(color),
        )
        Text(label, style = MaterialTheme.typography.labelMedium, color = color)
    }
}

/**
 * A section heading with a short trailing rule.
 *
 * Gives a long settings screen a scannable rhythm without a full divider system,
 * and the rule is far quieter than a horizontal line across the screen.
 */
@Composable
fun SectionLabel(text: String, modifier: Modifier = Modifier) {
    Row(
        modifier = modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(ShieldSpacing.md),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = text.uppercase(),
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Canvas(modifier = Modifier.weight(1f).height(1.dp)) {
            drawLine(
                color = Color.White.copy(alpha = 0.08f),
                start = Offset(0f, 0f),
                end = Offset(size.width, 0f),
                strokeWidth = 1f,
            )
        }
    }
}
