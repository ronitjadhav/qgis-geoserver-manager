#! python3  # noqa E265

"""
Usage from the repo root folder:

.. code-block:: bash

    QT_QPA_PLATFORM=offscreen python -m unittest tests.qgis.test_tab_cascaded
"""

from unittest.mock import patch

from qgis.PyQt.QtWidgets import QDialog
from qgis.testing import start_app, unittest

from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog
from geoserver_manager.gui.tab_cascaded import WMS, WMTS
from geoserver_manager.toolbelt.rest import PartlySaved
from tests.qgis.sync_dialog import SyncDialog

start_app()

CAPS = "http://remote.example.org/geoserver/wms?service=WMS&request=GetCapabilities"
TILES = "http://remote.example.org/geoserver/gwc/service/wmts?REQUEST=GetCapabilities"

# A cascaded WMS layer as the library's get_wms_layer() hands it back (asdict)
STATES_DETAIL = {
    "name": "states",
    "nativeName": "topp:states",
    "store": {"name": "topp:remote"},
    "namespace": {"name": "topp"},
    "title": "USA Population",
    "abstract": "Census data on the states.",
    "latLonBoundingBox": {
        "minx": -124.73,
        "maxx": -66.97,
        "miny": 24.96,
        "maxy": 49.37,
        "crs": "EPSG:4326",
    },
    "srs": "EPSG:4326",
    "keywords": ["census", "states"],
    "enabled": True,
}

# The same through a raw GET of a WMTS layer: keywords and CRS are wrapped
RAW_TILES_DETAIL = {
    "wmtsLayer": {
        "name": "states",
        "nativeName": "topp:states",
        "title": "USA Population",
        "srs": "EPSG:4326",
        "enabled": True,
        "keywords": {"string": "census"},
        "latLonBoundingBox": {
            "minx": -124.73,
            "maxx": -66.97,
            "miny": 24.96,
            "maxy": 49.37,
            "crs": {"@class": "projected", "$": "EPSG:4326"},
        },
    }
}


class Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def boom(*args, **kwargs):
    raise RuntimeError("HTTP 500: boom")


class FakeGS:
    """topp has a WMS store `remote` publishing `states`; sf a WMTS store
    `tiles` publishing nothing yet; `broken` cannot be listed at all."""

    PAYLOADS = {
        "/rest/workspaces/topp/wmsstores.json": {
            "wmsStores": {"wmsStore": [{"name": "remote", "href": "…"}]}
        },
        "/rest/workspaces/topp/wmtsstores.json": {"wmtsStores": ""},
        # The raw document: user, maxConnections and the timeouts are in it,
        # where the library's WmsStore model drops them.
        "/rest/workspaces/topp/wmsstores/remote.json": {
            "wmsStore": {
                "name": "remote",
                "type": "WMS",
                "enabled": True,
                "workspace": {"name": "topp"},
                "capabilitiesURL": CAPS,
                "user": "bob",
                "password": "crypt1:SECRET",
                "maxConnections": 10,
                "readTimeout": 90,
                "connectTimeout": 20,
            }
        },
        "/rest/workspaces/sf/wmsstores.json": {"wmsStores": ""},
        # a one-entry collection: GeoServer writes the entry bare, not in a list
        "/rest/workspaces/sf/wmtsstores.json": {
            "wmtsStores": {"wmtsStore": {"name": "tiles", "href": "…"}}
        },
        "/rest/workspaces/sf/wmtsstores/tiles.json": {
            "wmtsStore": {
                "name": "tiles",
                "type": "WMTS",
                "enabled": False,
                "workspace": {"name": "sf"},
                "capabilitiesURL": TILES,
            }
        },
        "/rest/workspaces/topp/wmsstores/remote/wmslayers.json": {
            "wmsLayers": {"wmsLayer": [{"name": "states", "href": "…"}]}
        },
        "/rest/workspaces/sf/wmtsstores/tiles/layers.json": {"wmtsLayers": ""},
        "/rest/workspaces/sf/wmtsstores/tiles/layers/states.json": RAW_TILES_DETAIL,
        # As GeoServer answers a layer with an international title: that one
        # alone, no plain title, which the library's model reads (measured).
        "/rest/workspaces/topp/wmsstores/remote/wmslayers/states.json": {
            "wmsLayer": {
                "name": "states",
                "nativeName": "topp:states",
                "internationalTitle": {"fr": "États", "en": "States"},
                "srs": "EPSG:4326",
                "enabled": True,
                "keywords": {"string": ["census", "states"]},
            }
        },
    }
    AVAILABLE = {
        "/rest/workspaces/topp/wmsstores/remote/wmslayers.json": {
            "list": {"string": ["topp:states", "topp:roads"]}
        },
        # one advertised layer: a bare string, not a one-item list
        "/rest/workspaces/sf/wmtsstores/tiles/layers.json": {
            "list": {"string": "topp:states"}
        },
    }

    def __init__(self):
        self.calls = []
        outer = self

        class Endpoints:
            def wmsstores(inner, ws):
                return f"/rest/workspaces/{ws}/wmsstores.json"

            def wmsstore(inner, ws, name):
                return f"/rest/workspaces/{ws}/wmsstores/{name}.json"

            def wmtsstores(inner, ws):
                return f"/rest/workspaces/{ws}/wmtsstores.json"

            def wmtsstore(inner, ws, name):
                return f"/rest/workspaces/{ws}/wmtsstores/{name}.json"

            def wmslayers(inner, ws, store):
                return f"/rest/workspaces/{ws}/wmsstores/{store}/wmslayers.json"

            def wmslayer(inner, ws, store, layer):
                return f"/rest/workspaces/{ws}/wmsstores/{store}/wmslayers/{layer}.json"

            def wmtslayers(inner, ws, store):
                return f"/rest/workspaces/{ws}/wmtsstores/{store}/layers.json"

            def wmtslayer(inner, ws, store, layer):
                return f"/rest/workspaces/{ws}/wmtsstores/{store}/layers/{layer}.json"

        class Client:
            def get(inner, path, **kwargs):
                outer.calls.append(("GET", path, kwargs))
                return outer.answer(path, kwargs.get("params") or {})

            def post(inner, path, **kwargs):
                outer.calls.append(("POST", path, kwargs))
                return Response("created", 201)

            def delete(inner, path, **kwargs):
                outer.calls.append(("DELETE", path, kwargs))
                return Response("", 200)

        class Service:
            rest_client = Client()
            rest_endpoints = Endpoints()

            def resource_exists(inner, path):
                outer.calls.append(("EXISTS", path))
                return outer.answer(path, {}).status_code == 200

        self.rest_service = Service()

    def answer(self, path, params):
        if "/broken/" in path:
            raise RuntimeError("HTTP 500: boom")
        if params.get("list") == "available":
            return Response(self.AVAILABLE[path])
        if path in self.PAYLOADS:
            return Response(self.PAYLOADS[path])
        return Response("No such resource", 404)

    def get_workspaces(self):
        return ([{"name": "topp"}, {"name": "sf"}, {"name": "broken"}], 200)

    def get_wms_store(self, ws, name):
        self.calls.append(("get_wms_store", ws, name))
        if (ws, name) == ("topp", "remote"):
            return (
                {
                    "name": "remote",
                    "type": "WMS",
                    "workspace": "topp",
                    "capabilitiesURL": CAPS,
                    "enabled": True,
                    "_default": False,
                    "disableOnConnFailure": False,
                },
                200,
            )
        return ("No such wms store", 404)

    def create_wms_store(self, ws, name, url):
        self.calls.append(("create_wms_store", ws, name, url))
        return (name, 201)

    def create_wmts_store(self, ws, name, url):
        self.calls.append(("create_wmts_store", ws, name, url))
        return (name, 201)

    def delete_wms_store(self, ws, name):
        self.calls.append(("delete_wms_store", ws, name))
        return ("", 200)

    def delete_wmts_store(self, ws, name):
        self.calls.append(("delete_wmts_store", ws, name))
        return ("", 200)

    def get_wms_layer(self, ws, store, layer):
        self.calls.append(("get_wms_layer", ws, store, layer))
        if (ws, store, layer) == ("topp", "remote", "states"):
            return (STATES_DETAIL, 200)
        return ("No such cascaded wms", 404)

    def create_wms_layer(self, ws, store, native, published=None):
        self.calls.append(("create_wms_layer", ws, store, native, published))
        return (published, 201)

    def delete_wms_layer(self, ws, store, layer):
        self.calls.append(("delete_wms_layer", ws, store, layer))
        return ("", 200)


REMOTE_ROW = ["remote", "topp", WMS, "Yes", CAPS]
TILES_ROW = ["tiles", "sf", WMTS, "No", TILES]


class TestStoreEditBody(unittest.TestCase):
    """What an edit of a cascaded store sends (rules measured on 2.28.5)."""

    BEFORE = {
        "capabilities_url": "http://a",
        "enabled": True,
        "user": "alice",
        "password": "",
        "max_connections": 6,
        "read_timeout": 60,
        "connect_timeout": 30,
    }

    def body(self, **after):
        from geoserver_manager.gui.tab_cascaded import CascadedStoreTabMixin

        return CascadedStoreTabMixin._cascaded_store_changes(
            self.BEFORE, dict(self.BEFORE, **after)
        )

    def test_a_blank_password_is_left_out_which_keeps_it(self):
        self.assertEqual(self.body(read_timeout=90), {"readTimeout": 90})
        self.assertEqual(self.body(), {})  # an untouched form sends nothing

    def test_a_typed_password_replaces_it(self):
        self.assertEqual(self.body(password="new"), {"password": "new"})

    def test_clearing_the_user_removes_authentication_with_nulls(self):
        """An empty string is stored encrypted and breaks the store."""
        self.assertEqual(self.body(user=""), {"user": None, "password": None})


class TestViewerRead(unittest.TestCase):
    def test_a_layer_that_cannot_be_read_leaves_the_viewer_alone(self):
        """It filled blank fields after the error banner; now like Coverages."""
        dlg = SyncDialog()
        dlg._fetch = lambda action, failure, **kwargs: None
        self.assertIsNone(dlg._cascaded_layer_values("topp", "store", "WMS", "x"))

    def test_an_international_title_reaches_the_viewer(self):
        # get_wms_layer()'s model reads the plain title only: it showed blank.
        dlg = SyncDialog()
        dlg.gs = FakeGS()
        values = dlg._cascaded_layer_values("topp", "remote", "WMS", "states")
        self.assertEqual(values["title"], "en: States; fr: États")
        self.assertEqual(values["keywords"], "census, states")


class TestWmsStoreDetail(unittest.TestCase):
    def test_the_edit_form_shows_what_is_stored_but_never_the_password(self):
        # Read through the library, the user was blank and the limits were
        # the defaults, so clearing the credentials could never be sent.
        dlg = SyncDialog()
        dlg.gs = FakeGS()
        detail = dlg._cascaded_store_detail("topp", "remote", "WMS")
        values = dlg._cascaded_store_form_values(detail, "topp", "WMS", [])
        self.assertEqual(values["user"], "bob")
        self.assertEqual(values["max_connections"], 10)
        self.assertEqual(values["read_timeout"], 90)
        self.assertEqual(values["connect_timeout"], 20)
        self.assertEqual(values["password"], "")  # never shown (invariant 5)

        values = dlg._cascaded_store_form_values(
            {
                "name": "tiles",
                "type": "WMTS",
                "enabled": False,
                "capabilitiesURL": TILES,
            },
            "sf",
            WMTS,
            [],
        )
        self.assertEqual(values["capabilities_url"], TILES)
        self.assertIs(values["enabled"], False)  # the edit form's checkbox
        self.assertEqual(values["password"], "")
        self.assertEqual(values["layers"], "-")


class TestListing(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.gs = self.dlg.gs = FakeGS()
        self.dlg.show_warning_message = lambda text: None

    def test_rows_carry_both_kinds_and_a_broken_workspace_is_reported_not_fatal(self):
        rows, failures = self.dlg._fetch_cascaded_store_rows()
        # Names and kinds at once; the detail cells follow per page.
        self.assertEqual(
            [row[:3] for row in rows], [r[:3] for r in (REMOTE_ROW, TILES_ROW)]
        )
        self.assertEqual([label for label, _ in failures], ["broken"])
        self.dlg._load_cascaded_stores()
        self.assertEqual(self.dlg._all_rows, [REMOTE_ROW, TILES_ROW])
        self.assertEqual(self.dlg.resultsTable.columnCount(), 6)
        self.assertEqual(self.dlg.btn_add.text(), "Add a Cascaded Store")

    def test_a_store_whose_get_fails_shows_dashes_and_is_named(self):
        warnings = []
        self.dlg.show_warning_message = warnings.append
        detail = self.dlg._cascaded_store_detail

        def broken(ws_name, name, kind):
            if name == "tiles":
                raise RuntimeError("HTTP 500: boom")
            return detail(ws_name, name, kind)

        self.dlg._cascaded_store_detail = broken
        self.dlg._load_cascaded_stores()
        self.assertIn(["tiles", "sf", WMTS, "-", "-"], self.dlg._all_rows)
        # Beside the listing's own warning about the `broken` workspace.
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("sf:tiles" in warning for warning in warnings), warnings)

    def test_layer_names_configured_and_advertised(self):
        self.assertEqual(
            self.dlg._cascaded_layer_names("topp", "remote", WMS), ["states"]
        )
        self.assertEqual(
            self.dlg._cascaded_layer_names("topp", "remote", WMS, available=True),
            ["topp:states", "topp:roads"],
        )
        # an empty collection, and a single advertised layer written bare
        self.assertEqual(self.dlg._cascaded_layer_names("sf", "tiles", WMTS), [])
        self.assertEqual(
            self.dlg._cascaded_layer_names("sf", "tiles", WMTS, available=True),
            ["topp:states"],
        )


class TestCreate(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.gs = self.dlg.gs = FakeGS()

    def values(self, **overrides):
        values = {
            "workspace": "sf",
            "name": "new",
            "type": WMS,
            "capabilities_url": CAPS,
        }
        values.update(overrides)
        return values

    def creates(self):
        return [c for c in self.gs.calls if c[0].startswith("create_")]

    def test_create_routes_by_type(self):
        self.dlg._create_cascaded_store_from_values(self.values())
        self.dlg._create_cascaded_store_from_values(
            self.values(type=WMTS, capabilities_url=TILES)
        )
        self.assertEqual(
            self.creates(),
            [
                ("create_wms_store", "sf", "new", CAPS),
                ("create_wmts_store", "sf", "new", TILES),
            ],
        )

    def test_credentials_and_limits_follow_the_create_in_one_merging_put(self):
        """An authenticated remote could not be cascaded at all before."""
        sent = []
        self.dlg._raw_rest = lambda method, path, **kw: sent.append((method, path, kw))
        self.dlg._create_cascaded_store_from_values(
            self.values(user="alice", password="s3cret", read_timeout=90)
        )
        ((method, path, kwargs),) = sent
        self.assertEqual(method, "put")
        self.assertIn("/wmsstores/new", path)
        self.assertEqual(
            kwargs["json"],
            {"wmsStore": {"user": "alice", "readTimeout": 90, "password": "s3cret"}},
        )

    def test_a_store_whose_credentials_fail_is_reported_created(self):
        self.dlg._put_cascaded_store = boom
        with self.assertRaises(PartlySaved) as caught:
            self.dlg._create_cascaded_store_from_values(
                self.values(user="bob", password="secret")
            )
        self.assertIn("created", str(caught.exception))
        self.assertEqual(self.creates(), [("create_wms_store", "sf", "new", CAPS)])

    def test_a_plain_create_sends_no_second_request(self):
        sent = []
        self.dlg._raw_rest = lambda method, path, **kw: sent.append(method)
        self.dlg._create_cascaded_store_from_values(
            self.values(
                user="",
                password="",
                max_connections=6,
                read_timeout=60,
                connect_timeout=30,
            )
        )
        self.assertEqual(sent, [])

    def test_add_refuses_an_existing_name_of_either_kind(self):
        with self.assertRaises(ValueError):
            self.dlg._create_cascaded_store_from_values(
                self.values(workspace="topp", name="remote")
            )
        with self.assertRaises(ValueError):
            self.dlg._create_cascaded_store_from_values(
                self.values(name="tiles", type=WMTS)
            )
        self.assertEqual(self.creates(), [])

    def test_both_forms_refuse_a_url_without_a_scheme_before_they_close(self):
        # The form's own check (dlg_resource_form: "url"), not a check after
        # Save: GeoServer accepts any string and fails when layers are listed.
        for fields in (
            self.dlg._cascaded_store_fields(["topp"]),
            self.dlg._cascaded_store_info_fields(),
        ):
            (field,) = [f for f in fields if f["key"] == "capabilities_url"]
            self.assertTrue(field["url"])


class TestCascadedLayers(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.gs = self.dlg.gs = FakeGS()
        self.messages = []
        self.dlg.show_success_message = self.messages.append
        self.dlg.show_warning_message = self.messages.append
        self.dlg.show_error_message = self.messages.append

    def test_publish_goes_through_the_library_for_wms_and_posts_for_wmts(self):
        self.dlg._create_cascaded_layer("topp", "remote", WMS, "topp:roads", "roads")
        self.dlg._create_cascaded_layer("sf", "tiles", WMTS, "topp:states", "usa")
        self.assertIn(
            ("create_wms_layer", "topp", "remote", "topp:roads", "roads"), self.gs.calls
        )
        posts = [c for c in self.gs.calls if c[0] == "POST"]
        self.assertEqual(
            posts,
            [
                (
                    "POST",
                    "/rest/workspaces/sf/wmtsstores/tiles/layers.json",
                    {
                        "json": {
                            "wmtsLayer": {"name": "usa", "nativeName": "topp:states"}
                        }
                    },
                )
            ],
        )

    def test_publish_refuses_a_name_already_published_or_unsafe(self):
        for name in ("states", "a/b"):
            with self.assertRaises(ValueError, msg=name):
                self.dlg._create_cascaded_layer(
                    "topp", "remote", WMS, "topp:states", name
                )
        self.assertFalse([c for c in self.gs.calls if c[0] == "create_wms_layer"])

    def test_the_publish_dialog_defaults_the_name_to_the_remote_name_unprefixed(self):
        with (
            patch.object(
                ResourceFormDialog, "exec", return_value=QDialog.DialogCode.Accepted
            ),
            patch.object(
                ResourceFormDialog,
                "get_values",
                return_value={"native_name": "topp:roads", "name": ""},
            ),
        ):
            self.dlg._publish_cascaded_layer(REMOTE_ROW)
        create = ("create_wms_layer", "topp", "remote", "topp:roads", "roads")
        self.assertIn(create, self.gs.calls)
        self.assertIn("'topp:roads' published as layer 'roads'.", self.messages)
        # No reload: no cell of the table depends on the store's layers, and
        # one cost two collection GETs per workspace.
        self.assertEqual(self.gs.calls[self.gs.calls.index(create) + 1 :], [])

    def test_delete_sends_recurse_because_the_layer_references_the_resource(self):
        self.dlg._delete_cascaded_layer("topp", "remote", WMS, "states")
        self.dlg._delete_cascaded_layer("sf", "tiles", WMTS, "states")
        self.assertIn(("delete_wms_layer", "topp", "remote", "states"), self.gs.calls)
        self.assertIn(
            (
                "DELETE",
                "/rest/workspaces/sf/wmtsstores/tiles/layers/states.json",
                {"params": {"recurse": "true"}},
            ),
            self.gs.calls,
        )

    def test_layer_form_values_read_both_payload_shapes(self):
        from_library = self.dlg._cascaded_layer_form_values(STATES_DETAIL)
        self.assertEqual(from_library["native_name"], "topp:states")
        self.assertEqual(from_library["keywords"], "census, states")
        # the one bounding-box format every tab uses (toolbelt/payload.py)
        self.assertEqual(
            from_library["bounds"], "-124.73, 24.96 → -66.97, 49.37  (EPSG:4326)"
        )
        self.assertEqual(from_library["enabled"], "Yes")
        raw = self.dlg._cascaded_layer_form_values(RAW_TILES_DETAIL["wmtsLayer"])
        self.assertEqual(raw["keywords"], "census")
        self.assertEqual(raw["bounds"], "-124.73, 24.96 → -66.97, 49.37  (EPSG:4326)")
        self.assertEqual(raw["abstract"], "")

    def test_the_layers_viewer_warns_when_nothing_is_published(self):
        self.dlg._show_cascaded_layers(TILES_ROW)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("publishes no layer yet", self.messages[0])


class TestDelete(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.gs = self.dlg.gs = FakeGS()
        self.dlg.show_success_message = lambda text: None
        self.asked = []

        def confirm(question, labels=(), cascade=""):
            self.asked.append((question, labels, cascade))
            return True

        self.dlg._confirm_delete = confirm

    def test_store_delete_names_the_cascade_and_uses_the_typed_library_call(self):
        self.dlg._delete_selected_cascaded_stores([REMOTE_ROW, TILES_ROW])
        question, labels, cascade = self.asked[0]
        self.assertIn("delete 2 cascaded store", question)
        self.assertEqual(labels, ["topp:remote", "sf:tiles"])
        self.assertIn("cascaded layer", cascade)
        self.assertIn(("delete_wms_store", "topp", "remote"), self.gs.calls)
        self.assertIn(("delete_wmts_store", "sf", "tiles"), self.gs.calls)


class TestNamesInPaths(unittest.TestCase):
    """Security: a name is refused before it reaches a request, and one that
    the server already holds is quoted on its way into a path."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.gs = FakeGS()
        self.dlg.gs = self.gs

    def test_a_store_name_the_paths_cannot_carry_is_refused_first(self):
        with self.assertRaises(ValueError):
            self.dlg._create_cascaded_store_from_values(
                {
                    "workspace": "topp",
                    "name": "a#b",
                    "type": WMS,
                    "capabilities_url": CAPS,
                }
            )
        self.assertEqual([c for c in self.gs.calls if c[0].startswith("create")], [])

    def test_a_workspace_the_paths_cannot_carry_is_refused_first(self):
        """The library sends workspaces/topp#x/... as workspaces/topp: the
        check read the workspace and called the store taken."""
        with self.assertRaises(ValueError) as refused:
            self.dlg._check_new_cascaded_store(
                {"workspace": "topp#x", "name": "new", "type": WMS}
            )
        self.assertIn("topp#x", str(refused.exception))
        self.assertEqual(self.gs.calls, [])

    def test_names_from_the_server_are_quoted_into_the_raw_paths(self):
        import contextlib

        # the fake knows none of these, so each GET is a 404; the path is the point
        with contextlib.suppress(RuntimeError):
            self.dlg._cascaded_store_detail("my ws", "my store", WMTS)
        with contextlib.suppress(RuntimeError):
            self.dlg._cascaded_layer_detail("my ws", "my store", WMTS, "a b")
        with contextlib.suppress(RuntimeError):
            self.dlg._cascaded_layer_names("my ws", "my store", WMS)
        self.dlg._cascaded_store_exists("my ws", "my store", WMTS)
        self.dlg._delete_cascaded_layer("my ws", "my store", WMTS, "a#b")
        paths = [
            call[1] for call in self.gs.calls if call[0] in ("GET", "DELETE", "EXISTS")
        ]
        self.assertEqual(
            paths.count("/rest/workspaces/my%20ws/wmtsstores/my%20store.json"), 2
        )
        self.assertIn(
            "/rest/workspaces/my%20ws/wmsstores/my%20store/wmslayers.json", paths
        )
        self.assertIn(
            "/rest/workspaces/my%20ws/wmtsstores/my%20store/layers/a%20b.json", paths
        )
        self.assertIn(
            "/rest/workspaces/my%20ws/wmtsstores/my%20store/layers/a%23b.json", paths
        )


class TestLayersViewerIsAViewer(unittest.TestCase):
    """Its primary button used to be *Delete layer*: Enter on a details dialog
    destroyed. It is a viewer now; deleting is the Layers tab's job."""

    def test_no_delete_button_and_no_delete_on_close(self):
        from qgis.PyQt.QtWidgets import QDialogButtonBox

        dlg = SyncDialog()
        gs = FakeGS()
        dlg.gs = gs
        opened = []

        class Recording(ResourceFormDialog):
            def exec(inner):
                opened.append(inner)
                return QDialog.DialogCode.Accepted  # Enter, as before

        with patch("geoserver_manager.gui.tab_cascaded.ResourceFormDialog", Recording):
            dlg._show_cascaded_layers(REMOTE_ROW)
        form = opened[0]
        ok = form._button_box.button(QDialogButtonBox.StandardButton.Ok)
        self.assertFalse(ok.isVisibleTo(form))
        self.assertEqual(
            [c for c in gs.calls if c[0] in ("DELETE", "delete_wms_layer")], []
        )


if __name__ == "__main__":
    unittest.main()
