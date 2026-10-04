#! python3  # noqa E265

"""
Usage from the repo root folder:

.. code-block:: bash

    python -m unittest tests.qgis.test_tab_workspaces
"""

# standard library
from unittest.mock import patch

from qgis.core import Qgis
from qgis.PyQt.QtWidgets import QDialog, QMessageBox
from qgis.testing import start_app, unittest

# project
from geoserver_manager.gui import tab_workspaces
from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog
from geoserver_manager.gui.scope import GLOBAL
from geoserver_manager.gui.tab_workspaces import WorkspaceTabMixin
from geoserver_manager.toolbelt.rest import PartlySaved
from tests.qgis.sync_dialog import SyncDialog, answer_next_box

start_app()

# One workspace's WMS settings as GeoServer really answers them: the abstract
# under "abstrct", keywords and SRS wrapped in {"string": …}, and the SRS codes
# as numbers. Everything the form does not model is here too, to prove it is
# left alone.
NE_WMS = {
    "workspace": {"name": "ne"},
    "name": "WMS",
    "enabled": True,
    "title": "GeoServer Natural Earth Maps",
    "abstrct": "Map images generated from Natural Earth data.",
    "keywords": {"string": ["WMS", "GEOSERVER"]},
    "srs": {"string": [4326, 3857]},
    "maxRenderingTime": 60,
    "maxRenderingErrors": 1000,
    "watermark": {"enabled": False, "position": "BOT_RIGHT"},
    "metadataLink": [{"type": "text/html"}],
    "interpolation": "Nearest",
}


# ############################################################################
# ########## Fakes ###############
# ################################


def boom(*args, **kwargs):
    raise RuntimeError("HTTP 500: boom")


class FakeGS:
    """Workspace 'ne' has its own WMS settings, 'topp' does not. A workspace
    it creates can be read back, unless `hidden`; a path in `gone` is 404."""

    def __init__(self):
        self.calls = []
        self.created, self.hidden, self.gone = [], False, set()
        outer = self

        class Response:
            def __init__(self, payload=None, status_code=200):
                self._payload = payload if payload is not None else {}
                self.status_code = status_code
                self.text = str(self._payload)

            def json(self):
                return self._payload

        class Client:
            def get(inner, path, **kwargs):
                outer.calls.append(("GET", path, kwargs))
                if path in outer.gone:
                    return Response("No such settings", 404)
                if path.endswith("/workspaces/default.json"):
                    return Response({"workspace": {"name": "topp"}})
                if path == "/rest/services/wfs/workspaces/ne/settings.json":
                    return Response(
                        {"wfs": {"title": "NE features", "maxFeatures": 50}}
                    )
                if "/workspaces/" in path and path.startswith(
                    ("/rest/services/wfs", "/rest/services/wcs", "/rest/services/wmts")
                ):
                    return Response("No such settings", 404)
                if path == "/rest/services/wfs/settings.json":
                    return Response(
                        {"wfs": {"title": "Global WFS", "maxFeatures": 1000000}}
                    )
                if path.startswith("/rest/namespaces/"):
                    name = path.rsplit("/", 1)[1][: -len(".json")]
                    return Response({"namespace": {"uri": f"http://{name}.org"}})
                if path == "/rest/services/wms/workspaces/topp/settings.json":
                    return Response("No such settings", 404)
                return Response({"wms": NE_WMS})

            def put(inner, path, **kwargs):
                outer.calls.append(("PUT", path, kwargs))
                return Response()

            def delete(inner, path, **kwargs):
                outer.calls.append(("DELETE", path, kwargs))
                return Response()

        class Endpoints:
            base_url = "/rest"

            def workspace(inner, name):
                return f"/rest/workspaces/{name}.json"

            def workspace_wms_settings(inner, name):
                return f"/rest/services/wms/workspaces/{name}/settings.json"

        class Rest:
            rest_client = Client()
            rest_endpoints = Endpoints()

            def resource_exists(inner, path):
                return Client().get(path).status_code == 200

        self.rest_service = Rest()

    def get_workspaces(self):
        return ([{"name": "ne"}, {"name": "topp"}], 200)

    def get_workspace(self, name):
        visible = () if self.hidden else self.created
        if name not in ("ne", "topp", *visible):
            return ("No such workspace", 404)
        return ({"name": name, "isolated": False}, 200)

    def get_workspace_wms_settings(self, workspace_name):
        # The facade is what answers "does this workspace have its own
        # settings"; its payload is missing the fields the form edits.
        self.calls.append(("get_workspace_wms_settings", workspace_name))
        if workspace_name == "ne":
            return ({"enabled": True, "name": "WMS"}, 200)
        return ("No such settings", 404)

    def create_workspace(self, name, isolated=False):
        self.calls.append(("create_workspace", name, isolated))
        self.created.append(name)
        return ("", 200)


class Recording(ResourceFormDialog):
    opened = []

    def exec(self):
        Recording.opened.append(self)
        return QDialog.DialogCode.Rejected


# ############################################################################
# ########## Tests ###############
# ################################


class TestWmsFormValues(unittest.TestCase):
    """Reading the settings GeoServer stores, spelling included."""

    def test_reads_the_abstract_from_geoservers_typo_key(self):
        values = WorkspaceTabMixin._wms_form_values(NE_WMS)
        # GeoServer's JSON says "abstrct"; reading "abstract" gives nothing
        self.assertEqual(
            values["wms_abstract"], "Map images generated from Natural Earth data."
        )
        self.assertEqual(values["wms_title"], "GeoServer Natural Earth Maps")
        self.assertTrue(values["wms_own"])
        self.assertTrue(values["wms_enabled"])

    def test_string_lists_are_flattened_for_the_form(self):
        values = WorkspaceTabMixin._wms_form_values(NE_WMS)
        self.assertEqual(values["wms_keywords"], ["WMS", "GEOSERVER"])
        # the codes come back as numbers, and must still read as codes
        self.assertEqual(values["wms_srs"], ["4326", "3857"])

    def test_limits_are_integers_for_the_spinboxes(self):
        values = WorkspaceTabMixin._wms_form_values(NE_WMS)
        self.assertEqual(values["wms_max_rendering_time"], 60)
        self.assertEqual(values["wms_max_rendering_errors"], 1000)

    def test_a_single_keyword_is_not_split_into_letters(self):
        values = WorkspaceTabMixin._wms_form_values(
            {"keywords": {"string": "solo"}, "srs": 4326}
        )
        self.assertEqual(values["wms_keywords"], ["solo"])
        self.assertEqual(values["wms_srs"], ["4326"])

    def test_no_settings_means_the_workspace_uses_the_global_ones(self):
        values = WorkspaceTabMixin._wms_form_values(None)
        self.assertFalse(values["wms_own"])
        self.assertEqual(values["wms_title"], "")
        self.assertEqual(values["wms_max_rendering_time"], 0)
        self.assertTrue(values["wms_enabled"])  # the default for a new one

    def test_a_new_own_wms_starts_from_the_global_limits(self):
        values = WorkspaceTabMixin._wms_form_values(
            None,
            {"title": "Global", "maxRenderingTime": 60, "maxRenderingErrors": 1000},
        )
        self.assertFalse(values["wms_own"])
        self.assertEqual(values["wms_title"], "Global")
        self.assertEqual(values["wms_max_rendering_time"], 60)
        self.assertEqual(values["wms_max_rendering_errors"], 1000)


class TestApplyWmsSettings(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()

    def sent(self, verb):
        return [call for call in self.dlg.gs.calls if call[0] == verb]

    def base_values(self, **overrides):
        values = {
            "wms_own": True,
            "wms_enabled": True,
            "wms_title": "Topp WMS",
            "wms_abstract": "Per-workspace service",
            "wms_keywords": ["topp", "wms"],
            "wms_srs": ["4326", "3857"],
            "wms_max_rendering_time": 60,
            "wms_max_rendering_errors": 1000,
            "wms_default_locale": "en",
        }
        values.update(overrides)
        return values

    def test_the_put_carries_the_form_fields_and_geoservers_spelling(self):
        self.dlg._apply_wms_settings("topp", self.base_values(), existed=False)
        _verb, path, kwargs = self.sent("PUT")[0]
        self.assertEqual(path, "/rest/services/wms/workspaces/topp/settings.json")
        wms = kwargs["json"]["wms"]
        self.assertEqual(wms["title"], "Topp WMS")
        self.assertEqual(wms["abstrct"], "Per-workspace service")
        self.assertNotIn("abstract", wms)  # the key GeoServer would ignore
        self.assertEqual(wms["keywords"], {"string": ["topp", "wms"]})
        self.assertEqual(wms["srs"], {"string": ["4326", "3857"]})
        self.assertEqual(wms["maxRenderingTime"], 60)
        self.assertEqual(wms["maxRenderingErrors"], 1000)
        self.assertEqual(wms["defaultLocale"], "en")
        self.assertEqual(wms["workspace"], {"name": "topp"})
        # only the modelled fields: GeoServer merges, so sending a template
        # would be the way to wipe the watermark and the metadata links
        self.assertNotIn("watermark", wms)
        self.assertNotIn("metadataLink", wms)

    def test_an_empty_list_field_clears_it(self):
        self.dlg._apply_wms_settings(
            "topp", self.base_values(wms_srs=[], wms_keywords=[" ", ""]), existed=True
        )
        wms = self.sent("PUT")[0][2]["json"]["wms"]
        self.assertEqual(wms["srs"], {"string": []})
        self.assertEqual(wms["keywords"], {"string": []})

    def test_an_empty_locale_is_sent_as_an_empty_string_never_null(self):
        """A null defaultLocale NPEs in GeoServer's LocaleConverter (500)."""
        self.dlg._apply_wms_settings(
            "topp", self.base_values(wms_default_locale="  "), existed=True
        )
        self.assertEqual(self.sent("PUT")[0][2]["json"]["wms"]["defaultLocale"], "")

    def test_unticking_own_settings_removes_them(self):
        self.dlg._apply_wms_settings(
            "ne", self.base_values(wms_own=False), existed=True
        )
        self.assertEqual(
            [path for _verb, path, _kw in self.sent("DELETE")],
            ["/rest/services/wms/workspaces/ne/settings.json"],
        )
        self.assertEqual(self.sent("PUT"), [])

    def test_a_workspace_that_never_had_its_own_is_left_alone(self):
        self.dlg._apply_wms_settings(
            "topp", self.base_values(wms_own=False), existed=False
        )
        self.assertEqual(self.sent("PUT"), [])
        self.assertEqual(self.sent("DELETE"), [])

    def test_one_get_answers_whether_a_workspace_has_its_own_settings(self):
        # The facade was asked first, then the same URL was read raw.
        self.assertIsNone(self.dlg._wms_settings("topp"))  # 404 -> no settings
        self.assertEqual(self.dlg._wms_settings("ne"), NE_WMS)
        self.assertEqual(
            [call[1] for call in self.dlg.gs.calls if call[0] == "GET"],
            [
                "/rest/services/wms/workspaces/topp/settings.json",
                "/rest/services/wms/workspaces/ne/settings.json",
            ],
        )
        self.assertEqual(len(self.dlg.gs.calls), 2)

    def test_a_rename_moves_the_settings_to_the_new_name(self):
        values = self.base_values(name="ne_renamed", isolated=False, set_default=False)
        with patch.object(type(self.dlg), "_put_workspace", lambda *a: None):
            self.dlg._save_workspace_and_wms(values, old_name="ne", had_wms=True)
        _verb, path, _kwargs = self.sent("PUT")[-1]
        self.assertIn("/workspaces/ne_renamed/settings.json", path)


class TestWorkspaceDialog(unittest.TestCase):
    def setUp(self):
        Recording.opened.clear()  # class-level: order must not matter
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        self.dlg.show_error_message = lambda text: self.fail(f"unexpected: {text}")
        self.dlg.show_success_message = lambda text: None

    def open_for(self, workspace_name):
        with patch.object(tab_workspaces, "ResourceFormDialog", Recording):
            self.dlg._show_workspace_info([workspace_name])
        return Recording.opened[-1]

    def test_the_wms_group_is_prefilled_and_visible(self):
        form = self.open_for("ne")
        self.assertTrue(form.get_widget("wms_own").isChecked())
        self.assertEqual(
            form.get_widget("wms_title").text(), "GeoServer Natural Earth Maps"
        )
        self.assertEqual(form.get_widget("wms_srs").list(), ["4326", "3857"])
        self.assertEqual(form.get_widget("wms_max_rendering_time").value(), 60)
        self.assertNotIn("wms_title", form._hidden_keys)

    def test_without_its_own_settings_the_wms_fields_are_out_of_the_way(self):
        form = self.open_for("topp")
        self.assertFalse(form.get_widget("wms_own").isChecked())
        for key in ("wms_enabled", "wms_title", "wms_srs"):
            self.assertIn(key, form._hidden_keys)

    def test_ticking_own_settings_reveals_the_fields(self):
        form = self.open_for("topp")
        form.get_widget("wms_own").setChecked(True)
        self.assertNotIn("wms_title", form._hidden_keys)
        form.get_widget("wms_own").setChecked(False)
        self.assertIn("wms_title", form._hidden_keys)

    def test_creating_a_workspace_offers_no_wms_group(self):
        # The settings can only be PUT once the workspace exists.
        keys = [field["key"] for field in self.dlg._workspace_fields()]
        self.assertEqual(keys, ["name", "uri", "isolated", "set_default"])
        with_wms = [field["key"] for field in self.dlg._workspace_fields(with_wms=True)]
        self.assertIn("wms_own", with_wms)

    def test_the_default_workspace_checkbox_still_locks_itself(self):
        form = self.open_for("topp")  # the fake's default workspace
        self.assertTrue(form.get_widget("set_default").isChecked())
        self.assertFalse(form.get_widget("set_default").isEnabled())

    def test_saving_the_default_workspace_does_not_set_it_again(self):
        # Its box is locked and ticked, so every save re-PUT default.json.
        class Accepting(ResourceFormDialog):
            def exec(inner):
                return QDialog.DialogCode.Accepted

        with patch.object(tab_workspaces, "ResourceFormDialog", Accepting):
            self.dlg._show_workspace_info(["topp"])  # the fake's default
        puts = [call[1] for call in self.dlg.gs.calls if call[0] == "PUT"]
        self.assertEqual(puts, [])  # an untouched form sends nothing at all

    def test_a_rename_onto_a_taken_name_keeps_the_edit_form_open(self):
        # Refused after the form closed, every setting had to be typed again.
        seen = {}

        class Filling(ResourceFormDialog):
            def exec(inner):
                inner.get_widget("name").setText("topp")  # the fake: it exists
                inner._on_accept()
                seen["open"] = not inner.result()
                seen["said"] = inner._validation_label.text()
                return QDialog.DialogCode.Rejected

        with patch.object(tab_workspaces, "ResourceFormDialog", Filling):
            self.dlg._show_workspace_info(["ne"])
        self.assertTrue(seen["open"])
        self.assertIn("already exists", seen["said"])
        self.assertEqual([c for c in self.dlg.gs.calls if c[0] == "PUT"], [])

    def test_a_name_with_a_space_keeps_the_add_form_open(self):
        # GeoServer kept it after a 500, and the plugin said "Failed to create".
        seen = {}

        class Filling(ResourceFormDialog):
            def exec(inner):
                inner.get_widget("name").setText("rv2_sp ace")
                inner._on_accept()
                seen["open"] = not inner.result()
                seen["said"] = inner._validation_label.text()
                return QDialog.DialogCode.Rejected

        with patch.object(tab_workspaces, "ResourceFormDialog", Filling):
            self.dlg._add_workspace()
        self.assertTrue(seen["open"])
        self.assertIn("cannot be a workspace name", seen["said"])
        self.assertNotIn("create_workspace", [c[0] for c in self.dlg.gs.calls])

    def test_a_name_geoservers_own_form_refuses_is_refused(self):
        for name in ("a<b", "a|b", "1ws", "a:b", "tab\there"):
            with self.assertRaises(ValueError, msg=name):
                self.dlg._check_new_workspace({"name": name})
        for name in ("ne_2", "géo", "_x", "a-b.c"):
            self.dlg._check_new_workspace({"name": name})

    def test_the_global_scope_label_is_no_workspace_name(self):
        # Its styles and groups would be edited and deleted as the global ones.
        with self.assertRaises(ValueError):
            self.dlg._check_new_workspace({"name": GLOBAL})
        with self.assertRaises(ValueError):
            self.dlg._check_workspace_rename("ne", {"name": "(global)"})
        self.assertEqual([c for c in self.dlg.gs.calls if c[0] != "GET"], [])

    def test_a_name_that_breaks_a_path_is_refused_before_any_request(self):
        """A '/' or '#' in a name would send the request to another resource."""
        for bad in ("a/b", "a#b"):
            with self.assertRaises(ValueError):
                self.dlg._save_workspace(
                    {"name": bad, "isolated": False, "set_default": False}
                )
        self.assertEqual(self.dlg.gs.calls, [])
        # a rename to a bad name is refused too; a plain edit does not re-check
        with self.assertRaises(ValueError):
            self.dlg._save_workspace(
                {"name": "a?b", "isolated": False, "set_default": False}, old_name="ok"
            )

    def test_the_help_and_the_cascade_say_what_is_true(self):
        fields = {f["key"]: f for f in self.dlg._workspace_fields()}
        self.assertIn("own URLs", fields["isolated"]["help"])
        self.assertNotIn("coexist", fields["isolated"]["help"])
        asked = []
        self.dlg._confirm_delete = (
            lambda question, labels=(), cascade="": asked.append(cascade) or False
        )
        self.dlg._delete_selected_workspaces([["topp", ""]])
        self.assertIn("coverage stores", asked[0])
        self.assertIn("cascaded stores", asked[0])
        self.assertFalse(asked[0].endswith("\n"))


class TestDefaultColumn(unittest.TestCase):
    def test_an_unreadable_default_is_reported_not_shown_as_no(self):
        # Every row read "No" and no banner said the default was unknown.
        dlg = SyncDialog()
        dlg.gs = FakeGS()
        original = dlg._raw_rest

        def raw(method, path, **kwargs):
            if path.endswith("/workspaces/default.json"):
                raise RuntimeError("HTTP 403: forbidden")
            return original(method, path, **kwargs)

        dlg._raw_rest = raw
        rows, failures = dlg._fetch_workspace_rows()
        self.assertEqual([row[1] for row in rows], [None, None])
        self.assertEqual(len(failures), 1)
        self.assertIn("default", failures[0][0])

    def test_an_empty_server_has_no_default_to_report(self):
        # Without workspaces default.json is a 404, shown as "could not be listed".
        dlg = SyncDialog()
        dlg.gs = FakeGS()
        dlg.gs.get_workspaces = lambda: ([], 200)
        original = dlg._raw_rest

        def raw(method, path, **kwargs):
            if path.endswith("/workspaces/default.json"):
                raise RuntimeError("HTTP 404: No such workspace: 'default' found")
            return original(method, path, **kwargs)

        dlg._raw_rest = raw
        self.assertEqual(dlg._fetch_workspace_rows(), ([], []))


class TestServerSync(unittest.TestCase):
    """What the dialog shows must come from the server, not from a cache."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()

    def test_workspace_names_are_fetched_on_every_call(self):
        calls = []
        listing = self.dlg.gs.get_workspaces
        self.dlg.gs.get_workspaces = lambda: calls.append(1) or listing()
        self.dlg._get_workspace_names()
        self.dlg._get_workspace_names()
        self.assertEqual(len(calls), 2)  # no cache to go stale

    def test_workspace_list_marks_the_servers_default(self):
        self.dlg._load_workspaces()
        rows = {row[0]: row[1] for row in self.dlg._all_rows}
        self.assertEqual(rows["topp"], "Yes")
        self.assertEqual(rows["ne"], "No")
        # column 0 is still the name: delete / edit callbacks rely on it
        self.assertEqual([row[0] for row in self.dlg._all_rows], ["ne", "topp"])


class TestNamespaceAndOtherServices(unittest.TestCase):
    """Measured on 2.28.5: per-workspace WFS/WCS/WMTS settings behave like
    WMS (404 without, PUT creates or merges, DELETE falls back), and a PUT of
    the namespace URI without `isolated` stores it false."""

    def setUp(self):
        Recording.opened.clear()
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        self.dlg.show_error_message = lambda text: self.fail(f"unexpected: {text}")
        self.dlg.show_success_message = lambda text: None

    def open_for(self, workspace_name):
        with patch.object(tab_workspaces, "ResourceFormDialog", Recording):
            self.dlg._show_workspace_info([workspace_name])
        return Recording.opened[-1]

    def calls(self, verb):
        return [call for call in self.dlg.gs.calls if call[0] == verb]

    def test_own_settings_are_prefilled_and_shown(self):
        form = self.open_for("ne")
        self.assertEqual(form.get_widget("uri").text(), "http://ne.org")
        self.assertTrue(form.get_widget("wfs_own").isChecked())
        self.assertEqual(form.get_widget("wfs_title").text(), "NE features")
        self.assertEqual(form.get_widget("wfs_max_features").value(), 50)
        self.assertFalse(form.get_widget("wcs_own").isChecked())
        self.assertIn("wcs_title", form._hidden_keys)

    def test_without_own_settings_the_form_starts_from_the_global_ones(self):
        # A fresh WFS override would otherwise start at maxFeatures 0.
        form = self.open_for("topp")
        self.assertFalse(form.get_widget("wfs_own").isChecked())
        self.assertEqual(form.get_widget("wfs_title").text(), "Global WFS")
        self.assertEqual(form.get_widget("wfs_max_features").value(), 1000000)

    def test_opening_a_workspace_reads_each_settings_path_once(self):
        # Each path was read twice (exists, then the payload) and the four
        # global documents came whatever the workspace had of its own: 13
        # requests for 'ne', which has its own WMS and WFS settings.
        self.open_for("ne")
        gets = [call[1] for call in self.dlg.gs.calls if call[0] == "GET"]
        self.assertEqual(len(gets), len(set(gets)))  # no path twice
        self.assertNotIn("/rest/services/wms/settings.json", gets)
        self.assertNotIn("/rest/services/wfs/settings.json", gets)

    def save(self, workspace_name, had, **changes):
        values = {"name": workspace_name, "isolated": False, "set_default": False}
        values.update({"uri": "http://ne.org", "wms_own": False})
        for service in tab_workspaces.OTHER_SERVICES:
            values.update(
                {
                    f"{service}_own": False,
                    f"{service}_enabled": True,
                    f"{service}_title": "",
                    f"{service}_abstract": "",
                    f"{service}_keywords": [],
                }
            )
        values["wfs_max_features"] = 0
        values.update(changes)
        self.dlg._save_workspace_and_wms(
            values, workspace_name, False, had, "http://ne.org"
        )

    def test_ticking_own_puts_them_and_unticking_deletes_them(self):
        self.save(
            "topp", {}, wcs_own=True, wcs_title="Coverages", wcs_keywords=["a", "b"]
        )
        (put,) = [c for c in self.calls("PUT") if "/services/" in c[1]]
        self.assertEqual(put[1], "/rest/services/wcs/workspaces/topp/settings.json")
        self.assertEqual(put[2]["json"]["wcs"]["title"], "Coverages")
        self.assertEqual(put[2]["json"]["wcs"]["keywords"], {"string": ["a", "b"]})
        self.dlg.gs.calls.clear()
        self.save("ne", {"wfs": True})
        self.assertEqual(
            [c[1] for c in self.calls("DELETE")],
            ["/rest/services/wfs/workspaces/ne/settings.json"],
        )

    def test_a_changed_uri_is_put_and_an_unchanged_one_is_not(self):
        self.save("ne", {})
        self.assertFalse([c for c in self.calls("PUT") if "/namespaces/" in c[1]])
        self.save("ne", {}, uri="http://example.org/ne")
        (put,) = [c for c in self.calls("PUT") if "/namespaces/" in c[1]]
        self.assertEqual(
            put[2]["json"],
            {"namespace": {"uri": "http://example.org/ne", "isolated": False}},
        )

    def test_an_isolated_workspace_stays_isolated_when_its_uri_changes(self):
        # A URI PUT without `isolated` un-isolated it, and refused a shared URI.
        self.save("ne", {}, isolated=True, uri="http://shared.example.org")
        (put,) = [c for c in self.calls("PUT") if "/namespaces/" in c[1]]
        self.assertIs(put[2]["json"]["namespace"]["isolated"], True)
        self.dlg.gs.calls.clear()
        self.dlg._save_workspace(
            {
                "name": "fresh",
                "isolated": True,
                "set_default": False,
                "uri": "http://shared.example.org",
            }
        )
        (put,) = [c for c in self.calls("PUT") if "/namespaces/" in c[1]]
        self.assertEqual(put[1], "/rest/namespaces/fresh.json")
        self.assertIs(put[2]["json"]["namespace"]["isolated"], True)

    def test_an_emptied_namespace_uri_goes_back_to_the_default(self):
        dlg = SyncDialog()
        put = []
        dlg._save_workspace = lambda values, old_name=None, before=None: None
        dlg._put_namespace_uri = lambda name, uri, isolated: put.append(uri)
        dlg._apply_wms_settings = lambda *args: None
        dlg._save_workspace_and_wms(
            {"name": "w", "uri": "", "isolated": False, "wms_own": False},
            "w",
            False,
            {},
            "http://old",
        )
        self.assertEqual(put, ["http://w"])

    def test_a_workspace_whose_uri_fails_is_reported_created(self):
        class GS:
            created = []

            def get_workspace(self, name):
                return ("", 200) if name in self.created else ("no", 404)

            def create_workspace(self, name, isolated=False):
                self.created.append(name)
                return ("", 201)

        self.dlg.gs = GS()
        self.dlg._put_namespace_uri = boom
        with self.assertRaises(PartlySaved) as caught:
            self.dlg._save_workspace(
                {"name": "w", "isolated": False, "set_default": False, "uri": "x:y"}
            )
        self.assertIn("created", str(caught.exception))

    def test_a_failed_default_is_still_reported_when_a_setting_fails(self):
        # The warning named the setting only; the default's failure was lost.
        def refuse(*args, **kwargs):
            raise RuntimeError("HTTP 403: forbidden")

        self.dlg._set_default_workspace = refuse
        self.dlg._apply_wms_settings = refuse
        with self.assertRaises(PartlySaved) as caught:
            self.save("ne", {}, set_default=True)
        self.assertIn("could not be made the default", str(caught.exception))
        self.assertIn("service settings were not", str(caught.exception))


class TestEditSendsOnlyWhatChanged(unittest.TestCase):
    """Measured on 2.28.5: a Save re-PUT every service
    group the form opened with, which reverted another client's WMS and WFS
    changes and recreated settings deleted meanwhile."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.gs = self.dlg.gs = FakeGS()
        self.warnings, self.successes = [], []
        self.dlg.show_warning_message = self.warnings.append
        self.dlg.show_success_message = self.successes.append
        self.dlg.show_error_message = lambda text: self.fail(f"unexpected: {text}")

    def save(self, edit, meanwhile=lambda gs: None):
        """Open 'ne' (its own WMS and WFS settings), edit, let another client
        change the server, then Save."""
        gs = self.gs

        class Editing(ResourceFormDialog):
            def exec(inner):
                edit(inner)
                meanwhile(gs)
                return QDialog.DialogCode.Accepted

        with patch.object(tab_workspaces, "ResourceFormDialog", Editing):
            self.dlg._show_workspace_info(["ne"])
        return [(path, kw["json"]) for verb, path, kw in gs.calls if verb == "PUT"]

    def test_ticking_isolated_sends_the_workspace_and_no_service(self):
        puts = self.save(lambda form: form.get_widget("isolated").setChecked(True))
        self.assertEqual(
            puts,
            [
                (
                    "/rest/workspaces/ne.json",
                    {"workspace": {"name": "ne", "isolated": True}},
                )
            ],
        )

    def test_a_changed_field_is_the_only_one_sent(self):
        puts = self.save(lambda form: form.get_widget("wfs_title").setText("Roads"))
        self.assertEqual(
            puts,
            [
                (
                    "/rest/services/wfs/workspaces/ne/settings.json",
                    {
                        "wfs": {
                            "workspace": {"name": "ne"},
                            "name": "WFS",
                            "title": "Roads",
                        }
                    },
                )
            ],
        )

    def test_settings_removed_meanwhile_are_not_recreated(self):
        wms = "/rest/services/wms/workspaces/ne/settings.json"
        puts = self.save(
            lambda form: form.get_widget("wms_title").setText("Maps"),
            meanwhile=lambda gs: gs.gone.add(wms),
        )
        self.assertEqual(puts, [])
        self.assertEqual(self.successes, [])
        (warning,) = self.warnings
        self.assertIn("own WMS settings were removed since the form opened", warning)

    def test_an_edit_without_a_rename_is_one_put_not_a_post_that_409s(self):
        self.dlg._save_workspace(
            {"name": "topp", "isolated": True, "set_default": False}, old_name="topp"
        )
        verbs = [call[0] for call in self.gs.calls]
        self.assertEqual(verbs, ["PUT"])
        self.assertEqual(self.gs.calls[0][1], "/rest/workspaces/topp.json")
        self.assertIs(self.gs.calls[0][2]["json"]["workspace"]["isolated"], True)


class TestDeleteWording(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = type("GS", (), {"delete_workspace": lambda s, n: ("", 200)})()
        self.dlg._load_workspaces = lambda: None
        self.asked = []
        self.banners = []
        self.dlg.show_success_message = self.banners.append
        self.dlg.show_error_message = self.banners.append

    def ask(self, run):
        # The real box, answered Yes: it is built plain text, not by warning().
        seen = []
        answer_next_box(QMessageBox.StandardButton.Yes, seen)
        run()
        self.asked += [text for text, _format in seen]

    def test_a_delete_reads_as_before(self):
        self.ask(lambda: self.dlg._delete_selected_workspaces([["topp", "uri"]]))
        self.assertIn("delete workspace 'topp'?", self.asked[0])
        self.assertIn("This action cannot be undone.", self.asked[0])
        self.assertEqual(self.banners, ["Workspace 'topp' deleted."])

    def test_several_resources_are_counted_by_the_tab_not_with_s(self):
        """A count like "3 workspace(s)" cannot be right in any locale; each tab
        hands in its own plural sentence, translated with the count.
        """
        self.ask(
            lambda: self.dlg._delete_selected_workspaces(
                [["a", ""], ["b", ""], ["c", ""]]
            )
        )
        self.assertIn("  • a\n  • b\n  • c", self.asked[0])
        self.assertTrue(self.banners[0].startswith("3 workspace"))


def library_error(status, body, url):
    """The HTTPError the library's raise_for_status() gives: no body in it."""
    import requests

    response = requests.Response()
    response.status_code = status
    response._content = body.encode()
    response.url = url
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as error:
        return error


class TestWhatAnAccountMayNotDo(unittest.TestCase):
    """Measured on 2.28.5 with a workspace administrator."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.gs = self.dlg.gs = FakeGS()
        self.warnings, self.successes = [], []
        self.dlg.show_warning_message = self.warnings.append
        self.dlg.show_success_message = self.successes.append

    def test_a_workspace_created_out_of_the_accounts_sight_is_not_called_created(self):
        # POST answered 201, the workspace was hidden from its creator, and
        # the banner said "created" over a table without it.
        self.gs.hidden = True

        class Filling(ResourceFormDialog):
            def exec(inner):
                inner.get_widget("name").setText("fresh")
                inner.get_widget("uri").setText("http://fresh.example.org")
                inner.get_widget("set_default").setChecked(True)
                return QDialog.DialogCode.Accepted

        with patch.object(tab_workspaces, "ResourceFormDialog", Filling):
            self.dlg._add_workspace()
        self.assertEqual(self.successes, [])
        (warning,) = self.warnings
        self.assertIn("cannot see or manage it", warning)
        # Neither the URI nor the default was tried on a workspace it cannot see.
        self.assertEqual([c for c in self.gs.calls if c[0] == "PUT"], [])

    def test_a_refused_default_is_returned_with_geoservers_reason_and_the_save_stands(
        self,
    ):
        url = "http://localhost:8080/geoserver/rest/workspaces/default.json"

        def refuse(name):
            raise library_error(403, "Administrative privileges required", url)

        self.dlg._set_default_workspace = refuse
        returned = []
        ok = self.dlg._run_action(
            lambda: returned.append(
                self.dlg._save_workspace(
                    {"name": "ne", "isolated": False, "set_default": True},
                    old_name="ne",
                )
            ),
            "Failed to save workspace 'ne'",
        )
        self.assertTrue(ok)  # the save itself succeeded and is reported so
        # Returned, not shown: the save runs in a worker, which must not
        # touch a widget; the caller shows it once it is back.
        self.assertEqual(self.warnings, [])
        (warning,) = returned
        self.assertIn("could not be made the default", warning)
        self.assertIn("Administrative privileges required", warning)
        self.assertNotIn("for url", warning)

    def test_a_refused_default_is_logged_as_a_warning(self):
        # The level went positionally into the logger's application slot.
        levels = []
        self.dlg.log = lambda message, *args, **kwargs: levels.append(
            kwargs.get("log_level")
        )
        self.dlg._set_default_workspace = boom
        self.dlg._save_workspace({"name": "ws", "isolated": False, "set_default": True})
        self.assertEqual(levels, [Qgis.MessageLevel.Warning])


# ############################################################################
# ####### Stand-alone run ########
# ################################
if __name__ == "__main__":
    unittest.main()
