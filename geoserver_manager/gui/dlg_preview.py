#! python3  # noqa: E265

"""
Preview a GeoServer layer on a map inside QGIS, with the feature info on click.

The map is a QgsMapCanvas showing one WMS layer built the way *Add to QGIS*
builds it (credentials as the auth config id), but the layer lives in this
window only: nothing reaches the project. A click asks the provider to
identify the point, which for a WMS layer is a GetFeatureInfo request; QGIS
already knows how to send it, so the plugin does not hand-roll the URL.
"""

from qgis.core import Qgis, QgsCoordinateReferenceSystem, QgsProject, QgsRectangle
from qgis.gui import QgsMapCanvas, QgsMapTool
from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QCursor
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QPlainTextEdit,
    QSplitter,
    QVBoxLayout,
)

from geoserver_manager.gui.theme import status_colour
from geoserver_manager.toolbelt.rest import summarise_body

WORLD = (-180.0, -90.0, 180.0, 90.0)
# A press and release closer than this is a click; further apart is a drag.
_CLICK_TOLERANCE_PX = 3


def load_error(layer):
    """Why QGIS could not load `layer`, as one line of plain text.

    The layer's own error is HTML, and for a failed request it only says
    "Provider is not valid" and the URI. The provider knows more: the request
    that failed (lastError) or the check that did. The WFS provider only
    writes its reason to QGIS's log, so then the text says where to look.
    """
    provider = layer.dataProvider()
    if provider is None:
        texts = [layer.error().summary()]
    else:
        texts = [provider.lastError(), provider.error().summary()]
    for text in texts:
        text = summarise_body(" ".join((text or "").split()))
        if text:
            return text
    return QCoreApplication.translate(
        "LayerPreviewDialog", "QGIS logged the reason in its log panel, on the {} tab"
    ).format(layer.providerType().upper())


class _ClickOrPanTool(QgsMapTool):
    """Drag pans, a click identifies: one tool, no mode to switch."""

    def __init__(self, canvas, on_click):
        super().__init__(canvas)
        self._on_click = on_click
        self._pressed = None
        self._dragging = False
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

    def canvasPressEvent(self, event):  # noqa: N802 (Qt override)
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = event.pos()
            self._dragging = False

    def canvasMoveEvent(self, event):  # noqa: N802 (Qt override)
        if self._pressed is None:
            return
        if (
            self._dragging
            or (event.pos() - self._pressed).manhattanLength() > _CLICK_TOLERANCE_PX
        ):
            self._dragging = True
            self.canvas().panAction(event)

    def canvasReleaseEvent(self, event):  # noqa: N802 (Qt override)
        if self._pressed is None:
            return
        self._pressed = None
        if self._dragging:
            self.canvas().panActionEnd(event.pos())
        else:
            self._on_click(event.mapPoint())


class LayerPreviewDialog(QDialog):
    """A map of one layer and a panel for what GetFeatureInfo says at a click.

    :param layer: a QgsRasterLayer, valid or not. An invalid one is reported
        in the window instead of a map.
    :param bbox: (minx, miny, maxx, maxy) in EPSG:4326 to open on, or None
        for the world.
    """

    def __init__(self, title, layer, bbox=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Preview: {}").format(title))
        # A window of its own: not modal, so the main dialog's loads and
        # tasks carry on, and gone for good once closed.
        self.setWindowFlags(Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(900, 600)
        self.layer = layer

        self.message = QLabel(self)
        self.message.setWordWrap(True)
        self.message.hide()
        self.canvas = QgsMapCanvas(self)
        # The name finds the node it writes into a saved project.
        self.canvas.setObjectName("geoserver_manager_preview")
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        self._view = self.extent_for(bbox)
        # These run after the canvas's own: PyQt cannot drop QGIS's connections.
        QgsProject.instance().readProject.connect(self._back_to_the_layer)
        QgsProject.instance().writeProject.connect(self._out_of_the_project)
        self.info = QPlainTextEdit(self)
        self.info.setReadOnly(True)
        self.info.setPlaceholderText(
            self.tr(
                "Click the map for the feature info at that point. Drag to pan, "
                "wheel to zoom."
            )
        )
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self.canvas)
        splitter.addWidget(self.info)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout = QVBoxLayout(self)
        layout.addWidget(self.message)
        layout.addWidget(splitter)

        if layer.isValid():
            self.canvas.setLayers([layer])
            self.canvas.setExtent(self._view)
            self.canvas.setMapTool(_ClickOrPanTool(self.canvas, self.identify))
            self.canvas.refresh()
        else:
            self._say(self.tr("The layer did not load: {}").format(load_error(layer)))

    @staticmethod
    def extent_for(bbox):
        """A QgsRectangle for a usable bbox, else the world."""
        if bbox and bbox[2] > bbox[0] and bbox[3] > bbox[1]:
            return QgsRectangle(*bbox)
        return QgsRectangle(*WORLD)

    def _back_to_the_layer(self, _document):
        """Undo what the canvas read from a project: its CRS and its extent.

        Every QgsMapCanvas follows the project QGIS opens. The preview moved
        to that project's CRS and extent, and identify then missed: the
        provider reads the point and the extent in the layer's EPSG:4326.
        """
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        self.canvas.setRotation(0)
        self.canvas.setExtent(self._view)
        self.canvas.refresh()

    def _out_of_the_project(self, document):
        """Remove the node the canvas just wrote into a project being saved."""
        nodes = document.elementsByTagName("mapcanvas")
        for index in reversed(range(nodes.count())):
            element = nodes.item(index).toElement()
            if element.attribute("name") == self.canvas.objectName():
                element.parentNode().removeChild(element)

    def _say(self, text):
        self.message.setText(text)
        self.message.setStyleSheet(f"color: {status_colour('error', self.palette())};")
        self.message.show()

    def identify(self, point):
        """GetFeatureInfo (or the band values) at a map point, into the panel."""
        # a failed WMS layer still has a provider, and QGIS 4 crashes asking it
        provider = self.layer.dataProvider() if self.layer.isValid() else None
        fmt = self.identify_format(provider) if provider is not None else None
        if fmt is None:
            self.info.setPlainText(
                self.tr("This layer does not answer feature info requests.")
            )
            return
        if getattr(self, "_identifying", False):
            # The WMS provider's request runs a nested event loop, so a second
            # click could start another identify inside the first one.
            return
        self._identifying = True
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            # The WMS provider needs the view to turn the point into a pixel.
            # A blocking QGIS call on the GUI thread, bounded by
            # QGIS's own network timeout; the provider is not thread-safe.
            result = provider.identify(
                point,
                fmt,
                self.canvas.extent(),
                self.canvas.width(),
                self.canvas.height(),
            )
        finally:
            QApplication.restoreOverrideCursor()
            self._identifying = False
        self.info.setPlainText(self.result_text(point, result))

    @staticmethod
    def identify_format(provider):
        """The richest identify format this provider supports, or None.

        Text is GetFeatureInfo as GeoServer writes it; Value is what a file
        raster answers; HTML is the fallback for a server that offers nothing
        plainer.
        """
        capabilities = provider.capabilities()
        for capability, fmt in (
            (
                Qgis.RasterInterfaceCapability.IdentifyText,
                Qgis.RasterIdentifyFormat.Text,
            ),
            (
                Qgis.RasterInterfaceCapability.IdentifyValue,
                Qgis.RasterIdentifyFormat.Value,
            ),
            (
                Qgis.RasterInterfaceCapability.IdentifyHtml,
                Qgis.RasterIdentifyFormat.Html,
            ),
        ):
            if int(capabilities & capability):
                return fmt
        return None

    @staticmethod
    def result_text(point, result):
        """The panel's text for one identify result."""
        if not result.isValid():
            # summary(): message() is HTML, which the plain text panel showed as tags.
            return result.error().summary() or QCoreApplication.translate(
                "LayerPreviewDialog", "GetFeatureInfo failed."
            )
        lines = [f"{point.x():.6f}, {point.y():.6f}"]
        values = result.results()
        for key in sorted(values):
            value = values[key]
            lines.append(
                str(value)
                if isinstance(value, str)
                else QCoreApplication.translate(
                    "LayerPreviewDialog", "Band {}: {}"
                ).format(key, value)
            )
        return "\n".join(lines) if len(lines) > 1 else lines[0] + "\n-"

    def closeEvent(self, event):  # noqa: N802 (Qt override)
        # A render still running would paint into a canvas on its way out.
        self.canvas.stopRendering()
        super().closeEvent(event)
