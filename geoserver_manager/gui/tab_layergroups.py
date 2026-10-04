#! python3  # noqa: E265

"""
Layer Groups tab: list, create, edit, delete layer groups.

Used as a mixin for GeoServerMainDialog. `_layer_uri()` comes from LayerTabMixin
through the shared dialog class; the global-or-workspace scope is `gui.scope`.
"""

from urllib.parse import quote

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsProject,
    QgsRectangle,
)
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtWidgets import QDialog

from geoserver_manager.gui.dlg_preview import LayerPreviewDialog
from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog
from geoserver_manager.gui.scope import GLOBAL, PENDING, global_label, scope
from geoserver_manager.toolbelt.payload import bbox_text, text_of, unwrap

# GeoServer's LayerGroupInfo.Mode enum. Spelled out rather than imported from
# geoservercloud.models: the bundled wheels only reach sys.path once the plugin
# has run ensure_dependencies(). test_tab_layergroups asserts it still matches
# LayerGroup.modes.
MODES = ("SINGLE", "OPAQUE_CONTAINER", "NAMED", "CONTAINER", "EO")

# The collection of each layer type's resources, reachable by workspace alone.
_RESOURCE_COLLECTIONS = {
    "VECTOR": "featuretypes",
    "RASTER": "coverages",
    "WMS": "wmslayers",
    "WMTS": "wmtslayers",
}


# Every user-visible string in this file goes through translate() with this
# file's own class as the context. self.tr() cannot: pylupdate extracts it
# under LayerGroupTabMixin, but at runtime self.tr is QObject.tr with the context of the
# *instance's* class, GeoServerMainDialog. QDialog precedes the mixins in the
# MRO, so every lookup would miss. A wrapper function would not be extracted
# at all (pylupdate only understands a literal context), hence the repetition.
translate = QCoreApplication.translate


def _mode_label(mode):
    """GeoServer's web-admin words for a LayerGroupInfo.Mode; the enum when unknown."""
    labels = {
        "SINGLE": translate("LayerGroupTabMixin", "Single"),
        "OPAQUE_CONTAINER": translate("LayerGroupTabMixin", "Opaque Container"),
        "NAMED": translate("LayerGroupTabMixin", "Named Tree"),
        "CONTAINER": translate("LayerGroupTabMixin", "Container Tree"),
        "EO": translate("LayerGroupTabMixin", "Earth Observation Tree"),
    }
    return labels.get(mode, mode)


class LayerGroupTabMixin:
    """Mixin that adds layer-group methods to the main dialog."""

    # -- Listing ---------------------------------------------------------------

    def _load_layer_groups(self):
        """Arm the Layer Groups tab, then fetch its rows in the background."""
        self._setup_add_button(
            translate("LayerGroupTabMixin", "Create a Layer Group"),
            translate("LayerGroupTabMixin", "Publish several layers as one"),
            self._add_layer_group,
        )
        self._setup_delete_selected_button(self._delete_selected_layer_groups)
        self._name_click_callback = self._show_layer_group_info
        self._extra_click_callbacks = {
            translate("LayerGroupTabMixin", "Workspace"): self._open_workspace_from_row
        }
        self._row_actions = [
            (
                "preview-map",
                translate("LayerGroupTabMixin", "Preview"),
                self._preview_group,
                translate(
                    "LayerGroupTabMixin",
                    "The group on a map of its own, with feature info on a click; "
                    "the project is not touched",
                ),
            ),
            (
                "add-to-qgis",
                translate("LayerGroupTabMixin", "Add to QGIS"),
                self._add_group_to_qgis,
                translate(
                    "LayerGroupTabMixin",
                    "Add the group to this QGIS project as one WMS layer",
                ),
            ),
            (
                "preview-browser",
                translate("LayerGroupTabMixin", "Preview in a browser"),
                self._preview_group_in_browser,
                translate(
                    "LayerGroupTabMixin",
                    "Preview in a browser: GeoServer's own OpenLayers page. A "
                    "secured server will ask the browser to log in.",
                ),
            ),
            (
                "delete",
                translate("LayerGroupTabMixin", "Delete"),
                self._delete_layer_group,
                translate(
                    "LayerGroupTabMixin",
                    "Delete: remove the group; its layers stay (asks first).",
                ),
            ),
        ]
        self._setup_table(
            [
                translate("LayerGroupTabMixin", "Name"),
                translate("LayerGroupTabMixin", "Workspace"),
                translate("LayerGroupTabMixin", "Mode"),
                translate("LayerGroupTabMixin", "Layers"),
                self.actions_column_label(),
            ]
        )
        self._row_detail = lambda row: tuple(
            str(cell) for cell in self._group_summary(row[0], row[1])
        )
        self._detail_columns = (2, 3)
        # The row keeps GeoServer's enum (SINGLE, EO), the cell says it in
        # words: the translated label was stored and read back.
        self._cell_display = {2: _mode_label}
        self._start_load(
            translate("LayerGroupTabMixin", "Failed to load layer groups"),
            self._fetch_layer_group_rows,
        )

    def _fetch_layer_group_rows(self, task=None):
        """(rows, failures) for the Layer Groups table. Runs in a worker thread."""
        groups, failures = self._group_names(self._get_workspace_names(), task)
        # Mode and size are only in the group itself: one GET per group, for
        # the page shown. A group that cannot be read keeps its row.
        rows = [[name, ws_label, PENDING, PENDING] for name, ws_label in groups]
        return rows, failures

    def _group_names(self, workspace_names, task=None, list_global=None):
        """The global groups, then these workspaces', as (name, workspace label).

        One listing per workspace, in parallel; a listing that fails, the
        global one included, is a failure beside the names, not the end of
        the listing. `list_global` lists the global groups, by default
        _global_group_names. Runs in a worker thread.
        """
        return self._scoped_names(
            list_global or self._global_group_names,
            # The library interpolates the name into the path as it is, so
            # "a#b" would list "a": hand it the quoted segment.
            lambda ws: self._fetch_list(self.gs.get_layer_groups, quote(ws, safe="")),
            workspace_names,
            task,
        )

    def _global_group_names(self):
        """Names of the layer groups that live outside any workspace.

        TODO(#1): upstream: every layer-group call in the library takes a
        workspace_name, so groups in the global scope cannot be reached at all
        (GeoServer's own demo data has three). Workaround: GET the collection.
        """
        payload = self._raw_rest("get", self._groups_path(None)).json()
        return sorted(
            self._name_of(group)
            for group in self._unwrap(payload, "layerGroups", "layerGroup")
        )

    def _group_summary(self, name, workspace_label):
        """(mode, number of layers) for the list view. Raises on HTTP errors."""
        detail = self._group_detail(name, scope(workspace_label))
        return detail.get("mode", ""), len(self._group_layers(detail))

    # -- One group -------------------------------------------------------------

    def _group_detail(self, name, workspace_name):
        """One layer group, as GeoServer stores it.

        TODO(#1): upstream: get_layer_group() exists, but its model drops the
        abstract (GeoServer writes "abstractTxt" while the model reads
        "abstract") and the "@type" that tells a nested group from a layer, and
        it has no global scope. Workaround: GET the layer-group path.
        """
        payload = self._raw_rest("get", self._group_path(name, workspace_name)).json()
        return payload.get("layerGroup") or {}

    def _groups_path(self, workspace_name):
        """REST path of the layer-group collection, global or in a workspace."""
        endpoints = self.gs.rest_service.rest_endpoints
        if workspace_name:
            return endpoints.layergroups(workspace_name)
        return f"{endpoints.base_url}/layergroups.json"

    def _group_path(self, name, workspace_name):
        """REST path of one layer group, global or in a workspace."""
        endpoints = self.gs.rest_service.rest_endpoints
        # quote(): a "/", "?" or "#" in a name would otherwise change the path
        if workspace_name:
            return endpoints.layergroup(
                quote(workspace_name, safe=""), quote(name, safe="")
            )
        return f"{endpoints.base_url}/layergroups/{quote(name, safe='')}.json"

    @classmethod
    def _group_layers(cls, detail):
        """The group's publishables in drawing order, as (name, @type) pairs."""
        return [
            (item.get("name", ""), item.get("@type", "layer"))
            for item in unwrap(detail, "publishables", "published")
            if isinstance(item, dict)
        ]

    @classmethod
    def _group_styles(cls, detail):
        """The style of each publishable; "" where the layer's default is used."""
        styles = unwrap(detail, "styles", "style")
        return [
            style.get("name", "") if isinstance(style, dict) else (style or "")
            for style in styles
        ]

    # -- View and edit ---------------------------------------------------------

    @classmethod
    def _group_form_values(cls, detail, name, workspace_label):
        """Prefill for the edit dialog: the layers as [name, style] rows. Pure."""
        styles = cls._group_styles(detail)
        rows = [
            [layer_name, styles[index] if index < len(styles) else ""]
            for index, (layer_name, _kind) in enumerate(cls._group_layers(detail))
        ]

        return {
            "name": name,
            # Shown, never read back: the save keeps the row's own scope.
            "workspace": (
                global_label() if workspace_label == GLOBAL else workspace_label
            ),
            "mode": detail.get("mode", ""),
            "title": text_of(detail.get("internationalTitle") or detail.get("title")),
            # GeoServer stores the abstract under "abstractTxt".
            "abstract": text_of(
                detail.get("internationalAbstract") or detail.get("abstractTxt")
            ),
            # A group GeoServer has never re-saved has no flags: both default on.
            "enabled": detail.get("enabled", True) is not False,
            "advertised": detail.get("advertised", True) is not False,
            "layers": rows,
            "root_layer": (detail.get("rootLayer") or {}).get("name", ""),
            "root_style": (detail.get("rootLayerStyle") or {}).get("name", ""),
            "bounds": bbox_text(detail.get("bounds")),
        }

    def _show_layer_group_info(self, row_data):
        """Open a layer group to edit it."""
        name, workspace_label = row_data[0], row_data[1]
        workspace_name = scope(workspace_label)
        fetched = self._fetch(
            lambda: (
                self._group_detail(name, workspace_name),
                self._all_layer_names(),
                # A workspace group holds its own workspace's groups only, and
                # the global ones: no need to list every other workspace.
                self._all_group_names(
                    [workspace_name] if workspace_name else self._get_workspace_names()
                ),
                self._style_choices(workspace_name),
            ),
            translate("LayerGroupTabMixin", "Failed to load layer group '{}'").format(
                name
            ),
        )
        if fetched is None:
            return
        detail, layer_names, group_names, style_names = fetched
        before = self._group_form_values(detail, name, workspace_label)
        own = f"{workspace_name}:{name}" if workspace_name else name

        dlg = ResourceFormDialog(
            title=translate("LayerGroupTabMixin", "Layer Group '{}'").format(name),
            description=translate(
                "LayerGroupTabMixin",
                "GeoServer cannot rename a layer group. When the layers change, "
                "the plugin recomputes the group's bounds.",
            ),
            fields=self._group_fields(
                [workspace_label],
                self._same_workspace(
                    workspace_name,
                    layer_names + [group for group in group_names if group != own],
                ),
                self._same_workspace(workspace_name, layer_names),
                edit_mode=True,
                styles=style_names,
            ),
            values=before,
            parent=self,
            ok_label=translate("LayerGroupTabMixin", "Save"),
            validate=self._form_check(
                lambda after: self._check_group_rows(
                    after, workspace_name, layer_names, group_names
                )
            ),
        )
        self._wire_group_form(dlg)
        for key, international in (
            ("title", "internationalTitle"),
            ("abstract", "internationalAbstract"),
        ):
            if detail.get(international):
                # Shown as "en: …; fr: …", which a save would store as the
                # plain title, in every language. Kept read-only instead.
                widget = dlg.get_widget(key)
                widget.setReadOnly(True)
                widget.setToolTip(
                    translate(
                        "LayerGroupTabMixin",
                        "Translated in several languages: edit it in GeoServer's "
                        "web interface.",
                    )
                )
        if detail.get("mode") == "EO":
            # GeoServer refuses every way of clearing the root layer (measured
            # on 2.28.5), so an Earth Observation group cannot change mode.
            dlg.get_widget("mode").setEnabled(False)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        after = dlg.get_values()
        # Read here: the save runs in a worker, and the project is the GUI's.
        context = QgsProject.instance().transformContext()
        saved = []
        if (
            self._run_action(
                lambda: saved.append(
                    self._wait_for_save(
                        lambda: self._save_layer_group(
                            name,
                            workspace_name,
                            before,
                            after,
                            layer_names,
                            group_names,
                            context,
                        )
                    )
                ),
                translate(
                    "LayerGroupTabMixin", "Failed to update layer group '{}'"
                ).format(name),
            )
            and saved[0]
        ):
            self.show_success_message(
                translate("LayerGroupTabMixin", "Layer group '{}' saved.").format(name)
            )
            self._load_layer_groups()

    @staticmethod
    def _group_changes(before, after):
        """(body, layers changed) for a partial PUT of the edited fields. Pure.

        The layer list is only compared here: it needs the server's names to
        become publishables, see _save_layer_group.
        """
        body = {}
        for key, target in (
            ("title", "title"),
            ("abstract", "abstractTxt"),
            ("enabled", "enabled"),
            ("advertised", "advertised"),
        ):
            old = before.get(key)
            if isinstance(old, str):
                # As the form hands text back: stripped, with LF line ends.
                old = old.replace("\r\n", "\n").replace("\r", "\n").strip()
            if after.get(key) != old:
                body[target] = after.get(key)
        if after["mode"] != before["mode"]:
            body["mode"] = after["mode"]
        before_layers = LayerGroupTabMixin._parse_group_layers(before["layers"])
        after_layers = LayerGroupTabMixin._parse_group_layers(after["layers"])
        return body, before_layers != after_layers

    def _save_layer_group(
        self, name, workspace_name, before, after, known_layers, known_groups, context
    ):
        """PUT what changed. False when nothing did. Runs in a worker thread.

        :param context: the project's transform context, read on the GUI
            thread, for the bounds.

        TODO(#1): no update_layer_group() in the library (row 57). GeoServer
        merges a partial PUT, but a new layer list needs a style per entry,
        and it keeps the old bounds, so they are recomputed here.
        """
        body, layers_changed = self._group_changes(before, after)
        if after["mode"] == "EO" and (
            layers_changed
            or "mode" in body
            or (after["root_layer"], after["root_style"])
            != (before["root_layer"], before["root_style"])
        ):
            body.update(self._eo_root(after, known_layers, workspace_name))
        if layers_changed or "rootLayer" in body:
            published, styles = self._group_publishables(
                after["layers"], workspace_name, known_layers, known_groups
            )
            if layers_changed:
                body["publishables"] = {"published": published}
                # A new list with fewer styles than entries is refused.
                body["styles"] = {"style": [{"name": s} if s else "" for s in styles]}
            # GeoServer's own box for a new EO group holds its root layer too.
            root = [body["rootLayer"]] if "rootLayer" in body else []
            body["bounds"] = self._group_bounds(published + root, context)
        if not body:
            return False
        try:
            self._raw_rest(
                "put", self._group_path(name, workspace_name), json={"layerGroup": body}
            )
        except Exception:
            self._refuse_gone_group(name, workspace_name)
            raise
        return True

    def _refuse_gone_group(self, name, workspace_name):
        """After a failed save or delete: raise when the group is no longer
        there. GeoServer answers both with a bare 500 then (a
        NullPointerException, measured on 2.28.5). Only a 404 means gone:
        resource_exists() says False for a 503 or a 401 as well, which the
        request's own error describes better."""
        try:
            # TODO(#1): no get for a global group in the library (row 16).
            status = self._raw_rest(
                "get", self._group_path(name, workspace_name), accept=(404,)
            ).status_code
        except Exception:  # the request's own error says more
            return
        if status == 404:
            raise ValueError(
                translate(
                    "LayerGroupTabMixin",
                    "it was deleted on the server after the list was loaded. "
                    "Press Refresh (F5).",
                )
            )

    @staticmethod
    def _same_workspace(workspace_name, names):
        """The names the picker offers a group in this workspace: all, for a
        global one.

        A workspace group holds its own workspace's layers and groups, and the
        global groups (the bare names) that hold nothing of another workspace,
        which the form check reads (measured on 2.28.5).
        """
        if not workspace_name:
            return list(names)
        return [
            name
            for name in names
            if ":" not in name or name.startswith(f"{workspace_name}:")
        ]

    def _group_publishables(self, rows, workspace_name, known_layers, known_groups):
        """The layer list as GeoServer publishables, and the styles beside it.

        GeoServer drops a name it does not know, answering 200, so every row
        is checked here. A bare name is the global group of that name, as
        GeoServer reads it, even beside a workspace namesake; else it takes
        the group's workspace. A qualified name is a layer first, then a group.
        A group named like a layer cannot be listed; GeoServer allows
        it, add a kind column to the table when someone needs it.
        """
        layers, styles = self._parse_group_layers(rows)
        if not layers:
            raise ValueError(
                translate("LayerGroupTabMixin", "List at least one layer.")
            )
        published = []
        for layer in layers:
            if ":" not in layer and layer in known_groups:
                foreign = workspace_name and self._foreign_member(layer, workspace_name)
                if foreign:
                    raise ValueError(
                        translate(
                            "LayerGroupTabMixin",
                            "'{}' holds '{}' of another workspace. A group in '{}' "
                            "may hold a global group only when everything in it, "
                            "nested groups and styles included, is in that "
                            "workspace.",
                        ).format(layer, foreign, workspace_name)
                    )
                published.append({"@type": "layerGroup", "name": layer})
                continue
            if ":" not in layer and workspace_name:
                layer = f"{workspace_name}:{layer}"
            if workspace_name and not layer.startswith(f"{workspace_name}:"):
                # GeoServer answers a bare 500 for it, after the form closed.
                raise self._in_another_workspace(layer, workspace_name)
            if known_layers is None or layer in known_layers:
                published.append({"@type": "layer", "name": layer})
            elif layer in known_groups:
                published.append({"@type": "layerGroup", "name": layer})
            else:
                raise ValueError(
                    translate(
                        "LayerGroupTabMixin",
                        "No layer or group named '{}' on the server. Pick it from "
                        "the list, or qualify it as workspace:layer.",
                    ).format(layer)
                )
        self._check_styles_exist(styles)
        return published, styles

    @staticmethod
    def _in_another_workspace(name, workspace_name):
        """The refusal of a member GeoServer answers a bare 500 for."""
        return ValueError(
            translate(
                "LayerGroupTabMixin",
                "'{}' is in another workspace. A group in '{}' holds only that "
                "workspace's layers and groups, and global groups that hold "
                "nothing of another workspace.",
            ).format(name, workspace_name)
        )

    def _foreign_member(self, group, workspace_name):
        """What a global group holds of another workspace, or None.

        GeoServer follows the nested groups, their styles and an Earth
        Observation root, and refuses any of another workspace in a group of
        this one (a 500, measured on 2.28.5). One GET per global group.
        """
        detail = self._group_detail(group, None)
        members = self._group_layers(detail)
        names = [name for name, _kind in members] + self._group_styles(detail)
        names += [
            (detail.get(key) or {}).get("name") or ""
            for key in ("rootLayer", "rootLayerStyle")
        ]
        for name in names:
            if ":" in name and not name.startswith(f"{workspace_name}:"):
                return name
        for name, kind in members:
            if kind == "layerGroup" and ":" not in name:
                found = self._foreign_member(name, workspace_name)
                if found:
                    return found
        return None

    def _eo_root(self, values, known_layers, workspace_name=None):
        """rootLayer and rootLayerStyle of an Earth Observation group.

        GeoServer requires both. A blank style takes the root layer's default.
        A group in a workspace needs a root of that workspace (a 500 else).
        """
        root = (values.get("root_layer") or "").strip()
        if not root:
            raise ValueError(
                translate(
                    "LayerGroupTabMixin",
                    "An Earth Observation group needs a root layer.",
                )
            )
        if workspace_name and ":" in root and not root.startswith(f"{workspace_name}:"):
            raise self._in_another_workspace(root, workspace_name)
        if known_layers is not None and root not in known_layers:
            raise ValueError(
                translate(
                    "LayerGroupTabMixin", "No layer named '{}' on the server."
                ).format(root)
            )
        style = (values.get("root_style") or "").strip() or self._layer_summary(root)[2]
        if style == "-":  # _layer_summary's "none", a cascaded layer's case
            raise ValueError(
                translate(
                    "LayerGroupTabMixin",
                    "Root layer '{}' has no default style: type one.",
                ).format(root)
            )
        self._check_styles_exist([style])
        return {
            "rootLayer": {"@type": "layer", "name": root},
            "rootLayerStyle": {"name": style},
        }

    def _group_bounds(self, published, context):
        """The union of the publishables' extents, in EPSG:4326.

        GeoServer computes it on a create but never on a PUT (a new layer list
        keeps the old box, and "bounds": null stores a zero one). Layers give
        their lon/lat box; a nested group gives its own, in any CRS, which
        `context`, the project's transform context, reprojects. Runs in a
        worker thread, which is why the context comes from the caller.
        """
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")

        def box_of(item):
            if item["@type"] == "layerGroup":
                workspace_name, _, bare = item["name"].rpartition(":")
                return self._group_detail(bare, workspace_name or None).get("bounds")
            return self._layer_lonlat_box(item["name"])

        total = None
        # Two GETs a member: in parallel, as the page fills are, instead of
        # one round trip after another on every save that changed the layers.
        for box, error in self._fan_out(box_of, published):
            if error is not None:
                raise error
            rect = self._box_in(box or {}, wgs84, context)
            if rect is None:
                continue
            if total is None:
                total = rect
            else:
                total.combineExtentWith(rect)
        if total is None:
            raise ValueError(
                translate(
                    "LayerGroupTabMixin",
                    "None of the layers has bounds, so the group would have none.",
                )
            )
        return {
            "minx": total.xMinimum(),
            "miny": total.yMinimum(),
            "maxx": total.xMaximum(),
            "maxy": total.yMaximum(),
            "crs": "EPSG:4326",
        }

    def _layer_lonlat_box(self, qualified_name):
        """A layer's latLonBoundingBox, read from its resource by workspace.

        TODO(#1): the library's Layer keeps only its resource's name, and
        nothing reads a resource by workspace alone (rows 39 and 57).
        """
        layer = self._raw_rest("get", self._layers_url(qualified_name)).json()
        layer = layer.get("layer") or {}
        collection = _RESOURCE_COLLECTIONS.get(layer.get("type"), "featuretypes")
        workspace_name, _, _ = qualified_name.rpartition(":")
        resource = (layer.get("resource") or {}).get("name") or qualified_name
        path = "{}/workspaces/{}/{}/{}.json".format(
            self.gs.rest_service.rest_endpoints.base_url,
            quote(workspace_name, safe=""),
            collection,
            quote(resource.rpartition(":")[2], safe=""),
        )
        payload = self._raw_rest("get", path).json()
        body = next(iter(payload.values()), {}) if isinstance(payload, dict) else {}
        return body.get("latLonBoundingBox")

    @staticmethod
    def _box_in(box, target, context=None):
        """A GeoServer bbox as a QgsRectangle in the target CRS; None if empty.

        Never reads the project itself: this runs in a worker for a save, and
        the project belongs to the GUI thread. The caller passes its
        transform context; without one, the transform has no overrides.
        """
        try:
            rect = QgsRectangle(
                float(box["minx"]),
                float(box["miny"]),
                float(box["maxx"]),
                float(box["maxy"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if rect.isEmpty():
            return None
        crs = box.get("crs")
        crs = crs.get("$") if isinstance(crs, dict) else crs
        source = QgsCoordinateReferenceSystem(crs or "EPSG:4326")
        if not source.isValid() or source == target:
            return rect
        return QgsCoordinateTransform(
            source, target, context or QgsCoordinateTransformContext()
        ).transformBoundingBox(rect)

    # -- Create ----------------------------------------------------------------

    def _group_fields(
        self, workspace_names, pickable, root_layers=None, edit_mode=False, styles=()
    ):
        """Field definitions for the create and the edit dialog.

        :param pickable: what the picker offers: layers, then groups.
        :param root_layers: the layers an Earth Observation root can be;
            what the picker offers when not given.
        """
        fields = [
            {
                "key": "name",
                "label": translate("LayerGroupTabMixin", "Name"),
                "type": "text",
                "required": not edit_mode,
                "read_only": edit_mode,
            },
            {
                "key": "workspace",
                "label": translate("LayerGroupTabMixin", "Workspace"),
                "type": "text" if edit_mode else "combo",
                "read_only": edit_mode,
                "options": [(global_label(), GLOBAL)] + list(workspace_names),
                "help": translate(
                    "LayerGroupTabMixin",
                    "A global group can mix layers from several workspaces",
                ),
            },
            {
                "key": "mode",
                "label": translate("LayerGroupTabMixin", "Mode"),
                "type": "combo",
                "options": [(_mode_label(mode), mode) for mode in MODES],
                "default": "SINGLE",
                "help": translate(
                    "LayerGroupTabMixin",
                    "Single publishes the group as one layer; Opaque Container is "
                    "the same but hides its layers from the capabilities; Named "
                    "Tree also keeps the layers addressable on their own; Container "
                    "Tree and Earth Observation Tree only group them",
                ),
            },
            {
                "key": "title",
                "label": translate("LayerGroupTabMixin", "Title"),
                "type": "text",
            },
            {
                "key": "abstract",
                "label": translate("LayerGroupTabMixin", "Abstract"),
                "type": "textarea",
            },
            {
                "key": "layers",
                "label": translate("LayerGroupTabMixin", "Layers"),
                "type": "table",
                "ordered": True,
                "required": True,
                "wide": True,
                "group": translate("LayerGroupTabMixin", "Layers"),
                "choices": list(pickable),
                "columns": [
                    {"label": translate("LayerGroupTabMixin", "Layer or group")},
                    {
                        "label": translate("LayerGroupTabMixin", "Style"),
                        "type": "combo",
                        # Blank: the layer's own default style.
                        "options": [""] + list(styles),
                        "placeholder": translate("LayerGroupTabMixin", "(default)"),
                    },
                ],
                "help": translate(
                    "LayerGroupTabMixin",
                    "In drawing order: the first row is drawn first, at the "
                    "bottom. A blank style is the layer's own default.",
                ),
            },
            {
                "key": "root_layer",
                "label": translate("LayerGroupTabMixin", "Root layer"),
                "type": "combo",
                # No value: Save then says a root layer is needed.
                "options": [(translate("LayerGroupTabMixin", "(pick a layer)"), "")]
                + list(pickable if root_layers is None else root_layers),
                "visible": False,
                "group": translate("LayerGroupTabMixin", "Layers"),
                "help": translate(
                    "LayerGroupTabMixin",
                    "What an Earth Observation group draws when it is requested "
                    "as a whole",
                ),
            },
            {
                "key": "root_style",
                "label": translate("LayerGroupTabMixin", "Root layer style"),
                "type": "text",
                "visible": False,
                "group": translate("LayerGroupTabMixin", "Layers"),
                "placeholder": translate("LayerGroupTabMixin", "Its default style"),
            },
        ]
        if edit_mode:
            fields[3:3] = [
                {
                    "key": "enabled",
                    "label": translate("LayerGroupTabMixin", "Enabled"),
                    "type": "checkbox",
                    "default": True,
                },
                {
                    "key": "advertised",
                    "label": translate("LayerGroupTabMixin", "Advertised"),
                    "type": "checkbox",
                    "default": True,
                    "help": translate(
                        "LayerGroupTabMixin",
                        "Listed in the capabilities. Off, the group is still "
                        "served to whoever names it.",
                    ),
                },
            ]
            fields.append(
                {
                    "key": "bounds",
                    "label": translate("LayerGroupTabMixin", "Bounds"),
                    "type": "text",
                    "read_only": True,
                }
            )
        return fields

    def _wire_group_form(self, dlg):
        """Show the root fields for an Earth Observation group only."""

        def show_root(mode):
            for key in ("root_layer", "root_style"):
                dlg.set_field_visible(key, mode == "EO")

        dlg.on_value_changed("mode", show_root)
        show_root(dlg.get_values()["mode"])

    def _all_group_names(self, workspace_names=None):
        """Every group's name as a publishable spells it: bare when global,
        "workspace:group" in these workspaces (every workspace when None). A
        workspace that cannot be listed only loses its own groups. Raises on
        HTTP errors of the global list: without it, a form refused each
        global group, and Used by left them out."""
        if workspace_names is None:
            workspace_names = self._get_workspace_names()
        global_names = self._global_group_names()
        groups, _failures = self._group_names(
            workspace_names, list_global=lambda: global_names
        )
        return [name if ws == GLOBAL else f"{ws}:{name}" for name, ws in groups]

    def _add_layer_group(self):
        """Create a layer group from picked or pasted layer names."""

        def load():
            workspace_names = self._get_workspace_names()
            return (
                workspace_names,
                self._all_layer_names(),
                self._all_group_names(workspace_names),
                self._style_choices(None),
            )

        fetched = self._fetch(
            load,
            translate("LayerGroupTabMixin", "Failed to load the workspaces and layers"),
        )
        if fetched is None:
            return
        workspace_names, layer_names, group_names, style_names = fetched

        dlg = ResourceFormDialog(
            title=translate("LayerGroupTabMixin", "Create a Layer Group"),
            description=translate(
                "LayerGroupTabMixin",
                "Publish several layers as one. GeoServer computes the group's "
                "bounds from the layers it contains.",
            ),
            fields=self._group_fields(
                workspace_names,
                layer_names + group_names,
                layer_names,
                styles=style_names,
            ),
            parent=self,
            ok_label=translate("LayerGroupTabMixin", "Create"),
            validate=self._form_check(
                lambda values: self._check_new_layer_group(
                    values, layer_names, group_names
                )
            ),
        )
        self._wire_group_form(dlg)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        values = dlg.get_values()
        if self._run_action(
            lambda: self._wait_for_save(
                lambda: self._create_layer_group_from_values(
                    values, layer_names, group_names
                )
            ),
            translate("LayerGroupTabMixin", "Failed to create layer group '{}'").format(
                values["name"]
            ),
        ):
            self.show_success_message(
                translate("LayerGroupTabMixin", "Layer group '{}' created.").format(
                    values["name"]
                )
            )
            self._load_layer_groups()

    @staticmethod
    def _parse_group_layers(rows):
        """The form's rows as (layers, styles), in drawing order.

        A row is [layer, style]; the styles are parallel to the layers, ""
        where the layer keeps its own default style. The names stay as
        typed: _group_publishables resolves a bare one.
        """
        layers, styles = [], []
        for row in rows or ():
            name = (row[0] or "").strip()
            if not name:
                continue
            layers.append(name)
            styles.append((row[1] if len(row) > 1 else "").strip())
        return layers, styles

    def _check_styles_exist(self, styles):
        """Refuse a style GeoServer would silently ignore.

        A layer group POST naming a style that does not exist answers 201 with
        the style simply dropped, so the group would quietly render with the
        layers' default styles and the plugin would report success.
        """
        for reference in sorted({style for style in styles if style}):
            workspace_name, _, name = reference.rpartition(":")
            if not self._resource_exists(
                self.gs.get_style_definition, name, workspace_name or None
            ):
                raise ValueError(
                    translate(
                        "LayerGroupTabMixin", "No style '{}' on the server."
                    ).format(reference)
                )

    def _check_group_rows(self, values, workspace_name, known_layers, known_groups):
        """Refuse layer rows GeoServer would drop or refuse. Reads only (one
        GET per style and per global group, and the root layer's): the form
        runs it behind the waiting box before it closes."""
        self._group_publishables(
            values["layers"], workspace_name, known_layers, known_groups
        )
        if values["mode"] == "EO":
            self._eo_root(values, known_layers, workspace_name)

    def _check_group_name(self, values):
        """Refuse a name a URL would eat, or one that is taken. Reads only."""
        name, workspace_name = values["name"], scope(values["workspace"])
        self._require_safe_name(name)
        if self.gs.rest_service.resource_exists(self._group_path(name, workspace_name)):
            raise ValueError(
                translate(
                    "LayerGroupTabMixin", "Layer group '{}' already exists in {}."
                ).format(name, workspace_name or global_label())
            )

    def _check_new_layer_group(self, values, known_layers, known_groups):
        """What the Create form is refused for, before it closes: the name,
        then the rows."""
        self._check_group_name(values)
        self._check_group_rows(
            values, scope(values["workspace"]), known_layers, known_groups
        )

    def _create_layer_group_from_values(
        self, values, known_layers=None, known_groups=()
    ):
        """POST a new layer group, refusing to overwrite an existing one.

        :param known_layers: the server's layer names, as the form's picker
            listed them; a typed line naming none of them, or none of
            known_groups, is refused here, by line, instead of GeoServer
            dropping it.

        TODO(#1): upstream: create_layer_group() cannot express any of this:
        it has no global scope, it qualifies every layer with the group's own
        workspace (so no group spanning workspaces, and no nested group), it
        replaces the bounds with a world bbox read from a three-entry EPSG
        table (a KeyError for any other code), it sends the abstract under
        "abstract", which GeoServer silently drops, and it has no root layer.
        """
        name = values["name"]
        workspace_name = scope(values["workspace"])
        self._check_group_name(values)
        # The rows once: building the publishables is the check itself.
        published, styles = self._group_publishables(
            values["layers"], workspace_name, known_layers, known_groups
        )
        mode = values["mode"]
        root = (
            self._eo_root(values, known_layers, workspace_name) if mode == "EO" else {}
        )

        group = {"name": name, "mode": mode, "publishables": {"published": published}}
        group.update(root)
        if workspace_name:
            group["workspace"] = {"name": workspace_name}
        if values.get("title"):
            group["title"] = values["title"]
        if values.get("abstract"):
            group["abstractTxt"] = values["abstract"]
        if any(styles) or any(item["@type"] != "layer" for item in published):
            # "" is how GeoServer itself spells "this layer's own default style".
            # With a nested group and no styles, GeoServer fails (HTTP 500).
            group["styles"] = {
                "style": [{"name": style} if style else "" for style in styles]
            }
        # No "bounds": GeoServer then computes the union of the layers' extents.
        self._raw_rest(
            "post", self._groups_path(workspace_name), json={"layerGroup": group}
        )

    # -- Add to QGIS -----------------------------------------------------------

    def _add_group_to_qgis(self, row_data):
        """Add the group to the current QGIS project as one WMS layer.

        WMS only: a group has no feature type to fetch over WFS, and it reaches
        GeoWebCache only once someone caches it there.
        """
        name, workspace_name = row_data[0], scope(row_data[1])
        qualified = f"{workspace_name}:{name}" if workspace_name else name

        def build():
            return self._valid_layer(self._server_layer("WMS", qualified, name))

        layer = self._fetch(
            build,
            translate("LayerGroupTabMixin", "Could not add '{}' to QGIS").format(name),
        )
        if layer is not None:
            QgsProject.instance().addMapLayer(layer)
            self.show_success_message(
                translate(
                    "LayerGroupTabMixin", "'{}' added to the project as WMS."
                ).format(name)
            )

    def _preview_group(self, row_data):
        """Show the group on a map of its own, like the Layers tab's Preview.

        Nothing reaches the project. The WMS layer reads the group's extent
        from the capabilities, in the URI's EPSG:4326 (measured on 2.28.5,
        a projected spearfish included): a GET of the group was a second read.
        """
        name, workspace_name = row_data[0], scope(row_data[1])
        qualified = f"{workspace_name}:{name}" if workspace_name else name
        # An invalid layer is not an error here: the window explains it.
        layer = self._fetch(
            lambda: self._server_layer("WMS", qualified, qualified),
            translate(
                "LayerGroupTabMixin", "Could not build the preview of '{}'"
            ).format(name),
        )
        if layer is None:
            return
        extent = layer.extent() if layer.isValid() else None
        bbox = (
            None
            if extent is None or extent.isEmpty()
            else (
                extent.xMinimum(),
                extent.yMinimum(),
                extent.xMaximum(),
                extent.yMaximum(),
            )
        )
        LayerPreviewDialog(qualified, layer, bbox, parent=self).show()

    def _preview_group_in_browser(self, row_data):
        """Open GeoServer's own preview of the group, on its bounds.

        The URL builder is LayerTabMixin._preview_url, reached through the
        dialog class like _layer_uri; a global group has no workspace in the
        path and no prefix on its name.
        """
        name, workspace_name = row_data[0], scope(row_data[1])
        detail = self._fetch(
            lambda: self._group_detail(name, workspace_name),
            translate("LayerGroupTabMixin", "Failed to load layer group '{}'").format(
                name
            ),
        )
        if detail is None:
            return
        bbox, srs = self._bbox_from(detail.get("bounds"))
        qualified = f"{workspace_name}:{name}" if workspace_name else name
        self._open_in_browser(
            self._preview_url(
                self.plg_settings.get_plg_settings().geoserver_url,
                qualified,
                bbox,
                srs,
                workspace=workspace_name,
            )
        )

    # -- Delete ----------------------------------------------------------------

    def _delete_layer_group(self, row_data):
        """Delete a single layer group after confirmation."""
        self._delete_selected_layer_groups([row_data])

    def _delete_selected_layer_groups(self, selected_rows):
        """Delete one or more layer groups after confirmation."""
        self._delete_many(
            [
                (
                    # As the Layers tab names them; a global group has no prefix.
                    f"{scope(row[1])}:{row[0]}" if scope(row[1]) else row[0],
                    lambda name=row[0], ws=scope(row[1]): self._do_delete_group(
                        name, ws
                    ),
                )
                for row in selected_rows
            ],
            self._load_layer_groups,
            ask=self._one_or_many(
                translate(
                    "LayerGroupTabMixin",
                    "Are you sure you want to delete layer group '{}'?",
                ),
                lambda n: translate(
                    "LayerGroupTabMixin",
                    "Are you sure you want to delete %n layer group(s)?",
                    None,
                    n,
                ),
            ),
            done=self._one_or_many(
                translate("LayerGroupTabMixin", "Layer group '{}' deleted."),
                lambda n: translate(
                    "LayerGroupTabMixin", "%n layer group(s) deleted.", None, n
                ),
            ),
            cascade=translate(
                "LayerGroupTabMixin",
                "Only the group goes away. The layers it published stay. "
                "GeoServer refuses if another layer group contains this one.",
            ),
        )

    def _do_delete_group(self, name, workspace_name):
        """DELETE one layer group; the library covers the workspace scope only.

        TODO(#1): see _global_group_names; delete_layer_group() requires a
        workspace_name, so a global group needs the raw path.
        """
        try:
            if workspace_name:
                # The library interpolates the names into the path as they
                # are, so "a#b" would delete "a": hand it the quoted segments.
                self._check(
                    self.gs.delete_layer_group(
                        quote(workspace_name, safe=""), quote(name, safe="")
                    )
                )
            else:
                self._raw_rest("delete", self._group_path(name, None))
        except Exception:
            self._refuse_gone_group(name, workspace_name)
            raise
