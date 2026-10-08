package dev.reelmemory.app.ui

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.PathParser
import androidx.compose.ui.unit.dp

/**
 * Tiny local vector assets replacing icons that only ship in
 * material-icons-extended. The extended AAR is ~44x the core AAR
 * (35.7 MB vs 0.8 MB at 1.7.0) and blew the dex heap, so the project
 * deliberately depends on material-icons-core only. Every path here is a
 * simple primitive (circle, rect, polygon) — no copied path data.
 */

private fun nodes(d: String) = PathParser().parsePathString(d).toNodes()

/**
 * Hollow circle — local replacement for Icons.Filled.RadioButtonUnchecked
 * (extended-only). Used for the not-yet-done steps of the processing
 * stage list.
 */
val CircleOutline: ImageVector by lazy {
    ImageVector.Builder(
        name = "CircleOutline",
        defaultWidth = 24.dp,
        defaultHeight = 24.dp,
        viewportWidth = 24f,
        viewportHeight = 24f
    ).addPath(
        // Circle of radius 8 centered at (12, 12), drawn as two arcs.
        pathData = nodes("M12,12 m-8,0 a8,8 0 1,0 16,0 a8,8 0 1,0 -16,0"),
        stroke = SolidColor(Color.Black),
        strokeLineWidth = 2f,
        strokeLineCap = StrokeCap.Round
    ).build()
}

/**
 * Image-frame glyph (frame + sun + mountains) — local replacement for
 * Icons.Filled.Image (extended-only). Used for the "Add images" add-sheet
 * row and the thumbnail placeholder.
 */
val ImageFrame: ImageVector by lazy {
    ImageVector.Builder(
        name = "ImageFrame",
        defaultWidth = 24.dp,
        defaultHeight = 24.dp,
        viewportWidth = 24f,
        viewportHeight = 24f
    ).apply {
        // Frame.
        addPath(
            pathData = nodes("M4,5 H20 V19 H4 Z"),
            stroke = SolidColor(Color.Black),
            strokeLineWidth = 2f
        )
        // Sun.
        addPath(
            pathData = nodes("M9.5,10 m-1.6,0 a1.6,1.6 0 1,0 3.2,0 a1.6,1.6 0 1,0 -3.2,0"),
            fill = SolidColor(Color.Black)
        )
        // Mountains.
        addPath(
            pathData = nodes("M6.5,17.5 L11,12 L13.8,14.8 L15.6,13 L19,17.5 Z"),
            fill = SolidColor(Color.Black)
        )
    }.build()
}
