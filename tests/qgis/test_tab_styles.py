#! python3  # noqa E265

"""
Usage from the repo root folder:

.. code-block:: bash

    python -m unittest tests.qgis.test_tab_styles
"""

# standard library
import io
import shutil
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

from qgis.core import QgsProject
from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox
from qgis.testing import start_app, unittest

# project
from geoserver_manager.gui import tab_styles
from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog
from geoserver_manager.gui.scope import GLOBAL
from geoserver_manager.gui.tab_styles import StyleTabMixin
from geoserver_manager.toolbelt.sld import SLD_1_0, SLD_1_1, layer_to_sld
from tests.qgis.sync_dialog import SyncDialog
from tests.qgis.test_sld import point_layer

start_app()

SLD = '<?xml version="1.0"?><StyledLayerDescriptor version="1.0.0"/>'

# ############################################################################
# ########## Fakes ###############
# ################################


def boom(*args, **kwargs):
    raise RuntimeError("HTTP 500: boom")


class FakeGS:
    """Two global styles, one workspace with one style; a REST client that records."""

    _NONE = object()  # None is the *global* scope, so it cannot mean "nothing broken"
    url = "http://gs.example.org/geoserver"

    def __init__(self, broken_workspace=_NONE):
        self.broken_workspace = broken_workspace
        self.calls = []
        self.body = SLD.encode()
        outer = self

        class Response:
            status_code = 200

            @property
            def content(inner):
                return outer.body

            def json(inner):
                return {}

        class Client:
            def get(inner, path, **kwargs):
                outer.calls.append(("GET", path, kwargs))
                return Response()

            def put(inner, path, **kwargs):
                outer.calls.append(("PUT", path, kwargs))
                return Response()

            def post(inner, path, **kwargs):
                outer.calls.append(("POST", path, kwargs))
                return Response()

            def delete(inner, path, **kwargs):
                outer.calls.append(("DELETE", path, kwargs))
                return Response()

        class Endpoints:
            base_url = "/rest"

            def style(inner, name, workspace_name=None, format="json"):
                base = (
                    f"/rest/workspaces/{workspace_name}/styles/{name}"
                    if workspace_name
                    else f"/rest/styles/{name}"
                )
                # As the library: an extension for these three formats only
                # (test_library_contract pins it).
                if format in ("json", "sld", "mbstyle"):
                    return f"{base}.{format}"
                return base

        class Rest:
            rest_client = Client()
            rest_endpoints = Endpoints()

            def create_style(inner, name, body, workspace_name=None, format="sld"):
                outer.calls.append(("PUT-body", name, workspace_name, format, body))
                return ("", 200)

        self.rest_service = Rest()

    def get_workspaces(self):
        return ([{"name": "topp"}, {"name": "empty"}], 200)

    def get_styles(self, workspace_name=None):
        if workspace_name == self.broken_workspace:
            raise RuntimeError("HTTP 500: boom")
        if workspace_name is None:
            return ([{"name": "population"}, {"name": "generic"}], 200)
        if workspace_name == "topp":
            return ([{"name": "roads_style"}], 200)
        return ([], 200)

    def get_style_definition(self, name, workspace_name=None):
        # Names that do not exist on this server yet.
        if name == "brand_new" or name.startswith("new_"):
            return ("<html>Not Found</html>", 404)
        fmt = "css" if name == "generic" else "sld"
        version = {"version": "1.1.0"} if name == "from_qgis" else {"version": "1.0.0"}
        return (
            {
                "name": name,
                "format": fmt,
                "filename": "popshade.sld",
                "languageVersion": version,
            },
            200,
        )

    def create_style_definition(self, name, filename, workspace_name=None):
        self.calls.append(("definition", name, filename, workspace_name))
        return ("", 201)

    def create_style_from_string(self, name, sld, workspace_name=None):
        self.calls.append(("from_string", name, workspace_name, sld))
        return ("", 201)

    def create_style_from_file(self, name, path, workspace_name=None):
        self.calls.append(("from_file", name, workspace_name, path))
        return ("", 201)


class Recording(ResourceFormDialog):
    opened = []

    def exec(self):
        Recording.opened.append(self)
        return QDialog.DialogCode.Rejected


# ############################################################################
# ########## Tests ###############
# ################################


LATIN1_SLD = (
    '<?xml version="1.0" encoding="ISO-8859-1"?>'
    '<StyledLayerDescriptor version="1.0.0"><Title>Caf\xe9</Title>'
    "</StyledLayerDescriptor>"
)


class TestOtherEncodings(unittest.TestCase):
    """GeoServer reads an SLD body as UTF-8 whatever its declaration names
    (measured on 2.28.5): a Latin-1 one was stored with replacement
    characters."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()

    def posted(self):
        ((_verb, _path, kwargs),) = [c for c in self.dlg.gs.calls if c[0] == "POST"]
        return kwargs["data"]

    def test_a_latin1_style_is_posted_as_utf8_by_an_upload_and_by_a_copy(self):
        with tempfile.NamedTemporaryFile(suffix=".sld", delete=False) as handle:
            handle.write(LATIN1_SLD.encode("latin-1"))
        self.dlg._create_style_from_values(
            {
                "name": "brand_new",
                "workspace": GLOBAL,
                "source": "From file",
                "file": handle.name,
            }
        )
        self.assertEqual(
            self.posted(), LATIN1_SLD.replace("ISO-8859-1", "UTF-8").encode("utf-8")
        )
        self.dlg.gs.calls.clear()
        self.dlg.gs.body = LATIN1_SLD.encode("latin-1")
        self.dlg._copy_style_to("population", None, "new_population", "topp")
        self.assertIn("Caf\xe9".encode("utf-8"), self.posted())

    def test_the_editor_shows_a_latin1_body_as_it_reads(self):
        # Decoded as UTF-8 with replacement, a save wrote U+FFFD back.
        self.dlg.gs.body = LATIN1_SLD.encode("latin-1")
        self.assertIn("Caf\xe9", self.dlg._style_body("population", None, "sld"))

    def test_an_edited_latin1_body_is_put_as_utf8(self):
        self.dlg._put_sld_body("population", None, LATIN1_SLD)
        body = self.dlg.gs.calls[-1][-1]
        self.assertIn(b'encoding="UTF-8"', body)
        self.assertIn("Caf\xe9".encode("utf-8"), body)


class TestStylesTab(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        self.warnings = []
        self.dlg.show_warning_message = self.warnings.append
        self.dlg.show_error_message = lambda t: self.fail(f"unexpected error: {t}")
        self.dlg.show_success_message = lambda t: None
        Recording.opened.clear()

    def test_lists_global_and_workspace_styles(self):
        self.dlg._load_styles()
        self.assertEqual(
            self.dlg._all_rows,
            [
                ["population", GLOBAL, "sld", "1.0.0"],
                ["generic", GLOBAL, "css", "1.0.0"],
                ["roads_style", "topp", "sld", "1.0.0"],
            ],
        )
        self.assertEqual(self.warnings, [])
        headers = [
            self.dlg.resultsTable.horizontalHeaderItem(i).text()
            for i in range(self.dlg.resultsTable.columnCount())
        ]
        self.assertEqual(headers[:4], ["Name", "Workspace", "Format", "Version"])

    def test_one_unreadable_workspace_keeps_the_rest(self):
        self.dlg.gs = FakeGS(broken_workspace="topp")
        self.dlg._load_styles()
        self.assertEqual([r[0] for r in self.dlg._all_rows], ["population", "generic"])
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("topp", self.warnings[0])

    def test_body_is_fetched_in_the_definitions_own_format(self):
        body = self.dlg._style_body("generic", None, "css")
        self.assertEqual(body, SLD)
        verb, path, _ = self.dlg.gs.calls[-1]
        self.assertEqual((verb, path), ("GET", "/rest/styles/generic.css"))

    def test_an_sld_and_a_css_style_are_editable_and_renamable(self):
        with patch.object(tab_styles, "ResourceFormDialog", Recording):
            self.dlg._show_style_info(["population", GLOBAL])
            self.dlg._show_style_info(["generic", GLOBAL])
        sld, css = Recording.opened
        self.assertEqual(sld.get_widget("format").text(), "sld")
        self.assertEqual(sld.get_widget("filename").text(), "popshade.sld")
        for form in (sld, css):
            self.assertFalse(form.get_widget("body").isReadOnly())
            self.assertFalse(form.get_widget("name").isReadOnly())
            self.assertFalse(
                form._button_box.button(QDialogButtonBox.StandardButton.Ok).isHidden()
            )

    def test_a_css_body_is_put_with_its_own_content_type(self):
        # The library's create_style() has no CSS content type at all, and
        # its path builder no .css: the bare path is a 500 "No such style
        # handler".
        self.dlg._save_style_body("generic", None, "css", "* { stroke: red; }")
        verb, path, kwargs = self.dlg.gs.calls[-1]
        self.assertEqual((verb, path), ("PUT", "/rest/styles/generic.css"))
        self.assertEqual(
            kwargs["headers"], {"Content-Type": "application/vnd.geoserver.geocss+css"}
        )

    def test_a_ysld_body_is_put_to_its_own_extension_in_a_workspace(self):
        self.dlg._save_style_body("roads_style", "topp", "ysld", "feature-styles: []")
        verb, path, _kwargs = self.dlg.gs.calls[-1]
        self.assertEqual(
            (verb, path), ("PUT", "/rest/workspaces/topp/styles/roads_style.ysld")
        )

    def test_saving_only_the_body_reloads_the_list(self):
        # The Version cell follows the body: a 1.0 body replaced by a 1.1
        # one is recorded as 1.1, and the row kept 1.0.0 until F5.
        class Editing(ResourceFormDialog):
            def exec(self):
                self.get_widget("body").setText("<sld version='1.1.0'/>")
                return QDialog.DialogCode.Accepted

        reloads = []
        self.dlg._load_styles = lambda: reloads.append(1)
        with patch.object(tab_styles, "ResourceFormDialog", Editing):
            self.dlg._show_style_info(["population", GLOBAL])
        self.assertEqual(reloads, [1])
        self.assertEqual(self.dlg.gs.calls[-1][:2], ("PUT-body", "population"))

    def test_upload_from_a_string_sends_the_body_with_its_own_content_type(self):
        self.dlg._create_style_from_values(
            {
                "name": "brand_new",
                "workspace": GLOBAL,
                "source": "Paste",
                "sld": SLD,
            }
        )
        # One POST with the body's own content type: a definition created first
        # stayed behind, empty, when GeoServer refused the body.
        creates = [c for c in self.dlg.gs.calls if c[0] in ("POST", "definition")]
        ((verb, path, kwargs),) = creates
        self.assertEqual((verb, path), ("POST", "/rest/styles.json"))
        self.assertEqual(kwargs["params"], {"name": "brand_new"})
        self.assertEqual(kwargs["headers"]["Content-Type"], SLD_1_0)
        self.assertEqual(kwargs["data"], SLD.encode())
        self.assertFalse(any(c[0] == "from_string" for c in self.dlg.gs.calls))

    def test_upload_from_an_sld_file_reads_it_and_takes_the_same_path(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".sld", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(SLD)
            path = handle.name
        self.dlg._create_style_from_values(
            {
                "name": "brand_new",
                "workspace": "topp",
                "source": "From file",
                "file": path,
            }
        )
        verb, path, kwargs = self.dlg.gs.calls[-1]
        self.assertEqual((verb, path), ("POST", "/rest/workspaces/topp/styles.json"))
        self.assertEqual(kwargs["data"], SLD.encode())
        self.assertFalse(any(c[0] == "from_file" for c in self.dlg.gs.calls))

    def test_a_zip_is_one_post_of_the_archive(self):
        """The library's create_style_from_file() POSTs the definition, then
        PUTs the zip: a zip GeoServer refused left an empty style behind, and
        the retry was refused as taken. One POST with ?name= creates it with
        its images, and a refused one leaves nothing (measured on 2.28.5)."""
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
            handle.write(b"PK\x03\x04 an archive")
        self.dlg._create_style_from_values(
            {
                "name": "brand_new",
                "workspace": "topp",
                "source": "From file",
                "file": handle.name,
            }
        )
        ((verb, path, kwargs),) = [
            c for c in self.dlg.gs.calls if c[0] in ("POST", "PUT", "from_file")
        ]
        self.assertEqual((verb, path), ("POST", "/rest/workspaces/topp/styles.json"))
        self.assertEqual(kwargs["params"], {"name": "brand_new"})
        self.assertEqual(kwargs["headers"], {"Content-Type": "application/zip"})
        self.assertEqual(kwargs["data"], b"PK\x03\x04 an archive")

    def test_a_file_of_no_style_kind_is_refused_before_the_form_closes(self):
        # The library raised "Unsupported file extension", untranslated,
        # after the form had closed.
        seen = {}

        class Uploading(ResourceFormDialog):
            def exec(inner):
                inner.set_values({"name": "brand_new", "source": "From file"})
                inner.get_widget("file").setFilePath("/tmp/style.xml")
                inner._on_accept()
                seen["open"] = not inner.result()
                seen["said"] = inner._validation_label.text()
                return QDialog.DialogCode.Rejected

        with patch.object(tab_styles, "ResourceFormDialog", Uploading):
            self.dlg._add_style()
        self.assertTrue(seen["open"])
        self.assertIn("style.xml", seen["said"])
        self.assertEqual(self.dlg.gs.calls, [])

    def test_upload_refuses_an_existing_style(self):
        with self.assertRaises(ValueError):
            self.dlg._create_style_from_values(
                {
                    "name": "population",
                    "workspace": GLOBAL,
                    "source": "Paste",
                    "sld": SLD,
                }
            )
        self.assertFalse(any(c[0].startswith("from_") for c in self.dlg.gs.calls))

    def test_upload_dialog_switches_between_paste_and_file(self):
        with patch.object(tab_styles, "ResourceFormDialog", Recording):
            self.dlg._add_style()
        form = Recording.opened[0]
        self.assertNotIn("sld", form._hidden_keys)
        self.assertIn("file", form._hidden_keys)
        form.get_widget("source").setCurrentText("From file")
        self.assertIn("sld", form._hidden_keys)
        self.assertNotIn("file", form._hidden_keys)
        self.assertEqual(
            [form.get_widget("workspace").itemText(i) for i in range(3)],
            [GLOBAL, "topp", "empty"],
        )

    def test_delete_purges_and_recurses(self):
        cascades = []
        self.dlg._confirm_delete = lambda question, labels=(), cascade="": (
            cascades.append(cascade) or True
        )
        self.dlg._delete_selected_styles([["roads_style", "topp"], ["generic", GLOBAL]])
        # purge renames the file to .bak, it does not remove it (2.28.5).
        self.assertIn(".bak", cascades[0])
        deletes = [c for c in self.dlg.gs.calls if c[0] == "DELETE"]
        self.assertEqual(
            [(path, kw["params"]) for _, path, kw in deletes],
            [
                (
                    "/rest/workspaces/topp/styles/roads_style.json",
                    {"purge": "true", "recurse": "true"},
                ),
                ("/rest/styles/generic.json", {"purge": "true", "recurse": "true"}),
            ],
        )


class TestStyleDialogLayout(unittest.TestCase):
    def test_the_definition_is_the_first_tab(self):
        """It is the one thing the dialog edits; it hid on a second tab."""
        fields = SyncDialog()._style_fields(editable=True)
        self.assertEqual(fields[0]["key"], "body")
        self.assertEqual(fields[0]["group"], "Definition")
        self.assertTrue(fields[0]["wide"] and fields[0]["code"])
        self.assertTrue(all(f.get("group") == "Details" for f in fields[1:]))


# ############################################################################
# ##### QGIS <-> GeoServer #######
# ################################

# QGIS writes 1.1; GeoServer's own styles are 1.0.
SLD_11 = (
    '<?xml version="1.0"?><StyledLayerDescriptor xmlns:se="http://www.opengis.net/se"'
    ' version="1.1.0"><NamedLayer/></StyledLayerDescriptor>'
)


class TestStoredVersionIsVisible(unittest.TestCase):
    """A 1.1 style is served as its 1.0 rendition; the dialog says so."""

    def setUp(self):
        Recording.opened.clear()
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        self.dlg.show_error_message = lambda text: self.fail(f"unexpected: {text}")

    def test_the_version_is_read_from_the_definition(self):
        self.assertEqual(
            StyleTabMixin._language_version({"languageVersion": {"version": "1.1.0"}}),
            "1.1.0",
        )
        self.assertEqual(
            StyleTabMixin._language_version({"languageVersion": "1.0.0"}), "1.0.0"
        )
        self.assertEqual(StyleTabMixin._language_version({}), "")

    def test_only_a_1_1_style_explains_the_rendition(self):
        with patch.object(tab_styles, "ResourceFormDialog", Recording):
            self.dlg._show_style_info(["from_qgis", GLOBAL])
        form = Recording.opened[-1]
        self.assertEqual(form.get_widget("version").text(), "1.1.0")
        help_texts = [
            f.get("help")
            for f in self.dlg._style_fields(True, "1.1.0")
            if f["key"] == "version"
        ]
        self.assertIn("rendition", help_texts[0])

        with patch.object(tab_styles, "ResourceFormDialog", Recording):
            self.dlg._show_style_info(["population", GLOBAL])
        self.assertEqual(Recording.opened[-1].get_widget("version").text(), "1.0.0")
        version_field = [
            f for f in self.dlg._style_fields(True, "1.0.0") if f["key"] == "version"
        ][0]
        self.assertIsNone(version_field["help"])


class TestContentTypeByVersion(unittest.TestCase):
    """The body's own SLD version decides how it is sent."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()

    def test_a_1_0_body_goes_through_the_library(self):
        self.dlg._put_sld_body("population", None, SLD)
        self.assertEqual(
            self.dlg.gs.calls[-1], ("PUT-body", "population", None, "sld", SLD.encode())
        )
        self.assertFalse([c for c in self.dlg.gs.calls if c[0] == "PUT"])

    def test_a_1_1_body_is_sent_with_the_symbology_encoding_content_type(self):
        # rest_service.create_style() can only send application/vnd.ogc.sld+xml,
        # under which GeoServer records a 1.1 body as languageVersion 1.0.0.
        self.dlg._put_sld_body("from_qgis", "topp", SLD_11)
        verb, path, kwargs = self.dlg.gs.calls[-1]
        self.assertEqual(verb, "PUT")
        self.assertEqual(path, "/rest/workspaces/topp/styles/from_qgis.sld")
        self.assertEqual(kwargs["headers"]["Content-Type"], SLD_1_1)
        self.assertEqual(kwargs["data"], SLD_11.encode())
        self.assertFalse([c for c in self.dlg.gs.calls if c[0] == "PUT-body"])

    def test_editing_an_sld_body_in_place_uses_the_same_rule(self):
        self.dlg._save_style_body("from_qgis", "topp", "sld", SLD_11)
        self.assertEqual(self.dlg.gs.calls[-1][0], "PUT")

    def test_a_non_sld_body_is_untouched_by_the_rule(self):
        self.dlg._save_style_body("basemap", None, "mbstyle", "{}")
        self.assertEqual(
            self.dlg.gs.calls[-1], ("PUT-body", "basemap", None, "mbstyle", b"{}")
        )


class TestStyleFromQgisLayer(unittest.TestCase):
    """Uploading the symbology of a layer in the current project."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        QgsProject.instance().removeAllMapLayers()
        self.layer = point_layer("towns", colour="#ff0000")
        QgsProject.instance().addMapLayer(self.layer)

    def tearDown(self):
        QgsProject.instance().removeAllMapLayers()

    def test_the_project_layer_becomes_a_style_with_the_1_1_content_type(self):
        """Through the Upload form, the one path there is: the export runs on
        the GUI thread, then the SLD is uploaded as a pasted one."""
        layer = self.layer

        class Uploading(ResourceFormDialog):
            def exec(self):
                self.set_values(
                    {"name": "new_towns", "workspace": "topp", "source": "Paste"}
                )
                self.set_values({"source": tab_styles._SOURCE_QGIS})
                self.get_widget("qgis_layer").setLayer(layer)
                return QDialog.DialogCode.Accepted

        self.dlg.show_error_message = lambda text: self.fail(text)
        with patch.object(tab_styles, "ResourceFormDialog", Uploading):
            self.dlg._add_style()
        posts = [call for call in self.dlg.gs.calls if call[0] == "POST"]
        self.assertFalse(any(c[0] == "definition" for c in self.dlg.gs.calls))
        ((_verb, path, kwargs),) = posts
        self.assertEqual(path, "/rest/workspaces/topp/styles.json")
        self.assertEqual(kwargs["params"], {"name": "new_towns"})
        self.assertEqual(kwargs["headers"]["Content-Type"], SLD_1_1)
        self.assertIn(b"ff0000", kwargs["data"].lower())  # the symbology travelled

    def test_the_source_field_shows_the_projects_layers(self):
        dlg = ResourceFormDialog(title="t", fields=self.dlg._upload_fields(["topp"]))
        self.assertIs(dlg.get_widget("qgis_layer").currentLayer(), self.layer)
        self.assertIn("qgis_layer", dlg._hidden_keys)  # until that source is picked

    def test_picking_the_qgis_source_reveals_only_that_field(self):
        dlg = ResourceFormDialog(title="t", fields=self.dlg._upload_fields(["topp"]))
        self.dlg._on_style_source_changed(dlg, "From a QGIS layer")
        self.assertNotIn("qgis_layer", dlg._hidden_keys)
        self.assertIn("sld", dlg._hidden_keys)
        self.assertIn("file", dlg._hidden_keys)

    def test_a_layer_that_leaves_the_project_is_no_longer_offered(self):
        # A picked label used to outlive its layer and fail on Save; QGIS's
        # combo follows the project.
        dlg = ResourceFormDialog(title="t", fields=self.dlg._upload_fields(["topp"]))
        QgsProject.instance().removeAllMapLayers()
        self.assertIsNone(dlg.get_values()["qgis_layer"])


class TestApplyStyleToQgis(unittest.TestCase):
    """Pulling a server style onto a project layer."""

    def setUp(self):
        Recording.opened.clear()
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        self.messages = {"warning": [], "success": []}
        self.dlg.show_warning_message = self.messages["warning"].append
        self.dlg.show_success_message = self.messages["success"].append
        self.dlg.show_error_message = lambda text: self.fail(f"unexpected: {text}")
        QgsProject.instance().removeAllMapLayers()

    def tearDown(self):
        QgsProject.instance().removeAllMapLayers()

    def test_the_style_lands_on_the_chosen_layer(self):
        target = point_layer("towns", colour="#0000ff")
        QgsProject.instance().addMapLayer(target)
        red = layer_to_sld(point_layer("source", colour="#ff0000"))

        class Accepting(ResourceFormDialog):
            def exec(inner):
                return QDialog.DialogCode.Accepted

        with (
            patch.object(tab_styles, "ResourceFormDialog", Accepting),
            patch.object(type(self.dlg), "_style_body", lambda *a: red),
        ):
            self.dlg._apply_style_to_qgis(["population", GLOBAL])
        self.assertEqual(target.renderer().symbol().color().name(), "#ff0000")
        self.assertIn("towns", self.messages["success"][0])

    def test_a_css_style_is_read_as_geoservers_sld_rendition(self):
        QgsProject.instance().addMapLayer(point_layer("towns"))
        with patch.object(tab_styles, "ResourceFormDialog", Recording):
            self.dlg._apply_style_to_qgis(["generic", GLOBAL])  # the fake's CSS style
        self.assertEqual(len(Recording.opened), 1)
        self.assertIn(
            "/rest/styles/generic.sld", [call[1] for call in self.dlg.gs.calls]
        )

    def test_what_qgis_cannot_read_is_a_warning_not_a_success(self):
        QgsProject.instance().addMapLayer(point_layer("towns"))

        class Accepting(ResourceFormDialog):
            def exec(inner):
                return QDialog.DialogCode.Accepted

        with (
            patch.object(tab_styles, "ResourceFormDialog", Accepting),
            patch.object(type(self.dlg), "_style_body", lambda *a: "<not-a-style/>"),
        ):
            self.dlg._apply_style_to_qgis(["population", GLOBAL])
        self.assertEqual(self.messages["success"], [])
        self.assertIn("could not apply", self.messages["warning"][0])


class TestSaveStyleToDisk(unittest.TestCase):
    def test_the_stored_bytes_are_written_where_the_user_pointed(self):
        # Decoded and written back as UTF-8, a Latin-1 style lost its accents.
        dlg = SyncDialog()
        dlg.gs = FakeGS()
        dlg.gs.body = LATIN1_SLD.encode("latin-1")
        saved = []
        dlg.show_success_message = saved.append
        dlg.show_error_message = lambda text: self.fail(f"unexpected: {text}")
        target = Path(tempfile.mkdtemp()) / "population.sld"

        with patch.object(
            tab_styles.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), "")
        ):
            dlg._save_style_to_disk(["population", GLOBAL])

        self.assertEqual(target.read_bytes(), LATIN1_SLD.encode("latin-1"))
        self.assertIn("population.sld", saved[0])
        self.assertIn("1.0.0", saved[0])  # the version it wrote, for the record

    def test_cancelling_the_file_dialog_writes_nothing(self):
        dlg = SyncDialog()
        dlg.gs = FakeGS()
        dlg.show_success_message = lambda text: self.fail("nothing should be saved")
        with patch.object(
            tab_styles.QFileDialog, "getSaveFileName", lambda *a, **k: ("", "")
        ):
            dlg._save_style_to_disk(["population", GLOBAL])


# ############################################################################
# ###### Legend preview ##########
# ################################

EXCEPTION_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<ServiceExceptionReport version="1.1.1"><ServiceException code="StyleNotDefined">'
    "\n      No such style: nope\n</ServiceException></ServiceExceptionReport>"
)


def png_bytes():
    """A real PNG, made by Qt itself."""
    from qgis.PyQt.QtCore import QBuffer, QIODevice
    from qgis.PyQt.QtGui import QPixmap

    pixmap = QPixmap(3, 2)
    pixmap.fill()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    return bytes(buffer.data())


RASTER_SLD = (
    '<StyledLayerDescriptor version="1.0.0"><sld:RasterSymbolizer><ColorMap/>'
    "</sld:RasterSymbolizer></StyledLayerDescriptor>"
)


class LegendFakeGS(FakeGS):
    """Feature types and coverages to draw with, and a GetLegendGraphic that
    answers a PNG or an OGC exception (HTTP 200 with XML, as GeoServer does)."""

    def __init__(
        self,
        layers=("tiger:poi", "topp:states"),
        exception=None,
        coverages=("nurc:mosaic",),
        body=SLD,
    ):
        super().__init__()
        self.layers = list(layers)
        self.coverages = list(coverages)
        self.exception = exception
        self.legend_calls = []
        outer = self

        class Response:
            status_code = 200
            content = body.encode()

            def __init__(inner, payload=None):
                inner._payload = payload or {}

            def json(inner):
                return inner._payload

        class Client:
            def get(inner, path, **kwargs):
                outer.calls.append(("GET", path, kwargs))
                parts = path.split("/")
                if path.startswith("/rest/workspaces/") and len(parts) == 5:
                    workspace, collection = parts[3], parts[4]
                    names, key, item = (
                        (outer.coverages, "coverages", "coverage")
                        if collection == "coverages.json"
                        else (outer.layers, "featureTypes", "featureType")
                    )
                    found = [
                        {"name": name.split(":", 1)[1]}
                        for name in names
                        if name.startswith(f"{workspace}:")
                    ]
                    return Response({key: {item: found} if found else ""})
                return Response()

        class Endpoints(type(self.rest_service.rest_endpoints)):
            base_url = "/rest"  # the real one has no layers path, only base_url

        self.rest_service.rest_client = Client()
        self.rest_service.rest_endpoints = Endpoints()

    def get_workspaces(self):
        names = sorted({n.split(":")[0] for n in self.layers + self.coverages})
        return ([{"name": name} for name in names or ["topp"]], 200)

    def get_legend_graphic(
        self, layer, format="image/png", language=None, style=None, workspace_name=None
    ):
        self.legend_calls.append((layer, style, workspace_name))

        class Legend:
            pass

        response = Legend()
        if self.exception:
            response.headers = {
                "Content-Type": "application/vnd.ogc.se_xml;charset=UTF-8"
            }
            response.text = self.exception
            response.content = self.exception.encode()
        else:
            response.headers = {"Content-Type": "image/png"}
            response.text = ""
            response.content = png_bytes()
        return response


class TestLegendPreview(unittest.TestCase):
    """The style dialog shows the legend GeoServer renders, a layer as context."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = LegendFakeGS()
        self.dlg.show_error_message = lambda t: self.fail(f"unexpected error: {t}")
        self.dlg.show_warning_message = lambda t: None
        Recording.opened.clear()

    def test_a_layer_of_the_styles_workspace_first_then_any(self):
        self.assertEqual(self.dlg._legend_layer("topp"), "topp:states")
        # the workspace's own collection was asked, not the whole server's
        self.assertIn(
            "/rest/workspaces/topp/featuretypes.json",
            [call[1] for call in self.dlg.gs.calls if call[0] == "GET"],
        )
        self.assertEqual(self.dlg._legend_layer("nurc"), "tiger:poi")
        self.assertEqual(self.dlg._legend_layer(None), "tiger:poi")
        self.dlg.gs = LegendFakeGS(layers=(), coverages=())
        self.assertIsNone(self.dlg._legend_layer("topp"))

    def test_a_layer_of_the_styles_kind_draws_its_legend(self):
        """Measured on 2.28.5: a raster style drawn with a vector layer is a
        blank 20x20 image, a vector style drawn with a raster layer an
        exception. The first layer of the workspace was taken, any kind."""
        self.assertEqual(self.dlg._legend_layer("topp", raster=True), "nurc:mosaic")
        self.dlg.gs = LegendFakeGS(
            layers=("nurc:bounds",), coverages=("nurc:mosaic",), body=RASTER_SLD
        )
        with patch.object(tab_styles, "ResourceFormDialog", Recording):
            self.dlg._show_style_info(["rain", "nurc"])
        self.assertEqual(self.dlg.gs.legend_calls, [("nurc:mosaic", "nurc:rain", None)])

    def test_the_legend_lands_in_the_open_dialog(self):
        with patch.object(tab_styles, "ResourceFormDialog", Recording):
            self.dlg._show_style_info(["roads_style", "topp"])
        label = Recording.opened[0].get_widget("legend")
        self.assertIsNotNone(label.pixmap())
        self.assertFalse(label.pixmap().isNull())
        self.assertEqual(
            self.dlg.gs.legend_calls, [("topp:states", "topp:roads_style", None)]
        )

    def test_a_global_style_is_asked_for_by_its_bare_name(self):
        with patch.object(tab_styles, "ResourceFormDialog", Recording):
            self.dlg._show_style_info(["population", GLOBAL])
        self.assertEqual(self.dlg.gs.legend_calls, [("tiger:poi", "population", None)])

    def test_an_ogc_exception_becomes_a_sentence_not_a_broken_picture(self):
        self.dlg.gs = LegendFakeGS(exception=EXCEPTION_XML)
        with patch.object(tab_styles, "ResourceFormDialog", Recording):
            self.dlg._show_style_info(["population", GLOBAL])
        label = Recording.opened[0].get_widget("legend")
        self.assertIn("No such style: nope", label.text())

    def test_no_layer_at_all_is_explained_without_asking(self):
        self.dlg.gs = LegendFakeGS(layers=())
        with patch.object(tab_styles, "ResourceFormDialog", Recording):
            self.dlg._show_style_info(["population", GLOBAL])
        label = Recording.opened[0].get_widget("legend")
        self.assertIn("No vector layer", label.text())
        self.assertEqual(self.dlg.gs.legend_calls, [])

    def test_a_dialog_closed_or_gone_before_the_legend_lands_is_left_alone(self):
        from qgis.PyQt import sip

        captured = {}

        class Capturing(SyncDialog):
            def _run_quietly(self, failure_message, work, on_success):
                captured["work"], captured["landed"] = work, on_success

        dlg = Capturing()
        dlg.gs = LegendFakeGS()
        form = ResourceFormDialog(title="t", fields=dlg._style_fields(False))
        dlg._load_legend(form, "population", None)
        form.reject()  # closed before the picture arrives
        captured["landed"](captured["work"](None))
        self.assertEqual(
            form.get_widget("legend").text(), "Asking GeoServer for the legend…"
        )

        form = ResourceFormDialog(title="t", fields=dlg._style_fields(False))
        dlg._load_legend(form, "population", None)
        result = captured["work"](None)
        sip.delete(form)  # the C++ dialog is gone
        captured["landed"](result)  # must not raise


class TestNamesInPaths(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.gs = FakeGS()
        self.dlg.gs = self.gs

    def test_an_upload_name_the_paths_cannot_carry_is_refused_first(self):
        with self.assertRaises(ValueError):
            self.dlg._create_style_from_values(
                {
                    "name": "new#style",
                    "workspace": GLOBAL,
                    "source": "Paste",
                    "sld": SLD,
                }
            )
        self.assertEqual([c for c in self.gs.calls if c[0] == "definition"], [])

    def test_a_style_name_from_the_server_is_quoted_into_its_paths(self):
        self.dlg._style_body("my style", "my ws", "sld")
        self.dlg._do_delete_style("a#b", None)
        paths = [call[1] for call in self.gs.calls if call[0] in ("GET", "DELETE")]
        self.assertIn("/rest/workspaces/my%20ws/styles/my%20style.sld", paths)
        self.assertIn("/rest/styles/a%23b.json", paths)


class TestWording(unittest.TestCase):
    def test_row_actions_say_what_they_do(self):
        dlg = SyncDialog()
        dlg.gs = FakeGS()
        dlg._load_styles()
        labels = [action[1] for action in dlg._row_actions]
        self.assertIn("Save to disk", labels)
        self.assertNotIn("Save as SLD", labels)
        tooltips = [action[3] for action in dlg._row_actions if len(action) > 3]
        self.assertTrue(all(len(tip) < 60 for tip in tooltips), tooltips)


class TestRenameCopyAndUsage(unittest.TestCase):
    """Measured on 2.28.5: a PUT of the name renames a style and its users
    follow; other formats are created by a POST with their content type."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()

    def calls(self, verb):
        return [call for call in self.dlg.gs.calls if call[0] == verb]

    def test_a_rename_is_a_put_of_the_name_and_nothing_else(self):
        # Its caller checked the name before the body PUT; a second
        # get_style_definition here was one GET per rename for nothing.
        reads = []
        definition = self.dlg.gs.get_style_definition
        self.dlg.gs.get_style_definition = lambda *args: reads.append(args) or (
            definition(*args)
        )
        self.dlg._rename_style("population", None, "new_population")
        ((_verb, path, kwargs),) = self.calls("PUT")
        self.assertEqual(path, "/rest/styles/population.json")
        self.assertEqual(kwargs["json"], {"style": {"name": "new_population"}})
        self.assertEqual(reads, [])

    def test_a_rename_onto_a_taken_name_stays_in_the_form_and_sends_nothing(self):
        seen = {}

        class Renaming(ResourceFormDialog):
            def exec(inner):
                inner.get_widget("name").setText("generic")
                inner._on_accept()
                seen["open"] = not inner.result()
                seen["said"] = inner._validation_label.text()
                # Accepted anyway: the save checks the name again.
                return QDialog.DialogCode.Accepted

        errors = []
        self.dlg.show_error_message = errors.append
        with patch.object(tab_styles, "ResourceFormDialog", Renaming):
            self.dlg._show_style_info(["population", GLOBAL])
        self.assertTrue(seen["open"])
        self.assertIn("already exists", seen["said"])
        self.assertEqual(self.calls("PUT"), [])
        self.assertEqual(len(errors), 1)
        self.assertIn("already exists", errors[0])

    def test_a_copy_onto_a_taken_or_unsafe_name_is_refused_and_sends_nothing(self):
        for typed, said in (("generic", "already exists"), ("a#b", "#")):
            seen = {}

            class Copying(ResourceFormDialog):
                def exec(inner):
                    inner.get_widget("name").setText(typed)
                    inner._on_accept()
                    seen["open"] = not inner.result()
                    seen["said"] = inner._validation_label.text()
                    return QDialog.DialogCode.Rejected

            with patch.object(tab_styles, "ResourceFormDialog", Copying):
                self.dlg._copy_style(["population", GLOBAL])
            self.assertTrue(seen["open"], typed)
            self.assertIn(said, seen["said"])
        with self.assertRaises(ValueError):
            self.dlg._copy_style_to("generic", None, "population", None)
        self.assertEqual(self.calls("POST"), [])

    def test_a_copy_elsewhere_warns_of_the_files_the_style_points_to(self):
        """GeoServer finds a relative icon in the style's own folder, and a
        copy into another workspace does not take the files along."""
        self.dlg.gs.body = (
            '<StyledLayerDescriptor version="1.0.0"><ExternalGraphic>'
            '<OnlineResource xlink:type="simple" xlink:href="icons/pin.png"/>'
            '<OnlineResource xlink:href="http://example.org/far.png"/>'
            "</ExternalGraphic></StyledLayerDescriptor>"
        ).encode()
        seen = {}

        class Copying(ResourceFormDialog):
            def exec(inner):
                seen["here"] = "files" in inner._hidden_keys
                inner.set_values({"workspace": "topp"})
                seen["elsewhere"] = "files" not in inner._hidden_keys
                seen["files"] = inner.get_values()["files"]
                return QDialog.DialogCode.Rejected

        with patch.object(tab_styles, "ResourceFormDialog", Copying):
            self.dlg._copy_style(["population", GLOBAL])
        self.assertEqual(
            seen, {"here": True, "elsewhere": True, "files": "icons/pin.png"}
        )

    def test_a_pasted_css_style_is_posted_with_its_content_type(self):
        self.dlg._create_style_from_values(
            {
                "name": "new_css",
                "workspace": GLOBAL,
                "source": "Paste",
                "format": "CSS",
                "sld": "* { stroke: red; }",
            }
        )
        ((_verb, path, kwargs),) = self.calls("POST")
        self.assertEqual(path, "/rest/styles.json")
        self.assertEqual(kwargs["params"], {"name": "new_css"})
        self.assertEqual(
            kwargs["headers"], {"Content-Type": "application/vnd.geoserver.geocss+css"}
        )

    def test_a_ysld_file_is_read_as_ysld(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("feature-styles: []")
        self.dlg._create_style_from_values(
            {
                "name": "new_ysld",
                "workspace": "topp",
                "source": "From file",
                "file": handle.name,
            }
        )
        ((_verb, path, kwargs),) = self.calls("POST")
        self.assertEqual(path, "/rest/workspaces/topp/styles.json")
        self.assertEqual(
            kwargs["headers"], {"Content-Type": "application/vnd.geoserver.ysld+yaml"}
        )

    def test_a_copy_keeps_the_format_and_lands_in_the_target_workspace(self):
        self.dlg._copy_style_to("generic", None, "new_generic", "topp")
        ((_verb, path, kwargs),) = self.calls("POST")
        self.assertEqual(path, "/rest/workspaces/topp/styles.json")
        self.assertEqual(kwargs["params"], {"name": "new_generic"})
        self.assertIn("geocss", kwargs["headers"]["Content-Type"])

    def test_users_are_found_by_default_other_style_and_group(self):
        layers = {
            "topp:roads": {"defaultStyle": {"name": "topp:roads_style"}},
            "topp:rivers": {
                "defaultStyle": {"name": "line"},
                "styles": {"style": {"name": "topp:roads_style"}},
            },
            "sf:streams": {"defaultStyle": {"name": "roads_style"}},  # global one
        }

        class Reply:
            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return {"layer": self.payload}

        def raw_rest(_verb, path, **_kwargs):
            qualified = path.rsplit("/", 1)[1][: -len(".json")]
            if qualified == "ne:broken":
                raise RuntimeError("HTTP 500: boom")
            return Reply(layers[qualified])

        self.dlg._raw_rest = raw_rest
        self.dlg._layers_url = lambda qualified: f"/rest/layers/{qualified}.json"
        self.dlg._all_layer_names = lambda: [*layers, "ne:broken"]
        self.dlg._all_group_names = lambda: ["tasmania", "bad_group"]

        def group_detail(name, ws):
            if name == "bad_group":
                raise RuntimeError("HTTP 500: boom")
            return {"styles": {"style": ["", {"name": "topp:roads_style"}]}}

        self.dlg._group_detail = group_detail
        users = self.dlg._style_users("roads_style", "topp")
        self.assertEqual(
            users[:2], ["topp:roads (default style)", "topp:rivers (other style)"]
        )
        # A layer or a group that could not be read is said, never skipped.
        self.assertIn("ne:broken (could not be read", users[2])
        self.assertEqual(users[3], "tasmania (layer group)")
        self.assertIn("bad_group (could not be read", users[4])
        self.assertEqual(len(users), 5)

        # Cancelled, nothing more is read: the group listing still ran whole.
        from types import SimpleNamespace

        read = []
        self.dlg._raw_rest = lambda *args, **kwargs: read.append(args)
        self.dlg._all_group_names = lambda: read.append("groups") or ["tasmania"]
        cancelled = SimpleNamespace(isCanceled=lambda: True, setProgress=lambda v: None)
        self.dlg._style_users("roads_style", "topp", cancelled)
        self.assertEqual(read, [])

    def test_an_sld_1_1_style_is_copied_as_its_stored_file(self):
        # {style}.sld serves the 1.0 rendition; the copy was stored as 1.0.
        dlg = self.dlg
        paths = []

        class Reply:
            content = b"<stored 1.1/>"

        def raw_rest(verb, path, **kwargs):
            paths.append(path)
            return Reply()

        dlg._raw_rest = raw_rest

        class GS:
            class rest_service:
                class rest_endpoints:
                    base_url = "/rest"

            def get_style_definition(self, name, workspace_name=None):
                return (
                    {"filename": "towns.sld", "languageVersion": {"version": "1.1.0"}},
                    200,
                )

        dlg.gs = GS()
        _definition, _format, body = dlg._style_as_stored("towns", "topp")
        # The bytes as stored: the one GET is the resource, not the rendition.
        self.assertEqual(body, b"<stored 1.1/>")
        self.assertEqual(paths, ["/rest/resource/workspaces/topp/styles/towns.sld"])

    def test_a_taken_name_is_refused_before_the_body_is_saved(self):
        # The new body was live on every layer, and the save "failed".
        dlg = self.dlg
        saved = []
        dlg._style_with_body = lambda name, ws: ({}, "sld", "<old/>")
        dlg._save_style_body = lambda *args: saved.append(args)
        dlg._refuse_taken_style = boom
        dlg._load_legend = lambda *args: None
        dlg.show_error_message = lambda text: None

        class Accepting(ResourceFormDialog):
            def exec(inner):
                return QDialog.DialogCode.Accepted

            def get_values(inner):
                return {"name": "taken", "body": "<new/>"}

        with patch.object(tab_styles, "ResourceFormDialog", Accepting):
            dlg._show_style_info(["population", "(global)"])
        self.assertEqual(saved, [])

    def test_used_by_hands_the_waiting_box_a_stop_event(self):
        import threading

        captured = {}

        def fetch(action, failure, **kwargs):
            captured.update(kwargs)
            return None

        self.dlg._fetch = fetch
        self.dlg._show_style_users(["roads_style", "topp"])
        self.assertIsInstance(captured.get("stop"), threading.Event)


# ############################################################################
# ##### Icons, rasters, text #####
# ################################


def sld_with_icon(href):
    """An SLD 1.1 document drawing one icon, as QGIS writes it."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?><StyledLayerDescriptor '
        'version="1.1.0" xmlns:se="http://www.opengis.net/se" '
        'xmlns:xlink="http://www.w3.org/1999/xlink"><se:ExternalGraphic>'
        f'<se:OnlineResource xlink:href="{href}" xlink:type="simple"/>'
        "</se:ExternalGraphic></StyledLayerDescriptor>"
    )


def raster_layer(folder):
    """A two-by-two grid GDAL reads, as a QGIS raster layer."""
    from qgis.core import QgsRasterLayer

    grid = Path(folder) / "dem.asc"
    grid.write_text(
        "ncols 2\nnrows 2\nxllcorner 0\nyllcorner 0\ncellsize 1\n1 2\n3 4\n"
    )
    return QgsRasterLayer(str(grid), "dem")


class TestIconsGoWithTheStyle(unittest.TestCase):
    """QGIS writes an SVG marker as its file's path on this machine, which
    GeoServer cannot read: every push drew grey squares and said "uploaded".
    Measured on 2.28.5: the same SLD in a zip with its icon drew the icon."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        self.dlg.show_error_message = lambda text: self.fail(f"unexpected: {text}")
        self.folder = Path(tempfile.mkdtemp(prefix="gsm_test_"))
        self.addCleanup(shutil.rmtree, self.folder)
        self.svg = self.folder / "plane.svg"
        self.svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        QgsProject.instance().removeAllMapLayers()
        self.addCleanup(QgsProject.instance().removeAllMapLayers)

    def test_a_new_style_is_one_post_of_a_zip_with_its_icons(self):
        self.dlg._create_style(
            "towns", "topp", "sld", sld_with_icon(self.svg), local_icons=True
        )
        ((_verb, path, kwargs),) = [c for c in self.dlg.gs.calls if c[0] == "POST"]
        self.assertEqual(path, "/rest/workspaces/topp/styles.json")
        self.assertEqual(kwargs["params"], {"name": "towns"})
        self.assertEqual(kwargs["headers"], {"Content-Type": "application/zip"})
        package = zipfile.ZipFile(io.BytesIO(kwargs["data"]))
        self.assertEqual(package.read("towns_plane.svg"), self.svg.read_bytes())
        self.assertIn(
            'xlink:href="towns_plane.svg"', package.read("style.sld").decode()
        )

    def test_a_replace_is_the_zip_put_then_the_put_of_its_sld(self):
        """A zip PUT unpacks the icons but keeps the style's recorded format
        and version (2.28.5): a CSS style replaced that way held SLD in its
        .css file, and no layer using it drew. The zip's own SLD follows."""
        self.dlg._put_sld_body(
            "from_qgis", "topp", sld_with_icon(self.svg), local_icons=True
        )
        zip_put, sld_put = self.dlg.gs.calls
        _put, name, workspace, style_format, body = zip_put
        self.assertEqual((name, workspace, style_format), ("from_qgis", "topp", "zip"))
        package = zipfile.ZipFile(io.BytesIO(body))
        self.assertIn("from_qgis_plane.svg", package.namelist())
        self.assertEqual(
            sld_put,
            (
                "PUT",
                "/rest/workspaces/topp/styles/from_qgis.sld",
                {
                    "data": package.read("style.sld"),
                    "headers": {"Content-Type": SLD_1_1},
                },
            ),
        )

    def test_an_sld_1_0_replace_follows_its_zip_as_sld_1_0(self):
        sld = sld_with_icon(self.svg).replace('version="1.1.0"', 'version="1.0.0"')
        self.dlg._put_sld_body("population", None, sld, local_icons=True)
        zip_put, sld_put = self.dlg.gs.calls
        self.assertEqual(zip_put[:4], ("PUT-body", "population", None, "zip"))
        document = zipfile.ZipFile(io.BytesIO(zip_put[4])).read("style.sld")
        self.assertEqual(sld_put, ("PUT-body", "population", None, "sld", document))

    def test_a_body_from_the_server_never_takes_a_file_of_this_machine(self):
        """A stored SLD naming a file here made Copy and the edit form's Save
        read that file and upload it into the style's folder, unasked."""
        secret = self.folder / "holiday.png"
        secret.write_bytes(b"PRIVATEPNG")
        self.dlg.gs.body = sld_with_icon(f"file://{secret}").encode()
        self.dlg._copy_style_to("population", None, "new_population", "topp")
        ((_verb, _path, kwargs),) = [c for c in self.dlg.gs.calls if c[0] == "POST"]
        self.assertNotEqual(kwargs["headers"]["Content-Type"], "application/zip")
        self.assertNotIn(b"PRIVATEPNG", kwargs["data"])
        self.dlg.gs.calls.clear()
        self.dlg._save_style_body(
            "from_qgis", "topp", "sld", sld_with_icon(f"file://{secret}") + " "
        )
        self.assertEqual([c[0] for c in self.dlg.gs.calls], ["PUT"])
        self.assertNotIn(b"PRIVATEPNG", self.dlg.gs.calls[0][2]["data"])

    def test_the_library_puts_reach_the_style_whose_name_has_a_hash(self):
        """create_style() puts the names in its path raw: "a#b" went to "a"
        while the SLD PUT after it, quoted, reached "a#b"."""
        self.dlg._put_sld_body("a#b", "t?p", sld_with_icon(self.svg), local_icons=True)
        zip_put, sld_put = self.dlg.gs.calls
        self.assertEqual(zip_put[1:4], ("a%23b", "t%3Fp", "zip"))
        self.assertEqual(sld_put[1], "/rest/workspaces/t%3Fp/styles/a%23b.sld")
        self.dlg.gs.calls.clear()
        sld = sld_with_icon(self.svg).replace('version="1.1.0"', 'version="1.0.0"')
        self.dlg._put_sld_body("a#b", None, sld)
        ((_put, name, workspace, style_format, _body),) = self.dlg.gs.calls
        self.assertEqual((name, workspace, style_format), ("a%23b", None, "sld"))

    def test_an_icon_geoserver_would_not_unpack_is_refused_before_a_request(self):
        """GeoServer unpacks svg, png, jpg, bmp and gif files from a style zip
        (2.28.5): a .webp was dropped, and the push said "uploaded"."""
        webp = self.folder / "pin.webp"
        webp.write_bytes(b"RIFF")
        for send in (
            lambda: self.dlg._create_style(
                "towns", "topp", "sld", sld_with_icon(webp), local_icons=True
            ),
            lambda: self.dlg._put_sld_body(
                "from_qgis", "topp", sld_with_icon(webp), local_icons=True
            ),
        ):
            with self.assertRaises(RuntimeError) as raised:
                send()
            self.assertIn(str(webp), str(raised.exception))
        self.assertEqual(self.dlg.gs.calls, [])

    def test_the_upload_form_sends_a_qgis_layers_svg_marker_along(self):
        from qgis.core import (
            QgsMarkerSymbol,
            QgsSingleSymbolRenderer,
            QgsSvgMarkerSymbolLayer,
        )

        layer = point_layer("towns")
        symbol = QgsMarkerSymbol()
        symbol.changeSymbolLayer(0, QgsSvgMarkerSymbolLayer(str(self.svg)))
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        QgsProject.instance().addMapLayer(layer)

        class Uploading(ResourceFormDialog):
            def exec(self):
                self.set_values({"name": "new_towns", "workspace": "topp"})
                self.set_values({"source": tab_styles._SOURCE_QGIS})
                self.get_widget("qgis_layer").setLayer(layer)
                return QDialog.DialogCode.Accepted

        with patch.object(tab_styles, "ResourceFormDialog", Uploading):
            self.dlg._add_style()
        ((_verb, _path, kwargs),) = [c for c in self.dlg.gs.calls if c[0] == "POST"]
        self.assertEqual(kwargs["headers"], {"Content-Type": "application/zip"})
        package = zipfile.ZipFile(io.BytesIO(kwargs["data"]))
        self.assertIn("new_towns_plane.svg", package.namelist())
        self.assertNotIn(str(self.svg), package.read("style.sld").decode())


class TestApplyFetchesTheIcons(unittest.TestCase):
    """QGIS keeps a relative href relative and draws a '?' (measured on
    3.44); GeoServer's 1.0 rendition of a 1.1 style names each icon by its
    path on the server. GeoServer serves the style folder without a login."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        self.dlg.show_error_message = lambda text: self.fail(f"unexpected: {text}")

    def test_a_1_1_style_is_read_as_stored_its_icons_as_urls(self):
        self.dlg.gs.body = sld_with_icon("plane.svg?fill=%23ff0000").encode()
        sld = self.dlg._sld_of("from_qgis", "topp")
        self.assertIn(
            ("GET", "/rest/resource/workspaces/topp/styles/popshade.sld"),
            [call[:2] for call in self.dlg.gs.calls],
        )
        self.assertIn(
            'xlink:href="http://gs.example.org/geoserver/styles/topp/plane.svg'
            '?fill=%23ff0000"',
            sld,
        )

    def test_a_global_1_0_style_keeps_its_sld_path(self):
        self.dlg.gs.body = sld_with_icon("burg02.svg").encode()
        sld = self.dlg._sld_of("population", None)
        self.assertEqual(
            self.dlg.gs.calls[-1][:2], ("GET", "/rest/styles/population.sld")
        )
        self.assertIn(
            'xlink:href="http://gs.example.org/geoserver/styles/burg02.svg"', sld
        )

    def test_an_icon_qgis_cannot_open_is_a_warning_not_a_success(self):
        from qgis.core import (
            QgsMarkerSymbol,
            QgsSingleSymbolRenderer,
            QgsSvgMarkerSymbolLayer,
        )

        folder = Path(tempfile.mkdtemp(prefix="gsm_test_"))
        gone = folder / "anchor.svg"
        gone.write_bytes(b"<svg/>")
        source = point_layer("source")
        symbol = QgsMarkerSymbol()
        symbol.changeSymbolLayer(0, QgsSvgMarkerSymbolLayer(str(gone)))
        source.setRenderer(QgsSingleSymbolRenderer(symbol))
        sld = layer_to_sld(source)
        shutil.rmtree(folder)  # pushed from another machine
        QgsProject.instance().removeAllMapLayers()
        self.addCleanup(QgsProject.instance().removeAllMapLayers)
        QgsProject.instance().addMapLayer(point_layer("towns"))
        warnings, successes = [], []
        self.dlg.show_warning_message = warnings.append
        self.dlg.show_success_message = successes.append

        class Accepting(ResourceFormDialog):
            def exec(inner):
                return QDialog.DialogCode.Accepted

        with (
            patch.object(tab_styles, "ResourceFormDialog", Accepting),
            patch.object(type(self.dlg), "_sld_of", lambda *a: sld),
        ):
            self.dlg._apply_style_to_qgis(["population", GLOBAL])
        self.assertEqual(successes, [])
        self.assertIn(str(gone), warnings[0])
        self.assertIn("now uses the style", warnings[0])


class TestApplyOffersVectorLayersOnly(unittest.TestCase):
    """QGIS reads no SLD into a raster: 'Layer type 1 not supported' (3.40
    source, 3.44 measured), which the warning called a partial read."""

    def setUp(self):
        Recording.opened.clear()
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        self.warnings = []
        self.dlg.show_warning_message = self.warnings.append
        self.dlg.show_error_message = lambda text: self.fail(f"unexpected: {text}")
        self.folder = tempfile.mkdtemp(prefix="gsm_test_")
        self.addCleanup(shutil.rmtree, self.folder)
        QgsProject.instance().removeAllMapLayers()
        self.addCleanup(QgsProject.instance().removeAllMapLayers)

    def test_the_picker_lists_the_vector_layers(self):
        raster, vector = raster_layer(self.folder), point_layer("towns")
        QgsProject.instance().addMapLayers([raster, vector])
        with patch.object(tab_styles, "ResourceFormDialog", Recording):
            self.dlg._apply_style_to_qgis(["population", GLOBAL])
        picker = Recording.opened[0].get_widget("qgis_layer")
        self.assertEqual(
            [picker.layer(index) for index in range(picker.count())], [vector]
        )

    def test_a_project_of_rasters_says_so_without_a_form(self):
        QgsProject.instance().addMapLayer(raster_layer(self.folder))
        with patch.object(tab_styles, "ResourceFormDialog", Recording):
            self.dlg._apply_style_to_qgis(["population", GLOBAL])
        self.assertEqual(Recording.opened, [])
        self.assertIn("no vector layer", self.warnings[0])
        self.assertIn("raster", self.warnings[0])


class TestLegendTextIsText(unittest.TestCase):
    """GeoServer escapes its exception text; the field showed '&amp;'."""

    REPORT = (
        '<?xml version="1.0" encoding="UTF-8"?><ServiceExceptionReport '
        'version="1.1.1"><ServiceException code="StyleNotDefined">No such style: '
        "roads &amp; rivers &quot;v2&quot; &lt;b&gt;</ServiceException>"
        "</ServiceExceptionReport>"
    )

    def test_the_entities_are_decoded_and_an_empty_answer_is_said(self):
        self.assertEqual(
            StyleTabMixin._ogc_exception_text(self.REPORT),
            'No such style: roads & rivers "v2" <b>',
        )
        self.assertEqual(
            StyleTabMixin._ogc_exception_text(""), "GeoServer returned no image"
        )

    def test_the_field_shows_a_tag_as_it_is(self):
        from qgis.PyQt.QtCore import Qt

        dlg = SyncDialog()
        dlg.gs = LegendFakeGS(exception=self.REPORT)
        Recording.opened.clear()
        with patch.object(tab_styles, "ResourceFormDialog", Recording):
            dlg._show_style_info(["population", GLOBAL])
        label = Recording.opened[0].get_widget("legend")
        self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn('rivers "v2" <b>', label.text())


class TestAListingRefusedAtTheTop(unittest.TestCase):
    """A rest.properties rule may refuse /rest/styles.json alone: the tab
    showed "Failed to load styles" and no row, the workspaces' included."""

    def test_the_workspaces_styles_are_listed_and_the_global_scope_reported(self):
        dlg = SyncDialog()
        dlg.gs = FakeGS(broken_workspace=None)  # the global listing
        rows, failures = dlg._fetch_style_rows()
        self.assertEqual([row[:2] for row in rows], [["roads_style", "topp"]])
        self.assertEqual([label for label, _error in failures], ["(global)"])
        self.assertIn("boom", str(failures[0][1]))

    def test_an_existing_workspace_named_like_the_global_scope_gives_no_rows(self):
        # Made elsewhere, its "generic" row was the global one: Delete took that.
        listed = []

        class GS:
            def get_workspaces(inner):
                return ([{"name": GLOBAL}, {"name": "topp"}], 200)

            def get_styles(inner, workspace_name=None):
                listed.append(workspace_name)
                return ([{"name": "generic"}], 200)

        dlg = SyncDialog()
        dlg.gs = GS()
        rows, failures = dlg._fetch_style_rows()
        self.assertEqual(
            [row[:2] for row in rows], [["generic", GLOBAL], ["generic", "topp"]]
        )
        self.assertEqual([label for label, _ in failures], [GLOBAL])
        self.assertNotIn(GLOBAL, listed)


class TestSavingAStyleDeletedMeanwhile(unittest.TestCase):
    """Measured on 2.28.5: a body PUT on a deleted style is a 400 "Invalid
    style: … info is null", a rename a 500 NullPointerException; the banner
    blamed the document."""

    def save(
        self, deleted, failure="HTTP 400: Invalid style: info is null", reread=None
    ):
        dlg = SyncDialog()
        dlg.gs = FakeGS()
        errors = []
        dlg.show_error_message = errors.append
        state = {"gone": False, "failed": False}
        definition = dlg.gs.get_style_definition

        def put_body(name, body, workspace_name=None, format="sld"):
            state["gone"] = deleted
            state["failed"] = True
            raise RuntimeError(failure)

        def get_definition(*args):
            if state["gone"]:
                return ("No such style", 404)
            if state["failed"] and reread:
                return reread
            return definition(*args)

        dlg.gs.rest_service.create_style = put_body
        dlg.gs.get_style_definition = get_definition

        class Editing(ResourceFormDialog):
            def exec(self):
                self.get_widget("body").setText("<StyledLayerDescriptor/>")
                return QDialog.DialogCode.Accepted

        with patch.object(tab_styles, "ResourceFormDialog", Editing):
            dlg._show_style_info(["population", GLOBAL])
        return errors

    def test_a_deleted_style_is_said_to_be_gone(self):
        (error,) = self.save(deleted=True)
        self.assertIn("deleted on the server", error)
        self.assertNotIn("Invalid style", error)

    def test_a_style_still_there_keeps_geoservers_reason(self):
        (error,) = self.save(deleted=False)
        self.assertIn("Invalid style", error)

    def test_a_server_that_fails_the_reread_too_keeps_its_own_error(self):
        # Only a 404 means gone: a 503 on the save and on the re-read is the
        # server's trouble, not a deletion.
        (error,) = self.save(
            deleted=False,
            failure="HTTP 503: Service Unavailable",
            reread=("Service Unavailable", 503),
        )
        self.assertIn("503", error)
        self.assertNotIn("deleted on the server", error)


# ############################################################################
# ####### Stand-alone run ########
# ################################
if __name__ == "__main__":
    unittest.main()
