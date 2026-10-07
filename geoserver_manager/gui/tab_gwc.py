#! python3  # noqa: E265

"""
Tile Cache tab: the layers GeoWebCache caches, with their gridsets and
formats, and the two things one does to a cache: truncate it, stop caching.

Used as a mixin for GeoServerMainDialog.

GeoWebCache's REST API is XML-first, and on GeoServer 2.28.5 its JSON *writes*
are broken: a PUT of the very document a GET returned fails with "Duplicate
field mimeFormats" (every array) or "defaultValue" (the STYLES parameter filter
loses its class). Every write here is therefore XML: GET `.xml`, edit the
document, PUT `.xml`, which round-trips byte for byte. Reads stay JSON.
"""

import xml.etree.ElementTree as ElementTree
from urllib.parse import quote
from xml.sax.saxutils import escape

from qgis.core import Qgis
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, QTimer
from qgis.PyQt.QtWidgets import QDialog

from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog
from geoserver_manager.gui.scope import GLOBAL, PENDING
from geoserver_manager.toolbelt.payload import as_list
from geoserver_manager.toolbelt.rest import raw_rest, summarise_body

_XML = {"Content-Type": "application/xml"}

# What GeoServer itself configures when it caches a new layer automatically.
DEFAULT_GRIDSETS = ("EPSG:4326", "EPSG:900913")
DEFAULT_FORMATS = ("image/png", "image/jpeg")
# What the Formats picker offers: the MIME types GeoWebCache caches.
KNOWN_FORMATS = (
    "image/png",
    "image/jpeg",
    "image/png8",
    "image/gif",
    "image/vnd.jpeg-png",
    "image/vnd.jpeg-png8",
    "application/vnd.mapbox-vector-tile",
    "application/json;type=geojson",
    "application/json;type=topojson",
    "application/json;type=utfgrid",
)

# The document GeoServer writes for a new layer, minus the id it fills in
# itself. The STYLES filter is what lets one cache hold a tile set per style.
_NEW_LAYER_XML = (
    "<GeoServerLayer><enabled>true</enabled><name>{name}</name>"
    "<mimeFormats/><gridSubsets/><metaWidthHeight/>"
    "<expireCache>0</expireCache><expireClients>0</expireClients><gutter>0</gutter>"
    "<parameterFilters><styleParameterFilter><key>STYLES</key>"
    "<defaultValue></defaultValue></styleParameterFilter></parameterFilters>"
    "</GeoServerLayer>"
)


# Every user-visible string in this file goes through translate() with this
# file's own class as the context. self.tr() cannot: pylupdate extracts it
# under GwcTabMixin, but at runtime self.tr is QObject.tr with the context of
# the *instance's* class, GeoServerMainDialog. QDialog precedes the mixins in
# the MRO, so every lookup would miss. A wrapper function would not be
# extracted at all (pylupdate only understands a literal context), hence the
# repetition.
translate = QCoreApplication.translate


def _seed_types():
    """The seed form's task choices: (label, GWC's seedRequest type)."""
    return [
        (translate("GwcTabMixin", "Seed"), "seed"),
        (translate("GwcTabMixin", "Reseed"), "reseed"),
        (translate("GwcTabMixin", "Truncate"), "truncate"),
    ]


def _int_or_zero(text):
    """An integer out of a GWC document, 0 when it is not one."""
    try:
        return int(text or 0)
    except ValueError:
        return 0


class GwcTabMixin:
    """Mixin that adds the tile-cache (GeoWebCache) methods to the main dialog."""

    # -- Load -----------------------------------------------------------------

    def _load_gwc_layers(self):
        """Arm the Tile Cache tab, then fetch its rows in the background."""
        self._setup_add_button(
            translate("GwcTabMixin", "Add a Layer to the Cache"),
            translate(
                "GwcTabMixin",
                "Add a published layer that the tile cache does not hold yet",
            ),
            self._add_gwc_layer,
        )
        self._setup_delete_selected_button(
            self._remove_selected_gwc_layers,
            translate("GwcTabMixin", "Remove Selected from Cache"),
        )
        self._name_click_callback = self._show_gwc_layer_info
        self._extra_click_callbacks = {
            translate("GwcTabMixin", "Workspace"): self._open_workspace_from_row
        }
        self._row_actions = [
            (
                "seed-cache",
                translate("GwcTabMixin", "Seed or truncate…"),
                self._seed_gwc_layer,
            ),
            (
                "seed-tasks",
                translate("GwcTabMixin", "Tasks"),
                self._show_seed_tasks,
                translate("GwcTabMixin", "The running seed and truncate tasks"),
            ),
            (
                "clear-cache",
                translate("GwcTabMixin", "Truncate"),
                self._truncate_gwc_layer,
                translate("GwcTabMixin", "Deletes the cached tiles"),
            ),
            (
                "remove-cache",
                translate("GwcTabMixin", "Remove from cache"),
                self._remove_gwc_layer,
                translate("GwcTabMixin", "Stops caching the layer; the layer stays"),
            ),
        ]
        self._setup_table(
            [
                translate("GwcTabMixin", "Name"),
                translate("GwcTabMixin", "Workspace"),
                translate("GwcTabMixin", "Enabled"),
                translate("GwcTabMixin", "Gridsets"),
                translate("GwcTabMixin", "Formats"),
                self.actions_column_label(),
            ]
        )
        self._row_detail = lambda row: self._gwc_layer_summary(
            self._gwc_layer_detail(row[0])
        )
        self._detail_columns = (2, 3, 4)
        # "ne:roads" is the id every action sends; the Workspace column
        # beside it already says "ne".
        self._cell_display = {0: lambda name: name.split(":", 1)[-1]}
        self._start_load(
            translate("GwcTabMixin", "Failed to load the tile cache"),
            self._fetch_gwc_rows,
        )

    def _fetch_gwc_rows(self, task=None):
        """(rows, failures) for the table. Runs in a worker thread."""
        names = self._gwc_layer_names()
        # Enabled, gridsets and formats follow for the page shown.
        rows = [
            [name, self._gwc_workspace(name), PENDING, PENDING, PENDING]
            for name in names
        ]
        return rows, []

    # -- Reads ----------------------------------------------------------------

    def _gwc_base(self):
        """GeoWebCache's REST root, `/gwc/rest` on a stock GeoServer."""
        return self.gs.rest_service.gwc_endpoints.base_url

    def _gwc_layer_path(self, name, ext="json"):
        """REST path of one cached layer, by its qualified (or bare) name.

        Quoted: `requests` would send `layers/a#b.json` as `layers/a`. The
        `:` between workspace and layer is part of GWC's own naming.
        """
        return f"{self._gwc_base()}/layers/{quote(name, safe=':')}.{ext}"

    def _gwc_layer_names(self):
        """Every layer GeoWebCache caches: `ws:name`, a global layer group bare.

        TODO(#1): the library has the endpoint (`GwcEndpoints.layers()`, which
        ignores its workspace argument) but no method that calls it.
        Workaround: GET the collection, a plain JSON array of names.
        """
        endpoints = self.gs.rest_service.gwc_endpoints
        payload = self._raw_rest("get", endpoints.layers(None)).json()
        return sorted(str(name) for name in self._as_list(payload))

    def _gwc_layer_detail(self, name):
        """The GeoServerLayer document: enabled, gridSubsets, mimeFormats, …"""
        workspace, _, layer = name.rpartition(":")
        if workspace:
            payload = self._check(self.gs.get_gwc_layer(workspace, layer))
        else:
            # TODO(#1): get_gwc_layer() takes a workspace and a layer, so a
            # global layer group (cached under its bare name) is out of reach.
            payload = self._raw_rest("get", self._gwc_layer_path(name)).json()
        if not isinstance(payload, dict):
            raise RuntimeError(
                translate("GwcTabMixin", "Unexpected response: {}").format(
                    summarise_body(str(payload))
                )
            )
        return payload.get("GeoServerLayer") or {}

    def _gwc_layer_xml(self, name):
        """The same document as XML: the only form a PUT accepts.

        TODO(#1): get_gwc_layer() reads JSON only, and nothing updates a
        cached layer. Workaround: GET the `.xml` rendition and edit it.
        """
        return self._raw_rest("get", self._gwc_layer_path(name, "xml")).text

    def _gwc_layer_exists(self, name):
        """True when GeoWebCache already caches the layer.

        TODO(#1): read from the list the tab shows. A GET of a layer GWC does
        not cache is a 404 "Unknown layer" on 2.28.5 but a 500 on 2.27 and
        3.0 (measured). get_gwc_layer() raised on the 500 before 0.8.14,
        which turns it into a 404.
        """
        return name in self._gwc_layer_names()

    def _gridset_names(self):
        """The gridsets the server knows, for the picker.

        TODO(#1): create_gridset(epsg) PUTs one of three shipped definitions
        and nothing lists them. Workaround: GET the collection.
        """
        endpoints = self.gs.rest_service.gwc_endpoints
        payload = self._raw_rest("get", endpoints.gridsets()).json()
        return sorted(str(name) for name in self._as_list(payload))

    def _gridset_crs(self, name):
        """The CRS a gridset's coordinates are in ("EPSG:900913"), or None.

        TODO(#1): nothing in the library reads a gridset's definition
        (row 47). Workaround: GET its XML.
        """
        base = self.gs.rest_service.gwc_endpoints.base_url
        text = self._raw_rest("get", f"{base}/gridsets/{quote(name, safe='')}.xml").text
        number = ElementTree.fromstring(text).findtext("srs/number")
        return f"EPSG:{number}" if number else None

    def _uncached_layer_names(self):
        """Published layers and layer groups GeoWebCache does not cache."""
        base = self.gs.rest_service.rest_endpoints.base_url
        # TODO(#1): no get_layers() in the library (row 39), and the global
        # layer-group collection is out of its reach too (row 16).
        layers = self._raw_rest("get", f"{base}/layers.json").json()
        groups = self._raw_rest("get", f"{base}/layergroups.json").json()
        published = {
            self._name_of(item) for item in self._unwrap(layers, "layers", "layer")
        }
        published |= {
            self._name_of(item)
            for item in self._unwrap(groups, "layerGroups", "layerGroup")
        }
        # A workspace's own groups are not in that collection; GWC names them
        # "ws:group", like its layers. Without them, one removed from the cache
        # could never be added back from here.
        workspaces = self._get_workspace_names()
        for ws_name, (items, error) in zip(
            workspaces,
            self._fan_out(
                lambda ws: self._fetch_list(self.gs.get_layer_groups, ws), workspaces
            ),
        ):
            if error is None:
                published |= {f"{ws_name}:{self._name_of(item)}" for item in items}
            else:
                # In a worker, so logged: silent, its groups just went missing.
                self.log(
                    f"Tile cache: the layer groups of '{ws_name}' could not be "
                    f"listed: {self._error_text(error)}",
                    log_level=Qgis.MessageLevel.Warning,
                )
        return sorted(published - set(self._gwc_layer_names()))

    @staticmethod
    def _gwc_workspace(name):
        """The workspace a cached layer belongs to; a global group has none."""
        workspace, _, _layer = name.rpartition(":")
        return workspace or GLOBAL

    def _gwc_layer_summary(self, detail):
        """(enabled, gridsets, formats) cells; a layer whose GET failed shows dashes."""
        if not isinstance(detail, dict) or not detail:
            return ("-", "-", "-")
        gridsets = ", ".join(
            (
                str(subset.get("gridSetName", "?"))
                if isinstance(subset, dict)
                else str(subset)
            )
            for subset in as_list(detail.get("gridSubsets"))
        )
        formats = ", ".join(str(fmt) for fmt in as_list(detail.get("mimeFormats")))
        return (
            self._yes_no(detail.get("enabled", True)),
            gridsets or "-",
            formats or "-",
        )

    # -- The document ---------------------------------------------------------

    @staticmethod
    def _gridset_row(row):
        """(name, (start, stop)) of a gridset row [name, from, to].

        The range is the published zoom levels (zoomStart / zoomStop). Each
        end is its own: None ("all") leaves it open, and GeoWebCache keeps
        either one alone.
        The cached levels (min/maxCachedLevel) are left as they are;
        add two columns when someone needs to cache less than is served.
        """
        name = (row[0] or "").strip()
        start = row[1] if len(row) > 1 else None
        stop = row[2] if len(row) > 2 else None
        if start is not None and stop is not None and start > stop:
            raise ValueError(
                translate(
                    "GwcTabMixin", "'{}': the first zoom level is after the last."
                ).format(name)
            )
        return name, tuple(None if end is None else int(end) for end in (start, stop))

    @staticmethod
    def _parse_xml(xml_text):
        if isinstance(xml_text, str):
            # fromstring() refuses a str that carries an encoding declaration
            xml_text = xml_text.encode("utf-8")
        return ElementTree.fromstring(xml_text)

    @staticmethod
    def _gwc_form_values(xml_text):
        """Prefill for the dialog, from the XML document. Pure."""
        root = GwcTabMixin._parse_xml(xml_text)

        def number(tag):
            return _int_or_zero(root.findtext(tag))

        meta = [
            _int_or_zero(element.text)
            for element in root.findall("metaWidthHeight/int")
        ]
        return {
            "name": root.findtext("name") or "",
            "enabled": (root.findtext("enabled") or "true").strip().lower() == "true",
            "gridsets": [
                GwcTabMixin._gridset_cells(element)
                for element in root.findall("gridSubsets/gridSubset")
            ],
            "formats": [
                element.text or "" for element in root.findall("mimeFormats/string")
            ],
            "meta_width": meta[0] if len(meta) > 0 else 4,
            "meta_height": meta[1] if len(meta) > 1 else 4,
            "expire_cache": number("expireCache"),
            "expire_clients": number("expireClients"),
            "gutter": number("gutter"),
            "filters": GwcTabMixin._filters_text(root),
        }

    @staticmethod
    def _filters_text(root):
        """The parameter filters as indented XML, one element after another."""
        parts = []
        for element in root.findall("parameterFilters/*"):
            element.tail = None
            ElementTree.indent(element)
            parts.append(ElementTree.tostring(element, encoding="unicode"))
        return "\n".join(parts)

    @staticmethod
    def _gridset_cells(element):
        """One gridSubset as a form row: [name, from, to], blanks as None."""
        name = element.findtext("gridSetName") or ""
        start, stop = element.findtext("zoomStart"), element.findtext("zoomStop")
        return [name, int(start) if start else None, int(stop) if stop else None]

    @staticmethod
    def _gwc_xml_with_values(xml_text, values):
        """The document with the form's fields written into it. Pure.

        Everything the form does not model (the id, a gridset's extent and
        cached levels) stays as GeoServer wrote it: a kept gridset keeps its
        element, only new ones are created bare. A zoom end left at "all"
        clears that end only; the filters are replaced only when the form
        has them.
        """
        # One subset per gridset: a second row naming one was dropped
        # silently, with its zoom range.
        gridsets = {}
        for row in values.get("gridsets") or ():
            name, levels = GwcTabMixin._gridset_row(row)
            if name in gridsets:
                raise ValueError(
                    translate("GwcTabMixin", "Gridset '{}' is listed twice.").format(
                        name
                    )
                )
            if name:
                gridsets[name] = levels
        formats = list(
            dict.fromkeys(f.strip() for f in values.get("formats") or () if f.strip())
        )
        if not gridsets:
            raise ValueError(translate("GwcTabMixin", "Pick at least one gridset."))
        if not formats:
            raise ValueError(translate("GwcTabMixin", "Pick at least one format."))
        root = GwcTabMixin._parse_xml(xml_text)

        def child(tag):
            element = root.find(tag)
            if element is None:
                element = ElementTree.SubElement(root, tag)
            return element

        def leaf(tag, text):
            element = ElementTree.Element(tag)
            element.text = text
            return element

        def replace_children(tag, elements):
            parent = child(tag)
            for old in list(parent):
                parent.remove(old)
            for element in elements:
                parent.append(element)

        child("enabled").text = "true" if values.get("enabled", True) else "false"
        replace_children("mimeFormats", [leaf("string", fmt) for fmt in formats])
        kept = {
            element.findtext("gridSetName"): element
            for element in root.findall("gridSubsets/gridSubset")
        }
        subsets = []
        for name, levels in gridsets.items():
            element = kept.get(name)
            if element is None:
                element = ElementTree.Element("gridSubset")
                element.append(leaf("gridSetName", name))
            for tag, level in zip(("zoomStart", "zoomStop"), levels):
                for old in element.findall(tag):
                    element.remove(old)
                if level is not None:
                    element.append(leaf(tag, str(level)))
            subsets.append(element)
        replace_children("gridSubsets", subsets)
        if "filters" in values:
            replace_children(
                "parameterFilters", GwcTabMixin._parse_filters(values["filters"])
            )
        for element in child("parameterFilters").iter():
            # GWC writes this after a style rename, then refuses it in a PUT.
            if element.get("class", "").startswith("java.util.Collections$"):
                del element.attrib["class"]
        replace_children(
            "metaWidthHeight",
            [
                leaf("int", str(int(values.get("meta_width", 4)))),
                leaf("int", str(int(values.get("meta_height", 4)))),
            ],
        )
        child("expireCache").text = str(int(values.get("expire_cache", 0)))
        child("expireClients").text = str(int(values.get("expire_clients", 0)))
        child("gutter").text = str(int(values.get("gutter", 0)))
        return ElementTree.tostring(root, encoding="unicode")

    @staticmethod
    def _parse_filters(text):
        """The parameter-filter elements of the form's XML text. Pure.

        Kept as XML: GeoWebCache has several filter kinds (style, string,
        regex, float, integer), each with its own fields, and GeoServer
        answers a misspelt one with a bare 500 naming only the element.
        """
        try:
            wrapper = ElementTree.fromstring(
                f"<parameterFilters>{text or ''}</parameterFilters>"
            )
        except ElementTree.ParseError as error:
            raise ValueError(
                translate(
                    "GwcTabMixin", "The parameter filters are not valid XML: {}"
                ).format(error)
            ) from None
        return list(wrapper)

    # -- Writes ---------------------------------------------------------------

    def _save_gwc_layer(self, name, before, values):
        """PUT the fields the user changed onto the document GWC has now.

        `before` is what the form gave back untouched. The PUT replaces the
        whole document, and creates the cache of a layer GWC does not cache,
        so the document is read again: another client's edit survives, and a
        cache removed since the form opened is refused, not recreated.
        Raises on a bad form or an HTTP error.
        """
        if not self._gwc_layer_exists(name):
            raise RuntimeError(
                translate(
                    "GwcTabMixin",
                    "'{}' is no longer cached: its cache was removed since the form "
                    "opened. Refresh the list.",
                ).format(name)
            )
        current = self._gwc_layer_xml(name)
        edits = {
            key: value for key, value in values.items() if value != before.get(key)
        }
        values = {**self._gwc_form_values(current), **edits}
        # TODO(#1): no update of a cached layer in the library, and a JSON PUT
        # fails server-side ("Duplicate field mimeFormats"), so XML it is.
        self._put_gwc_xml(name, self._gwc_xml_with_values(current, values))

    def _check_new_gwc_layer(self, values):
        """(name, document) of the cache the Add form would create, or a
        ValueError: no layer, a name a URL eats, one cached already, a zoom
        range backwards, filters that are not XML. Reads only: the form runs
        it before it closes, so a refusal keeps what was typed."""
        name = (values.get("layer") or "").strip()
        if not name:
            raise ValueError(translate("GwcTabMixin", "Pick a layer."))
        self._require_safe_name(name)
        if self._gwc_layer_exists(name):
            raise ValueError(
                translate("GwcTabMixin", "'{}' is cached already.").format(name)
            )
        return name, self._gwc_xml_with_values(
            _NEW_LAYER_XML.format(name=escape(name)), values
        )

    def _create_gwc_layer_from_values(self, values):
        """Start caching the layer the form names. Raises when it is cached already."""
        name, document = self._check_new_gwc_layer(values)
        # TODO(#1): publish_gwc_layer() PUTs a JSON template GeoWebCache reads
        # as a degraded configuration: no formats, 0×0 meta-tiles, a single
        # gridset, no STYLES filter, after a needless configuration reload.
        # Workaround: PUT the XML document GeoServer itself would write.
        self._put_gwc_xml(name, document)

    def _put_gwc_xml(self, name, document):
        """PUT one cached layer's XML document: the only write GWC takes whole."""
        self._raw_rest(
            "put",
            self._gwc_layer_path(name, "xml"),
            data=document.encode("utf-8"),
            headers=_XML,
        )

    def _do_truncate_gwc_layer(self, name):
        """Drop every cached tile of the layer; its configuration stays.

        TODO(#1): the library has no seed or truncate call. Workaround: GWC's
        mass-truncate endpoint, the one request that covers every gridset,
        format and parameter set at once; the seed endpoint takes one
        combination per request. It wants `text/xml`: `application/xml`, which
        the layer PUTs take, is a 400 "Format extension unknown" here.
        """
        # Bytes: a str body is encoded by the HTTP stack, as Latin-1 on an
        # older urllib3, which garbles or refuses a name outside it.
        document = (
            f"<truncateLayer><layerName>{escape(name)}</layerName></truncateLayer>"
        )
        self._raw_rest(
            "post",
            f"{self._gwc_base()}/masstruncate",
            data=document.encode("utf-8"),
            headers={"Content-Type": "text/xml"},
        )

    def _do_remove_gwc_layer(self, name):
        """Delete the cache configuration and the tiles; the layer itself stays."""
        workspace, _, layer = name.rpartition(":")
        if workspace:
            self._check(self.gs.delete_gwc_layer(workspace, layer))
            return
        # TODO(#1): delete_gwc_layer() takes a workspace and a layer; a global
        # layer group is cached under its bare name.
        self._raw_rest("delete", self._gwc_layer_path(name))

    # -- Forms ----------------------------------------------------------------

    def _gwc_fields(self, gridset_names, layer_names=None):
        """Field definitions: the picker for a new cache, read-only name for an edit."""
        advanced = translate("GwcTabMixin", "Advanced")
        if layer_names is None:
            first = {
                "key": "name",
                "label": translate("GwcTabMixin", "Layer"),
                "type": "text",
                "read_only": True,
            }
        else:
            first = {
                "key": "layer",
                "label": translate("GwcTabMixin", "Layer"),
                "type": "combo",
                "options": list(layer_names),
                "required": True,
                "help": translate(
                    "GwcTabMixin",
                    "A published layer or layer group that the tile cache does not "
                    "hold yet.",
                ),
            }
        return [
            first,
            {
                "key": "enabled",
                "label": translate("GwcTabMixin", "Enabled"),
                "type": "checkbox",
                "help": translate(
                    "GwcTabMixin",
                    "Disabled: the tile cache neither serves nor stores tiles for it.",
                ),
            },
            {
                "key": "gridsets",
                "label": translate("GwcTabMixin", "Gridsets"),
                "type": "table",
                "unique": True,
                "required": True,
                "choices": list(gridset_names),
                "columns": [
                    {"label": translate("GwcTabMixin", "Gridset")},
                    {
                        "label": translate("GwcTabMixin", "From zoom"),
                        "type": "spin",
                        "min": 0,
                        "max": 40,
                        "none_text": translate("GwcTabMixin", "all"),
                    },
                    {
                        "label": translate("GwcTabMixin", "To zoom"),
                        "type": "spin",
                        "min": 0,
                        "max": 40,
                        "none_text": translate("GwcTabMixin", "all"),
                    },
                ],
                "help": translate(
                    "GwcTabMixin",
                    "The tile grids that hold the layer. Set the zoom levels to serve "
                    'only those; "all" leaves that end open.',
                ),
            },
            {
                "key": "formats",
                "label": translate("GwcTabMixin", "Formats"),
                "type": "table",
                "unique": True,
                "required": True,
                "choices": list(KNOWN_FORMATS),
                "columns": [{"label": translate("GwcTabMixin", "Format")}],
                "min_height": 120,
                "max_height": 220,
                "help": translate(
                    "GwcTabMixin", "The image formats of the cached tiles."
                ),
            },
            {
                "key": "meta_width",
                "label": translate("GwcTabMixin", "Meta-tile width"),
                "type": "spinbox",
                "min": 1,
                "max": 20,
                "group": advanced,
                "help": translate(
                    "GwcTabMixin",
                    "Tiles rendered together in one request, so labels are not cut "
                    "at tile edges.",
                ),
            },
            {
                "key": "meta_height",
                "label": translate("GwcTabMixin", "Meta-tile height"),
                "type": "spinbox",
                "min": 1,
                "max": 20,
                "group": advanced,
            },
            {
                "key": "gutter",
                "label": translate("GwcTabMixin", "Gutter (px)"),
                "type": "spinbox",
                "min": 0,
                "max": 100,
                "group": advanced,
                "help": translate(
                    "GwcTabMixin",
                    "Extra pixels rendered around each meta-tile, for symbols that "
                    "overflow.",
                ),
            },
            {
                "key": "expire_cache",
                "label": translate("GwcTabMixin", "Expire cached tiles after (s)"),
                "type": "spinbox",
                "min": 0,
                "max": 2147483647,
                "group": advanced,
                "help": translate(
                    "GwcTabMixin", "0 keeps a tile until Truncate deletes it."
                ),
            },
            {
                "key": "expire_clients",
                "label": translate("GwcTabMixin", "Client cache max-age (s)"),
                "type": "spinbox",
                "min": 0,
                "max": 2147483647,
                "group": advanced,
                "help": translate(
                    "GwcTabMixin",
                    "Sent to browsers and QGIS as Cache-Control; 0 sends none.",
                ),
            },
            {
                "key": "filters",
                "label": translate("GwcTabMixin", "Parameter filters"),
                "type": "textarea",
                "code": "xml",
                "group": translate("GwcTabMixin", "Parameter filters"),
                "min_height": 200,
                "help": translate(
                    "GwcTabMixin",
                    "Which request parameters get a cache of their own (STYLES, "
                    "CQL_FILTER, TIME...), as GeoWebCache's XML. A value no filter "
                    "allows is not cached.",
                ),
            },
        ]

    def _show_gwc_layer_info(self, row_data):
        """Open a cached layer's configuration for editing."""
        name = row_data[0]
        fetched = self._fetch(
            lambda: (self._gwc_layer_xml(name), self._gridset_names()),
            translate("GwcTabMixin", "Failed to load the tile cache of '{}'").format(
                name
            ),
        )
        if fetched is None:
            return
        xml_text, gridset_names = fetched

        dlg = ResourceFormDialog(
            title=translate("GwcTabMixin", "Tile cache of '{}'").format(name),
            description=translate(
                "GwcTabMixin",
                "How the tile cache stores this layer. Changes apply to the tiles "
                "rendered from now on; Truncate deletes the tiles cached already.",
            ),
            fields=self._gwc_fields(gridset_names),
            values=self._gwc_form_values(xml_text),
            parent=self,
            # Pure: a zoom range backwards, a gridset twice or filters
            # that are not XML stay in the form, with the rest of the edit.
            validate=lambda values: self._gwc_xml_with_values(xml_text, values),
        )
        before = dlg.get_values()  # as the form gives it back untouched
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        values = dlg.get_values()
        if self._run_action(
            lambda: self._wait_for_save(
                lambda: self._save_gwc_layer(name, before, values)
            ),
            translate("GwcTabMixin", "Failed to save the tile cache of '{}'").format(
                name
            ),
        ):
            self.show_success_message(
                translate("GwcTabMixin", "Tile cache of '{}' saved.").format(name)
            )
            self._load_gwc_layers()

    def _add_gwc_layer(self):
        """Open a form dialog to start caching a published layer."""
        fetched = self._fetch(
            lambda: (self._uncached_layer_names(), self._gridset_names()),
            translate("GwcTabMixin", "Failed to list the layers that can be cached"),
        )
        if fetched is None:
            return
        candidates, gridset_names = fetched
        if not candidates:
            self.show_warning_message(
                translate(
                    "GwcTabMixin",
                    "Every published layer is cached already. GeoServer caches new "
                    "layers by itself.",
                )
            )
            return

        dlg = ResourceFormDialog(
            title=translate("GwcTabMixin", "Add a Layer to the Cache"),
            description=translate(
                "GwcTabMixin",
                "GeoServer caches every new layer by itself, so this is for a layer "
                "whose cache was removed. The cache stores a tile the first time a "
                "client requests it.",
            ),
            fields=self._gwc_fields(gridset_names, layer_names=candidates),
            # From the template the create writes, so its STYLES filter shows
            # in the form and an untouched form keeps it.
            values={
                **self._gwc_form_values(_NEW_LAYER_XML.format(name="")),
                "gridsets": [[name, None, None] for name in DEFAULT_GRIDSETS],
                "formats": list(DEFAULT_FORMATS),
            },
            parent=self,
            ok_label=translate("GwcTabMixin", "Create"),
            # A refusal (a zoom range backwards, a layer cached meanwhile)
            # stays in the form, with the rest of what was typed.
            validate=self._form_check(self._check_new_gwc_layer),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        values = dlg.get_values()
        if self._run_action(
            lambda: self._wait_for_save(
                lambda: self._create_gwc_layer_from_values(values)
            ),
            translate("GwcTabMixin", "Failed to cache '{}'").format(
                values.get("layer", "")
            ),
        ):
            self.show_success_message(
                translate("GwcTabMixin", "'{}' is now cached.").format(values["layer"])
            )
            self._load_gwc_layers()

    # -- Seed, reseed, truncate -------------------------------------------------

    def _seed_path(self, name, ext=".json"):
        """GWC's seed endpoint for one cached layer."""
        return f"{self._gwc_base()}/seed/{quote(name, safe=':')}{ext}"

    @staticmethod
    def _seed_request(name, values):
        """The seedRequest document for the form's values. Pure.

        TODO(#1): the library has no seed call (row 59). Measured on 2.28.5:
        GWC answers 200 and starts `threadCount` tasks; an unknown gridset is
        a 500 naming it.
        """
        start, stop = int(values["zoom_start"]), int(values["zoom_stop"])
        if start > stop:
            raise ValueError(
                translate("GwcTabMixin", "The first zoom level is after the last.")
            )
        request = {
            "name": name,
            "gridSetId": values["gridset"],
            "format": values["format"],
            "type": values["type"],
            "zoomStart": start,
            "zoomStop": stop,
            "threadCount": int(values["threads"]),
        }
        bounds = (values.get("bounds") or "").strip()
        if bounds:
            try:
                coords = [float(part) for part in bounds.replace(",", " ").split()]
            except ValueError:
                coords = []
            if len(coords) != 4 or coords[0] >= coords[2] or coords[1] >= coords[3]:
                raise ValueError(
                    translate(
                        "GwcTabMixin",
                        "The area is not a box: each minimum must be below its "
                        "maximum.",
                    )
                )
            request["bounds"] = {"coords": {"double": coords}}
        parameters = values.get("parameters") or {}
        if parameters:
            request["parameters"] = {
                "entry": [
                    {"string": [key.strip(), str(value).strip()]}
                    for key, value in parameters.items()
                ]
            }
        return {"seedRequest": request}

    @staticmethod
    def _seed_tasks_text(payload):
        """GWC's task list as one line per task. Pure.

        Each task is [tiles done, tiles total, seconds left, id, state]; GWC
        writes -1 for a count it has not made yet.
        """
        tasks = (payload or {}).get("long-array-array") or []
        if not tasks:
            return translate("GwcTabMixin", "No task running for this layer.")
        states = {
            -1: translate("GwcTabMixin", "aborted"),
            0: translate("GwcTabMixin", "pending"),
            1: translate("GwcTabMixin", "running"),
            2: translate("GwcTabMixin", "done"),
        }
        lines = []
        for done, total, left, task_id, state in tasks:
            if total < 0 or done < 0:
                progress = translate("GwcTabMixin", "counting the tiles")
            elif total < 2**31:
                progress = translate(
                    "GwcTabMixin", "{} of %n tile(s)", None, total
                ).format(done)
            else:  # Qt's count is a C int, which a world seed's total overflows
                progress = translate("GwcTabMixin", "{} of {} tiles").format(
                    done, total
                )
            eta = (
                translate("GwcTabMixin", ", about {} s left").format(left)
                if left > 0
                else ""
            )
            lines.append(
                translate("GwcTabMixin", "Task {}: {}, {}{}").format(
                    task_id, states.get(state, state), progress, eta
                )
            )
        return "\n".join(lines)

    def _seed_fields(self, gridsets, formats, canvas=None):
        """Field definitions for the seed form."""
        return [
            {
                "key": "type",
                "label": translate("GwcTabMixin", "Task"),
                "type": "combo",
                "options": _seed_types(),
                "help": translate(
                    "GwcTabMixin",
                    "Seed renders the missing tiles, Reseed renders them all again, "
                    "Truncate deletes them",
                ),
            },
            {
                "key": "gridset",
                "label": translate("GwcTabMixin", "Gridset"),
                "type": "combo",
                "options": list(gridsets),
            },
            {
                "key": "format",
                "label": translate("GwcTabMixin", "Format"),
                "type": "combo",
                "options": list(formats),
            },
            {
                "key": "zoom_start",
                "label": translate("GwcTabMixin", "From zoom level"),
                "type": "spinbox",
                "min": 0,
                "max": 40,
                "default": 0,
            },
            {
                "key": "zoom_stop",
                "label": translate("GwcTabMixin", "To zoom level"),
                "type": "spinbox",
                "min": 0,
                "max": 40,
                "default": 8,
                "help": translate(
                    "GwcTabMixin",
                    "Each level has 4 times the tiles of the one before; the task "
                    "list shows the total once the tile cache has counted it",
                ),
            },
            {
                "key": "threads",
                "label": translate("GwcTabMixin", "Threads"),
                "type": "spinbox",
                "min": 1,
                "max": 32,
                "default": 2,
            },
            {
                "key": "bounds",
                "label": translate("GwcTabMixin", "Only this area"),
                "type": "extent",
                "canvas": canvas,
                "group": translate("GwcTabMixin", "Advanced"),
                "help": translate(
                    "GwcTabMixin",
                    "From the map view, a layer or a bookmark, or typed in the order "
                    "of QGIS (xmin, xmax, ymin, ymax). Sent in the CRS of the "
                    "gridset; empty means the whole extent of the layer.",
                ),
            },
            {
                "key": "parameters",
                "label": translate("GwcTabMixin", "Parameters"),
                "type": "keyvalue",
                "group": translate("GwcTabMixin", "Advanced"),
                "help": translate(
                    "GwcTabMixin",
                    "A parameter and its value (STYLES, population) for the tiles "
                    "of one parameter filter value. Empty: the default tiles.",
                ),
            },
        ]

    def _seed_gwc_layer(self, row_data):
        """Seed, reseed or truncate part of a layer's cache, then watch it."""
        name = row_data[0]

        def load():
            current = self._gwc_form_values(self._gwc_layer_xml(name))
            # Each gridset's CRS: an area picked on the map is sent in it.
            # One GET per gridset, in parallel; any failure fails the load.
            names = [row[0] for row in current["gridsets"]]
            crs = {}
            for gridset, (result, error) in zip(
                names, self._fan_out(self._gridset_crs, names)
            ):
                if error is not None:
                    raise error
                crs[gridset] = result
            return current, crs

        fetched = self._fetch(
            load,
            translate("GwcTabMixin", "Failed to load the tile cache of '{}'").format(
                name
            ),
        )
        if fetched is None:
            return
        current, crs = fetched
        gridsets = list(crs)
        canvas = self.iface.mapCanvas() if getattr(self, "iface", None) else None
        dlg = ResourceFormDialog(
            title=translate("GwcTabMixin", "Seed or Truncate '{}'").format(name),
            description=translate(
                "GwcTabMixin",
                "The tile cache runs the task in the background; the task list opens "
                "next and shows its progress.",
            ),
            fields=self._seed_fields(gridsets, current["formats"], canvas),
            parent=self,
            ok_label=translate("GwcTabMixin", "Start"),
            # Pure: a refusal stays in the form, before a Truncate asks.
            validate=lambda values: self._seed_request(name, values),
        )
        dlg.get_widget("gridset").currentTextChanged.connect(
            lambda gridset: dlg.set_extent_crs("bounds", crs.get(gridset))
        )
        dlg.set_extent_crs("bounds", crs.get(dlg.get_widget("gridset").currentText()))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        values = dlg.get_values()
        # A truncate deletes tiles, like the row action, which asks first.
        if values["type"] == "truncate" and not self._confirm_delete(
            translate(
                "GwcTabMixin",
                "Truncate the tiles of layer '{}'?",
            ).format(name),
            cascade=translate(
                "GwcTabMixin",
                "This deletes the tiles of this gridset, format and zoom range "
                "(within the area, if one is set). The cache renders them again on "
                "request.",
            ),
        ):
            return
        if self._run_action(
            # TODO(#1): no seed in the library (row 59).
            lambda: self._wait_for_save(
                lambda: self._raw_rest(
                    "post", self._seed_path(name), json=self._seed_request(name, values)
                )
            ),
            translate("GwcTabMixin", "Failed to start the task on '{}'").format(name),
        ):
            self._show_seed_tasks(row_data)

    def _read_seed_tasks(self, client, path):
        """The task list as text, or why it could not be read. Runs in a worker.

        The error comes back as text, not raised: the monitor polls every 2 s,
        and a banner each time would bury the message bar.
        """
        try:
            # TODO(#1): nor a task list (row 59).
            return self._seed_tasks_text(raw_rest(client, "get", path).json())
        except Exception as error:  # noqa: BLE001 (shown in the dialog)
            return self._error_text(error)

    def _show_seed_tasks(self, row_data):
        """The layer's running tasks, refreshed every 2 s, with Stop all."""
        name = row_data[0]
        client = self.gs.rest_service.rest_client
        path = self._seed_path(name)
        dlg = ResourceFormDialog(
            title=translate("GwcTabMixin", "Tasks of '{}'").format(name),
            fields=[
                {
                    "key": "tasks",
                    "label": translate("GwcTabMixin", "Tasks"),
                    "type": "textarea",
                    "read_only": True,
                    "wide": True,
                }
            ],
            values={"tasks": translate("GwcTabMixin", "Asking the tile cache…")},
            parent=self,
        )
        dlg.hide_save_button()
        stop = dlg.add_button(translate("GwcTabMixin", "Stop all"))
        failure = translate("GwcTabMixin", "Failed to read the tasks")
        closed = []

        def read(_task):
            return self._read_seed_tasks(client, path)

        def landed(text):
            if closed or sip.isdeleted(dlg) or not dlg.isVisible():
                return
            dlg.get_widget("tasks").setPlainText(text)
            timer.start()  # the next read only once this one has landed

        def poll():
            self._run_quietly(failure, read, landed)

        # Single shot, re-armed by landed: a read slower than the interval
        # used to be superseded by the next tick, so nothing ever landed and
        # a dead server collected a new pending request every 2 s.
        timer = QTimer(dlg)
        timer.setSingleShot(True)
        timer.setInterval(2000)
        timer.timeout.connect(poll)

        def stop_all():
            if self._run_action(
                # TODO(#1): nor a kill of the tasks (row 59).
                lambda: self._wait_for_save(
                    lambda: self._raw_rest(
                        "post", self._seed_path(name, ""), data={"kill_all": "all"}
                    )
                ),
                translate("GwcTabMixin", "Failed to stop the tasks on '{}'").format(
                    name
                ),
            ):
                # In the dialog: a banner would sit behind this modal one.
                dlg.get_widget("tasks").setPlainText(
                    translate("GwcTabMixin", "Stopping the tasks…")
                )
                timer.stop()
                poll()

        stop.clicked.connect(stop_all)
        poll()
        dlg.exec()
        closed.append(True)
        timer.stop()

    # -- Truncate / remove ----------------------------------------------------

    def _truncate_gwc_layer(self, row_data):
        """Drop the layer's cached tiles after confirmation."""
        name = row_data[0]
        if not self._confirm_delete(
            translate(
                "GwcTabMixin",
                "Truncate the tiles of layer '{}'?",
            ).format(name),
            cascade=translate(
                "GwcTabMixin",
                "This deletes every cached tile of this layer, in every gridset and "
                "format. The layer and its cache configuration stay; the cache "
                "renders tiles again on request.",
            ),
        ):
            return
        if self._run_action(
            lambda: self._wait_for_save(lambda: self._do_truncate_gwc_layer(name)),
            translate(
                "GwcTabMixin", "Failed to truncate the tile cache of '{}'"
            ).format(name),
        ):
            self.show_success_message(
                translate("GwcTabMixin", "Tile cache of '{}' truncated.").format(name)
            )

    def _remove_gwc_layer(self, row_data):
        """Stop caching a single layer after confirmation."""
        self._remove_selected_gwc_layers([row_data])

    def _remove_selected_gwc_layers(self, selected_rows):
        """Stop caching one or more layers after confirmation."""
        self._delete_many(
            [
                (row[0], lambda name=row[0]: self._do_remove_gwc_layer(name))
                for row in selected_rows
            ],
            self._load_gwc_layers,
            ask=self._one_or_many(
                translate("GwcTabMixin", "Stop caching layer '{}'?"),
                lambda n: translate(
                    "GwcTabMixin",
                    "Stop caching %n layer(s)?",
                    None,
                    n,
                ),
            ),
            done=self._one_or_many(
                translate("GwcTabMixin", "Layer '{}' removed from the cache."),
                lambda n: translate(
                    "GwcTabMixin", "%n layer(s) removed from the cache.", None, n
                ),
            ),
            cascade=translate(
                "GwcTabMixin",
                "This removes the cached tiles and the cache configuration. The layer "
                "stays published and can be added to the cache again.",
            ),
        )
