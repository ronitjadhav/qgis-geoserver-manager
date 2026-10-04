#! python3  # noqa E265

"""
The SLD rules that do not need QGIS, so they are checked by the CI job that has
no QGIS at all.

Usage from the repo root folder:

.. code-block:: bash

    python -m unittest tests.unit.test_sld
"""

import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from geoserver_manager.toolbelt.sld import (
    SLD_1_0,
    SLD_1_1,
    has_raster_symbolizer,
    icon_package,
    icons_for_qgis,
    local_icons,
    relative_hrefs,
    sld_content_type,
    sld_version,
    style_text,
    unresolved_icons,
    utf8_sld,
)

# What QgsMapLayer.saveSldStyle() writes on QGIS 3.40: version 1.1.0, with
# Symbology Encoding elements.
QGIS_SLD = """<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor xmlns="http://www.opengis.net/sld"
 xmlns:se="http://www.opengis.net/se" version="1.1.0">
 <NamedLayer><se:Name>towns</se:Name></NamedLayer>
</StyledLayerDescriptor>"""

# What GeoServer's own demo styles look like.
GEOSERVER_SLD = """<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor xmlns="http://www.opengis.net/sld" version="1.0.0">
 <NamedLayer><Name>roads</Name></NamedLayer>
</StyledLayerDescriptor>"""


class TestSldVersion(unittest.TestCase):
    def test_reads_the_version_attribute(self):
        self.assertEqual(sld_version(QGIS_SLD), "1.1.0")
        self.assertEqual(sld_version(GEOSERVER_SLD), "1.0.0")

    def test_a_document_without_a_version_is_judged_by_its_namespace(self):
        se_only = '<StyledLayerDescriptor xmlns:se="http://www.opengis.net/se"/>'
        self.assertEqual(sld_version(se_only), "1.1.0")
        self.assertEqual(sld_version("<StyledLayerDescriptor/>"), "1.0.0")

    def test_symbology_encoding_elements_count_even_without_the_namespace(self):
        self.assertEqual(sld_version("<sld><se:Rule/></sld>"), "1.1.0")

    def test_a_1_1_1_style_document_is_still_1_1(self):
        self.assertEqual(
            sld_version('<StyledLayerDescriptor version="1.1.1"/>'), "1.1.0"
        )

    def test_nothing_at_all_is_not_a_crash(self):
        self.assertEqual(sld_version(""), "1.0.0")
        self.assertEqual(sld_version(None), "1.0.0")


class TestSldContentType(unittest.TestCase):
    """GeoServer picks its parser from the content type, not from the document."""

    def test_a_qgis_export_needs_the_symbology_encoding_content_type(self):
        self.assertEqual(sld_content_type(QGIS_SLD), SLD_1_1)
        self.assertEqual(SLD_1_1, "application/vnd.ogc.se+xml")

    def test_a_1_0_document_keeps_the_classic_content_type(self):
        self.assertEqual(sld_content_type(GEOSERVER_SLD), SLD_1_0)
        self.assertEqual(SLD_1_0, "application/vnd.ogc.sld+xml")


LATIN1 = (
    '<?xml version="1.0" encoding="ISO-8859-1"?>'
    '<StyledLayerDescriptor version="1.0.0"><Title>Caf\xe9</Title>'
    "</StyledLayerDescriptor>"
)
AS_UTF8 = LATIN1.replace("ISO-8859-1", "UTF-8").encode("utf-8")


class TestEncoding(unittest.TestCase):
    """GeoServer reads an SLD as UTF-8 whatever its declaration names
    (measured on 2.28.5), so every SLD goes as UTF-8, declared as such."""

    def test_a_latin1_body_reads_as_it_declares(self):
        # Decoded as UTF-8 with replacement, "Café" came back "Caf\ufffd".
        self.assertEqual(style_text(LATIN1.encode("latin-1")), LATIN1)

    def test_a_utf8_body_reads_as_utf8_whatever_it_declares(self):
        # GeoServer reads it so: the declaration of such a file is wrong.
        self.assertEqual(style_text(LATIN1.encode("utf-8")), LATIN1)

    def test_an_unknown_encoding_falls_back_to_utf8(self):
        data = b'<?xml version="1.0" encoding="x-nothing"?><a>\xe9</a>'
        self.assertEqual(
            style_text(data), '<?xml version="1.0" encoding="x-nothing"?><a>\ufffd</a>'
        )
        data = b'<?xml version="1.0" encoding="base64"?><a>\xe9</a>'
        self.assertIn("\ufffd", style_text(data))

    def test_latin1_bytes_or_text_go_as_utf8_under_their_declaration_rewritten(self):
        self.assertEqual(utf8_sld(LATIN1.encode("latin-1")), AS_UTF8)
        self.assertEqual(utf8_sld(LATIN1), AS_UTF8)
        self.assertEqual(utf8_sld(LATIN1.encode("utf-8")), AS_UTF8)

    def test_a_utf8_document_is_left_as_it_is(self):
        for document in (GEOSERVER_SLD, "<StyledLayerDescriptor/>"):
            self.assertEqual(
                utf8_sld(document.encode("utf-8")), document.encode("utf-8")
            )
        lower = GEOSERVER_SLD.replace("UTF-8", "utf-8")
        self.assertEqual(utf8_sld(lower), lower.encode("utf-8"))


class TestWhatAnSldHolds(unittest.TestCase):
    def test_a_raster_style_is_told_from_a_vector_one(self):
        # GetLegendGraphic needs a layer of the style's kind.
        self.assertTrue(has_raster_symbolizer("<sld:RasterSymbolizer><ColorMap/>"))
        self.assertTrue(has_raster_symbolizer("<se:RasterSymbolizer>"))
        self.assertFalse(has_raster_symbolizer(GEOSERVER_SLD))
        self.assertFalse(has_raster_symbolizer(None))

    def test_the_files_beside_the_style_are_its_relative_hrefs(self):
        sld = (
            '<OnlineResource xlink:type="simple" xlink:href="icons/pin.png"/>'
            "<se:OnlineResource xlink:href='fill.svg' xlink:type='simple'/>"
            '<OnlineResource xlink:href="icons/pin.png"/>'
            '<OnlineResource xlink:href="file:local.png"/>'
            '<OnlineResource xlink:href="http://example.org/far.png"/>'
            '<OnlineResource xlink:href="file:/data/abs.png"/>'
            '<OnlineResource xlink:href="/data/abs.png"/>'
        )
        self.assertEqual(
            relative_hrefs(sld), ["icons/pin.png", "fill.svg", "file:local.png"]
        )
        self.assertEqual(relative_hrefs(None), [])


def graphic(href):
    return f'<se:OnlineResource xlink:href="{href}" xlink:type="simple"/>'


class TestIconsTravelWithTheStyle(unittest.TestCase):
    """QGIS writes an SVG marker as the path of its file on this machine,
    which GeoServer cannot read (measured on 2.28.5: it drew the fallback
    square). The icons go with the style, in a zip, their hrefs made the
    names they get beside it."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="gsm_test_"))
        self.addCleanup(shutil.rmtree, self.folder)
        (self.folder / "gpsicons").mkdir()
        (self.folder / "a&b").mkdir()
        self.plane = self.folder / "gpsicons" / "plane.svg"
        self.plane.write_bytes(b"<svg>plane</svg>")
        self.other = self.folder / "a&b" / "plane.svg"
        self.other.write_bytes(b"<svg>other</svg>")
        self.png = self.folder / "fill.png"
        self.png.write_bytes(b"\x89PNG")
        # As QGIS 3.44 exports them: the parametric SVG, its relative
        # fallback, a raster marker, an escaped '&' in a folder name.
        self.sld = "".join(
            (
                graphic(f"{self.plane}?fill=%23232323&amp;outline-width=1"),
                graphic("gpsicons/plane.svg"),
                graphic(self.png),
                graphic(str(self.other).replace("&", "&amp;")),
                graphic("burg02.svg"),
                graphic("http://example.org/pin.svg"),
                graphic("ttf://DejaVu%20Sans"),
                graphic(self.folder / "missing.svg"),
            )
        )

    def test_the_local_files_are_found_the_fallback_included(self):
        found = local_icons(self.sld)
        self.assertEqual(
            found,
            {
                f"{self.plane}?fill=%23232323&amp;outline-width=1": self.plane,
                "gpsicons/plane.svg": self.plane,
                str(self.png): self.png,
                str(self.other).replace("&", "&amp;"): self.other,
            },
        )

    def test_the_zip_holds_the_style_and_its_icons_under_new_names(self):
        zipped, document = icon_package(self.sld, "roads")
        package = zipfile.ZipFile(io.BytesIO(zipped))
        self.assertEqual(
            sorted(package.namelist()),
            ["roads_fill.png", "roads_plane.svg", "roads_plane_2.svg", "style.sld"],
        )
        self.assertEqual(package.read("roads_plane.svg"), b"<svg>plane</svg>")
        self.assertEqual(package.read("roads_plane_2.svg"), b"<svg>other</svg>")
        # A replace sends it again after the zip: a zip PUT keeps the version.
        self.assertEqual(document, package.read("style.sld"))
        sld = package.read("style.sld").decode("utf-8")
        self.assertEqual(
            relative_hrefs(sld),
            [
                "roads_plane.svg?fill=%23232323&amp;outline-width=1",
                "roads_plane.svg",
                "roads_fill.png",
                "roads_plane_2.svg",
                "burg02.svg",
            ],
        )
        # What is not a file here is left as it was.
        self.assertIn('xlink:href="http://example.org/pin.svg"', sld)
        self.assertIn(f'xlink:href="{self.folder / "missing.svg"}"', sld)

    def test_a_style_without_local_icons_is_no_zip(self):
        self.assertIsNone(icon_package(graphic("burg02.svg"), "roads"))
        self.assertIsNone(icon_package("<StyledLayerDescriptor/>", "roads"))

    def test_a_name_a_file_cannot_carry_is_made_safe(self):
        package = zipfile.ZipFile(
            io.BytesIO(icon_package(graphic(self.png), "my style/é")[0])
        )
        self.assertIn("my_style___fill.png", package.namelist())

    def test_a_jpeg_goes_as_jpg_the_name_geoserver_unpacks(self):
        """GeoServer unpacks svg, png, jpg, bmp and gif files from a style zip
        (2.28.5): a pin.jpeg was left out, the upload said "uploaded" and no
        icon was drawn. Same bytes, and the Format stays image/jpeg."""
        for name in ("pin.jpeg", "photo.JPEG"):
            with self.subTest(name):
                jpeg = self.folder / name
                jpeg.write_bytes(b"\xff\xd8\xff\xe0 JFIF")
                sld = graphic(jpeg) + "<se:Format>image/jpeg</se:Format>"
                zipped, document = icon_package(sld, "towns")
                stored = f"towns_{jpeg.stem}.jpg"
                package = zipfile.ZipFile(io.BytesIO(zipped))
                self.assertEqual(package.read(stored), jpeg.read_bytes())
                self.assertEqual(
                    document.decode("utf-8"),
                    graphic(stored) + "<se:Format>image/jpeg</se:Format>",
                )


class TestIconsForQgis(unittest.TestCase):
    """QGIS draws a '?' for a relative href (measured on 3.44), and GeoServer
    serves a style's folder without a login."""

    BASE = "http://gs.example.org/geoserver"

    def test_a_relative_icon_is_fetched_from_the_styles_folder(self):
        self.assertEqual(
            icons_for_qgis(graphic("burg02.svg"), self.BASE),
            graphic(f"{self.BASE}/styles/burg02.svg"),
        )
        self.assertEqual(
            icons_for_qgis(graphic("plane.svg?fill=%23ff0000"), self.BASE, "topp"),
            graphic(f"{self.BASE}/styles/topp/plane.svg?fill=%23ff0000"),
        )

    def test_a_path_in_the_servers_style_folder_is_its_url(self):
        # GeoServer's SLD 1.0 rendition of a stored SLD 1.1 style.
        rendition = graphic(
            "file:/opt/geoserver_data/workspaces/topp/styles/plane.svg"
            "?fill=%23232323&amp;fill-opacity=1"
        ) + graphic("file:/opt/geoserver_data/styles/icons/pin.png")
        self.assertEqual(
            icons_for_qgis(rendition, self.BASE, "topp"),
            graphic(
                f"{self.BASE}/styles/topp/plane.svg?fill=%23232323&amp;fill-opacity=1"
            )
            + graphic(f"{self.BASE}/styles/icons/pin.png"),
        )

    def test_urls_fonts_and_local_paths_are_left_alone(self):
        kept = (
            graphic("http://example.org/pin.svg")
            + graphic("ttf://DejaVu%20Sans")
            + graphic("/usr/share/qgis/svg/gpsicons/plane.svg?fill=%23232323")
            + graphic("C:/Users/me/pin.svg")
            + graphic("file:/opt/geoserver_data/usr/share/qgis/svg/plane.svg")
        )
        self.assertEqual(icons_for_qgis(kept, self.BASE, "topp"), kept)

    def test_what_qgis_cannot_open_is_named_once(self):
        with tempfile.NamedTemporaryFile(suffix=".svg") as here:
            sld = (
                graphic("http://example.org/pin.svg")
                + graphic("ttf://DejaVu%20Sans")
                + graphic(here.name)
                + graphic(f"file://{here.name}")
                + graphic("/nowhere/plane.svg?fill=%23232323")
                + graphic("/nowhere/plane.svg")
                + graphic("burg02.svg")
                + graphic("file:/opt/geoserver_data/usr/plane.svg")
            )
            self.assertEqual(
                unresolved_icons(sld),
                [
                    "/nowhere/plane.svg",
                    "burg02.svg",
                    "file:/opt/geoserver_data/usr/plane.svg",
                ],
            )
        self.assertEqual(unresolved_icons(None), [])


if __name__ == "__main__":
    unittest.main()
