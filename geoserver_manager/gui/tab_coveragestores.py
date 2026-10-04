#! python3  # noqa: E265

"""
Coverage Stores tab: list and create raster stores, view and publish their
coverages, delete a store.

Used as a mixin for GeoServerMainDialog, which provides `_open_workspace_from_row`
and `_unwrap` (toolbelt/payload.py) to every tab.

A coverage store is to rasters what a datastore is to tables, with one twist:
creating the store does not publish anything (except for an ImageMosaic built
from a directory, which auto-discovers its coverages, and a raster uploaded
from this QGIS project, which GeoServer publishes on arrival), so a new store
usually starts with zero coverages and the *Publish* row action turns one into
a layer.
"""

import shutil
import tempfile
from pathlib import Path

from qgis.core import Qgis
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtWidgets import QDialog

from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog
from geoserver_manager.gui.scope import PENDING

# Every path this tab builds quotes its segments, as the cascaded tab does;
# _q's docstring says why, and when it goes.
from geoserver_manager.gui.tab_cascaded import _q
from geoserver_manager.toolbelt.payload import bbox_text, changed, keyword_list, words
from geoserver_manager.toolbelt.qgis_export import (
    export_to_geotiff,
    geoserver_name,
    local_geotiff_path,
    reprojection_target,
    require_crs,
)
from geoserver_manager.toolbelt.rest import PartlySaved, raw_rest

# Store types offered by the Add form: the ones GeoServer ships without an
# extension (NetCDF, GRIB and the like need one), plus the upload of a raster
# from this project, which is a GeoTIFF store GeoServer fills itself.
GEOTIFF = "GeoTIFF"
ARCGRID = "ArcGrid"
WORLDIMAGE = "WorldImage"
COG = "GeoTIFF (COG)"
MOSAIC_DIRECTORY = "ImageMosaic (server directory)"
MOSAIC_ZIP = "ImageMosaic (properties ZIP)"
QGIS_RASTER = "A raster layer from this QGIS project"
STORE_TYPES = (
    GEOTIFF,
    COG,
    ARCGRID,
    WORLDIMAGE,
    MOSAIC_DIRECTORY,
    MOSAIC_ZIP,
    QGIS_RASTER,
)


def _store_type_label(kind):
    """A store type as the Add form shows it: GeoServer's own names stay,
    the plugin's descriptions are translated."""
    return {
        COG: translate("CoverageStoreTabMixin", "GeoTIFF (COG)"),
        MOSAIC_DIRECTORY: translate(
            "CoverageStoreTabMixin", "ImageMosaic (server directory)"
        ),
        MOSAIC_ZIP: translate("CoverageStoreTabMixin", "ImageMosaic (properties ZIP)"),
        QGIS_RASTER: translate(
            "CoverageStoreTabMixin", "A raster layer from this QGIS project"
        ),
    }.get(kind, kind)


# A cloud-optimised GeoTIFF is a GeoTIFF store plus this metadata entry; the
# library turns {"cogSettings": …} into GeoServer's {"@key": "CogSettings.Key"}
# wrapper itself. Needs GeoServer's COG extension installed server-side.
_COG_METADATA = {"cogSettings": {"rangeReaderSettings": "HTTP"}}

# What the store edit form may change, as (form key, REST key).
_STORE_EDITS = (
    ("name", "name"),
    ("url", "url"),
    ("enabled", "enabled"),
    ("description", "description"),
)

# Fields of the Add form that belong to one store type only.
_TYPE_FIELDS = {
    GEOTIFF: ("url",),
    ARCGRID: ("url",),
    WORLDIMAGE: ("url",),
    COG: ("url",),
    MOSAIC_DIRECTORY: ("directory",),
    MOSAIC_ZIP: ("zip",),
    QGIS_RASTER: ("qgis_layer", "replace", "title", "abstract"),
}
_TYPED_KEYS = tuple(
    dict.fromkeys(key for keys in _TYPE_FIELDS.values() for key in keys)
)


# Every user-visible string in this file goes through translate() with this
# file's own class as the context. self.tr() cannot: pylupdate extracts it
# under CoverageStoreTabMixin, but at runtime self.tr is QObject.tr with the context of the
# *instance's* class, GeoServerMainDialog. QDialog precedes the mixins in the
# MRO, so every lookup would miss. A wrapper function would not be extracted
# at all (pylupdate only understands a literal context), hence the repetition.
translate = QCoreApplication.translate


class CoverageStoreTabMixin:
    """Mixin that adds coverage-store methods to the main dialog."""

    # -- Listing ---------------------------------------------------------------

    def _load_coverage_stores(self):
        """Arm the Coverage Stores tab, then fetch its rows in the background."""
        self._setup_add_button(
            translate("CoverageStoreTabMixin", "Add a Coverage Store"),
            translate(
                "CoverageStoreTabMixin",
                "Create a coverage store from a GeoTIFF, a COG, an ImageMosaic, or a raster layer of this project, uploaded and published",
            ),
            self._add_coverage_store,
        )
        self._setup_delete_selected_button(self._delete_selected_coverage_stores)
        self._name_click_callback = self._show_coverage_store_info
        self._extra_click_callbacks = {
            translate(
                "CoverageStoreTabMixin", "Workspace"
            ): self._open_workspace_from_row
        }
        self._row_actions = [
            (
                "browse-resources",
                translate("CoverageStoreTabMixin", "Coverages"),
                self._show_coverages,
            ),
            (
                "publish-layer",
                translate("CoverageStoreTabMixin", "Publish a coverage"),
                self._publish_coverage,
            ),
            (
                "update-from-source",
                translate("CoverageStoreTabMixin", "Reset"),
                self._reset_coverage_store,
                translate("CoverageStoreTabMixin", "GeoServer re-reads the store"),
            ),
            (
                "delete",
                translate("CoverageStoreTabMixin", "Delete"),
                self._delete_coverage_store,
                translate(
                    "CoverageStoreTabMixin",
                    "Deletes its coverages and their layers too",
                ),
            ),
        ]
        self._setup_table(
            [
                translate("CoverageStoreTabMixin", "Name"),
                translate("CoverageStoreTabMixin", "Workspace"),
                translate("CoverageStoreTabMixin", "Type"),
                translate("CoverageStoreTabMixin", "Coverages"),
                self.actions_column_label(),
            ]
        )
        self._row_detail = self._coverage_store_cells
        self._detail_columns = (2, 3)
        self._start_load(
            translate("CoverageStoreTabMixin", "Failed to load coverage stores"),
            self._fetch_coverage_store_rows,
        )

    def _fetch_coverage_store_rows(self, task=None):
        """(rows, failures) for the Coverage Stores table. Runs in a worker."""
        failures = []
        stores = []
        ws_names = self._get_workspace_names()
        for ws_name, (names, error) in zip(
            ws_names, self._fan_out(self._coverage_store_names, ws_names, task)
        ):
            if error:
                failures.append((ws_name, error))
                continue
            stores.extend((ws_name, name) for name in names)

        # Type and Coverages follow for the page shown: two GETs a store.
        rows = [[name, ws_name, PENDING, PENDING] for ws_name, name in stores]
        return rows, failures

    def _coverage_store_names(self, workspace_name):
        """Coverage-store names of one workspace. Raises on HTTP errors.

        TODO(#1): upstream as get_coverage_stores(ws); the library has
        get_coverage_store() for one store but no call that lists them, so the
        whole tab would have nothing to show. Workaround: GET the collection.
        """
        path = self.gs.rest_service.rest_endpoints.coveragestores(_q(workspace_name))
        payload = self._raw_rest("get", path).json()
        return sorted(
            self._name_of(store)
            for store in self._unwrap(payload, "coverageStores", "coverageStore")
        )

    def _coverage_store_cells(self, row):
        """The Type and Coverages cells of one row. Runs in a worker."""
        store_type, coverage_count = self._coverage_store_summary(row[1], row[0])
        return store_type, str(coverage_count)

    def _coverage_store_summary(self, workspace_name, name):
        """(type, number of published coverages). Raises on HTTP errors."""
        detail = self._coverage_store_detail(workspace_name, name)
        published = self._published_coverage_names(workspace_name, name)
        return detail.get("type", "-"), len(published)

    # -- One store -------------------------------------------------------------

    def _coverage_store_detail(self, workspace_name, name):
        """One coverage store, as GeoServer stores it.

        TODO(#1): upstream: get_coverage_store() exists, but its model drops
        the store's description, and CoverageStore.put_payload() raises
        NotImplementedError, so there is no way to edit a store either.
        Workaround: GET the store path.
        """
        path = self.gs.rest_service.rest_endpoints.coveragestore(
            _q(workspace_name), _q(name)
        )
        payload = self._raw_rest("get", path).json()
        return payload.get("coverageStore") or {}

    def _published_coverage_names(self, workspace_name, store_name):
        """The store's coverages that are published as layers.

        TODO(#1): upstream as get_coverages(ws, store, list="configured").
        The library hardcodes `list=all`, which returns every coverage the
        store can expose, published or not. Both are needed: "all" to offer
        publish candidates, "configured" to say what is live.
        """
        path = self.gs.rest_service.rest_endpoints.coverages(
            _q(workspace_name), _q(store_name)
        )
        payload = self._raw_rest("get", path, params={"list": "configured"}).json()
        return sorted(
            self._name_of(coverage)
            for coverage in self._unwrap(payload, "coverages", "coverage")
        )

    def _coverage_store_info_fields(self):
        """The store edit form: what a partial PUT changes, then the coverages.

        Measured on 2.28.5: the PUT merges (type and the rest stay), and a
        rename keeps the coverages and their layers.
        """
        return [
            {
                "key": "name",
                "label": translate("CoverageStoreTabMixin", "Name"),
                "type": "text",
                "required": True,
            },
            {
                "key": "workspace",
                "label": translate("CoverageStoreTabMixin", "Workspace"),
                "type": "text",
                "read_only": True,
            },
            {
                "key": "type",
                "label": translate("CoverageStoreTabMixin", "Type"),
                "type": "text",
                "read_only": True,
            },
            {
                "key": "url",
                "label": translate("CoverageStoreTabMixin", "URL"),
                "type": "text",
                "required": True,
                "help": translate(
                    "CoverageStoreTabMixin",
                    "A path on the GeoServer machine (file:...) or a URL, as "
                    "GeoServer reaches it.",
                ),
            },
            {
                "key": "enabled",
                "label": translate("CoverageStoreTabMixin", "Enabled"),
                "type": "checkbox",
            },
            {
                "key": "description",
                "label": translate("CoverageStoreTabMixin", "Description"),
                "type": "textarea",
                "max_height": 72,
            },
            {
                "key": "coverages",
                "label": translate("CoverageStoreTabMixin", "Published coverages"),
                "type": "textarea",
                "read_only": True,
                "group": translate("CoverageStoreTabMixin", "Coverages"),
                "help": translate(
                    "CoverageStoreTabMixin",
                    "Open the Coverages action for one coverage's details.",
                ),
            },
        ]

    @staticmethod
    def _coverage_store_form_values(detail, published):
        """Prefill for the store dialog. Pure, so it is unit-testable."""
        return {
            "name": detail.get("name", ""),
            "workspace": (detail.get("workspace") or {}).get("name", ""),
            "type": detail.get("type", ""),
            "url": detail.get("url", ""),
            "description": detail.get("description", ""),
            "enabled": bool(detail.get("enabled", True)),
            "coverages": "\n".join(published) or "-",
        }

    def _show_coverage_store_info(self, row_data):
        """Open a coverage store to edit its name, URL, state and description."""
        name, ws_name = row_data[0], row_data[1]
        fetched = self._fetch(
            lambda: (
                self._coverage_store_detail(ws_name, name),
                self._published_coverage_names(ws_name, name),
            ),
            translate(
                "CoverageStoreTabMixin", "Failed to load coverage store '{}'"
            ).format(name),
        )
        if fetched is None:
            return
        detail, published = fetched
        before = self._coverage_store_form_values(detail, published)
        dlg = ResourceFormDialog(
            title=translate("CoverageStoreTabMixin", "Coverage Store '{}'").format(
                name
            ),
            fields=self._coverage_store_info_fields(),
            values=before,
            parent=self,
            validate=self._form_check(
                lambda values: self._check_store_rename(ws_name, name, values["name"])
            ),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        after = dlg.get_values()
        body = changed(before, after, _STORE_EDITS)
        if not body:
            return
        if self._run_action(
            lambda: self._wait_for_save(
                lambda: self._save_coverage_store(ws_name, name, body)
            ),
            translate(
                "CoverageStoreTabMixin", "Failed to save coverage store '{}'"
            ).format(name),
        ):
            saved = after["name"]
            self.show_success_message(
                translate("CoverageStoreTabMixin", "Coverage store '{}' saved.").format(
                    saved
                )
            )
            self._warn_if_store_unreachable(
                saved, lambda: self._fetch_list(self.gs.get_coverages, ws_name, saved)
            )
            self._load_coverage_stores()

    def _save_coverage_store(self, ws_name, name, body):
        """One merging PUT on the store. Raises on a bad or taken name.

        TODO(#1): CoverageStore.put_payload() raises NotImplementedError and
        there is no update in the library (row 23); a partial PUT merges
        (measured on 2.28.5).
        """
        self._check_store_rename(ws_name, name, body.get("name", name))
        path = self.gs.rest_service.rest_endpoints.coveragestore(_q(ws_name), _q(name))
        self._raw_rest("put", path, json={"coverageStore": body})

    def _check_store_rename(self, ws_name, name, new_name):
        """Refuse a new name a URL would eat, or one taken. Reads only: the
        edit form runs it before it closes, the save again."""
        if new_name != name:
            self._require_safe_name(new_name)
            self._refuse_existing_store(ws_name, new_name)

    def _reset_coverage_store(self, row_data):
        """Make GeoServer re-read the store: a replaced file, a changed mosaic.

        TODO(#1): no reset in the library (row 54): POST .../reset (measured).
        """
        name, ws_name = row_data[0], row_data[1]
        path = self.gs.rest_service.rest_endpoints.coveragestore(_q(ws_name), _q(name))
        if self._run_action(
            lambda: self._wait_for_save(
                lambda: self._raw_rest("post", path.removesuffix(".json") + "/reset")
            ),
            translate("CoverageStoreTabMixin", "Failed to reset '{}'").format(name),
        ):
            self.show_success_message(
                translate(
                    "CoverageStoreTabMixin", "'{}' reset: GeoServer re-reads it."
                ).format(name)
            )

    # -- Coverages -------------------------------------------------------------

    def _coverage_detail(self, workspace_name, store_name, name):
        """One coverage, as GeoServer stores it.

        TODO(#1): upstream: get_coverage() exists, but Coverage.asdict()
        drops nativeBoundingBox, latLonBoundingBox and keywords, which is most
        of what a detail view is for. Workaround: GET the coverage path.
        """
        path = self.gs.rest_service.rest_endpoints.coverage(
            _q(workspace_name), _q(store_name), _q(name)
        )
        payload = self._raw_rest("get", path).json()
        return payload.get("coverage") or {}

    @classmethod
    def _coverage_form_values(cls, detail):
        """Prefill for the coverage viewer. Pure, so it is unit-testable."""
        bounds = bbox_text(detail.get("nativeBoundingBox"))

        # GeoServer writes the grid range's "high" as the exclusive upper bound,
        # so the size is high - low: sfdem reports "0 0" / "634 477" and gdalinfo
        # says 634 x 477, and Arc_Sample's "720 360" is a half-degree world grid.
        grid_range = (detail.get("grid") or {}).get("range") or {}
        size = ""
        try:
            low = [int(value) for value in str(grid_range["low"]).split()]
            high = [int(value) for value in str(grid_range["high"]).split()]
            size = " × ".join(str(h - lo) for lo, h in zip(low, high))
        except (KeyError, ValueError):
            pass

        dimensions = (detail.get("dimensions") or {}).get("coverageDimension") or []
        if isinstance(dimensions, dict):  # a single band is not a list
            dimensions = [dimensions]
        bands = "\n".join(
            "{}{}".format(
                band.get("name", "?"),
                (
                    "  ({min} … {max})".format(**band["range"])
                    if isinstance(band.get("range"), dict)
                    and {"min", "max"} <= set(band["range"])
                    else ""
                ),
            )
            for band in dimensions
            if isinstance(band, dict)
        )
        keywords = keyword_list(detail.get("keywords"))

        return {
            "native_name": detail.get("nativeName", ""),
            "title": detail.get("title", ""),
            "srs": detail.get("srs", ""),
            "native_format": detail.get("nativeFormat", ""),
            "size": size,
            "bounds": bounds,
            "keywords": ", ".join(keywords),
            # The abstract is what the capabilities carry; "description" is
            # GeoServer's own "Generated from <file>" note on a configured
            # coverage, worth showing only when nobody wrote an abstract.
            "abstract": detail.get("abstract") or detail.get("description") or "",
            "bands": bands or "-",
        }

    def _coverage_fields(self, names):
        """Field definitions for the coverage viewer: a picker plus its details."""
        fields = [
            {
                "key": "coverage",
                "label": translate("CoverageStoreTabMixin", "Coverage"),
                "type": "combo",
                "options": list(names),
            }
        ]
        fields += [
            {"key": key, "label": label, "type": "text", "read_only": True}
            for key, label in (
                ("native_name", translate("CoverageStoreTabMixin", "Native name")),
                ("title", translate("CoverageStoreTabMixin", "Title")),
                ("srs", translate("CoverageStoreTabMixin", "SRS")),
                ("native_format", translate("CoverageStoreTabMixin", "Native format")),
                ("enabled", translate("CoverageStoreTabMixin", "Enabled")),
                ("size", translate("CoverageStoreTabMixin", "Size in pixels")),
                ("bounds", translate("CoverageStoreTabMixin", "Bounds")),
                ("keywords", translate("CoverageStoreTabMixin", "Keywords")),
            )
        ]
        fields += [
            {
                "key": "abstract",
                "label": translate("CoverageStoreTabMixin", "Abstract"),
                "type": "textarea",
                "read_only": True,
            },
            {
                "key": "bands",
                "label": translate("CoverageStoreTabMixin", "Bands"),
                "type": "textarea",
                "read_only": True,
                "group": translate("CoverageStoreTabMixin", "Bands"),
            },
        ]
        return fields

    def _coverage_values(self, workspace_name, store_name, name):
        """The viewer's values for one coverage; None once reported."""
        detail = self._fetch(
            lambda: self._coverage_detail(workspace_name, store_name, name),
            translate("CoverageStoreTabMixin", "Failed to load coverage '{}'").format(
                name
            ),
        )
        if detail is None:
            return None
        return dict(
            self._coverage_form_values(detail),
            enabled=self._yes_no(detail.get("enabled", True)),
        )

    def _show_coverages(self, row_data):
        """List the store's published coverages and view one at a time."""
        store_name, ws_name = row_data[0], row_data[1]
        published = self._fetch(
            lambda: self._published_coverage_names(ws_name, store_name),
            translate(
                "CoverageStoreTabMixin", "Failed to load the coverages of '{}'"
            ).format(store_name),
        )
        if published is None:
            return
        if not published:
            self.show_warning_message(
                translate(
                    "CoverageStoreTabMixin",
                    "'{}' has no published coverage yet. Use Publish a coverage.",
                ).format(store_name)
            )
            return

        dlg = ResourceFormDialog(
            title=translate("CoverageStoreTabMixin", "Coverages of '{}'").format(
                store_name
            ),
            description=translate(
                "CoverageStoreTabMixin", "Read-only view of what this store publishes."
            ),
            fields=self._coverage_fields(published),
            parent=self,
        )
        dlg.hide_save_button()
        self._wire_picker(
            dlg,
            "coverage",
            published[0],
            lambda name: self._coverage_values(ws_name, store_name, name),
        )
        dlg.exec()

    # -- Publish ---------------------------------------------------------------

    def _publishable_coverages(self, workspace_name, store_name):
        """Coverages the store exposes that are not published yet.

        The library's get_coverages() answers `list=all`, the store's native
        names; `list=configured` answers the published names. Measured on
        2.28.5: "sfdem" published as "elev" keeps nativeName "sfdem" and stays
        in `list=all`, so each published coverage is read for the native name
        it came from (one GET each, usually one or two).
        """
        every = [
            self._name_of(coverage)
            for coverage in self._fetch_list(
                self.gs.get_coverages, workspace_name, store_name
            )
        ]
        published = set()
        for name in self._published_coverage_names(workspace_name, store_name):
            detail = self._check(
                self.gs.get_coverage(_q(workspace_name), _q(store_name), _q(name))
            )
            published.add(
                detail.get("nativeCoverageName") or detail.get("nativeName") or name
            )
        return [name for name in every if name not in published]

    def _publish_coverage(self, row_data):
        """Publish one of the store's coverages as a layer."""
        store_name, ws_name = row_data[0], row_data[1]
        candidates = self._fetch(
            lambda: self._publishable_coverages(ws_name, store_name),
            translate(
                "CoverageStoreTabMixin", "Failed to list the coverages of '{}'"
            ).format(store_name),
        )
        if candidates is None:
            return
        if not candidates:
            self.show_warning_message(
                translate(
                    "CoverageStoreTabMixin",
                    "Every coverage of '{}' is already published.",
                ).format(store_name)
            )
            return

        dlg = ResourceFormDialog(
            title=translate(
                "CoverageStoreTabMixin", "Publish a coverage of '{}'"
            ).format(store_name),
            description=translate(
                "CoverageStoreTabMixin",
                "Publishing a coverage makes it a layer. Leave the layer name "
                "empty to reuse the coverage's own name.",
            ),
            fields=[
                {
                    "key": "native_name",
                    "label": translate("CoverageStoreTabMixin", "Coverage"),
                    "type": "combo",
                    "options": candidates,
                    "required": True,
                },
                {
                    "key": "name",
                    "label": translate("CoverageStoreTabMixin", "Layer name"),
                    "type": "text",
                },
                {
                    "key": "title",
                    "label": translate("CoverageStoreTabMixin", "Title"),
                    "type": "text",
                },
            ],
            parent=self,
            ok_label=translate("CoverageStoreTabMixin", "Publish"),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        values = dlg.get_values()
        published_name = values["name"] or values["native_name"]

        def publish():
            self._require_safe_name(published_name)
            self._check(
                self.gs.create_coverage(
                    ws_name,
                    store_name,
                    published_name,
                    title=values["title"] or None,
                    native_name=values["native_name"],
                )
            )

        if self._run_action(
            lambda: self._wait_for_save(publish),
            translate("CoverageStoreTabMixin", "Failed to publish '{}'").format(
                values["native_name"]
            ),
        ):
            self.show_success_message(
                translate("CoverageStoreTabMixin", "'{}' published as a layer.").format(
                    published_name
                )
            )
            self._load_coverage_stores()

    # -- Create ----------------------------------------------------------------

    def _coverage_store_fields(self, workspace_names):
        """Field definitions for the Add form."""
        return [
            {
                "key": "name",
                "label": translate("CoverageStoreTabMixin", "Name"),
                "type": "text",
                "required": True,
            },
            {
                "key": "workspace",
                "label": translate("CoverageStoreTabMixin", "Workspace"),
                "type": "combo",
                "options": list(workspace_names),
                "required": True,
            },
            {
                "key": "type",
                "label": translate("CoverageStoreTabMixin", "Type"),
                "type": "combo",
                "options": [(_store_type_label(kind), kind) for kind in STORE_TYPES],
                "default": GEOTIFF,
            },
            {
                "key": "url",
                "label": translate("CoverageStoreTabMixin", "URL"),
                "type": "text",
                "required": True,
                "placeholder": "file:data/sf/sfdem.tif",
                "help": translate(
                    "CoverageStoreTabMixin",
                    "A path on the GeoServer machine (file:…) or, for a COG, an http(s):// or s3:// URL. The server resolves the path, not QGIS.",
                ),
            },
            {
                "key": "directory",
                "label": translate("CoverageStoreTabMixin", "Directory"),
                "type": "text",
                "required": True,
                "visible": False,
                "placeholder": "/opt/geoserver_data/coverages/my_mosaic",
                "help": translate(
                    "CoverageStoreTabMixin",
                    "A directory on the GeoServer machine holding the granules. GeoServer discovers and publishes its coverages itself.",
                ),
            },
            {
                "key": "zip",
                "label": translate("CoverageStoreTabMixin", "Properties ZIP"),
                "type": "file",
                "required": True,
                "visible": False,
                "filter": translate(
                    "CoverageStoreTabMixin", "ZIP (*.zip);;All files (*)"
                ),
                "help": translate(
                    "CoverageStoreTabMixin",
                    "A ZIP holding indexer.properties, datastore.properties and at least 1 granule; GeoServer refuses a properties-only archive, and publishes nothing yet. Give the indexer a Name nobody used before: a deleted mosaic store leaves its granule index table behind, and a re-used name picks it up.",
                ),
            },
            {
                "key": "qgis_layer",
                "label": translate("CoverageStoreTabMixin", "QGIS layer"),
                "type": "layer",
                "raster_files": True,
                # The upload declares it: a raster is never reprojected.
                "show_crs": True,
                "required": True,
                "visible": False,
                "help": translate(
                    "CoverageStoreTabMixin",
                    "File-based rasters of this project. The plugin writes the layer to a GeoTIFF (a copy, not a link), which can take a moment, then uploads it in the background. GeoServer publishes it under the store's name.",
                ),
            },
            {
                "key": "replace",
                "label": translate(
                    "CoverageStoreTabMixin", "Replace it if it already exists"
                ),
                "type": "checkbox",
                "default": False,
                "visible": False,
            },
            {
                "key": "title",
                "label": translate("CoverageStoreTabMixin", "Title"),
                "type": "text",
                "visible": False,
                "placeholder": translate("CoverageStoreTabMixin", "Optional"),
            },
            {
                "key": "abstract",
                "label": translate("CoverageStoreTabMixin", "Abstract"),
                "type": "textarea",
                "visible": False,
                "placeholder": translate("CoverageStoreTabMixin", "Optional"),
            },
        ]

    def _on_store_type_changed(self, dlg, store_type):
        """Show only the fields the chosen store type needs."""
        wanted = _TYPE_FIELDS.get(store_type, ())
        for key in _TYPED_KEYS:
            dlg.set_field_visible(key, key in wanted)
        if store_type == QGIS_RASTER:
            self._prefill_publish_name(dlg, dlg.get_widget("qgis_layer").currentLayer())

    def _add_coverage_store(self):
        """Create a coverage store."""
        workspace_names = self._fetch(
            self._get_workspace_names,
            translate("CoverageStoreTabMixin", "Failed to load the workspaces"),
        )
        if workspace_names is None:
            return
        if not workspace_names:
            # The other Add forms say this up front, rather than after Create.
            self.show_warning_message(
                translate(
                    "CoverageStoreTabMixin",
                    "No workspaces available. Create a workspace first.",
                )
            )
            return

        dlg = ResourceFormDialog(
            title=translate("CoverageStoreTabMixin", "Add a Coverage Store"),
            description=translate(
                "CoverageStoreTabMixin",
                "A coverage store is a source of rasters. Creating it publishes nothing, with 2 exceptions. An ImageMosaic from a directory discovers its coverages itself, and GeoServer publishes a raster uploaded from this project as a layer on arrival.",
            ),
            fields=self._coverage_store_fields(workspace_names),
            parent=self,
            ok_label=translate("CoverageStoreTabMixin", "Create"),
            validate=self._form_check(self._check_new_coverage_store),
        )
        dlg.on_value_changed(
            "type", lambda store_type: self._on_store_type_changed(dlg, store_type)
        )
        dlg.get_widget("qgis_layer").layerChanged.connect(
            lambda layer: self._prefill_publish_name(dlg, layer)
        )
        self._on_store_type_changed(dlg, GEOTIFF)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        values = dlg.get_values()
        if values["type"] == QGIS_RASTER:
            # The checks and the export raise here; the upload reports itself.
            self._run_action(
                lambda: self._publish_qgis_raster(values),
                translate(
                    "CoverageStoreTabMixin", "Failed to publish raster '{}'"
                ).format(geoserver_name(values["name"])),
            )
            return
        if values["type"] == MOSAIC_ZIP:
            self._upload_mosaic_zip(values)
            return
        if self._run_action(
            lambda: self._wait_for_save(
                lambda: self._create_coverage_store_from_values(values)
            ),
            translate(
                "CoverageStoreTabMixin", "Failed to create coverage store '{}'"
            ).format(values["name"]),
        ):
            self.show_success_message(
                translate(
                    "CoverageStoreTabMixin", "Coverage store '{}' created."
                ).format(values["name"])
            )
            self._warn_if_cog_settings_dropped(values)
            self._load_coverage_stores()

    def _warn_if_cog_settings_dropped(self, values):
        """Say so when GeoServer keeps the store but throws the COG settings away.

        Store metadata it does not understand is dropped silently (verified on
        2.28.5 with both the object and the array payload shape), so without
        GeoServer's COG extension the store ends up a plain GeoTIFF that reads
        whole files instead of ranges. That still works, so it is a warning
        rather than a failure, but it must not pass unmentioned.
        """
        if values["type"] != COG:
            return
        try:
            detail = self._wait_for(
                lambda: self._coverage_store_detail(values["workspace"], values["name"])
            )
        except Exception:  # best effort (or Cancel): the store is already created
            return
        if not (detail.get("metadata") or {}):
            self.show_warning_message(
                translate(
                    "CoverageStoreTabMixin",
                    "GeoServer created '{}' but dropped the COG settings, so it reads whole files instead of ranges. Check that the COG extension is installed on the server.",
                ).format(values["name"])
            )

    def _create_coverage_store_from_values(self, values):
        """Create a store through the library, refusing an existing name.

        The upload of a project raster is not here: it streams in a task, see
        _publish_qgis_raster.
        """
        name, ws_name, store_type = values["name"], values["workspace"], values["type"]
        self._require_safe_name(name)
        # create_coverage_store POSTs to the collection, and GeoServer answers
        # 409 for a name in use, but the message is clearer from here, and the
        # mosaic calls are PUTs, which overwrite the store instead.
        self._refuse_existing_store(ws_name, name)

        if store_type == MOSAIC_DIRECTORY:
            self._check(
                self.gs.create_imagemosaic_store_from_directory(
                    ws_name, name, values["directory"]
                )
            )
        else:
            self._check(
                self.gs.create_coverage_store(
                    ws_name,
                    name,
                    values["url"],
                    # A COG is a GeoTIFF store with a metadata entry.
                    type=GEOTIFF if store_type == COG else store_type,
                    metadata=_COG_METADATA if store_type == COG else None,
                )
            )

    def _upload_mosaic_zip(self, values):
        """Create an ImageMosaic store from a properties ZIP, streamed in a task.

        The archive holds granules, so it can be hundreds of MB: it goes through
        _upload_file like the GeoTIFF, with progress and Cancel, instead of
        being read into memory under the wait cursor.

        TODO(#1): create_imagemosaic_store_from_properties_zip() takes bytes
        only. A file-like body would let the library stream it. Until then this
        is its PUT (…/file.imagemosaic?configure=none, application/zip) done raw.
        """
        if not self._upload_slot_free():
            return
        name, ws_name = values["name"], values["workspace"]
        failure = translate(
            "CoverageStoreTabMixin", "Failed to create coverage store '{}'"
        ).format(name)

        def check():
            self._require_safe_name(name)
            self._refuse_existing_store(ws_name, name)
            return Path(values["zip"])

        source = self._fetch(check, failure)
        if source is None:
            return
        endpoints = self.gs.rest_service.rest_endpoints

        def created(_result):
            self.show_success_message(
                translate(
                    "CoverageStoreTabMixin", "Coverage store '{}' created."
                ).format(name)
            )
            self._reload_current_tab()

        def cancelled(_task):
            # Never a Replace (a taken name is refused), so the GeoTIFF's
            # "store without its file" report does not apply. What an aborted
            # archive leaves is not measured: say where to look.
            self.show_warning_message(
                translate(
                    "CoverageStoreTabMixin",
                    "Upload of '{}' cancelled. Check the Coverage Stores tab for "
                    "what was left.",
                ).format(name)
            )

        self._upload_file(
            failure,
            self.gs.rest_service.rest_client,
            endpoints.coveragestore(_q(ws_name), _q(name), "file", "imagemosaic"),
            source,
            {"configure": "none"},
            {"Content-Type": "application/zip", "Accept": "application/json"},
            created,
            cancelled,
        )

    def _publish_qgis_raster(self, values, layer=None, on_done=None):
        """Upload a project raster as a GeoTIFF store and publish its coverage.

        One request does it all (measured on 2.28.5): GeoServer saves the file
        as data/{ws}/{store}/{store}.geotiff, creates a GeoTIFF store and, with
        configure=first&coverageName, configures one coverage of that name,
        published as a layer with the SRS and bounds read from the file. A PUT
        to an existing store replaces the file and re-reads the coverage, which
        is all *Replace* needs. The data is copied: later edits in QGIS do not
        reach it, and deleting the store leaves the file in the data directory.

        The layer, its CRS, the name check and the export happen here on the
        GUI thread (a live QGIS layer, invariant 9), and raise into the
        caller's _run_action: a Cancel during the checks is an Abandoned, which
        stops a batch. The PUTs then stream in a task through _upload_file,
        with progress and Cancel. `layer` is given when the Layers tab's
        *Publish a Layer* routes a raster here; else it is the one the
        coverage-store form picked.

        TODO(#1): upstream as create_coverage_store_from_file(ws, name, path,
        coverage_name=None). create_coverage_store() only points at a path
        already on the server, so the upload is a raw PUT of
        .../coveragestores/{name}/file.geotiff (row 31).
        """
        if not self._upload_slot_free():
            return False
        ws_name = values["workspace"]
        # Also the coverage's and the layer's name, so it has to be one a layer
        # can carry.
        name = geoserver_name(values["name"])
        failure = translate(
            "CoverageStoreTabMixin", "Failed to publish raster '{}'"
        ).format(name)
        source, folder = self._prepare_qgis_raster(ws_name, name, values, layer)
        client = self.gs.rest_service.rest_client
        endpoints = self.gs.rest_service.rest_endpoints
        upload_path = endpoints.coveragestore(_q(ws_name), _q(name), "file", "geotiff")
        metadata = {
            key: values[key] for key in ("title", "abstract") if values.get(key)
        }
        keywords = words(values.get("keywords"))
        if keywords:
            # The feature type's shape; a partial coverage PUT merges it too.
            metadata["keywords"] = {"string": keywords}
        metadata_path = endpoints.coverage(_q(ws_name), _q(name), _q(name))
        partly = translate(
            "CoverageStoreTabMixin",
            "Raster '{}' is published, but its title, abstract and keywords "
            "could not be set",
        ).format(name)
        landed = []

        def after(client):
            landed.append(True)  # the file is stored
            if metadata:
                # A partial coverage PUT merges (measured), so the SRS, bounds
                # and grid read from the file stay. TODO(#1): update_coverage(
                # ws, store, name, title=…, abstract=…); create_coverage()
                # POSTs a new one (row 32).
                try:
                    raw_rest(client, "put", metadata_path, json={"coverage": metadata})
                except Exception as error:  # the raster itself is published
                    raise PartlySaved(f"{partly}: {self._error_text(error)}") from error

        def published(_result):
            self.show_success_message(
                translate(
                    "CoverageStoreTabMixin",
                    "Raster '{}' uploaded and published as a layer.",
                ).format(name)
            )
            # The user may have moved to another tab while it uploaded.
            self._reload_current_tab()

        return self._upload_file(
            failure,
            client,
            upload_path,
            source,
            {"configure": "first", "coverageName": name},
            {"Content-Type": "image/tiff"},
            published,
            self._store_upload_cancelled(ws_name, name),
            folder=folder,
            after=after,
            on_done=self._store_upload_ended(
                ws_name, name, values.get("replace"), landed, on_done
            ),
        )

    def _check_new_coverage_store(self, values):
        """What the Add form would be refused for, before anything is sent: a
        name a URL would eat, a store that exists (a raster upload may
        replace it, Replace ticked), a layer of that name elsewhere. Reads
        only: the form runs it before it closes."""
        name, ws_name = values["name"], values["workspace"]
        self._require_safe_name(name)
        if values["type"] != QGIS_RASTER:
            self._refuse_existing_store(ws_name, name)
            return
        # The upload's name, the one a layer can carry, not the typed one:
        # checked as typed, "My DEM" passed while "My_DEM" was refused after
        # the form had closed, with everything typed into it.
        self._check_raster_target(ws_name, geoserver_name(name), values.get("replace"))

    def _check_raster_target(self, ws_name, name, replace):
        """Refuse the store, and the layer, a raster upload would land on.

        Reads only: the form runs it before it closes, the upload again.
        """
        # The library's reads take the workspace raw: "sf#x" reads "sf".
        self._require_safe_name(ws_name)
        if not replace:
            self._refuse_existing_store(
                ws_name,
                name,
                translate("CoverageStoreTabMixin", "Tick Replace to overwrite it."),
            )
        self._refuse_layer_clash(ws_name, name, replace, "coverage", "GeoTIFF")

    def _refuse_existing_store(self, ws_name, name, hint=""):
        """Raise when the store exists: its creators upsert, or PUT over it.

        A workspace a path cannot carry is refused first: the library builds
        this read's path from the raw name, and "sf#x" read "sf".
        """
        self._require_safe_name(ws_name)
        if self._resource_exists(self.gs.get_coverage_store, ws_name, name):
            message = translate(
                "CoverageStoreTabMixin", "Coverage store '{}' already exists in '{}'."
            ).format(name, ws_name)
            raise ValueError(f"{message} {hint}".strip())

    def _store_upload_cancelled(self, ws_name, name):
        """The on_cancel of a store upload: say what the server was left with."""
        return lambda _task: self._report_cancelled_upload(
            translate("CoverageStoreTabMixin", "coverage store"),
            translate("CoverageStoreTabMixin", "Coverage Stores"),
            lambda: self._resource_exists(self.gs.get_coverage_store, ws_name, name),
            name,
        )

    def _store_upload_ended(self, ws_name, name, replace, landed, on_done):
        """The on_done of a raster upload: warn when a Replace did not land.

        A body cut short (a reset, a proxy, a timeout) closes the connection
        as a Cancel does, and a cancelled Replace leaves the store and its
        layer without their file (measured on 2.28.5). The cancel says so
        itself; a failure says it here, and any end after the dialog closed
        says it in the log. `landed` is filled once the PUT succeeded, and
        `on_done` (a batch) runs after.
        """

        def ended(outcome):
            if replace and not landed:
                lost = translate(
                    "CoverageStoreTabMixin",
                    "Upload of '{}' did not finish: the coverage store it was replacing can be left without its data file, and its layer without data. Upload it again with Replace ticked, or delete the coverage store.",
                ).format(name)
                if self._closing:
                    self.log(lost, log_level=Qgis.MessageLevel.Warning)
                elif outcome == "failed":  # a cancel has said it already
                    try:
                        kept = self._wait_for(
                            lambda: self._resource_exists(
                                self.gs.get_coverage_store, ws_name, name
                            )
                        )
                    except Exception:  # unknown, and the warning says "may"
                        kept = None
                    if kept is not False:
                        self.show_warning_message(lost)
            if on_done is not None:
                on_done(outcome)

        return ended

    def _prepare_qgis_raster(self, ws_name, name, values, layer=None):
        """GUI-thread half of the raster upload: the checks, then the file to send.

        Returns (path, temporary folder or None). Refuses, before any request,
        a raster with no file behind it, a layer without a CRS or with one
        GeoServer cannot declare (a raster is uploaded as it is, never
        reprojected), and a taken name unless *Replace* is ticked, because the
        PUT would overwrite the store silently.
        """
        if layer is None:
            layer = values["qgis_layer"]
        if layer.providerType() != "gdal":
            raise ValueError(
                translate(
                    "CoverageStoreTabMixin",
                    "'{}' has no file to upload. A WMS, XYZ or other remote raster "
                    "cannot be published this way.",
                ).format(layer.name())
            )
        require_crs(layer)
        if reprojection_target(layer) is not None:
            raise ValueError(
                translate(
                    "CoverageStoreTabMixin",
                    "'{}' uses a CRS without an EPSG code, which GeoServer cannot declare. Reproject the raster in QGIS first: the plugin uploads rasters as they are.",
                ).format(layer.name())
            )

        # Reads, so off the GUI thread; the form ran it once, before it closed.
        self._wait_for(
            lambda: self._check_raster_target(ws_name, name, values.get("replace"))
        )
        source = local_geotiff_path(layer)
        if source is not None:
            return source, None
        # The export runs here and holds the dialog for a big raster;
        # QgsRasterFileWriterTask is the upgrade path if that ever hurts.
        folder = Path(tempfile.mkdtemp(prefix="gsm_publish_"))
        try:
            return export_to_geotiff(layer, folder / f"{name}.tif"), folder
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    def _delete_coverage_store(self, row_data):
        """Delete a single coverage store after confirmation."""
        self._delete_selected_coverage_stores([row_data])

    def _delete_selected_coverage_stores(self, selected_rows):
        """Delete one or more coverage stores after confirmation."""
        self._delete_many(
            [
                (
                    f"{row[1]}:{row[0]}",
                    lambda name=row[0], ws=row[1]: self._check(
                        self.gs.delete_coverage_store(ws, name)
                    ),
                )
                for row in selected_rows
            ],
            self._load_coverage_stores,
            ask=self._one_or_many(
                translate(
                    "CoverageStoreTabMixin",
                    "Are you sure you want to delete coverage store '{}'?",
                ),
                lambda n: translate(
                    "CoverageStoreTabMixin",
                    "Are you sure you want to delete %n coverage store(s)?",
                    None,
                    n,
                ),
            ),
            done=self._one_or_many(
                translate("CoverageStoreTabMixin", "Coverage store '{}' deleted."),
                lambda n: translate(
                    "CoverageStoreTabMixin", "%n coverage store(s) deleted.", None, n
                ),
            ),
            cascade=translate(
                "CoverageStoreTabMixin",
                "This also deletes its coverages and the layers published from them. The raster files stay on the server.",
            ),
        )
