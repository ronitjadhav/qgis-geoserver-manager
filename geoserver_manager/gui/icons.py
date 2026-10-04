#! python3  # noqa: E265

"""Small SVG icons, rendered with the widget's palette at the requested size."""

import re
from functools import lru_cache

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QByteArray, QRect, QRectF, Qt
from qgis.PyQt.QtGui import QIcon, QIconEngine, QPainter, QPalette, QPixmap
from qgis.PyQt.QtSvg import QSvgRenderer
from qgis.PyQt.QtWidgets import QApplication

from geoserver_manager.gui.theme import icon_colours
from geoserver_manager.toolbelt.icon_catalog import (
    RESOURCES,
    SOURCE_COLOURS,
    icon_spec,
)


class _SvgIconEngine(QIconEngine):
    """Paint vectors directly, including selected and disabled icon states."""

    def __init__(self, source, normal, selected, disabled, for_menu):
        super().__init__()
        self._args = source, normal, selected, disabled, for_menu
        self._renderers = {}
        for mode, colours in (
            (QIcon.Mode.Normal, normal),
            (QIcon.Mode.Selected, (selected,) * 4),
            (QIcon.Mode.Disabled, (disabled,) * 4),
        ):
            replacements = dict(zip(SOURCE_COLOURS, colours))
            svg = re.sub(
                "|".join(SOURCE_COLOURS),
                lambda match: replacements[match.group()],
                source,
            )
            self._renderers[mode] = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        if for_menu:
            # QMenu requests Active for its highlighted item. Buttons also
            # use Active on hover, so only menu icons map it to selection.
            self._renderers[QIcon.Mode.Active] = self._renderers[QIcon.Mode.Selected]

    def clone(self):
        return _SvgIconEngine(*self._args)

    def paint(self, painter, rect, mode, state):
        renderer = self._renderers.get(mode, self._renderers[QIcon.Mode.Normal])
        # Preserve the square canvas even if a caller supplies a wider rectangle.
        side = min(rect.width(), rect.height())
        bounds = QRectF(
            rect.x() + (rect.width() - side) / 2,
            rect.y() + (rect.height() - side) / 2,
            side,
            side,
        )
        painter.save()
        renderer.render(painter, bounds)
        painter.restore()

    def pixmap(self, size, mode, state):
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self.paint(painter, QRect(0, 0, size.width(), size.height()), mode, state)
        painter.end()
        return pixmap


@lru_cache(maxsize=128)
def _custom_icon(asset, normal, selected, disabled, for_menu):
    # All rows share an icon for each palette, rather than parsing each SVG
    # for every button. Qt requests a fresh vector rendering at any DPI.
    source = (RESOURCES / asset).read_text(encoding="utf-8")
    return QIcon(_SvgIconEngine(source, normal, selected, disabled, for_menu))


def icon(name, palette=None, selected=False, *, for_menu=False):
    """Load a registered icon; for_menu also recolours a highlighted menu item."""
    entry = icon_spec(name)
    if entry["status"] == "needs-custom":
        return QgsApplication.getThemeIcon(entry["fallback"])
    if palette is None:
        palette = QApplication.palette()
    highlight = palette.color(QPalette.ColorRole.HighlightedText).name()
    normal = SOURCE_COLOURS if entry["category"] == "Brand" else icon_colours(palette)
    return _custom_icon(
        entry["asset"],
        (highlight,) * 4 if selected else normal,
        highlight,
        palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text).name(),
        for_menu,
    )
