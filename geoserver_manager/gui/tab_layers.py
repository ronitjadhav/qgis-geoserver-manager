#! python3  # noqa: E265

"""
Layers tab: every published layer, whatever its type: list, inspect,
preview, style, add to QGIS, delete.

Used as a mixin for GeoServerMainDialog.
"""

import math
import re
from urllib.parse import quote, unquote, urlencode, urlsplit

from qgis.core import QgsDataSourceUri, QgsProject, QgsRasterLayer, QgsVectorLayer
from qgis.PyQt.QtCore import QCoreApplication, Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import QApplication, QDialog, QMessageBox

from geoserver_manager.gui.dlg_preview import LayerPreviewDialog, load_error
from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog
from geoserver_manager.gui.scope import PENDING
from geoserver_manager.toolbelt.payload import (
    DIMENSIONS,
    GEOMETRY_TYPES,
    TIME_UNITS,
    VIEW_FIELDS,
    bbox_text,
    dimension_attributes,
    dimension_form_values,
    keyword_list,
    metadata_entries,
    text_of,
    unwrap,
    view_form_values,
    view_of,
    virtual_table,
    with_dimensions,
    with_view,
    words,
)
from geoserver_manager.toolbelt.qgis_export import (
    geoserver_name,
)
from geoserver_manager.toolbelt.rest import PartlySaved
from geoserver_manager.toolbelt.sld import (
    layer_to_sld,
    styleable_project_layers,
)

# The projection policies GeoServer knows, as its REST API spells them.
_PROJECTION_POLICIES = ("FORCE_DECLARED", "REPROJECT_TO_DECLARED", "NONE")

# How a GeoServer layer can be brought into QGIS. WFS gives the actual features
# (editable, stylable in QGIS); WMS/WMTS give rendered images. WMTS goes through
# GeoWebCache, which caches EPSG:900913 and EPSG:4326 for every layer by default.
PROTOCOLS = ("WMS", "WFS", "WMTS")

_WMTS_TILE_MATRIX_SET = "EPSG:900913"
# GeoServer's own Layer Preview page renders at 768 px on the long side.
_PREVIEW_SIZE = 768

# GeoServer's layer types, as /rest/layers writes them. The Type column
# carries them verbatim: they decide which resource a row's actions talk to.
VECTOR, RASTER, WMS, WMTS = "VECTOR", "RASTER", "WMS", "WMTS"


# Every user-visible string in this file goes through translate() with this
# file's own class as the context. self.tr() cannot: pylupdate extracts it
# under LayerTabMixin, but at runtime self.tr is QObject.tr with the context of the
# *instance's* class, GeoServerMainDialog. QDialog precedes the mixins in the
# MRO, so every lookup would miss. A wrapper function would not be extracted
# at all (pylupdate only understands a literal context), hence the repetition.
translate = QCoreApplication.translate


def _kind_label(kind):
    """A layer's type in words, as the Type column shows it.

    The row keeps GeoServer's own ("VECTOR", "WMS"): the row actions read it.
    """
    return {
        VECTOR: translate("LayerTabMixin", "Vector"),
        RASTER: translate("LayerTabMixin", "Raster"),
        WMS: translate("LayerTabMixin", "Cascaded WMS"),
        WMTS: translate("LayerTabMixin", "Cascaded WMTS"),
    }.get(kind, kind)


def _dimension_values(detail):
    """A vector layer's dimensions as the form shows them, and the attributes
    each one can use (`time_choices`, `elevation_choices`)."""
    values = dimension_form_values(detail)
    for dimension in DIMENSIONS:
        values[f"{dimension}_choices"] = dimension_attributes(detail, dimension)
    return values


def _origin(url):
    """scheme://host[:port] of a URL, a default port left out; None without a host."""
    parts = urlsplit(url)
    if not parts.hostname:
        return None
    scheme = parts.scheme.lower()
    port = parts.port if parts.port != {"http": 80, "https": 443}.get(scheme) else None
    return f"{scheme}://{parts.hostname}" + (f":{port}" if port else "")


def _hrefs(node):
    """Every xlink:href under a node of a document xmltodict parsed."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "@xlink:href":
                yield value
            else:
                yield from _hrefs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _hrefs(item)


class LayerTabMixin:
    """Mixin that adds the Layers tab: every published layer, of any type."""

    def _load_layers(self):
        """Arm the Layers tab, then fetch its rows in the background."""
        self._setup_add_button(
            translate("LayerTabMixin", "Publish a Layer"),
            translate(
                "LayerTabMixin",
                "Publish a table of a datastore, or a layer of this QGIS project",
            ),
            self._publish_layer,
        )
        self._setup_delete_selected_button(self._delete_selected_layers)
        self._name_click_callback = self._show_layer_info
        self._extra_click_callbacks = {
            translate("LayerTabMixin", "Workspace"): self._open_workspace_from_row
        }
        # An icon-only button shows its label as tooltip; a tooltip of its own
        # only where the effect is not obvious.
        self._row_actions = [
            (
                "add-to-qgis",
                translate("LayerTabMixin", "Add to QGIS"),
                self._add_layer_to_qgis,
            ),
            (
                "preview-map",
                translate("LayerTabMixin", "Preview"),
                self._preview_layer,
            ),
            (
                "preview-browser",
                translate("LayerTabMixin", "Preview in a browser"),
                self._preview_layer_in_browser,
            ),
            (
                "styles",
                translate("LayerTabMixin", "Set style"),
                self._set_layer_style,
            ),
            (
                "push-style",
                translate("LayerTabMixin", "Push style from QGIS"),
                self._style_from_qgis,
            ),
            (
                "update-from-source",
                translate("LayerTabMixin", "Update from the data"),
                self._update_layer_from_source,
                translate(
                    "LayerTabMixin",
                    "GeoServer re-reads the data and recomputes the bounds",
                ),
            ),
            (
                "delete",
                translate("LayerTabMixin", "Delete"),
                self._delete_layer,
            ),
        ]
        self._setup_table(
            [
                translate("LayerTabMixin", "Name"),
                translate("LayerTabMixin", "Workspace"),
                translate("LayerTabMixin", "Type"),
                translate("LayerTabMixin", "Store"),
                translate("LayerTabMixin", "Default style"),
                self.actions_column_label(),
            ]
        )
        self._path_columns = (0, 1, 3)  # the store is in the resource's path
        self._row_detail = lambda row: self._layer_summary(f"{row[1]}:{row[0]}")
        self._detail_columns = (2, 3, 4)
        self._cell_display = {2: _kind_label}
        self._start_load(
            translate("LayerTabMixin", "Failed to load layers"), self._fetch_layer_rows
        )

    def _fetch_layer_rows(self, task=None):
        """(rows, failures) for the Layers table. Runs in a worker thread.

        GeoServer's own layer list first: every published layer, whatever its
        type (vector, raster, cascaded WMS or WMTS), then one GET per layer,
        fanned out, for the type, the store and the default style. A layer
        whose detail cannot be read still gets a row, with placeholders, and
        a warning names it.
        """
        names = self._all_layer_names()
        # Type, store and style follow for the page shown; a row action
        # fetches them first when they are still pending (_addressable).
        rows = []
        for qualified in names:
            workspace, _, name = qualified.rpartition(":")
            rows.append([name, workspace, PENDING, PENDING, PENDING])
        return rows, []

    def _layers_url(self, qualified_name=None):
        """/rest/layers.json, or one layer's own document under it."""
        base = self.gs.rest_service.rest_endpoints.base_url
        if qualified_name is None:
            return f"{base}/layers.json"
        return f"{base}/layers/{quote(qualified_name, safe=':')}.json"

    def _all_layer_names(self):
        """Every layer's qualified name ("workspace:layer"), from GeoServer's
        own list. Raises on HTTP errors.

        TODO(#1): row 39: the facade has no get_layers(). Walking the
        datastores instead, as this tab did, misses every raster and cascaded
        layer; the workspace-less list is the one place they all appear.
        """
        payload = self._raw_rest("get", self._layers_url()).json()
        return sorted(
            self._name_of(layer) for layer in self._unwrap(payload, "layers", "layer")
        )

    def _layer_summary(self, qualified_name):
        """(type, store, default style) of one layer. Raises on HTTP errors.

        TODO(#1): rest_service.get_layer() exists, but its Layer model keeps
        only the resource's *name*, not its class or href, which is where the
        store comes from, so this reads GeoServer's payload itself.
        """
        payload = self._raw_rest("get", self._layers_url(qualified_name)).json()
        layer = payload.get("layer") if isinstance(payload, dict) else None
        layer = layer if isinstance(layer, dict) else {}
        kind = layer.get("type") or "-"
        store = self._store_from_href((layer.get("resource") or {}).get("href"))
        if store is None and kind == WMTS:
            # GeoServer 2.28.5 writes no href for a wmtsLayer resource, so the
            # store has to be found among the workspace's WMTS stores.
            workspace, _, name = qualified_name.rpartition(":")
            store = self._wmts_store_of(workspace, name)
        style = (layer.get("defaultStyle") or {}).get("name") or "-"
        return kind, store or "-", style

    @staticmethod
    def _store_from_href(href):
        """The store in a resource href: .../workspaces/{ws}/{kind}stores/
        {store}/..., whatever host GeoServer wrote it with (behind a proxy it
        is not the one the plugin talks to, which is why the href is parsed
        and never followed). None when there is no such segment."""
        match = re.search(
            r"/workspaces/[^/]+/(?:data|coverage|wms|wmts)stores/([^/]+)/", href or ""
        )
        return unquote(match.group(1)) if match else None

    def _wmts_store_of(self, workspace_name, layer_name):
        """The WMTS store holding this cascaded layer, or None.

        The workspace's WMTS stores only: the Cascaded Stores tab's listing
        GETs the WMS stores too, which this never reads. Their layers come
        from that tab's helper, reached through the shared dialog class.
        TODO(#1): upstream as get_wmts_stores(ws); the library lists no
        cascaded store.
        """
        endpoints = self.gs.rest_service.rest_endpoints
        payload = self._raw_rest(
            "get", endpoints.wmtsstores(quote(workspace_name, safe=""))
        ).json()
        for store in self._unwrap(payload, "wmtsStores", "wmtsStore"):
            store = self._name_of(store)
            if layer_name in self._cascaded_layer_names(workspace_name, store, WMTS):
                return store
        return None

    @staticmethod
    def _layer_form_values(row_data, detail):
        """Prefill for the detail view, from what GeoServer returned.

        The same for a feature type and a coverage: the fields the edit form
        shows, flattened, and the read-only details beside them.
        """
        # A row is [name, workspace, type, store, default style].
        name, ws_name, ds_name = row_data[0], row_data[1], row_data[3]
        detail = detail if isinstance(detail, dict) else {}

        # The library's FeatureType.asdict() normalises keywords to a list and
        # attributes to a list; raw REST wraps them ({"string": […]},
        # {"attribute": […]}). Accept both: a live server showed the difference.
        keywords = keyword_list(detail.get("keywords"))
        view = virtual_table(detail)

        bounds = bbox_text(detail.get("nativeBoundingBox")) or "-"

        attributes = detail.get("attributes") or []
        if isinstance(attributes, dict):
            attributes = attributes.get("attribute") or []
        if isinstance(attributes, dict):  # a single attribute is not a list
            attributes = [attributes]
        attribute_text = (
            "\n".join(
                "{} : {}".format(
                    a.get("name"), str(a.get("binding") or "").rsplit(".", 1)[-1]
                )
                for a in attributes
                if isinstance(a, dict)
            )
            or "-"
        )

        return {
            "name": name,
            "native_name": detail.get("nativeName", ""),
            "workspace": ws_name,
            "datastore": ds_name,
            "srs": detail.get("srs", ""),
            "projection_policy": detail.get("projectionPolicy", ""),
            "enabled": bool(detail.get("enabled", True)),
            "advertised": bool(detail.get("advertised", True)),
            "title": text_of(detail.get("title")),
            "abstract": text_of(detail.get("abstract")),
            "keywords": [str(k) for k in keywords],
            "cql_filter": detail.get("cqlFilter") or "",
            "bbox": bounds,
            "attributes": attribute_text,
            **({} if view is None else view_form_values(view)),
            **(_dimension_values(detail) if row_data[2] == VECTOR else {}),
        }

    def _layer_fields(self, kind=VECTOR, values=None):
        """The layer edit form: what GeoServer lets a partial PUT change on the
        resource first, read-only details on the Data tab, and a SQL view's
        own fields on a tab of their own.

        Every field on the first tab was measured to merge on 2.28.5 (feature
        types and coverages alike); the CQL filter exists for vectors only.
        """
        data = translate("LayerTabMixin", "Data")
        fields = [
            {
                "key": "name",
                "label": translate("LayerTabMixin", "Layer name"),
                "type": "text",
                "required": True,
                "help": translate(
                    "LayerTabMixin",
                    "Renaming keeps the data: GeoServer updates the layer groups "
                    "and the tile cache that use it. Clients that ask for the old "
                    "name stop finding it.",
                ),
            },
            {
                "key": "title",
                "label": translate("LayerTabMixin", "Title"),
                "type": "text",
            },
            {
                "key": "abstract",
                "label": translate("LayerTabMixin", "Abstract"),
                "type": "textarea",
                "max_height": 72,
            },
            {
                "key": "keywords",
                "label": translate("LayerTabMixin", "Keywords"),
                "type": "list",
            },
            {
                "key": "srs",
                "label": translate("LayerTabMixin", "SRS (EPSG code)"),
                "type": "text",
                "required": True,
                "crs": True,
                "help": translate(
                    "LayerTabMixin",
                    "The SRS GeoServer declares for the layer. Changing it "
                    "recomputes the bounds.",
                ),
            },
            {
                "key": "projection_policy",
                "label": translate("LayerTabMixin", "Projection policy"),
                "type": "combo",
                "options": list(_PROJECTION_POLICIES),
                "help": translate(
                    "LayerTabMixin",
                    "FORCE_DECLARED uses the declared SRS as it is. "
                    "REPROJECT_TO_DECLARED reprojects from the data's own SRS. NONE "
                    "keeps the data's own SRS.",
                ),
            },
            {
                "key": "enabled",
                "label": translate("LayerTabMixin", "Enabled"),
                "type": "checkbox",
                "help": translate(
                    "LayerTabMixin",
                    "Off: GeoServer stops serving the layer, and keeps it.",
                ),
            },
            {
                "key": "advertised",
                "label": translate("LayerTabMixin", "Advertised"),
                "type": "checkbox",
                "help": translate(
                    "LayerTabMixin",
                    "Off: the layer is not in the capabilities, but GeoServer still "
                    "serves it to whoever names it.",
                ),
            },
        ]
        if kind == VECTOR:
            fields.append(
                {
                    "key": "cql_filter",
                    "label": translate("LayerTabMixin", "CQL filter"),
                    "type": "text",
                    "placeholder": translate(
                        "LayerTabMixin", "Optional, for example pop > 1000"
                    ),
                    "help": translate(
                        "LayerTabMixin",
                        "GeoServer serves only the features that match it. Empty for "
                        "all.",
                    ),
                }
            )
        details = [
            ("native_name", translate("LayerTabMixin", "Native name")),
            ("workspace", translate("LayerTabMixin", "Workspace")),
            ("datastore", translate("LayerTabMixin", "Store")),
            ("bbox", translate("LayerTabMixin", "Native bounding box")),
        ]
        if kind == RASTER:
            details += [
                ("native_format", translate("LayerTabMixin", "Native format")),
                ("size", translate("LayerTabMixin", "Size in pixels")),
            ]
        fields += [
            {
                "key": key,
                "label": label,
                "type": "text",
                "read_only": True,
                "group": data,
            }
            for key, label in details
        ]
        if kind in (VECTOR, RASTER):
            fields.append(
                {
                    "key": "attributes" if kind == VECTOR else "bands",
                    "label": (
                        translate("LayerTabMixin", "Attributes")
                        if kind == VECTOR
                        else translate("LayerTabMixin", "Bands")
                    ),
                    "type": "textarea",
                    "read_only": True,
                    "group": data,
                }
            )
        if values is not None and "sql" in values:  # a SQL view's layer
            group = translate("LayerTabMixin", "SQL view")
            fields += [dict(field, group=group) for field in self._view_fields()]
        if kind == VECTOR and values is not None:
            fields += self._dimension_fields(values)
        return fields

    def _dimension_fields(self, values):
        """The Time and Elevation tabs of a vector layer.

        :param values: the form's prefill, with the attributes each dimension
            can use. A stored value is always among the options: the combo
            would show another one, and an untouched Save would send it.
        """

        def options(choices, key):
            stored = values.get(key)
            known = [
                choice[1] if isinstance(choice, tuple) else choice for choice in choices
            ]
            return list(choices) + ([stored] if stored and stored not in known else [])

        presentations = [
            (translate("LayerTabMixin", "A list of values"), "LIST"),
            (
                translate("LayerTabMixin", "A continuous interval"),
                "CONTINUOUS_INTERVAL",
            ),
            (
                translate("LayerTabMixin", "An interval with a resolution"),
                "DISCRETE_INTERVAL",
            ),
        ]
        strategies = [
            (translate("LayerTabMixin", "The smallest value"), "MINIMUM"),
            (translate("LayerTabMixin", "The largest value"), "MAXIMUM"),
            (
                translate("LayerTabMixin", "The value nearest to the reference"),
                "NEAREST",
            ),
            (translate("LayerTabMixin", "The reference value"), "FIXED"),
        ]
        units = {
            "weeks": translate("LayerTabMixin", "weeks"),
            "days": translate("LayerTabMixin", "days"),
            "hours": translate("LayerTabMixin", "hours"),
            "minutes": translate("LayerTabMixin", "minutes"),
            "seconds": translate("LayerTabMixin", "seconds"),
            "milliseconds": translate("LayerTabMixin", "milliseconds"),
        }
        fields = []
        for dimension in DIMENSIONS:
            start = len(fields)
            time = dimension == "time"
            group = (
                translate("LayerTabMixin", "Time")
                if time
                else translate("LayerTabMixin", "Elevation")
            )
            attributes = values.get(f"{dimension}_choices") or []
            fields += [
                {
                    "key": f"{dimension}_enabled",
                    "label": (
                        translate("LayerTabMixin", "Serve a time dimension")
                        if time
                        else translate("LayerTabMixin", "Serve an elevation dimension")
                    ),
                    "type": "checkbox",
                },
                {
                    "key": f"{dimension}_attribute",
                    "label": translate("LayerTabMixin", "Attribute"),
                    "type": "combo",
                    "options": options(attributes, f"{dimension}_attribute"),
                    "help": (
                        translate(
                            "LayerTabMixin", "The layer's date and time attributes."
                        )
                        if time
                        else translate(
                            "LayerTabMixin", "The layer's numeric attributes."
                        )
                    ),
                },
                {
                    "key": f"{dimension}_end_attribute",
                    "label": translate("LayerTabMixin", "End attribute"),
                    "type": "combo",
                    "options": options(
                        [(translate("LayerTabMixin", "None"), "")] + attributes,
                        f"{dimension}_end_attribute",
                    ),
                    "help": translate(
                        "LayerTabMixin",
                        "For a feature that covers a range: where the range ends.",
                    ),
                },
                {
                    "key": f"{dimension}_presentation",
                    "label": translate("LayerTabMixin", "Capabilities show"),
                    "type": "combo",
                    "options": options(presentations, f"{dimension}_presentation"),
                },
            ]
            if time:
                fields += [
                    {
                        "key": "time_resolution",
                        "label": translate("LayerTabMixin", "Resolution"),
                        "type": "spinbox",
                        "min": 1,
                        "max": 1_000_000,
                        "help": translate(
                            "LayerTabMixin", "For an interval with a resolution."
                        ),
                    },
                    {
                        "key": "time_resolution_unit",
                        "label": translate("LayerTabMixin", "Resolution unit"),
                        "type": "combo",
                        "options": [(units[unit], unit) for unit, _ in TIME_UNITS],
                    },
                ]
            else:
                fields += [
                    {
                        "key": "elevation_resolution",
                        "label": translate("LayerTabMixin", "Resolution"),
                        "type": "text",
                        "help": translate(
                            "LayerTabMixin",
                            "For an interval with a resolution, in the units below.",
                        ),
                    },
                    {
                        "key": "elevation_units",
                        "label": translate("LayerTabMixin", "Units"),
                        "type": "text",
                        "help": translate(
                            "LayerTabMixin",
                            "EPSG:5030 is metres above the WGS 84 ellipsoid.",
                        ),
                    },
                    {
                        "key": "elevation_unit_symbol",
                        "label": translate("LayerTabMixin", "Unit symbol"),
                        "type": "text",
                    },
                ]
            fields += [
                {
                    "key": f"{dimension}_default",
                    "label": translate("LayerTabMixin", "Default value"),
                    "type": "combo",
                    "options": options(strategies, f"{dimension}_default"),
                    "help": translate(
                        "LayerTabMixin",
                        "What a request that names no value gets.",
                    ),
                },
                {
                    "key": f"{dimension}_reference",
                    "label": translate("LayerTabMixin", "Reference value"),
                    "type": "text",
                    "help": (
                        translate(
                            "LayerTabMixin",
                            "For the nearest or the reference value. A time in "
                            "ISO 8601, such as 2026-01-01T00:00:00Z.",
                        )
                        if time
                        else translate(
                            "LayerTabMixin",
                            "For the nearest or the reference value.",
                        )
                    ),
                },
            ]
            fields[start:] = [dict(field, group=group) for field in fields[start:]]
        return fields

    @staticmethod
    def _dimensions_changed(before, after):
        """Whether the form changed a dimension. Pure.

        A dimension off before and after is unchanged: its fields are not
        saved, and an empty attribute reads back as the combo's first one.
        """
        for dimension in DIMENSIONS:
            on = f"{dimension}_enabled"
            if on not in before or not (before[on] or after.get(on)):
                continue
            if any(
                before[key] != after.get(key)
                for key in before
                if key.startswith(f"{dimension}_") and not key.endswith("_choices")
            ):
                return True
        return False

    @staticmethod
    def _check_dimensions(values):
        """Refuse a dimension GeoServer would store and then break on. Pure."""
        for dimension in DIMENSIONS:
            if not values.get(f"{dimension}_enabled"):
                continue
            name = (
                translate("LayerTabMixin", "time")
                if dimension == "time"
                else translate("LayerTabMixin", "elevation")
            )
            if not values.get(f"{dimension}_attribute"):
                raise ValueError(
                    translate(
                        "LayerTabMixin",
                        "The {} dimension needs an attribute, and this layer has "
                        "none it can use.",
                    ).format(name)
                )
            if (
                values.get(f"{dimension}_default") in ("NEAREST", "FIXED")
                and not (values.get(f"{dimension}_reference") or "").strip()
            ):
                raise ValueError(
                    translate(
                        "LayerTabMixin",
                        "The {} dimension's default needs a reference value.",
                    ).format(name)
                )
            if (
                dimension == "elevation"
                and values.get("elevation_presentation") == "DISCRETE_INTERVAL"
            ):
                try:
                    resolution = float(values.get("elevation_resolution") or "")
                except ValueError:
                    resolution = 0
                if not math.isfinite(resolution) or resolution <= 0:
                    raise ValueError(
                        translate(
                            "LayerTabMixin",
                            "An elevation interval with a resolution needs a "
                            "resolution above 0.",
                        )
                    )

    def _view_fields(self, visible=True):
        """A SQL view's fields: the publish form's, and a view's edit form's."""
        shown = {} if visible else {"visible": False}
        return [
            {
                "key": "sql",
                "label": translate("LayerTabMixin", "SQL"),
                "type": "textarea",
                "code": "sql",
                "required": True,
                **shown,
                "help": translate(
                    "LayerTabMixin",
                    "One SELECT. Write a parameter as %name%: a request can then "
                    "give it a value through viewparams.",
                ),
            },
            {
                "key": "geometry_column",
                "label": translate("LayerTabMixin", "Geometry column"),
                "type": "text",
                "placeholder": "geom",
                **shown,
                "help": translate(
                    "LayerTabMixin",
                    "Without one, GeoServer gives the geometry the type Geometry.",
                ),
            },
            {
                "key": "geometry_type",
                "label": translate("LayerTabMixin", "Geometry type"),
                "type": "combo",
                "options": list(GEOMETRY_TYPES),
                **shown,
            },
            {
                "key": "key_columns",
                "label": translate("LayerTabMixin", "Key columns"),
                "type": "list",
                **shown,
                "help": translate(
                    "LayerTabMixin",
                    "The columns that identify a feature. Without them, a feature "
                    "gets a new id at every request.",
                ),
            },
            {
                "key": "parameters",
                "label": translate("LayerTabMixin", "Parameters"),
                "type": "table",
                "columns": [
                    {"label": translate("LayerTabMixin", "Parameter")},
                    {"label": translate("LayerTabMixin", "Default value")},
                    {"label": translate("LayerTabMixin", "Validator")},
                ],
                **shown,
                "help": translate(
                    "LayerTabMixin",
                    "An empty validator gets GeoServer's default, ^[\\w\\d\\s]+$: "
                    "letters, digits and spaces only. Without a validator, a "
                    "request could put any SQL into the view.",
                ),
            },
        ]

    @staticmethod
    def _unreadable(name):
        """What a row action says of a row whose details could not be read
        when the page filled: its type is unknown, not unsupported."""
        return translate(
            "LayerTabMixin",
            "The details of '{}' could not be read. Refresh the list, then try again.",
        ).format(name)

    def _layer_resource(self, row_data):
        """The resource behind a layer row, as GeoServer stores it: a feature
        type, a coverage or a cascaded layer, by the row's type and store."""
        name, ws_name, kind, store = row_data[0], row_data[1], row_data[2], row_data[3]
        if kind == VECTOR:
            return self._feature_type_detail(ws_name, store, name)
        if kind == RASTER:
            return self._coverage_detail(ws_name, store, name)
        if kind in (WMS, WMTS):
            return self._cascaded_layer_detail(ws_name, store, kind, name)
        if kind == "-":
            raise ValueError(self._unreadable(name))
        raise ValueError(
            translate("LayerTabMixin", "Unsupported layer type '{}'").format(kind)
        )

    def _feature_type_detail(self, ws_name, store, name):
        """One feature type, as GeoServer stores it.

        TODO(#1): upstream: get_feature_type() exists, but FeatureType drops
        title when an internationalTitle is set, and dropped cqlFilter before
        0.8.14 (row 63). The
        edit form then showed an existing filter as empty, and emptying the
        field changed nothing. Workaround: GET the feature type path.
        """
        path = self.gs.rest_service.rest_endpoints.featuretype(ws_name, store, name)
        return self._raw_rest("get", path).json().get("featureType") or {}

    def _show_layer_info(self, row_data):
        """Open a layer: an edit form for a vector or a raster, a view for a
        cascaded layer.

        Save sends one partial PUT with only what changed: GeoServer merges it
        (measured on 2.28.5), so everything the form does not show stays as it
        is. A cascaded layer stays a view: GeoServer 2.28.5 answers any PUT on a
        cascaded WMS layer with UnsupportedOperationException, JSON, XML and
        the very document a GET returned alike.
        """
        detail = self._fetch(
            lambda: self._layer_resource(row_data),
            translate("LayerTabMixin", "Failed to load layer details"),
        )
        if detail is None:
            return
        detail = detail if isinstance(detail, dict) else {}
        name, ws_name, kind, store = row_data[0], row_data[1], row_data[2], row_data[3]
        if kind in (WMS, WMTS):
            dlg = ResourceFormDialog(
                title=translate("LayerTabMixin", "Layer '{}'").format(name),
                description=translate(
                    "LayerTabMixin",
                    "Cascaded through {type} store {ws}/{store}. Read-only: "
                    "GeoServer's REST API cannot change a cascaded layer, only its web "
                    "interface can.",
                ).format(type=kind, ws=ws_name, store=store),
                fields=[
                    f for f in self._cascaded_layer_fields([]) if f["key"] != "layer"
                ],
                values=self._cascaded_layer_form_values(detail),
                parent=self,
            )
            dlg.hide_save_button()
            dlg.exec()
            return

        values = self._layer_form_values(row_data, detail)
        if kind == RASTER:
            coverage = self._coverage_form_values(detail)
            values.update(
                native_format=coverage.get("native_format", ""),
                size=coverage.get("size", ""),
                bands=coverage.get("bands", ""),
            )
        before = dict(values)

        def check(after):
            self._check_dimensions(after)
            self._check_layer_edit(
                ws_name, self._layer_changes(before, after, kind)[0] or {}
            )

        dlg = ResourceFormDialog(
            title=translate("LayerTabMixin", "Layer '{}'").format(name),
            description=translate(
                "LayerTabMixin", "Published from {ws}/{store}."
            ).format(ws=ws_name, store=store),
            fields=self._layer_fields(kind, values=values),
            values=values,
            parent=self,
            validate=self._form_check(check),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        after = dlg.get_values()
        if self._layer_changes(before, after, kind)[0] is None and not (
            self._view_changed(before, after) or self._dimensions_changed(before, after)
        ):
            return  # nothing changed: no request
        if self._run_action(
            # In the worker, behind the waiting box: a slow server must not
            # freeze QGIS on Save either.
            lambda: self._wait_for_save(
                lambda: self._save_layer(row_data, before, after)
            ),
            translate("LayerTabMixin", "Failed to save layer '{}'").format(name),
        ):
            self.show_success_message(
                translate("LayerTabMixin", "Layer '{}' saved.").format(after["name"])
            )
            self._load_layers()

    @staticmethod
    def _layer_changes(before, after, kind):
        """(body, recalculate) for a partial resource PUT, or (None, False).

        Only the fields that changed go out, in GeoServer's own spelling; an
        emptied title, abstract, keyword list or filter is sent empty, which
        clears it (measured). A changed SRS or projection policy recomputes
        the bounds in the same request. Pure, so the mapping is testable.
        """

        def srs(text):
            code = str(text or "").strip().upper().removeprefix("EPSG:")
            return f"EPSG:{code}"

        body = {}
        for key, rest_key in (
            ("name", "name"),
            ("title", "title"),
            ("abstract", "abstract"),
            ("projection_policy", "projectionPolicy"),
            ("enabled", "enabled"),
            ("advertised", "advertised"),
        ):
            if after.get(key) != before.get(key):
                body[rest_key] = after.get(key)
        if kind == VECTOR and (after.get("cql_filter") or "") != (
            before.get("cql_filter") or ""
        ):
            body["cqlFilter"] = after.get("cql_filter") or ""
        if words(after.get("keywords")) != words(before.get("keywords")):
            body["keywords"] = {"string": words(after.get("keywords"))}
        if srs(after.get("srs")) != srs(before.get("srs")):
            body["srs"] = srs(after.get("srs"))
        recalculate = "srs" in body or "projectionPolicy" in body
        return (body or None), recalculate

    def _resource_path(self, workspace_name, kind, store, name):
        """REST path of the resource behind a vector or raster layer, quoted."""
        endpoints = self.gs.rest_service.rest_endpoints
        segments = [quote(part, safe="") for part in (workspace_name, store, name)]
        if kind == RASTER:
            return endpoints.coverage(*segments)
        return endpoints.featuretype(*segments)

    def _check_layer_edit(self, ws_name, body):
        """Refuse an edit GeoServer would take badly. Reads only: the form
        runs it before it closes, the save again."""
        if "name" in body:
            self._require_safe_name(body["name"])
            qualified = "{}:{}".format(ws_name, body["name"])
            if self.gs.rest_service.resource_exists(self._layers_url(qualified)):
                raise ValueError(
                    translate(
                        "LayerTabMixin", "Layer '{}' already exists in '{}'."
                    ).format(body["name"], ws_name)
                )
        if "srs" in body and not body["srs"].removeprefix("EPSG:").isdigit():
            raise ValueError(
                translate(
                    "LayerTabMixin",
                    "The SRS must be an EPSG code number, such as 3857 or 4326.",
                )
            )

    def _save_layer(self, row_data, before, after):
        """Validate, then PUT what changed on the layer's resource.

        TODO(#1): no update_feature_type() or update_coverage() in the
        library (row 53). create_feature_type() upserts by the *new* name, so a
        rename would POST a second layer, and it knows no cqlFilter; coverages
        have no update at all. Workaround: one merging PUT on the resource.
        """
        ws_name, kind, store = row_data[1], row_data[2], row_data[3]
        body, recalculate = self._layer_changes(before, after, kind)
        if body is not None:
            self._check_layer_edit(ws_name, body)
            wrapper = "coverage" if kind == RASTER else "featureType"
            self._raw_rest(
                "put",
                self._resource_path(ws_name, kind, store, before["name"]),
                json={wrapper: body},
                params=(
                    {"recalculate": "nativebbox,latlonbbox"} if recalculate else None
                ),
            )
        saved = body is not None
        if self._view_changed(before, after):
            try:
                self._save_view(ws_name, store, after)
            except Exception as error:
                if not saved:
                    raise
                raise PartlySaved(
                    translate(
                        "LayerTabMixin", "Layer '{}' saved, but not its SQL view: {}"
                    ).format(after["name"], self._error_text(error))
                ) from error
            saved = True
        if self._dimensions_changed(before, after):
            try:
                self._check_dimensions(after)
                self._save_dimensions(ws_name, store, after)
            except Exception as error:
                if not saved:
                    raise
                raise PartlySaved(
                    translate(
                        "LayerTabMixin", "Layer '{}' saved, but not its dimensions: {}"
                    ).format(after["name"], self._error_text(error))
                ) from error

    def _save_dimensions(self, ws_name, store, values):
        """PUT the layer's time and elevation, keeping its other metadata.

        TODO(#1): the library's FeatureType models the time dimension only,
        and writes it as the whole metadata map (row 83). Measured on 2.28.5:
        a metadata PUT replaces the map, so that would drop a SQL view and the
        elevation. Workaround: read the entries again, swap the dimensions'.
        """
        path = self._resource_path(ws_name, VECTOR, store, values["name"])
        current = self._raw_rest("get", path).json().get("featureType") or {}
        entries = with_dimensions(metadata_entries(current), values)
        self._raw_rest(
            "put", path, json={"featureType": {"metadata": {"entry": entries}}}
        )

    @staticmethod
    def _view_changed(before, after):
        """Whether the form changed a SQL view's own fields. Pure."""
        return "sql" in before and any(
            before.get(key) != after.get(key) for key in VIEW_FIELDS
        )

    def _save_view(self, ws_name, store, values):
        """PUT a view's new SQL, and put the old one back if GeoServer cannot
        run it.

        TODO(#1): nothing in the library writes a feature type's metadata (row
        82). Measured on 2.28.5: a metadata PUT replaces the whole map, so the
        other entries go with it, and a SQL GeoServer cannot run is answered
        200, after which every read of the layer fails with "Could not get
        native attributes". Putting the old view back repairs it.
        """
        path = self._resource_path(ws_name, VECTOR, store, values["name"])
        current = self._raw_rest("get", path).json().get("featureType") or {}
        entries = metadata_entries(current)
        old = virtual_table(current) or {}
        # The geometry's srid is the data's own CRS, not the layer's declared
        # SRS (projectionPolicy reconciles the two): a view made elsewhere
        # with srid 2056 and declared 3857 keeps 2056. Only a view without a
        # geometry yet takes the declared one.
        srid = (old.get("geometry") or {}).get("srid")
        if srid is None:
            code = str(values.get("srs") or "").upper().removeprefix("EPSG:")
            srid = int(code) if code.isdigit() else None
        view = view_of(
            old.get("name") or current.get("nativeName") or values["name"],
            values,
            srid,
            escape_sql=bool(old.get("escapeSql")),
        )
        self._raw_rest(
            "put",
            path,
            json={"featureType": {"metadata": {"entry": with_view(entries, view)}}},
        )
        try:
            self._raw_rest("get", path)
        except Exception as error:
            # GeoServer's own refusal is a 500 "Could not get native
            # attributes". A read that failed on the way (a proxy, a timeout)
            # says nothing about the SQL, and the save landed: keep it.
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status != 500:
                raise RuntimeError(
                    translate(
                        "LayerTabMixin",
                        "The SQL view was saved, but reading it back failed: {}",
                    ).format(self._error_text(error))
                ) from error
            self._raw_rest(
                "put", path, json={"featureType": {"metadata": {"entry": entries}}}
            )
            raise RuntimeError(
                translate(
                    "LayerTabMixin",
                    "GeoServer cannot run the new SQL, so the view keeps its "
                    "previous one. {}",
                ).format(self._error_text(error))
            ) from error

    def _update_layer_from_source(self, row_data):
        """Re-read the data behind a layer, then recompute its bounds.

        After the table gained a column or the file was replaced: GeoServer
        keeps the resource's schema cached until reset. TODO(#1): neither
        call is in the library (row 53): POST .../reset, then a PUT with
        ?recalculate=nativebbox,latlonbbox (both measured on 2.28.5).
        """
        name, ws_name, kind, store = row_data[0], row_data[1], row_data[2], row_data[3]
        if kind == "-":
            self.show_warning_message(self._unreadable(name))
            return
        if kind not in (VECTOR, RASTER):
            self.show_warning_message(
                translate(
                    "LayerTabMixin",
                    "'{}' is a cascaded layer: GeoServer's REST API cannot update it. "
                    "Use GeoServer's web interface.",
                ).format(name)
            )
            return
        path = self._resource_path(ws_name, kind, store, name)
        wrapper = "coverage" if kind == RASTER else "featureType"

        def update():
            self._raw_rest("post", path.removesuffix(".json") + "/reset")
            self._raw_rest(
                "put",
                path,
                json={wrapper: {"name": name}},
                params={"recalculate": "nativebbox,latlonbbox"},
            )

        if self._run_action(
            lambda: self._wait_for_save(update),
            translate("LayerTabMixin", "Failed to update '{}' from its data").format(
                name
            ),
        ):
            self.show_success_message(
                translate(
                    "LayerTabMixin", "'{}' re-read from its data, bounds recomputed."
                ).format(name)
            )

    # -- Publish --------------------------------------------------------------

    # -- Default style --------------------------------------------------------

    def _layer_styles(self, workspace_name, name):
        """(default style, [other styles]) of a layer. Raises on HTTP errors.

        TODO(#1): rest_service.get_layer() keeps a single other style as the
        bare {"name", "href"} dict GeoServer sends, and Layer.asdict() then
        reads its keys as two styles, "name" and "href" (row 62): Set style
        refused to save, and deleting those lines wiped the real style. So
        the layer document is read here and unwrapped like every list.
        """
        payload = self._raw_rest(
            "get", self._layers_url(f"{workspace_name}:{name}")
        ).json()
        layer = payload.get("layer") or {}
        default = (layer.get("defaultStyle") or {}).get("name")
        others = [
            self._name_of(style)
            for style in unwrap(layer, "styles", "style")
            if isinstance(style, dict)
        ]
        return default, others

    def _style_choices(self, workspace_name):
        """Styles a layer in this workspace may use: global ones and its workspace's.

        A workspace style is referenced by its qualified name, "ws:style".
        """
        choices = [self._name_of(st) for st in self._fetch_list(self.gs.get_styles)]
        if workspace_name:  # the global scope has no workspace styles of its own
            choices += [
                f"{workspace_name}:{self._name_of(st)}"
                for st in self._fetch_list(self.gs.get_styles, workspace_name)
            ]
        return sorted(choices)

    def _set_layer_style(self, row_data):
        """Pick the default style of one layer, and the other styles it offers.

        The other styles are what a client may ask for with STYLES=; GeoServer
        lists them in the capabilities. One layer PUT through the library's
        Layer model carries whichever of the two changed: the PUT replaces
        the list (measured on 2.28.5; an empty list clears it). A cascaded WMS
        layer is offered its other styles only: GeoServer answers 200 to a new
        default and keeps none (measured on 2.28.5).
        """
        name, ws_name, kind = row_data[0], row_data[1], row_data[2]
        fetched = self._fetch(
            lambda: (
                self._style_choices(ws_name),
                self._layer_styles(ws_name, name),
            ),
            translate("LayerTabMixin", "Failed to load styles for '{}'").format(name),
        )
        if fetched is None:
            return
        choices, (current, others) = fetched
        if current and current not in choices:
            choices.insert(0, current)
        description = translate(
            "LayerTabMixin",
            "Global styles and the styles of workspace '{}'.",
        ).format(ws_name)
        fields = []
        if kind == WMS:
            description += " " + translate(
                "LayerTabMixin",
                "A cascaded WMS layer keeps the remote server's default style: you can "
                "set only its other styles.",
            )
        else:
            fields.append(
                {
                    "key": "style",
                    "label": translate("LayerTabMixin", "Default style"),
                    "type": "combo",
                    "options": choices,
                    "default": current,
                    "required": True,
                }
            )

        dlg = ResourceFormDialog(
            title=translate("LayerTabMixin", "Styles of '{}'").format(name),
            description=description,
            fields=fields
            + [
                {
                    "key": "others",
                    "label": translate("LayerTabMixin", "Other styles"),
                    "type": "table",
                    "unique": True,
                    "default": list(others),
                    "choices": choices,
                    "columns": [{"label": translate("LayerTabMixin", "Style")}],
                    "help": translate(
                        "LayerTabMixin",
                        "The styles a client can also ask for. Empty for none.",
                    ),
                },
            ],
            parent=self,
            ok_label=translate("LayerTabMixin", "Set styles"),
            validate=lambda values: self._wanted_styles(values, choices),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        values = dlg.get_values()
        style = values.get("style", current)
        try:  # the form checked it already; a caller that skipped it did not
            wanted = self._wanted_styles(values, choices)
        except ValueError as error:
            self.show_error_message(str(error))
            return
        if style == current and sorted(wanted) == sorted(others):
            return

        def save():
            from geoservercloud.models.layer import Layer

            layer = Layer(
                name=name,
                default_style_name=style if style != current else None,
                styles=(
                    [{"name": s} for s in wanted]
                    if sorted(wanted) != sorted(others)
                    else None
                ),
            )
            self._check(self.gs.rest_service.update_layer(layer, ws_name))

        if self._run_action(
            lambda: self._wait_for_save(save),
            translate("LayerTabMixin", "Failed to set the styles of '{}'").format(name),
        ):
            self.show_success_message(
                translate("LayerTabMixin", "Styles of '{}' saved.").format(name)
            )
            self._load_layers()

    @staticmethod
    def _wanted_styles(values, choices):
        """The other styles picked, once each, refusing a name not on the
        server. Pure: the form runs it before it closes."""
        style = values.get("style")
        wanted = [name.strip() for name in values["others"] if name.strip()]
        wanted = list(dict.fromkeys(s for s in wanted if s != style))  # no repeats
        unknown = [s for s in wanted if s not in choices]
        if unknown:
            raise ValueError(
                translate("LayerTabMixin", "No style named {} on the server.").format(
                    ", ".join(f"'{s}'" for s in unknown)
                )
            )
        return wanted

    # -- Style from QGIS -------------------------------------------------------

    @staticmethod
    def _matching_project_layer(layer_name, layers):
        """The project layer that looks like this GeoServer layer, or None.

        Matched on the name, ignoring case and any "workspace:" prefix, because
        that is how a layer added by this plugin (or by QGIS's own browser)
        comes into a project.
        """
        wanted = layer_name.split(":")[-1].casefold()
        for layer in layers:
            if layer.name().split(":")[-1].casefold() == wanted:
                return layer
        return None

    def _style_from_qgis(self, row_data):
        """Upload a QGIS layer's symbology as this layer's style.

        A cascaded WMS layer takes no default style (GeoServer answers 200 and
        keeps none), so there the style is only uploaded.
        """
        name, ws_name, kind = row_data[0], row_data[1], row_data[2]
        layers = styleable_project_layers()
        if not layers:
            self.show_warning_message(
                translate(
                    "LayerTabMixin",
                    "This QGIS project has no vector or raster layer to take a style from.",
                )
            )
            return
        match = self._matching_project_layer(name, layers)
        description = translate(
            "LayerTabMixin",
            "The plugin exports the layer's symbology as SLD and uploads it to "
            "workspace '{}'. A style of that name there is replaced: that is how you "
            "push a change made in QGIS.",
        ).format(ws_name)
        default_field = []
        if kind == WMS:
            description += " " + translate(
                "LayerTabMixin",
                "A cascaded WMS layer keeps the remote server's default style: "
                "the style is uploaded, not assigned.",
            )
        else:
            default_field.append(
                {
                    "key": "set_default",
                    "label": translate(
                        "LayerTabMixin", "Make it the layer's default style"
                    ),
                    "type": "checkbox",
                    "default": True,
                }
            )

        dlg = ResourceFormDialog(
            title=translate("LayerTabMixin", "Style '{}' from QGIS").format(name),
            description=description,
            fields=[
                {
                    "key": "qgis_layer",
                    "label": translate("LayerTabMixin", "QGIS layer"),
                    "type": "layer",
                    "default": match,
                    # With no match, the first layer was preselected, and its
                    # style pushed as the default of an unrelated layer.
                    "allow_empty": True,
                    "required": True,
                    "help": (
                        None
                        if match
                        else translate(
                            "LayerTabMixin", "No project layer matches '{}' by name."
                        ).format(name)
                    ),
                },
                {
                    "key": "style",
                    "label": translate("LayerTabMixin", "Style name"),
                    "type": "text",
                    "default": geoserver_name(name),
                    "required": True,
                },
            ]
            + default_field,
            parent=self,
            ok_label=translate("LayerTabMixin", "Upload"),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        values = dlg.get_values()
        # The export reads a live QGIS layer, so it happens here on the GUI
        # thread, before the upload (invariant 9).
        layer = values["qgis_layer"]
        sld = self._fetch(
            lambda: layer_to_sld(layer),
            translate("LayerTabMixin", "Could not export the symbology of '{}'").format(
                layer.name()
            ),
            in_worker=False,
        )
        if sld is None:
            return

        style_name = geoserver_name(values["style"])
        set_default = values.get("set_default", False)
        outcome = {}
        if not self._run_action(
            lambda: outcome.setdefault(
                "pushed",
                self._push_qgis_style(style_name, ws_name, sld, name, set_default),
            ),
            translate("LayerTabMixin", "Failed to upload the style of '{}'").format(
                layer.name()
            ),
        ):
            return
        if not outcome.get("pushed"):
            self.show_warning_message(
                translate("LayerTabMixin", "Style '{}' left as it is.").format(
                    style_name
                )
            )
        else:
            self.show_success_message(
                translate("LayerTabMixin", "'{}' styled from '{}'.").format(
                    name, layer.name()
                )
                if set_default
                else translate("LayerTabMixin", "Style '{}' uploaded to '{}'.").format(
                    style_name, ws_name
                )
            )
            self._reload_current_tab()

    def _push_qgis_style(
        self, style_name, workspace_name, sld, layer_name, set_default
    ):
        """Create or replace the style in the layer's workspace, then assign it.

        Returns False when the style exists and the user chose to keep it:
        a style is shared, and every layer using it
        would render differently, so replacing is confirmed, never silent
        (the Styles tab refuses an existing name outright; here replacing is
        the stated workflow: push the change you just made in QGIS).

        Workspace styles are referenced by their qualified name, so the layer's
        defaultStyle gets "workspace:style"; a bare name there would resolve
        to a global style of the same name instead.
        """
        # The requests run off the GUI thread; only the question stays on it.
        exists = self._wait_for(
            lambda: self._resource_exists(
                self.gs.get_style_definition, style_name, workspace_name
            )
        )
        if exists and not self._confirm_replace_style(style_name, workspace_name):
            return False

        def write():
            if exists:
                self._put_sld_body(style_name, workspace_name, sld, local_icons=True)
            else:
                # One request: a definition created first stayed behind,
                # empty, when GeoServer refused the body (_create_style).
                self._create_style(
                    style_name, workspace_name, "sld", sld, local_icons=True
                )
            if set_default:
                self._check(
                    self.gs.set_default_layer_style(
                        layer_name, workspace_name, f"{workspace_name}:{style_name}"
                    )
                )

        self._wait_for_save(write)
        return True

    def _confirm_replace_style(self, style_name, workspace_name):
        """Ask before a push overwrites a style other layers may share."""
        QApplication.setOverrideCursor(
            Qt.CursorShape.ArrowCursor
        )  # a caller's wait cursor
        try:
            reply = QMessageBox.question(
                self,
                translate("LayerTabMixin", "Replace the style?"),
                translate(
                    "LayerTabMixin",
                    "Style '{style}' already exists in '{workspace}'. Replacing it "
                    "changes how every layer that uses it renders.",
                ).format(style=style_name, workspace=workspace_name),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        finally:
            QApplication.restoreOverrideCursor()
        return reply == QMessageBox.StandardButton.Yes

    # -- Add to QGIS ----------------------------------------------------------

    def _server_layer(self, protocol, qualified_name, title):
        """A QGIS layer of one of this server's layers, as WMS, WMTS or WFS.

        The constructor reads the capabilities: a request, so build it in a
        worker (_fetch). It may be invalid; _valid_layer() makes that an error.
        A WFS layer is refused first when its capabilities point elsewhere.
        """
        settings = self.plg_settings.get_plg_settings()
        if protocol == "WFS":
            self._refuse_foreign_wfs(
                qualified_name.rpartition(":")[0], settings.geoserver_url
            )
        uri, provider = self._layer_uri(
            protocol,
            settings.geoserver_url,
            qualified_name,
            settings.geoserver_auth_cfg_id,
            # The plain fields, never get_credentials(): this runs in a
            # worker, and the auth database is the GUI thread's.
            (settings.geoserver_username, settings.geoserver_password),
        )
        layer_class = QgsVectorLayer if provider == "WFS" else QgsRasterLayer
        return layer_class(uri, title, provider)

    def _refuse_foreign_wfs(self, workspace_name, base_url):
        """Refuse a WFS whose capabilities send QGIS to another address.

        The WFS provider sends DescribeFeatureType and GetFeature, with the
        saved credentials, to the URLs the capabilities advertise, and has no
        option to stay on the one it is given (measured on QGIS 3.44).
        GeoServer writes them from its Proxy base URL, or from the Host a
        proxy forwards. A read, for the worker that builds the layer.
        """
        capabilities = self.gs.ows_service.get_wfs_capabilities(workspace_name)
        metadata = (capabilities.get("wfs:WFS_Capabilities") or {}).get(
            "ows:OperationsMetadata"
        )
        ours = _origin(base_url)
        elsewhere = sorted({_origin(url) for url in _hrefs(metadata)} - {ours, None})
        if elsewhere:
            raise RuntimeError(
                translate(
                    "LayerTabMixin",
                    "GeoServer advertises its WFS at {}, not at {} where the plugin "
                    "connects, and QGIS would send the user name and password there. "
                    "Check the Proxy base URL in the Global settings of the Server "
                    "tab, or add the layer as WMS.",
                ).format(", ".join(elsewhere), ours)
            )

    @staticmethod
    def _valid_layer(layer):
        """The layer, or a RuntimeError with QGIS's reason when it is invalid."""
        if not layer.isValid():
            raise RuntimeError(load_error(layer))
        return layer

    @staticmethod
    def _layer_uri(
        protocol, base_url, qualified_name, authcfg="", credentials=("", "")
    ):
        """Provider URI for one GeoServer layer. Returns (uri, provider_key).

        A workspace's layer goes through that workspace's own service,
        {base}/{ws}/ows: an isolated workspace's layers are in no other
        capabilities. There WMS and WMTS name it bare (a qualified name is
        not in those capabilities, measured), WFS by its qualified type
        name. A global layer group stays on the global service.

        IgnoreGetMapUrl and IgnoreGetFeatureInfoUrl keep QGIS on this URL,
        the one the plugin reached, instead of the one the capabilities
        advertise: behind a proxy that can be an inside address, which got
        the saved credentials. A WMTS identify needs the second one too
        (measured). WFS has no such option; see _refuse_foreign_wfs.

        `authcfg` is the id of the QGIS authentication config the plugin
        stores when the password is kept encrypted: the providers resolve it
        themselves, so a saved project holds no password. Otherwise the
        plain-text `credentials` go in the source, as QGIS's own Basic
        connections keep them, and a saved project holds them too.
        """
        base = base_url.rstrip("/")
        workspace, _, name = qualified_name.rpartition(":")
        if workspace:
            base = f"{base}/{quote(workspace, safe='')}"
        username, password = credentials
        if authcfg:
            auth = f"&authcfg={authcfg}"
        elif username:
            auth = (
                f"&username={quote(username, safe='')}"
                f"&password={quote(password, safe='')}"
            )
        else:
            auth = ""
        if protocol == "WMS":
            return (
                f"crs=EPSG:4326&format=image/png&layers={name}&styles="
                f"&url={base}/ows&IgnoreGetMapUrl=1&IgnoreGetFeatureInfoUrl=1{auth}",
                "wms",
            )
        if protocol == "WMTS":
            # No crs= here: the tile matrix set fixes it (EPSG:900913), and a
            # crs=EPSG:4326 alongside made QGIS accept the layer and reproject
            # every tile on the fly (measured on 2.28.5 / QGIS 3.44).
            return (
                f"format=image/png&layers={name}&styles="
                f"&tileMatrixSet={_WMTS_TILE_MATRIX_SET}"
                f"&url={base}/gwc/service/wmts?REQUEST=GetCapabilities"
                f"&IgnoreGetMapUrl=1&IgnoreGetFeatureInfoUrl=1{auth}",
                "wms",
            )
        if protocol == "WFS":
            uri = QgsDataSourceUri()
            uri.setParam("url", f"{base}/ows")
            uri.setParam("typename", qualified_name)
            uri.setParam("version", "auto")
            # No srsname: QGIS takes the type's own CRS from the capabilities
            # and the features arrive native. srsname=EPSG:4326 made GeoServer
            # reproject every feature, and QGIS again to the canvas (measured
            # on 2.28.5 with sf:archsites, EPSG:26713).
            uri.setParam("pagingEnabled", "true")
            if authcfg:
                uri.setAuthConfigId(authcfg)
            elif username:
                uri.setUsername(username)
                uri.setPassword(password)
            return (uri.uri(False), "WFS")
        raise ValueError(f"Unknown protocol: {protocol}")

    # -- Preview in a browser --------------------------------------------------

    @staticmethod
    def _bbox_from(box):
        """((minx, miny, maxx, maxy), crs) from a GeoServer bounding box, else
        (None, None) when it is missing or incomplete."""
        box = box or {}
        if not {"minx", "miny", "maxx", "maxy"} <= set(box):
            return None, None
        try:
            bbox = tuple(float(box[key]) for key in ("minx", "miny", "maxx", "maxy"))
        except (TypeError, ValueError):
            return None, None
        crs = box.get("crs")
        if isinstance(crs, dict):
            # A projected CRS comes as {"@class": "projected", "$": "EPSG:…"}.
            crs = crs.get("$")
        return bbox, str(crs or "EPSG:4326")

    @staticmethod
    def _preview_url(base_url, qualified_name, bbox=None, srs=None, workspace=None):
        """GeoServer's own OpenLayers preview page for a layer or a layer group.

        A URL for the browser, not a request from the plugin: the browser's
        session is not the plugin's, so a secured server asks it to log in.
        Without a usable bbox the map opens on the world. 768 px on the long
        side and the other from the bbox's aspect, as GeoServer's own Layer
        Preview page computes them.
        """
        base = base_url.rstrip("/")
        service = (
            f"{base}/{quote(workspace, safe='')}/wms" if workspace else f"{base}/wms"
        )
        if not bbox or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            bbox, srs = (-180.0, -90.0, 180.0, 90.0), "EPSG:4326"
        width = height = _PREVIEW_SIZE
        ratio = (bbox[3] - bbox[1]) / (bbox[2] - bbox[0])
        if ratio <= 1:
            height = max(1, round(_PREVIEW_SIZE * ratio))
        else:
            width = max(1, round(_PREVIEW_SIZE / ratio))
        query = urlencode(
            {
                "service": "WMS",
                "version": "1.1.0",
                "request": "GetMap",
                "layers": qualified_name,
                "bbox": ",".join(str(value) for value in bbox),
                "width": width,
                "height": height,
                "srs": srs or "EPSG:4326",
                "styles": "",
                "format": "application/openlayers",
            },
            safe=":/,",
        )
        return f"{service}?{query}"

    def _open_in_browser(self, url):
        """Hand a URL to the system browser; say so when nothing opens."""
        if not QDesktopServices.openUrl(QUrl(url)):
            self.show_warning_message(
                translate("LayerTabMixin", "Could not open a browser for {}").format(
                    url
                )
            )

    def _preview_layer_in_browser(self, row_data):
        """Open GeoServer's own preview of the layer, on its extent."""
        name, ws_name = row_data[0], row_data[1]
        detail = self._fetch(
            lambda: self._layer_resource(row_data),
            translate("LayerTabMixin", "Failed to load layer details"),
        )
        if detail is None:
            return
        detail = detail if isinstance(detail, dict) else {}
        bbox, srs = self._bbox_from(detail.get("latLonBoundingBox"))
        self._open_in_browser(
            self._preview_url(
                self.plg_settings.get_plg_settings().geoserver_url,
                f"{ws_name}:{name}" if ws_name else name,
                bbox,
                srs,
                workspace=ws_name or None,
            )
        )

    # -- Preview inside QGIS ---------------------------------------------------

    def _preview_layer(self, row_data):
        """Show the layer on a map of its own, with the feature info on click.

        The map layer is built like *Add to QGIS* builds one (credentials as
        the auth config id), but it lives in the preview window only: nothing
        reaches the project.
        """
        name, ws_name = row_data[0], row_data[1]
        qualified = f"{ws_name}:{name}" if ws_name else name

        # An invalid layer is not an error here: the window explains it in
        # place of the map.
        layer = self._fetch(
            lambda: self._server_layer("WMS", qualified, qualified),
            translate("LayerTabMixin", "Could not build the preview of '{}'").format(
                name
            ),
        )
        if layer is None:
            return
        # The WMS layer reads its extent from the capabilities, in the URI's
        # EPSG:4326, the map's CRS (measured on 2.28.5): the resource GET
        # that fetched latLonBoundingBox for it was a second read of the same.
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

    def _add_layer_to_qgis(self, row_data):
        """Ask which protocol, then add the layer to the current QGIS project."""
        name, ws_name, kind = row_data[0], row_data[1], row_data[2]
        if kind == VECTOR:
            protocols = list(PROTOCOLS)
            description = translate(
                "LayerTabMixin",
                "WFS loads the features themselves (editable, styled in QGIS). WMS and "
                "WMTS load rendered images. The user name and password come from the "
                "plugin's connection, not from the layer.",
            )
        else:
            # A raster or a cascaded layer has no features to serve over WFS.
            protocols = [protocol for protocol in PROTOCOLS if protocol != "WFS"]
            description = translate(
                "LayerTabMixin",
                "WMS and WMTS load rendered images; this layer has no features to "
                "serve over WFS. The user name and password come from the plugin's "
                "connection, not from the layer.",
            )
        dlg = ResourceFormDialog(
            title=translate("LayerTabMixin", "Add '{}' to QGIS").format(name),
            description=description,
            fields=[
                {
                    "key": "protocol",
                    "label": translate("LayerTabMixin", "Load as"),
                    "type": "combo",
                    "options": protocols,
                    # The features themselves for a vector; images otherwise.
                    "default": "WFS" if kind == VECTOR else "WMS",
                    "required": True,
                }
            ],
            parent=self,
            ok_label=translate("LayerTabMixin", "Add"),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        protocol = dlg.get_values()["protocol"]

        def build():
            return self._valid_layer(
                self._server_layer(protocol, f"{ws_name}:{name}", name)
            )

        # Built and checked here, not through iface.addRasterLayer(), so an
        # unreachable layer becomes our banner rather than QGIS's modal.
        layer = self._fetch(
            build,
            translate("LayerTabMixin", "Could not add '{}' to QGIS").format(name),
        )
        if layer is not None:
            QgsProject.instance().addMapLayer(layer)
            self.show_success_message(
                translate("LayerTabMixin", "'{}' added to the project as {}.").format(
                    name, protocol
                )
            )

    def _delete_layer(self, row_data):
        """Delete a single layer, with its resource, after confirmation."""
        self._delete_selected_layers([row_data])

    def _delete_layer_resource(self, workspace_name, kind, store, name):
        """Remove the resource behind a layer: the published layer goes with it."""
        if kind == VECTOR:
            self._check(self.gs.delete_feature_type(workspace_name, store, name))
        elif kind == RASTER:
            # TODO(#1): no delete_coverage() in the library, only
            # delete_coverage_store(). Workaround: DELETE the coverage with
            # recurse=true, which removes the layer and keeps the store.
            path = self.gs.rest_service.rest_endpoints.coverage(
                workspace_name, store, name
            )
            self._raw_rest("delete", path, params={"recurse": "true"})
        elif kind in (WMS, WMTS):
            self._delete_cascaded_layer(workspace_name, store, kind, name)
        elif kind == "-":
            raise ValueError(self._unreadable(name))
        else:
            raise ValueError(
                translate("LayerTabMixin", "Unsupported layer type '{}'").format(kind)
            )

    def _delete_selected_layers(self, selected_rows):
        """Delete one or more layers, each through its own resource type."""
        self._delete_many(
            [
                (
                    f"{row[1]}:{row[0]}" if row[1] else row[0],
                    lambda row=row: self._delete_layer_resource(
                        row[1], row[2], row[3], row[0]
                    ),
                )
                for row in selected_rows
            ],
            self._load_layers,
            ask=self._one_or_many(
                translate(
                    "LayerTabMixin", "Are you sure you want to delete layer '{}'?"
                ),
                lambda n: translate(
                    "LayerTabMixin",
                    "Are you sure you want to delete %n layer(s)?",
                    None,
                    n,
                ),
            ),
            done=self._one_or_many(
                translate("LayerTabMixin", "Layer '{}' deleted."),
                lambda n: translate("LayerTabMixin", "%n layer(s) deleted.", None, n),
            ),
            # Every resource delete sends recurse=true, which removes the
            # published layer, but GeoServer refuses outright while a layer
            # group still references it (verified against 2.28.5).
            cascade=translate(
                "LayerTabMixin",
                "The published layer goes too; the table, file or remote layer "
                "behind it is not touched. GeoServer refuses while a layer group "
                "still uses the layer: remove it from the group first.",
            ),
        )
