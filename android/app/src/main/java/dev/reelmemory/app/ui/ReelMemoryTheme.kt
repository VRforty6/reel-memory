package dev.reelmemory.app.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/**
 * Calm, premium light theme with a purple accent. Deliberately fixed (no
 * dynamic-color dependency) so the look is identical on every device.
 */
private val PurplePrimary = Color(0xFF6750A4)
private val OnPurplePrimary = Color(0xFFFFFFFF)
private val PurplePrimaryContainer = Color(0xFFEADDFF)
private val OnPurplePrimaryContainer = Color(0xFF21005D)
private val PurpleSecondary = Color(0xFF625B71)
private val PurpleSecondaryContainer = Color(0xFFE8DEF8)
private val PurpleTertiary = Color(0xFF7D5260)
private val PurpleTertiaryContainer = Color(0xFFFFD8E4)
private val AppBackground = Color(0xFFFAF7FF)
private val AppSurface = Color(0xFFFFFFFF)
private val AppSurfaceVariant = Color(0xFFEDE7F6)

private val ReelMemoryColors = lightColorScheme(
    primary = PurplePrimary,
    onPrimary = OnPurplePrimary,
    primaryContainer = PurplePrimaryContainer,
    onPrimaryContainer = OnPurplePrimaryContainer,
    secondary = PurpleSecondary,
    onSecondary = Color(0xFFFFFFFF),
    secondaryContainer = PurpleSecondaryContainer,
    onSecondaryContainer = Color(0xFF1D192B),
    tertiary = PurpleTertiary,
    onTertiary = Color(0xFFFFFFFF),
    tertiaryContainer = PurpleTertiaryContainer,
    onTertiaryContainer = Color(0xFF31111D),
    background = AppBackground,
    onBackground = Color(0xFF1C1B1F),
    surface = AppSurface,
    onSurface = Color(0xFF1C1B1F),
    surfaceVariant = AppSurfaceVariant,
    onSurfaceVariant = Color(0xFF49454F),
    error = Color(0xFFBA1A1A),
    errorContainer = Color(0xFFFFDAD6),
    onErrorContainer = Color(0xFF410002),
    outline = Color(0xFF79747E),
)

@Composable
fun ReelMemoryTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = ReelMemoryColors,
        content = content,
    )
}
