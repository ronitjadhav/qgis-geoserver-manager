#! python3  # noqa: E265

"""
Colours that stay legible in both QGIS themes.

The plugin used literal `red`, `green` and `gray` in stylesheets. On QGIS's
dark themes (Night Mapping, Blend of Gray) those read poorly: pure red on a
near-black panel is hard on the eyes, and `gray` hint text sinks into the
background. Everything here derives from the widget's own palette instead, so
a theme the plugin has never seen still gets readable text.

Qt has no palette role for "this went wrong", so the semantic colours are two
fixed pairs (one for light backgrounds, one for dark) chosen for contrast
against the palette's window colour rather than for brand.
"""

from qgis.PyQt.QtGui import QColor, QPalette

from geoserver_manager.toolbelt.icon_catalog import load_catalog

# Status colours, as (on a light background, on a dark background).
_STATUS = {
    "ok": ("#1e7b34", "#6ddf8a"),
    "error": ("#b3261e", "#ff8a80"),
    "busy": ("#5f6368", "#b0b4ba"),
    "neutral": (None, None),  # let the palette's own text colour through
}


def is_dark(palette):
    """True when this palette paints widgets on a dark background."""
    return palette.color(QPalette.ColorRole.Window).lightness() < 128


def status_colour(kind, palette):
    """Hex colour for a connection-status message, or None for "leave it".

    :param kind: one of "ok", "error", "busy", "neutral".
    """
    light, dark = _STATUS.get(kind, _STATUS["neutral"])
    colour = dark if is_dark(palette) else light
    return colour


def icon_colours(palette):
    """Ink, blue, green and destructive strokes for the resource icons.

    The accents echo the logo, with darker strokes on light backgrounds and
    lighter ones on dark backgrounds. Selection uses HighlightedText instead.
    """
    accents = load_catalog()["accents"]["dark" if is_dark(palette) else "light"]
    return (
        palette.color(QPalette.ColorRole.Text).name(),
        accents["blue"],
        accents["green"],
        status_colour("error", palette),
    )


def hint_colour(palette):
    """Colour for secondary text: descriptions, field hints.

    `PlaceholderText` is the role Qt itself uses for text that should read as
    subdued without disappearing, and every QGIS theme sets it. Qt's default
    is the text colour at half alpha, which a stylesheet's #rrggbb would
    drop, so the colour comes back already blended over the window colour.
    """
    colour = palette.color(QPalette.ColorRole.PlaceholderText)
    if not colour.isValid() or colour.alpha() == 0:
        # Older styles leave it unset; the disabled text colour is the next
        # best thing and is always defined.
        colour = palette.color(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText
        )
    window = palette.color(QPalette.ColorRole.Window)
    alpha = colour.alphaF()
    return QColor(
        *(
            round(alpha * ink + (1 - alpha) * paper)
            for ink, paper in zip(colour.getRgb()[:3], window.getRgb()[:3])
        )
    ).name()


def invalid_field_colour(palette):
    """Border colour marking a field the form will not accept."""
    return status_colour("error", palette)


def contrast_ratio(first, second):
    """WCAG contrast ratio between two colours, 1.0 (same) to 21.0 (black/white).

    Used by the tests to assert the choices above stay readable in both
    themes; kept here so the rule lives next to the colours it judges.
    """

    def luminance(colour):
        channels = []
        for value in (colour.redF(), colour.greenF(), colour.blueF()):
            channels.append(
                value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
            )
        red, green, blue = channels
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    first, second = QColor(first), QColor(second)
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)
