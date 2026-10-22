#! python3  # noqa E265

"""
Every tab against a real GeoServer: what the fakes of the other tests cannot show.

Runs only when GSM_LIVE_URL names a GeoServer with its demo data, and with a
PostGIS that GeoServer reaches as host "postgis" (database, user and password
"geoserver"). The docker sandbox is one; the weekly workflow starts another for
the latest 2.x and 3.x releases. Against the sandbox:

    GSM_LIVE_URL=http://localhost:8080/geoserver QT_QPA_PLATFORM=offscreen \
        python3 -m pytest tests/qgis/test_live_geoserver.py

The steps run in order and build on each other, all inside the workspace
`gsm_live`, which the last step deletes. A step goes through the handler a
click reaches, with its real form filled and saved, and reads the result back
from the server. Server-wide settings it changes are restored.
"""

import os
import time
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import requests
from qgis.core import QgsProject
from qgis.PyQt.QtWidgets import QCheckBox, QDialog, QSpinBox
from qgis.testing import start_app, unittest

from geoserver_manager.gui import (
    dlg_resource_form,
    tab_coveragestores,
    tab_datastores,
    tab_gwc,
    tab_layergroups,
    tab_layers,
    tab_server,
    tab_styles,
    tab_workspaces,
)
from geoserver_manager.toolbelt.dependencies import ensure_dependencies
from tests.qgis.sync_dialog import FakePrefs, SyncDialog

start_app()

URL = os.environ.get("GSM_LIVE_URL", "").rstrip("/")
AUTH = ("admin", "geoserver")
WS = "gsm_live"
LAYER = f"{WS}:archsites"
SLD = """<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0" xmlns="http://www.opengis.net/sld"
    xmlns:ogc="http://www.opengis.net/ogc">
  <NamedLayer>
    <Name>live_point</Name>
    <UserStyle>
      <FeatureTypeStyle>
        <Rule>
          <PointSymbolizer>
            <Graphic>
              <Mark>
                <WellKnownName>circle</WellKnownName>
                <Fill><CssParameter name="fill">{colour}</CssParameter></Fill>
              </Mark>
              <Size>6</Size>
            </Graphic>
          </PointSymbolizer>
        </Rule>
      </FeatureTypeStyle>
    </UserStyle>
  </NamedLayer>
</StyledLayerDescriptor>
"""
TAB_MODULES = (
    tab_coveragestores,
    tab_datastores,
    tab_gwc,
    tab_layergroups,
    tab_layers,
    tab_server,
    tab_styles,
    tab_workspaces,
)


def fill(form, values):
    """Type values into a form, in the order given (a type combo first)."""
    for key, value in values.items():
        widget = form.get_widget(key)
        if widget is None:
            raise KeyError(f"the form has no field '{key}'")
        if isinstance(widget, QCheckBox):
            widget.setChecked(value)
        elif isinstance(widget, QSpinBox):
            widget.setValue(value)
        else:
            form.set_values({key: value})


class Forms:
    """What the forms showed when they opened, and why one refused its Save."""

    def __init__(self):
        self.shown = []
        self.refused = []


@contextmanager
def answering(*answers):
    """Fill the forms a handler opens, one answer each, and press Save.

    An answer is a dict of values, a function of the form that returns one, or
    None to close the form without saving (a viewer).
    """
    queue = list(answers)
    forms = Forms()

    class Answering(dlg_resource_form.ResourceFormDialog):
        def exec(inner):
            forms.shown.append(inner.get_values())
            if not queue:
                raise AssertionError(f"an unexpected form: {inner.windowTitle()}")
            answer = queue.pop(0)
            if answer is None:
                return QDialog.DialogCode.Rejected
            fill(inner, answer(inner) if callable(answer) else answer)
            inner._on_accept()
            if not inner.result():
                forms.refused.append(inner._validation_label.text())
            return inner.result()

    with ExitStack() as stack:
        for module in TAB_MODULES:
            stack.enter_context(patch.object(module, "ResourceFormDialog", Answering))
        yield forms
    if queue:
        raise AssertionError(f"{len(queue)} form(s) never opened")
    if forms.refused:
        raise AssertionError(f"a form refused its Save: {forms.refused}")


@unittest.skipUnless(URL, "GSM_LIVE_URL names no GeoServer")
class TestLiveGeoServer(unittest.TestCase):
    """The steps share one dialog and one server, in the order of their numbers."""

    @classmethod
    def setUpClass(cls):
        assert ensure_dependencies()
        cls.dlg = SyncDialog()
        cls.dlg.plg_settings = FakePrefs(url=URL)
        cls.dlg._confirm_delete = lambda *args, **kwargs: True
        cls.banners = []
        cls.dlg.show_error_message = lambda text: cls.banners.append(("error", text))
        cls.dlg.show_warning_message = lambda text: cls.banners.append(
            ("warning", text)
        )
        cls.dlg.show_success_message = lambda text: cls.banners.append(("ok", text))
        cls.dlg.refresh_ui()
        cls.connecting = list(cls.banners)
        # A run that stopped half-way left its workspace behind.
        cls.rest("delete", f"workspaces/{WS}", params={"recurse": "true"}, accept=404)
        # Server-wide settings the steps change, put back at the end.
        cls.server = {
            path: cls.rest("get", path).json()
            for path in ("settings/contact.json", "logging.json")
        }

    @classmethod
    def tearDownClass(cls):
        for layer in cls.added_to_qgis:
            QgsProject.instance().removeMapLayer(layer.id())
        if cls.dlg.gs is not None:
            cls.rest(
                "delete", f"workspaces/{WS}", params={"recurse": "true"}, accept=404
            )
            for path, settings in cls.server.items():
                cls.rest("put", path, json=settings)
        cls.dlg.close()

    added_to_qgis = []

    @classmethod
    def rest(cls, method, path, accept=(), **kwargs):
        """A request of the test itself, beside the plugin: to look, or tidy up."""
        accept = (accept,) if isinstance(accept, int) else accept
        base = cls.dlg.gs.rest_service.rest_endpoints.base_url
        return cls.dlg._raw_rest(method, f"{base}/{path}", accept=accept, **kwargs)

    def get(self, path):
        return self.rest("get", path).json()

    def gone(self, path):
        return self.rest("get", path, accept=404).status_code == 404

    def setUp(self):
        self.banners.clear()

    def said(self, kind="ok"):
        """The banners of the step so far, failing on any error or warning."""
        trouble = [text for level, text in self.banners if level != "ok"]
        self.assertEqual(trouble, [], "the plugin reported a problem")
        return [text for level, text in self.banners if level == kind]

    def show_tab(self, label):
        """Open a tab and load it, as a click on its name does."""
        labels = [tab[0] for tab in self.dlg.TABS]
        self.dlg.navList.setCurrentRow(labels.index(label))
        self.dlg._reload_current_tab()
        self.said()
        return self.dlg._all_rows

    def row(self, label, name, workspace=WS):
        """The tab's row for one resource, as the table holds it."""
        rows = [
            row
            for row in self.show_tab(label)
            if row[0] == name and (workspace is None or row[1] == workspace)
        ]
        self.assertEqual(len(rows), 1, f"{name} in {label}: {rows}")
        self.dlg._complete_rows(rows)
        return rows[0]

    # -- Connection and reads ------------------------------------------------

    def test_01_connects(self):
        self.assertTrue(self.dlg.lbl_status.text().startswith("Connected"))
        # A new GeoServer series gets a warning: test it, then add it to
        # _TESTED_SERIES.
        self.assertEqual(self.connecting, [])

    def test_02_every_tab_lists_the_demo_data(self):
        for label, _, _ in self.dlg.TABS:
            with self.subTest(tab=label):
                self.banners.clear()
                rows = self.show_tab(label)
                if label != "Cascaded Stores":  # the demo data has none
                    self.assertGreater(len(rows), 0)

    # -- Workspace -----------------------------------------------------------

    def test_10_add_a_workspace(self):
        with answering({"name": WS}):
            self.dlg._add_workspace()
        self.assertEqual(self.said(), [f"Workspace '{WS}' created."])
        self.assertEqual(self.get(f"workspaces/{WS}.json")["workspace"]["name"], WS)

    def test_11_edit_the_workspace(self):
        uri = "http://example.org/gsm_live"
        with answering({"uri": uri, "wms_own": True, "wms_title": "Live WMS"}):
            self.dlg._show_workspace_info(self.row("Workspaces", WS, workspace=None))
        self.assertEqual(self.said(), [f"Workspace '{WS}' saved."])
        self.assertEqual(self.get(f"namespaces/{WS}.json")["namespace"]["uri"], uri)
        wms = self.get(f"services/wms/workspaces/{WS}/settings.json")["wms"]
        self.assertEqual(wms["title"], "Live WMS")

    # -- Datastores ----------------------------------------------------------

    def test_20_add_a_directory_store(self):
        with answering(
            {
                "type": "Directory of spatial files (shapefiles)",
                "workspace": WS,
                "name": "shapes",
                "file_url": "file:data/sf",
            }
        ):
            self.dlg._add_datastore()
        self.assertEqual(self.said(), ["Datastore 'shapes' created."])
        available = self.dlg._available_tables(WS, "shapes")
        self.assertIn("archsites", available)

    def test_21_add_a_postgis_store(self):
        with answering(
            {
                "type": "PostGIS",
                "workspace": WS,
                "name": "pg",
                "pg_host": "postgis",
                "pg_db": "geoserver",
                "pg_user": "geoserver",
                "pg_password": "geoserver",
            }
        ):
            self.dlg._add_datastore()
        # No "reaches nothing" warning: GeoServer connected to the database.
        self.assertEqual(self.said(), ["Datastore 'pg' created."])
        params = self.store_params("pg")
        self.assertTrue(params["passwd"].startswith("crypt1:"), "stored in clear")

    def store_params(self, name):
        store = self.get(f"workspaces/{WS}/datastores/{name}.json")["dataStore"]
        return {
            entry["@key"]: entry.get("$")
            for entry in store["connectionParameters"]["entry"]
        }

    def test_22_edit_the_postgis_store_keeps_what_the_form_does_not_show(self):
        # A parameter the PostGIS form does not model, set by another client.
        store = self.get(f"workspaces/{WS}/datastores/pg.json")
        store["dataStore"]["connectionParameters"]["entry"].append(
            {"@key": "max connections", "$": "7"}
        )
        self.rest("put", f"workspaces/{WS}/datastores/pg.json", json=store)
        with answering({"description": "edited live"}):
            self.dlg._show_datastore_info(self.row("Datastores", "pg"))
        # Still connects: the blank password field kept the stored one.
        self.assertEqual(self.said(), ["Datastore 'pg' saved."])
        self.assertEqual(self.store_params("pg")["max connections"], "7")
        detail = self.get(f"workspaces/{WS}/datastores/pg.json")["dataStore"]
        self.assertEqual(detail["description"], "edited live")

    def test_23_reset_the_directory_store(self):
        self.dlg._reset_datastore(self.row("Datastores", "shapes"))
        self.assertEqual(self.said(), ["'shapes' reset: GeoServer re-reads it."])

    # -- Layers --------------------------------------------------------------

    def test_30_publish_a_table(self):
        with answering(
            {
                "workspace": WS,
                "datastore": "shapes",
                "table": "archsites",
                "epsg": "26713",
                "title": "Live sites",
            }
        ):
            self.dlg._publish_layer()
        self.assertEqual(len(self.said()), 1)
        layer = self.get(f"layers/{LAYER}.json")["layer"]
        self.assertEqual(layer["type"], "VECTOR")
        feature_type = self.feature_type()
        self.assertEqual(feature_type["title"], "Live sites")
        self.assertEqual(feature_type["srs"], "EPSG:26713")
        # GeoServer computed the box from the data: Spearfish, not the world.
        box = feature_type["latLonBoundingBox"]
        self.assertTrue(-105 < box["minx"] < box["maxx"] < -103, box)

    def feature_type(self):
        path = f"workspaces/{WS}/datastores/shapes/featuretypes/archsites.json"
        return self.get(path)["featureType"]

    def test_31_edit_the_layer(self):
        before = self.feature_type()
        with answering(
            {
                "title": "Live sites, edited",
                "abstract": "From the live test",
                "keywords": ["live", "test"],
            }
        ):
            self.dlg._show_layer_info(self.row("Layers", "archsites"))
        self.assertEqual(len(self.said()), 1)
        after = self.feature_type()
        self.assertEqual(after["title"], "Live sites, edited")
        self.assertEqual(after["abstract"], "From the live test")
        self.assertEqual(sorted(after["keywords"]["string"]), ["live", "test"])
        # A partial PUT: what the form did not change is still there.
        self.assertEqual(after["latLonBoundingBox"], before["latLonBoundingBox"])
        self.assertEqual(after["attributes"], before["attributes"])

    def test_32_upload_a_style(self):
        with answering(
            {"name": "live_point", "workspace": WS, "sld": SLD.format(colour="#FF0000")}
        ):
            self.dlg._add_style()
        self.assertEqual(len(self.said()), 1)
        body = self.rest("get", f"workspaces/{WS}/styles/live_point.sld").text
        self.assertIn("#FF0000", body)

    def test_33_set_the_layer_styles(self):
        with answering({"style": f"{WS}:live_point", "others": ["point"]}):
            self.dlg._set_layer_style(self.row("Layers", "archsites"))
        self.assertEqual(len(self.said()), 1)
        layer = self.get(f"layers/{LAYER}.json")["layer"]
        self.assertEqual(layer["defaultStyle"]["name"], f"{WS}:live_point")
        others = layer["styles"]["style"]
        others = others if isinstance(others, list) else [others]
        self.assertEqual([style["name"] for style in others], ["point"])

    def test_34_what_uses_the_style(self):
        with answering(None) as forms:
            self.dlg._show_style_users(self.row("Styles", "live_point"))
        self.said()
        self.assertIn(f"{LAYER} (default style)", forms.shown[0]["users"])

    def test_35_edit_and_rename_the_style(self):
        with answering({"body": SLD.format(colour="#0000FF"), "name": "live_blue"}):
            self.dlg._show_style_info(self.row("Styles", "live_point"))
        self.assertEqual(len(self.said()), 1)
        self.assertTrue(self.gone(f"workspaces/{WS}/styles/live_point.json"))
        body = self.rest("get", f"workspaces/{WS}/styles/live_blue.sld").text
        self.assertIn("#0000FF", body)
        # The layer follows the rename.
        layer = self.get(f"layers/{LAYER}.json")["layer"]
        self.assertEqual(layer["defaultStyle"]["name"], f"{WS}:live_blue")

    def test_36_copy_the_style(self):
        with answering({"name": "live_copy"}):
            self.dlg._copy_style(self.row("Styles", "live_blue"))
        self.assertEqual(len(self.said()), 1)
        body = self.rest("get", f"workspaces/{WS}/styles/live_copy.sld").text
        self.assertIn("#0000FF", body)

    def test_37_add_the_layer_to_qgis(self):
        for protocol in ("WFS", "WMS", "WMTS"):
            with self.subTest(protocol=protocol):
                before = set(QgsProject.instance().mapLayers())
                with answering({"protocol": protocol}):
                    self.dlg._add_layer_to_qgis(self.row("Layers", "archsites"))
                self.said()
                added = [
                    layer
                    for layer_id, layer in QgsProject.instance().mapLayers().items()
                    if layer_id not in before
                ]
                self.added_to_qgis.extend(added)
                self.assertEqual(len(added), 1)
                self.assertTrue(added[0].isValid())
                if protocol == "WFS":
                    self.assertGreater(added[0].featureCount(), 0)

    # -- Layer groups --------------------------------------------------------

    def test_40_create_a_layer_group(self):
        with answering(
            {
                "name": "live_group",
                "workspace": WS,
                "title": "Live group",
                "layers": [[LAYER, ""]],
            }
        ):
            self.dlg._add_layer_group()
        self.assertEqual(len(self.said()), 1)
        group = self.get(f"workspaces/{WS}/layergroups/live_group.json")["layerGroup"]
        self.assertEqual(group["title"], "Live group")
        # The members' box, in their CRS: not the world, not zero.
        box = group["bounds"]
        self.assertEqual(box["crs"]["$"], "EPSG:26713")
        self.assertTrue(589000 < box["minx"] < box["maxx"] < 609000, box)

    def test_41_edit_the_layer_group(self):
        with answering({"title": "Live group, edited"}):
            self.dlg._show_layer_group_info(self.row("Layer Groups", "live_group"))
        self.assertEqual(len(self.said()), 1)
        group = self.get(f"workspaces/{WS}/layergroups/live_group.json")["layerGroup"]
        self.assertEqual(group["title"], "Live group, edited")

    # -- Tile cache ----------------------------------------------------------

    def gwc_xml(self):
        return self.dlg._raw_rest("get", self.dlg._gwc_layer_path(LAYER, "xml")).text

    def tile(self):
        """Whether GeoWebCache served zoom 0's western tile from its cache."""
        answer = requests.get(
            f"{URL}/gwc/service/tms/1.0.0/{LAYER}@EPSG:4326@png/0/0/0.png",
            auth=AUTH,
            timeout=60,
        )
        answer.raise_for_status()
        return answer.headers.get("geowebcache-cache-result")

    def test_50_edit_the_cached_layer(self):
        with answering({"expire_cache": 60, "meta_width": 2}):
            self.dlg._show_gwc_layer_info(self.row("Tile Cache", LAYER))
        self.assertEqual(len(self.said()), 1)
        xml = self.gwc_xml()
        self.assertIn("<expireCache>60</expireCache>", xml)
        self.assertIn("<int>2</int>", xml)  # metaWidthHeight

    def test_51_seed_the_layer(self):
        with answering(
            {
                "type": "seed",
                "gridset": "EPSG:4326",
                "format": "image/png",
                "zoom_stop": 1,
            },
            None,  # the task list it opens next
        ):
            self.dlg._seed_gwc_layer(self.row("Tile Cache", LAYER))
        self.said()
        path = self.dlg._seed_path(LAYER)
        deadline = time.monotonic() + 120
        while self.dlg._raw_rest("get", path).json()["long-array-array"]:
            self.assertLess(time.monotonic(), deadline, "the seed never ended")
            time.sleep(1)
        self.assertEqual(self.tile(), "HIT")

    def test_52_truncate_the_layer(self):
        self.dlg._truncate_gwc_layer(self.row("Tile Cache", LAYER))
        self.assertEqual(len(self.said()), 1)
        self.assertEqual(self.tile(), "MISS")

    def test_53_stop_and_start_caching_the_layer(self):
        self.dlg._remove_selected_gwc_layers([self.row("Tile Cache", LAYER)])
        self.assertEqual(len(self.said()), 1)
        self.assertNotIn(LAYER, self.dlg._gwc_layer_names())
        self.banners.clear()
        with answering({"layer": LAYER}):
            self.dlg._add_gwc_layer()
        self.assertEqual(len(self.said()), 1)
        self.assertIn(LAYER, self.dlg._gwc_layer_names())

    # -- Coverage stores -----------------------------------------------------

    def test_60_add_a_geotiff_store(self):
        with answering(
            {"name": "dem", "workspace": WS, "url": "file:data/sf/sfdem.tif"}
        ):
            self.dlg._add_coverage_store()
        self.assertEqual(len(self.said()), 1)
        store = self.get(f"workspaces/{WS}/coveragestores/dem.json")["coverageStore"]
        self.assertEqual(store["type"], "GeoTIFF")

    def test_61_publish_its_coverage(self):
        with answering(
            {"native_name": "sfdem", "name": "dem_layer", "title": "Live DEM"}
        ):
            self.dlg._publish_coverage(self.row("Coverage Stores", "dem"))
        self.assertEqual(len(self.said()), 1)
        layer = self.get(f"layers/{WS}:dem_layer.json")["layer"]
        self.assertEqual(layer["type"], "RASTER")

    def test_62_read_the_coverage(self):
        with answering(None) as forms:
            self.dlg._show_coverages(self.row("Coverage Stores", "dem"))
        self.said()
        self.assertEqual(forms.shown[0]["srs"], "EPSG:26713")
        self.assertEqual(forms.shown[0]["title"], "Live DEM")

    def test_63_edit_the_coverage_store(self):
        with answering({"description": "edited live"}):
            self.dlg._show_coverage_store_info(self.row("Coverage Stores", "dem"))
        self.assertEqual(len(self.said()), 1)
        store = self.get(f"workspaces/{WS}/coveragestores/dem.json")["coverageStore"]
        self.assertEqual(store["description"], "edited live")
        self.assertEqual(store["url"], "file:data/sf/sfdem.tif")

    def test_64_reset_the_coverage_store(self):
        self.dlg._reset_coverage_store(self.row("Coverage Stores", "dem"))
        self.assertEqual(len(self.said()), 1)

    # -- Server --------------------------------------------------------------

    def test_70_edit_the_contact(self):
        before = self.get("settings/contact.json")["contact"]
        with answering({"city": "Live City"}):
            self.dlg._show_server_section(self.row("Server", "Contact", workspace=None))
        self.assertEqual(self.said(), ["'Contact' saved."])
        after = self.get("settings/contact.json")["contact"]
        self.assertEqual(after["addressCity"], "Live City")
        # GeoServer replaces the contact on a PUT: the rest must have been sent.
        unchanged = {k: v for k, v in before.items() if k != "addressCity"}
        self.assertEqual({k: after.get(k) for k in unchanged}, unchanged)

    def test_71_edit_the_logging(self):
        before = self.get("logging.json")["logging"]
        with answering({"level": "VERBOSE_LOGGING"}):
            self.dlg._show_server_section(self.row("Server", "Logging", workspace=None))
        self.assertEqual(self.said(), ["'Logging' saved."])
        after = self.get("logging.json")["logging"]
        self.assertEqual(after, dict(before, level="VERBOSE_LOGGING"))

    # -- Deletes, in the order the references allow --------------------------

    def test_90_delete_the_layer_group(self):
        self.dlg._delete_selected_layer_groups([self.row("Layer Groups", "live_group")])
        self.assertEqual(len(self.said()), 1)
        self.assertTrue(self.gone(f"workspaces/{WS}/layergroups/live_group.json"))

    def test_91_delete_the_layers(self):
        rows = [self.row("Layers", "archsites"), self.row("Layers", "dem_layer")]
        self.dlg._delete_selected_layers(rows)
        self.assertEqual(len(self.said()), 1)
        self.assertTrue(self.gone(f"layers/{LAYER}.json"))
        self.assertTrue(self.gone(f"layers/{WS}:dem_layer.json"))

    def test_92_delete_the_styles(self):
        rows = [self.row("Styles", "live_blue"), self.row("Styles", "live_copy")]
        self.dlg._delete_selected_styles(rows)
        self.assertEqual(len(self.said()), 1)
        self.assertTrue(self.gone(f"workspaces/{WS}/styles/live_blue.json"))
        self.assertTrue(self.gone(f"workspaces/{WS}/styles/live_copy.json"))

    def test_93_delete_the_stores(self):
        self.dlg._delete_selected_coverage_stores([self.row("Coverage Stores", "dem")])
        self.assertEqual(len(self.said()), 1)
        self.banners.clear()
        rows = [self.row("Datastores", "pg"), self.row("Datastores", "shapes")]
        self.dlg._delete_selected_datastores(rows)
        self.assertEqual(len(self.said()), 1)
        for path in ("coveragestores/dem", "datastores/pg", "datastores/shapes"):
            self.assertTrue(self.gone(f"workspaces/{WS}/{path}.json"))

    def test_94_delete_the_workspace(self):
        self.dlg._delete_selected_workspaces(
            [self.row("Workspaces", WS, workspace=None)]
        )
        self.assertEqual(self.said(), [f"Workspace '{WS}' deleted."])
        self.assertTrue(self.gone(f"workspaces/{WS}.json"))
