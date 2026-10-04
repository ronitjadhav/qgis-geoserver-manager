#! python3  # noqa E265

"""
The embedded layer preview: a map of one layer, feature info on click.

A file raster stands in for the WMS layer: the canvas, the click-or-pan tool
and the identify path are the same; only the provider differs, and QGIS's
GDAL provider answers `identify` without a server.

Usage from the repo root folder:

.. code-block:: bash

    QT_QPA_PLATFORM=offscreen python -m unittest tests.qgis.test_dlg_preview
"""

import struct
import tempfile
from pathlib import Path

from osgeo import gdal, osr
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsError,
    QgsMapSettings,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
)
from qgis.gui import QgsMapMouseEvent
from qgis.PyQt.QtCore import QEvent, QPoint, Qt
from qgis.PyQt.QtXml import QDomDocument
from qgis.testing import start_app, unittest

from geoserver_manager.gui.dlg_preview import LayerPreviewDialog

start_app()
gdal.UseExceptions()


def tiny_raster(path):
    """A 4×4 GeoTIFF over 0..4 / 0..4 (EPSG:4326); pixel value = row * 4 + col."""
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), 4, 4, 1, gdal.GDT_Int16)
    dataset.SetGeoTransform((0, 1, 0, 4, 0, -1))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    dataset.SetProjection(srs.ExportToWkt())
    dataset.GetRasterBand(1).WriteRaster(0, 0, 4, 4, struct.pack("<16h", *range(16)))
    dataset.FlushCache()
    dataset = None
    return path


class TestLayerPreview(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.layer = QgsRasterLayer(
            str(tiny_raster(Path(self.folder.name) / "tiny.tif")), "tiny"
        )
        self.assertTrue(self.layer.isValid())

    def tearDown(self):
        self.folder.cleanup()

    def dialog(self, bbox=(0.0, 0.0, 4.0, 4.0)):
        dlg = LayerPreviewDialog("ws:tiny", self.layer, bbox)
        dlg.show()
        return dlg

    def test_opens_on_the_layers_extent(self):
        dlg = self.dialog()
        self.assertEqual(dlg.canvas.layers(), [self.layer])
        self.assertTrue(dlg.canvas.extent().contains(QgsRectangle(0, 0, 4, 4)))
        self.assertIn("ws:tiny", dlg.windowTitle())
        self.assertTrue(dlg.message.isHidden())
        dlg.canvas.refresh()
        dlg.close()  # with a render pending

    def test_without_an_extent_the_world(self):
        self.assertEqual(
            LayerPreviewDialog.extent_for(None), QgsRectangle(-180, -90, 180, 90)
        )
        self.assertEqual(
            LayerPreviewDialog.extent_for((1.0, 1.0, 1.0, 1.0)),
            QgsRectangle(-180, -90, 180, 90),
        )
        self.assertEqual(
            LayerPreviewDialog.extent_for((0.0, 0.0, 4.0, 2.0)),
            QgsRectangle(0, 0, 4, 2),
        )

    def test_identify_puts_the_value_under_the_point_in_the_panel(self):
        dlg = self.dialog()
        dlg.identify(QgsPointXY(0.5, 3.5))  # top-left pixel
        self.assertIn("Band 1: 0", dlg.info.toPlainText())
        dlg.identify(QgsPointXY(3.5, 0.5))  # bottom-right pixel
        self.assertIn("Band 1: 15", dlg.info.toPlainText())
        self.assertIn("3.500000, 0.500000", dlg.info.toPlainText())
        dlg.close()

    def test_a_click_identifies_and_a_drag_pans(self):
        dlg = self.dialog()
        canvas = dlg.canvas
        tool = canvas.mapTool()
        identified = []
        dlg.identify = identified.append  # the tool calls through the dialog
        tool._on_click = dlg.identify

        def event(kind, x, y):
            return QgsMapMouseEvent(
                canvas,
                kind,
                QPoint(x, y),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )

        # press and release in place: a click
        tool.canvasPressEvent(event(QEvent.Type.MouseButtonPress, 100, 100))
        tool.canvasReleaseEvent(event(QEvent.Type.MouseButtonRelease, 101, 100))
        self.assertEqual(len(identified), 1)
        self.assertIsInstance(identified[0], QgsPointXY)

        # press, move far, release: a pan. The map moves, nothing is identified
        before = canvas.extent().center()
        tool.canvasPressEvent(event(QEvent.Type.MouseButtonPress, 100, 100))
        tool.canvasMoveEvent(event(QEvent.Type.MouseMove, 160, 140))
        tool.canvasReleaseEvent(event(QEvent.Type.MouseButtonRelease, 160, 140))
        self.assertEqual(len(identified), 1)
        self.assertNotEqual(canvas.extent().center(), before)
        dlg.close()

    def test_the_format_is_the_richest_the_provider_offers(self):
        # a file raster answers values; GeoServer over WMS answers text
        self.assertEqual(
            LayerPreviewDialog.identify_format(self.layer.dataProvider()),
            Qgis.RasterIdentifyFormat.Value,
        )

        class Mute:
            def capabilities(self):
                return Qgis.RasterInterfaceCapabilities()

        self.assertIsNone(LayerPreviewDialog.identify_format(Mute()))

    def test_a_failed_identify_shows_the_providers_message(self):
        """QgsError.message() is HTML: the plain text panel showed
        '<p><b>WMS:</b> Cannot identify: ...'."""

        class Result:
            def isValid(self):  # noqa: N802
                return False

            def error(self):
                return QgsError("Cannot identify: GetFeatureInfo refused", "WMS")

        text = LayerPreviewDialog.result_text(QgsPointXY(1, 2), Result())
        self.assertEqual(text, "Cannot identify: GetFeatureInfo refused")

    def test_text_answers_are_shown_as_they_are(self):
        class Result:
            def isValid(self):  # noqa: N802
                return True

            def results(self):
                return {0: "Results for FeatureType 'sfdem':\nGRAY_INDEX = 1538.0"}

        text = LayerPreviewDialog.result_text(QgsPointXY(1, 2), Result())
        self.assertIn("GRAY_INDEX = 1538.0", text)
        self.assertNotIn("Band", text)

    def test_an_invalid_layer_is_explained_in_plain_words_in_place_of_the_map(self):
        """The window showed QGIS's HTML chain: 'Provider is not valid' and
        the URI, never the request that failed."""
        server = "http://127.0.0.1:1/geoserver/topp/ows"  # nothing listens here
        broken = QgsRasterLayer(
            f"crs=EPSG:4326&format=image/png&layers=roads&styles=&url={server}",
            "roads",
            "wms",
        )
        self.assertFalse(broken.isValid())
        dlg = LayerPreviewDialog("topp:roads", broken, None)
        self.assertFalse(dlg.message.isHidden())
        self.assertEqual(
            dlg.message.text(),
            "The layer did not load: "
            "Download of capabilities failed: Connection refused",
        )
        self.assertEqual(dlg.canvas.layers(), [])
        dlg.identify(QgsPointXY(0, 0))
        self.assertIn("does not answer feature info", dlg.info.toPlainText())
        dlg.close()

    def test_a_project_read_leaves_the_preview_on_its_layer(self):
        """Every map canvas reads the project QGIS opens: the preview moved to
        its EPSG:3857 over Europe, and a click sent metres as degrees."""
        settings = QgsMapSettings()
        settings.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        settings.setExtent(QgsRectangle(-1000000, 4000000, 3000000, 7500000))
        document = QDomDocument()
        root = document.createElement("qgis")
        document.appendChild(root)
        canvas = document.createElement("mapcanvas")
        canvas.setAttribute("name", "theMapCanvas")
        root.appendChild(canvas)
        settings.writeXml(canvas, document)
        path = Path(self.folder.name) / "europe.qgs"
        path.write_text(document.toString())
        self.addCleanup(QgsProject.instance().clear)

        dlg = self.dialog()
        self.assertTrue(QgsProject.instance().read(str(path)))
        self.assertEqual(
            dlg.canvas.mapSettings().destinationCrs().authid(), "EPSG:4326"
        )
        self.assertTrue(dlg.canvas.extent().contains(QgsRectangle(0, 0, 4, 4)))
        dlg.identify(QgsPointXY(0.5, 3.5))
        self.assertIn("Band 1: 0", dlg.info.toPlainText())
        dlg.close()

    def test_saving_the_project_leaves_the_preview_out(self):
        """A save wrote the preview's canvas into the .qgs, as a nameless one."""
        path = Path(self.folder.name) / "saved.qgs"
        self.addCleanup(QgsProject.instance().clear)

        def canvases():
            self.assertTrue(QgsProject.instance().write(str(path)))
            return path.read_text().count("<mapcanvas")

        before = canvases()
        dlg = self.dialog()
        self.assertEqual(canvases(), before)
        dlg.close()


if __name__ == "__main__":
    unittest.main()
