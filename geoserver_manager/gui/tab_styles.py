#! python3  # noqa: E265

"""
Styles tab: list, view/edit, upload and delete styles.

Used as a mixin for GeoServerMainDialog.
"""

import html
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from qgis.core import Qgis
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import QDialog, QFileDialog

from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog
from geoserver_manager.gui.scope import GLOBAL, PENDING, global_label, scope
from geoserver_manager.toolbelt.payload import unwrap
from geoserver_manager.toolbelt.sld import (
    SLD_1_0,
    apply_sld_to_layer,
    has_raster_symbolizer,
    icon_package,
    icons_for_qgis,
    layer_to_sld,
    relative_hrefs,
    sld_content_type,
    sld_version,
    style_text,
    styleable_project_layers,
    unresolved_icons,
    utf8_sld,
)

# Styles live either globally or inside a workspace; the label for the global
# scope and the mapping back to None are shared with layer groups (gui.scope).

# The content type GeoServer reads each format's body in. CSS, YSLD and
# MBStyle need their GeoServer extension; without it GeoServer answers "No such
# style handler", which the banner shows as it is.
_CONTENT_TYPES = {
    "sld": SLD_1_0,
    "css": "application/vnd.geoserver.geocss+css",
    "ysld": "application/vnd.geoserver.ysld+yaml",
    "mbstyle": "application/vnd.geoserver.mbstyle+json",
}
_EDITABLE_FORMATS = tuple(_CONTENT_TYPES)
# Each format as its authors spell it (the combo showed "MBSTYLE").
_FORMAT_NAMES = {"sld": "SLD", "css": "CSS", "ysld": "YSLD", "mbstyle": "MBStyle"}
# How the form's code editor highlights each format; YSLD stays plain.
_CODE_LANGUAGES = {"sld": "xml", "css": "css", "mbstyle": "json"}
# A file's format, from its extension; a .zip is an SLD with its images.
_FORMAT_OF_SUFFIX = {
    ".sld": "sld",
    ".zip": "zip",
    ".css": "css",
    ".ysld": "ysld",
    ".yaml": "ysld",
    ".mbstyle": "mbstyle",
    ".json": "mbstyle",
}

_SOURCE_PASTE = "Paste"
_SOURCE_FILE = "From file"
_SOURCE_QGIS = "From a QGIS layer"


def _quoted(name, workspace_name, body):
    """`create_style`'s positional (name, body, workspace), the names quoted.

    TODO(#1): the library interpolates a style's names into its path raw,
    and `requests` sends `styles/a#b` as `styles/a`, a different style.
    """
    return (
        quote(name, safe=""),
        body,
        quote(workspace_name, safe="") if workspace_name else None,
    )


# Every user-visible string in this file goes through translate() with this
# file's own class as the context. self.tr() cannot: pylupdate extracts it
# under StyleTabMixin, but at runtime self.tr is QObject.tr with the context of the
# *instance's* class, GeoServerMainDialog. QDialog precedes the mixins in the
# MRO, so every lookup would miss. A wrapper function would not be extracted
# at all (pylupdate only understands a literal context), hence the repetition.
translate = QCoreApplication.translate


class StyleTabMixin:
    """Mixin that adds style methods to the main dialog."""

    def _load_styles(self):
        """Arm the Styles tab, then fetch its rows in the background."""
        self._setup_add_button(
            translate("StyleTabMixin", "Upload a Style"),
            translate(
                "StyleTabMixin",
                "Upload a style: a pasted document or a file (SLD, CSS, YSLD, "
                "MBStyle), or a QGIS layer's symbology",
            ),
            self._add_style,
        )
        self._setup_delete_selected_button(self._delete_selected_styles)
        self._name_click_callback = self._show_style_info
        self._extra_click_callbacks = {
            translate("StyleTabMixin", "Workspace"): self._open_workspace_from_row
        }
        self._row_actions = [
            (
                "apply-style",
                translate("StyleTabMixin", "Apply to a QGIS layer"),
                self._apply_style_to_qgis,
            ),
            (
                "save-style",
                translate("StyleTabMixin", "Save to disk"),
                self._save_style_to_disk,
            ),
            (
                "copy-style",
                translate("StyleTabMixin", "Copy"),
                self._copy_style,
            ),
            (
                # Not browse-resources: that one is a quick button in the row.
                "layers",
                translate("StyleTabMixin", "Used by"),
                self._show_style_users,
            ),
            (
                "delete",
                translate("StyleTabMixin", "Delete"),
                self._delete_style,
                translate(
                    "StyleTabMixin", "Layers that use it fall back to the default style"
                ),
            ),
        ]
        self._setup_table(
            [
                translate("StyleTabMixin", "Name"),
                translate("StyleTabMixin", "Workspace"),
                translate("StyleTabMixin", "Format"),
                translate("StyleTabMixin", "Version"),
                self.actions_column_label(),
            ]
        )
        self._row_detail = lambda row: self._style_summary(row[0], scope(row[1]))
        self._detail_columns = (2, 3)
        self._start_load(
            translate("StyleTabMixin", "Failed to load styles"), self._fetch_style_rows
        )

    def _fetch_style_rows(self, task=None):
        """(rows, failures) for the Styles table. Runs in a worker thread.

        The format and the SLD version come from each style's definition. One
        GET per style, fanned out, because whether a style is SLD decides what
        *Apply to a QGIS layer* can do with it.
        """
        pairs, failures = self._scoped_names(
            lambda: [self._name_of(s) for s in self._fetch_list(self.gs.get_styles)],
            lambda ws: self._fetch_list(self.gs.get_styles, ws),
            self._get_workspace_names(),
            task,
        )
        # Format and version follow for the page shown.
        rows = [[name, ws_label, PENDING, PENDING] for name, ws_label in pairs]
        return rows, failures

    def _style_summary(self, name, workspace_name):
        """(format, SLD version) cells of one style. Raises on HTTP errors."""
        definition = self._check(self.gs.get_style_definition(name, workspace_name))
        if not isinstance(definition, dict):
            return ("-", "-")
        return (
            self._style_format(definition),
            self._language_version(definition) or "-",
        )

    @staticmethod
    def _style_format(definition):
        """A style's format as GeoServer names it, in lower case; SLD if unsaid."""
        definition = definition if isinstance(definition, dict) else {}
        return str(definition.get("format") or "sld").lower()

    # -- Body ------------------------------------------------------------------

    def _style_with_body(self, name, workspace_name):
        """(definition, format, body) of one style. Runs in a worker."""
        definition = self._check(self.gs.get_style_definition(name, workspace_name))
        definition = definition if isinstance(definition, dict) else {}
        style_format = self._style_format(definition)
        return (
            definition,
            style_format,
            self._style_body(name, workspace_name, style_format),
        )

    def _style_as_stored(self, name, workspace_name):
        """(definition, format, body), the body as the bytes GeoServer keeps.

        `{style}.sld` serves an SLD 1.1 style as its 1.0 rendition, so Copy
        stored a 1.0 conversion and Save to disk wrote one. The stored file is
        the resource under styles/ (measured on 2.28.5). Bytes, not text: an
        ISO-8859-1 body decoded with replacement characters and written back
        as UTF-8 is another file. Runs in a worker.
        TODO(#1): no call for a style's stored file in the library (row 58).
        """
        definition = self._check(self.gs.get_style_definition(name, workspace_name))
        definition = definition if isinstance(definition, dict) else {}
        style_format = self._style_format(definition)
        path = self._stored_sld_path(workspace_name, definition) or self._style_path(
            name, workspace_name, style_format
        )
        return definition, style_format, self._raw_rest("get", path).content

    def _stored_sld_path(self, workspace_name, definition):
        """The resource path of a stored SLD 1.1 file, else None: for any
        other style, its own path serves the stored body."""
        filename = definition.get("filename")
        if not (
            self._style_format(definition) == "sld"
            and self._language_version(definition).startswith("1.1")
            and filename
        ):
            return None
        folder = (
            f"workspaces/{quote(workspace_name, safe='')}/styles"
            if workspace_name
            else "styles"
        )
        base = self.gs.rest_service.rest_endpoints.base_url
        return f"{base}/resource/{folder}/{quote(filename, safe='')}"

    def _sld_of(self, name, workspace_name):
        """A style's SLD, what QGIS reads, its icons as URLs. Runs in a worker.

        A CSS, YSLD or MBStyle style comes back as GeoServer's own conversion:
        its `.sld` path renders any format as SLD (measured on 2.28.5). An SLD
        1.1 style is read as stored: its `.sld` rendition names each icon by
        its path on the server. The layer tree's Apply style goes through
        here too.
        TODO(#1): no call for a style's stored file in the library (row 58).
        """
        definition = self._check(self.gs.get_style_definition(name, workspace_name))
        definition = definition if isinstance(definition, dict) else {}
        stored = self._stored_sld_path(workspace_name, definition)
        if stored:
            sld = style_text(self._raw_rest("get", stored).content)
        else:
            sld = self._style_body(name, workspace_name, "sld")
        return icons_for_qgis(sld, self.gs.url, workspace_name)

    def _style_body(self, name, workspace_name, style_format):
        """The style document itself (SLD, CSS, …), as text (`style_text`).

        TODO(#1): upstream as get_style_body(name, ws, format) on the facade.
        rest_service.get_style() exists but its endpoint only knows json / sld /
        mbstyle, so a CSS style comes back as its JSON definition instead of its
        body. Workaround: GET the style path with the definition's own format.
        """
        path = self._style_path(name, workspace_name, style_format)
        return style_text(self._raw_rest("get", path).content)

    def _save_style_body(self, name, workspace_name, style_format, body):
        """PUT a new body for an existing style, leaving its definition alone."""
        # Not create_style_from_string: that also rewrites the definition and
        # renames the file to <name>.sld, which changes a style that was
        # e.g. popshade.sld under the hood.
        if style_format == "sld":
            self._put_sld_body(name, workspace_name, body)
            return
        if style_format == "mbstyle":
            self._check(
                self.gs.rest_service.create_style(
                    *_quoted(name, workspace_name, body.encode("utf-8")),
                    format=style_format,
                )
            )
            return
        # TODO(#1): create_style() knows no CSS or YSLD content type, and
        # fails with UnboundLocalError on them (row 58).
        self._raw_rest(
            "put",
            self._style_path(name, workspace_name, style_format),
            data=body.encode("utf-8"),
            headers={"Content-Type": _CONTENT_TYPES[style_format]},
        )

    def _put_sld_body(self, name, workspace_name, sld, local_icons=False):
        """PUT an SLD body with the content type its own version needs.

        TODO(#1): upstream as a content type chosen from the document (or a
        `content_type=` argument). rest_service.create_style() derives it from
        the *format* alone and only knows application/vnd.ogc.sld+xml, so an
        SLD 1.1 document (what QgsMapLayer.saveSldStyle() writes for a vector
        layer; a raster comes out as SLD 1.0) is stored with languageVersion
        1.0.0: accepted, rendered, and mislabelled. Sending
        application/vnd.ogc.se+xml records it as 1.1.0. The body goes as
        UTF-8 (`utf8_sld`): GeoServer reads it so, whatever its declaration
        says. An SLD that draws icons from files on this machine first goes
        as a zip with them (`icon_package`). That PUT unpacks the icons but
        keeps the style's recorded format and version (2.28.5: a CSS style
        then held SLD and drew nothing), so the zip's SLD follows as below.

        :param local_icons: package the icons only when the SLD was made on
            this machine (a QGIS export, a file or text the user gave). A body
            the server supplied (the edit form) could name any file here,
            which would be read and uploaded without the user knowing.
        """
        content_type = sld_content_type(sld)
        data = utf8_sld(sld)
        package = icon_package(data.decode("utf-8"), name) if local_icons else None
        if package is not None:
            zipped, data = package
            self._check(
                self.gs.rest_service.create_style(
                    *_quoted(name, workspace_name, zipped), format="zip"
                )
            )
        if content_type == SLD_1_0:
            self._check(
                self.gs.rest_service.create_style(
                    *_quoted(name, workspace_name, data), format="sld"
                )
            )
            return
        self._raw_rest(
            "put",
            self._style_path(name, workspace_name, "sld"),
            data=data,
            headers={"Content-Type": content_type},
        )

    def _style_path(self, name, workspace_name, style_format):
        """The style's REST path in any format, its segments URL-quoted.

        TODO(#1): `RestEndpoints.style()` interpolates the names raw, and
        `requests` sends `styles/a#b.json` as `styles/a`, a different style.
        Pre-quoting the segments the builder receives is the smallest fix; it
        has to go when the library quotes them itself, or `%` doubles. The
        builder also appends an extension for json, sld and mbstyle only: a
        CSS or YSLD body PUT to the bare path is a 500 "No such style
        handler" (row 58), so the .json path gets its suffix swapped.
        """
        path = self.gs.rest_service.rest_endpoints.style(
            quote(name, safe=""),
            quote(workspace_name, safe="") if workspace_name else None,
            format="json",
        )
        return path[: -len(".json")] + f".{style_format}"

    # -- View / edit -----------------------------------------------------------

    @staticmethod
    def _language_version(definition):
        """The SLD version GeoServer recorded for a style, or "" if unknown."""
        value = (definition or {}).get("languageVersion")
        if isinstance(value, dict):
            value = value.get("version")
        return str(value) if value else ""

    def _style_fields(self, editable, language_version="", style_format="sld"):
        """Field definitions for the style dialog.

        The definition comes first, on its own tab: it is the one thing the
        dialog edits, and on a second tab it hid behind read-only details.
        """
        return [
            {
                "key": "body",
                "label": translate("StyleTabMixin", "Definition"),
                "type": "textarea",
                "read_only": not editable,
                "required": editable,
                "group": translate("StyleTabMixin", "Definition"),
                "min_height": 320,
                "max_height": None,  # grow with the dialog
                "code": _CODE_LANGUAGES.get(style_format, True),
                "wide": True,
                "help": (
                    translate(
                        "StyleTabMixin",
                        "Edit and Save to replace the style on the server.",
                    )
                    if editable
                    else translate(
                        "StyleTabMixin",
                        "Read-only: this editor saves only SLD, CSS, YSLD and MBStyle "
                        "bodies.",
                    )
                ),
            },
            {
                "key": "name",
                "group": translate("StyleTabMixin", "Details"),
                "label": translate("StyleTabMixin", "Name"),
                "type": "text",
                "required": True,
                "help": translate(
                    "StyleTabMixin",
                    "Renaming keeps every layer and group that uses the style.",
                ),
            },
            {
                "key": "workspace",
                "group": translate("StyleTabMixin", "Details"),
                "label": translate("StyleTabMixin", "Workspace"),
                "type": "text",
                "read_only": True,
            },
            {
                "key": "format",
                "group": translate("StyleTabMixin", "Details"),
                "label": translate("StyleTabMixin", "Format"),
                "type": "text",
                "read_only": True,
            },
            {
                "key": "version",
                "group": translate("StyleTabMixin", "Details"),
                "label": translate("StyleTabMixin", "SLD version"),
                "type": "text",
                "read_only": True,
                "help": (
                    # GeoServer keeps the 1.1 document but serves .sld as its
                    # 1.0 rendition, so the body below is not the stored bytes.
                    translate(
                        "StyleTabMixin",
                        "Stored as SLD 1.1 (Symbology Encoding), what QGIS "
                        "exports. GeoServer serves it here as its SLD 1.0 "
                        "rendition, and saving stores that rendition instead.",
                    )
                    if language_version.startswith("1.1")
                    else None
                ),
            },
            {
                "key": "filename",
                "group": translate("StyleTabMixin", "Details"),
                "label": translate("StyleTabMixin", "File"),
                "type": "text",
                "read_only": True,
            },
            {
                "key": "legend",
                "group": translate("StyleTabMixin", "Details"),
                "label": translate("StyleTabMixin", "Legend"),
                "type": "image",
                "placeholder": translate(
                    "StyleTabMixin", "Asking GeoServer for the legend…"
                ),
                "help": translate(
                    "StyleTabMixin", "As GeoServer renders it (GetLegendGraphic)."
                ),
            },
        ]

    def _show_style_info(self, row_data):
        """Open a style: definition read-only, body editable for SLD/MBStyle."""
        name, workspace_name = row_data[0], scope(row_data[1])

        fetched = self._fetch(
            lambda: self._style_with_body(name, workspace_name),
            translate("StyleTabMixin", "Failed to load style '{}'").format(name),
        )
        if fetched is None:
            return
        definition, style_format, body = fetched
        editable = style_format in _EDITABLE_FORMATS
        language_version = self._language_version(definition)

        def check(values):
            new_name = values["name"].strip()
            if new_name != name:
                self._require_safe_name(new_name)
                self._refuse_taken_style(new_name, workspace_name)

        dlg = ResourceFormDialog(
            title=translate("StyleTabMixin", "Style '{}'").format(name),
            description=(
                translate(
                    "StyleTabMixin",
                    "Edit the definition, then Save to replace it on the server. Every "
                    "layer that uses the style changes with it.",
                )
                if editable
                else None
            ),
            fields=self._style_fields(editable, language_version, style_format),
            values={
                "name": name,
                "workspace": global_label() if row_data[1] == GLOBAL else row_data[1],
                "format": style_format,
                "version": language_version or "-",
                "filename": definition.get("filename", ""),
                "body": body,
            },
            parent=self,
            validate=self._form_check(check),
        )
        self._load_legend(
            dlg, name, workspace_name, body if style_format == "sld" else None
        )
        if not editable:
            dlg.hide_save_button()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        values = dlg.get_values()
        new_body, new_name = values["body"], values["name"].strip()
        if new_body == body.strip() and new_name == name:
            return

        def save():
            if new_name != name:
                # Before the body: a taken name used to fail the save after
                # the new body was already live on every layer using it.
                self._require_safe_name(new_name)
                self._refuse_taken_style(new_name, workspace_name)
            try:
                if new_body != body.strip():
                    self._save_style_body(name, workspace_name, style_format, new_body)
                if new_name != name:
                    self._rename_style(name, workspace_name, new_name)
            except Exception:
                self._refuse_gone_style(name, workspace_name)
                raise

        if self._run_action(
            lambda: self._wait_for_save(save),
            translate("StyleTabMixin", "Failed to save style '{}'").format(name),
        ):
            self.show_success_message(
                translate("StyleTabMixin", "Style '{}' saved.").format(new_name)
            )
            # After a body save too: a 1.0 body replaced by a 1.1 one is
            # recorded as 1.1, and the Version cell kept the old one.
            self._load_styles()

    def _rename_style(self, name, workspace_name, new_name):
        """Rename a style in place. Layers and groups keep using it.

        Measured on 2.28.5: a PUT of the new name on the definition renames the
        style and its references follow, since GeoServer links them by id.
        The caller has refused an unsafe or taken name before the body PUT.
        TODO(#1): no rename in the library (row 58).
        """
        self._raw_rest(
            "put",
            self._style_path(name, workspace_name, "json"),
            json={"style": {"name": new_name}},
        )

    def _refuse_taken_style(self, name, workspace_name):
        """Raise when the scope already has a style of that name."""
        if self._resource_exists(self.gs.get_style_definition, name, workspace_name):
            raise ValueError(
                translate("StyleTabMixin", "Style '{}' already exists in {}.").format(
                    name, workspace_name or global_label()
                )
            )

    def _refuse_gone_style(self, name, workspace_name):
        """After a failed save: raise when the style is no longer there.

        GeoServer answers a save of a deleted style with "Invalid style" or a
        NullPointerException (measured on 2.28.5), which blamed the document.
        Only a 404 means gone: a 503 or a 401 on the re-read is the same
        trouble the save met, and the save's own error says more.
        """
        try:
            _, status = self.gs.get_style_definition(name, workspace_name)
        except Exception:  # the save's own error says more
            return
        if status == 404:
            raise ValueError(
                translate(
                    "StyleTabMixin",
                    "it was deleted on the server after the list was loaded. "
                    "Press Refresh (F5).",
                )
            )

    # -- Legend ----------------------------------------------------------------

    def _legend_layer(self, workspace_name, raster=False):
        """A layer of the style's kind to draw the legend with, or None.

        GetLegendGraphic needs a LAYER even for a stored style, and its kind
        matters (measured on 2.28.5): a raster style drawn with a vector layer
        is a blank 20x20 image, a vector style drawn with a raster layer an
        exception. So a coverage for a raster style, else a feature type, from
        the style's own workspace first, then from the others.
        TODO(#1): the library lists coverages and feature types per store
        only, so this GETs a workspace's collection across its stores.
        """
        base = self.gs.rest_service.rest_endpoints.base_url
        collection, list_key, item_key = (
            ("coverages", "coverages", "coverage")
            if raster
            else ("featuretypes", "featureTypes", "featureType")
        )

        def workspaces():
            if workspace_name:
                yield workspace_name
            yield from (
                ws for ws in self._get_workspace_names() if ws != workspace_name
            )

        for ws in workspaces():
            path = f"{base}/workspaces/{quote(ws, safe='')}/{collection}.json"
            payload = self._raw_rest("get", path).json()
            # A layer is named as its resource, bare within the workspace.
            for entry in self._unwrap(payload, list_key, item_key):
                return f"{ws}:{self._name_of(entry)}"
        return None

    def _legend_png(self, layer, name, workspace_name):
        """The legend GeoServer renders for the style, as PNG bytes.

        TODO(#1): get_legend_graphic() is a plain GET through the REST client
        (stateless, so fine in a worker), but it hands back the raw Response, an
        OGC exception is HTTP 200 with an XML body, and it runs with the
        client's 120 s timeout.
        """
        style = f"{workspace_name}:{name}" if workspace_name else name
        response = self.gs.get_legend_graphic(layer, style=style)
        if not response.headers.get("Content-Type", "").startswith("image/"):
            raise RuntimeError(self._ogc_exception_text(response.text))
        return response.content

    @staticmethod
    def _ogc_exception_text(text):
        """The sentence inside an OGC exception report, else its first line."""
        match = re.search(
            r"<(?:\w+:)?(?:ServiceException|ExceptionText)\b[^>]*>\s*([^<]+?)\s*<",
            text or "",
        )
        if match:
            # GeoServer escapes it: "No such style: roads &amp; rivers".
            return html.unescape(match.group(1))
        lines = (text or "").strip().splitlines()
        if lines:
            return lines[0][:200]
        return translate("StyleTabMixin", "GeoServer returned no image")

    def _load_legend(self, dlg, name, workspace_name, sld=None):
        """Fetch the legend into the dialog's image field, off the GUI thread.

        The dialog is modal and may be closed, even gone, before the picture
        lands, so the landing looks before it paints. Failures land in the
        field too: a banner would sit behind the modal.

        :param sld: the style's SLD when the dialog holds it; else its SLD
            rendition is read, to tell a raster style from a vector one.
        """
        closed = []
        dlg.finished.connect(lambda _result: closed.append(True))
        # GeoServer's words as they are: a "<b>" in a style name is no markup.
        dlg.get_widget("legend").setTextFormat(Qt.TextFormat.PlainText)

        def work(task):
            try:
                body = self._sld_of(name, workspace_name) if sld is None else sld
                raster = has_raster_symbolizer(body)
                layer = self._legend_layer(workspace_name, raster)
                if layer is None:
                    return None, (
                        translate(
                            "StyleTabMixin",
                            "No raster layer to draw this raster style's legend "
                            "with. GetLegendGraphic needs one.",
                        )
                        if raster
                        else translate(
                            "StyleTabMixin",
                            "No vector layer to draw the legend with. "
                            "GetLegendGraphic needs one.",
                        )
                    )
                return self._legend_png(layer, name, workspace_name), None
            except Exception as e:
                return None, translate("StyleTabMixin", "No legend: {}").format(
                    self._error_text(e)
                )

        def landed(result):
            if closed or sip.isdeleted(dlg):
                return
            png, problem = result
            pixmap = QPixmap()
            if png and pixmap.loadFromData(png):
                dlg.set_image("legend", pixmap)
            else:
                dlg.set_image(
                    "legend",
                    None,
                    problem
                    or translate("StyleTabMixin", "GeoServer did not return an image."),
                )

        self._run_quietly(
            translate("StyleTabMixin", "Failed to load the legend"), work, landed
        )

    # -- Upload ----------------------------------------------------------------

    def _upload_fields(self, workspace_names):
        return [
            {
                "key": "name",
                "label": translate("StyleTabMixin", "Name"),
                "type": "text",
                "required": True,
            },
            {
                "key": "workspace",
                "label": translate("StyleTabMixin", "Workspace"),
                "type": "combo",
                "options": [(global_label(), GLOBAL)] + list(workspace_names),
                "help": translate(
                    "StyleTabMixin",
                    "Layers of every workspace can use a global style",
                ),
            },
            {
                "key": "source",
                "label": translate("StyleTabMixin", "Source"),
                "type": "combo",
                "options": [
                    (translate("StyleTabMixin", "Paste"), _SOURCE_PASTE),
                    (translate("StyleTabMixin", "From file"), _SOURCE_FILE),
                    (translate("StyleTabMixin", "From a QGIS layer"), _SOURCE_QGIS),
                ],
            },
            {
                "key": "format",
                "label": translate("StyleTabMixin", "Format"),
                "type": "combo",
                # Their own names; the value is GeoServer's format key.
                "options": [
                    (_FORMAT_NAMES[style_format], style_format)
                    for style_format in _CONTENT_TYPES
                ],
                "help": translate(
                    "StyleTabMixin",
                    "CSS, YSLD and MBStyle need their GeoServer extension",
                ),
            },
            {
                "key": "sld",
                "label": translate("StyleTabMixin", "Style"),
                "type": "textarea",
                "required": True,
                # Highlighted as SLD, the usual paste; CSS or JSON just
                # stays plain in it.
                "code": "xml",
                "help": translate("StyleTabMixin", "Paste the style document here"),
            },
            {
                "key": "file",
                "label": translate("StyleTabMixin", "File"),
                "type": "file",
                "required": True,
                "visible": False,
                "filter": translate(
                    "StyleTabMixin",
                    "Styles (*.sld *.zip *.css *.ysld *.yaml *.mbstyle *.json);;"
                    "All files (*)",
                ),
                "help": translate(
                    "StyleTabMixin",
                    ".sld, a .zip with an SLD and its resources, .css, .ysld or "
                    ".mbstyle",
                ),
            },
            {
                "key": "qgis_layer",
                "label": translate("StyleTabMixin", "QGIS layer"),
                "type": "layer",
                "required": True,
                "visible": False,
                "help": translate(
                    "StyleTabMixin",
                    "The plugin exports the layer's symbology as SLD and uploads it "
                    "with the icons it draws. QGIS writes a vector layer's style as "
                    "SLD 1.1 and a raster layer's as SLD 1.0; GeoServer stores that "
                    "version.",
                ),
            },
        ]

    def _on_style_source_changed(self, dlg, source):
        dlg.set_field_visible("sld", source == _SOURCE_PASTE)
        dlg.set_field_visible("format", source == _SOURCE_PASTE)
        dlg.set_field_visible("file", source == _SOURCE_FILE)
        dlg.set_field_visible("qgis_layer", source == _SOURCE_QGIS)

    def _add_style(self):
        """Upload a style from pasted SLD, a file, or a QGIS layer."""
        workspace_names = self._fetch(
            self._get_workspace_names,
            translate("StyleTabMixin", "Failed to load the workspaces"),
        )
        if workspace_names is None:
            return
        dlg = ResourceFormDialog(
            title=translate("StyleTabMixin", "Upload a Style"),
            description=translate(
                "StyleTabMixin",
                "Create a style from a document you paste, a file you pick, or "
                "the symbology of a layer in this QGIS project.",
            ),
            fields=self._upload_fields(workspace_names),
            parent=self,
            ok_label=translate("StyleTabMixin", "Upload"),
            validate=self._form_check(self._check_new_style),
        )
        dlg.on_value_changed(
            "source", lambda source: self._on_style_source_changed(dlg, source)
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        values = dlg.get_values()
        failure = translate("StyleTabMixin", "Failed to upload style '{}'").format(
            values["name"]
        )
        if values.get("source") == _SOURCE_QGIS:
            # The export reads a live QGIS layer, so it stays on the GUI thread
            # (invariant 9); the upload is then a pasted SLD, off it.
            sld = self._fetch(
                lambda: layer_to_sld(values["qgis_layer"]), failure, in_worker=False
            )
            if sld is None:
                return
            values = dict(values, source=_SOURCE_PASTE, format="sld", sld=sld)
        if self._run_action(
            lambda: self._wait_for_save(lambda: self._create_style_from_values(values)),
            failure,
        ):
            self.show_success_message(
                translate("StyleTabMixin", "Style '{}' uploaded.").format(
                    values["name"]
                )
            )
            self._load_styles()

    def _check_new_style(self, values):
        """Refuse a name a URL would eat, or one taken. Reads only: the form
        runs it before it closes, the upload again."""
        name = values["name"].strip()
        self._require_safe_name(name)
        if values.get("source") == _SOURCE_FILE:
            path = Path(values["file"])
            if path.suffix.lower() not in _FORMAT_OF_SUFFIX:
                raise ValueError(
                    translate(
                        "StyleTabMixin",
                        "'{}' is not a style file GeoServer reads: pick a .sld, "
                        ".zip, .css, .ysld or .mbstyle file.",
                    ).format(path.name)
                )
        # create_style_* upsert (and rewrite the definition's filename)
        self._refuse_taken_style(name, scope(values["workspace"]))

    def _create_style_from_values(self, values):
        """Create a style through the library, refusing to overwrite an existing one."""
        name, workspace_name = values["name"].strip(), scope(values["workspace"])
        self._check_new_style(values)
        source = values.get("source")
        if source == _SOURCE_FILE:
            path = Path(values["file"])
            # Bytes: an SLD is decoded as it declares, then sent as UTF-8.
            self._create_style(
                name,
                workspace_name,
                _FORMAT_OF_SUFFIX[path.suffix.lower()],
                path.read_bytes(),
                local_icons=True,
            )
        else:
            # A QGIS layer arrives here as a pasted SLD: _add_style exports it
            # on the GUI thread first (invariant 9).
            style_format = (values.get("format") or "sld").lower()
            self._create_style(
                name, workspace_name, style_format, values["sld"], local_icons=True
            )

    def _create_style(
        self, name, workspace_name, style_format, body, local_icons=False
    ):
        """Create a style of any format from its body, in one request.

        TODO(#1): create_style_from_string() is SLD 1.0 only (row 58). So a
        POST to the collection with the body's own content type creates the
        definition and the body together (a PUT there is refused, 400). One
        request, measured on 2.28.5: an SLD 1.1 body is recorded as 1.1, and
        a body GeoServer refuses leaves nothing. Creating the definition
        first left an empty style behind, and the retry "already exists".
        The same POST takes a .zip (an SLD and its images, stored beside it)
        as application/zip; the library's create_style_from_file() sends it
        in two requests. An SLD goes as UTF-8, which is how GeoServer reads
        it whatever its declaration says (`utf8_sld`), and as such a zip when
        it draws icons from files on this machine (`icon_package`), when
        `local_icons` says the SLD was made here (see `_put_sld_body`): a
        copy's body comes from the server.
        """
        if style_format == "sld":
            data = utf8_sld(body)
            text = data.decode("utf-8")
            content_type = sld_content_type(text)
            package = icon_package(text, name) if local_icons else None
            if package is not None:
                data, content_type = package[0], "application/zip"
        elif style_format == "zip":
            data, content_type = body, "application/zip"
        else:
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            content_type = _CONTENT_TYPES[style_format]
        collection = self._style_path(name, workspace_name, "json")
        collection = collection.rsplit("/", 1)[0] + ".json"
        self._raw_rest(
            "post",
            collection,
            params={"name": name},
            data=data,
            headers={"Content-Type": content_type},
        )

    # -- QGIS <-> GeoServer ----------------------------------------------------

    def _sld_for_qgis(self, row_data):
        """One style's SLD, or None once the reason has been reported.

        QGIS reads SLD only; any other format comes as GeoServer converts it.
        """
        name, workspace_name = row_data[0], scope(row_data[1])
        return self._fetch(
            lambda: self._sld_of(name, workspace_name),
            translate("StyleTabMixin", "Failed to load the SLD of '{}'").format(name),
        )

    def _apply_style_to_qgis(self, row_data):
        """Load a server style into one of the project's vector layers.

        QGIS reads no SLD into a raster layer ("Layer type 1 not supported",
        on 3.40 as on 3.44), so none is offered.
        """
        name = row_data[0]
        layers = styleable_project_layers(rasters=False)
        if not layers:
            self.show_warning_message(
                translate(
                    "StyleTabMixin",
                    "This QGIS project has no vector layer to apply a style to. "
                    "QGIS cannot load an SLD into a raster layer.",
                )
            )
            return
        sld = self._sld_for_qgis(row_data)
        if sld is None:
            return

        dlg = ResourceFormDialog(
            title=translate("StyleTabMixin", "Apply '{}' to a QGIS layer").format(name),
            description=translate(
                "StyleTabMixin",
                "The style applies to the layer in this project only. The server is "
                "not changed.",
            ),
            fields=[
                {
                    "key": "qgis_layer",
                    "label": translate("StyleTabMixin", "QGIS layer"),
                    "type": "layer",
                    "required": True,
                }
            ],
            parent=self,
            ok_label=translate("StyleTabMixin", "Apply"),
        )
        dlg.get_widget("qgis_layer").setFilters(Qgis.LayerFilter.VectorLayer)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        layer = dlg.get_values()["qgis_layer"]
        outcome = []
        if not self._run_action(
            lambda: outcome.extend(apply_sld_to_layer(layer, sld)),
            translate("StyleTabMixin", "Failed to apply '{}' to '{}'").format(
                name, layer.name()
            ),
        ):
            return
        ok, message = outcome[0], outcome[1]
        missing = unresolved_icons(sld)
        if ok and missing:
            self.show_warning_message(
                translate(
                    "StyleTabMixin",
                    "'{}' now uses the style '{}', but QGIS cannot open these "
                    "icons of it: {}",
                ).format(layer.name(), name, ", ".join(missing))
            )
        elif ok:
            self.show_success_message(
                translate("StyleTabMixin", "'{}' now uses the style '{}'.").format(
                    layer.name(), name
                )
            )
        else:
            # QGIS reads less SLD than it writes; say what it could not take.
            self.show_warning_message(
                translate("StyleTabMixin", "QGIS could not apply '{}': {}").format(
                    name, message or translate("StyleTabMixin", "no detail given")
                )
            )

    def _save_style_to_disk(self, row_data):
        """Write a style's body to a file the user picks."""
        name, workspace_name = row_data[0], scope(row_data[1])
        fetched = self._fetch(
            lambda: self._style_as_stored(name, workspace_name),
            translate("StyleTabMixin", "Failed to load style '{}'").format(name),
        )
        if fetched is None:
            return
        definition, style_format, body = fetched

        suggested = definition.get("filename") or f"{name}.{style_format}"
        path, _selected = QFileDialog.getSaveFileName(
            self,
            translate("StyleTabMixin", "Save style '{}'").format(name),
            suggested,
            translate("StyleTabMixin", "{} (*.{});;All files (*)").format(
                style_format.upper(), style_format
            ),
        )
        if not path:
            return
        if self._run_action(
            lambda: Path(path).write_bytes(body),
            translate("StyleTabMixin", "Failed to save '{}'").format(name),
        ):
            self.show_success_message(
                translate("StyleTabMixin", "Style '{}' saved as {} ({}).").format(
                    name,
                    Path(path).name,
                    sld_version(body.decode("utf-8", errors="replace")),
                )
                if style_format == "sld"
                else translate("StyleTabMixin", "Style '{}' saved as {}.").format(
                    name, Path(path).name
                )
            )

    # -- Copy and usage --------------------------------------------------------

    def _copy_style(self, row_data):
        """Copy a style under a new name, in its own or another workspace."""
        name, workspace_name = row_data[0], scope(row_data[1])
        fetched = self._fetch(
            lambda: (
                self._get_workspace_names(),
                self._style_as_stored(name, workspace_name),
            ),
            translate("StyleTabMixin", "Failed to load style '{}'").format(name),
        )
        if fetched is None:
            return
        workspace_names, stored = fetched
        _definition, style_format, body = stored
        # GeoServer finds an icon or a fill image in the style's own folder.
        files = relative_hrefs(style_text(body)) if style_format == "sld" else []

        def check(values):
            self._require_safe_name(values["name"])
            self._refuse_taken_style(values["name"], scope(values["workspace"]))

        dlg = ResourceFormDialog(
            title=translate("StyleTabMixin", "Copy Style '{}'").format(name),
            description=translate(
                "StyleTabMixin",
                "A new style with the same definition. Layers keep the original.",
            ),
            fields=[
                {
                    "key": "name",
                    "label": translate("StyleTabMixin", "New name"),
                    "type": "text",
                    "required": True,
                    "default": f"{name}_copy",
                },
                {
                    "key": "workspace",
                    "label": translate("StyleTabMixin", "Workspace"),
                    "type": "combo",
                    "options": [(global_label(), GLOBAL)] + list(workspace_names),
                    "default": row_data[1],
                },
                {
                    "key": "files",
                    "label": translate("StyleTabMixin", "Files it uses"),
                    "type": "text",
                    "read_only": True,
                    "default": ", ".join(files),
                    "visible": False,
                    "help": translate(
                        "StyleTabMixin",
                        "They sit beside the style on the server and are not "
                        "copied: in another workspace, the copy draws without "
                        "them.",
                    ),
                },
            ],
            parent=self,
            ok_label=translate("StyleTabMixin", "Copy"),
            validate=self._form_check(check),
        )
        dlg.on_value_changed(
            "workspace",
            lambda value: dlg.set_field_visible(
                "files", bool(files) and scope(value) != workspace_name
            ),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        values = dlg.get_values()
        target, target_ws = values["name"].strip(), scope(values["workspace"])
        if self._run_action(
            lambda: self._wait_for_save(
                lambda: self._copy_style_to(
                    name, workspace_name, target, target_ws, stored
                )
            ),
            translate("StyleTabMixin", "Failed to copy style '{}'").format(name),
        ):
            self.show_success_message(
                translate("StyleTabMixin", "Style '{}' copied to '{}'.").format(
                    name, target
                )
            )
            self._load_styles()

    def _copy_style_to(self, name, workspace_name, target, target_ws, stored=None):
        """Create `target` from the body of `name`, in the same format.

        :param stored: what `_style_as_stored` answered, when the form read it.
        """
        self._require_safe_name(target)
        self._refuse_taken_style(target, target_ws)
        _definition, style_format, body = stored or self._style_as_stored(
            name, workspace_name
        )
        if style_format not in _CONTENT_TYPES:
            raise ValueError(
                translate("StyleTabMixin", "A {} style cannot be copied here.").format(
                    style_format.upper()
                )
            )
        self._create_style(target, target_ws, style_format, body)

    def _show_style_users(self, row_data):
        """List the layers and layer groups that use a style."""
        name, workspace_name = row_data[0], scope(row_data[1])
        # The waiting box's Cancel starts no more GETs, as in _complete_rows.
        stop = threading.Event()
        halt = SimpleNamespace(isCanceled=stop.is_set, setProgress=lambda _value: None)
        users = self._fetch(
            lambda: self._style_users(name, workspace_name, halt),
            translate("StyleTabMixin", "Failed to find what uses '{}'").format(name),
            stop=stop,
        )
        if users is None:
            return
        dlg = ResourceFormDialog(
            title=translate("StyleTabMixin", "What Uses '{}'").format(name),
            description=(
                translate(
                    "StyleTabMixin",
                    "Editing the style changes all of these. Deleting it moves "
                    "the layers to GeoServer's default style.",
                )
                if users
                else translate("StyleTabMixin", "No layer or group uses this style.")
            ),
            fields=[
                {
                    "key": "users",
                    "label": translate("StyleTabMixin", "Used by"),
                    "type": "textarea",
                    "read_only": True,
                }
            ],
            values={"users": "\n".join(users)},
            parent=self,
        )
        dlg.hide_save_button()
        dlg.exec()

    def _style_users(self, name, workspace_name, task=None):
        """Every layer and group using the style, with how. Runs in a worker.

        GeoServer has no endpoint for this, so every layer and group is read:
        one GET each, fanned out; `task` (or a stand-in) stops the fan-outs
        when cancelled. A layer names a workspace style "ws:name".
        TODO(#1): no get_layers() nor a layer's styles in the library (row 58).
        """
        reference = f"{workspace_name}:{name}" if workspace_name else name
        users = []
        layers = self._all_layer_names()
        for layer, (payload, error) in zip(
            layers,
            self._fan_out(
                lambda qualified: self._raw_rest("get", self._layers_url(qualified))
                .json()
                .get("layer", {}),
                layers,
                task,
            ),
        ):
            if error:
                # Said, not skipped: a short list would read as "safe to edit".
                users.append(
                    translate("StyleTabMixin", "{} (could not be read: {})").format(
                        layer, self._error_text(error)
                    )
                )
                continue
            payload = payload or {}
            if (payload.get("defaultStyle") or {}).get("name") == reference:
                users.append(
                    translate("StyleTabMixin", "{} (default style)").format(layer)
                )
            elif reference in (
                style.get("name")
                for style in unwrap(payload, "styles", "style")
                if isinstance(style, dict)
            ):
                users.append(
                    translate("StyleTabMixin", "{} (other style)").format(layer)
                )
        if task is not None and task.isCanceled():
            return users  # the group listing's GETs ran on after a Cancel
        groups = self._all_group_names()
        for group, (detail, error) in zip(
            groups,
            self._fan_out(
                lambda qualified: self._group_detail(
                    qualified.rpartition(":")[2], qualified.rpartition(":")[0] or None
                ),
                groups,
                task,
            ),
        ):
            if error:
                # Listed like an unreadable layer: one bad group failed it all.
                users.append(
                    translate("StyleTabMixin", "{} (could not be read: {})").format(
                        group, self._error_text(error)
                    )
                )
                continue
            root = (detail.get("rootLayerStyle") or {}).get("name")
            if reference in self._group_styles(detail) or root == reference:
                users.append(
                    translate("StyleTabMixin", "{} (layer group)").format(group)
                )
        return users

    # -- Delete ----------------------------------------------------------------

    def _delete_style(self, row_data):
        """Delete a single style after confirmation."""
        self._delete_selected_styles([row_data])

    def _delete_selected_styles(self, selected_rows):
        """Delete one or more styles after confirmation."""
        self._delete_many(
            [
                (
                    # "ws:name" as layers and groups are named; a global one bare
                    f"{scope(row[1])}:{row[0]}" if scope(row[1]) else row[0],
                    lambda name=row[0], ws=scope(row[1]): self._do_delete_style(
                        name, ws
                    ),
                )
                for row in selected_rows
            ],
            self._load_styles,
            ask=self._one_or_many(
                translate(
                    "StyleTabMixin", "Are you sure you want to delete style '{}'?"
                ),
                lambda n: translate(
                    "StyleTabMixin",
                    "Are you sure you want to delete %n style(s)?",
                    None,
                    n,
                ),
            ),
            done=self._one_or_many(
                translate("StyleTabMixin", "Style '{}' deleted."),
                lambda n: translate("StyleTabMixin", "%n style(s) deleted.", None, n),
            ),
            cascade=translate(
                "StyleTabMixin",
                "The style goes away, and its file stays in the data directory "
                "only as a .bak backup. Layers that used it fall back to "
                "GeoServer's default style.",
            ),
        )

    def _do_delete_style(self, name, workspace_name):
        """DELETE a style and its references (recurse); purge renames its file
        to a .bak (measured on 2.28.5), it does not remove it.

        TODO(#1): upstream as delete_style(name, ws, purge=True, recurse=True):
        the library has no delete for styles. Workaround: DELETE the style path
        with purge=true&recurse=true.
        """
        self._raw_rest(
            "delete",
            self._style_path(name, workspace_name, "json"),
            params={"purge": "true", "recurse": "true"},
        )
