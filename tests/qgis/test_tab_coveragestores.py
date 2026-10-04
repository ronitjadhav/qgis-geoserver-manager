#! python3  # noqa E265

"""
Usage from the repo root folder:

.. code-block:: bash

    python -m unittest tests.qgis.test_tab_coveragestores
"""

# standard library
from pathlib import Path
from unittest.mock import patch

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QDialog
from qgis.testing import start_app, unittest

# project
from geoserver_manager.gui import tab_coveragestores
from geoserver_manager.gui.dlg_main import GeoServerMainDialog
from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog
from geoserver_manager.gui.tab_coveragestores import (
    ARCGRID,
    COG,
    GEOTIFF,
    MOSAIC_DIRECTORY,
    MOSAIC_ZIP,
    WORLDIMAGE,
    CoverageStoreTabMixin,
)
from geoserver_manager.toolbelt.qgis_export import (
    export_to_geotiff,
    local_geotiff_path,
)
from tests.qgis.sync_dialog import SyncDialog

start_app()

# A store and a coverage as GeoServer really answers them: the store carries a
# description the library's model drops, the coverage a bounding box and
# keywords its model drops, and a single band is not wrapped in a list.
SFDEM_STORE = {
    "name": "sfdem",
    "description": "Digital elevation model for Spearfish.",
    "type": "GeoTIFF",
    "enabled": True,
    "workspace": {"name": "sf"},
    "url": "file:data/sf/sfdem.tif",
}

SFDEM_COVERAGE = {
    "name": "sfdem",
    "nativeName": "sfdem",
    "title": "Spearfish elevation",
    "description": "Elevation in metres.",
    "keywords": {"string": ["WCS", "sfdem"]},
    "srs": "EPSG:26713",
    "nativeFormat": "GeoTIFF",
    "enabled": True,
    "nativeBoundingBox": {
        "minx": 589980,
        "maxx": 609000,
        "miny": 4913700,
        "maxy": 4928010,
        "crs": {"@class": "projected", "$": "EPSG:26713"},
    },
    "grid": {"@dimension": 2, "range": {"low": "0 0", "high": "634 477"}},
    "dimensions": {
        "coverageDimension": {"name": "GRAY_INDEX", "range": {"min": -100, "max": 2000}}
    },
}


# ############################################################################
# ########## Fakes ###############
# ################################


class FakeGS:
    """Two workspaces: sf has one GeoTIFF store, nurc an unpublished mosaic."""

    def __init__(self, broken_workspace=None, exists=False):
        self.broken_workspace = broken_workspace
        self.exists = exists
        self.calls = []
        outer = self

        class Response:
            def __init__(self, payload):
                self._payload = payload
                self.status_code = 200
                self.text = str(payload)

            def json(self):
                return self._payload

        class Client:
            def get(inner, path, **kwargs):
                outer.calls.append(("GET", path, kwargs))
                return Response(outer.payload_for(path, kwargs.get("params") or {}))

            def put(inner, path, **kwargs):
                data = kwargs.pop("data", None)
                if hasattr(data, "read"):  # an upload streams a file handle
                    data = data.read()
                outer.calls.append(("PUT", path, dict(kwargs, data=data)))
                return Response("")

        class Endpoints:
            base_url = "/rest"

            def coveragestores(inner, workspace_name):
                return f"/rest/workspaces/{workspace_name}/coveragestores.json"

            def coveragestore(
                inner, workspace_name, name, method=None, store_type=None
            ):
                base = f"/rest/workspaces/{workspace_name}/coveragestores/{name}"
                if method is None and store_type is None:
                    return f"{base}.json"
                return f"{base}/{method}.{store_type}"

            def coverages(inner, workspace_name, store_name):
                return (
                    f"/rest/workspaces/{workspace_name}"
                    f"/coveragestores/{store_name}/coverages.json"
                )

            def coverage(inner, workspace_name, store_name, name):
                return (
                    f"/rest/workspaces/{workspace_name}"
                    f"/coveragestores/{store_name}/coverages/{name}.json"
                )

        class Rest:
            rest_client = Client()
            rest_endpoints = Endpoints()

            def resource_exists(inner, path):
                return False  # no layer of the name in another store

        self.rest_service = Rest()

    # -- payloads -----------------------------------------------------------

    STORES = {"sf": ["sfdem"], "nurc": ["mosaic"], "empty": []}
    PUBLISHED = {"sfdem": ["sfdem"], "mosaic": []}  # the mosaic has none yet

    def payload_for(self, path, params):
        if path.endswith("/coveragestores.json"):
            workspace_name = path.split("/workspaces/")[1].split("/")[0]
            if workspace_name == self.broken_workspace:
                raise RuntimeError("HTTP 500: boom")
            names = self.STORES.get(workspace_name, [])
            if not names:
                return {"coverageStores": ""}  # how GeoServer spells "none"
            return {"coverageStores": {"coverageStore": [{"name": n} for n in names]}}
        if path.endswith("/coverages.json"):
            store_name = path.split("/coveragestores/")[1].split("/")[0]
            names = (
                self.PUBLISHED[store_name]
                if params.get("list") == "configured"
                else ["mosaic"]
            )
            if not names:
                return {"coverages": ""}
            return {"coverages": {"coverage": [{"name": n} for n in names]}}
        if "/coverages/" in path:
            return {"coverage": SFDEM_COVERAGE}
        if "/coveragestores/" in path:
            name = path.rsplit("/", 1)[1].removesuffix(".json")
            store = dict(SFDEM_STORE, name=name)
            if name == "mosaic":
                store.update(type="ImageMosaic", workspace={"name": "nurc"})
            return {"coverageStore": store}
        raise AssertionError(f"unexpected GET {path}")

    # -- library calls ------------------------------------------------------

    def get_workspaces(self):
        return ([{"name": "sf"}, {"name": "nurc"}, {"name": "empty"}], 200)

    def get_coverage_store(self, workspace_name, name):
        self.calls.append(("get_coverage_store", workspace_name, name))
        # The library's CoverageStore keeps the type, as GeoServer reports it.
        return ({"name": name, "type": "GeoTIFF"}, 200 if self.exists else 404)

    def get_coverages(self, workspace_name, store_name):
        # The library answers list=all: everything the store can expose.
        return ([{"name": "mosaic"}, {"name": "extra"}], 200)

    def get_coverage(self, workspace_name, store_name, name):
        self.calls.append(("get_coverage", workspace_name, store_name, name))
        # Published under its own name: GeoServer writes no nativeCoverageName.
        return ({"name": name, "nativeName": name}, 200)

    def create_coverage_store(
        self, workspace_name, name, url, type=None, metadata=None
    ):
        self.calls.append(
            ("create_coverage_store", workspace_name, name, url, type, metadata)
        )
        return ("", 201)

    def create_imagemosaic_store_from_directory(self, workspace_name, name, directory):
        self.calls.append(("from_directory", workspace_name, name, directory))
        return (name, 201)

    def create_imagemosaic_store_from_properties_zip(self, workspace_name, name, blob):
        self.calls.append(("from_zip", workspace_name, name, blob))
        return ("", 201)

    def create_coverage(
        self, workspace_name, store_name, name, title=None, native_name=None
    ):
        self.calls.append(
            ("create_coverage", workspace_name, store_name, name, title, native_name)
        )
        return ("", 201)

    def delete_coverage_store(self, workspace_name, name):
        self.calls.append(("delete_coverage_store", workspace_name, name))
        return ("", 200)


class Recording(ResourceFormDialog):
    opened = []

    def exec(self):
        Recording.opened.append(self)
        return QDialog.DialogCode.Rejected


# ############################################################################
# ########## Tests ###############
# ################################


class TestCoverageStoresTab(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        self.warnings = []
        self.dlg.show_warning_message = self.warnings.append
        self.dlg.show_error_message = lambda text: self.fail(f"unexpected: {text}")
        self.dlg.show_success_message = lambda text: None
        Recording.opened.clear()

    def test_lists_stores_with_their_type_and_published_count(self):
        self.dlg._load_coverage_stores()  # the page's details fill as it shows
        self.assertEqual(
            self.dlg._all_rows,
            [  # workspace order, as get_workspaces gives them
                ["sfdem", "sf", "GeoTIFF", "1"],
                ["mosaic", "nurc", "ImageMosaic", "0"],  # created, nothing published
            ],
        )
        self.assertEqual(self.warnings, [])

    def test_add_without_workspaces_says_so_before_the_form(self):
        self.dlg._get_workspace_names = lambda: []
        with patch.object(tab_coveragestores, "ResourceFormDialog", Recording):
            self.dlg._add_coverage_store()
        self.assertEqual(Recording.opened, [])
        self.assertIn("Create a workspace first", self.warnings[0])

    def test_a_coverage_that_cannot_be_read_leaves_the_viewer_alone(self):
        """After the error banner, blank fields must not claim Enabled: Yes."""
        self.dlg._fetch = lambda action, failure, **kwargs: None
        self.assertIsNone(self.dlg._coverage_values("sf", "sfdem", "sfdem"))

    def test_a_store_that_cannot_be_read_keeps_its_row(self):
        """It is the one to delete; it used to vanish from the table."""
        summary = self.dlg._coverage_store_summary

        def broken(ws_name, name):
            if name == "mosaic":
                raise RuntimeError("HTTP 500: corrupt mosaic")
            return summary(ws_name, name)

        self.dlg._coverage_store_summary = broken
        self.dlg._load_coverage_stores()
        self.assertIn(["mosaic", "nurc", "-", "-"], self.dlg._all_rows)
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("nurc:mosaic", self.warnings[0])

    def test_a_workspace_without_stores_is_not_a_failure(self):
        # GeoServer answers {"coverageStores": ""}, not a list, not an error.
        self.assertEqual(self.dlg._coverage_store_names("empty"), [])

    def test_one_unreadable_workspace_keeps_the_rest(self):
        self.dlg.gs = FakeGS(broken_workspace="nurc")
        self.dlg._load_coverage_stores()
        self.assertEqual([row[0] for row in self.dlg._all_rows], ["sfdem"])
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("nurc", self.warnings[0])

    def test_published_and_available_are_asked_for_separately(self):
        self.dlg._published_coverage_names("sf", "sfdem")
        _verb, path, kwargs = self.dlg.gs.calls[-1]
        # The library hardcodes list=all, which would count unpublished ones too
        self.assertEqual(kwargs["params"], {"list": "configured"})
        self.assertIn("/coveragestores/sfdem/coverages.json", path)

    def test_publish_candidates_are_the_unpublished_ones(self):
        # get_coverages (list=all) answers mosaic + extra; mosaic is published
        self.dlg.gs.PUBLISHED = {"mosaic": ["mosaic"]}
        self.assertEqual(self.dlg._publishable_coverages("nurc", "mosaic"), ["extra"])

    def test_a_coverage_published_under_another_name_is_not_offered_again(self):
        """list=all answers native names, list=configured the published ones:
        "extra" published as "elev" stayed on offer, and a second publish made
        a duplicate layer."""
        self.dlg.gs.PUBLISHED = {"mosaic": ["elev"]}
        # As GeoServer stores a coverage published under another name (measured).
        self.dlg.gs.get_coverage = lambda ws, store, name: (
            {"name": "elev", "nativeName": "extra"},
            200,
        )
        self.assertEqual(self.dlg._publishable_coverages("nurc", "mosaic"), ["mosaic"])

        # An uploaded raster renamed later: GeoServer also wrote nativeCoverageName.
        self.dlg.gs.PUBLISHED = {"mosaic": ["elev", "dem"]}
        stored = {
            "elev": {"name": "elev", "nativeName": "extra"},
            "dem": {
                "name": "dem",
                "nativeName": "mosaic",
                "nativeCoverageName": "mosaic",
            },
        }
        self.dlg.gs.get_coverage = lambda ws, store, name: (stored[name], 200)
        with patch.object(tab_coveragestores, "ResourceFormDialog", Recording):
            self.dlg._publish_coverage(["mosaic", "nurc"])
        self.assertEqual(Recording.opened, [])
        self.assertIn("Every coverage of 'mosaic' is already published.", self.warnings)


class TestStoreAndCoverageDetail(unittest.TestCase):
    """The detail views read what GeoServer stores, not what the models keep."""

    def setUp(self):
        Recording.opened.clear()  # class-level: order must not matter

    def test_store_prefill_keeps_the_description(self):
        values = CoverageStoreTabMixin._coverage_store_form_values(
            SFDEM_STORE, ["sfdem"]
        )
        # CoverageStore.from_get_response_payload() drops "description": this
        # would be empty if the detail came from the library's model.
        self.assertEqual(
            values["description"], "Digital elevation model for Spearfish."
        )
        self.assertEqual(values["type"], "GeoTIFF")
        self.assertEqual(values["workspace"], "sf")
        self.assertEqual(values["url"], "file:data/sf/sfdem.tif")
        self.assertEqual(values["coverages"], "sfdem")

    def test_store_without_published_coverages_says_so(self):
        values = CoverageStoreTabMixin._coverage_store_form_values(SFDEM_STORE, [])
        self.assertEqual(values["coverages"], "-")

    def test_coverage_prefill_keeps_the_bbox_keywords_and_bands(self):
        values = CoverageStoreTabMixin._coverage_form_values(SFDEM_COVERAGE)
        # Coverage.asdict() drops all three of these.
        self.assertIn("589980, 4913700 → 609000, 4928010", values["bounds"])
        self.assertIn("EPSG:26713", values["bounds"])  # crs arrives as {"$": …}
        self.assertEqual(values["keywords"], "WCS, sfdem")
        self.assertEqual(values["bands"], "GRAY_INDEX  (-100 … 2000)")
        # GeoServer's grid "high" is the exclusive bound: this file is 634 x 477
        # (checked with gdalinfo on the demo data's sfdem.tif)
        self.assertEqual(values["size"], "634 × 477")
        self.assertEqual(values["title"], "Spearfish elevation")
        self.assertEqual(values["srs"], "EPSG:26713")

    def test_the_viewer_prefers_the_abstract_over_the_generated_description(self):
        both = {"abstract": "Written by hand", "description": "Generated from GeoTIFF"}
        self.assertEqual(
            CoverageStoreTabMixin._coverage_form_values(both)["abstract"],
            "Written by hand",
        )
        only = {"description": "Generated from x"}
        self.assertEqual(
            CoverageStoreTabMixin._coverage_form_values(only)["abstract"],
            "Generated from x",
        )

    def test_a_coverage_without_grid_or_bands_still_renders(self):
        values = CoverageStoreTabMixin._coverage_form_values({"name": "bare"})
        self.assertEqual(values["size"], "")
        self.assertEqual(values["bounds"], "")
        self.assertEqual(values["bands"], "-")

    def store_dialog(self, **edits):
        """Open the store form, apply `edits`, and press Save."""
        dlg = SyncDialog()
        dlg.gs = FakeGS()
        sent = []
        dlg._raw_rest = lambda method, path, **kw: sent.append((method, path, kw))
        dlg._coverage_store_detail = lambda ws, name: dict(SFDEM_STORE)
        dlg._published_coverage_names = lambda ws, name: ["sfdem"]
        dlg._warn_if_store_unreachable = lambda name, read: None
        dlg._load_coverage_stores = lambda: None
        dlg.show_success_message = lambda text: None

        class Editing(ResourceFormDialog):
            opened = []

            def exec(inner):
                Editing.opened.append(inner)
                for key, value in edits.items():
                    widget = inner.get_widget(key)
                    if hasattr(widget, "setPlainText"):
                        widget.setPlainText(value)
                    elif hasattr(widget, "setChecked") and isinstance(value, bool):
                        widget.setChecked(value)
                    else:
                        widget.setText(value)
                return QDialog.DialogCode.Accepted

        with patch.object(tab_coveragestores, "ResourceFormDialog", Editing):
            dlg._show_coverage_store_info(["sfdem", "sf"])
        return dlg, Editing.opened[-1], sent

    def test_the_store_can_be_edited_here_workspace_and_type_stay(self):
        _dlg, form, _sent = self.store_dialog()
        self.assertFalse(form.get_widget("url").isReadOnly())
        self.assertFalse(form.get_widget("name").isReadOnly())
        self.assertTrue(form.get_widget("workspace").isReadOnly())
        self.assertTrue(form.get_widget("type").isReadOnly())

    def test_save_sends_only_what_changed(self):
        _dlg, _form, sent = self.store_dialog(description="Edited", enabled=False)
        ((method, path, kwargs),) = sent
        self.assertEqual(method, "put")
        self.assertTrue(path.endswith("/workspaces/sf/coveragestores/sfdem.json"), path)
        self.assertEqual(
            kwargs["json"],
            {"coverageStore": {"enabled": False, "description": "Edited"}},
        )

    def test_an_untouched_form_sends_nothing(self):
        _dlg, _form, sent = self.store_dialog()
        self.assertEqual(sent, [])

    def test_a_rename_onto_a_taken_name_stays_in_the_form_and_sends_nothing(self):
        """Refused after the form closed, the rest of the edit was lost."""
        dlg = SyncDialog()
        dlg.gs = FakeGS(exists=True)
        sent = []
        dlg._raw_rest = lambda method, path, **kw: sent.append(method)
        dlg._coverage_store_detail = lambda ws, name: dict(SFDEM_STORE)
        dlg._published_coverage_names = lambda ws, name: ["sfdem"]
        seen = {}

        class Editing(ResourceFormDialog):
            def exec(inner):
                inner.get_widget("name").setText("taken")
                inner.get_widget("description").setPlainText("Typed")
                inner._on_accept()
                seen["open"] = not inner.result()
                seen["said"] = inner._validation_label.text()
                return QDialog.DialogCode.Rejected

        with patch.object(tab_coveragestores, "ResourceFormDialog", Editing):
            dlg._show_coverage_store_info(["sfdem", "sf"])
        self.assertTrue(seen["open"])
        self.assertIn("'taken' already exists", seen["said"])
        with self.assertRaises(ValueError):  # the save checks it again
            dlg._save_coverage_store("sf", "sfdem", {"name": "taken"})
        self.assertEqual(sent, [])

    def test_reset_posts_to_the_stores_reset_path(self):
        dlg = SyncDialog()
        dlg.gs = FakeGS()
        sent = []
        dlg._raw_rest = lambda method, path, **kw: sent.append((method, path))
        dlg.show_success_message = lambda text: None
        dlg._reset_coverage_store(["sfdem", "sf"])
        self.assertEqual(sent[0][0], "post")
        self.assertTrue(sent[0][1].endswith("/coveragestores/sfdem/reset"), sent)

    def test_the_coverage_viewer_fills_itself_from_the_picked_coverage(self):
        dlg = SyncDialog()
        dlg.gs = FakeGS()
        dlg.show_warning_message = lambda text: None
        with patch.object(tab_coveragestores, "ResourceFormDialog", Recording):
            dlg._show_coverages(["sfdem", "sf"])
        form = Recording.opened[-1]
        self.assertEqual(form.get_widget("coverage").currentText(), "sfdem")
        self.assertEqual(form.get_widget("srs").text(), "EPSG:26713")
        self.assertIn("609000", form.get_widget("bounds").text())
        self.assertIn("GRAY_INDEX", form.get_widget("bands").toPlainText())

    def test_a_store_with_nothing_published_says_so_instead_of_an_empty_dialog(self):
        dlg = SyncDialog()
        dlg.gs = FakeGS()
        warnings = []
        dlg.show_warning_message = warnings.append
        with patch.object(tab_coveragestores, "ResourceFormDialog", Recording):
            dlg._show_coverages(["mosaic", "nurc"])
        self.assertEqual(Recording.opened, [])
        self.assertIn("no published coverage", warnings[0])


class TestCreateCoverageStore(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()

    def test_geotiff_goes_through_the_library_with_no_cog_metadata(self):
        self.dlg._create_coverage_store_from_values(
            {
                "name": "dem",
                "workspace": "sf",
                "type": GEOTIFF,
                "url": "file:data/sf/sfdem.tif",
            }
        )
        self.assertEqual(
            self.dlg.gs.calls[-1],
            (
                "create_coverage_store",
                "sf",
                "dem",
                "file:data/sf/sfdem.tif",
                "GeoTIFF",
                None,
            ),
        )

    def test_arcgrid_and_worldimage_keep_their_own_type(self):
        # GeoServer reads the file with the reader the type names: an ArcGrid
        # store created as a GeoTIFF one fails to open.
        for store_type in (ARCGRID, WORLDIMAGE):
            self.dlg._create_coverage_store_from_values(
                {"name": "s", "workspace": "sf", "type": store_type, "url": "file:x"}
            )
            self.assertEqual(self.dlg.gs.calls[-1][4], store_type)

    def test_cog_is_a_geotiff_store_plus_the_cog_settings(self):
        self.dlg._create_coverage_store_from_values(
            {
                "name": "cog",
                "workspace": "sf",
                "type": COG,
                "url": "https://example.org/dem.tif",
            }
        )
        _call, _ws, _name, url, store_type, metadata = self.dlg.gs.calls[-1]
        self.assertEqual((url, store_type), ("https://example.org/dem.tif", "GeoTIFF"))
        self.assertEqual(metadata, {"cogSettings": {"rangeReaderSettings": "HTTP"}})

    def test_a_mosaic_directory_uses_the_directory_call(self):
        self.dlg._create_coverage_store_from_values(
            {
                "name": "mos",
                "workspace": "nurc",
                "type": MOSAIC_DIRECTORY,
                "directory": "/opt/geoserver_data/coverages/mos",
            }
        )
        self.assertEqual(
            self.dlg.gs.calls[-1],
            ("from_directory", "nurc", "mos", "/opt/geoserver_data/coverages/mos"),
        )

    def test_a_properties_zip_streams_to_the_imagemosaic_file_endpoint(self):
        """Not read into memory under the wait cursor: the ZIP holds granules."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
            handle.write(b"PK\x03\x04 pretend")
            path = handle.name
        self.dlg._upload_mosaic_zip(
            {"name": "mos", "workspace": "nurc", "type": MOSAIC_ZIP, "zip": path}
        )
        puts = [call for call in self.dlg.gs.calls if call[0] == "PUT"]
        self.assertEqual(len(puts), 1)  # the reload afterwards only GETs
        _verb, url, kwargs = puts[0]
        self.assertTrue(url.endswith("/coveragestores/mos/file.imagemosaic"), url)
        self.assertEqual(kwargs["params"], {"configure": "none"})
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/zip")
        self.assertEqual(kwargs["data"], b"PK\x03\x04 pretend")
        self.assertTrue(Path(path).exists())  # the user's own file is not removed
        Path(path).unlink()

    def test_a_cancelled_zip_upload_does_not_offer_replace(self):
        """The GeoTIFF's report says "upload it again with Replace ticked",
        which the ZIP form has no box for."""
        import tempfile

        warnings = []
        self.dlg.show_warning_message = warnings.append
        captured = {}
        self.dlg._upload_file = lambda *args, **kwargs: captured.update(
            on_cancel=args[7]
        )
        with tempfile.NamedTemporaryFile(suffix=".zip") as handle:
            self.dlg._upload_mosaic_zip(
                {
                    "name": "mos",
                    "workspace": "nurc",
                    "type": MOSAIC_ZIP,
                    "zip": handle.name,
                }
            )
        self.dlg.gs = FakeGS(exists=True)  # what a cancel could leave behind
        captured["on_cancel"](None)
        self.assertIn("'mos'", warnings[-1])
        self.assertNotIn("Replace", warnings[-1])

    def test_a_server_that_drops_the_cog_settings_is_reported(self):
        """GeoServer silently ignores store metadata it does not understand."""
        warnings = []
        self.dlg.show_warning_message = warnings.append
        self.dlg.show_success_message = lambda text: None

        class Accepting(ResourceFormDialog):
            def exec(inner):
                inner.get_widget("name").setText("cog")
                inner.get_widget("type").setCurrentText(COG)
                inner.get_widget("url").setText("https://example.org/dem.tif")
                return QDialog.DialogCode.Accepted

        self.dlg._load_coverage_stores = lambda: None
        with patch.object(tab_coveragestores, "ResourceFormDialog", Accepting):
            self.dlg._add_coverage_store()  # the fake store has no metadata
        self.assertEqual(len(warnings), 1)
        self.assertIn("COG extension", warnings[0])

    def test_a_server_that_keeps_them_says_nothing(self):
        kept = FakeGS()
        original = kept.payload_for

        def with_metadata(path, params):
            payload = original(path, params)
            if "coverageStore" in payload:
                payload["coverageStore"]["metadata"] = {
                    "entry": {"@key": "CogSettings.Key"}
                }
            return payload

        kept.payload_for = with_metadata
        self.dlg.gs = kept
        warnings = []
        self.dlg.show_warning_message = warnings.append
        self.dlg._warn_if_cog_settings_dropped(
            {"type": COG, "name": "cog", "workspace": "sf"}
        )
        self.assertEqual(warnings, [])

    def test_the_zip_help_warns_that_a_granule_is_required(self):
        """GeoServer's upload validator refuses a properties-only archive."""
        field = [
            f for f in self.dlg._coverage_store_fields(["sf"]) if f["key"] == "zip"
        ][0]
        self.assertIn("at least one granule", field["help"])

    def test_an_existing_name_is_refused_before_anything_is_sent(self):
        self.dlg.gs = FakeGS(exists=True)
        with self.assertRaises(ValueError):
            self.dlg._create_coverage_store_from_values(
                {"name": "sfdem", "workspace": "sf", "type": GEOTIFF, "url": "file:x"}
            )
        self.assertFalse(
            [call for call in self.dlg.gs.calls if call[0].startswith("create")]
        )

    def test_the_type_combo_shows_only_that_type_s_source_field(self):
        dlg = ResourceFormDialog(
            title="t", fields=self.dlg._coverage_store_fields(["sf", "nurc"])
        )
        dlg.get_widget("type").currentTextChanged.connect(
            lambda store_type: self.dlg._on_store_type_changed(dlg, store_type)
        )
        self.dlg._on_store_type_changed(dlg, GEOTIFF)
        self.assertNotIn("url", dlg._hidden_keys)
        self.assertIn("directory", dlg._hidden_keys)
        self.assertIn("zip", dlg._hidden_keys)

        dlg.get_widget("type").setCurrentText(MOSAIC_ZIP)
        self.assertIn("url", dlg._hidden_keys)
        self.assertIn("directory", dlg._hidden_keys)
        self.assertNotIn("zip", dlg._hidden_keys)

    def test_a_store_name_with_a_hash_never_reaches_the_server(self):
        """requests sends coveragestores/a#b.json as coveragestores/a, a different store."""
        with self.assertRaises(ValueError):
            self.dlg._create_coverage_store_from_values(
                {
                    "name": "dem#2",
                    "workspace": "sf",
                    "type": GEOTIFF,
                    "url": "file:x.tif",
                }
            )
        self.assertEqual(
            [call for call in self.dlg.gs.calls if call[0].startswith("create")], []
        )


class TestPublishAndDelete(unittest.TestCase):
    def setUp(self):
        Recording.opened.clear()  # class-level: order must not matter
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        self.dlg._confirm_delete = lambda question, labels=(), cascade="": True
        self.dlg._load_coverage_stores = lambda: None
        self.dlg.show_success_message = lambda text: None
        self.dlg.show_warning_message = lambda text: None
        self.dlg.show_error_message = lambda text: self.fail(f"unexpected: {text}")

    def test_publishing_sends_the_native_name_and_the_chosen_layer_name(self):
        class Accepting(ResourceFormDialog):
            def exec(inner):
                inner.get_widget("name").setText("mosaic_layer")
                return QDialog.DialogCode.Accepted

        with patch.object(tab_coveragestores, "ResourceFormDialog", Accepting):
            self.dlg._publish_coverage(["mosaic", "nurc"])
        self.assertEqual(
            self.dlg.gs.calls[-1],
            ("create_coverage", "nurc", "mosaic", "mosaic_layer", None, "mosaic"),
        )

    def test_an_empty_layer_name_reuses_the_coverage_name(self):
        class Accepting(ResourceFormDialog):
            def exec(inner):
                return QDialog.DialogCode.Accepted

        with patch.object(tab_coveragestores, "ResourceFormDialog", Accepting):
            self.dlg._publish_coverage(["mosaic", "nurc"])
        self.assertEqual(
            self.dlg.gs.calls[-1],
            ("create_coverage", "nurc", "mosaic", "mosaic", None, "mosaic"),
        )

    def test_delete_goes_through_the_library_for_every_selected_store(self):
        self.dlg._delete_selected_coverage_stores(
            [["sfdem", "sf", "GeoTIFF", "1"], ["mosaic", "nurc", "ImageMosaic", "0"]]
        )
        self.assertEqual(
            [call for call in self.dlg.gs.calls if call[0] == "delete_coverage_store"],
            [
                ("delete_coverage_store", "sf", "sfdem"),
                ("delete_coverage_store", "nurc", "mosaic"),
            ],
        )

    def test_the_delete_confirmation_names_the_cascade(self):
        seen = {}

        def confirm(question, labels=(), cascade=""):
            seen.update(question=question, cascade=cascade)
            return False

        self.dlg._confirm_delete = confirm
        self.dlg._delete_selected_coverage_stores([["sfdem", "sf", "GeoTIFF", "1"]])
        self.assertIn("delete coverage store 'sf:sfdem'?", seen["question"])
        self.assertIn("layers published from them", seen["cascade"])


# ############################################################################
# ##### Upload of a QGIS raster ##
# ################################


def write_raster(path, driver="GTiff", epsg=4326):
    """A 6x4 one-band raster on disk, georeferenced unless epsg is None."""
    from osgeo import gdal, osr

    gdal.UseExceptions()
    dataset = gdal.GetDriverByName(driver).Create(str(path), 6, 4, 1, gdal.GDT_Byte)
    if epsg:
        dataset.SetGeoTransform((7.0, 0.01, 0, 46.1, 0, -0.01))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(epsg)
        dataset.SetProjection(srs.ExportToWkt())
    dataset.GetRasterBand(1).Fill(42)
    dataset.FlushCache()
    dataset = None
    return path


def gdal_info(path):
    from osgeo import gdal

    return gdal.Info(str(path), format="json")


class RasterFixture(unittest.TestCase):
    """A temp folder, and a project emptied before and after."""

    def setUp(self):
        import tempfile

        Recording.opened.clear()
        self.folder = Path(tempfile.mkdtemp(prefix="gsm_test_"))
        QgsProject.instance().removeAllMapLayers()

    def tearDown(self):
        import shutil

        QgsProject.instance().removeAllMapLayers()
        shutil.rmtree(self.folder, ignore_errors=True)

    def add_layer(self, name, filename="dem.tif", driver="GTiff", epsg=4326):
        path = write_raster(self.folder / filename, driver, epsg)
        layer = QgsRasterLayer(str(path), name, "gdal")
        self.assertTrue(layer.isValid(), filename)
        QgsProject.instance().addMapLayer(layer)
        return layer


class TestExportToGeotiff(RasterFixture):
    def test_a_tiled_compressed_geotiff_with_the_same_grid_and_crs(self):
        layer = self.add_layer("dem", "dem.img", driver="HFA")
        info = gdal_info(export_to_geotiff(layer, self.folder / "out.tif"))
        self.assertEqual(info["size"], [6, 4])
        self.assertEqual(info["metadata"]["IMAGE_STRUCTURE"]["COMPRESSION"], "DEFLATE")
        self.assertEqual(info["bands"][0]["block"], [256, 256])  # tiled, not striped
        self.assertIn("4326", info["coordinateSystem"]["wkt"])
        self.assertEqual(info["geoTransform"][:2], [7.0, 0.01])

    def test_a_crs_override_is_written_without_reprojecting(self):
        layer = self.add_layer("dem")
        layer.setCrs(QgsCoordinateReferenceSystem("EPSG:2056"))
        info = gdal_info(export_to_geotiff(layer, self.folder / "out.tif"))
        self.assertIn("2056", info["coordinateSystem"]["wkt"])
        self.assertEqual(info["geoTransform"][:2], [7.0, 0.01])  # the pixels stayed

    def test_an_unwritable_path_is_a_runtime_error_naming_the_layer(self):
        layer = self.add_layer("dem")
        with self.assertRaises(RuntimeError) as caught:
            export_to_geotiff(layer, self.folder / "no" / "such" / "dir.tif")
        self.assertIn("dem", str(caught.exception))


class TestLocalGeotiffPath(RasterFixture):
    def test_a_plain_local_geotiff_is_the_file_itself(self):
        layer = self.add_layer("dem")
        self.assertEqual(local_geotiff_path(layer), self.folder / "dem.tif")

    def test_other_formats_overrides_and_remote_files_are_exported_instead(self):
        self.assertIsNone(
            local_geotiff_path(self.add_layer("img", "dem.img", driver="HFA"))
        )
        overridden = self.add_layer("over", "over.tif")
        overridden.setCrs(QgsCoordinateReferenceSystem("EPSG:2056"))
        self.assertIsNone(local_geotiff_path(overridden))

        class Remote:
            def providerType(inner):
                return "gdal"

            def source(inner):
                return "/vsicurl/https://example.org/dem.tif"

        class Wms:
            def providerType(inner):
                return "wms"

        self.assertIsNone(local_geotiff_path(Remote()))
        self.assertIsNone(local_geotiff_path(Wms()))


class TestRasterPicker(RasterFixture):
    def test_offers_the_gdal_rasters_with_their_crs_and_nothing_else(self):
        # The upload sends the file and declares its CRS: a vector, or a
        # raster with no file behind it, cannot be picked.
        self.add_layer("dem")
        QgsProject.instance().addMapLayer(
            QgsVectorLayer("Point?crs=EPSG:4326", "points", "memory")
        )
        form = ResourceFormDialog(
            title="t", fields=SyncDialog()._coverage_store_fields(["sf"])
        )
        combo = form.get_widget("qgis_layer")
        self.assertEqual(
            [combo.itemText(i) for i in range(combo.count())], ["dem [EPSG:4326]"]
        )


class TestPublishQgisRaster(RasterFixture):
    """The raster twin of the GeoPackage upload: one PUT, GeoServer does the rest.

    SyncDialog runs the upload task inline, so the requests are recorded by
    the time _publish_qgis_raster returns.
    """

    def setUp(self):
        super().setUp()
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        self.errors, self.warnings, self.successes = [], [], []
        self.dlg.show_error_message = self.errors.append
        self.dlg.show_warning_message = self.warnings.append
        self.dlg.show_success_message = self.successes.append
        self.dlg._reload_current_tab = lambda: None

    def values(self, **extra):
        return {
            "name": "My DEM",
            "workspace": "sf",
            "type": tab_coveragestores.QGIS_RASTER,
            # The form hands the layer itself; the tests add just the one.
            "qgis_layer": next(iter(QgsProject.instance().mapLayers().values())),
            "replace": False,
            **extra,
        }

    def puts(self):
        return [call for call in self.dlg.gs.calls if call[0] == "PUT"]

    def publish(self, on_done=None, **extra):
        """As the Add form and the Layers tab call it: under _run_action."""
        return self.dlg._run_action(
            lambda: self.dlg._publish_qgis_raster(
                self.values(**extra), on_done=on_done
            ),
            "Failed to publish raster",
        )

    def test_a_local_geotiff_is_uploaded_as_it_is_under_a_safe_name(self):
        self.add_layer("dem")
        # blank metadata: the upload is the only request
        self.dlg._publish_qgis_raster(self.values(title="", abstract=""))
        ((_verb, path, kwargs),) = self.puts()
        self.assertEqual(path, "/rest/workspaces/sf/coveragestores/My_DEM/file.geotiff")
        self.assertEqual(
            kwargs["params"], {"configure": "first", "coverageName": "My_DEM"}
        )
        self.assertEqual(kwargs["headers"], {"Content-Type": "image/tiff"})
        self.assertEqual(kwargs["data"], (self.folder / "dem.tif").read_bytes())
        self.assertFalse(
            [call for call in self.dlg.gs.calls if call[0].startswith("create")]
        )
        self.assertEqual(
            self.successes, ["Raster 'My_DEM' uploaded and published as a layer."]
        )

    def test_another_format_is_re_encoded_and_the_temporary_file_removed(self):
        import glob
        import tempfile

        self.add_layer("dem", "dem.img", driver="HFA")
        self.dlg._publish_qgis_raster(self.values())
        ((_verb, _path, kwargs),) = self.puts()
        self.assertIn(kwargs["data"][:4], (b"II*\x00", b"MM\x00*"))  # TIFF magic
        self.assertNotEqual(kwargs["data"], (self.folder / "dem.img").read_bytes())
        self.assertEqual(glob.glob(f"{tempfile.gettempdir()}/gsm_publish_*"), [])

    def test_an_existing_store_is_refused_unless_replace_is_ticked(self):
        self.add_layer("dem")
        self.dlg.gs = FakeGS(exists=True)
        self.publish()
        self.assertEqual(len(self.errors), 1)
        self.assertIn("Replace", self.errors[0])
        self.assertEqual(self.puts(), [])

        self.publish(replace=True)
        self.assertEqual(len(self.puts()), 1)

    def test_the_form_check_asks_for_the_name_the_upload_will_use(self):
        """ "My DEM" is uploaded as My_DEM: checked as typed, the form let it
        through and the refusal came after it had closed."""
        self.add_layer("dem")
        self.dlg.gs = FakeGS(exists=True)
        with self.assertRaises(ValueError) as refused:
            self.dlg._check_new_coverage_store(self.values())
        self.assertIn("'My_DEM'", str(refused.exception))
        self.assertIn(("get_coverage_store", "sf", "My_DEM"), self.dlg.gs.calls)

    def test_the_paths_carry_quoted_names(self):
        self.add_layer("dem")
        self.dlg._coverage_store_detail("my ws", "my store")
        self.dlg._coverage_detail("my ws", "my store", "a b")
        self.dlg._publish_qgis_raster(self.values(workspace="my ws"))
        paths = [call[1] for call in self.dlg.gs.calls if call[0] in ("GET", "PUT")]
        self.assertIn("/rest/workspaces/my%20ws/coveragestores/my%20store.json", paths)
        self.assertIn(
            "/rest/workspaces/my%20ws/coveragestores/my%20store/coverages/a%20b.json",
            paths,
        )
        self.assertIn(
            "/rest/workspaces/my%20ws/coveragestores/My_DEM/file.geotiff", paths
        )

    def test_title_and_abstract_go_in_a_partial_coverage_put(self):
        self.add_layer("dem")
        self.dlg._publish_qgis_raster(
            self.values(title="Elevation", abstract="Metres above the sea")
        )
        _upload, (_verb, path, kwargs) = self.puts()
        self.assertEqual(
            path, "/rest/workspaces/sf/coveragestores/My_DEM/coverages/My_DEM.json"
        )
        self.assertEqual(
            kwargs["json"],
            {"coverage": {"title": "Elevation", "abstract": "Metres above the sea"}},
        )

    def test_keywords_go_in_the_same_put(self):
        """The Publish form asks for keywords; a raster used to drop them."""
        self.add_layer("dem")
        self.dlg._publish_qgis_raster(
            self.values(title="", keywords=["dem", " ", "terrain "])
        )
        _upload, (_verb, _path, kwargs) = self.puts()
        self.assertEqual(
            kwargs["json"], {"coverage": {"keywords": {"string": ["dem", "terrain"]}}}
        )

    def test_a_raster_without_a_crs_is_refused_before_anything_is_sent(self):
        self.add_layer("dem", epsg=None)
        self.publish()
        self.assertEqual(len(self.errors), 1)
        self.assertIn("CRS", self.errors[0])
        self.assertEqual(self.puts(), [])

    def test_a_crs_without_an_epsg_code_is_refused_rasters_are_not_reprojected(self):
        layer = self.add_layer("dem")
        layer.setCrs(
            QgsCoordinateReferenceSystem.fromProj(
                "+proj=tmerc +lat_0=0 +lon_0=9 +k=1 +x_0=500000 +y_0=0 "
                "+ellps=WGS84 +units=m +no_defs"
            )
        )
        self.publish()
        self.assertEqual(len(self.errors), 1)
        self.assertIn("EPSG", self.errors[0])
        self.assertEqual(self.puts(), [])

    def test_a_workspace_a_path_cannot_carry_is_refused_before_any_request(self):
        """requests sends workspaces/sf#x/... as workspaces/sf: the existence
        checks read another resource than the upload wrote to."""
        self.add_layer("dem")
        for values in (
            {"name": "dem", "workspace": "sf#x", "type": GEOTIFF, "url": "file:x"},
            self.values(workspace="sf#x", replace=True),
            self.values(workspace="sf?x"),
        ):
            with self.assertRaises(ValueError) as refused:
                self.dlg._check_new_coverage_store(values)
            self.assertIn(values["workspace"], str(refused.exception))
        self.publish(workspace="sf#x", replace=True)  # the Layers tab's route
        self.assertIn("'sf#x'", self.errors[-1])
        self.assertEqual(self.dlg.gs.calls, [])

    def test_a_vector_into_such_a_workspace_is_refused_the_same_way(self):
        """Publish a Layer's vector twin read the datastore of "sf" and
        uploaded the GeoPackage to "sf#x"."""
        layer = QgsVectorLayer("Point?crs=EPSG:4326", "roads", "memory")
        QgsProject.instance().addMapLayer(layer)
        self.dlg.gs.get_datastore = lambda ws, name: (
            self.dlg.gs.calls.append(("get_datastore", ws, name)) or ({}, 404)
        )
        for workspace, replace in (("sf#x", False), ("sf?x", True)):
            self.dlg._run_action(
                lambda: self.dlg._publish_qgis_layer(
                    {"workspace": workspace, "name": "roads", "replace": replace},
                    layer=layer,
                ),
                "Failed to publish",
            )
        self.assertEqual(self.dlg.gs.calls, [])
        self.assertEqual(len(self.errors), 2)
        self.assertIn("'sf#x'", self.errors[0])
        self.assertIn("'sf?x'", self.errors[1])

    def test_a_cancel_during_a_raster_s_checks_stops_the_batch(self):
        """Cancel on the waiting box while a raster of a batch was checked
        counted it as failed and checked the next one; a vector stopped."""
        from geoserver_manager.gui import tab_layers
        from geoserver_manager.toolbelt.rest import Abandoned

        layers = [self.add_layer("dem"), self.add_layer("dem2", "dem2.tif")]
        checked = []

        def cancelled(ws_name, name, replace):
            checked.append(name)
            raise Abandoned()  # what the waiting box's Cancel raises

        self.dlg._check_raster_target = cancelled

        class Accepting(ResourceFormDialog):
            def exec(inner):
                return QDialog.DialogCode.Accepted

        with patch.object(tab_layers, "ResourceFormDialog", Accepting):
            self.dlg._publish_layers(layers)
        self.assertEqual(checked, ["dem"])
        self.assertEqual(self.puts(), [])
        self.assertEqual(self.errors, [])
        self.assertIn("Not started: dem, dem2.", self.warnings[-1])

    def test_a_replace_cut_short_says_the_store_may_have_lost_its_file(self):
        """A connection dropped mid-body leaves what a cancel leaves; it was
        reported as a plain failure."""
        import requests

        def reset(path, **kwargs):
            raise requests.exceptions.ConnectionError("Connection reset by peer")

        self.add_layer("dem")
        outcomes = []
        self.dlg.gs = FakeGS(exists=True)
        self.dlg.gs.rest_service.rest_client.put = reset
        self.publish(replace=True, on_done=outcomes.append)
        self.assertIn("Connection reset", self.errors[-1])
        self.assertIn("may have removed the data file", " ".join(self.warnings))
        self.assertEqual(outcomes, ["failed"])  # a batch still goes on

        # A Replace with nothing to replace: a failed first upload leaves nothing.
        self.warnings.clear()
        self.dlg.gs = FakeGS(exists=False)
        self.dlg.gs.rest_service.rest_client.put = reset
        self.publish(replace=True)
        self.assertEqual(self.warnings, [])

    def test_a_cancelled_upload_says_what_the_server_kept(self):
        def report():
            self.dlg._report_cancelled_upload(
                "coverage store",
                "Coverage Stores",
                lambda: self.dlg._resource_exists(
                    self.dlg.gs.get_coverage_store, "sf", "My_DEM"
                ),
                "My_DEM",
            )

        self.dlg.gs = FakeGS(exists=False)
        report()
        self.assertIn("Nothing was left", self.warnings[-1])

        self.dlg.gs = FakeGS(exists=True)  # a Replace: the store outlives its file
        report()
        self.assertIn("removed their data file", self.warnings[-1])
        self.assertIn("coverage store", self.warnings[-1])

        self.dlg.gs = None  # a Refresh dropped the client meanwhile
        report()
        self.assertIn("Check the Coverage Stores tab", self.warnings[-1])

    def test_the_form_shows_that_types_fields_and_prefills_a_safe_name(self):
        self.add_layer("Rivière DEM")
        dlg = ResourceFormDialog(
            title="t", fields=self.dlg._coverage_store_fields(["sf"])
        )
        self.dlg._on_store_type_changed(dlg, GEOTIFF)
        self.assertIn("qgis_layer", dlg._hidden_keys)
        self.assertIn("replace", dlg._hidden_keys)
        self.assertIn("title", dlg._hidden_keys)

        self.dlg._on_store_type_changed(dlg, tab_coveragestores.QGIS_RASTER)
        for key in ("qgis_layer", "replace", "title", "abstract"):
            self.assertNotIn(key, dlg._hidden_keys)
        self.assertIn("url", dlg._hidden_keys)
        self.assertEqual(dlg.get_widget("name").text(), "Riviere_DEM")

    def test_the_whole_add_flow_ends_in_a_banner_naming_the_layer(self):
        layer = self.add_layer("dem")

        class Accepting(ResourceFormDialog):
            def exec(inner):
                inner.get_widget("type").setCurrentText(tab_coveragestores.QGIS_RASTER)
                inner.get_widget("qgis_layer").setLayer(layer)
                return QDialog.DialogCode.Accepted

        with patch.object(tab_coveragestores, "ResourceFormDialog", Accepting):
            self.dlg._add_coverage_store()
        self.assertEqual(len(self.puts()), 1)
        self.assertEqual(
            self.successes, ["Raster 'dem' uploaded and published as a layer."]
        )


class TestRasterUploadRunsInATask(RasterFixture):
    """The real dialog: the PUT streams off the GUI thread, with progress."""

    def setUp(self):
        super().setUp()
        self.dlg = GeoServerMainDialog()
        self.dlg.gs = FakeGS()
        self.successes, self.errors = [], []
        self.dlg.show_success_message = self.successes.append
        self.dlg.show_error_message = self.errors.append
        self.dlg._reload_current_tab = lambda: None

    def tearDown(self):
        self.dlg._closing = True
        self.dlg._cancel_load(user=True)
        super().tearDown()

    def test_the_upload_is_a_task_with_progress_and_the_dialog_stays_usable(self):
        layer = self.add_layer("dem")
        self.dlg._publish_qgis_raster(
            {
                "name": "dem",
                "workspace": "sf",
                "qgis_layer": layer,
                "replace": False,
            }
        )
        self.assertIsNotNone(self.dlg._upload)  # the upload slot, not a load
        self.assertEqual(self.dlg.btn_refresh.text(), "Cancel")
        self.assertEqual(self.dlg.lbl_page_info.text(), "Uploading…")

        waited = 0
        while self.dlg._loading() and waited < 20000:
            QTest.qWait(20)
            waited += 20
        puts = [call for call in self.dlg.gs.calls if call[0] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0][2]["data"], (self.folder / "dem.tif").read_bytes())
        self.assertEqual(self.errors, [])
        self.assertEqual(
            self.successes, ["Raster 'dem' uploaded and published as a layer."]
        )
        self.assertEqual(self.dlg.btn_refresh.text(), "Refresh")

    def test_a_refused_metadata_put_says_the_raster_is_published_and_why(self):
        # The data was stored; "Failed to publish" hid it, and a retry then
        # said the store exists. The library's HTTPError text is the URL alone.
        import requests

        layer = self.add_layer("dem")
        warnings, outcomes = [], []
        self.dlg.show_warning_message = warnings.append
        client = self.dlg.gs.rest_service.rest_client
        put = client.put

        def refuse_metadata(path, **kwargs):
            if not path.endswith("/coverages/dem.json"):
                return put(path, **kwargs)
            response = requests.Response()
            response.status_code = 403
            response._content = b"Administrative privileges required"
            response.url = f"http://localhost:8080/geoserver{path}"
            response.raise_for_status()  # as the library's client does

        client.put = refuse_metadata
        self.dlg._publish_qgis_raster(
            {
                "name": "dem",
                "workspace": "sf",
                "qgis_layer": layer,
                "replace": False,
                "title": "Elevation",
            },
            on_done=outcomes.append,
        )
        waited = 0
        while self.dlg._loading() and waited < 20000:
            QTest.qWait(20)
            waited += 20
        self.assertEqual(self.errors, [])
        (warning,) = warnings
        self.assertIn("is published, but its title", warning)
        self.assertIn("Administrative privileges required", warning)
        self.assertNotIn("for url", warning)
        self.assertEqual(outcomes, ["done"])

    def test_a_replace_that_ends_after_the_dialog_closed_says_so_in_the_log(self):
        """The closing branch logged the failure alone, never that the
        replaced store may be left without its file."""
        import requests

        def reset(path, **kwargs):
            raise requests.exceptions.ConnectionError("Connection reset by peer")

        layer = self.add_layer("dem")
        logged = []
        self.dlg.log = lambda text, **kwargs: logged.append(text)
        self.dlg.gs = FakeGS(exists=True)
        self.dlg.gs.rest_service.rest_client.put = reset
        self.dlg._publish_qgis_raster(
            {"name": "dem", "workspace": "sf", "qgis_layer": layer, "replace": True}
        )
        self.dlg._closing = True  # closed while it uploads
        waited = 0
        while self.dlg._upload is not None and waited < 20000:
            QTest.qWait(20)
            waited += 20
        self.assertTrue(
            [line for line in logged if "may have removed the data file" in line],
            logged,
        )


# ############################################################################
# ####### Stand-alone run ########
# ################################
if __name__ == "__main__":
    unittest.main()
