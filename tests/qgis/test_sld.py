#! python3  # noqa E265

"""
The QGIS half of the SLD helpers: exporting a layer's symbology, loading one
back, and listing what a project can style.

Usage from the repo root folder:

.. code-block:: bash

    QT_QPA_PLATFORM=offscreen python -m unittest tests.qgis.test_sld
"""

import io
import shutil
import tempfile
import zipfile
from pathlib import Path

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsFeature,
    QgsFontMarkerSymbolLayer,
    QgsGeometry,
    QgsMarkerSymbol,
    QgsPalLayerSettings,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRendererCategory,
    QgsSingleSymbolRenderer,
    QgsSvgMarkerSymbolLayer,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)
from qgis.testing import start_app, unittest

from geoserver_manager.toolbelt.sld import (
    apply_sld_to_layer,
    icon_package,
    layer_to_sld,
    sld_version,
    styleable_project_layers,
)

start_app()


class Exporting:
    """A layer whose export answers as QGIS does, with `written` in the file."""

    def __init__(self, answer, written=""):
        self.answer, self.written = answer, written

    def saveSldStyle(self, path):  # noqa: N802
        if self.written:
            Path(path).write_text(self.written, encoding="utf-8")
        return self.answer

    def name(self):
        return "towns"


def raster_layer(folder):
    """A two-by-two grid GDAL reads, as a QGIS raster layer."""
    grid = Path(folder) / "dem.asc"
    grid.write_text(
        "ncols 2\nnrows 2\nxllcorner 0\nyllcorner 0\ncellsize 1\n1 2\n3 4\n"
    )
    return QgsRasterLayer(str(grid), "dem")


def point_layer(name, colour="#3388ff"):
    """A two-feature point layer with a single-symbol renderer."""
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=kind:string", name, "memory")
    provider = layer.dataProvider()
    for kind, x, y in (("city", 1, 1), ("village", 2, 2)):
        feature = QgsFeature(layer.fields())
        feature.setAttributes([kind])
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        provider.addFeature(feature)
    layer.updateExtents()
    layer.setRenderer(
        QgsSingleSymbolRenderer(
            QgsMarkerSymbol.createSimple({"color": colour, "size": "3"})
        )
    )
    return layer


class TestApplyInAnotherEncoding(unittest.TestCase):
    def test_the_text_reaches_qgis_under_a_utf8_declaration(self):
        """QGIS reads the file in the encoding its declaration names. The text
        of an ISO-8859-1 style was written as UTF-8 under that declaration,
        so "Café" arrived as "CafÃ©"."""
        import xml.etree.ElementTree as ElementTree

        read = []

        class Layer:
            def loadSldStyle(self, path):  # noqa: N802
                read.append(ElementTree.parse(path).getroot().findtext("Title"))
                return True, ""

            def triggerRepaint(self):  # noqa: N802
                pass

        sld = (
            '<?xml version="1.0" encoding="ISO-8859-1"?>'
            "<StyledLayerDescriptor><Title>Caf\xe9</Title></StyledLayerDescriptor>"
        )
        self.assertEqual(apply_sld_to_layer(Layer(), sld), (True, ""))
        self.assertEqual(read, ["Caf\xe9"])


class TestLayerToSld(unittest.TestCase):
    def test_a_qgis_export_is_sld_1_1(self):
        """The fact the upload's content type hangs on."""
        sld = layer_to_sld(point_layer("towns"))
        self.assertEqual(sld_version(sld), "1.1.0")
        self.assertIn("StyledLayerDescriptor", sld)

    def test_a_layer_that_exports_nothing_is_refused_in_the_users_language(self):
        from qgis.PyQt.QtCore import QCoreApplication

        from tests.qgis.test_i18n import Spy

        class Mute:  # a renderer QGIS cannot write as SLD
            def saveSldStyle(self, path):  # noqa: N802
                return None

            def name(self):
                return "towns"

        spy = Spy(["Sld"])
        QCoreApplication.installTranslator(spy)
        self.addCleanup(QCoreApplication.removeTranslator, spy)
        with self.assertRaises(RuntimeError) as raised:
            layer_to_sld(Mute())
        self.assertTrue(str(raised.exception).startswith("[Sld] "), raised.exception)
        self.assertIn("'towns'", str(raised.exception))

    def test_the_symbology_is_actually_in_there(self):
        layer = point_layer("towns", colour="#ff0000")
        sld = layer_to_sld(layer)
        self.assertIn("ff0000", sld.lower())

    def test_categories_survive_the_export(self):
        layer = point_layer("towns")
        layer.setRenderer(
            QgsCategorizedSymbolRenderer(
                "kind",
                [
                    QgsRendererCategory(
                        "city",
                        QgsMarkerSymbol.createSimple({"color": "red"}),
                        "City",
                    ),
                    QgsRendererCategory(
                        "village",
                        QgsMarkerSymbol.createSimple({"color": "blue"}),
                        "Village",
                    ),
                ],
            )
        )
        sld = layer_to_sld(layer)
        self.assertIn("City", sld)
        self.assertIn("Village", sld)
        self.assertIn("kind", sld)  # the filter references the attribute


class TestWhatQgisCannotExport(unittest.TestCase):
    """QGIS 3.44 refuses the export with its reason; 3.40 writes what it
    cannot as a comment and reports a success (qgsrenderer.h:360-364,
    qgssymbollayer.h:427-428, qgsvectorlayerlabeling.cpp:291-294)."""

    def test_qgis_own_reason_is_the_error(self):
        # The fixed text blamed the symbology of a layer whose labels failed.
        reason = "Cannot export label expression upper(str1) || ' #' to SLD"
        with self.assertRaises(RuntimeError) as raised:
            layer_to_sld(Exporting((reason, False)))
        self.assertEqual(str(raised.exception), reason)

    def test_a_partial_export_is_refused_naming_what_was_left_out(self):
        written = (
            '<StyledLayerDescriptor version="1.1.0"><se:FeatureTypeStyle>'
            "<!--FeatureRenderer heatmapRenderer not implemented yet-->"
            "</se:FeatureTypeStyle><se:Label><!--SE Export for "
            'upper("str1") not implemented yet-->Placeholder</se:Label>'
            "<!--Parametric SVG--></StyledLayerDescriptor>"
        )
        with self.assertRaises(RuntimeError) as raised:
            layer_to_sld(Exporting(("Created default style file", True), written))
        said = str(raised.exception)
        self.assertIn("FeatureRenderer heatmapRenderer not implemented yet", said)
        self.assertIn('SE Export for upper("str1") not implemented yet', said)
        self.assertNotIn("Parametric SVG", said)

    def test_an_expression_label_is_refused_with_its_expression(self):
        # Refused by QGIS 3.44, written as a comment by 3.40.
        layer = point_layer("towns")
        settings = QgsPalLayerSettings()
        settings.fieldName = "upper(\"kind\") || ' #'"
        settings.isExpression = True
        layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        layer.setLabelsEnabled(True)
        with self.assertRaises(RuntimeError) as raised:
            layer_to_sld(layer)
        self.assertIn("upper", str(raised.exception))


class TestIconsAndFontsInTheExport(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="gsm_test_"))
        self.addCleanup(shutil.rmtree, self.folder)

    def test_a_font_with_a_space_in_its_name_is_encoded_and_reads_back(self):
        """GeoServer refused ttf://DejaVu Sans with a URISyntaxException (a
        500); ttf://DejaVu%20Sans draws the same letter (2.28.5)."""
        layer = point_layer("towns")
        symbol = QgsMarkerSymbol()
        symbol.changeSymbolLayer(0, QgsFontMarkerSymbolLayer("DejaVu Sans", "A"))
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        sld = layer_to_sld(layer)
        self.assertIn("ttf://DejaVu%20Sans", sld)
        self.assertNotIn("ttf://DejaVu Sans", sld)
        target = point_layer("target")
        self.assertTrue(apply_sld_to_layer(target, sld)[0])
        font = target.renderer().symbol().symbolLayer(0)
        self.assertEqual(font.fontFamily(), "DejaVu Sans")

    def test_an_svg_marker_travels_with_its_style(self):
        """QGIS writes the SVG's path on this machine, which GeoServer cannot
        read: the zip carries the file, the href names it."""
        svg = self.folder / "my icons" / "anchor.svg"
        svg.parent.mkdir()
        svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        layer = point_layer("towns")
        symbol = QgsMarkerSymbol()
        symbol.changeSymbolLayer(0, QgsSvgMarkerSymbolLayer(str(svg)))
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        sld = layer_to_sld(layer)
        self.assertIn(str(svg), sld)
        package = zipfile.ZipFile(io.BytesIO(icon_package(sld, "towns")[0]))
        self.assertIn("towns_anchor.svg", package.namelist())
        inside = package.read("style.sld").decode("utf-8")
        self.assertNotIn(str(svg), inside)
        self.assertIn('xlink:href="towns_anchor.svg', inside)


class TestApplySldToLayer(unittest.TestCase):
    def test_a_style_exported_from_one_layer_lands_on_another(self):
        source = point_layer("source", colour="#ff0000")
        target = point_layer("target", colour="#0000ff")
        ok, message = apply_sld_to_layer(target, layer_to_sld(source))
        self.assertTrue(ok, message)
        colour = target.renderer().symbol().color().name()
        self.assertEqual(colour, "#ff0000")

    def test_a_size_given_as_a_literal_is_the_size_drawn(self):
        """QGIS read the demo style burg's <Size><ogc:Literal>20</ogc:Literal>
        </Size> as 0: Apply said "now uses the style" and drew nothing."""
        folder = Path(tempfile.mkdtemp(prefix="gsm_test_"))
        self.addCleanup(shutil.rmtree, folder)
        svg = folder / "burg02.svg"
        svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        # burg as GeoServer serves it, then the same Size in an SE document.
        burg = (
            '<StyledLayerDescriptor version="1.0.0" xmlns="http://www.opengis.net/sld"'
            ' xmlns:ogc="http://www.opengis.net/ogc"'
            ' xmlns:xlink="http://www.w3.org/1999/xlink"><NamedLayer>'
            "<Name>redflag</Name><UserStyle><Name>burg</Name><FeatureTypeStyle>"
            "<Rule><PointSymbolizer><Graphic><ExternalGraphic>"
            f'<OnlineResource xlink:type="simple" xlink:href="{svg}" />'
            "<Format>image/svg+xml</Format></ExternalGraphic>"
            "<Size>\n  <ogc:Literal>20</ogc:Literal>\n</Size></Graphic>"
            "</PointSymbolizer></Rule></FeatureTypeStyle></UserStyle></NamedLayer>"
            "</StyledLayerDescriptor>"
        )
        se = (
            '<StyledLayerDescriptor version="1.1.0" xmlns="http://www.opengis.net/sld"'
            ' xmlns:se="http://www.opengis.net/se"'
            ' xmlns:ogc="http://www.opengis.net/ogc"'
            ' xmlns:xlink="http://www.w3.org/1999/xlink"><NamedLayer>'
            "<se:Name>f</se:Name><UserStyle><se:Name>f</se:Name>"
            "<se:FeatureTypeStyle><se:Rule><se:PointSymbolizer><se:Graphic>"
            f'<se:ExternalGraphic><se:OnlineResource xlink:href="{svg}"/>'
            "<se:Format>image/svg+xml</se:Format></se:ExternalGraphic>"
            "<se:Size><ogc:Literal>20</ogc:Literal></se:Size></se:Graphic>"
            "</se:PointSymbolizer></se:Rule></se:FeatureTypeStyle></UserStyle>"
            "</NamedLayer></StyledLayerDescriptor>"
        )
        for sld in (burg, se):
            with self.subTest(sld_version(sld)):
                layer = point_layer("towns")
                ok, message = apply_sld_to_layer(layer, sld)
                self.assertTrue(ok, message)
                self.assertEqual(layer.renderer().symbol().symbolLayer(0).size(), 20)

    def test_nonsense_is_reported_not_raised(self):
        layer = point_layer("towns")
        ok, message = apply_sld_to_layer(layer, "<not-a-style/>")
        self.assertFalse(ok)
        self.assertTrue(message)  # QGIS explains itself
        # It named the temporary file, deleted by the time it is read.
        self.assertNotIn(tempfile.gettempdir(), message)

    def test_neither_export_nor_apply_leaves_a_temporary_file(self):
        import glob

        # What these calls leave, not what the directory holds: another
        # process may have its own export folder there.
        before = set(glob.glob(f"{tempfile.gettempdir()}/gsm_sld_*"))
        apply_sld_to_layer(point_layer("towns"), layer_to_sld(point_layer("other")))
        after = set(glob.glob(f"{tempfile.gettempdir()}/gsm_sld_*"))
        self.assertEqual(after - before, set())


class TestStyleableProjectLayers(unittest.TestCase):
    def setUp(self):
        QgsProject.instance().removeAllMapLayers()

    def tearDown(self):
        QgsProject.instance().removeAllMapLayers()

    def test_lists_the_vector_and_raster_layers(self):
        layer = point_layer("towns")
        QgsProject.instance().addMapLayer(layer)
        self.assertEqual(styleable_project_layers(), [layer])

    def test_an_empty_project_is_an_empty_list(self):
        self.assertEqual(styleable_project_layers(), [])

    def test_an_apply_is_offered_no_raster(self):
        """QGIS reads no SLD into a raster ("Layer type 1 not supported"),
        and writes one from it, which a push uploads."""
        folder = tempfile.mkdtemp(prefix="gsm_test_")
        self.addCleanup(shutil.rmtree, folder)
        vector, raster = point_layer("towns"), raster_layer(folder)
        self.assertTrue(raster.isValid())
        QgsProject.instance().addMapLayers([vector, raster])
        self.assertEqual(set(styleable_project_layers()), {vector, raster})
        self.assertEqual(styleable_project_layers(rasters=False), [vector])
        self.assertFalse(apply_sld_to_layer(raster, layer_to_sld(vector))[0])


# ############################################################################
# ##### GeoPackage export ########
# ################################


if __name__ == "__main__":
    unittest.main()
