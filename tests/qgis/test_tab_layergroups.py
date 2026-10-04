#! python3  # noqa E265

"""
Usage from the repo root folder:

.. code-block:: bash

    python -m unittest tests.qgis.test_tab_layergroups
"""

# standard library
import json
from unittest.mock import patch

from qgis.core import QgsCoordinateTransformContext, QgsRectangle
from qgis.PyQt.QtWidgets import QDialog
from qgis.testing import start_app, unittest

# project
from geoserver_manager.gui import tab_layergroups, tab_layers
from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog
from geoserver_manager.gui.scope import GLOBAL
from geoserver_manager.gui.tab_layergroups import LayerGroupTabMixin
from tests.qgis.sync_dialog import FakePrefs, SyncDialog

start_app()


def rows(text):
    """The group form's rows from a compact "layer = style" notation, one per line."""
    return [
        [name.strip(), style.strip()]
        for name, _, style in (line.partition("=") for line in text.splitlines())
        if name.strip()
    ]


# A group as GeoServer really answers it: the abstract under "abstractTxt",
# per-publishable styles parallel to the publishables, computed bounds.
TASMANIA = {
    "name": "tasmania",
    "mode": "SINGLE",
    "title": "Tasmania",
    "abstractTxt": "Tasmania from the Digital Chart of the World.",
    "publishables": {
        "published": [
            {"@type": "layer", "name": "topp:tasmania_state_boundaries"},
            {"@type": "layer", "name": "topp:tasmania_roads"},
        ]
    },
    "styles": {"style": ["", {"name": "simple_roads"}]},
    "bounds": {
        "minx": 143.83,
        "maxx": 148.47,
        "miny": -43.64,
        "maxy": -39.57,
        "crs": "EPSG:4326",
    },
}

# GeoServer unwraps a single entry into a bare object instead of a one-item list.
SOLO = {
    "name": "solo",
    "mode": "NAMED",
    "publishables": {"published": {"@type": "layerGroup", "name": "tasmania"}},
    "styles": {"style": ""},
}

ROADS_GROUP = {
    "name": "roads_group",
    "mode": "CONTAINER",
    "workspace": {"name": "topp"},
    "publishables": {"published": [{"@type": "layer", "name": "topp:tasmania_roads"}]},
}


EO_GROUP = {
    "name": "eo_group",
    "mode": "EO",
    "publishables": {"published": {"@type": "layer", "name": "nurc:mosaic"}},
    "styles": {"style": ""},
    "rootLayer": {"name": "topp:tasmania_roads"},
    "rootLayerStyle": {"name": "simple_roads"},
}


# ############################################################################
# ########## Fakes ###############
# ################################


class FakeGS:
    """Two global groups, one in a workspace, and a REST client that records."""

    def __init__(self, broken_workspace=None, exists=False):
        self.broken_workspace = broken_workspace
        self.exists = exists
        self.calls = []
        outer = self

        class Response:
            def __init__(self, payload, status_code=200):
                self._payload = payload
                self.status_code = status_code
                self.text = json.dumps(payload)

            def json(self):
                return self._payload

        class Client:
            def get(inner, path, **kwargs):
                outer.calls.append(("GET", path, kwargs))
                return Response(outer.payload_for(path))

            def post(inner, path, **kwargs):
                outer.calls.append(("POST", path, kwargs))
                return Response({}, 201)

            def put(inner, path, **kwargs):
                outer.calls.append(("PUT", path, kwargs))
                return Response({})

            def delete(inner, path, **kwargs):
                outer.calls.append(("DELETE", path, kwargs))
                return Response({})

        class Endpoints:
            base_url = "/rest"

            def layergroups(inner, workspace_name):
                return f"/rest/workspaces/{workspace_name}/layergroups.json"

            def layergroup(inner, workspace_name, layergroup_name):
                return (
                    f"/rest/workspaces/{workspace_name}"
                    f"/layergroups/{layergroup_name}.json"
                )

        class Rest:
            rest_client = Client()
            rest_endpoints = Endpoints()

            def resource_exists(inner, path):
                outer.calls.append(("EXISTS", path, {}))
                return outer.exists

        self.rest_service = Rest()

    def payload_for(self, path):
        if path == "/rest/layergroups.json":
            return {
                "layerGroups": {
                    "layerGroup": [
                        {"name": "solo", "href": "…"},
                        {"name": "tasmania", "href": "…"},
                        {"name": "eo_group", "href": "…"},
                    ]
                }
            }
        if path == "/rest/layers.json":
            return {
                "layers": {
                    "layer": [{"name": "topp:tasmania_roads"}, {"name": "nurc:mosaic"}]
                }
            }
        for group in (TASMANIA, SOLO, ROADS_GROUP, EO_GROUP):
            if path.endswith(f"/layergroups/{group['name']}.json"):
                return {"layerGroup": group}
        raise AssertionError(f"unexpected GET {path}")

    def get_workspaces(self):
        return ([{"name": "topp"}, {"name": "empty"}], 200)

    def get_layer_groups(self, workspace_name):
        if workspace_name == self.broken_workspace:
            raise RuntimeError("HTTP 500: boom")
        if workspace_name == "topp":
            return ([{"name": "roads_group"}], 200)
        return ([], 200)

    def get_styles(self, workspace_name=None):
        return ([{"name": "simple_roads"}, {"name": "line"}], 200)

    def get_style_definition(self, name, workspace_name=None):
        self.calls.append(("get_style_definition", name, workspace_name))
        if name in ("simple_roads", "disputed"):
            return ({"name": name, "format": "sld"}, 200)
        return ("<html>Not Found</html>", 404)

    def delete_layer_group(self, workspace_name, name):
        self.calls.append(("delete_layer_group", workspace_name, name))
        return ("", 200)


class Recording(ResourceFormDialog):
    opened = []

    def exec(self):
        Recording.opened.append(self)
        return QDialog.DialogCode.Rejected


# ############################################################################
# ########## Tests ###############
# ################################


class TestLayerGroupsTab(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        self.warnings = []
        self.dlg.show_warning_message = self.warnings.append
        self.dlg.show_error_message = lambda text: self.fail(f"unexpected: {text}")
        self.dlg.show_success_message = lambda text: None
        Recording.opened.clear()

    def test_lists_global_and_workspace_groups_with_mode_and_size(self):
        self.dlg._load_layer_groups()
        self.assertEqual(
            self.dlg._all_rows,
            [
                # GeoServer's own words for the modes, not the enum
                ["eo_group", GLOBAL, "EO", "1"],
                ["solo", GLOBAL, "NAMED", "1"],
                ["tasmania", GLOBAL, "SINGLE", "2"],
                ["roads_group", "topp", "CONTAINER", "1"],
            ],
        )
        self.assertEqual(self.warnings, [])
        # The row keeps the enum; the cell shows it in words.
        shown = [
            self.dlg.resultsTable.item(row, 2).text()
            for row in range(self.dlg.resultsTable.rowCount())
        ]
        self.assertIn("Earth Observation Tree", shown)

    def test_one_unreadable_workspace_keeps_the_rest(self):
        self.dlg.gs = FakeGS(broken_workspace="topp")
        self.dlg._load_layer_groups()
        self.assertEqual(
            [row[0] for row in self.dlg._all_rows], ["eo_group", "solo", "tasmania"]
        )
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("topp", self.warnings[0])

    def test_a_workspace_name_is_quoted_for_the_library(self):
        """The library interpolates the name into the path as it is, so the
        listing of "w#x" asked for "w"; the pickers already quoted it."""
        asked = []
        self.dlg.gs.get_workspaces = lambda: ([{"name": "w#x"}], 200)
        self.dlg.gs.get_layer_groups = lambda ws: (asked.append(ws), ([], 200))[1]
        _rows, failures = self.dlg._fetch_layer_group_rows()
        self.assertEqual(asked, ["w%23x"])
        self.assertEqual(failures, [])


class TestGroupDetail(unittest.TestCase):
    """The detail view reads what GeoServer actually stores."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()

    def test_prefill_keeps_the_abstract_the_order_and_the_styles(self):
        values = LayerGroupTabMixin._group_form_values(TASMANIA, "tasmania", GLOBAL)
        # GeoServer writes "abstractTxt"; the library's model reads "abstract"
        # and so loses it; this would be empty if the detail came from there.
        self.assertEqual(
            values["abstract"], "Tasmania from the Digital Chart of the World."
        )
        self.assertEqual(values["title"], "Tasmania")
        self.assertEqual(values["mode"], "SINGLE")
        self.assertEqual(values["workspace"], GLOBAL)
        self.assertEqual(
            values["layers"],
            [
                ["topp:tasmania_state_boundaries", ""],
                ["topp:tasmania_roads", "simple_roads"],
            ],
        )
        self.assertIn("143.83, -43.64 → 148.47, -39.57", values["bounds"])
        self.assertIn("EPSG:4326", values["bounds"])
        values = LayerGroupTabMixin._group_form_values(
            {"bounds": {"minx": 1, "miny": 2, "maxx": 3, "maxy": 4}}, "g", GLOBAL
        )
        self.assertEqual(values["bounds"], "1, 2 → 3, 4")  # no crs: no empty parens

    def test_a_single_publishable_and_a_nested_group_are_not_lost(self):
        values = LayerGroupTabMixin._group_form_values(SOLO, "solo", GLOBAL)
        # The edit form's own syntax: a group is named like a layer.
        self.assertEqual(values["layers"], [["tasmania", ""]])
        self.assertEqual(values["bounds"], "")
        self.assertEqual(values["abstract"], "")

    def test_internationalised_text_is_readable(self):
        values = LayerGroupTabMixin._group_form_values(
            {"internationalTitle": {"en": "Roads", "fr": "Routes"}}, "g", GLOBAL
        )
        self.assertEqual(values["title"], "en: Roads; fr: Routes")

    def test_the_dialog_edits_everything_but_the_name(self):
        # GeoServer answers 403 to a layer group rename.
        with patch.object(tab_layergroups, "ResourceFormDialog", Recording):
            self.dlg._show_layer_group_info(["tasmania", GLOBAL])
        form = Recording.opened[-1]
        self.assertFalse(form.get_widget("layers").isReadOnly())
        self.assertTrue(form.get_widget("mode").isEnabled())
        self.assertTrue(form.get_widget("name").isReadOnly())  # copyable, not greyed
        # The group itself is not offered as one of its own members.
        pick = form.get_widget("layers").picker
        offered = [pick.itemText(i) for i in range(pick.count())]
        self.assertIn("solo", offered)
        self.assertNotIn("tasmania", offered)

    def test_a_workspace_group_is_offered_the_global_groups(self):
        # Measured on 2.28.5: a group in "sf" may hold a global group (201,
        # stored). The picker offered this workspace's names only, although
        # the same name typed was accepted.
        with patch.object(tab_layergroups, "ResourceFormDialog", Recording):
            self.dlg._show_layer_group_info(["roads_group", "topp"])
        pick = Recording.opened[-1].get_widget("layers").picker
        offered = [pick.itemText(i) for i in range(pick.count())]
        self.assertIn("tasmania", offered)
        self.assertIn("topp:tasmania_roads", offered)
        self.assertNotIn("nurc:mosaic", offered)
        self.assertNotIn("topp:roads_group", offered)

    def test_an_earth_observation_group_keeps_its_mode_shown_in_words(self):
        # Every way of clearing the root layer is refused by GeoServer.
        with patch.object(tab_layergroups, "ResourceFormDialog", Recording):
            self.dlg._show_layer_group_info(["eo_group", GLOBAL])
        form = Recording.opened[-1]
        self.assertFalse(form.get_widget("mode").isEnabled())
        self.assertIn("root_layer", form.get_values())
        # The translated label was stored and read back to the enum.
        self.assertEqual(
            form.get_widget("mode").currentText(), "Earth Observation Tree"
        )
        self.assertEqual(form.get_values()["mode"], "EO")
        [mode] = [f for f in self.dlg._group_fields([], []) if f["key"] == "mode"]
        self.assertEqual(mode["options"][0], ("Single", "SINGLE"))
        self.assertIn("Opaque Container", mode["help"])

    def test_a_workspace_group_lists_only_its_own_workspace(self):
        """Every workspace's groups were listed, then all but one dropped."""
        asked = []
        real = self.dlg.gs.get_layer_groups
        self.dlg.gs.get_layer_groups = lambda ws: (asked.append(ws), real(ws))[1]
        with patch.object(tab_layergroups, "ResourceFormDialog", Recording):
            self.dlg._show_layer_group_info(["roads_group", "topp"])
        self.assertEqual(asked, ["topp"])

    def test_the_edit_form_checks_its_rows_behind_the_waiting_box(self):
        """The row check GETs each style. Run on the GUI thread on Save, a
        hung server froze QGIS for the library's timeout."""
        waited = []
        self.dlg._wait_for = lambda fn, **kwargs: (waited.append(kwargs), fn())[1]
        with patch.object(tab_layergroups, "ResourceFormDialog", Recording):
            self.dlg._show_layer_group_info(["eo_group", GLOBAL])
        form = Recording.opened[-1]
        waited.clear()
        form._validate(form.get_values())
        self.assertEqual(len(waited), 1)


class TestCreateLayerGroup(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()

    def posted(self):
        return [call for call in self.dlg.gs.calls if call[0] == "POST"]

    def test_a_create_checks_each_style_once(self):
        """The form's check ran again inside the save, and the save then built
        the publishables, which checks again: three GETs per style."""
        self.dlg._create_layer_group_from_values(
            {
                "name": "g",
                "workspace": GLOBAL,
                "mode": "SINGLE",
                "layers": rows("topp:tasmania_roads = simple_roads"),
            },
            ["topp:tasmania_roads"],
            ["tasmania"],
        )
        checks = [c for c in self.dlg.gs.calls if c[0] == "get_style_definition"]
        self.assertEqual(checks, [("get_style_definition", "simple_roads", None)])
        self.assertEqual(len(self.posted()), 1)

    def test_the_create_form_lists_the_workspaces_once(self):
        asked = []
        real = self.dlg.gs.get_workspaces
        self.dlg.gs.get_workspaces = lambda: (asked.append(1), real())[1]
        with patch.object(tab_layergroups, "ResourceFormDialog", Recording):
            self.dlg._add_layer_group()
        self.assertEqual(asked, [1])

    def test_global_group_payload(self):
        self.dlg._create_layer_group_from_values(
            {
                "name": "new_group",
                "workspace": GLOBAL,
                "mode": "SINGLE",
                "title": "New",
                "abstract": "Why it exists",
                "layers": rows("topp:tasmania_roads\nnurc:mosaic\n"),
            }
        )
        (_verb, path, kwargs) = self.posted()[0]
        self.assertEqual(path, "/rest/layergroups.json")
        group = kwargs["json"]["layerGroup"]
        self.assertEqual(group["name"], "new_group")
        self.assertEqual(group["mode"], "SINGLE")
        self.assertEqual(
            group["publishables"]["published"],
            [
                {"@type": "layer", "name": "topp:tasmania_roads"},
                {"@type": "layer", "name": "nurc:mosaic"},
            ],
        )
        # GeoServer drops an "abstract" key, which is what the library sends.
        self.assertEqual(group["abstractTxt"], "Why it exists")
        self.assertNotIn("abstract", group)
        # Bounds omitted on purpose: GeoServer computes the layers' union, while
        # the library would write a world bbox from its EPSG table.
        self.assertNotIn("bounds", group)
        self.assertNotIn("styles", group)
        self.assertNotIn("workspace", group)

    def test_workspace_group_qualifies_a_bare_layer_name(self):
        self.dlg._create_layer_group_from_values(
            {
                "name": "ws_group",
                "workspace": "topp",
                "mode": "NAMED",
                "layers": rows("tasmania_roads\ntopp:states"),
            }
        )
        (_verb, path, kwargs) = self.posted()[0]
        self.assertEqual(path, "/rest/workspaces/topp/layergroups.json")
        group = kwargs["json"]["layerGroup"]
        self.assertEqual(group["workspace"], {"name": "topp"})
        self.assertEqual(
            [item["name"] for item in group["publishables"]["published"]],
            ["topp:tasmania_roads", "topp:states"],
        )

    def test_a_workspace_group_refuses_another_workspaces_layer(self):
        # GeoServer answers a bare 500 for it, after the form has closed.
        with self.assertRaises(ValueError) as caught:
            self.dlg._create_layer_group_from_values(
                {
                    "name": "ws_group",
                    "workspace": "topp",
                    "mode": "NAMED",
                    "layers": rows("tasmania_roads\nne:coastlines"),
                }
            )
        self.assertIn("ne:coastlines", str(caught.exception))
        self.assertEqual(self.posted(), [])

    def test_a_style_per_layer_is_sent_parallel_to_the_layers(self):
        self.dlg._create_layer_group_from_values(
            {
                "name": "styled",
                "workspace": GLOBAL,
                "mode": "SINGLE",
                "layers": rows(
                    (
                        "topp:tasmania_state_boundaries\n"
                        "topp:tasmania_roads = simple_roads\n"
                        "ne:coastlines = ne:disputed"
                    )
                ),
            }
        )
        group = self.posted()[0][2]["json"]["layerGroup"]
        self.assertEqual(
            [item["name"] for item in group["publishables"]["published"]],
            ["topp:tasmania_state_boundaries", "topp:tasmania_roads", "ne:coastlines"],
        )
        # "" is what GeoServer itself stores for "the layer's own default style"
        self.assertEqual(
            group["styles"]["style"],
            ["", {"name": "simple_roads"}, {"name": "ne:disputed"}],
        )
        # a workspace-qualified style is looked up in its own workspace
        self.assertIn(("get_style_definition", "disputed", "ne"), self.dlg.gs.calls)

    def test_a_style_that_does_not_exist_is_refused_not_dropped(self):
        # GeoServer answers 201 and silently drops an unknown style, which would
        # leave the group rendering with default styles and look like a success.
        with self.assertRaises(ValueError) as caught:
            self.dlg._create_layer_group_from_values(
                {
                    "name": "styled",
                    "workspace": GLOBAL,
                    "mode": "SINGLE",
                    "layers": rows("topp:tasmania_roads = no_such_style"),
                }
            )
        self.assertIn("no_such_style", str(caught.exception))
        self.assertEqual(self.posted(), [])

    def test_parsing_keeps_the_order_and_the_names_as_typed(self):
        layers, styles = self.dlg._parse_group_layers(rows(" b:two = s2 \n\none\n"))
        self.assertEqual(layers, ["b:two", "one"])
        self.assertEqual(styles, ["s2", ""])

    def test_a_bare_pick_is_the_global_group_beside_a_workspace_namesake(self):
        """GeoServer reads a bare name in a workspace group as the global group
        (measured on 2.28.5). The row was qualified first, so the pick went
        out as the workspace's own layer or group of that name."""
        self.dlg._create_layer_group_from_values(
            {
                "name": "ws_group",
                "workspace": "topp",
                "mode": "SINGLE",
                "layers": rows("tasmania\ntopp:roads_group"),
            },
            ["topp:tasmania", "topp:tasmania_roads"],
            ["tasmania", "topp:tasmania", "topp:roads_group"],
        )
        group = self.posted()[0][2]["json"]["layerGroup"]
        self.assertEqual(
            group["publishables"]["published"],
            [
                {"@type": "layerGroup", "name": "tasmania"},
                {"@type": "layerGroup", "name": "topp:roads_group"},
            ],
        )

    def test_a_global_group_holding_another_workspaces_layer_is_refused(self):
        """GeoServer follows the nested groups and answers 500 "can not contain
        resources from other workspace" (measured on 2.28.5), after the form
        closed."""
        details = {
            "outer": {
                "publishables": {"published": {"@type": "layerGroup", "name": "inner"}}
            },
            "inner": {
                "publishables": {"published": {"@type": "layer", "name": "nurc:mosaic"}}
            },
        }
        self.dlg._group_detail = lambda name, ws: details[name]
        with self.assertRaises(ValueError) as caught:
            self.dlg._create_layer_group_from_values(
                {
                    "name": "ws_group",
                    "workspace": "topp",
                    "mode": "SINGLE",
                    "layers": rows("topp:tasmania_roads\nouter"),
                },
                ["topp:tasmania_roads"],
                ["outer", "inner"],
            )
        self.assertIn("'outer' holds 'nurc:mosaic'", str(caught.exception))
        self.assertEqual(self.posted(), [])

    def test_a_global_group_with_another_workspaces_style_or_root_is_refused(self):
        for detail, named in (
            (
                {
                    "publishables": {"published": {"@type": "layer", "name": "topp:a"}},
                    "styles": {"style": {"name": "ne:disputed"}},
                },
                "ne:disputed",
            ),
            (
                {
                    "mode": "EO",
                    "publishables": {"published": {"@type": "layer", "name": "topp:a"}},
                    "rootLayer": {"name": "nurc:mosaic"},
                    "rootLayerStyle": {"name": "raster"},
                },
                "nurc:mosaic",
            ),
        ):
            self.dlg._group_detail = lambda name, ws, detail=detail: detail
            with self.assertRaises(ValueError) as caught:
                self.dlg._group_publishables(rows("g"), "topp", ["topp:a"], ["g"])
            self.assertIn(named, str(caught.exception))

    def test_a_global_group_of_the_workspaces_own_content_is_accepted(self):
        # tasmania holds topp layers only, one of them with a global style.
        published, _styles = self.dlg._group_publishables(
            rows("solo"), "topp", [], ["solo", "tasmania"]
        )
        self.assertEqual(published, [{"@type": "layerGroup", "name": "solo"}])

    def test_the_create_form_refuses_a_mixed_global_group_before_it_closes(self):
        seen = {}

        class Adding(ResourceFormDialog):
            def exec(inner):
                inner.set_values(
                    {"name": "g", "workspace": "topp", "layers": rows("eo_group")}
                )
                inner._on_accept()
                seen["open"] = not inner.result()
                seen["said"] = inner._validation_label.text()
                return QDialog.DialogCode.Rejected

        with patch.object(tab_layergroups, "ResourceFormDialog", Adding):
            self.dlg._add_layer_group()
        self.assertTrue(seen["open"])
        self.assertIn("nurc:mosaic", seen["said"])

    def test_a_workspace_group_refuses_a_root_layer_of_another_workspace(self):
        # GeoServer answers a 500 for it (measured on 2.28.5), after the
        # form closed; the rows had the check, the root did not.
        with self.assertRaises(ValueError) as caught:
            self.dlg._create_layer_group_from_values(
                {
                    "name": "eo",
                    "workspace": "topp",
                    "mode": "EO",
                    "layers": rows("topp:tasmania_roads"),
                    "root_layer": "nurc:mosaic",
                    "root_style": "simple_roads",
                },
                ["topp:tasmania_roads", "nurc:mosaic"],
                [],
            )
        self.assertIn("another workspace", str(caught.exception))
        self.assertEqual(self.posted(), [])

    def test_refuses_an_existing_name(self):
        self.dlg.gs = FakeGS(exists=True)
        with self.assertRaises(ValueError):
            self.dlg._create_layer_group_from_values(
                {
                    "name": "tasmania",
                    "workspace": GLOBAL,
                    "mode": "SINGLE",
                    "layers": rows("topp:tasmania_roads"),
                }
            )
        self.assertEqual(self.posted(), [])

    def test_refuses_an_empty_layer_list(self):
        with self.assertRaises(ValueError):
            self.dlg._create_layer_group_from_values(
                {
                    "name": "empty_group",
                    "workspace": GLOBAL,
                    "mode": "SINGLE",
                    "layers": rows("  \n\n"),
                }
            )
        self.assertEqual(self.posted(), [])

    def test_the_layers_are_picked_ordered_and_styled_in_a_table(self):
        # One layer per line in a text box, "= style" typed by hand, was the
        # form's only way; now a picker adds rows and buttons reorder them.
        dlg = ResourceFormDialog(
            title="t",
            fields=self.dlg._group_fields(
                ["topp"], ["a:one", "b:two"], styles=["line", "simple_roads"]
            ),
        )
        table = dlg.get_widget("layers")
        for name in ("b:two", "b:two", "a:one"):  # the same layer twice is fine
            table.picker.setEditText(name)
            table._add_picked()
        table.table.cellWidget(2, 1).setCurrentText("line")
        table.table.selectRow(2)
        table._up()
        self.assertEqual(
            dlg.get_values()["layers"],
            [["b:two", ""], ["a:one", "line"], ["b:two", ""]],
        )
        style = table.table.cellWidget(0, 1)
        offered = [style.itemText(i) for i in range(style.count())]
        self.assertEqual(offered, ["", "line", "simple_roads"])

    def test_a_name_that_breaks_a_path_is_refused_before_any_request(self):
        with self.assertRaises(ValueError):
            self.dlg._create_layer_group_from_values(
                {
                    "name": "a#b",
                    "workspace": GLOBAL,
                    "mode": "SINGLE",
                    "layers": rows("topp:states"),
                }
            )
        self.assertEqual(self.dlg.gs.calls, [])

    def test_a_layer_the_server_does_not_have_is_named_before_any_request(self):
        with self.assertRaises(ValueError) as caught:
            self.dlg._create_layer_group_from_values(
                {
                    "name": "g",
                    "workspace": "topp",
                    "mode": "SINGLE",
                    "layers": rows("states\nroadz"),
                },
                known_layers=["topp:states"],
            )
        self.assertIn("'topp:roadz'", str(caught.exception))
        self.assertEqual(self.posted(), [])


class TestEditLayerGroup(unittest.TestCase):
    """Measured on 2.28.5: a partial PUT merges, a new layer list needs one
    style per entry, and the bounds are never recomputed on a PUT."""

    LAYERS = ["topp:tasmania_state_boundaries", "topp:tasmania_roads", "nurc:mosaic"]
    GROUPS = ["solo", "tasmania", "topp:roads_group"]

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        self.before = LayerGroupTabMixin._group_form_values(
            TASMANIA, "tasmania", GLOBAL
        )

    CONTEXT = QgsCoordinateTransformContext()

    def save(self, **changes):
        after = dict(self.before, **changes)
        with patch.object(
            LayerGroupTabMixin, "_group_bounds", return_value={"crs": "EPSG:4326"}
        ) as bounds:
            saved = self.dlg._save_layer_group(
                "tasmania",
                None,
                self.before,
                after,
                self.LAYERS,
                self.GROUPS,
                self.CONTEXT,
            )
        puts = [call for call in self.dlg.gs.calls if call[0] == "PUT"]
        return saved, puts, bounds

    def test_a_title_edit_sends_only_the_title(self):
        saved, puts, bounds = self.save(title="Tassie", enabled=False)
        self.assertTrue(saved)
        ((_verb, path, kwargs),) = puts
        self.assertEqual(path, "/rest/layergroups/tasmania.json")
        self.assertEqual(
            kwargs["json"], {"layerGroup": {"title": "Tassie", "enabled": False}}
        )
        bounds.assert_not_called()

    def test_nothing_changed_sends_nothing(self):
        saved, puts, _bounds = self.save(layers=self.before["layers"] + [["", ""]])
        self.assertFalse(saved)
        self.assertEqual(puts, [])

    def test_an_untouched_form_with_crlf_text_sends_nothing(self):
        """The form hands the abstract back stripped and with LF line ends,
        which read as an edit: a PUT, and "saved"."""
        group = dict(TASMANIA, title=" Tasmania ", abstractTxt="One.\r\nTwo.\r\n")
        real = self.dlg.gs.payload_for
        self.dlg.gs.payload_for = lambda path: (
            {"layerGroup": group} if path.endswith("/tasmania.json") else real(path)
        )
        banners = []
        self.dlg.show_success_message = banners.append

        class Saving(ResourceFormDialog):
            def exec(self):
                return QDialog.DialogCode.Accepted

        with patch.object(tab_layergroups, "ResourceFormDialog", Saving):
            self.dlg._show_layer_group_info(["tasmania", GLOBAL])
        self.assertEqual([call for call in self.dlg.gs.calls if call[0] == "PUT"], [])
        self.assertEqual(banners, [])

    def test_a_workspace_edit_picks_the_global_namesake_not_itself(self):
        """In an edit of topp:roads_group, the global roads_group went out as
        topp:roads_group: the group nested in itself."""
        before = LayerGroupTabMixin._group_form_values(
            ROADS_GROUP, "roads_group", "topp"
        )
        self.dlg._group_detail = lambda name, ws: {
            "publishables": {"published": {"@type": "layer", "name": "topp:x"}}
        }
        with patch.object(
            LayerGroupTabMixin, "_group_bounds", return_value={"crs": "EPSG:4326"}
        ):
            self.dlg._save_layer_group(
                "roads_group",
                "topp",
                before,
                dict(before, layers=rows("topp:tasmania_roads\nroads_group")),
                ["topp:tasmania_roads"],
                ["roads_group", "topp:roads_group"],
                self.CONTEXT,
            )
        puts = [call for call in self.dlg.gs.calls if call[0] == "PUT"]
        self.assertEqual(
            puts[0][2]["json"]["layerGroup"]["publishables"]["published"][1],
            {"@type": "layerGroup", "name": "roads_group"},
        )

    def test_an_earth_observation_groups_bounds_hold_its_root_layer(self):
        # GeoServer's own box for a new EO group holds the root layer; the
        # recomputed one cut it off after any edit of the layer list.
        before = LayerGroupTabMixin._group_form_values(EO_GROUP, "eo_group", GLOBAL)
        with patch.object(
            LayerGroupTabMixin, "_group_bounds", return_value={"crs": "EPSG:4326"}
        ) as bounds:
            self.dlg._save_layer_group(
                "eo_group",
                None,
                before,
                dict(before, layers=rows("nurc:mosaic\ntopp:tasmania_roads")),
                self.LAYERS,
                self.GROUPS,
                self.CONTEXT,
            )
        self.assertIn(
            {"@type": "layer", "name": "topp:tasmania_roads"},
            bounds.call_args.args[0][2:],
        )

    def test_a_new_root_layer_alone_recomputes_the_bounds(self):
        # GeoServer keeps the stored box on a PUT, which then left the new
        # root out when only the root changed.
        before = LayerGroupTabMixin._group_form_values(EO_GROUP, "eo_group", GLOBAL)
        with patch.object(
            LayerGroupTabMixin, "_group_bounds", return_value={"crs": "EPSG:4326"}
        ) as bounds:
            self.dlg._save_layer_group(
                "eo_group",
                None,
                before,
                dict(before, root_layer="nurc:mosaic"),
                self.LAYERS,
                self.GROUPS,
                self.CONTEXT,
            )
        puts = [call for call in self.dlg.gs.calls if call[0] == "PUT"]
        group = puts[0][2]["json"]["layerGroup"]
        self.assertEqual(sorted(group), ["bounds", "rootLayer", "rootLayerStyle"])
        self.assertEqual(
            bounds.call_args.args[0],
            [
                {"@type": "layer", "name": "nurc:mosaic"},
                {"@type": "layer", "name": "nurc:mosaic"},
            ],
        )

    def test_new_layers_carry_a_style_each_and_fresh_bounds(self):
        _saved, puts, bounds = self.save(layers=rows("nurc:mosaic\nsolo"))
        group = puts[0][2]["json"]["layerGroup"]
        self.assertEqual(
            group["publishables"]["published"],
            [
                {"@type": "layer", "name": "nurc:mosaic"},
                {"@type": "layerGroup", "name": "solo"},
            ],
        )
        self.assertEqual(group["styles"], {"style": ["", ""]})
        self.assertEqual(group["bounds"], {"crs": "EPSG:4326"})
        bounds.assert_called_once()
        # The project's transform context, read on the GUI thread, travels
        # with the save: the worker must not read the project itself.
        self.assertIs(bounds.call_args.args[1], self.CONTEXT)

    def test_saving_from_the_form_is_a_write_that_reloads(self):
        """The PUT ran under the read helper: a Cancel on the waiting box was
        silent and nothing reloaded, while the request still landed."""
        waited, banners, loads = [], [], []
        self.dlg._wait_for = lambda fn, write=False, stop=None: (
            waited.append(write),
            fn(),
        )[1]
        self.dlg.show_success_message = banners.append
        self.dlg._load_layer_groups = lambda: loads.append(1)

        class Saving(ResourceFormDialog):
            def exec(self):
                self.get_widget("title").setText("Tassie")
                return QDialog.DialogCode.Accepted

        with patch.object(tab_layergroups, "ResourceFormDialog", Saving):
            self.dlg._show_layer_group_info(["tasmania", GLOBAL])
        puts = [call for call in self.dlg.gs.calls if call[0] == "PUT"]
        self.assertEqual(puts[0][2]["json"], {"layerGroup": {"title": "Tassie"}})
        self.assertTrue(waited[-1], "the save is a write: a Cancel says so")
        self.assertEqual(banners, ["Layer group 'tasmania' saved."])
        self.assertEqual(loads, [1])

    def test_a_projected_box_is_reprojected_without_reading_the_project(self):
        """QgsProject.instance() belongs to the GUI thread; the save that
        recomputes the bounds runs in a worker, with the context it was given."""

        class NoProject:
            @staticmethod
            def instance():
                raise AssertionError("the project was read from the worker")

        with patch.object(tab_layergroups, "QgsProject", NoProject):
            rect = LayerGroupTabMixin._box_in(
                {
                    "minx": 589425.9,
                    "maxx": 609518.7,
                    "miny": 4913959.2,
                    "maxy": 4928082.9,
                    # spearfish, as GeoServer stores it: UTM zone 13N
                    "crs": {"@class": "projected", "$": "EPSG:26713"},
                },
                tab_layergroups.QgsCoordinateReferenceSystem("EPSG:4326"),
                self.CONTEXT,
            )
        self.assertAlmostEqual(rect.xMinimum(), -103.87, places=1)
        self.assertAlmostEqual(rect.yMaximum(), 44.5, places=1)

    def test_an_unknown_name_is_refused_since_geoserver_drops_it(self):
        with self.assertRaises(ValueError) as caught:
            self.save(layers=rows("topp:tasmania_roads\nno_such_group"))
        self.assertIn("no_such_group", str(caught.exception))
        self.assertEqual([call for call in self.dlg.gs.calls if call[0] == "PUT"], [])

    def test_turning_into_earth_observation_sends_the_root(self):
        _saved, puts, _bounds = self.save(
            mode="EO",
            root_layer="topp:tasmania_roads",
            root_style="simple_roads",
        )
        group = puts[0][2]["json"]["layerGroup"]
        self.assertEqual(group["mode"], "EO")
        self.assertEqual(
            group["rootLayer"], {"@type": "layer", "name": "topp:tasmania_roads"}
        )
        self.assertEqual(group["rootLayerStyle"], {"name": "simple_roads"})

    def test_earth_observation_needs_a_root_layer(self):
        with self.assertRaises(ValueError):
            self.save(mode="EO", root_layer="(pick a layer)")

    def test_a_zero_box_counts_as_no_box(self):
        # What GeoServer stores after "bounds": null.
        self.assertIsNone(
            LayerGroupTabMixin._box_in(
                {"minx": 0, "maxx": 0, "miny": 0, "maxy": 0},
                tab_layergroups.QgsCoordinateReferenceSystem("EPSG:4326"),
            )
        )


class TestCreateNestedAndEarthObservation(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()

    def create(self, **values):
        base = {"name": "g", "workspace": GLOBAL, "mode": "SINGLE"}
        self.dlg._create_layer_group_from_values(
            dict(base, **values), ["topp:tasmania_roads"], ["tasmania"]
        )
        return [c for c in self.dlg.gs.calls if c[0] == "POST"][0][2]["json"][
            "layerGroup"
        ]

    def test_a_nested_group_is_sent_with_its_type_and_styles(self):
        # Without styles, GeoServer answers 500 for a group holding a group.
        group = self.create(layers=rows("tasmania\ntopp:tasmania_roads"))
        self.assertEqual(
            [item["@type"] for item in group["publishables"]["published"]],
            ["layerGroup", "layer"],
        )
        self.assertEqual(group["styles"], {"style": ["", ""]})

    def test_a_blank_root_style_takes_the_root_layers_default(self):
        with patch.object(
            self.dlg, "_layer_summary", return_value=("VECTOR", "s", "simple_roads")
        ):
            group = self.create(
                mode="EO",
                layers=rows("topp:tasmania_roads"),
                root_layer="topp:tasmania_roads",
                root_style="",
            )
        self.assertEqual(group["rootLayerStyle"], {"name": "simple_roads"})

    def test_a_root_layer_without_a_default_style_asks_for_one(self):
        # A cascaded layer has no default style: "-" is no style name.
        with patch.object(self.dlg, "_layer_summary", return_value=("WMS", "s", "-")):
            with self.assertRaises(ValueError) as caught:
                self.create(
                    mode="EO",
                    layers=rows("topp:tasmania_roads"),
                    root_layer="topp:tasmania_roads",
                    root_style="",
                )
        self.assertIn("no default style", str(caught.exception))


class TestDeleteAndAddToQgis(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        self.dlg._confirm_delete = lambda question, labels=(), cascade="": True
        self.dlg.show_success_message = lambda text: None
        self.dlg.show_error_message = lambda text: self.fail(f"unexpected: {text}")
        self.dlg._load_layer_groups = lambda: None

    def test_a_name_with_a_hash_is_quoted_in_both_scopes(self):
        """ "a#b" went out as ".../layergroups/a", another group's path."""
        self.dlg._delete_selected_layer_groups([["a#b", "topp", "SINGLE", "1"]])
        self.assertIn(("delete_layer_group", "topp", "a%23b"), self.dlg.gs.calls)
        self.assertIn("/layergroups/a%23b.json", self.dlg._group_path("a#b", "topp"))
        self.assertEqual(
            self.dlg._group_path("my group", None), "/rest/layergroups/my%20group.json"
        )
        self.assertEqual(
            self.dlg._group_path("a?b", None), "/rest/layergroups/a%3Fb.json"
        )

    def test_workspace_group_deletes_through_the_library_global_through_rest(self):
        self.dlg._delete_selected_layer_groups(
            [["roads_group", "topp", "CONTAINER", "1"], ["tasmania", GLOBAL, "", "2"]]
        )
        self.assertIn(("delete_layer_group", "topp", "roads_group"), self.dlg.gs.calls)
        self.assertIn(
            ("DELETE", "/rest/layergroups/tasmania.json", {}), self.dlg.gs.calls
        )

    def test_add_to_qgis_uses_the_groups_own_service_and_wms(self):
        built = []

        class FakeLayer:
            def __init__(inner, uri, name, provider):
                built.append((uri, name, provider))

            def isValid(inner):
                return True

        self.dlg.plg_settings = FakePrefs(credentials=("", ""), auth_cfg_id="abc123")
        with (
            patch.object(tab_layers, "QgsRasterLayer", FakeLayer),
            patch.object(tab_layergroups.QgsProject, "instance") as instance,
        ):
            self.dlg._add_group_to_qgis(["tasmania", GLOBAL, "SINGLE", "2"])
            self.dlg._add_group_to_qgis(["roads_group", "topp", "CONTAINER", "1"])
        self.assertEqual(instance.call_count, 2)
        self.assertIn("layers=tasmania&", built[0][0])  # global: the bare name
        self.assertIn("url=http://gs.example.org/geoserver/ows&", built[0][0])
        # a workspace group: bare too, on its workspace's own service
        self.assertIn("layers=roads_group&", built[1][0])
        self.assertIn("url=http://gs.example.org/geoserver/topp/ows&", built[1][0])
        self.assertIn("authcfg=abc123", built[0][0])
        self.assertEqual([provider for _uri, _name, provider in built], ["wms", "wms"])


class TestPreviewInBrowser(unittest.TestCase):
    """The group's own bounds, global groups without a prefix."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        self.dlg.plg_settings = FakePrefs("http://gs/geoserver", credentials=("", ""))
        self.dlg.show_error_message = lambda text: self.fail(f"unexpected: {text}")

    def opened(self, row):
        urls = []
        with patch.object(
            tab_layers.QDesktopServices,
            "openUrl",
            lambda url: urls.append(url.toString()) or True,
        ):
            self.dlg._preview_group_in_browser(row)
        return urls

    def test_a_group_previews_on_its_own_service_and_bounds(self):
        self.dlg._group_detail = lambda name, ws: {
            "bounds": {
                "minx": 143.0,
                "miny": -44.0,
                "maxx": 149.0,
                "maxy": -40.0,
                "crs": "EPSG:4326",
            }
        }
        (url,) = self.opened(["tasmania", GLOBAL, "SINGLE", "2"])
        self.assertTrue(url.startswith("http://gs/geoserver/wms?"), url)  # no prefix
        self.assertIn("layers=tasmania&", url)
        self.assertIn("bbox=143.0,-44.0,149.0,-40.0", url)
        (url,) = self.opened(["roads_group", "topp", "CONTAINER", "1"])
        self.assertTrue(url.startswith("http://gs/geoserver/topp/wms?"), url)
        self.assertIn("layers=topp:roads_group", url)

    def test_the_action_is_offered_and_explains_the_login(self):
        self.dlg.show_warning_message = lambda t: None
        self.dlg._load_layer_groups()
        labels = [action[1] for action in self.dlg._row_actions]
        self.assertEqual(
            labels, ["Preview", "Add to QGIS", "Preview in a browser", "Delete"]
        )
        self.assertIn("log in", self.dlg._row_actions[2][3])
        # Every action says what it does, as on the Layers tab.
        self.assertTrue(all(len(action) > 3 for action in self.dlg._row_actions))

    def test_the_in_qgis_preview_opens_on_the_wms_layers_own_extent(self):
        """The WMS layer reads the group's lon/lat extent from the capabilities
        (measured on 2.28.5, spearfish stored in EPSG:26713 included): the
        group GET and its reprojection were a second read of the same."""
        opened = []

        class Layer:
            def isValid(self):
                return True

            def extent(self):
                return QgsRectangle(-103.88, 44.37, -103.62, 44.50)

        def no_read(name, ws):
            raise AssertionError("the group was read again")

        self.dlg._group_detail = no_read
        with (
            patch.object(
                tab_layergroups,
                "LayerPreviewDialog",
                lambda name, layer, bbox, parent: opened.append((name, bbox))
                or type("D", (), {"show": lambda inner: None})(),
            ),
            patch.object(tab_layers, "QgsRasterLayer", lambda *args: Layer()),
        ):
            self.dlg._preview_group(["spearfish", GLOBAL])
        ((name, bbox),) = opened
        self.assertEqual(name, "spearfish")
        self.assertEqual(bbox, (-103.88, 44.37, -103.62, 44.50))


class TestAGlobalListingRefused(unittest.TestCase):
    """A rest.properties rule may refuse GET /rest/layergroups.json alone:
    "Failed to load layer groups: HTTP 403", and no row at all."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        listed = self.dlg.gs.payload_for

        def refusing(path):
            if path == "/rest/layergroups.json":
                raise RuntimeError("HTTP 403: Forbidden")
            return listed(path)

        self.dlg.gs.payload_for = refusing

    def test_the_workspaces_groups_are_listed_and_the_global_scope_reported(self):
        warnings = []
        self.dlg.show_warning_message = warnings.append
        self.dlg.show_error_message = lambda text: self.fail(f"unexpected: {text}")
        self.dlg._load_layer_groups()
        self.assertEqual(
            [row[:2] for row in self.dlg._all_rows], [["roads_group", "topp"]]
        )
        (warning,) = warnings
        self.assertIn("(global)", warning)

    def test_a_form_still_needs_the_global_groups(self):
        # Without them, a form refused each global group it holds, and Used
        # by left them out.
        with self.assertRaises(RuntimeError):
            self.dlg._all_group_names()


class TestAGroupDeletedMeanwhile(unittest.TestCase):
    """Measured on 2.28.5: a PUT or a DELETE of a group that is gone is a
    bare 500 (a NullPointerException), shown as it was."""

    class Failed:
        status_code = 500
        text = 'java.lang.NullPointerException: Cannot invoke "Object.getClass()"'
        history = ()

    class Answer:
        history = ()

        def __init__(self, status_code):
            self.status_code = status_code
            self.text = ""

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        client = self.dlg.gs.rest_service.rest_client
        client.put = client.delete = lambda path, **kwargs: self.Failed()
        self.reread = 404  # what the re-read of the group answers
        self.reread_paths = []

        def get(path, **kwargs):
            self.reread_paths.append(path)
            return self.Answer(self.reread)

        client.get = get

    def test_a_save_says_the_group_is_gone(self):
        before = LayerGroupTabMixin._group_form_values(TASMANIA, "tasmania", GLOBAL)
        after = dict(before, title="Tassie")
        with self.assertRaises(ValueError) as raised:
            self.dlg._save_layer_group(
                "tasmania", None, before, after, [], [], QgsCoordinateTransformContext()
            )
        self.assertIn("deleted on the server", str(raised.exception))
        self.assertEqual(self.reread_paths, ["/rest/layergroups/tasmania.json"])

    def test_a_delete_says_so_in_both_scopes(self):
        def refused(*args):
            raise RuntimeError("HTTP 500")  # as the library's raise_for_status

        self.dlg.gs.delete_layer_group = refused
        for scope_name in ("topp", None):
            with self.assertRaises(ValueError) as raised:
                self.dlg._do_delete_group("roads_group", scope_name)
            self.assertIn("deleted on the server", str(raised.exception))

    def test_a_group_still_there_keeps_geoservers_answer(self):
        self.reread = 200
        with self.assertRaises(RuntimeError) as raised:
            self.dlg._do_delete_group("tasmania", None)
        self.assertIn("NullPointerException", str(raised.exception))

    def test_a_server_that_fails_the_reread_too_keeps_its_own_error(self):
        # Only a 404 means gone: resource_exists() said False for a 503 too.
        self.Failed.status_code = 503
        self.addCleanup(setattr, self.Failed, "status_code", 500)
        self.reread = 503
        before = LayerGroupTabMixin._group_form_values(TASMANIA, "tasmania", GLOBAL)
        after = dict(before, title="Tassie")
        with self.assertRaises(RuntimeError) as raised:
            self.dlg._save_layer_group(
                "tasmania", None, before, after, [], [], QgsCoordinateTransformContext()
            )
        self.assertIn("HTTP 503", str(raised.exception))
        with self.assertRaises(RuntimeError) as raised:
            self.dlg._do_delete_group("tasmania", None)
        self.assertNotIn("deleted on the server", str(raised.exception))


# ############################################################################
# ####### Stand-alone run ########
# ################################
if __name__ == "__main__":
    unittest.main()
