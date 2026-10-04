#! python3  # noqa E265

"""
Datastore edits: a rename is a PUT on the old path, the parameters a typed
form does not own stay editable, and any other store type can be created.

Usage from the repo root folder:

.. code-block:: bash

    QT_QPA_PLATFORM=offscreen python -m unittest tests.qgis.test_tab_datastores
"""

import copy
from unittest import mock

from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox
from qgis.testing import start_app, unittest

from geoserver_manager.gui import tab_datastores
from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog
from geoserver_manager.gui.tab_datastores import (
    _MASKED,
    _OTHER,
    _TYPE_SPECIFIC_FIELDS,
    DatastoreTabMixin,
)
from geoserver_manager.toolbelt.rest import PartlySaved
from tests.qgis.sync_dialog import SyncDialog

start_app()

STORED = {
    "dbtype": "postgis",
    "host": "db",
    "port": "5432",
    "database": "gis",
    "user": "u",
    "passwd": "crypt1:PG",
    "schema": "public",
    "namespace": "http://topp",
    "Loose bbox": "true",
    "max connections": "10",
}
PG_VALUES = {
    "workspace": "topp",
    "name": "pg",
    "type": "PostGIS",
    "pg_host": "db",
    "pg_port": 5432,
    "pg_db": "gis",
    "pg_user": "u",
    "pg_password": "",
    "pg_schema": "public",
}


class RecordingGS:
    def __init__(self, taken=()):
        self.taken = set(taken)
        self.created = []
        self.rest_service = mock.MagicMock()
        self.rest_service.rest_endpoints.datastore = (
            lambda ws, name: f"/rest/workspaces/{ws}/datastores/{name}.json"
        )

    def get_datastore(self, workspace_name, datastore_name):
        return ({}, 200 if datastore_name in self.taken else 404)

    def create_datastore(self, **kwargs):
        self.created.append(kwargs)
        return ("ok", 201)


def boom(*args, **kwargs):
    raise RuntimeError("HTTP 500: boom")


class TestAddFormChecksFirst(unittest.TestCase):
    def test_a_taken_name_keeps_the_add_form_open(self):
        # Refused after the form closed, the whole form had to be typed again.

        dlg = SyncDialog()
        dlg.gs = RecordingGS(taken={"pg"})
        dlg._get_workspace_names = lambda: ["topp"]
        seen = {}

        class Filling(ResourceFormDialog):
            def exec(inner):
                inner.set_values({"type": "PostGIS"})
                for key, text in (
                    ("name", "pg"),
                    ("pg_host", "db"),
                    ("pg_db", "d"),
                    ("pg_user", "u"),
                    ("pg_password", "secret"),
                ):
                    inner.get_widget(key).setText(text)
                inner._on_accept()
                seen["open"] = not inner.result()
                seen["said"] = inner._validation_label.text()
                return QDialog.DialogCode.Rejected

        with mock.patch.object(tab_datastores, "ResourceFormDialog", Filling):
            dlg._add_datastore()
        self.assertTrue(seen["open"])
        self.assertIn("already exists", seen["said"])
        self.assertEqual(dlg.gs.created, [])

    def test_a_name_that_breaks_a_path_is_refused_before_any_request(self):
        dlg = SyncDialog()
        dlg.gs = RecordingGS()
        with self.assertRaises(ValueError):
            dlg._create_datastore_from_values(
                {
                    "workspace": "topp",
                    "name": "a/b",
                    "type": "PMTiles",
                    "pmtiles_url": "x",
                }
            )
        self.assertEqual(dlg.gs.created, [])


class TestNamespaceFollowsTheWorkspace(unittest.TestCase):
    """The library's PostGIS, JNDI and PMTiles creates send
    namespace=http://{ws}: a workspace with its own URI got a store serving
    another namespace (measured on 2.28.5)."""

    def setUp(self):
        created, saved, gets = [], [], []

        class GS:
            def get_datastore(inner, ws, name):
                gets.append(name)
                if not created:
                    return ("not found", 404)
                return (
                    {
                        "type": "PostGIS",
                        "enabled": True,
                        "description": "d",
                        "connectionParameters": {
                            "entry": {"passwd": "crypt1:x", "namespace": f"http://{ws}"}
                        },
                    },
                    200,
                )

            def create_pg_datastore(inner, **kwargs):
                created.append(kwargs)
                return ("", 201)

            def create_datastore(inner, **kwargs):
                saved.append(kwargs)
                return ("", 200)

        self.dlg = SyncDialog()
        self.dlg.gs = GS()
        self.saved, self.gets = saved, gets
        self.values = dict(PG_VALUES, name="pg_new", workspace="topp")

    def test_a_workspace_with_its_own_uri_gets_it_on_the_store(self):
        self.dlg._namespace_uri = lambda ws: "http://example.org/topp"
        self.dlg._create_datastore_from_values(self.values)
        (save,) = self.saved
        self.assertEqual(
            save["connection_parameters"]["namespace"], "http://example.org/topp"
        )
        self.assertEqual(save["connection_parameters"]["passwd"], "crypt1:x")
        self.assertEqual(save["description"], "d")

    def test_nothing_more_is_sent_when_the_uri_already_matches(self):
        self.dlg._namespace_uri = lambda ws: f"http://{ws}"
        self.dlg._create_datastore_from_values(self.values)
        self.assertEqual(self.saved, [])
        # The store was read before the URI was compared: one GET for nothing.
        self.assertEqual(self.gets, ["pg_new"])  # the Add check only


class TestDatastoreEdit(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = RecordingGS(taken={"other"})
        self.dlg._raw_rest = mock.MagicMock()

    def update(self, old_name=None, **extra):
        values = dict(PG_VALUES, **extra)
        self.dlg._update_datastore_from_values(
            values, {"type": "PostGIS", "enabled": True}, STORED, old_name=old_name
        )
        return self.dlg.gs.created[-1]

    def test_a_rename_is_a_put_on_the_old_path_before_the_save(self):
        call = self.update(old_name="pg_old")
        self.dlg._raw_rest.assert_called_once_with(
            "put",
            "/rest/workspaces/topp/datastores/pg_old.json",
            json={"dataStore": {"name": "pg"}},
        )
        # The save then goes to the new name, not a second store at the old one.
        self.assertEqual(call["datastore_name"], "pg")

    def test_a_rename_onto_a_taken_name_sends_nothing(self):
        with self.assertRaises(ValueError):
            self.update(old_name="pg_old", name="other")
        self.dlg._raw_rest.assert_not_called()
        self.assertEqual(self.dlg.gs.created, [])

    def test_a_renamed_store_whose_save_fails_says_it_was_renamed(self):
        self.dlg.gs.create_datastore = boom
        with self.assertRaises(PartlySaved) as caught:
            self.update(old_name="pg_old")
        self.assertIn("renamed to 'pg'", str(caught.exception))

    def test_a_blank_charset_removes_the_stored_one(self):
        # The merge kept ISO-8859-1, and the banner said "updated".
        self.dlg._update_datastore_from_values(
            {
                "workspace": "topp",
                "name": "shp",
                "file_url": "file:x.shp",
                "charset": "",
            },
            {"type": "Shapefile", "enabled": True},
            {"url": "file:x.shp", "charset": "ISO-8859-1"},
        )
        self.assertNotIn("charset", self.dlg.gs.created[-1]["connection_parameters"])

    def test_an_unchanged_name_is_no_rename(self):
        self.update(old_name="pg")
        self.dlg._raw_rest.assert_not_called()

    def test_every_store_path_is_quoted(self):
        # The delete built its path from the raw names while the rename and
        # the reset quoted theirs.
        self.dlg._do_delete_datastore("a b", "c#d")
        self.dlg._raw_rest.assert_called_once_with(
            "delete",
            "/rest/workspaces/a%20b/datastores/c%23d.json",
            params={"recurse": "true"},
        )

    def test_other_parameters_list_what_the_form_does_not_own(self):
        self.assertEqual(
            self.dlg._other_params("PostGIS", STORED),
            {"Loose bbox": "true", "max connections": "10"},
        )

    def test_other_parameters_edit_remove_and_keep_owned_keys(self):
        # max connections changed, Loose bbox removed, a new key added, and a
        # line naming an owned key is ignored: the typed field wins.
        merged = self.update(
            other_params={"max connections": "20", "fetch size": "500", "host": "evil"}
        )["connection_parameters"]
        self.assertEqual(merged["max connections"], "20")
        self.assertEqual(merged["fetch size"], "500")
        self.assertNotIn("Loose bbox", merged)
        self.assertEqual(merged["host"], "db")
        self.assertEqual(merged["namespace"], "http://topp")
        self.assertEqual(merged["passwd"], "crypt1:PG")

    def test_an_untouched_row_goes_back_as_stored(self):
        # An empty parameter came back as "None", which GeoServer then ran as
        # the session startup SQL of every connection; a number became text.
        stored = dict(STORED, **{"Session startup SQL": None, "max connections": 10})
        form = ResourceFormDialog(
            title="t",
            fields=[{"key": "p", "label": "P", "type": "keyvalue"}],
            values={"p": self.dlg._other_params("PostGIS", stored)},
        )
        pairs = form.get_values()["p"]
        merged = self.dlg._merge_other_params(dict(stored), stored, "PostGIS", pairs)
        self.assertIsNone(merged["Session startup SQL"])
        self.assertEqual(merged["max connections"], 10)
        pairs["Session startup SQL"] = "SET search_path TO x"  # an edit is sent
        merged = self.dlg._merge_other_params(dict(stored), stored, "PostGIS", pairs)
        self.assertEqual(merged["Session startup SQL"], "SET search_path TO x")

    def test_a_masked_other_parameter_keeps_the_stored_value(self):
        stored = dict(STORED, **{"proxy password": "crypt1:X"})
        shown = self.dlg._other_params("PostGIS", stored)
        self.assertEqual(shown["proxy password"], _MASKED)
        merged = self.dlg._merge_other_params(dict(stored), stored, "PostGIS", shown)
        self.assertEqual(merged["proxy password"], "crypt1:X")

    def test_a_secret_typed_in_the_table_keeps_its_edge_spaces(self):
        # The typed password fields kept them; the key/value tables did not.
        stored = dict(STORED, **{"proxy password": "crypt1:X"})
        pairs = {" proxy password ": " new pass ", " fetch size ": " 500 "}
        merged = self.dlg._merge_other_params(dict(stored), stored, "PostGIS", pairs)
        self.assertEqual(merged["proxy password"], " new pass ")
        self.assertEqual(merged["fetch size"], "500")
        self.assertEqual(
            self.dlg._parse_params({"s3.secret-access-key": " k "}),
            {"s3.secret-access-key": " k "},
        )

    def test_secrets_are_masked_whatever_the_key_looks_like(self):
        from geoserver_manager.gui.tab_datastores import _is_secret

        for key in (
            "s3.secret-access-key",
            "azure.account.key",
            "io.token",
            "AWS Secret",
            "api_token",
        ):
            self.assertTrue(_is_secret(key), key)
        self.assertFalse(_is_secret("Expose primary keys"))

    def test_a_parametrised_postgis_port_is_kept(self):
        from geoserver_manager.gui.tab_datastores import DatastoreTabMixin

        stored = "${PG_PORT}"
        values = {"pg_port": 5432}  # what the spinbox shows for it
        self.assertEqual(DatastoreTabMixin._kept_port(stored, values), stored)
        self.assertEqual(DatastoreTabMixin._kept_port(stored, {"pg_port": 6543}), 6543)
        self.assertEqual(DatastoreTabMixin._kept_port("5432", values), "5432")

    def test_an_on_off_flag_is_shown_even_when_its_key_reads_as_a_secret(self):
        # PMTiles stores carry these two, always "true" or "false".
        flags = {
            "io.tileverse.rangereader.s3.use-default-credentials-provider": "false",
            "io.tileverse.rangereader.gcs.default-credentials-chain": "true",
            "io.tileverse.rangereader.s3.aws-secret-access-key": "crypt1:S",
        }
        shown = self.dlg._other_params("PMTiles", dict(flags, pmtiles="s3://b/x"))
        self.assertEqual(
            shown,
            {
                "io.tileverse.rangereader.s3.use-default-credentials-provider": "false",
                "io.tileverse.rangereader.gcs.default-credentials-chain": "true",
                "io.tileverse.rangereader.s3.aws-secret-access-key": _MASKED,
            },
        )

    def test_the_prefill_reads_the_enabled_flag_and_the_form_can_flip_it(self):
        values = self.dlg._datastore_form_values(
            "topp", "pg", "PostGIS", {"enabled": False}, {"host": "db"}
        )
        self.assertIs(values["enabled"], False)
        values.update({"pg_password": "", "enabled": True})
        self.dlg._update_datastore_from_values(
            values,
            {"type": "PostGIS", "enabled": False},
            {"host": "db", "passwd": "crypt1:x"},
        )
        (call,) = self.dlg.gs.created
        self.assertIs(call["enabled"], True)
        self.dlg._raw_rest.assert_not_called()

    def test_without_a_checkbox_the_servers_enabled_flag_is_kept(self):
        values = self.dlg._datastore_form_values(
            "topp", "pg", "PMTiles", {"enabled": False}, {}
        )
        values.pop("enabled")
        self.dlg._update_datastore_from_values(
            values, {"type": "PMTiles", "enabled": False}, {}
        )
        self.assertIs(self.dlg.gs.created[0]["enabled"], False)

    def test_the_table_reads_the_enabled_flag_as_yes_no(self):
        class GS:
            def get_datastore(inner, ws, name):
                return ({"type": "PostGIS", "enabled": False}, 200)

        self.dlg.gs = GS()
        self.assertEqual(self.dlg._datastore_summary("topp", "pg"), ("PostGIS", "No"))


class TestOtherType(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = RecordingGS()

    def test_the_typed_name_and_parameters_go_to_the_library(self):
        self.dlg._create_datastore_from_values(
            {
                "workspace": "topp",
                "name": "props",
                "type": _OTHER,
                "custom_type": " Properties ",
                "raw_params": {"directory": "file:data/props"},
                "description": "",
            }
        )
        (call,) = self.dlg.gs.created
        self.assertEqual(call["datastore_type"], "Properties")
        self.assertEqual(
            call["connection_parameters"], {"directory": "file:data/props"}
        )

    def test_a_blank_type_name_is_refused(self):
        with self.assertRaises(ValueError):
            self.dlg._create_datastore_from_values(
                {
                    "workspace": "topp",
                    "name": "props",
                    "type": _OTHER,
                    "custom_type": "",
                    "raw_params": {},
                    "description": "",
                }
            )
        self.assertEqual(self.dlg.gs.created, [])


class TestDatastoreForm(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()

    def test_other_shows_the_type_name_and_the_parameter_editor(self):
        fields = self.dlg._datastore_fields(["topp"])
        form = ResourceFormDialog(title="t", fields=fields)
        self.dlg._on_type_changed(form, _OTHER)
        self.assertNotIn("custom_type", form._hidden_keys)
        self.assertNotIn("raw_params", form._hidden_keys)
        self.dlg._on_type_changed(form, "PostGIS")
        self.assertIn("raw_params", form._hidden_keys)

    def test_the_advanced_tab_and_the_enabled_box_are_for_editing_only(self):
        # In Add it would be an empty tab: a new store has no other parameters.
        add = {f["key"] for f in self.dlg._datastore_fields(["topp"])}
        edit = {f["key"] for f in self.dlg._datastore_fields(["topp"], edit_mode=True)}
        for key in ("other_params", "enabled"):
            self.assertNotIn(key, add)
            self.assertIn(key, edit)
        # Name, Workspace, Type: the order every other form uses
        self.assertEqual(
            [f["key"] for f in self.dlg._datastore_fields(["topp"])][:3],
            ["name", "workspace", "type"],
        )

    def test_the_generic_editor_does_not_list_the_parameters_twice(self):
        keys = {
            f["key"]
            for f in self.dlg._datastore_fields(["topp"], edit_mode=True, typed=False)
        }
        self.assertIn("raw_params", keys)
        self.assertNotIn("other_params", keys)

    def test_a_type_without_a_form_opens_on_one_page_with_the_parameter_editor(self):
        # The Advanced tab held one hidden field: an empty page to click on.

        class GS:
            def get_datastore(inner, ws, name):
                detail = {
                    "type": "Properties",
                    "enabled": True,
                    "connectionParameters": {"entry": {"directory": "file:data/p"}},
                }
                return (detail, 200)

        self.dlg.gs = GS()
        opened = []

        class Recording(ResourceFormDialog):
            def exec(inner):
                opened.append(inner)
                return QDialog.DialogCode.Rejected

        with mock.patch.object(tab_datastores, "ResourceFormDialog", Recording):
            self.dlg._show_datastore_info(["props", "topp", "Properties"])
        (form,) = opened
        self.assertIsNone(form._tabs)
        self.assertIsNone(form.get_widget("other_params"))
        self.assertNotIn("raw_params", form._hidden_keys)
        combo = form.get_widget("type")
        self.assertEqual(combo.currentText(), "Properties")
        self.assertFalse(combo.isEnabled())
        for key in ("pg_host", "pg_password", "jndi_reference", "pmtiles_url"):
            self.assertIn(key, form._hidden_keys)
        editor = form.get_widget("raw_params")
        self.assertEqual(editor.map()["directory"], "file:data/p")
        self.assertFalse(editor.isReadOnly())  # it is an editor, not a view
        save = form._button_box.button(QDialogButtonBox.StandardButton.Ok)
        self.assertFalse(save.isHidden())  # any type can be saved


class TestEditFormChecksFirst(unittest.TestCase):
    def test_a_rename_onto_a_taken_name_keeps_the_edit_form_open(self):
        # Refused after the form closed, the whole edit was lost.

        dlg = SyncDialog()
        dlg.gs = RecordingGS(taken={"pg", "other"})
        seen = {}

        class Filling(ResourceFormDialog):
            def exec(inner):
                for key, text in (
                    ("name", "other"),
                    ("pg_host", "db"),
                    ("pg_db", "d"),
                    ("pg_user", "u"),
                ):
                    inner.get_widget(key).setText(text)
                inner._on_accept()
                seen["open"] = not inner.result()
                seen["said"] = inner._validation_label.text()
                return QDialog.DialogCode.Rejected

        with mock.patch.object(tab_datastores, "ResourceFormDialog", Filling):
            dlg._show_datastore_info(["pg", "topp", "PostGIS"])
        self.assertTrue(seen["open"])
        self.assertIn("already exists", seen["said"])
        self.assertEqual(dlg.gs.created, [])


# A directory store as get_datastore() hands it back.
DIRECTORY = {
    "name": "shp",
    "type": "Directory of spatial files (shapefiles)",
    "enabled": True,
    "description": "old text",
    "workspace": "topp",
    "connectionParameters": {
        "entry": {
            "url": "file:data/sf",
            "charset": "ISO-8859-1",
            "memory mapped buffer": "false",
            "namespace": "http://topp",
        }
    },
}


class LiveGS:
    """One store, which another client edits or deletes while the form is open."""

    def __init__(self):
        self.stored = copy.deepcopy(DIRECTORY)  # None once deleted
        self.created = []

    def get_datastore(self, workspace_name, datastore_name):
        if self.stored is None:
            return ("No such datastore: topp,shp", 404)
        return (copy.deepcopy(self.stored), 200)

    def create_datastore(self, **kwargs):
        # The library POSTs a new store when its GET answers 404.
        self.created.append(kwargs)
        return ("", 200)


class TestEditMeetsTheServerAsItIsNow(unittest.TestCase):
    """Measured on 2.28.5: the save sent the snapshot the
    form opened with, which reverted another client's edits and recreated a
    store deleted meanwhile, empty, reported as saved."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.gs = self.dlg.gs = LiveGS()
        self.errors, self.successes = [], []
        self.dlg.show_error_message = self.errors.append
        self.dlg.show_success_message = self.successes.append
        self.dlg._warn_if_reaches_nothing = lambda values: None
        self.dlg._load_datastores = lambda: None

    def edit(self, meanwhile, **typed):
        """Open the store's form, let another client change it, type, Save."""
        gs = self.gs

        class Editing(ResourceFormDialog):
            def exec(inner):
                meanwhile(gs)
                for key, text in typed.items():
                    inner.get_widget(key).setText(text)
                return QDialog.DialogCode.Accepted

        with mock.patch.object(tab_datastores, "ResourceFormDialog", Editing):
            self.dlg._show_datastore_info(["shp", "topp", DIRECTORY["type"]])
        return gs.created

    def test_a_store_deleted_meanwhile_is_refused_not_recreated(self):
        def delete(gs):
            gs.stored = None

        self.assertEqual(self.edit(delete, description="new text"), [])
        self.assertIn("no longer on the server", self.errors[0])
        self.assertEqual(self.successes, [])

    def test_what_another_client_changed_meanwhile_survives_the_save(self):
        def reconfigure(gs):
            gs.stored["enabled"] = False
            gs.stored["connectionParameters"]["entry"].update(
                {
                    "charset": "UTF-8",
                    "memory mapped buffer": "true",
                    "cache and reuse memory maps": "true",
                }
            )

        (sent,) = self.edit(reconfigure, description="new text")
        params = sent["connection_parameters"]
        self.assertIs(sent["enabled"], False)
        self.assertEqual(params["charset"], "UTF-8")
        self.assertEqual(params["memory mapped buffer"], "true")
        self.assertEqual(params["cache and reuse memory maps"], "true")
        self.assertEqual(sent["description"], "new text")

    def test_only_what_the_user_changed_is_applied(self):
        def reconfigure(gs):
            gs.stored["description"] = "their text"
            gs.stored["connectionParameters"]["entry"]["charset"] = "UTF-8"

        (sent,) = self.edit(reconfigure, file_url="file:data/other")
        self.assertEqual(sent["connection_parameters"]["url"], "file:data/other")
        self.assertEqual(sent["connection_parameters"]["charset"], "UTF-8")
        # Left out of the PUT, so GeoServer keeps theirs.
        self.assertIsNone(sent["description"])


# GET /rest/workspaces/sf/datastores/sf.json on 2.27: no "type" at all.
UNTYPED = {
    "dataStore": {
        "name": "sf",
        "enabled": True,
        "workspace": {"name": "sf", "href": "http://gs/rest/workspaces/sf.json"},
        "connectionParameters": {
            "entry": [
                {"@key": "url", "$": "file:data/sf"},
                {"@key": "namespace", "$": "http://www.openplans.org/spearfish"},
            ]
        },
    }
}


class UntypedGS:
    """The bundled library over a store GeoServer writes without a type."""

    def __init__(self):
        self.created = []

        class Reply:
            status_code, history = 200, ()
            text = str(UNTYPED)

            def json(inner):
                return copy.deepcopy(UNTYPED)

        class Client:
            def get(inner, path, **kwargs):
                return Reply()

        class Endpoints:
            base_url = "/rest"

            def datastore(inner, ws, name):
                return f"/rest/workspaces/{ws}/datastores/{name}.json"

        class Rest:
            rest_client = Client()
            rest_endpoints = Endpoints()

            def resource_exists(inner, path):
                return False  # no layer of that name

        self.rest_service = Rest()

    def get_datastore(self, workspace_name, datastore_name):
        from geoservercloud.models.datastore import DataStore

        # What the library does with that payload: KeyError('type').
        store = DataStore.from_get_response_payload(copy.deepcopy(UNTYPED))
        return store.asdict(), 200

    def create_datastore(self, **kwargs):
        self.created.append(kwargs)
        return ("", 200)


class TestStoreWithoutAType(unittest.TestCase):
    """4 of the 5 demo stores on 2.27, or one POSTed without a type: it
    works, but the library's model raised KeyError('type') on every read."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.gs = self.dlg.gs = UntypedGS()

    def test_it_is_listed_with_no_type(self):
        self.assertEqual(self.dlg._datastore_summary("sf", "sf"), ("-", "Yes"))

    def test_its_name_is_taken_for_a_new_store_and_a_publish(self):
        with self.assertRaises(ValueError) as caught:
            self.dlg._check_new_datastore(
                {"workspace": "sf", "name": "sf", "type": "Shapefile"}
            )
        self.assertIn("already exists", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            self.dlg._refuse_vector_clash("sf", "sf", {"replace": False})
        self.assertIn("already exists", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            self.dlg._refuse_vector_clash("sf", "sf", {"replace": True})
        self.assertIn(
            "Replace only overwrites a GeoPackage store", str(caught.exception)
        )

    def test_it_opens_in_the_parameter_editor_and_saves_without_a_type(self):
        self.dlg._warn_if_reaches_nothing = lambda values: None
        self.dlg._load_datastores = lambda: None
        errors = []
        self.dlg.show_error_message = errors.append
        opened = []

        class Editing(ResourceFormDialog):
            def exec(inner):
                opened.append(inner)
                inner.get_widget("description").setText("Spearfish")
                return QDialog.DialogCode.Accepted

        with mock.patch.object(tab_datastores, "ResourceFormDialog", Editing):
            self.dlg._show_datastore_info(["sf", "sf", "-"])
        self.assertEqual(errors, [])
        (form,) = opened
        self.assertNotIn("raw_params", form._hidden_keys)
        (sent,) = self.gs.created
        # The PUT carries a null type, which GeoServer keeps as none (measured).
        self.assertIsNone(sent["datastore_type"])
        self.assertEqual(sent["connection_parameters"]["url"], "file:data/sf")
        self.assertEqual(sent["description"], "Spearfish")


# ############################################################################
# ##### Cascaded WFS store #######
# ################################

WFS = "Web Feature Server (NG)"
K = "WFSDataStoreFactory:"
CAPS = "https://remote.example.org/wfs?service=WFS&request=GetCapabilities"


def form_values(**extra):
    values = {
        "wfs_url": CAPS,
        "wfs_user": "",
        "wfs_password": "",
        "wfs_timeout": 3000,
        "wfs_max_features": 0,
        "wfs_lenient": True,
    }
    values.update(extra)
    return values


class TestWfsParams(unittest.TestCase):
    """The parameter map GeoServer stores for a Web Feature Server (NG) store."""

    def test_without_a_user_no_credentials_are_sent(self):
        self.assertEqual(
            SyncDialog._wfs_params(form_values(wfs_password="x")),
            {
                K + "GET_CAPABILITIES_URL": CAPS,
                K + "TIMEOUT": "3000",
                K + "MAXFEATURES": "0",
                K + "LENIENT": "true",
            },
        )

    def test_credentials_travel_when_a_user_is_given(self):
        params = SyncDialog._wfs_params(form_values(wfs_user="bob", wfs_password="s3"))
        self.assertEqual(params[K + "USERNAME"], "bob")
        self.assertEqual(params[K + "PASSWORD"], "s3")

    def test_numbers_and_flags_are_geoserver_style_strings(self):
        params = SyncDialog._wfs_params(
            form_values(wfs_timeout=5000, wfs_max_features=100, wfs_lenient=False)
        )
        self.assertEqual(params[K + "TIMEOUT"], "5000")
        self.assertEqual(params[K + "MAXFEATURES"], "100")
        self.assertEqual(params[K + "LENIENT"], "false")

    def test_a_blank_password_on_edit_keeps_the_stored_one_a_typed_one_replaces_it(
        self,
    ):
        stored = {K + "USERNAME": "bob", K + "PASSWORD": "crypt1:SECRET"}
        params = SyncDialog._wfs_params(form_values(wfs_user="bob"), stored)
        self.assertEqual(params[K + "PASSWORD"], "crypt1:SECRET")
        params = SyncDialog._wfs_params(
            form_values(wfs_user="bob", wfs_password="new"), stored
        )
        self.assertEqual(params[K + "PASSWORD"], "new")


class TestWfsCreateAndEdit(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.gs = RecordingGS()
        self.dlg.gs = self.gs

    def values(self, **extra):
        values = {"workspace": "topp", "name": "remote", "type": WFS, "description": ""}
        values.update(form_values(**extra))
        return values

    def test_create_goes_through_the_generic_creator_with_the_wfs_type(self):
        self.dlg._create_datastore_from_values(self.values())
        (call,) = self.gs.created
        self.assertEqual(call["datastore_type"], WFS)
        self.assertEqual(
            call["connection_parameters"][K + "GET_CAPABILITIES_URL"], CAPS
        )

    def test_edit_keeps_namespace_the_stored_password_and_the_enabled_flag(self):
        stored = {
            K + "GET_CAPABILITIES_URL": CAPS,
            K + "USERNAME": "bob",
            K + "PASSWORD": "crypt1:SECRET",
            K + "TIMEOUT": "3000",
            K + "MAXFEATURES": "0",
            K + "LENIENT": "true",
            "namespace": "http://topp",
        }
        self.dlg._update_datastore_from_values(
            self.values(wfs_user="bob", wfs_timeout=9000),
            {"type": WFS, "enabled": False},
            stored,
        )
        (call,) = self.gs.created
        merged = call["connection_parameters"]
        self.assertEqual(merged["namespace"], "http://topp")
        self.assertEqual(merged[K + "PASSWORD"], "crypt1:SECRET")
        self.assertEqual(merged[K + "TIMEOUT"], "9000")
        self.assertIs(call["enabled"], False)

    def test_clearing_the_user_drops_both_credentials(self):
        stored = {
            K + "GET_CAPABILITIES_URL": CAPS,
            K + "USERNAME": "bob",
            K + "PASSWORD": "crypt1:SECRET",
        }
        self.dlg._update_datastore_from_values(
            self.values(wfs_user=""), {"type": WFS, "enabled": True}, stored
        )
        merged = self.gs.created[0]["connection_parameters"]
        self.assertNotIn(K + "USERNAME", merged)
        self.assertNotIn(K + "PASSWORD", merged)


class TestWfsForm(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()

    def test_the_type_is_offered_and_shows_only_its_fields(self):
        fields = self.dlg._datastore_fields(["topp"])
        options = [
            value
            for field in fields
            if field["key"] == "type"
            for _label, value in field["options"]
        ]
        self.assertIn(WFS, options)
        form = ResourceFormDialog(title="t", fields=fields)
        self.dlg._on_type_changed(form, WFS)
        shown = {
            field["key"]
            for field in fields
            if field["key"] in _TYPE_SPECIFIC_FIELDS
            and field["key"] not in form._hidden_keys
        }
        self.assertEqual(
            shown,
            {
                "wfs_url",
                "wfs_user",
                "wfs_password",
                "wfs_timeout",
                "wfs_max_features",
                "wfs_lenient",
            },
        )
        self.dlg._on_type_changed(form, "PostGIS")
        self.assertIn("wfs_url", form._hidden_keys)

    def test_a_password_is_required_only_when_creating_a_postgis_store(self):
        create = {f["key"]: f for f in self.dlg._datastore_fields(["topp"])}
        edit = {
            f["key"]: f for f in self.dlg._datastore_fields(["topp"], edit_mode=True)
        }
        self.assertTrue(create["pg_password"]["required"])
        self.assertFalse(edit["pg_password"]["required"])
        self.assertFalse(create["wfs_password"].get("required", False))

    def test_the_prefill_never_carries_the_password_and_parses_the_numbers(self):
        stored = {
            K + "GET_CAPABILITIES_URL": CAPS,
            K + "USERNAME": "bob",
            K + "PASSWORD": "crypt1:SECRET",
            K + "TIMEOUT": "7000",
            K + "MAXFEATURES": "not a number",
            K + "LENIENT": "false",
        }
        values = self.dlg._datastore_form_values("topp", "remote", WFS, {}, stored)
        self.assertEqual(values["wfs_url"], CAPS)
        self.assertEqual(values["wfs_user"], "bob")
        self.assertEqual(values["wfs_password"], "")
        self.assertEqual(values["wfs_timeout"], 7000)
        self.assertEqual(values["wfs_max_features"], 0)
        self.assertFalse(values["wfs_lenient"])

    def test_the_generic_editor_masks_a_prefixed_password_too(self):
        """Before: only a key *named* password was masked, so a WFS store's
        `WFSDataStoreFactory:PASSWORD` showed its ciphertext in the editor."""
        values = self.dlg._datastore_form_values(
            "topp",
            "x",
            "Oracle NG",
            {},
            {K + "PASSWORD": "crypt1:SECRET", "passwd": "crypt1:PG", "user": "u"},
        )
        self.assertNotIn("crypt1", "".join(values["raw_params"].values()))
        self.assertEqual(values["raw_params"]["user"], "u")


class TestStoreReachAndReset(unittest.TestCase):
    """A saved store is checked at once, and can be reset."""

    def test_a_store_geoserver_cannot_read_is_said_after_the_save(self):
        dlg = SyncDialog()
        warnings = []
        dlg.show_warning_message = warnings.append

        def unreachable(ws, name):
            raise RuntimeError("HTTP 500: Connection refused to host postgis2")

        dlg._available_tables = unreachable
        dlg._warn_if_reaches_nothing({"workspace": "topp", "name": "pg"})
        self.assertIn("'pg' was saved, but GeoServer cannot read it", warnings[0])
        self.assertIn("Connection refused", warnings[0])

    def test_a_store_that_answers_says_nothing_more(self):
        dlg = SyncDialog()
        warnings = []
        dlg.show_warning_message = warnings.append
        dlg._available_tables = lambda ws, name: ["roads"]
        dlg._warn_if_reaches_nothing({"workspace": "topp", "name": "pg"})
        self.assertEqual(warnings, [])

    def test_reset_posts_to_the_datastores_reset_path(self):
        class GS:
            class rest_service:
                class rest_endpoints:
                    @staticmethod
                    def datastore(ws, ds):
                        return f"/rest/workspaces/{ws}/datastores/{ds}.json"

        dlg = SyncDialog()
        dlg.gs = GS()
        sent = []
        dlg._raw_rest = lambda method, path, **kw: sent.append((method, path))
        dlg.show_success_message = lambda text: None
        dlg._reset_datastore(["pg", "topp", "PostGIS", "Yes"])
        self.assertEqual(sent, [("post", "/rest/workspaces/topp/datastores/pg/reset")])


class TestDatastoreUpdate(unittest.TestCase):
    """Editing a datastore must not discard configuration it does not show."""

    STORED = {
        "host": "db.example.org",
        "port": "5432",
        "database": "gis",
        "user": "geo",
        "passwd": "crypt1:SECRET",
        "schema": "public",
        # none of these are on the form, all of them must survive an edit
        "max connections": "20",
        "Loose bbox": "false",
        "preparedStatements": "true",
        "namespace": "http://custom.example.org/ns",
        "Expose primary keys": "false",
    }

    def setUp(self):
        self.dlg = SyncDialog()
        self.captured = {}

        class FakeGS:
            def create_datastore(inner, **kwargs):
                self.captured.update(kwargs)
                return ("ok", 200)

        self.dlg.gs = FakeGS()

    def test_edit_preserves_unmodelled_parameters_and_disabled_state(self):
        detail = {"type": "PostGIS", "enabled": False}
        values = {
            "workspace": "ws",
            "name": "store",
            "description": "new description",
            "pg_host": "db.example.org",
            "pg_port": 5432,
            "pg_db": "gis",
            "pg_user": "geo",
            "pg_password": "typed-again",
            "pg_schema": "public",
        }

        self.dlg._update_datastore_from_values(values, detail, self.STORED)
        params = self.captured["connection_parameters"]

        # the form owns these
        self.assertEqual(params["passwd"], "typed-again")
        self.assertEqual(self.captured["description"], "new description")
        # the server owns these - they must come back unchanged
        self.assertEqual(params["max connections"], "20")
        self.assertEqual(params["Loose bbox"], "false")
        self.assertEqual(params["preparedStatements"], "true")
        self.assertEqual(params["namespace"], "http://custom.example.org/ns")
        self.assertEqual(params["Expose primary keys"], "false")
        # a disabled store must not be silently re-enabled
        self.assertFalse(self.captured["enabled"])
        # and the type comes from the server, not the combo box
        self.assertEqual(self.captured["datastore_type"], "PostGIS")

    def test_an_emptied_description_is_sent_to_clear_it(self):
        values = {
            "workspace": "ws",
            "name": "store",
            "description": "",
            "pg_host": "db.example.org",
            "pg_port": 5432,
            "pg_db": "gis",
            "pg_user": "geo",
            "pg_password": "",
            "pg_schema": "public",
        }
        self.dlg._update_datastore_from_values(
            values, {"type": "PostGIS", "enabled": True}, self.STORED
        )
        self.assertEqual(self.captured["description"], "")

    def test_pmtiles_edit_keeps_its_range_reader_config(self):
        stored = {
            "pmtiles": "s3://bucket/tiles.pmtiles",
            "io.tileverse.rangereader.provider": "s3",
            "io.tileverse.rangereader.caching.enabled": "true",
        }
        detail = {"type": "PMTiles", "enabled": True}
        values = {
            "workspace": "ws",
            "name": "tiles",
            "description": "",
            "pmtiles_url": "s3://bucket/tiles.pmtiles",
        }

        self.dlg._update_datastore_from_values(values, detail, stored)
        params = self.captured["connection_parameters"]

        # "file" here would make the store unable to open its own data
        self.assertEqual(params["io.tileverse.rangereader.provider"], "s3")

    def test_prefill_comes_from_the_server_and_never_includes_the_password(self):
        detail = {"type": "PostGIS", "description": "prod"}
        values = self.dlg._datastore_form_values(
            "ws", "store", "PostGIS", detail, self.STORED
        )

        self.assertEqual(values["pg_host"], "db.example.org")
        self.assertEqual(values["pg_port"], 5432)
        self.assertEqual(values["description"], "prod")
        self.assertEqual(values["pg_password"], "")  # crypt1:SECRET must not leak in
        self.assertEqual(values["type"], "PostGIS")

    def test_prefill_keeps_the_real_type_and_dumps_the_parameters(self):
        stored = {"url": "file:data/shapes", "passwd": "crypt1:SECRET"}
        values = self.dlg._datastore_form_values("ws", "shp", "Shapefile", {}, stored)
        self.assertEqual(values["type"], "Shapefile")  # never shown as PostGIS
        self.assertEqual(values["pg_port"], 5432)
        self.assertEqual(values["raw_params"]["url"], "file:data/shapes")
        self.assertEqual(values["raw_params"]["passwd"], "••••")  # secrets masked

    def test_connection_params_tolerates_odd_payloads(self):
        self.assertEqual(self.dlg._connection_params("not a dict"), {})
        self.assertEqual(self.dlg._connection_params({}), {})
        self.assertEqual(
            self.dlg._connection_params(
                {"connectionParameters": {"entry": {"host": "h"}}}
            ),
            {"host": "h"},
        )

    def test_refuses_to_update_when_the_server_reports_no_store(self):
        with self.assertRaises(RuntimeError):
            self.dlg._update_datastore_from_values(
                {"workspace": "ws", "name": "store"}, "<html>Sign in</html>", {}
            )


class TestGenericParameterEditor(unittest.TestCase):
    """Any datastore type is editable through 'key = value' lines."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.sent = {}
        outer = self

        class FakeGS:
            def create_datastore(inner, **kwargs):
                outer.sent.update(kwargs)
                return ("ok", 200)

        self.dlg.gs = FakeGS()

    def test_pairs_are_stripped_and_a_blank_key_is_dropped(self):
        # A key/value table, not text: no line can lack its "=" any more.
        self.assertEqual(
            self.dlg._parse_params(
                {" url ": " file:data/shapes ", "": "orphan", "a = b": "c"}
            ),
            {"url": "file:data/shapes", "a = b": "c"},
        )

    def test_editor_is_authoritative_but_keeps_masked_secrets(self):
        stored = {
            "host": "old.example.org",
            "port": "3000",
            "passwd": "crypt1:SECRET",
            "obsolete": "x",
        }
        detail = {"type": "Oracle NG", "enabled": False}
        values = {
            "workspace": "topp",
            "name": "cascaded",
            "description": "",
            "raw_params": {  # 'obsolete' removed
                "host": "new.example.org",
                "port": "5000",
                "passwd": "••••",
            },
        }

        self.dlg._update_datastore_from_values(values, detail, stored)

        self.assertEqual(
            self.sent["connection_parameters"],
            {
                "host": "new.example.org",
                "port": "5000",
                "passwd": "crypt1:SECRET",
            },
        )
        self.assertEqual(
            self.sent["datastore_type"], "Oracle NG"
        )  # server's type, not the combo
        self.assertIs(self.sent["enabled"], False)  # still disabled


class TestFileStoreParams(unittest.TestCase):
    """The typed fields of a file-based store, as connection parameters."""

    def shapefile(self, **overrides):
        values = {
            "file_url": "file:data/shapefiles/states.shp",
            "charset": "UTF-8",
            "spatial_index": True,
        }
        values.update(overrides)
        return DatastoreTabMixin._file_store_params("Shapefile", values)

    def test_a_shapefile_carries_its_path_charset_and_index_flag(self):
        self.assertEqual(
            self.shapefile(),
            {
                "url": "file:data/shapefiles/states.shp",
                "charset": "UTF-8",
                "create spatial index": "true",
            },
        )

    def test_an_empty_charset_is_left_to_geoserver_rather_than_sent_blank(self):
        # A blank charset is not the same as "use your default".
        self.assertNotIn("charset", self.shapefile(charset=""))
        self.assertNotIn("charset", self.shapefile(charset="   "))

    def test_the_index_flag_is_a_geoserver_style_string_not_a_python_bool(self):
        self.assertEqual(
            self.shapefile(spatial_index=False)["create spatial index"], "false"
        )

    def test_a_directory_store_has_no_index_flag_of_its_own(self):
        params = DatastoreTabMixin._file_store_params(
            "Directory of spatial files (shapefiles)",
            {"file_url": "file:data/taz_shapes", "charset": "", "spatial_index": True},
        )
        self.assertEqual(params, {"url": "file:data/taz_shapes"})

    def test_a_geopackage_always_declares_its_dbtype(self):
        # That parameter is how GeoServer picks the GeoPackage factory.
        params = DatastoreTabMixin._file_store_params(
            "GeoPackage",
            {
                "gpkg_database": "file:data/ne/natural_earth.gpkg",
                "gpkg_read_only": True,
                "gpkg_expose_pk": False,
            },
        )
        self.assertEqual(
            params,
            {
                "database": "file:data/ne/natural_earth.gpkg",
                "dbtype": "geopkg",
                "read_only": "true",
                "Expose primary keys": "false",
            },
        )


class TestFileStoreCreate(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.sent = {}
        outer = self

        class FakeGS:
            def get_datastore(inner, ws, name):
                return ("not found", 404)

            def create_datastore(inner, **kwargs):
                outer.sent.update(kwargs)
                return ("", 201)

        self.dlg.gs = FakeGS()

    def test_a_shapefile_goes_through_the_librarys_generic_creator(self):
        self.dlg._create_datastore_from_values(
            {
                "workspace": "topp",
                "name": "states",
                "type": "Shapefile",
                "description": "US states",
                "file_url": "file:data/shapefiles/states.shp",
                "charset": "ISO-8859-1",
                "spatial_index": True,
            }
        )
        self.assertEqual(self.sent["datastore_type"], "Shapefile")
        self.assertEqual(self.sent["workspace_name"], "topp")
        self.assertEqual(self.sent["description"], "US states")
        self.assertEqual(
            self.sent["connection_parameters"]["url"],
            "file:data/shapefiles/states.shp",
        )

    def test_a_geopackage_too(self):
        self.dlg._create_datastore_from_values(
            {
                "workspace": "ne",
                "name": "natural_earth",
                "type": "GeoPackage",
                "description": "",
                "gpkg_database": "file:data/ne/natural_earth.gpkg",
                "gpkg_read_only": True,
                "gpkg_expose_pk": False,
            }
        )
        self.assertEqual(self.sent["datastore_type"], "GeoPackage")
        self.assertEqual(self.sent["connection_parameters"]["dbtype"], "geopkg")
        self.assertIsNone(self.sent["description"])

    def test_an_existing_name_is_still_refused_first(self):
        class Taken:
            def get_datastore(inner, ws, name):
                return ({"name": name}, 200)

        self.dlg.gs = Taken()
        with self.assertRaises(ValueError):
            self.dlg._create_datastore_from_values(
                {
                    "workspace": "topp",
                    "name": "taz_shapes",
                    "type": "Shapefile",
                    "file_url": "file:x.shp",
                    "charset": "",
                    "spatial_index": False,
                }
            )


class TestFileStoreEdit(unittest.TestCase):
    """An edit merges onto the server's map, like every other type."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.sent = {}
        outer = self

        class FakeGS:
            def create_datastore(inner, **kwargs):
                outer.sent.update(kwargs)
                return ("", 200)

        self.dlg.gs = FakeGS()

    def test_editing_a_shapefile_keeps_what_the_form_does_not_model(self):
        stored = {
            "url": "file:data/old",
            "charset": "ISO-8859-1",
            "namespace": "http://www.openplans.org/topp",
            "memory mapped buffer": "false",
            "cache and reuse memory maps": "false",
        }
        self.dlg._update_datastore_from_values(
            {
                "workspace": "topp",
                "name": "taz_shapes",
                "description": "",
                "file_url": "file:data/new",
                "charset": "UTF-8",
                "spatial_index": True,
            },
            {"type": "Shapefile", "enabled": True},
            stored,
        )
        params = self.sent["connection_parameters"]
        self.assertEqual(params["url"], "file:data/new")
        self.assertEqual(params["charset"], "UTF-8")
        self.assertEqual(params["create spatial index"], "true")
        # untouched by the form, kept by the merge
        self.assertEqual(params["namespace"], "http://www.openplans.org/topp")
        self.assertEqual(params["memory mapped buffer"], "false")

    def test_editing_a_geopackage_keeps_its_tuning_parameters(self):
        stored = {
            "database": "file:data/ne/natural_earth.gpkg",
            "dbtype": "geopkg",
            "namespace": "https://www.naturalearthdata.com",
            "fetch size": "1000",
            "Batch insert size": "1",
            "read_only": "true",
        }
        self.dlg._update_datastore_from_values(
            {
                "workspace": "ne",
                "name": "NaturalEarth",
                "description": "",
                "gpkg_database": "file:data/ne/natural_earth.gpkg",
                "gpkg_read_only": False,
                "gpkg_expose_pk": True,
            },
            {"type": "GeoPackage", "enabled": True},
            stored,
        )
        params = self.sent["connection_parameters"]
        self.assertEqual(params["read_only"], "false")  # the form owns this one
        self.assertEqual(params["Expose primary keys"], "true")
        self.assertEqual(params["fetch size"], "1000")  # it does not own these
        self.assertEqual(params["Batch insert size"], "1")
        self.assertEqual(params["namespace"], "https://www.naturalearthdata.com")


class TestFileStoreFormBehaviour(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.form = ResourceFormDialog(
            title="t", fields=self.dlg._datastore_fields(["topp"])
        )

    def visible_connection_fields(self, store_type):
        self.dlg._on_type_changed(self.form, store_type)
        return {
            field["key"]
            for field in self.dlg._datastore_fields(["topp"])
            if field["key"] in _TYPE_SPECIFIC_FIELDS
            and field["key"] not in self.form._hidden_keys
        }

    def test_the_add_form_is_one_page_with_the_types_fields_under_it(self):
        # The fields sat on a Connection tab: after picking PostGIS they had
        # to be found.
        self.assertIsNone(self.form._tabs)
        self.dlg._on_type_changed(self.form, "PostGIS")
        self.assertNotIn("pg_host", self.form._hidden_keys)

    def test_each_type_shows_only_its_own_fields(self):
        self.assertEqual(
            self.visible_connection_fields("Shapefile"),
            {"file_url", "charset", "spatial_index"},
        )
        self.assertEqual(
            self.visible_connection_fields("Directory of spatial files (shapefiles)"),
            {"file_url", "charset"},
        )
        self.assertEqual(
            self.visible_connection_fields("GeoPackage"),
            {"gpkg_database", "gpkg_read_only", "gpkg_expose_pk"},
        )
        self.assertEqual(
            self.visible_connection_fields("PostGIS"),
            {"pg_host", "pg_port", "pg_db", "pg_user", "pg_password", "pg_schema"},
        )

    def test_the_new_types_are_offered_in_the_type_combo(self):
        options = [
            value
            for field in self.dlg._datastore_fields(["topp"])
            if field["key"] == "type"
            for _label, value in field["options"]
        ]
        for store_type in (
            "Shapefile",
            "Directory of spatial files (shapefiles)",
            "GeoPackage",
        ):
            self.assertIn(store_type, options)

    def test_the_prefill_reads_the_servers_parameters(self):
        values = self.dlg._datastore_form_values(
            "topp",
            "taz_shapes",
            "Shapefile",
            {"description": "Tasmania"},
            {
                "url": "file:data/taz_shapes",
                "charset": "UTF-8",
                "create spatial index": "false",
            },
        )
        self.assertEqual(values["file_url"], "file:data/taz_shapes")
        self.assertEqual(values["charset"], "UTF-8")
        self.assertIs(values["spatial_index"], False)

    def test_a_geopackage_prefill_reads_its_flags(self):
        values = self.dlg._datastore_form_values(
            "ne",
            "NaturalEarth",
            "GeoPackage",
            {},
            {
                "database": "file:data/ne/natural_earth.gpkg",
                "read_only": "true",
                "Expose primary keys": "false",
            },
        )
        self.assertEqual(values["gpkg_database"], "file:data/ne/natural_earth.gpkg")
        self.assertIs(values["gpkg_read_only"], True)
        self.assertIs(values["gpkg_expose_pk"], False)

    def test_a_missing_index_parameter_prefills_as_geoservers_own_default(self):
        # GeoServer creates the index unless told otherwise.
        values = self.dlg._datastore_form_values("topp", "s", "Shapefile", {}, {})
        self.assertIs(values["spatial_index"], True)


if __name__ == "__main__":
    unittest.main()
