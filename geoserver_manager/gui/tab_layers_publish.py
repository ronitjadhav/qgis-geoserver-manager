#! python3  # noqa: E265

"""
Publishing a layer, from the Layers tab: a table of a datastore, or a
layer of the QGIS project (one or a batch, the layer tree's menu too).

Used as a mixin for GeoServerMainDialog.
"""

import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote

from qgis.core import Qgis, QgsRasterLayer
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtWidgets import QDialog

from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog
from geoserver_manager.toolbelt.payload import VIRTUAL_TABLE, view_of, words
from geoserver_manager.toolbelt.qgis_export import (
    export_to_geopackage,
    geoserver_name,
    reprojection_target,
    require_crs,
)
from geoserver_manager.toolbelt.rest import Abandoned
from geoserver_manager.toolbelt.sld import (
    layer_to_sld,
)

# Where the new layer's data comes from, in the publish form.
_SOURCE_TABLE = "A table in a datastore"
_SOURCE_QGIS = "A layer from this QGIS project"
_SOURCE_SQL = "A SQL view of a database store"

# Fields that belong to one source only.
_SOURCE_FIELDS = {
    _SOURCE_TABLE: ("datastore", "table", "epsg"),
    _SOURCE_QGIS: ("qgis_layer", "name", "replace", "with_style"),
    _SOURCE_SQL: (
        "datastore",
        "view_name",
        "epsg",
        "sql",
        "geometry_column",
        "geometry_type",
        "key_columns",
        "parameters",
    ),
}

# The same reason as in tab_layers.py: translate() with this file's own class.
translate = QCoreApplication.translate


class LayerPublishMixin:
    """Mixin that adds publishing to the Layers tab."""

    def _refuse_vector_clash(self, ws_name, name, values):
        """Refuse a vector upload onto a store or layer it would not own.

        The store first, then the layer: a layer of the name in another
        store is what _refuse_layer_clash catches.
        """
        # The library's reads take the workspace raw: "sf#x" reads "sf".
        self._require_safe_name(ws_name)
        if not values.get("replace") and self._resource_exists(
            self._get_datastore, ws_name, name
        ):
            raise ValueError(
                translate(
                    "LayerPublishMixin", "Datastore '{}' already exists in '{}'."
                ).format(name, ws_name)
                + " "
                + translate("LayerPublishMixin", "Tick Replace to overwrite it.")
            )
        self._refuse_layer_clash(
            ws_name, name, values.get("replace"), "data", "GeoPackage"
        )

    def _refuse_layer_clash(self, ws_name, name, replace, kind, store_type):
        """Refuse an upload that would land on another store's layer.

        Measured on 2.28.5: a layer ws:name in another store makes GeoServer
        call the new one name1, and the style and metadata that follow went to
        the old ws:name, reported as "name published". With Replace, a store
        of the name that is not the plugin's own file store (a PostGIS one)
        had GeoServer import the file's tables into that database.

        :param kind: "data" or "coverage", the store collection.
        :param store_type: the only store type Replace may overwrite.
        """
        qualified = f"{ws_name}:{name}"
        if self.gs.rest_service.resource_exists(self._layers_url(qualified)):
            if not replace:
                raise ValueError(
                    translate(
                        "LayerPublishMixin",
                        "Layer '{}' already exists. Tick Replace to overwrite it, "
                        "or pick another name.",
                    ).format(qualified)
                )
            _kind, store, _style = self._layer_summary(qualified)
            if store != name:
                raise ValueError(
                    translate(
                        "LayerPublishMixin",
                        "Layer '{}' is published from store '{}', which this "
                        "upload would not replace. Pick another name.",
                    ).format(qualified, store)
                )
        if not replace:
            return
        # Through the library: both store models keep the type (it was a
        # raw GET, with no TODO and no row in #1 to say why).
        getter = self._get_datastore if kind == "data" else self.gs.get_coverage_store
        detail, status = getter(ws_name, name)
        if status == 404:
            return
        found = (self._check((detail, status)) or {}).get("type") or "-"
        if found != store_type:
            raise ValueError(
                translate(
                    "LayerPublishMixin",
                    "Store '{}' is a {} store, and Replace only overwrites a {} store "
                    "this plugin published. Pick another name.",
                ).format(name, found, store_type)
            )

    def _available_tables(self, workspace_name, datastore_name):
        """Tables of a datastore that are not published as layers yet.

        TODO(#1): upstream as get_available_feature_types(ws, ds); the
        library has no call for GeoServer's ?list=available. Workaround: GET
        the featuretypes path with that query.
        """
        path = self.gs.rest_service.rest_endpoints.featuretypes(
            workspace_name, datastore_name
        )
        payload = self._raw_rest("get", path, params={"list": "available"}).json()
        return sorted(self._unwrap(payload, "list", "string"))

    def _publish_fields(self, workspace_names):
        """Field definitions for the publish form; combos cascade at runtime."""
        return [
            {
                "key": "source",
                "label": translate("LayerPublishMixin", "Source"),
                "type": "combo",
                "options": [
                    (
                        translate("LayerPublishMixin", "A table in a datastore"),
                        _SOURCE_TABLE,
                    ),
                    (
                        translate(
                            "LayerPublishMixin", "A layer from this QGIS project"
                        ),
                        _SOURCE_QGIS,
                    ),
                    (
                        translate(
                            "LayerPublishMixin", "A SQL view of a database store"
                        ),
                        _SOURCE_SQL,
                    ),
                ],
            },
            {
                "key": "workspace",
                "label": translate("LayerPublishMixin", "Workspace"),
                "type": "combo",
                "options": workspace_names,
                "required": True,
            },
            {
                "key": "datastore",
                "label": translate("LayerPublishMixin", "Datastore"),
                "type": "combo",
                "options": [],
                "required": True,
                "help": translate(
                    "LayerPublishMixin", "Datastores of the selected workspace"
                ),
            },
            {
                "key": "table",
                "label": translate("LayerPublishMixin", "Table"),
                "type": "combo",
                "options": [],
                "required": True,
                "help": translate(
                    "LayerPublishMixin",
                    "Tables in the datastore that are not published yet. The layer "
                    "takes the table's name.",
                ),
            },
            {
                "key": "view_name",
                "label": translate("LayerPublishMixin", "Layer name"),
                "type": "text",
                "required": True,
                "visible": False,
                "help": translate(
                    "LayerPublishMixin", "The name of the view, and of its layer."
                ),
            },
            {
                "key": "epsg",
                # Short: the longest label sets the width of the label column
                # for the whole form, and this one squeezed every combo.
                "label": translate("LayerPublishMixin", "SRS (EPSG code)"),
                "type": "text",
                "required": True,
                "placeholder": translate("LayerPublishMixin", "for example 3857"),
                "crs": True,
                # Required, so on the first tab: on Metadata it bounced the
                # user there after Publish.
                "help": translate(
                    "LayerPublishMixin",
                    "The SRS GeoServer declares for the layer: the table's own, as a "
                    "bare EPSG number. A wrong value misplaces the data. Look it up in "
                    "the table's geometry column, or let GeoServer's web interface "
                    "compute it.",
                ),
            },
            *self._view_fields(visible=False),
            {
                "key": "title",
                "label": translate("LayerPublishMixin", "Title"),
                "type": "text",
                "group": translate("LayerPublishMixin", "Metadata"),
                "placeholder": translate("LayerPublishMixin", "Optional"),
            },
            {
                "key": "abstract",
                "label": translate("LayerPublishMixin", "Abstract"),
                "type": "textarea",
                "group": translate("LayerPublishMixin", "Metadata"),
                "placeholder": translate("LayerPublishMixin", "Optional"),
            },
            {
                "key": "keywords",
                "label": translate("LayerPublishMixin", "Keywords"),
                "type": "list",
                "group": translate("LayerPublishMixin", "Metadata"),
            },
            {
                "key": "qgis_layer",
                "label": translate("LayerPublishMixin", "QGIS layer"),
                "type": "layer",
                "required": True,
                "visible": False,
                "help": translate(
                    "LayerPublishMixin",
                    "A vector becomes a GeoPackage datastore and a raster a GeoTIFF "
                    "coverage store. Either way the plugin copies the data to the "
                    "server; nothing is linked.",
                ),
            },
            {
                "key": "name",
                "label": translate("LayerPublishMixin", "Layer name"),
                "type": "text",
                "required": True,
                "visible": False,
                "help": translate(
                    "LayerPublishMixin",
                    "Also the name of the store it creates and, for a vector, of the "
                    "table inside it. The plugin replaces any character a layer name "
                    "cannot carry.",
                ),
            },
            {
                "key": "replace",
                "label": translate(
                    "LayerPublishMixin", "Replace it if it already exists"
                ),
                "type": "checkbox",
                "default": False,
                "visible": False,
            },
            {
                "key": "with_style",
                "label": translate(
                    "LayerPublishMixin", "Upload its symbology as the layer's style"
                ),
                "type": "checkbox",
                "default": True,
                "visible": False,
                "help": translate(
                    "LayerPublishMixin",
                    "Vector layers only. A raster's symbology is not uploaded.",
                ),
            },
        ]

    def _on_publish_source_changed(self, dlg, source):
        """Show the fields of the chosen source only."""
        wanted = _SOURCE_FIELDS.get(source, ())
        for key in {key for keys in _SOURCE_FIELDS.values() for key in keys}:
            dlg.set_field_visible(key, key in wanted)
        if source == _SOURCE_QGIS:
            self._on_publish_layer_picked(
                dlg, dlg.get_widget("qgis_layer").currentLayer()
            )
        else:
            self._refill_publish_combos(dlg)

    def _on_publish_layer_picked(self, dlg, layer):
        """Suggest the name, and hide what a raster ignores (its symbology)."""
        self._prefill_publish_name(dlg, layer)
        dlg.set_field_visible("with_style", not isinstance(layer, QgsRasterLayer))

    @staticmethod
    def _prefill_publish_name(dlg, layer):
        """Suggest the GeoServer-safe form of the picked layer's name.

        Shared with the Coverage Stores tab's raster upload: every mixin is
        on the same dialog.
        """
        widget = dlg.get_widget("name")
        if layer is not None and not widget.text().strip():
            widget.setText(geoserver_name(layer.name()))

    def _refill_publish_combos(self, dlg, workspace=None, datastore=None):
        """Cascade: workspace -> its datastores -> the store's unpublished tables.

        A fetch that fails leaves its combo empty (and logs why); the required
        check then stops Save with the empty combo highlighted. The QGIS
        source never reads these combos, so a workspace change there costs
        nothing; switching back to the table source fills them.
        """
        source = dlg.get_values().get("source")
        if source == _SOURCE_QGIS:
            return
        ws_combo = dlg.get_widget("workspace")
        ds_combo = dlg.get_widget("datastore")
        table_combo = dlg.get_widget("table")
        workspace = workspace or ws_combo.currentText()

        if datastore is None:
            ds_combo.blockSignals(True)  # the table refill below is explicit
            ds_combo.clear()
            try:
                ds_combo.addItems(
                    self._wait_for(lambda: self._datastore_names(workspace))
                )
            except Exception as e:
                self.log(f"Could not list datastores of {workspace}: {e}")
            ds_combo.blockSignals(False)
            datastore = ds_combo.currentText()

        table_combo.clear()
        if workspace and datastore and source == _SOURCE_TABLE:
            try:
                table_combo.addItems(
                    self._wait_for(lambda: self._available_tables(workspace, datastore))
                )
            except Exception as e:
                self.log(f"Could not list tables of {workspace}/{datastore}: {e}")

    def _publish_workspaces(self):
        """The workspaces a publish form offers, or None once a failure or an
        empty server is reported."""
        workspace_names = self._fetch(
            self._get_workspace_names,
            translate("LayerPublishMixin", "Failed to load the workspaces"),
        )
        if workspace_names is None:
            return None
        if not workspace_names:
            self.show_warning_message(
                translate(
                    "LayerPublishMixin",
                    "No workspaces available. Create a workspace first.",
                )
            )
            return None
        return workspace_names

    def _check_publish_form(self, values):
        """What the publish would be refused for, before the form closes: a
        bad EPSG code, a layer or a store that exists. Reads only, in a
        worker; the publish checks again, before its export."""
        if values.get("source") == _SOURCE_TABLE:
            self._check_publish_table(values)
            return
        if values.get("source") == _SOURCE_SQL:
            self._check_publish_sql(values)
            return
        ws_name, name = values["workspace"], geoserver_name(values["name"])
        if isinstance(values["qgis_layer"], QgsRasterLayer):
            self._check_raster_target(ws_name, name, values.get("replace"))
        else:
            self._refuse_vector_clash(ws_name, name, values)

    def _publish_layer(self, layer=None, typed=None):
        """Open the publish form: pick workspace, datastore and table.

        :param layer: a project layer to preselect as the source, which is how
            the layer tree's *Publish to GeoServer* entry opens this form.
        :param typed: the form's values GeoServer refused after it closed: the
            form opens again with them, its combos refilled.
        """
        workspace_names = self._publish_workspaces()
        if workspace_names is None:
            return

        dlg = ResourceFormDialog(
            title=translate("LayerPublishMixin", "Publish a Layer"),
            description=translate(
                "LayerPublishMixin",
                "Publish a table of a datastore, or a layer of this QGIS project. A "
                "project layer is uploaded as a GeoPackage (a vector becomes a "
                "datastore) or as a GeoTIFF (a raster becomes a coverage store).",
            ),
            fields=self._publish_fields(workspace_names),
            values=typed,
            parent=self,
            ok_label=translate("LayerPublishMixin", "Publish"),
            validate=self._form_check(self._check_publish_form),
        )
        dlg.get_widget("workspace").currentTextChanged.connect(
            lambda ws: self._refill_publish_combos(dlg, workspace=ws)
        )
        dlg.get_widget("datastore").currentTextChanged.connect(
            lambda ds: self._refill_publish_combos(dlg, datastore=ds)
        )
        dlg.on_value_changed(
            "source", lambda source: self._on_publish_source_changed(dlg, source)
        )
        dlg.get_widget("qgis_layer").layerChanged.connect(
            lambda layer: self._on_publish_layer_picked(dlg, layer)
        )
        if typed is not None:
            if typed["source"] == _SOURCE_QGIS:
                dlg.get_widget("qgis_layer").setLayer(typed["qgis_layer"])
            self._on_publish_source_changed(dlg, typed["source"])
            if typed["source"] != _SOURCE_QGIS:
                # The source's refill picked the first store: the typed one.
                dlg.set_values({"datastore": typed["datastore"]})
                if typed["source"] == _SOURCE_TABLE:
                    dlg.set_values({"table": typed["table"]})
        elif layer is not None:
            # The layer first: switching the source prefills the name from
            # whichever layer the combo shows at that moment. The table
            # source's combos are not filled: that source is not shown.
            dlg.get_widget("qgis_layer").setLayer(layer)
            dlg.set_values({"source": _SOURCE_QGIS})
        else:
            self._on_publish_source_changed(dlg, _SOURCE_TABLE)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        values = dlg.get_values()
        if values.get("source") == _SOURCE_QGIS:
            # The checks and the export raise here, under the wait cursor; the
            # upload then streams in a task and reports itself.
            started = self._run_action(
                lambda: self._publish_qgis_layer(values),
                translate("LayerPublishMixin", "Failed to publish '{}'").format(
                    geoserver_name(values["name"])
                ),
            )
            if started is False:
                self._publish_layer(typed=values)
            return
        if values.get("source") == _SOURCE_SQL:
            published, publish = values["view_name"], self._publish_sql_view
        else:
            published, publish = values["table"], self._publish_table
        done = self._run_action(
            lambda: self._wait_for_save(lambda: publish(values)),
            translate("LayerPublishMixin", "Failed to publish '{}'").format(published),
        )
        if done:
            self.show_success_message(
                translate("LayerPublishMixin", "Layer '{}' published.").format(
                    published
                )
            )
            self._load_layers()
        elif done is False:
            # Refused after the form closed (a query GeoServer cannot run): back
            # in the form, as typed.
            self._publish_layer(typed=values)

    def _publish_layers(self, layers):
        """Publish several project layers into one workspace, one after another.

        One small form (workspace, Replace, style) for all of them; each layer
        keeps the GeoServer-safe name the form lists, even when it is renamed
        in QGIS before its turn. Only one upload runs at a time, so each layer
        starts when the previous one has ended. A layer refused before any
        request, or whose upload fails, is reported and skipped, and so is one
        no longer in the project when its turn comes; Cancel stops the batch,
        and the summary names what was published and what was not.
        """
        names = [geoserver_name(layer.name()) for layer in layers]
        clashes = sorted({name for name in names if names.count(name) > 1})
        if clashes:
            # Published one after another, the second would replace the first
            # (with Replace) or be refused as existing (without it).
            self.show_warning_message(
                translate(
                    "LayerPublishMixin",
                    "These layers would get the same GeoServer name: {}. "
                    "Rename them in QGIS first.",
                ).format(", ".join(clashes))
            )
            return
        if not self._upload_slot_free():
            return
        workspace_names = self._publish_workspaces()
        if workspace_names is None:
            return
        fields = [
            dict(field, visible=True)
            for field in self._publish_fields(workspace_names)
            if field["key"] in ("workspace", "replace", "with_style")
        ]
        for field in fields:
            if field["key"] == "replace":
                # Several layers: "it" read as one of them.
                field["label"] = translate(
                    "LayerPublishMixin", "Replace those that already exist"
                )
        listing = "\n".join(
            f"  • {layer.name()} → {name}" for layer, name in zip(layers, names)
        )
        dlg = ResourceFormDialog(
            title=translate(
                "LayerPublishMixin", "Publish %n Layer(s)", None, len(layers)
            ),
            description=translate(
                "LayerPublishMixin",
                "The plugin uploads each layer on its own, under the name shown: a "
                "vector as a GeoPackage datastore, a raster as a GeoTIFF coverage "
                "store."
                "\n\n{}",
            ).format(listing),
            fields=fields,
            parent=self,
            ok_label=translate("LayerPublishMixin", "Publish"),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        values = dlg.get_values()
        queue = list(zip(layers, names))
        published, failed = [], []
        gone = []  # removed from the project, or a new project, before their turn
        ended = []  # how the layer that stopped the batch ended, in words

        def summary(stopped):
            left = [name for _layer, name in queue]
            if not stopped and not failed and not gone:
                self.show_success_message(
                    translate(
                        "LayerPublishMixin",
                        "%n layer(s) published.",
                        None,
                        len(published),
                    )
                )
                return
            parts = [
                translate("LayerPublishMixin", "Published: {}.").format(
                    ", ".join(published) or "-"
                )
            ]
            if failed:
                parts.append(
                    translate("LayerPublishMixin", "Failed: {}.").format(
                        ", ".join(failed)
                    )
                )
            if gone:
                parts.append(
                    translate(
                        "LayerPublishMixin",
                        "Not started, no longer in the project: {}.",
                    ).format(", ".join(gone))
                )
            parts += ended
            if stopped and left:
                parts.append(
                    translate("LayerPublishMixin", "Not started: {}.").format(
                        ", ".join(left)
                    )
                )
            text = " ".join(parts)
            self.show_warning_message(text)
            if stopped:
                # The dialog may be closed by now: the log is what is seen.
                self.log(text, log_level=Qgis.MessageLevel.Warning)

        def next_layer():
            # A layer the project deleted meanwhile: its wrapper raises on any call.
            while queue and sip.isdeleted(queue[0][0]):
                gone.append(queue.pop(0)[1])
            if not queue:
                summary(stopped=False)
                return
            layer, name = queue.pop(0)

            def done(outcome):
                if outcome == "stopped":
                    ended.append(
                        translate(
                            "LayerPublishMixin",
                            "Published, stopped waiting for its title, keywords "
                            "or style: {}.",
                        ).format(name)
                    )
                elif outcome == "cancelled" and self._closing:
                    # Its upload ran on, and the log says how it ended.
                    ended.append(
                        translate(
                            "LayerPublishMixin", "Uploading when the dialog closed: {}."
                        ).format(name)
                    )
                elif outcome == "cancelled":
                    ended.append(
                        translate("LayerPublishMixin", "Cancelled: {}.").format(name)
                    )
                else:
                    (published if outcome == "done" else failed).append(name)
                    next_layer()
                    return
                summary(stopped=True)

            started, stopped = [], []

            def start():
                try:
                    started.append(
                        self._publish_qgis_layer(
                            {
                                "workspace": values["workspace"],
                                # As listed and checked: a rename since then is ignored.
                                "name": name,
                                "replace": values.get("replace"),
                                "with_style": values.get("with_style"),
                            },
                            layer=layer,
                            on_done=done,
                        )
                    )
                except Abandoned:
                    # Cancel on the waiting box, during the layer's checks:
                    # the same word as Cancel on the upload, the same end.
                    stopped.append(name)
                    raise

            self._run_action(
                start,
                translate("LayerPublishMixin", "Failed to publish '{}'").format(name),
            )
            if stopped:
                queue.insert(0, (layer, name))
                summary(stopped=True)
            elif started != [True]:
                failed.append(name)
                next_layer()

        next_layer()

    def _publish_qgis_layer(self, values, layer=None, on_done=None):
        """Upload a QGIS layer and publish it: a GeoPackage datastore for a
        vector, a GeoTIFF coverage store for a raster.

        One store per published layer, named after it, which is also the name
        of the table inside the GeoPackage. GeoServer configures a feature
        type per table when the file lands, so this publishes the layer in one
        request. The data is copied: later edits in QGIS do not reach it, and
        deleting the store leaves the uploaded file in the data directory.

        The layer, its CRS, the name check, the export and the SLD happen here
        on the GUI thread (a live QGIS layer, invariant 9), and raise into the
        caller's _run_action; the PUT then streams in a task through
        _upload_file, with progress and Cancel, and the metadata and the style
        follow on the GUI thread once it lands. A raster goes down the Coverage
        Stores tab's path (_publish_qgis_raster), the same Replace semantics.

        `layer` overrides the form's pick (a batch hands each one in), and
        `on_done(outcome)` runs once the upload has ended however it ended,
        with "stopped" for a vector published whose title, keywords or style
        a Cancel stopped waiting for. Returns True when an upload started, so
        a batch can tell a layer refused before any request from one still on
        its way.

        TODO(#1): upstream as create_datastore_from_file(ws, name, path); the
        library can only create datastores from connection parameters, so the
        upload is a raw PUT of .../datastores/{name}/file.gpkg (row 28).
        """
        if not self._upload_slot_free():
            return False
        ws_name = values["workspace"]
        name = geoserver_name(values["name"])
        if layer is None:
            layer = values["qgis_layer"]
        if isinstance(layer, QgsRasterLayer):
            return self._publish_qgis_raster(
                {
                    "workspace": ws_name,
                    "name": values["name"],
                    "replace": values.get("replace"),
                    "title": values.get("title", ""),
                    "abstract": values.get("abstract", ""),
                    "keywords": values.get("keywords", ""),
                },
                layer=layer,
                on_done=on_done,
            )
        require_crs(layer)
        # The checks are reads: off the GUI thread (the export below is not).
        self._wait_for(lambda: self._refuse_vector_clash(ws_name, name, values))

        # Before the export: a failure here used to leave the GeoPackage behind.
        sld = layer_to_sld(layer) if values.get("with_style") else None
        # A CRS without an EPSG code would be published as UNKNOWN: the export
        # reprojects it to one GeoServer can declare (reprojection_target).
        folder = Path(tempfile.mkdtemp(prefix="gsm_publish_"))
        package = folder / f"{name}.gpkg"
        try:
            export_to_geopackage(
                layer, package, name, target_crs=reprojection_target(layer)
            )
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        upload_path = (
            f"{self.gs.rest_service.rest_endpoints.base_url}"
            f"/workspaces/{quote(ws_name, safe='')}/datastores/{quote(name, safe='')}"
            "/file.gpkg"
        )
        failure = translate("LayerPublishMixin", "Failed to publish '{}'").format(name)
        abandoned = []  # a Cancel stopped the steps after the upload

        def published(_result):
            # self.gs is still the client the upload used: a Refresh and a
            # profile switch are refused while an upload runs.
            kept = []  # the style of the name was kept: is it the default?

            def server_side():
                # Best effort: the data is published at this point, so a
                # failure to set a performance flag belongs in the log, not in
                # the user's face.
                try:
                    self._make_datastore_read_only(ws_name, name)
                except Exception as error:
                    self.log(
                        f"Could not mark '{ws_name}:{name}' read-only: {error}",
                        log_level=Qgis.MessageLevel.Warning,
                    )
                self._set_feature_type_metadata(ws_name, name, values)

            def finish():
                try:
                    self._wait_for_save(server_side)  # requests, off the GUI thread
                    if sld is not None and not self._push_qgis_style(
                        name, ws_name, sld, name, True
                    ):
                        # A Replace usually finds it still the layer's default.
                        default, _others = self._wait_for(
                            lambda: self._layer_styles(ws_name, name)
                        )
                        kept.append(default == f"{ws_name}:{name}")
                except Abandoned:
                    abandoned.append(name)
                    raise

            done = translate(
                "LayerPublishMixin",
                "Layer '{}' is published, but its title, keywords or style "
                "could not be set",
            ).format(name)
            if self._run_action(lambda: self._partly_saved(finish, done), failure):
                if kept == [True]:
                    self.show_warning_message(
                        translate(
                            "LayerPublishMixin",
                            "Layer '{}' published. Style '{}' left as it is.",
                        ).format(name, name)
                    )
                elif kept:
                    # Not assigned either: it may not be this layer's style.
                    self.show_warning_message(
                        translate(
                            "LayerPublishMixin",
                            "Layer '{}' published. Style '{}' already existed, so it "
                            "stays as it is, not assigned to the layer.",
                        ).format(name, name)
                    )
                else:
                    self.show_success_message(
                        translate("LayerPublishMixin", "Layer '{}' published.").format(
                            name
                        )
                    )
            # The user may have moved to another tab while it uploaded.
            self._reload_current_tab()

        def upload_done(outcome):
            on_done("stopped" if abandoned and outcome == "done" else outcome)

        return self._upload_file(
            failure,
            self.gs.rest_service.rest_client,
            upload_path,
            package,
            {"update": "overwrite"},
            {"Content-Type": "application/x-sqlite3"},
            published,
            lambda _task: self._report_cancelled_upload(
                translate("LayerPublishMixin", "datastore"),
                translate("LayerPublishMixin", "Datastores"),
                lambda: self._resource_exists(self.gs.get_datastore, ws_name, name),
                name,
            ),
            folder=folder,
            on_done=upload_done if on_done is not None else None,
        )

    def _make_datastore_read_only(self, workspace_name, name):
        """Mark an uploaded GeoPackage store read-only.

        Nothing writes to a GeoPackage the plugin has just uploaded, and a
        read-only file store is the recommended setting for that case; it
        lets GeoServer serve it without taking write locks (not measured
        here). Merged onto the server's own parameters, never sent as a
        template (invariant 3).
        """
        detail = self._check(self.gs.get_datastore(workspace_name, name))
        params = dict(self._connection_params(detail))
        if params.get("read_only") == "true":
            return
        params["read_only"] = "true"
        self._check(
            self.gs.create_datastore(
                workspace_name=workspace_name,
                datastore_name=name,
                datastore_type=detail.get("type") or "GeoPackage",
                connection_parameters=params,
                enabled=bool(detail.get("enabled", True)),
            )
        )

    def _set_feature_type_metadata(self, workspace_name, name, values):
        """Add the form's title, abstract and keywords to a published layer.

        A partial feature-type PUT merges (verified on GeoServer 2.28.5), so
        the SRS, bounding box and attributes GeoServer computed from the upload
        survive, which create_feature_type() would overwrite with a template.

        TODO(#1): no update_feature_type() in the library (row 29).
        """
        keywords = words(values.get("keywords"))
        metadata = {}
        if values.get("title"):
            metadata["title"] = values["title"]
        if values.get("abstract"):
            metadata["abstract"] = values["abstract"]
        if keywords:
            metadata["keywords"] = {"string": keywords}
        if not metadata:
            return
        path = self.gs.rest_service.rest_endpoints.featuretype(
            workspace_name, name, name
        )
        self._raw_rest("put", path, json={"featureType": metadata})

    def _check_publish_table(self, values):
        """Refuse, before any write, a table publish GeoServer would take badly;
        returns the EPSG code as a number. Reads only: the form runs it before
        it closes, the publish again."""
        # A layer name is unique in its workspace: the Table list only offers
        # tables unpublished in this store, so the clash is another store's.
        qualified = f"{values['workspace']}:{values['table']}"
        if self.gs.rest_service.resource_exists(self._layers_url(qualified)):
            _kind, store, _style = self._layer_summary(qualified)
            raise ValueError(
                translate(
                    "LayerPublishMixin",
                    "Layer '{}' already exists, published from store '{}'. A "
                    "layer name is unique in its workspace: rename or delete "
                    "that layer first.",
                ).format(qualified, store)
            )
        return self._epsg_number(values.get("epsg"))

    def _check_publish_sql(self, values):
        """Refuse a SQL view GeoServer would take badly; returns the EPSG code.
        Reads only: the form runs it before it closes, the publish again."""
        ws_name, ds_name, name = (
            values["workspace"],
            values["datastore"],
            values["view_name"],
        )
        self._require_safe_name(name)
        if self.gs.rest_service.resource_exists(self._layers_url(f"{ws_name}:{name}")):
            raise ValueError(
                translate(
                    "LayerPublishMixin", "Layer '{}' already exists in '{}'."
                ).format(name, ws_name)
            )
        # On a store of files GeoServer answers a bare 500 "Not available"
        # (measured on 2.28.5). A database store has a dbtype parameter.
        store = self._check(self._get_datastore(ws_name, ds_name))
        if "dbtype" not in self._connection_params(store):
            raise ValueError(
                translate(
                    "LayerPublishMixin",
                    "A SQL view needs a database store, such as PostGIS. '{}' is a "
                    "store of files.",
                ).format(ds_name)
            )
        return self._epsg_number(values.get("epsg"))

    def _publish_sql_view(self, values):
        """Publish a SQL view as a feature type.

        TODO(#1): nothing in the library creates a SQL view (row 82). Raw POST
        of a feature type whose metadata holds a JDBC_VIRTUAL_TABLE entry.
        Measured on 2.28.5: GeoServer computes the bounds and the attributes
        from the query; a query it cannot run is a 500 that leaves nothing.
        """
        ws_name, ds_name, name = (
            values["workspace"],
            values["datastore"],
            values["view_name"],
        )
        epsg = self._check_publish_sql(values)
        body = {
            "name": name,
            "nativeName": name,
            "srs": f"EPSG:{epsg}",
            "projectionPolicy": "FORCE_DECLARED",
            "metadata": {
                "entry": [
                    {"@key": VIRTUAL_TABLE, "virtualTable": view_of(name, values, epsg)}
                ]
            },
        }
        for key in ("title", "abstract"):
            if values.get(key):
                body[key] = values[key]
        if words(values.get("keywords")):
            body["keywords"] = {"string": words(values.get("keywords"))}
        path = self.gs.rest_service.rest_endpoints.featuretypes(
            quote(ws_name, safe=""), quote(ds_name, safe="")
        )
        self._raw_rest("post", path, json={"featureType": body})

    def _publish_table(self, values):
        """Publish one table as a feature type through the library."""
        ws_name, ds_name, table = (
            values["workspace"],
            values["datastore"],
            values["table"],
        )
        epsg = self._check_publish_table(values)
        keywords = words(values.get("keywords"))
        # TODO(#1): the facade's create_feature_type(epsg=...) fills both
        # bounding boxes from utils.EPSG_BBOX, which knows 2056, 4326 and 3857
        # only: any other code raised KeyError before a request was sent, and
        # 4326 or 3857 published a world extent. The model without epsg_code
        # sends no box, and GeoServer computes both from the data (measured on
        # 2.28.5 with an EPSG:25832 table).
        from geoservercloud.models.featuretype import FeatureType

        self._check(
            self.gs.rest_service.create_feature_type(
                FeatureType(
                    name=table,
                    native_name=table,
                    workspace_name=ws_name,
                    store_name=ds_name,
                    srs=f"EPSG:{epsg}",
                    projection_policy="FORCE_DECLARED",
                    title=values.get("title") or None,
                    abstract=values.get("abstract") or None,
                    keywords=keywords or None,
                )
            )
        )
