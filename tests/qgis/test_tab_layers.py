#! python3  # noqa E265

"""
Usage from the repo root folder:

.. code-block:: bash

    # for whole tests
    python -m unittest tests.qgis.test_tab_layers
    # for specific test
    python -m unittest tests.qgis.test_tab_layers.TestLayersTab.test_lists_every_layer_of_every_type
"""

# standard library
import shutil
from pathlib import Path
from unittest.mock import patch

from qgis.PyQt.QtWidgets import QDialog
from qgis.testing import start_app, unittest

# project
from geoserver_manager.gui import tab_layers
from geoserver_manager.gui.dlg_main import GeoServerMainDialog
from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog
from tests.qgis.sync_dialog import FakePrefs, SyncDialog

start_app()

# ############################################################################
# ########## Classes #############
# ################################

DETAIL = {
    "name": "tasmania_roads",
    "nativeName": "tasmania_roads",
    "srs": "EPSG:4326",
    "enabled": True,
    "advertised": True,
    "projectionPolicy": "FORCE_DECLARED",
    "title": "Tasmania roads",
    "abstract": "Main Tasmania roads",
    "keywords": {"string": ["Roads", "Tasmania"]},
    "nativeBoundingBox": {
        "minx": 145.19,
        "miny": -43.42,
        "maxx": 148.27,
        "maxy": -40.85,
        "crs": "EPSG:4326",
    },
    "attributes": {
        "attribute": [
            {
                "name": "the_geom",
                "binding": "org.locationtech.jts.geom.MultiLineString",
            },
            {"name": "TYPE", "binding": "java.lang.String"},
        ]
    },
}


BASE = "http://gs/geoserver/rest"

# What /rest/layers knows about each layer: GeoServer's type, the segments of
# the resource href (a WMTS resource has none), the store, the default style.
LAYERS = {
    "topp:tasmania_roads": (
        "VECTOR",
        "datastores",
        "taz_shapes",
        "featuretypes",
        "simple_roads",
    ),
    "topp:tasmania_cities": (
        "VECTOR",
        "datastores",
        "taz_shapes",
        "featuretypes",
        "capitals",
    ),
    "sf:sfdem": ("RASTER", "coveragestores", "sfdem", "coverages", "dem"),
    "sf:roads_cascade": ("WMS", "wmsstores", "remote_wms", "wmslayers", ""),
    "sf:tiles": ("WMTS", None, "remote_wmts", None, "raster"),
}
LLBBOX = {
    "minx": -103.87,
    "maxx": -103.62,
    "miny": 44.37,
    "maxy": 44.5,
    "crs": "EPSG:4326",
}
COVERAGE = {
    "name": "sfdem",
    "nativeName": "sfdem",
    "title": "Spearfish DEM",
    "srs": "EPSG:26713",
    "enabled": True,
    "nativeFormat": "GeoTIFF",
    "latLonBoundingBox": LLBBOX,
    "nativeBoundingBox": {
        "minx": 589980,
        "miny": 4913700,
        "maxx": 609000,
        "maxy": 4928010,
        "crs": {"@class": "projected", "$": "EPSG:26713"},
    },
    "grid": {"range": {"low": "0 0", "high": "634 477"}},
    "dimensions": {
        "coverageDimension": {"name": "GRAY_INDEX", "range": {"min": 1000, "max": 2000}}
    },
    "abstract": "Elevation",
}
CASCADED = {
    "name": "roads_cascade",
    "nativeName": "sf:roads",
    "title": "Spearfish roads",
    "srs": "EPSG:26713",
    "enabled": True,
    "latLonBoundingBox": LLBBOX,
    "keywords": {"string": ["roads"]},
    "abstract": "",
}


class Response:
    """What the REST client hands back: a payload, a status, its text."""

    def __init__(self, payload=None, status_code=200):
        self._payload = {} if payload is None else payload
        self.status_code = status_code
        self.text = "" if payload is None else str(payload)

    def json(self):
        return self._payload


class FakeGS:
    """GeoServer as /rest/layers shows it: five layers of four types, plus
    the library calls the tab still makes for vectors, publishing and styles."""

    def __init__(self, broken_detail=None, broken_list=False):
        self.broken_detail = broken_detail
        self.broken_list = broken_list
        self.deleted = []
        self.requests = []
        outer = self

        class Client:
            def get(inner, path, **kwargs):
                outer.requests.append(("get", path))
                return Response(*outer.answer(path))

            def delete(inner, path, **kwargs):
                outer.deleted.append(("DELETE", path, kwargs.get("params")))
                return Response("", 200)

        class Endpoints:
            base_url = BASE

            def coverage(inner, ws, store, name):
                return f"{BASE}/workspaces/{ws}/coveragestores/{store}/coverages/{name}.json"

            def coveragestore(inner, ws, name, method=None, store_type=None):
                base = f"{BASE}/workspaces/{ws}/coveragestores/{name}"
                return f"{base}/{method}.{store_type}" if method else f"{base}.json"

            def featuretype(inner, ws, store, name):
                return f"{BASE}/workspaces/{ws}/datastores/{store}/featuretypes/{name}.json"

            def wmsstores(inner, ws):
                return f"{BASE}/workspaces/{ws}/wmsstores.json"

            def wmtsstores(inner, ws):
                return f"{BASE}/workspaces/{ws}/wmtsstores.json"

            def wmslayers(inner, ws, store):
                return f"{BASE}/workspaces/{ws}/wmsstores/{store}/wmslayers.json"

            def wmslayer(inner, ws, store, name):
                return f"{BASE}/workspaces/{ws}/wmsstores/{store}/wmslayers/{name}.json"

            def wmtslayers(inner, ws, store):
                return f"{BASE}/workspaces/{ws}/wmtsstores/{store}/layers.json"

            def wmtslayer(inner, ws, store, name):
                return f"{BASE}/workspaces/{ws}/wmtsstores/{store}/layers/{name}.json"

        class Rest:
            rest_client = Client()
            rest_endpoints = Endpoints()

        self.rest_service = Rest()

    def answer(self, path):
        """(payload, status) for the raw GETs the tab makes."""
        if path == f"{BASE}/layers.json":
            if self.broken_list:
                return ("boom", 500)
            layers = [{"name": n, "href": f"{BASE}/layers/{n}.json"} for n in LAYERS]
            return ({"layers": {"layer": layers}}, 200)
        if path.startswith(f"{BASE}/layers/"):
            qualified = path[len(f"{BASE}/layers/") : -len(".json")]
            if qualified == self.broken_detail:
                return ("gone", 500)
            if qualified not in LAYERS:
                return ("no such layer", 404)
            kind, stores, store, resources, style = LAYERS[qualified]
            ws, _, name = qualified.rpartition(":")
            resource = {"name": qualified}
            if stores:
                # Another host on purpose: behind a proxy GeoServer writes its
                # own idea of the base URL, so the href must never be followed.
                resource["href"] = (
                    f"http://inside:8080/geoserver/rest/workspaces/{ws}/{stores}/"
                    f"{store}/{resources}/{name}.json"
                )
            layer = {"name": name, "type": kind, "resource": resource}
            layer["defaultStyle"] = {"name": style}
            return ({"layer": layer}, 200)
        if path == f"{BASE}/workspaces/sf/wmsstores.json":
            return ({"wmsStores": ""}, 200)
        if path == f"{BASE}/workspaces/sf/wmtsstores.json":
            return ({"wmtsStores": {"wmtsStore": [{"name": "remote_wmts"}]}}, 200)
        if path == f"{BASE}/workspaces/sf/wmtsstores/remote_wmts/layers.json":
            return ({"wmtsLayers": {"wmtsLayer": [{"name": "tiles"}]}}, 200)
        if path == f"{BASE}/workspaces/sf/wmtsstores/remote_wmts/layers/tiles.json":
            detail = dict(CASCADED, name="tiles", nativeName="topp:states")
            return ({"wmtsLayer": detail}, 200)
        if "/wmsstores/" in path and "/wmslayers/" in path:
            # A cascaded WMS layer is read raw, as GeoServer writes it (row 65).
            name = path.rsplit("/", 1)[1][: -len(".json")]
            return ({"wmsLayer": dict(CASCADED, name=name)}, 200)
        if path.endswith("/coveragestores/sfdem/coverages/sfdem.json"):
            return ({"coverage": COVERAGE}, 200)
        if "/featuretypes/" in path:
            # As GeoServer answers it; a subclass's get_feature_type() says what.
            parts = path[len(f"{BASE}/workspaces/") : -len(".json")].split("/")
            detail, status = self.get_feature_type(parts[0], parts[2], parts[4])
            return ({"featureType": detail}, status)
        raise AssertionError(f"unexpected GET {path}")

    # -- the library calls the tab still makes --

    def get_workspaces(self):
        return ([{"name": "topp"}, {"name": "empty"}], 200)

    def get_datastores(self, workspace_name):
        if workspace_name == "empty":
            return ([], 200)
        return ([{"name": "taz_shapes"}], 200)

    def get_feature_type(self, workspace_name, datastore_name, name):
        return (dict(DETAIL, name=name), 200)

    def get_wms_layer(self, workspace_name, store_name, name):
        return (dict(CASCADED, name=name), 200)

    def delete_feature_type(self, workspace_name, datastore_name, name):
        self.deleted.append((workspace_name, datastore_name, name))
        return ("", 200)

    def delete_wms_layer(self, workspace_name, store_name, name):
        self.deleted.append(("wms", workspace_name, store_name, name))
        return ("", 200)


class TestLayersTab(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.warnings = []
        self.dlg.show_warning_message = self.warnings.append
        self.dlg.show_error_message = lambda text: self.fail(
            f"unexpected error: {text}"
        )
        self.dlg.show_success_message = lambda text: None
        self.dlg.gs = FakeGS()

    def test_lists_every_layer_of_every_type(self):
        self.dlg._load_layers()

        self.assertEqual(
            self.dlg._all_rows,
            [
                ["roads_cascade", "sf", "WMS", "remote_wms", "-"],
                ["sfdem", "sf", "RASTER", "sfdem", "dem"],
                ["tiles", "sf", "WMTS", "remote_wmts", "raster"],
                ["tasmania_cities", "topp", "VECTOR", "taz_shapes", "capitals"],
                ["tasmania_roads", "topp", "VECTOR", "taz_shapes", "simple_roads"],
            ],
        )
        headers = [
            self.dlg.resultsTable.horizontalHeaderItem(col).text()
            for col in range(self.dlg.resultsTable.columnCount())
        ]
        self.assertEqual(
            headers,
            ["Name", "Workspace", "Type", "Store", "Default style", "Actions"],
        )
        self.assertEqual(self.warnings, [])
        # one request for the list, one per layer; the datastores are not walked
        gets = [path for verb, path in self.dlg.gs.requests if verb == "get"]
        self.assertEqual(gets.count(f"{BASE}/layers.json"), 1)
        self.assertEqual([path for path in gets if "/datastores/" in path], [])

    def test_the_type_reads_in_words_and_the_row_keeps_geoservers(self):
        # "VECTOR" and "WMS" beside every other tab's readable names; the row
        # actions read the enum, so only the cell changes.
        self.dlg._load_layers()
        shown = [
            self.dlg.resultsTable.item(row, 2).text()
            for row in range(self.dlg.resultsTable.rowCount())
        ]
        self.assertEqual(
            shown, ["Cascaded WMS", "Raster", "Cascaded WMTS", "Vector", "Vector"]
        )
        self.assertEqual(self.dlg._all_rows[0][2], "WMS")
        self.dlg.searchBox.setText("cascaded")  # what the cell says
        self.dlg._apply_filter()
        self.assertEqual(len(self.dlg._filtered_rows), 2)

    def test_the_store_is_read_off_the_href_and_the_href_never_followed(self):
        self.dlg._load_layers()
        self.assertEqual(
            [path for _verb, path in self.dlg.gs.requests if "inside:8080" in path], []
        )
        store_from_href = tab_layers.LayerTabMixin._store_from_href
        self.assertEqual(
            store_from_href(
                "http://inside:8080/geoserver/rest/workspaces/sf/coveragestores/"
                "my%20dem/coverages/x.json"
            ),
            "my dem",
        )
        self.assertIsNone(store_from_href(None))
        self.assertIsNone(store_from_href("http://gs/geoserver/rest/styles/x.json"))

    def test_one_unreadable_layer_still_gets_a_row(self):
        self.dlg.gs = FakeGS(broken_detail="topp:tasmania_cities")
        self.dlg._load_layers()

        rows = {row[0]: row for row in self.dlg._all_rows}
        self.assertEqual(rows["tasmania_cities"][2:], ["-", "-", "-"])  # placeholders
        self.assertEqual(rows["tasmania_roads"][2], "VECTOR")
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("topp:tasmania_cities", self.warnings[0])

    def test_an_unreadable_list_fails_the_load(self):
        self.dlg.gs = FakeGS(broken_list=True)
        with self.assertRaises(RuntimeError):
            self.dlg._fetch_layer_rows()

    def test_name_and_workspace_columns_are_links(self):
        self.dlg._load_layers()
        self.assertIsNotNone(self.dlg._cell_click_callback(0))  # name
        self.assertIsNotNone(self.dlg._cell_click_callback(1))  # workspace
        self.assertIsNone(self.dlg._cell_click_callback(2))  # type is plain

    def test_delete_goes_through_each_types_own_call(self):
        self.dlg._load_layers()
        confirmed = {}
        self.dlg._confirm_delete = (
            lambda question, labels=(), cascade="": confirmed.update(
                question=question, labels=labels, cascade=cascade
            )
            or True
        )
        rows = {row[0]: row for row in self.dlg._all_rows}

        self.dlg._delete_selected_layers(
            [
                rows["tasmania_cities"],
                rows["sfdem"],
                rows["roads_cascade"],
                rows["tiles"],
            ]
        )

        deleted = self.dlg.gs.deleted
        self.assertIn(("topp", "taz_shapes", "tasmania_cities"), deleted)  # library
        self.assertIn(("wms", "sf", "remote_wms", "roads_cascade"), deleted)  # library
        raw = [(entry[1], entry[2]) for entry in deleted if entry[0] == "DELETE"]
        self.assertIn(
            (
                f"{BASE}/workspaces/sf/coveragestores/sfdem/coverages/sfdem.json",
                {"recurse": "true"},
            ),
            raw,
        )
        self.assertIn(
            (
                f"{BASE}/workspaces/sf/wmtsstores/remote_wmts/layers/tiles.json",
                {"recurse": "true"},
            ),
            raw,
        )
        self.assertEqual(
            confirmed["labels"],
            ["topp:tasmania_cities", "sf:sfdem", "sf:roads_cascade", "sf:tiles"],
        )
        self.assertIn("layer group", confirmed["cascade"])  # recurse=true is stated


class TestEveryLayerType(unittest.TestCase):
    """The row's type decides which resource the actions read, and which
    protocols make sense for it."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        # nothing listens here
        self.dlg.plg_settings = FakePrefs(
            "http://127.0.0.1:1/geoserver", credentials=("", "")
        )
        self.dlg.show_error_message = lambda text: self.fail(f"unexpected: {text}")
        self.dlg.show_warning_message = lambda text: None
        self.dlg._load_layers()
        self.rows = {row[0]: row for row in self.dlg._all_rows}

    def opened(self, action, row):
        opened = []

        class Recording(ResourceFormDialog):
            def exec(inner):
                opened.append(inner)
                return QDialog.DialogCode.Rejected

        with patch.object(tab_layers, "ResourceFormDialog", Recording):
            action(row)
        return opened[0]

    def test_a_raster_layer_shows_its_coverage(self):
        form = self.opened(self.dlg._show_layer_info, self.rows["sfdem"])
        self.assertEqual(form.get_widget("size").text(), "634 × 477")
        self.assertEqual(form.get_widget("srs").text(), "EPSG:26713")
        self.assertIn("GRAY_INDEX", form.get_widget("bands").toPlainText())
        self.assertIsNone(form.get_widget("coverage"))  # the picker stays home
        self.assertIsNone(form.get_widget("cql_filter"))  # a vector's only
        self.assertTrue(form.get_widget("enabled").isChecked())  # editable now

    def test_a_cascaded_layer_shows_its_remote_details(self):
        form = self.opened(self.dlg._show_layer_info, self.rows["roads_cascade"])
        self.assertEqual(form.get_widget("native_name").text(), "sf:roads")
        self.assertEqual(form.get_widget("title").text(), "Spearfish roads")
        self.assertIsNone(form.get_widget("layer"))
        form = self.opened(self.dlg._show_layer_info, self.rows["tiles"])
        self.assertEqual(form.get_widget("native_name").text(), "topp:states")

    def test_a_vector_layers_cql_filter_and_title_are_shown(self):
        # The library's FeatureType drops cqlFilter, and the title when an
        # internationalTitle is set: the form showed an existing filter as
        # empty, and emptying the field changed nothing.
        stored = dict(
            DETAIL,
            cqlFilter="name = 'a'",
            title="Roads",
            internationalTitle={"fr": "Routes"},
        )
        gs = self.dlg.gs
        gs.get_feature_type = lambda ws, ds, name: (
            {k: v for k, v in stored.items() if k not in ("cqlFilter", "title")},
            200,
        )
        answer = gs.answer
        gs.answer = lambda path: (
            ({"featureType": stored}, 200) if "/featuretypes/" in path else answer(path)
        )
        form = self.opened(self.dlg._show_layer_info, self.rows["tasmania_roads"])
        self.assertEqual(form.get_widget("cql_filter").text(), "name = 'a'")
        self.assertEqual(form.get_widget("title").text(), "Roads")

    def test_a_vector_layer_keeps_the_feature_type_view(self):
        form = self.opened(self.dlg._show_layer_info, self.rows["tasmania_roads"])
        self.assertIn("the_geom", form.get_widget("attributes").toPlainText())
        self.assertEqual(form.get_widget("datastore").text(), "taz_shapes")
        # Editable flags, and an abstract with room for prose.
        self.assertTrue(form.get_widget("enabled").isChecked())
        self.assertTrue(hasattr(form.get_widget("abstract"), "toPlainText"))

    def test_add_to_qgis_offers_wfs_only_for_vectors(self):
        for name, expected in (
            ("tasmania_roads", ["WMS", "WFS", "WMTS"]),
            ("sfdem", ["WMS", "WMTS"]),
            ("roads_cascade", ["WMS", "WMTS"]),
            ("tiles", ["WMS", "WMTS"]),
        ):
            combo = self.opened(
                self.dlg._add_layer_to_qgis, self.rows[name]
            ).get_widget("protocol")
            self.assertEqual(
                [combo.itemText(i) for i in range(combo.count())], expected, name
            )

    def test_add_to_qgis_defaults_to_wfs_for_a_vector_and_wms_otherwise(self):
        for name, expected in (("tasmania_roads", "WFS"), ("sfdem", "WMS")):
            combo = self.opened(
                self.dlg._add_layer_to_qgis, self.rows[name]
            ).get_widget("protocol")
            self.assertEqual(combo.currentText(), expected, name)

    def test_the_browser_preview_frames_any_type_on_its_extent(self):
        opened = []
        with patch.object(
            tab_layers.QDesktopServices,
            "openUrl",
            lambda url: opened.append(url.toString()) or True,
        ):
            self.dlg._preview_layer_in_browser(self.rows["sfdem"])
            self.dlg._preview_layer_in_browser(self.rows["tiles"])
        self.assertEqual(len(opened), 2)
        for url, layer in zip(opened, ("sf:sfdem", "sf:tiles")):
            self.assertTrue(url.startswith("http://127.0.0.1:1/geoserver/sf/wms?"), url)
            self.assertIn(f"layers={layer}", url)
            self.assertIn("bbox=-103.87,44.37,-103.62,44.5", url)

    def test_the_map_preview_opens_a_window_without_reading_the_resource(self):
        # The WMS layer reads its extent from the capabilities (measured on
        # 2.28.5): the resource GET for latLonBoundingBox was a second read.
        # Against this fake the layer is invalid, so the window gets no box.
        windows, reads = [], []

        class Window:
            def __init__(inner, title, layer, bbox=None, parent=None):
                windows.append((title, layer, bbox, parent))

            def show(inner):
                pass

        with (
            patch.object(tab_layers, "LayerPreviewDialog", Window),
            patch.object(
                self.dlg, "_layer_resource", lambda row: reads.append(row) or {}
            ),
        ):
            self.dlg._preview_layer(self.rows["sfdem"])

        title, layer, bbox, parent = windows[0]
        self.assertEqual(title, "sf:sfdem")
        self.assertEqual(reads, [])
        self.assertIsNone(bbox)
        self.assertIs(parent, self.dlg)
        # built like Add to QGIS builds it, never added to the project
        self.assertIn("layers=sfdem&", layer.source())
        self.assertIn("url=http://127.0.0.1:1/geoserver/sf/ows", layer.source())
        from qgis.core import QgsProject

        self.assertNotIn(layer.id(), QgsProject.instance().mapLayers())


class TestLayerDetailPrefill(unittest.TestCase):
    """The view is built from what the server returned, not from the row."""

    def test_flattens_the_interesting_fields(self):
        values = GeoServerMainDialog._layer_form_values(
            ["tasmania_roads", "topp", "VECTOR", "taz_shapes", "simple_roads"], DETAIL
        )

        self.assertEqual(values["native_name"], "tasmania_roads")
        self.assertEqual(values["projection_policy"], "FORCE_DECLARED")
        self.assertEqual(values["keywords"], ["Roads", "Tasmania"])
        self.assertIn("145.19", values["bbox"])
        self.assertIn("(EPSG:4326)", values["bbox"])  # one format on every tab
        self.assertIn("the_geom : MultiLineString", values["attributes"])
        self.assertIn("TYPE : String", values["attributes"])
        self.assertIs(values["enabled"], True)

    def test_survives_a_sparse_payload(self):
        values = GeoServerMainDialog._layer_form_values(
            ["l", "ws", "VECTOR", "ds", ""], {}
        )

        self.assertEqual(values["name"], "l")
        self.assertEqual(values["bbox"], "-")
        self.assertEqual(values["attributes"], "-")
        self.assertEqual(values["keywords"], [])

    def test_handles_translated_title_and_single_attribute(self):
        detail = {
            "title": {"en": "Roads", "fr": "Routes"},
            "keywords": "Solo",
            "attributes": {"attribute": {"name": "geom", "binding": "a.b.Point"}},
        }
        values = GeoServerMainDialog._layer_form_values(
            ["l", "ws", "VECTOR", "ds", ""], detail
        )

        self.assertIn("en: Roads", values["title"])
        self.assertEqual(values["keywords"], ["Solo"])
        self.assertEqual(values["attributes"], "geom : Point")


class TestLibraryPayloadShape(unittest.TestCase):
    """geoservercloud normalises the payload; raw REST does not. Both must work.

    Caught on a live server: FeatureType.asdict() returns attributes and
    keywords as lists, while /featuretypes/x.json wraps them in
    {"attribute": [...]} / {"string": [...]}.
    """

    LIBRARY_SHAPE = {
        "name": "tasmania_roads",
        "srs": "EPSG:4326",
        "keywords": ["Roads", "Tasmania"],
        "attributes": [
            {
                "name": "the_geom",
                "binding": "org.locationtech.jts.geom.MultiLineString",
                "nillable": True,
            },
            {"name": "TYPE", "binding": "java.lang.String"},
        ],
        "nativeBoundingBox": {"minx": 145.19, "crs": "EPSG:4326"},
    }

    def test_library_normalised_lists(self):
        values = GeoServerMainDialog._layer_form_values(
            ["tasmania_roads", "topp", "VECTOR", "taz_shapes", ""], self.LIBRARY_SHAPE
        )
        self.assertEqual(values["keywords"], ["Roads", "Tasmania"])
        self.assertIn("the_geom : MultiLineString", values["attributes"])
        self.assertIn("TYPE : String", values["attributes"])

    def test_attribute_without_a_binding(self):
        values = GeoServerMainDialog._layer_form_values(
            ["l", "ws", "VECTOR", "ds", ""], {"attributes": [{"name": "plain"}]}
        )
        self.assertEqual(values["attributes"], "plain : ")


class TestAddToQgis(unittest.TestCase):
    """URIs carry the auth config id, never a password; the right provider is used."""

    BASE = "http://gs.example.org/geoserver/"  # trailing slash must not matter

    def test_wms_uri(self):
        uri, provider = GeoServerMainDialog._layer_uri(
            "WMS", self.BASE, "topp:roads", "abc123"
        )
        self.assertEqual(provider, "wms")
        self.assertIn("layers=roads&", uri)
        self.assertIn("url=http://gs.example.org/geoserver/topp/ows&", uri)
        self.assertIn("authcfg=abc123", uri)
        self.assertNotIn("//geoserver//", uri)

    def test_plain_credentials_go_in_the_source_when_there_is_no_auth_config(self):
        # The default store: as QGIS keeps a Basic connection's own.
        creds = ("admin", "p@ss word")
        for protocol in ("WMS", "WMTS"):
            uri, _ = GeoServerMainDialog._layer_uri(
                protocol, self.BASE, "topp:roads", "", creds
            )
            self.assertIn("&username=admin&password=p%40ss%20word", uri)
            self.assertNotIn("authcfg", uri)
        wfs, _ = GeoServerMainDialog._layer_uri(
            "WFS", self.BASE, "topp:roads", "", creds
        )
        self.assertIn("user='admin'", wfs)
        self.assertIn("password='p@ss word'", wfs)
        # Encrypted, the auth config id goes instead, and no password.
        wms, _ = GeoServerMainDialog._layer_uri(
            "WMS", self.BASE, "topp:roads", "abc123", creds
        )
        self.assertIn("authcfg=abc123", wms)
        self.assertNotIn("password", wms)

    def test_a_layer_goes_through_its_workspaces_own_service(self):
        """An isolated workspace's layers are in no global capabilities, so
        the global service could not reach any of them (measured)."""
        wms, _ = GeoServerMainDialog._layer_uri("WMS", self.BASE, "iso:roads")
        wmts, _ = GeoServerMainDialog._layer_uri("WMTS", self.BASE, "iso:roads")
        wfs, _ = GeoServerMainDialog._layer_uri("WFS", self.BASE, "iso:roads")
        # bare for WMS and WMTS: a qualified name is not in those capabilities
        self.assertIn("layers=roads&", wms)
        self.assertIn("url=http://gs.example.org/geoserver/iso/ows&", wms)
        self.assertNotIn("authcfg", wms)  # no auth config, no login: anonymous
        self.assertNotIn("username", wms)
        self.assertIn("layers=roads&", wmts)
        self.assertIn("url=http://gs.example.org/geoserver/iso/gwc/service/wmts?", wmts)
        self.assertIn("url='http://gs.example.org/geoserver/iso/ows'", wfs)
        self.assertIn("typename='iso:roads'", wfs)
        # a global layer group has no workspace: the global service
        group, _ = GeoServerMainDialog._layer_uri("WMS", self.BASE, "tasmania")
        self.assertIn("layers=tasmania&", group)
        self.assertIn("url=http://gs.example.org/geoserver/ows&", group)

    def test_qgis_stays_on_the_plugins_url_behind_a_proxy(self):
        """GetMap went to the address the capabilities advertise, an inside
        one behind a proxy: the layer was valid and drew nothing. A WMTS
        identify went there too, with the saved credentials (measured)."""
        wms, _ = GeoServerMainDialog._layer_uri("WMS", self.BASE, "topp:roads")
        wmts, _ = GeoServerMainDialog._layer_uri("WMTS", self.BASE, "topp:roads")
        self.assertIn("&IgnoreGetMapUrl=1&IgnoreGetFeatureInfoUrl=1", wms)
        self.assertIn("&IgnoreGetMapUrl=1&IgnoreGetFeatureInfoUrl=1", wmts)

    @staticmethod
    def advertising(url, base="http://gs.example.org/geoserver/", authcfg="abc123"):
        """A dialog connected to `base` whose GeoServer advertises its WFS at
        `url`, the WFS 1.1.0 capabilities as the library parses them."""

        endpoint = {"@xlink:href": url}
        http = {"ows:HTTP": {"ows:Get": endpoint, "ows:Post": endpoint}}

        class Ows:
            asked = []

            def get_wfs_capabilities(self, workspace_name):
                Ows.asked.append(workspace_name)
                operations = [
                    {"@name": name, "ows:DCP": http}
                    for name in ("GetCapabilities", "DescribeFeatureType", "GetFeature")
                ]
                return {
                    "wfs:WFS_Capabilities": {
                        "ows:OperationsMetadata": {"ows:Operation": operations}
                    }
                }

        class GS:
            ows_service = Ows()

        dlg = SyncDialog()
        dlg.gs = GS()
        dlg.plg_settings = FakePrefs(base, credentials=("", ""), auth_cfg_id=authcfg)
        return dlg

    def test_a_wfs_advertised_elsewhere_is_never_built(self):
        """The WFS provider sent DescribeFeatureType and GetFeature, with the
        saved credentials, to the address the capabilities advertise, over
        plain HTTP when that one said http (measured): nothing keeps it on
        the plugin's URL, so such a layer is refused before QGIS asks."""

        class Accepting(ResourceFormDialog):
            def exec(self):
                return QDialog.DialogCode.Accepted

        dlg = self.advertising("http://inside:8080/geoserver/topp/wfs")
        built, errors = [], []
        dlg.show_error_message = errors.append
        with (
            patch.object(tab_layers, "ResourceFormDialog", Accepting),
            patch.object(tab_layers, "QgsVectorLayer", lambda *a: built.append(a)),
        ):
            dlg._add_layer_to_qgis(["roads", "topp", "VECTOR", "ds", ""])
        self.assertEqual(built, [])
        self.assertEqual(dlg.gs.ows_service.asked, ["topp"])
        self.assertIn("Could not add 'roads'", errors[0])
        self.assertIn("advertises its WFS at http://inside:8080,", errors[0])
        self.assertIn("Proxy base URL", errors[0])

    def test_a_wfs_advertised_where_the_plugin_connects_is_built(self):
        # Another path and the default port spelt out: the same origin.
        dlg = self.advertising("http://GS.example.org:80/geoserver/topp/wfs")
        built = []
        with patch.object(tab_layers, "QgsVectorLayer", lambda *a: built.append(a)):
            dlg._server_layer("WFS", "topp:roads", "roads")
        self.assertEqual(len(built), 1)
        self.assertIn("typename='topp:roads'", built[0][0])

    def test_an_invalid_layer_gives_the_providers_reason_as_text(self):
        """The layer's own error is HTML: its <p> blocks pushed the reason
        below the banner's fold and the log showed the tags."""
        from qgis.core import QgsError

        class Provider:
            def lastError(self):  # noqa: N802
                return ""

            def error(self):
                return QgsError("Cannot calculate extent", "WMS provider")

        class Layer:
            def isValid(self):  # noqa: N802
                return False

            def dataProvider(self):  # noqa: N802
                return Provider()

            def error(self):
                error = QgsError("Cannot calculate extent", "WMS provider")
                error.append("Provider is not valid (provider: wms, URI: x", "Raster")
                return error

        with self.assertRaises(RuntimeError) as raised:
            GeoServerMainDialog._valid_layer(Layer())
        self.assertEqual(str(raised.exception), "Cannot calculate extent")

    def test_wmts_goes_through_geowebcache(self):
        uri, provider = GeoServerMainDialog._layer_uri(
            "WMTS", self.BASE, "topp:roads", "abc123"
        )
        self.assertEqual(provider, "wms")
        self.assertIn("geoserver/topp/gwc/service/wmts", uri)
        self.assertIn("tileMatrixSet=EPSG:900913", uri)
        self.assertIn("authcfg=abc123", uri)
        # a crs= of its own made QGIS reproject every 900913 tile (measured)
        self.assertNotIn("crs=", uri)

    def test_wfs_uri_uses_the_datasource_uri_and_no_password(self):
        uri, provider = GeoServerMainDialog._layer_uri(
            "WFS", self.BASE, "topp:roads", "abc123"
        )
        self.assertEqual(provider, "WFS")
        self.assertIn("typename='topp:roads'", uri)
        self.assertIn("authcfg=abc123", uri)
        self.assertIn("pagingEnabled='true'", uri)
        self.assertNotIn("password", uri.lower())
        # No srsname: the features arrive in the type's own CRS (measured).
        self.assertNotIn("srsname", uri)

    def test_unknown_protocol_is_refused(self):
        with self.assertRaises(ValueError):
            GeoServerMainDialog._layer_uri("FTP", self.BASE, "x:y")

    def test_unreachable_layer_is_a_banner_not_a_project_entry(self):
        from qgis.core import QgsProject

        class Accepting(ResourceFormDialog):
            def exec(self):
                return QDialog.DialogCode.Accepted

        # Nothing listens there; the capabilities check passes, QGIS then fails.
        dlg = self.advertising(
            "http://127.0.0.1:1/geoserver/topp/wfs",
            base="http://127.0.0.1:1/geoserver",
            authcfg="",
        )
        errors = []
        dlg.show_error_message = errors.append
        before = len(QgsProject.instance().mapLayers())
        with patch.object(tab_layers, "ResourceFormDialog", Accepting):
            dlg._add_layer_to_qgis(["roads", "topp", "VECTOR", "ds", ""])

        self.assertEqual(len(errors), 1)
        self.assertIn("Could not add 'roads'", errors[0])
        self.assertIn("on the WFS tab", errors[0])
        self.assertEqual(len(QgsProject.instance().mapLayers()), before)


class PublishFakeGS(FakeGS):
    """Adds the pieces the publish flow touches: raw REST for ?list=available,
    the layer list's existence check, and a create that records what it was
    asked. A table is taken where LAYERS publishes it: topp:tasmania_roads
    comes from store taz_shapes."""

    def __init__(self):
        super().__init__()
        self.created = []
        outer = self

        class Client:
            def get(inner, path, **kwargs):
                if path.startswith(f"{BASE}/layers/"):
                    return Response(*outer.answer(path))
                outer.last_query = (path, kwargs.get("params"))
                return Response({"list": {"string": ["plugin_demo", "another"]}})

        class Endpoints:
            base_url = BASE

            def featuretypes(inner, ws, ds):
                return f"/rest/workspaces/{ws}/datastores/{ds}/featuretypes.json"

        class Rest:
            rest_client = Client()
            rest_endpoints = Endpoints()

            def resource_exists(inner, path):
                return inner.rest_client.get(path).status_code == 200

            def create_feature_type(inner, feature_type):
                # What GeoServer would receive: the real model's payload.
                outer.created.append(feature_type.post_payload()["featureType"])
                return ("", 201)

        self.rest_service = Rest()

    def get_feature_type(self, ws, ds, name):
        published = LAYERS.get(f"{ws}:{name}")
        if published and published[2] == ds:
            return ({"name": name}, 200)  # taken in this store
        return ("<html>Not Found</html>", 404)


class Rejecting(ResourceFormDialog):
    """A form that records itself and closes as if cancelled."""

    opened = []

    def exec(self):
        Rejecting.opened.append(self)
        return QDialog.DialogCode.Rejected


class TestPublish(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = PublishFakeGS()
        self.dlg.show_warning_message = lambda t: None

    def test_available_tables_uses_the_list_available_query(self):
        tables = self.dlg._available_tables("topp", "pg")
        self.assertEqual(tables, ["another", "plugin_demo"])
        path, params = self.dlg.gs.last_query
        self.assertTrue(path.endswith("/topp/datastores/pg/featuretypes.json"))
        self.assertEqual(params, {"list": "available"})

    def test_available_tables_tolerates_odd_payloads(self):
        for payload, expected in (
            ({"list": {"string": "solo"}}, ["solo"]),  # one table is unwrapped
            ({"list": ""}, []),  # none available
            ("not json at all", []),
        ):

            class R:
                status_code = 200

                def json(inner, p=payload):
                    return p

            self.dlg.gs.rest_service.rest_client.get = lambda *a, **k: R()
            self.assertEqual(self.dlg._available_tables("w", "d"), expected)

    def test_the_srs_must_be_an_epsg_number_on_the_first_tab_with_no_default(self):
        """4326 as a default was usually wrong for a projected table."""
        field = next(
            f for f in self.dlg._publish_fields(["topp"]) if f["key"] == "epsg"
        )
        self.assertTrue(field["required"])
        self.assertNotIn("default", field)
        self.assertNotIn("group", field)
        for bad in ("", "abc", "EPSG:"):
            with self.assertRaises(ValueError, msg=bad):
                self.dlg._publish_table(
                    {
                        "workspace": "topp",
                        "datastore": "pg",
                        "table": "plugin_demo",
                        "epsg": bad,
                        "title": "",
                        "abstract": "",
                        "keywords": [],
                    }
                )
        self.dlg._publish_table(
            {
                "workspace": "topp",
                "datastore": "pg",
                "table": "plugin_demo",
                "epsg": "EPSG:3857",  # tolerated, the number is what counts
                "title": "",
                "abstract": "",
                "keywords": [],
            }
        )
        self.assertEqual(self.dlg.gs.created[-1]["srs"], "EPSG:3857")

    def test_publish_sends_what_the_form_collected_with_any_epsg_code(self):
        """The library's own call raised KeyError for any code but 2056, 4326
        and 3857, and gave those a world bounding box."""
        self.dlg._publish_table(
            {
                "workspace": "topp",
                "datastore": "pg",
                "table": "plugin_demo",
                "epsg": 25832,
                "title": "Demo",
                "abstract": "",
                "keywords": ["a", "b", "", "c "],
            }
        )
        sent = self.dlg.gs.created[0]
        self.assertEqual(sent["name"], "plugin_demo")
        self.assertEqual(sent["store"], {"name": "topp:pg"})
        self.assertEqual(sent["srs"], "EPSG:25832")
        self.assertNotIn("nativeBoundingBox", sent)  # GeoServer computes it
        self.assertNotIn("latLonBoundingBox", sent)
        self.assertEqual(sent["title"], "Demo")
        self.assertNotIn("abstract", sent)  # empty is not sent, not ""
        self.assertEqual(sent["keywords"], {"string": ["a", "b", "c"]})

    def test_publish_refuses_an_existing_layer(self):
        with self.assertRaises(ValueError) as ctx:
            self.dlg._publish_table(
                {"workspace": "topp", "datastore": "pg", "table": "tasmania_roads"}
            )
        self.assertIn("already exists", str(ctx.exception))
        self.assertEqual(self.dlg.gs.created, [])  # upsert never reached

    def test_a_bad_table_publish_is_refused_in_the_form(self):
        """The Table list offers the tables unpublished in *this* store, so
        the feature type check always passed; the POST then failed on the
        workspace-wide layer name, after the form had closed and the title,
        abstract and keywords typed were lost."""
        with patch.object(tab_layers, "ResourceFormDialog", Rejecting):
            self.dlg._publish_layer()
        values = {
            "source": tab_layers._SOURCE_TABLE,
            "workspace": "topp",
            "datastore": "pg",
            "table": "tasmania_roads",  # published from taz_shapes
            "epsg": "4326",
        }
        with self.assertRaises(ValueError) as ctx:
            Rejecting.opened[-1]._validate(values)
        self.assertIn("topp:tasmania_roads", str(ctx.exception))
        self.assertIn("taz_shapes", str(ctx.exception))
        with self.assertRaises(ValueError):
            Rejecting.opened[-1]._validate(
                dict(values, table="plugin_demo", epsg="abc")
            )
        Rejecting.opened[-1]._validate(dict(values, table="plugin_demo"))
        self.assertEqual(self.dlg.gs.created, [])

    def test_a_taken_name_from_the_qgis_project_is_refused_in_the_form(self):
        """The Metadata tab is on screen for this source too: refused after
        the form closed, the title, abstract and keywords typed were lost."""
        from qgis.core import QgsProject, QgsVectorLayer

        layer = QgsVectorLayer("Point?crs=epsg:4326", "Roads (2024)", "memory")
        QgsProject.instance().addMapLayers([layer])
        self.addCleanup(QgsProject.instance().removeAllMapLayers)
        self.dlg.gs = GpkgPublishFakeGS(datastore_exists=True)
        with patch.object(tab_layers, "ResourceFormDialog", Rejecting):
            self.dlg._publish_layer(layer=layer)
        values = {
            "source": tab_layers._SOURCE_QGIS,
            "workspace": "topp",
            "qgis_layer": layer,
            "name": "Roads (2024)",
            "replace": False,
            "title": "Main roads",
        }
        with self.assertRaises(ValueError) as ctx:
            Rejecting.opened[-1]._validate(values)
        self.assertIn("Datastore 'Roads_2024' already exists", str(ctx.exception))
        Rejecting.opened[-1]._validate(dict(values, replace=True))
        self.assertEqual(self.dlg.gs.style_calls, [])  # the check only reads

    def test_dialog_combos_cascade_from_the_workspace(self):
        opened = []

        class Recording(ResourceFormDialog):
            def exec(self):
                opened.append(self)
                return QDialog.DialogCode.Rejected

        with patch.object(tab_layers, "ResourceFormDialog", Recording):
            self.dlg._publish_layer()

        form = opened[0]
        ws, ds, table = (
            form.get_widget(k) for k in ("workspace", "datastore", "table")
        )
        self.assertEqual([ws.itemText(i) for i in range(ws.count())], ["topp", "empty"])
        self.assertEqual([ds.itemText(i) for i in range(ds.count())], ["taz_shapes"])
        self.assertEqual(
            [table.itemText(i) for i in range(table.count())],
            ["another", "plugin_demo"],
        )
        # switching to a workspace with no datastores empties both dependants
        ws.setCurrentText("empty")
        self.assertEqual(ds.count(), 0)
        self.assertEqual(table.count(), 0)

    def test_a_raster_hides_the_style_option_it_ignores(self):
        from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer

        vector = QgsVectorLayer("Point?crs=epsg:4326", "points", "memory")
        raster = QgsRasterLayer("/nonexistent.tif", "dem")
        QgsProject.instance().addMapLayers([vector, raster], False)
        self.addCleanup(QgsProject.instance().removeAllMapLayers)
        opened = []

        class Recording(ResourceFormDialog):
            def exec(self):
                opened.append(self)
                return QDialog.DialogCode.Rejected

        with patch.object(tab_layers, "ResourceFormDialog", Recording):
            self.dlg._publish_layer(layer=vector)
        form = opened[0]
        self.assertNotIn("with_style", form._hidden_keys)
        form.get_widget("qgis_layer").setLayer(raster)
        self.assertIn("with_style", form._hidden_keys)

    def test_the_layer_tree_can_preselect_a_project_layer(self):
        """Publish to GeoServer on a layer opens the form on that layer, not on
        the project's first one, with the name suggested from it.
        """

        from qgis.core import QgsProject, QgsVectorLayer

        first = QgsVectorLayer("Point?crs=epsg:4326", "cities", "memory")
        clicked = QgsVectorLayer("LineString?crs=epsg:4326", "Rivières", "memory")
        QgsProject.instance().addMapLayers([first, clicked])
        self.addCleanup(QgsProject.instance().removeAllMapLayers)
        opened = []

        class Recording(ResourceFormDialog):
            def exec(self):
                opened.append(self)
                return QDialog.DialogCode.Rejected

        with patch.object(tab_layers, "ResourceFormDialog", Recording):
            self.dlg._publish_layer(layer=clicked)

        form = opened[0]
        self.assertEqual(
            form.get_widget("source").currentText(), tab_layers._SOURCE_QGIS
        )
        self.assertIs(form.get_widget("qgis_layer").currentLayer(), clicked)
        self.assertEqual(form.get_widget("name").text(), "Rivieres")

    def test_the_qgis_source_asks_for_no_datastores_or_tables(self):
        """Opened on a project layer, the form still listed the first
        workspace's datastores and the first store's tables, and again on
        every workspace change; the QGIS source never reads them."""

        from qgis.core import QgsProject, QgsVectorLayer

        layer = QgsVectorLayer("Point?crs=epsg:4326", "cities", "memory")
        QgsProject.instance().addMapLayers([layer])
        self.addCleanup(QgsProject.instance().removeAllMapLayers)
        asked = []
        self.dlg._datastore_names = lambda ws: asked.append(("stores", ws)) or []
        self.dlg._available_tables = (
            lambda ws, ds: asked.append(("tables", ws, ds)) or []
        )
        opened = []

        class Recording(ResourceFormDialog):
            def exec(self):
                opened.append(self)
                return QDialog.DialogCode.Rejected

        with patch.object(tab_layers, "ResourceFormDialog", Recording):
            self.dlg._publish_layer(layer=layer)
        form = opened[0]
        self.assertEqual(asked, [])
        form.get_widget("workspace").setCurrentText("empty")
        self.assertEqual(asked, [])
        # back to the table source: its combos are filled then
        form.set_values({"source": tab_layers._SOURCE_TABLE})
        self.assertEqual(asked, [("stores", "empty")])


class TestBatchPublish(unittest.TestCase):
    """Several project layers, one form, one upload after another."""

    def setUp(self):
        from qgis.core import QgsProject, QgsVectorLayer

        class FakeGS:
            def get_workspaces(self):
                return ([{"name": "topp"}], 200)

        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        self.warnings, self.successes, self.errors = [], [], []
        self.dlg.show_warning_message = self.warnings.append
        self.dlg.show_success_message = self.successes.append
        self.dlg.show_error_message = self.errors.append
        self.layers = [
            QgsVectorLayer("Point?crs=epsg:4326", name, "memory")
            for name in ("a", "b", "c")
        ]
        QgsProject.instance().addMapLayers(self.layers)
        self.addCleanup(QgsProject.instance().removeAllMapLayers)
        # Each call is parked with its on_done; a test ends it when it wants.
        self.calls = []

        def publish(values, layer=None, on_done=None):
            self.calls.append((values, layer, on_done))
            return True

        self.dlg._publish_qgis_layer = publish

        class Accepting(ResourceFormDialog):
            def exec(inner):
                return QDialog.DialogCode.Accepted

        patcher = patch.object(tab_layers, "ResourceFormDialog", Accepting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def finish(self, outcome):
        self.calls[-1][2](outcome)

    def test_one_upload_at_a_time_in_order(self):
        self.dlg._publish_layers(self.layers)
        self.assertEqual([layer.name() for _v, layer, _d in self.calls], ["a"])
        self.finish("done")
        self.assertEqual(len(self.calls), 2)
        self.finish("done")
        self.finish("done")
        self.assertEqual(
            [layer.name() for _v, layer, _d in self.calls], ["a", "b", "c"]
        )
        self.assertEqual(self.calls[0][0]["workspace"], "topp")
        self.assertEqual(len(self.successes), 1)
        self.assertTrue(self.successes[0].startswith("3 layer"))
        self.assertEqual(self.warnings, [])

    def test_a_failure_is_reported_and_the_next_layer_still_goes(self):
        self.dlg._publish_layers(self.layers)
        self.finish("done")
        self.finish("failed")
        self.finish("done")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.successes, [])
        self.assertIn("Published: a, c.", self.warnings[-1])
        self.assertIn("Failed: b.", self.warnings[-1])

    def test_a_layer_refused_before_any_request_does_not_stall_the_batch(self):
        def refuse_b(values, layer=None, on_done=None):
            if layer.name() == "b":
                raise ValueError("Layer 'b' already exists in 'topp'.")
            self.calls.append((values, layer, on_done))
            return True

        self.dlg._publish_qgis_layer = refuse_b
        self.dlg._publish_layers(self.layers)
        self.finish("done")  # a; b is refused at once, so c starts
        self.assertEqual(self.calls[-1][1].name(), "c")
        self.finish("done")
        self.assertIn("already exists", self.errors[0])
        self.assertIn("Failed: b.", self.warnings[-1])

    def test_cancel_stops_the_rest_and_says_what_did_not_start(self):
        self.dlg._publish_layers(self.layers)
        self.finish("done")
        self.finish("cancelled")
        self.assertEqual(len(self.calls), 2)  # c never started
        self.assertIn("Published: a.", self.warnings[-1])
        self.assertIn("Cancelled: b.", self.warnings[-1])
        self.assertIn("Not started: c.", self.warnings[-1])

    def test_closed_during_the_last_upload_it_warns_and_logs_that_layer(self):
        """The layer in flight was in no list, and with nothing left to
        start the batch ended on a success banner nobody saw or logged."""
        logs = []
        self.dlg.log = lambda text, **kwargs: logs.append(text)
        self.dlg._publish_layers(self.layers)
        self.finish("done")
        self.finish("done")
        self.dlg._closing = True  # the dialog closed while c uploaded
        self.addCleanup(setattr, self.dlg, "_closing", False)
        self.finish("cancelled")
        self.assertEqual(self.successes, [])
        self.assertIn("Published: a, b.", self.warnings[-1])
        self.assertIn("Uploading when the dialog closed: c.", self.warnings[-1])
        self.assertEqual(logs, [self.warnings[-1]])

    def test_a_cancel_after_the_upload_stops_the_batch_and_says_so(self):
        """A Cancel while a layer's title or style was saved counted it as
        published, started the next one and ended on success."""
        self.dlg._publish_layers(self.layers)
        self.finish("done")
        self.finish("stopped")
        self.assertEqual(len(self.calls), 2)  # c never started
        self.assertEqual(self.successes, [])
        self.assertIn("Published: a.", self.warnings[-1])
        self.assertIn(
            "Published, stopped waiting for its title, keywords or style: b.",
            self.warnings[-1],
        )
        self.assertIn("Not started: c.", self.warnings[-1])

    def test_a_cancel_while_a_layer_is_checked_stops_the_batch_too(self):
        """Cancel on the waiting box during a layer's checks counted that
        layer as failed and started the next one."""
        from geoserver_manager.toolbelt.rest import Abandoned

        def stop_at_b(values, layer=None, on_done=None):
            if layer.name() == "b":
                raise Abandoned()
            self.calls.append((values, layer, on_done))
            return True

        self.dlg._publish_qgis_layer = stop_at_b
        self.dlg._publish_layers(self.layers)
        self.finish("done")
        self.assertEqual([layer.name() for _v, layer, _d in self.calls], ["a"])
        self.assertIn("Published: a.", self.warnings[-1])
        self.assertIn("Not started: b, c.", self.warnings[-1])
        self.assertEqual(self.errors, [])

    def test_a_layer_renamed_before_its_turn_keeps_the_name_the_form_listed(self):
        """Its turn read the live name: renamed "a" mid-batch, with Replace,
        it overwrote the layer the batch had just published."""
        self.dlg._publish_layers(self.layers)
        self.layers[1].setName("a")
        self.finish("done")
        self.assertEqual([values["name"] for values, _l, _d in self.calls], ["a", "b"])
        self.finish("done")
        self.finish("done")
        self.assertTrue(self.successes[0].startswith("3 layer"))

    def test_a_layer_removed_before_its_turn_is_not_started(self):
        """Its turn called the deleted layer: an error banner with Python's
        'wrapped C/C++ object ... has been deleted', counted as failed."""
        from qgis.core import QgsProject

        self.dlg._publish_layers(self.layers)
        QgsProject.instance().removeMapLayer(self.layers[1].id())
        self.finish("done")
        self.assertEqual([values["name"] for values, _l, _d in self.calls], ["a", "c"])
        self.finish("done")
        self.assertEqual(self.errors, [])
        self.assertEqual(self.successes, [])
        self.assertIn("Published: a, c.", self.warnings[-1])
        self.assertIn("Not started, no longer in the project: b.", self.warnings[-1])
        self.assertNotIn("Failed", self.warnings[-1])

    def test_a_new_project_mid_batch_starts_nothing_more(self):
        from qgis.core import QgsProject

        self.dlg._publish_layers(self.layers)
        QgsProject.instance().clear()  # every queued layer is deleted with it
        self.finish("done")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.errors, [])
        self.assertIn("Published: a.", self.warnings[-1])
        self.assertIn("Not started, no longer in the project: b, c.", self.warnings[-1])

    def test_two_layers_with_one_geoserver_name_are_refused_up_front(self):
        from qgis.core import QgsProject, QgsVectorLayer

        twin = QgsVectorLayer("Point?crs=epsg:4326", "A", "memory")
        same = QgsVectorLayer("Point?crs=epsg:4326", "A", "memory")
        QgsProject.instance().addMapLayers([twin, same])
        self.dlg._publish_layers([twin, same])
        self.assertEqual(self.calls, [])
        self.assertIn("same GeoServer name: A", self.warnings[0])


class TestEditLayer(unittest.TestCase):
    """A layer is edited here, not in GeoServer's web UI."""

    BEFORE = {
        "name": "roads",
        "title": "Roads",
        "abstract": "",
        "keywords": ["a", "b"],
        "srs": "EPSG:4326",
        "projection_policy": "FORCE_DECLARED",
        "enabled": True,
        "advertised": True,
        "cql_filter": "",
    }

    def changes(self, kind=tab_layers.VECTOR, **after):
        return GeoServerMainDialog._layer_changes(
            self.BEFORE, dict(self.BEFORE, **after), kind
        )

    def test_an_untouched_form_sends_nothing(self):
        self.assertEqual(self.changes(), (None, False))
        # the same keywords written differently are the same keywords
        self.assertEqual(self.changes(keywords=["a", "b "]), (None, False))

    def test_only_what_changed_goes_out_in_geoservers_spelling(self):
        body, recalc = self.changes(
            name="main_roads", title="", advertised=False, cql_filter="type = 'A'"
        )
        self.assertEqual(
            body,
            {
                "name": "main_roads",
                "title": "",
                "advertised": False,
                "cqlFilter": "type = 'A'",
            },
        )
        self.assertFalse(recalc)

    def test_emptied_keywords_are_sent_empty_which_clears_them(self):
        body, _ = self.changes(keywords=[])
        self.assertEqual(body, {"keywords": {"string": []}})

    def test_a_new_srs_is_normalised_and_recomputes_the_bounds(self):
        body, recalc = self.changes(srs="25832")
        self.assertEqual(body, {"srs": "EPSG:25832"})
        self.assertTrue(recalc)
        _body, recalc = self.changes(projection_policy="REPROJECT_TO_DECLARED")
        self.assertTrue(recalc)

    def test_a_raster_has_no_cql_filter(self):
        self.assertEqual(self.changes(tab_layers.RASTER, cql_filter="x"), (None, False))

    def dialog(self, taken=()):
        dlg = SyncDialog()
        dlg.gs = _EditFakeGS(taken)
        sent = []

        def raw_rest(method, path, **kwargs):
            sent.append((method, path, kwargs))
            return None

        dlg._raw_rest = raw_rest
        return dlg, sent

    def test_save_is_one_merging_put_on_the_resource(self):
        dlg, sent = self.dialog()
        row = ["roads", "topp", tab_layers.VECTOR, "pg", "line"]
        dlg._save_layer(row, self.BEFORE, dict(self.BEFORE, title="Main", srs="3857"))
        ((method, path, kwargs),) = sent
        self.assertEqual(method, "put")
        self.assertEqual(
            path, "/rest/workspaces/topp/datastores/pg/featuretypes/roads.json"
        )
        self.assertEqual(
            kwargs["json"], {"featureType": {"title": "Main", "srs": "EPSG:3857"}}
        )
        self.assertEqual(kwargs["params"], {"recalculate": "nativebbox,latlonbbox"})

    def test_a_raster_saves_under_coverage(self):
        dlg, sent = self.dialog()
        row = ["dem", "sf", tab_layers.RASTER, "sfdem", "raster"]
        dlg._save_layer(
            row,
            dict(self.BEFORE, name="dem"),
            dict(self.BEFORE, name="dem", enabled=False),
        )
        ((_m, path, kwargs),) = sent
        self.assertEqual(
            path, "/rest/workspaces/sf/coveragestores/sfdem/coverages/dem.json"
        )
        self.assertEqual(kwargs["json"], {"coverage": {"enabled": False}})
        self.assertIsNone(kwargs["params"])

    def test_a_rename_to_a_taken_name_is_refused_before_the_put(self):
        dlg, sent = self.dialog(taken=("streets",))
        with self.assertRaises(ValueError) as ctx:
            dlg._save_layer(
                ["roads", "topp", tab_layers.VECTOR, "pg", "line"],
                self.BEFORE,
                dict(self.BEFORE, name="streets"),
            )
        self.assertIn("already exists", str(ctx.exception))
        # The check is the library's own existence GET, on the layer list.
        self.assertEqual(dlg.gs.rest_service.asked, ["/rest/layers/topp:streets.json"])
        self.assertEqual(sent, [])

    def test_a_row_whose_details_failed_is_refused_as_unreadable(self):
        """Delete said "Unsupported layer type '-'" and Update from the data
        called the row a cascaded layer; both now say its details could not
        be read, as opening it does."""
        dlg, sent = self.dialog()
        with self.assertRaises(ValueError) as ctx:
            dlg._delete_layer_resource("topp", "-", "-", "roads")
        self.assertIn("could not be read", str(ctx.exception))
        warnings = []
        dlg.show_warning_message = warnings.append
        dlg._update_layer_from_source(["roads", "topp", "-", "-", "-"])
        self.assertEqual(sent, [])
        self.assertIn("could not be read", warnings[0])
        with self.assertRaises(ValueError) as ctx:
            dlg._layer_resource(["roads", "topp", "-", "-", "-"])
        self.assertIn("could not be read", str(ctx.exception))

    def test_a_bad_srs_is_refused_before_the_put(self):
        dlg, sent = self.dialog()
        with self.assertRaises(ValueError):
            dlg._save_layer(
                ["roads", "topp", tab_layers.VECTOR, "pg", "line"],
                self.BEFORE,
                dict(self.BEFORE, srs="Lambert"),
            )
        self.assertEqual(sent, [])

    def test_update_from_the_data_resets_then_recomputes(self):
        dlg, sent = self.dialog()
        dlg.show_success_message = lambda text: None
        dlg._update_layer_from_source(
            ["roads", "topp", tab_layers.VECTOR, "pg", "line"]
        )
        self.assertEqual(
            [(m, p) for m, p, _k in sent],
            [
                (
                    "post",
                    "/rest/workspaces/topp/datastores/pg/featuretypes/roads/reset",
                ),
                ("put", "/rest/workspaces/topp/datastores/pg/featuretypes/roads.json"),
            ],
        )
        self.assertEqual(sent[1][2]["params"], {"recalculate": "nativebbox,latlonbbox"})

    def test_update_from_the_data_says_why_not_for_a_cascaded_layer(self):
        dlg, sent = self.dialog()
        warnings = []
        dlg.show_warning_message = warnings.append
        dlg._update_layer_from_source(["tiles", "topp", tab_layers.WMTS, "wmts", "-"])
        self.assertEqual(sent, [])
        self.assertIn("cascaded", warnings[0])


class _EditFakeGS:
    def __init__(self, taken=()):
        class Rest:
            asked = []

            class rest_endpoints:
                base_url = "/rest"

                @staticmethod
                def featuretype(ws, ds, name):
                    return (
                        f"/rest/workspaces/{ws}/datastores/{ds}/featuretypes/"
                        f"{name}.json"
                    )

                @staticmethod
                def coverage(ws, cs, name):
                    return (
                        f"/rest/workspaces/{ws}/coveragestores/{cs}/coverages/"
                        f"{name}.json"
                    )

                @staticmethod
                def coveragestore(ws, name, method=None, store_type=None):
                    base = f"/rest/workspaces/{ws}/coveragestores/{name}"
                    return f"{base}/{method}.{store_type}" if method else f"{base}.json"

            @staticmethod
            def resource_exists(path):
                Rest.asked.append(path)
                return any(path.endswith(f":{name}.json") for name in taken)

        self.rest_service = Rest


class TestSetLayerStyle(unittest.TestCase):
    """The default style is read from the layer and written through the library."""

    def setUp(self):
        self.dlg = SyncDialog()
        outer = self
        self.set_calls = []

        self.update_calls = []

        class LayerModel:
            def asdict(inner):
                return {
                    "name": "tasmania_roads",
                    "defaultStyle": "simple_roads",
                    "styles": {"style": [{"name": "population"}]},
                }

        class Rest:
            def get_layer(inner, ws, name):
                return (LayerModel(), 200)

            def update_layer(inner, layer, ws):
                outer.update_calls.append((ws, layer.put_payload()["layer"]))
                return ("", 200)

        class GS(FakeGS):
            def __init__(inner):
                super().__init__()
                # keep the base's client and endpoints; add the layer getter
                inner.rest_service.get_layer = Rest().get_layer
                inner.rest_service.update_layer = Rest().update_layer

            def get_styles(inner, workspace_name=None):
                if workspace_name is None:
                    return ([{"name": "population"}, {"name": "simple_roads"}], 200)
                return ([{"name": "roads_ws"}], 200)

            def answer(inner, path):
                if path.endswith("/layers/topp:tasmania_roads.json"):
                    # As GeoServer writes a single other style: a bare object,
                    # which the library's model read as "name" and "href".
                    return (
                        {
                            "layer": {
                                "defaultStyle": {"name": "simple_roads"},
                                "styles": {
                                    "@class": "linked-hash-set",
                                    "style": {"name": "population", "href": "…"},
                                },
                            }
                        },
                        200,
                    )
                return super().answer(path)

            def set_default_layer_style(inner, layer_name, workspace_name, style):
                outer.set_calls.append((layer_name, workspace_name, style))
                return ("", 200)

        self.dlg.gs = GS()
        self.dlg.show_error_message = lambda t: self.fail(t)
        self.dlg.show_success_message = lambda t: None

    def test_choices_are_global_plus_qualified_workspace_styles(self):
        self.assertEqual(
            self.dlg._style_choices("topp"),
            ["population", "simple_roads", "topp:roads_ws"],
        )

    def test_current_default_is_read_from_the_layer(self):
        default, others = self.dlg._layer_styles("topp", "tasmania_roads")
        self.assertEqual(default, "simple_roads")
        self.assertEqual(others, ["population"])

    def run_form(self, row=None, **edits):
        opened = []

        class Editing(ResourceFormDialog):
            def exec(inner):
                opened.append(inner)
                for key, value in edits.items():
                    widget = inner.get_widget(key)
                    if hasattr(widget, "set_rows"):  # a table: its rows
                        widget.set_rows(value)
                    else:
                        widget.setCurrentText(value)
                return QDialog.DialogCode.Accepted

        with patch.object(tab_layers, "ResourceFormDialog", Editing):
            self.dlg._set_layer_style(
                row
                or ["tasmania_roads", "topp", "VECTOR", "taz_shapes", "simple_roads"]
            )
        return opened[0]

    def test_a_cascaded_wms_layer_is_offered_its_other_styles_only(self):
        """GeoServer answers 200 to a cascaded WMS layer's new default and
        keeps none (measured on 2.28.5): the first style was preselected, and
        an untouched form wrote it and said the styles were saved."""
        cascaded = ["roads_cascade", "sf", "WMS", "remote_wms", "-"]
        from qgis.PyQt.QtWidgets import QLabel

        form = self.run_form(row=cascaded)
        self.assertNotIn("style", form.get_values())
        texts = " ".join(label.text() for label in form.findChildren(QLabel))
        self.assertIn("remote server's default style", texts)
        self.assertEqual(self.update_calls, [])
        self.run_form(row=cascaded, others=["population"])
        self.assertEqual(
            self.update_calls,
            [
                (
                    "sf",
                    {
                        "name": "roads_cascade",
                        "styles": {"style": [{"name": "population"}]},
                    },
                )
            ],
        )

    def test_the_other_styles_are_listed_and_written_through_the_library(self):
        form = self.run_form(others=["population", "topp:roads_ws"])
        self.assertEqual(self.set_calls, [])  # the default did not change
        self.assertEqual(
            self.update_calls,
            [
                (
                    "topp",
                    {
                        "name": "tasmania_roads",
                        "styles": {
                            "style": [{"name": "population"}, {"name": "topp:roads_ws"}]
                        },
                    },
                )
            ],
        )
        self.assertIn("population", form.get_widget("others").rows())

    def test_the_form_preselects_the_default_and_the_picker_adds_a_row(self):
        form = self.run_form()
        self.assertEqual(form.get_widget("style").currentText(), "simple_roads")
        table = form.get_widget("others")
        offered = [table.picker.itemText(i) for i in range(table.picker.count())]
        self.assertIn("topp:roads_ws", offered)
        table.picker.setEditText("topp:roads_ws")
        table._add_picked()
        self.assertEqual(table.rows(), ["population", "topp:roads_ws"])

    def test_an_unknown_style_is_refused_and_nothing_is_written(self):
        errors = []
        self.dlg.show_error_message = errors.append
        self.run_form(others=["no_such_style"])
        self.assertIn("no_such_style", errors[0])
        self.assertEqual((self.set_calls, self.update_calls), ([], []))

    def test_an_unchanged_form_writes_nothing(self):
        self.run_form()
        self.assertEqual((self.set_calls, self.update_calls), ([], []))

    def test_choosing_another_style_writes_it(self):
        self.run_form(style="population")
        # One PUT: the new default, and the others without it (it was one).
        self.assertEqual(
            self.update_calls,
            [
                (
                    "topp",
                    {
                        "name": "tasmania_roads",
                        "defaultStyle": {"name": "population"},
                        "styles": {"style": []},
                    },
                )
            ],
        )

    def test_both_changed_is_one_put(self):
        """The default went through set_default_layer_style() and the others
        through update_layer(): two PUTs on the same layer document."""
        self.run_form(style="population", others=["topp:roads_ws"])
        self.assertEqual(self.set_calls, [])
        self.assertEqual(
            self.update_calls,
            [
                (
                    "topp",
                    {
                        "name": "tasmania_roads",
                        "defaultStyle": {"name": "population"},
                        "styles": {"style": [{"name": "topp:roads_ws"}]},
                    },
                )
            ],
        )

    def test_the_row_actions_are_in_order_and_say_what_they_do(self):
        self.dlg.show_warning_message = lambda t: None
        self.dlg._load_layers()
        self.assertEqual(
            [action[1] for action in self.dlg._row_actions],
            [
                "Add to QGIS",
                "Preview",
                "Preview in a browser",
                "Set style",
                "Push style from QGIS",
                "Update from the data",
                "Delete",
            ],
        )
        # icon-only buttons: every one says what it does
        for action in self.dlg._row_actions:
            self.assertEqual(len(action), 4, action[1])
            self.assertGreater(len(action[3]), len(action[1]), action[1])
        self.assertIn("log in", self.dlg._row_actions[2][3])  # the browser preview


# ############################################################################
# ###### Style from QGIS #########
# ################################


# GET /rest/layers/sf:roads.json on 2.28.5: one other style, as a bare object.
ROADS = {
    "layer": {
        "name": "roads",
        "path": "/",
        "type": "VECTOR",
        "defaultStyle": {"name": "simple_roads", "href": "…/styles/simple_roads.json"},
        "styles": {
            "@class": "linked-hash-set",
            "style": {"name": "line", "href": "…/styles/line.json"},
        },
        "resource": {
            "@class": "featureType",
            "name": "sf:roads",
            "href": "…/workspaces/sf/datastores/sf/featuretypes/roads.json",
        },
        "attribution": {"logoWidth": 0, "logoHeight": 0},
    }
}


class RestGS:
    """A client whose rest_service answers what each test sets."""

    def __init__(self, documents=None, existing=()):
        from geoservercloud.models.layer import Layer

        documents = documents or {}
        existing = set(existing)

        class Client:
            def get(inner, path, **kwargs):
                if path in documents:
                    return Response(documents[path])
                return Response("not found", 404)

        class Endpoints:
            base_url = "/rest"

        class Rest:
            rest_client = Client()
            rest_endpoints = Endpoints()

            def get_layer(inner, workspace_name, name):
                # What the library really answers: its own model of the document.
                path = f"/rest/layers/{workspace_name}:{name}.json"
                return (Layer.from_get_response_payload(documents[path]), 200)

            def resource_exists(inner, path):
                return path in existing

        self.rest_service = Rest()
        self._documents, self._existing = documents, existing

    def _store(self, collection, wrapper, workspace_name, name):
        # As the library answers: the store's own fields, type included.
        path = f"/rest/workspaces/{workspace_name}/{collection}/{name}.json"
        if path not in self._existing:
            return ("not found", 404)
        return ((self._documents.get(path) or {}).get(wrapper, {}), 200)

    def get_datastore(self, workspace_name, name):
        return self._store("datastores", "dataStore", workspace_name, name)

    def get_coverage_store(self, workspace_name, name):
        return self._store("coveragestores", "coverageStore", workspace_name, name)


class TestLayerStylesAsGeoServerWritesThem(unittest.TestCase):
    def test_a_single_other_style_is_one_style(self):
        # The library's model read its keys as two styles, "name" and "href":
        # Set style refused to save, and deleting them wiped the real one.
        dlg = SyncDialog()
        dlg.gs = RestGS({"/rest/layers/sf:roads.json": ROADS})
        self.assertEqual(dlg._layer_styles("sf", "roads"), ("simple_roads", ["line"]))

    def test_an_unreadable_layer_is_an_error_not_no_styles(self):
        # (None, []) made the form offer the first style as the new default.
        dlg = SyncDialog()
        dlg.gs = RestGS({"/rest/layers/sf:roads.json": ROADS})
        with self.assertRaises(Exception):
            dlg._layer_styles("sf", "missing")


class StyleFakeGS(FakeGS):
    """Records the style calls the push makes."""

    def __init__(self):
        super().__init__()
        self.style_calls = []
        outer = self

        class Client:
            def put(inner, path, **kwargs):
                outer.style_calls.append(("PUT", path, kwargs))
                return Response()

            def post(inner, path, **kwargs):
                outer.style_calls.append(("POST", path, kwargs))
                return Response()

        class Endpoints:
            base_url = "/rest"

            def style(inner, style_name, workspace_name=None, format="json"):
                base = (
                    f"/rest/workspaces/{workspace_name}/styles/{style_name}"
                    if workspace_name
                    else f"/rest/styles/{style_name}"
                )
                return f"{base}.{format}"

        class Rest:
            rest_client = Client()
            rest_endpoints = Endpoints()

        self.rest_service = Rest()

    existing_styles = ()  # (name, workspace) pairs the server already has

    def get_style_definition(self, style_name, workspace_name=None):
        if (style_name, workspace_name) in self.existing_styles:
            return ({"name": style_name}, 200)
        return ("not found", 404)

    def create_style_definition(self, name, filename, workspace_name=None):
        self.style_calls.append(("definition", name, filename, workspace_name))
        return ("", 201)

    def set_default_layer_style(self, layer_name, workspace_name, style):
        self.style_calls.append(("set_default", layer_name, workspace_name, style))
        return ("", 200)


class TestStyleFromQgis(unittest.TestCase):
    def setUp(self):
        from qgis.core import QgsProject

        self.project = QgsProject.instance()
        self.project.removeAllMapLayers()
        self.dlg = SyncDialog()
        self.dlg.gs = StyleFakeGS()
        self.messages = {"warning": [], "success": []}
        self.dlg.show_warning_message = self.messages["warning"].append
        self.dlg.show_success_message = self.messages["success"].append
        self.dlg.show_error_message = lambda text: self.fail(f"unexpected: {text}")
        self.dlg._reload_current_tab = lambda: None

    def tearDown(self):
        self.project.removeAllMapLayers()

    def add_layer(self, name, colour="#ff0000"):
        from tests.qgis.test_sld import point_layer

        layer = point_layer(name, colour=colour)
        self.project.addMapLayer(layer)
        return layer

    def push(self, row_data, **edits):
        class Accepting(ResourceFormDialog):
            def exec(inner):
                for key, value in edits.items():
                    widget = inner.get_widget(key)
                    if hasattr(widget, "setChecked"):
                        widget.setChecked(value)
                    elif hasattr(widget, "setCurrentText"):
                        widget.setCurrentText(value)
                    else:
                        widget.setText(value)
                return QDialog.DialogCode.Accepted

        with patch.object(tab_layers, "ResourceFormDialog", Accepting):
            self.dlg._style_from_qgis(row_data)

    def test_with_no_matching_layer_nothing_is_preselected(self):
        # The first layer was: its style pushed as an unrelated layer's
        # default.

        self.add_layer("rivers")
        opened = []

        class Looking(ResourceFormDialog):
            def exec(inner):
                opened.append(inner)
                return QDialog.DialogCode.Rejected

        with patch.object(tab_layers, "ResourceFormDialog", Looking):
            self.dlg._style_from_qgis(["roads", "topp", "VECTOR", "pg", "line"])
        self.assertIsNone(opened[0].get_values()["qgis_layer"])
        opened[0]._on_accept()
        self.assertFalse(opened[0].result())  # Save asks for a layer

    def test_the_matching_project_layer_is_found_by_name(self):
        from tests.qgis.test_sld import point_layer

        # a layer added by this plugin keeps GeoServer's "workspace:layer" name
        roads, rivers = point_layer("topp:roads"), point_layer("Rivers")
        layers = [roads, rivers]
        # matched ignoring case and any workspace prefix on either side
        self.assertIs(self.dlg._matching_project_layer("roads", layers), roads)
        self.assertIs(self.dlg._matching_project_layer("topp:rivers", layers), rivers)
        self.assertIsNone(self.dlg._matching_project_layer("nothing", layers))

    def test_the_push_creates_the_style_and_assigns_it_qualified(self):
        self.add_layer("tasmania_roads")
        self.push(["tasmania_roads", "topp", "VECTOR", "taz_shapes", "simple_roads"])

        # One request: a definition created first stayed behind, empty, when
        # GeoServer refused the body.
        verb, path, kwargs = self.dlg.gs.style_calls[0]
        self.assertEqual((verb, path), ("POST", "/rest/workspaces/topp/styles.json"))
        self.assertEqual(kwargs["params"], {"name": "tasmania_roads"})
        self.assertEqual(
            kwargs["headers"]["Content-Type"], "application/vnd.ogc.se+xml"
        )
        self.assertIn(b"ff0000", kwargs["data"].lower())
        # a workspace style is referenced as "workspace:style"; a bare name
        # would resolve to a global style of the same name
        self.assertEqual(
            self.dlg.gs.style_calls[1],
            ("set_default", "tasmania_roads", "topp", "topp:tasmania_roads"),
        )

    def test_replacing_an_existing_style_is_confirmed_not_silent(self):
        """create_style_definition upserts; other layers may share the style."""
        from qgis.PyQt.QtWidgets import QMessageBox

        self.add_layer("tasmania_roads")
        self.dlg.gs.existing_styles = (("tasmania_roads", "topp"),)
        warnings = []
        self.dlg.show_warning_message = warnings.append
        asked = []

        def decline(parent, title, text, buttons, default):
            asked.append(text)
            return QMessageBox.StandardButton.No

        with patch.object(QMessageBox, "question", staticmethod(decline)):
            self.push(
                ["tasmania_roads", "topp", "VECTOR", "taz_shapes", "simple_roads"]
            )
        self.assertEqual(self.dlg.gs.style_calls, [])  # nothing sent
        self.assertIn("already exists in 'topp'", asked[0])
        self.assertIn("render differently", asked[0])
        self.assertTrue(any("left as it is" in w for w in warnings), warnings)

        with patch.object(
            QMessageBox,
            "question",
            staticmethod(lambda *args: QMessageBox.StandardButton.Yes),
        ):
            self.push(
                ["tasmania_roads", "topp", "VECTOR", "taz_shapes", "simple_roads"]
            )
        # Replacing puts the new body only; the definition stays as it is.
        verb, path, _kwargs = self.dlg.gs.style_calls[0]
        self.assertEqual(
            (verb, path), ("PUT", "/rest/workspaces/topp/styles/tasmania_roads.sld")
        )

    def test_the_style_name_is_the_layers_and_can_be_changed(self):
        self.add_layer("tasmania_roads")
        self.push(
            ["tasmania_roads", "topp", "VECTOR", "taz_shapes", "simple_roads"],
            style="roads_from_qgis",
        )
        self.assertEqual(
            self.dlg.gs.style_calls[0][2]["params"], {"name": "roads_from_qgis"}
        )
        self.assertEqual(self.dlg.gs.style_calls[-1][3], "topp:roads_from_qgis")

    def test_unticking_the_default_uploads_without_assigning(self):
        self.add_layer("tasmania_roads")
        self.push(
            ["tasmania_roads", "topp", "VECTOR", "taz_shapes", "simple_roads"],
            set_default=False,
        )
        self.assertFalse(
            [call for call in self.dlg.gs.style_calls if call[0] == "set_default"]
        )
        self.assertIn("uploaded", self.messages["success"][0])

    def test_a_cascaded_wms_layer_gets_the_style_uploaded_not_assigned(self):
        """GeoServer answers 200 to a cascaded WMS layer's new default and
        keeps none: "'x' styled from 'y'" was said of an assignment that did
        not happen."""
        self.add_layer("roads_cascade")
        self.push(["roads_cascade", "sf", "WMS", "remote_wms", "-"])
        self.assertEqual(
            self.dlg.gs.style_calls[0][:2], ("POST", "/rest/workspaces/sf/styles.json")
        )
        self.assertFalse(
            [call for call in self.dlg.gs.style_calls if call[0] == "set_default"]
        )
        self.assertEqual(
            self.messages["success"], ["Style 'roads_cascade' uploaded to 'sf'."]
        )

    def test_an_empty_project_is_a_banner_not_a_dialog(self):
        class Recording(ResourceFormDialog):
            opened = []

            def exec(inner):
                Recording.opened.append(inner)
                return QDialog.DialogCode.Rejected

        with patch.object(tab_layers, "ResourceFormDialog", Recording):
            self.dlg._style_from_qgis(["tasmania_roads", "topp", "VECTOR", "x", ""])
        self.assertEqual(Recording.opened, [])
        self.assertIn("no vector or raster layer", self.messages["warning"][0])


# ############################################################################
# ##### Publish from QGIS ########
# ################################


class GpkgPublishFakeGS(StyleFakeGS):
    """Adds what the GeoPackage publish path touches."""

    layer_default = None  # the published layer's default style

    def __init__(self, datastore_exists=False, layer_exists=False):
        super().__init__()
        self.datastore_exists = datastore_exists
        self.layer_exists = layer_exists
        outer = self

        class Client:
            def post(inner, path, **kwargs):
                outer.style_calls.append(("POST", path, kwargs))
                return Response()

            def get(inner, path, **kwargs):
                response = Response()
                if "/layers/" in path:  # the layer's document: its default style
                    style = {"name": outer.layer_default}
                    response.json = lambda: {"layer": {"defaultStyle": style}}
                else:  # the store Replace would overwrite: the plugin's own kind
                    response.json = lambda: {"dataStore": {"type": "GeoPackage"}}
                return response

            def put(inner, path, **kwargs):
                body = kwargs.get("data")
                if hasattr(body, "read"):  # a streaming upload: record its bytes
                    kwargs = {**kwargs, "data": body.read()}
                outer.style_calls.append(("PUT", path, kwargs))
                if path.endswith("file.gpkg"):
                    # GeoServer creates the datastore from the uploaded file
                    outer.datastore_exists = True
                return Response()

        class Endpoints:
            base_url = "/rest"

            def style(inner, style_name, workspace_name=None, format="json"):
                base = (
                    f"/rest/workspaces/{workspace_name}/styles/{style_name}"
                    if workspace_name
                    else f"/rest/styles/{style_name}"
                )
                return f"{base}.{format}"

            def featuretype(inner, workspace_name, datastore_name, name):
                return (
                    f"/rest/workspaces/{workspace_name}/datastores/"
                    f"{datastore_name}/featuretypes/{name}.json"
                )

            def coverage(inner, workspace_name, store_name, name):
                return (
                    f"/rest/workspaces/{workspace_name}/coveragestores/"
                    f"{store_name}/coverages/{name}.json"
                )

            def coveragestore(inner, ws, name, method=None, store_type=None):
                base = f"/rest/workspaces/{ws}/coveragestores/{name}"
                return f"{base}/{method}.{store_type}" if method else f"{base}.json"

        class Rest:
            rest_client = Client()
            rest_endpoints = Endpoints()

            def resource_exists(inner, path):
                # The layer list as GeoServer has it; the store as the fake has it.
                if "/layers/" in path:
                    return outer.layer_exists
                if "/datastores/" in path:
                    return outer.datastore_exists
                return False

        self.rest_service = Rest()
        self.feature_type_asked = 0

    def get_datastore(self, workspace_name, name):
        if not self.datastore_exists:
            return ("not found", 404)
        return (
            {
                "name": name,
                "type": "GeoPackage",
                "enabled": True,
                "connectionParameters": {
                    "entry": {"database": f"/data/{name}.gpkg", "dbtype": "geopkg"}
                },
            },
            200,
        )

    def get_feature_type(self, workspace_name, datastore_name, name):
        self.feature_type_asked += 1
        return ({"name": name}, 200 if self.layer_exists else 404)

    def get_coverage_store(self, workspace_name, name):
        return ("not found", 404)

    def create_datastore(self, **kwargs):
        self.style_calls.append(("create_datastore", kwargs))
        return ("", 200)


class TestPublishQgisLayer(unittest.TestCase):
    """Uploading a project layer as a GeoPackage datastore."""

    def setUp(self):
        from qgis.core import QgsProject

        self.project = QgsProject.instance()
        self.project.removeAllMapLayers()
        self.dlg = SyncDialog()
        self.dlg.gs = GpkgPublishFakeGS()
        self.dlg.show_error_message = lambda text: self.fail(f"unexpected: {text}")
        self.dlg.show_success_message = lambda text: None
        self.dlg.show_warning_message = lambda text: None

    def tearDown(self):
        self.project.removeAllMapLayers()

    def add_layer(self, name="Roads (2024)", colour="#ff0000"):
        from tests.qgis.test_sld import point_layer

        layer = point_layer(name, colour=colour)
        self.project.addMapLayer(layer)
        return layer

    def values(self, **overrides):
        base = {
            "source": "A layer from this QGIS project",
            "workspace": "topp",
            # The form hands the layer itself: the one of that name here.
            "qgis_layer": (self.project.mapLayersByName("Roads (2024)") or [None])[0],
            "name": "Roads (2024)",
            "replace": False,
            "with_style": False,
            "title": "",
            "abstract": "",
            "keywords": [],
        }
        base.update(overrides)
        return base

    def sent(self, verb):
        return [call for call in self.dlg.gs.style_calls if call[0] == verb]

    def test_a_failed_metadata_step_says_the_layer_is_published(self):
        # The GeoPackage is stored and the layer live; "Failed to publish"
        # hid that, and a retry then said it exists.
        self.add_layer()
        warnings = []
        self.dlg.show_warning_message = warnings.append

        def refuse(*args):
            raise RuntimeError("HTTP 500: boom")

        self.dlg._set_feature_type_metadata = refuse
        self.dlg._publish_qgis_layer(self.values(title="Roads"))
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("is published, but its title", warnings[0])
        self.assertIn("boom", warnings[0])

    def test_the_geopackage_is_put_under_the_normalised_name(self):
        self.add_layer()
        self.dlg._publish_qgis_layer(self.values())

        verb, path, kwargs = self.sent("PUT")[0]
        # "Roads (2024)" is not a WFS type name; "Roads_2024" is
        self.assertEqual(path, "/rest/workspaces/topp/datastores/Roads_2024/file.gpkg")
        self.assertEqual(kwargs["params"], {"update": "overwrite"})
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/x-sqlite3")
        self.assertTrue(kwargs["data"].startswith(b"SQLite format 3"))

    def test_the_uploaded_store_is_made_read_only_by_merging(self):
        self.add_layer()
        self.dlg._publish_qgis_layer(self.values())

        # the store exists because the upload created it
        kwargs = self.sent("create_datastore")[0][1]
        params = kwargs["connection_parameters"]
        self.assertEqual(params["read_only"], "true")
        # the server's own parameters are kept, not replaced by a template
        self.assertEqual(params["dbtype"], "geopkg")
        self.assertEqual(params["database"], "/data/Roads_2024.gpkg")
        self.assertEqual(kwargs["datastore_type"], "GeoPackage")

    def test_metadata_is_merged_onto_what_geoserver_computed(self):
        self.add_layer()
        self.dlg._publish_qgis_layer(
            self.values(
                title="Roads", abstract="Main roads", keywords=["roads", "2024"]
            )
        )
        feature_type_puts = [
            call for call in self.sent("PUT") if "featuretypes" in call[1]
        ]
        _verb, path, kwargs = feature_type_puts[0]
        self.assertIn("/datastores/Roads_2024/featuretypes/Roads_2024.json", path)
        payload = kwargs["json"]["featureType"]
        self.assertEqual(payload["title"], "Roads")
        self.assertEqual(payload["abstract"], "Main roads")
        self.assertEqual(payload["keywords"], {"string": ["roads", "2024"]})
        # a partial PUT merges, so nothing else may be sent
        self.assertEqual(set(payload), {"title", "abstract", "keywords"})

    def test_no_metadata_means_no_extra_request(self):
        self.add_layer()
        self.dlg._publish_qgis_layer(self.values())
        self.assertEqual(
            [call for call in self.sent("PUT") if "featuretypes" in call[1]], []
        )

    def test_the_symbology_can_travel_with_the_data(self):
        self.add_layer(colour="#00aa44")
        self.dlg._publish_qgis_layer(self.values(with_style=True))
        style_posts = [call for call in self.dlg.gs.style_calls if call[0] == "POST"]
        self.assertEqual(style_posts[0][1], "/rest/workspaces/topp/styles.json")
        self.assertEqual(style_posts[0][2]["params"], {"name": "Roads_2024"})
        self.assertIn(b"00aa44", style_posts[0][2]["data"].lower())
        self.assertIn(
            ("set_default", "Roads_2024", "topp", "topp:Roads_2024"),
            self.dlg.gs.style_calls,
        )

    def test_an_existing_datastore_is_refused_unless_replace_is_ticked(self):
        self.add_layer()
        self.dlg.gs = GpkgPublishFakeGS(datastore_exists=True)
        with self.assertRaises(ValueError) as caught:
            self.dlg._publish_qgis_layer(self.values())
        self.assertIn("Roads_2024", str(caught.exception))
        self.assertIn("Replace", str(caught.exception))
        self.assertEqual(self.sent("PUT"), [])
        # the store and the layer list are asked, no feature type GET
        self.assertEqual(self.dlg.gs.feature_type_asked, 0)

        self.dlg._publish_qgis_layer(self.values(replace=True))
        self.assertTrue(self.sent("PUT"))

    def test_an_existing_layer_is_refused_too(self):
        self.add_layer()
        self.dlg.gs = GpkgPublishFakeGS(layer_exists=True)
        with self.assertRaises(ValueError):
            self.dlg._publish_qgis_layer(self.values())

    def test_a_kept_style_is_said_and_not_claimed(self):
        """The user answered No to replacing a style of the layer's name: the
        banner claimed a plain publish while the layer had GeoServer's
        generic default style."""
        self.add_layer()
        warnings = []
        self.dlg.show_warning_message = warnings.append
        self.dlg.show_success_message = lambda text: self.fail(f"claimed: {text}")
        self.dlg._push_qgis_style = lambda *args, **kwargs: False
        self.dlg._publish_qgis_layer(self.values(with_style=True))
        self.assertIn("Roads_2024", warnings[0])
        self.assertIn("left as it is", warnings[0])
        self.assertIn("not assigned", warnings[0])

    def test_a_kept_style_that_is_the_default_is_not_said_unassigned(self):
        """On a Replace the style of the name is usually the one the first
        publish assigned: 'not assigned to the layer' was wrong there."""
        self.add_layer()
        self.dlg.gs = GpkgPublishFakeGS(datastore_exists=True)
        self.dlg.gs.layer_default = "topp:Roads_2024"
        warnings = []
        self.dlg.show_warning_message = warnings.append
        self.dlg.show_success_message = lambda text: self.fail(f"claimed: {text}")
        self.dlg._push_qgis_style = lambda *args, **kwargs: False
        self.dlg._publish_qgis_layer(self.values(with_style=True, replace=True))
        self.assertEqual(
            warnings,
            ["Layer 'Roads_2024' published. Style 'Roads_2024' left as it is."],
        )

    def test_a_cancel_after_the_upload_reports_the_layer_stopped(self):
        """Cancel on the waiting box of the metadata step: the upload still
        reported 'done', so a batch went on as if all of it was set."""
        from geoserver_manager.toolbelt.rest import Abandoned

        self.add_layer()
        outcomes = []

        def abandon(*args):
            raise Abandoned(write=True)

        self.dlg._set_feature_type_metadata = abandon
        self.dlg._publish_qgis_layer(
            self.values(title="Roads"), on_done=outcomes.append
        )
        self.assertEqual(outcomes, ["stopped"])
        self.dlg.gs = GpkgPublishFakeGS()
        self.dlg._set_feature_type_metadata = lambda *args: None
        self.dlg._publish_qgis_layer(
            self.values(title="Roads"), on_done=outcomes.append
        )
        self.assertEqual(outcomes, ["stopped", "done"])

    def test_nothing_is_left_in_the_temporary_folder(self):
        import glob
        import tempfile

        self.add_layer()
        self.dlg._publish_qgis_layer(self.values())
        self.assertEqual(glob.glob(f"{tempfile.gettempdir()}/gsm_publish_*"), [])

    def test_a_raster_picked_here_goes_down_the_coverage_store_path(self):
        """The picker lists rasters too; they used to hit the GeoPackage writer."""
        import tempfile

        from qgis.core import QgsRasterLayer

        from tests.qgis.test_tab_coveragestores import write_raster

        folder = Path(tempfile.mkdtemp(prefix="gsm_test_"))
        try:
            layer = QgsRasterLayer(str(write_raster(folder / "dem.tif")), "dem", "gdal")
            self.assertTrue(layer.isValid())
            self.project.addMapLayer(layer)
            self.dlg._publish_qgis_layer(self.values(qgis_layer=layer, name="dem"))
            puts = self.sent("PUT")
            self.assertEqual(len(puts), 1)
            self.assertTrue(puts[0][1].endswith("/coveragestores/dem/file.geotiff"))
            self.assertEqual(puts[0][2]["headers"]["Content-Type"], "image/tiff")
            self.assertEqual(puts[0][2]["params"]["coverageName"], "dem")
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_a_second_upload_is_refused_before_anything_is_exported(self):
        import glob
        import tempfile

        self.add_layer()
        warnings = []
        self.dlg.show_warning_message = warnings.append
        self.dlg._upload = object()  # one is running
        self.dlg._publish_qgis_layer(self.values())
        self.assertEqual(self.sent("PUT"), [])
        self.assertTrue(any("already running" in w for w in warnings), warnings)
        self.assertEqual(glob.glob(f"{tempfile.gettempdir()}/gsm_publish_*"), [])
        self.dlg._upload = None


class TestPublishLandsOnItsOwnLayer(unittest.TestCase):
    """Measured on 2.28.5: a layer of the name in another store made the new
    one name1 while the style went to the old one; Replace over a PostGIS
    store had GeoServer import the GeoPackage into that database."""

    def dialog(self, existing, documents=None):
        dlg = SyncDialog()
        dlg.gs = RestGS(documents or {}, existing)
        return dlg

    def test_a_layer_of_the_name_elsewhere_is_refused(self):
        dlg = self.dialog({"/rest/layers/topp:roads.json"})
        with self.assertRaises(ValueError):
            dlg._refuse_layer_clash("topp", "roads", False, "data", "GeoPackage")

    def test_replace_refuses_a_layer_from_another_store(self):
        dlg = self.dialog({"/rest/layers/topp:roads.json"})
        with patch.object(dlg, "_layer_summary", return_value=("VECTOR", "other", "-")):
            with self.assertRaises(ValueError) as caught:
                dlg._refuse_layer_clash("topp", "roads", True, "data", "GeoPackage")
        self.assertIn("other", str(caught.exception))

    def test_replace_refuses_a_store_of_another_type_and_allows_its_own(self):
        path = "/rest/workspaces/topp/datastores/roads.json"
        dlg = self.dialog({path}, {path: {"dataStore": {"type": "PostGIS"}}})
        with self.assertRaises(ValueError) as caught:
            dlg._refuse_layer_clash("topp", "roads", True, "data", "GeoPackage")
        self.assertIn("PostGIS", str(caught.exception))
        # its own GeoPackage store and layer: Replace goes through
        dlg = self.dialog(
            {path, "/rest/layers/topp:roads.json"},
            {path: {"dataStore": {"type": "GeoPackage"}}},
        )
        with patch.object(dlg, "_layer_summary", return_value=("VECTOR", "roads", "-")):
            dlg._refuse_layer_clash("topp", "roads", True, "data", "GeoPackage")


class TestVectorUploadRunsInATask(unittest.TestCase):
    """The real dialog: the GeoPackage streams off the GUI thread, with progress."""

    def setUp(self):
        from qgis.core import QgsProject

        self.project = QgsProject.instance()
        self.project.removeAllMapLayers()
        self.dlg = GeoServerMainDialog()
        self.dlg.gs = GpkgPublishFakeGS()
        self.successes, self.errors = [], []
        self.dlg.show_success_message = self.successes.append
        self.dlg.show_error_message = self.errors.append
        self.dlg.show_warning_message = lambda text: None
        self.dlg._reload_current_tab = lambda: None

    def tearDown(self):
        self.dlg._closing = True
        self.dlg._cancel_load(user=True)
        self.project.removeAllMapLayers()

    def test_the_upload_is_a_task_with_progress_and_the_dialog_stays_usable(self):
        from qgis.PyQt.QtTest import QTest

        from tests.qgis.test_sld import point_layer

        layer = point_layer("Roads (2024)", colour="#ff0000")
        self.project.addMapLayer(layer)
        self.dlg._publish_qgis_layer(
            {
                "source": "A layer from this QGIS project",
                "workspace": "topp",
                "qgis_layer": layer,
                "name": "Roads (2024)",
                "replace": False,
                "with_style": False,
                "title": "",
                "abstract": "",
                "keywords": [],
            }
        )
        self.assertIsNotNone(self.dlg._upload)  # the upload slot, not a load
        self.assertEqual(self.dlg.btn_refresh.text(), "Cancel")

        waited = 0
        while self.dlg._loading() and waited < 20000:
            QTest.qWait(20)
            waited += 20
        puts = [
            call
            for call in self.dlg.gs.style_calls
            if call[0] == "PUT" and call[1].endswith("file.gpkg")
        ]
        self.assertEqual(len(puts), 1)
        self.assertTrue(puts[0][2]["data"].startswith(b"SQLite format 3"))
        self.assertEqual(self.errors, [])
        self.assertEqual(self.successes, ["Layer 'Roads_2024' published."])
        self.assertEqual(self.dlg.btn_refresh.text(), "Refresh")


class TestPublishForm(unittest.TestCase):
    """The publish dialog only shows the fields of the chosen source."""

    def setUp(self):
        from qgis.core import QgsProject

        QgsProject.instance().removeAllMapLayers()
        from tests.qgis.test_sld import point_layer

        QgsProject.instance().addMapLayer(point_layer("Roads (2024)"))
        self.dlg = SyncDialog()
        self.dlg.gs = GpkgPublishFakeGS()

    def tearDown(self):
        from qgis.core import QgsProject

        QgsProject.instance().removeAllMapLayers()

    def form(self):
        return ResourceFormDialog(title="t", fields=self.dlg._publish_fields(["topp"]))

    def test_the_table_source_hides_the_qgis_fields(self):
        form = self.form()
        self.dlg._on_publish_source_changed(form, "A table in a datastore")
        for key in ("datastore", "table", "epsg"):
            self.assertNotIn(key, form._hidden_keys)
        for key in ("qgis_layer", "name", "replace", "with_style"):
            self.assertIn(key, form._hidden_keys)

    def test_the_qgis_source_hides_the_table_fields_and_suggests_a_name(self):
        form = self.form()
        self.dlg._on_publish_source_changed(form, "A layer from this QGIS project")
        for key in ("datastore", "table", "epsg"):
            self.assertIn(key, form._hidden_keys)
        for key in ("qgis_layer", "name", "replace", "with_style"):
            self.assertNotIn(key, form._hidden_keys)
        # the name is filled in already, normalised, and still editable
        self.assertEqual(form.get_widget("name").text(), "Roads_2024")
        self.assertTrue(form.get_widget("name").isEnabled())

    def test_a_name_the_user_typed_is_not_overwritten(self):
        form = self.form()
        form.get_widget("name").setText("my_choice")
        self.dlg._prefill_publish_name(
            form, form.get_widget("qgis_layer").currentLayer()
        )
        self.assertEqual(form.get_widget("name").text(), "my_choice")


# ############################################################################
# ###### Preview in a browser ####
# ################################


class TestPreviewInBrowser(unittest.TestCase):
    """GeoServer's own OpenLayers page, on the layer's extent, in the browser."""

    def url(self, **kwargs):
        from geoserver_manager.gui.tab_layers import LayerTabMixin

        return LayerTabMixin._preview_url("http://gs/geoserver/", **kwargs)

    def test_a_workspace_layer_goes_through_its_virtual_service(self):
        url = self.url(
            qualified_name="topp:states",
            bbox=(0.0, 0.0, 4.0, 2.0),
            srs="EPSG:4326",
            workspace="topp",
        )
        self.assertTrue(url.startswith("http://gs/geoserver/topp/wms?"), url)
        for part in (
            "service=WMS",
            "version=1.1.0",
            "request=GetMap",
            "layers=topp:states",
            "bbox=0.0,0.0,4.0,2.0",
            "width=768",
            "height=384",
            "srs=EPSG:4326",
            "styles=",
            "format=application/openlayers",
        ):
            self.assertIn(part, url)
        url = self.url(
            qualified_name="my ws:roads",
            bbox=(0, 0, 1, 1),
            srs="EPSG:4326",
            workspace="my ws",
        )
        self.assertTrue(url.startswith("http://gs/geoserver/my%20ws/wms?"), url)

    def test_a_tall_extent_caps_the_height_instead(self):
        url = self.url(
            qualified_name="topp:states",
            bbox=(0.0, 0.0, 2.0, 4.0),
            srs="EPSG:4326",
            workspace="topp",
        )
        self.assertIn("width=384", url)
        self.assertIn("height=768", url)

    def test_a_global_group_has_no_workspace_anywhere(self):
        url = self.url(
            qualified_name="tasmania",
            bbox=(143.0, -44.0, 149.0, -40.0),
            srs="EPSG:4326",
        )
        self.assertTrue(url.startswith("http://gs/geoserver/wms?"), url)
        self.assertIn("layers=tasmania&", url)

    def test_without_an_extent_the_world(self):
        for bbox in (None, (1.0, 1.0, 1.0, 1.0)):
            url = self.url(
                qualified_name="x:y", bbox=bbox, srs="EPSG:2154", workspace="x"
            )
            self.assertIn("bbox=-180.0,-90.0,180.0,90.0", url)
            self.assertIn("srs=EPSG:4326", url)
            self.assertIn("width=768&height=384", url)

    def test_bbox_from_geoservers_shapes(self):
        from geoserver_manager.gui.tab_layers import LayerTabMixin

        box = {
            "minx": -124.731422,
            "maxx": -66.969849,
            "miny": 24.955967,
            "maxy": 49.371735,
            "crs": "EPSG:4326",
        }
        self.assertEqual(
            LayerTabMixin._bbox_from(box),
            ((-124.731422, 24.955967, -66.969849, 49.371735), "EPSG:4326"),
        )
        box["crs"] = {"@class": "projected", "$": "EPSG:2154"}
        self.assertEqual(LayerTabMixin._bbox_from(box)[1], "EPSG:2154")
        self.assertEqual(LayerTabMixin._bbox_from({"minx": 1}), (None, None))
        self.assertEqual(LayerTabMixin._bbox_from(None), (None, None))

    def test_the_row_action_opens_the_layers_own_extent(self):
        class BboxGS(FakeGS):
            def get_feature_type(self, workspace_name, datastore_name, name):
                detail, status = super().get_feature_type(
                    workspace_name, datastore_name, name
                )
                detail["latLonBoundingBox"] = {
                    "minx": -124.731422,
                    "maxx": -66.969849,
                    "miny": 24.955967,
                    "maxy": 49.371735,
                    "crs": "EPSG:4326",
                }
                return detail, status

        dlg = SyncDialog()
        dlg.gs = BboxGS()
        dlg.plg_settings = FakePrefs("http://gs/geoserver", credentials=("", ""))
        opened = []
        with patch.object(
            tab_layers.QDesktopServices,
            "openUrl",
            lambda url: opened.append(url.toString()) or True,
        ):
            dlg._preview_layer_in_browser(
                ["states", "topp", "VECTOR", "states_shapefile", "polygon"]
            )
        self.assertEqual(len(opened), 1, opened)
        self.assertTrue(opened[0].startswith("http://gs/geoserver/topp/wms?"), opened)
        self.assertIn("layers=topp:states", opened[0])
        self.assertIn("bbox=-124.731422,24.955967,-66.969849,49.371735", opened[0])

    def test_a_browser_that_does_not_open_is_a_warning(self):
        dlg = SyncDialog()
        warnings = []
        dlg.show_warning_message = warnings.append
        with patch.object(tab_layers.QDesktopServices, "openUrl", lambda url: False):
            dlg._open_in_browser("http://gs/geoserver/wms")
        self.assertEqual(len(warnings), 1)
        self.assertIn("http://gs/geoserver/wms", warnings[0])


# ############################################################################
# ####### Stand-alone run ########
# ################################
if __name__ == "__main__":
    unittest.main()
