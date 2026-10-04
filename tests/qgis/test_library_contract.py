#! python3  # noqa E265

"""
Lock the parts of geoservercloud's behaviour the plugin relies on, using the
bundled wheel itself; no server needed.

Usage from the repo root folder:

.. code-block:: bash

    QT_QPA_PLATFORM=offscreen python -m unittest tests.qgis.test_library_contract
"""

# standard library
import inspect
import re

from geoservercloud import GeoServerCloud  # noqa: E402
from geoservercloud.models.datastore import DataStore  # noqa: E402
from geoservercloud.models.layergroup import LayerGroup  # noqa: E402
from geoservercloud.models.workspace import Workspace  # noqa: E402
from qgis.testing import unittest

# project
from geoserver_manager.toolbelt.dependencies import BUNDLED_WHLS, GSC_REQUIRED

# ############################################################################
# ########## Classes #############
# ################################


class TestBundledVersion(unittest.TestCase):
    def test_pin_matches_the_bundled_wheel(self):
        """dependencies.GSC_REQUIRED must be the version actually shipped."""
        wheel = [w for w in BUNDLED_WHLS if w.name.startswith("geoservercloud-")][0]
        self.assertTrue(wheel.exists(), wheel)
        version = re.match(r"geoservercloud-([\d.]+)-", wheel.name).group(1)
        self.assertEqual(version, GSC_REQUIRED)


class TestDatastoreUpdateContract(unittest.TestCase):
    """_update_datastore_from_values merges onto the fetched parameters and
    hands the result to create_datastore(). These assertions fail if the
    library changes the shape that merge relies on.
    """

    def test_create_datastore_accepts_what_the_plugin_passes(self):
        params = inspect.signature(GeoServerCloud.create_datastore).parameters
        for name in (
            "workspace_name",
            "datastore_name",
            "datastore_type",
            "connection_parameters",
            "description",
            "enabled",
        ):
            self.assertIn(name, params)

    def test_put_payload_sends_the_whole_parameter_map_and_enabled(self):
        merged = {"host": "h", "max connections": "20", "Loose bbox": "false"}
        payload = DataStore("ws", "store", merged, type="PostGIS", enabled=False)
        body = payload.put_payload()["dataStore"]

        # GeoServer replaces connectionParameters wholesale, so every key we
        # merged must be present in what is sent…
        sent = {e["@key"]: e["$"] for e in body["connectionParameters"]["entry"]}
        self.assertEqual(sent, merged)
        # …and a disabled store must be sent as disabled, not dropped/defaulted.
        self.assertIs(body["enabled"], False)
        self.assertEqual(body["type"], "PostGIS")

    def test_workspace_rename_payload_shape(self):
        """_put_workspace PUTs Workspace(new_name, isolated).put_payload()."""
        self.assertEqual(
            Workspace("new", True).put_payload(),
            {"workspace": {"name": "new", "isolated": True}},
        )


class TestRestClientPolicy(unittest.TestCase):
    """_check exists because these statuses are *not* raised by the library."""

    def test_which_statuses_pass_through(self):
        from geoservercloud.services import restclient

        src = inspect.getsource(restclient.RestClient)
        self.assertIn("if response.status_code != 404:", src)  # GET / DELETE
        self.assertIn("if response.status_code != 409:", src)  # POST
        self.assertTrue(hasattr(restclient, "TIMEOUT"))
        self.assertNotIn("timeout", inspect.signature(restclient.RestClient).parameters)


class TestLayerGroupModes(unittest.TestCase):
    def test_the_tab_offers_exactly_the_librarys_modes(self):
        """tab_layergroups.MODES spells out the enum, so it must not drift."""
        from geoserver_manager.gui.tab_layergroups import MODES

        self.assertEqual(list(MODES), LayerGroup.modes)


class TestStylePathExtensions(unittest.TestCase):
    def test_the_builder_appends_an_extension_for_three_formats_only(self):
        """Why tab_styles._style_path swaps the suffix of the .json path: a CSS
        or YSLD body PUT to the bare path is a 500 "No such style handler"."""
        from geoservercloud.services.restservice import RestService

        endpoints = RestService.RestEndpoints("/rest")
        for known in ("json", "sld", "mbstyle"):
            self.assertEqual(
                endpoints.style("s", "ws", format=known),
                f"/rest/workspaces/ws/styles/s.{known}",
            )
        for bare in ("css", "ysld"):
            self.assertEqual(endpoints.style("s", format=bare), "/rest/styles/s")


class TestPathBuildersInterpolateRaw(unittest.TestCase):
    def test_names_reach_the_path_as_they_were_given(self):
        """The plugin quotes names before these builders see them: _q in
        tab_cascaded and tab_coveragestores, _datastore_path, _style_path,
        _group_path, _resource_path and the layer-group listing. A library
        that quoted them too would send "a b" as a%2520b, and every such
        request would 404."""
        from geoservercloud.services.restservice import RestService

        endpoints = RestService.RestEndpoints("/rest")
        ws, name = "a b", "c#d"
        built = {
            endpoints.datastore(ws, name): "datastores/c#d.json",
            endpoints.featuretype(ws, name, name): (
                "datastores/c#d/featuretypes/c#d.json"
            ),
            endpoints.style(name, ws): "styles/c#d.json",
            endpoints.coveragestore(ws, name): "coveragestores/c#d.json",
            endpoints.coveragestore(ws, name, "file", "geotiff"): (
                "coveragestores/c#d/file.geotiff"
            ),
            endpoints.coverage(ws, name, name): "coveragestores/c#d/coverages/c#d.json",
            endpoints.layergroup(ws, name): "layergroups/c#d.json",
            endpoints.layergroups(ws): "layergroups.json",
            endpoints.wmsstore(ws, name): "wmsstores/c#d.json",
            endpoints.wmtsstore(ws, name): "wmtsstores/c#d.json",
            endpoints.wmslayer(ws, name, name): "wmsstores/c#d/wmslayers/c#d.json",
            endpoints.wmtslayer(ws, name, name): "wmtsstores/c#d/layers/c#d.json",
        }
        for path, rest in built.items():
            self.assertEqual(path, f"/rest/workspaces/a b/{rest}")


# ############################################################################
# ####### Stand-alone run ########
# ################################
if __name__ == "__main__":
    unittest.main()
