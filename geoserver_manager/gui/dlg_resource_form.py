#! python3  # noqa: E265

"""
Reusable modal form dialog for creating and editing GeoServer resources.

Usage:
    fields = [
        {"key": "name", "label": "Name", "type": "text", "required": True},
        {"key": "uri", "label": "Namespace URI", "type": "text",
         "help": "The namespace URI associated with this workspace"},
        {"key": "isolated", "label": "Isolated Workspace", "type": "checkbox"},
    ]
    dlg = ResourceFormDialog(
        title="Add a Workspace",
        description="Configure a new workspace",
        fields=fields,
        parent=self,
    )
    if dlg.exec() == QDialog.DialogCode.Accepted:
        values = dlg.get_values()

For edit mode, pass existing values:
    dlg = ResourceFormDialog(
        title="Workspace 'my_ws'",
        fields=fields,
        values={"name": "my_ws", "isolated": False},
        parent=self,
    )

Supported field types:
    - "text"      -> QLineEdit
    - "checkbox"  -> QCheckBox
    - "combo"     -> QComboBox (provide "options": ["a", "b", ...]). An option
                     can be (label, value): the label is shown, and can be
                     translated; the value is what get_values() returns and
                     on_value_changed() passes, the one the code compares
    - "spinbox"   -> QSpinBox (optional "min", "max", "default")
    - "textarea"  -> QPlainTextEdit
    - "file"      -> QgsFileWidget (optional "filter", e.g. "Styles (*.sld)")
    - "list"      -> QgsListWidget: a list of strings, typed (keywords)
    - "keyvalue"  -> QgsKeyValueWidget: a {key: value} dict (parameters)
    - "table"     -> ListTable: rows picked from "choices", with typed
                     "columns", in order when "ordered" (a group's layers),
                     each name once when "unique" (gridsets, formats)
    - "layer"     -> QgsMapLayerComboBox: a layer of the QGIS project, the
                     value being the layer itself. Vector and raster layers,
                     or "raster_files": rasters GDAL reads from a file;
                     "show_crs" adds each layer's CRS; "allow_empty" starts
                     with none picked when the value is None
    - "extent"    -> QgsExtentWidget: an area, typed or taken from "canvas"
                     (the map view), a layer or a bookmark, in the CRS set by
                     set_extent_crs(); "minx, miny, maxx, maxy", or "" unset
    - "image"     -> QLabel showing a picture set later with
                     set_image(key, pixmap, text); "placeholder" is shown until
                     then and "max_height" caps the picture (default 240). It is
                     never a value: get_values() skips it.

Field options:
    - key (str): identifier used in get_values()
    - label (str): display label
    - type (str): widget type (see above)
    - required (bool): mark as mandatory (default False)
    - url (bool): when filled, must start with http:// or https://; OK keeps
                  the dialog open and says so, like a missing required field
    - crs (bool): a "text" field holding an SRS code, with a button that
                  looks one up in QGIS's CRS picker
    - echo_password (bool): a "text" field for a password: QGIS's password
                  box, hidden, with its eye to show what was typed
    - default: default value (an "extent" always starts not set)
    - help (str): hint text shown below the widget
    - placeholder (str): placeholder text for text/textarea
    - options (list): choices for "combo": strings, or (label, value)
    - min/max (int): range for "spinbox"
    - read_only (bool): disable editing
    - group (str): optional tab group name; fields with the same group
      appear under one tab; ungrouped fields go to the first tab
    - on_change (callable): called with the new value when a "combo" (its
      value, not its label) or a "layer" (the layer) changes
    - visible (bool): initial visibility (default True)
    - max_height / min_height (int): a "textarea", "list", "keyvalue" or
      "table"'s height bounds. A textarea is at most 120 px by default, a
      list 160 and a key/value list 200; a table is uncapped. None uncaps:
      the widget then grows with the dialog, as a style's body does
    - code (bool or str): a "textarea" of markup, in QGIS's code editor:
      "xml", "css" or "json" highlight it, True is plain (a log)
    - wide (bool): span the whole form, without a label beside the widget
"""

from qgis.core import Qgis, QgsCoordinateReferenceSystem, QgsProviderRegistry
from qgis.gui import (
    QgsCodeEditor,
    QgsCodeEditorCSS,
    QgsCodeEditorHTML,
    QgsCodeEditorJson,
    QgsExtentWidget,
    QgsFileWidget,
    QgsKeyValueWidget,
    QgsListWidget,
    QgsMapLayerComboBox,
    QgsPasswordLineEdit,
    QgsProjectionSelectionDialog,
    QgsScrollArea,
)
from qgis.PyQt.QtCore import QCoreApplication, QMetaType, QSize, Qt
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QSizePolicy,
    QSpinBox,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from geoserver_manager.gui.icons import icon
from geoserver_manager.gui.list_table import ListTable, short_combo
from geoserver_manager.gui.theme import hint_colour, invalid_field_colour
from geoserver_manager.toolbelt.rest import Abandoned

# QGIS has no XML editor; its HTML one reads SLD and GeoWebCache's XML well,
# and text that is not markup simply stays plain.
_CODE_EDITORS = {
    "xml": QgsCodeEditorHTML,
    "css": QgsCodeEditorCSS,
    "json": QgsCodeEditorJson,
}


def _scaled(font, factor):
    """The font, `factor` times its size, never below a readable 7 pt."""
    if font.pointSizeF() > 0:
        font.setPointSizeF(max(font.pointSizeF() * factor, 7.0))
    else:  # a font set in pixels
        font.setPixelSize(max(round(font.pixelSize() * factor), 9))
    return font


class _FormPage(QgsScrollArea):
    """A form that scrolls when the dialog is too small, rather than squeeze.

    Laid out straight in a tab, a form ignored the height its wrapped help
    needs at a narrower width, and the rows were drawn over each other; a
    tall form also could not shrink to fit a short screen. Qt's own size hint
    for a scroll area stops at 24 lines, so the form's is used: a form that
    fits the screen opens without a scroll bar. QGIS's own scroll area: a
    plain one let the wheel change a combo or a spin box the cursor passed
    over while scrolling (a layer's projection policy, which Save then sent).
    """

    def __init__(self, form):
        super().__init__()
        self.setWidget(form)
        self.setWidgetResizable(True)
        self.setFrameShape(QgsScrollArea.Shape.NoFrame)
        # Not a Tab stop of its own: it took the focus when a form opened.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # The dialog is never narrower than the form: no sideways scrolling.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # The tab's own background: setWidget() makes the form paint its own.
        self.viewport().setAutoFillBackground(False)
        form.setAutoFillBackground(False)

    def sizeHint(self):  # noqa: N802 (Qt's own spelling)
        return self.widget().sizeHint()

    def minimumSizeHint(self):  # noqa: N802
        width = self.widget().minimumSizeHint().width()
        return QSize(
            width + self.verticalScrollBar().sizeHint().width(),
            super().minimumSizeHint().height(),
        )


class ResourceFormDialog(QDialog):
    """Generic modal form dialog built from a field definition list."""

    def __init__(
        self,
        title,
        fields,
        values=None,
        description=None,
        parent=None,
        ok_label=None,
        validate=None,
    ):
        """
        :param title: dialog window title.
        :param fields: list of field dicts (see module docstring).
        :param values: dict of existing values to pre-fill (edit mode).
        :param description: optional subtitle shown below the title.
        :param parent: parent widget.
        :param ok_label: what the primary button does: "Create", "Publish",
            "Upload", "Apply"… Defaults to "Save", which is right for an edit
            and wrong for everything else.
        :param validate: validate(values), run on Save once the fields are
            filled: a ValueError keeps the form open with its message, and
            what was typed. For the checks a save would refuse (a name that
            is taken): after the form closed, the input was lost.
        """
        super().__init__(parent)
        self.setWindowTitle(title)

        self._fields = fields
        self._validate = validate
        self._opened_with = None  # the values as shown, taken when first shown
        self._widgets = {}  # key -> widget
        self._row_widgets = {}  # key -> (label_widget, wrapper_widget) for visibility
        # Keys hidden on purpose. Tracked explicitly because Qt reports every
        # widget on a non-active tab as invisible, which would silently skip
        # validation of required fields the user simply hasn't scrolled to.
        self._hidden_keys = set()

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        # A floor, not an explicit minimum: that one would stop the layout
        # raising it to the form's own, and a scrolled page narrower than
        # its form cuts the fields off at the right.
        margins = layout.contentsMargins()
        layout.addStrut(450 - margins.left() - margins.right())

        # Header
        title_label = QLabel(title)
        # A name like "a<b>c" in "Style '{}'" made it rich text, and lost the tag.
        title_label.setTextFormat(Qt.TextFormat.PlainText)
        # Sizes from the dialog's own font: fixed pixels ignored QGIS's font
        # size setting, and the title came out smaller than the description.
        font = _scaled(title_label.font(), 1.15)
        font.setBold(True)
        title_label.setFont(font)
        layout.addWidget(title_label)

        if description:
            desc_label = QLabel(description)
            desc_label.setTextFormat(Qt.TextFormat.PlainText)
            desc_label.setWordWrap(True)
            desc_label.setStyleSheet(
                f"color: {hint_colour(self.palette())}; margin-bottom: 6px;"
            )
            layout.addWidget(desc_label)

        # Group fields by tab
        groups = self._collect_groups(fields)

        self._tabs = None
        self._field_page = {}  # key -> its page, to reveal validation errors

        if len(groups) == 1:
            # Single group: no tabs needed
            form = self._build_form(list(groups.values())[0], values)
            layout.addWidget(form)
        else:
            # Multiple groups: use tabs
            self._tabs = QTabWidget()
            for group_name, group_fields in groups.items():
                page = self._build_form(group_fields, values)
                self._tabs.addTab(page, group_name)
            layout.addWidget(self._tabs)
        # No stretch below: the page takes the height a user gives the
        # dialog, and a table or a list in it grows with it.

        # Buttons
        self._button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._button_box.button(QDialogButtonBox.StandardButton.Ok).setText(
            ok_label or self.tr("Save")
        )
        self._button_box.accepted.connect(self._on_accept)
        self._button_box.rejected.connect(self._on_cancel)
        # Why the form did not accept: a red border alone says nothing when
        # the field is an empty combo with nothing to pick.
        self._validation_label = QLabel()
        self._validation_label.setTextFormat(Qt.TextFormat.PlainText)
        self._validation_label.setWordWrap(True)
        self._validation_label.setStyleSheet(
            f"color: {invalid_field_colour(self.palette())};"
        )
        self._validation_label.hide()
        layout.addWidget(self._validation_label)
        layout.addWidget(self._button_box)

    @staticmethod
    def _collect_groups(fields):
        """Organise fields into ordered groups (preserves insertion order)."""
        groups = {}
        # A tab name the user reads, so it is translated like the rest.
        default_group = QCoreApplication.translate("ResourceFormDialog", "General")
        for field in fields:
            group = field.get("group", default_group)
            groups.setdefault(group, []).append(field)
        return groups

    def _build_form(self, fields, values):
        """A scrolling page holding a QFormLayout for the given fields."""
        container = QWidget()
        form = QFormLayout(container)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(12)
        # The rows' own margins space them, not the layout: Qt 5 keeps the
        # spacing of a hidden row, and a datastore form hides a dozen (the
        # other types' parameters), which opened its tab on a blank band.
        form.setVerticalSpacing(0)

        for field in fields:
            widget = self._create_widget(field, values)
            self._widgets[field["key"]] = widget

            # Label
            label_text = field["label"]
            # Not on a locked field: an asterisk asks for input it cannot take.
            if field.get("required") and not field.get("read_only"):
                label_text += " *"
            label = QLabel(label_text)
            label.setContentsMargins(0, 4, 0, 4)

            # Build a wrapper that stacks the widget + optional help text
            wrapper_widget = QWidget()
            wrapper = QVBoxLayout(wrapper_widget)
            wrapper.setSpacing(2)
            wrapper.setContentsMargins(0, 4, 0, 4)
            wrapper.addWidget(widget)

            help_text = field.get("help")
            if help_text:
                help_label = QLabel(help_text)
                help_label.setTextFormat(Qt.TextFormat.PlainText)
                help_label.setWordWrap(True)
                help_label.setStyleSheet(f"color: {hint_colour(self.palette())};")
                help_label.setFont(_scaled(help_label.font(), 0.85))
                help_label.setSizePolicy(
                    QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
                )
                wrapper.addWidget(help_label)

            if field.get("wide"):
                # The whole width, no label beside it: a document to edit
                # whose tab already names it. The label still names it in a
                # validation message.
                label.setParent(container)
                label.hide()
                form.addRow(wrapper_widget)
            else:
                form.addRow(label, wrapper_widget)
            self._row_widgets[field["key"]] = (label, wrapper_widget)

            # Initial visibility
            if field.get("visible") is False:
                self.set_field_visible(field["key"], False)

            # on_change callback for combo widgets
            if field.get("type") == "combo" and field.get("on_change"):
                self.on_value_changed(field["key"], field["on_change"])
            elif field.get("type") == "layer" and field.get("on_change"):
                widget.layerChanged.connect(field["on_change"])

        page = _FormPage(container)
        for field in fields:
            self._field_page[field["key"]] = page
        return page

    def _create_widget(self, field, values):
        """Instantiate the appropriate widget for a field definition."""
        ftype = field.get("type", "text")
        key = field["key"]
        default = field.get("default", "" if ftype == "text" else None)
        value = values.get(key, default) if values else default
        read_only = field.get("read_only", False)

        if ftype == "text":
            # QGIS's own password box: hidden, with a toggle to check what
            # was typed. It is a QLineEdit, so the rest reads it the same.
            w = QgsPasswordLineEdit() if field.get("echo_password") else QLineEdit()
            if value:
                w.setText(str(value))
                # From its start: a long title or URL opened on its tail.
                w.setCursorPosition(0)
            placeholder = field.get("placeholder")
            if placeholder:
                w.setPlaceholderText(placeholder)
            if read_only:
                # Not setEnabled(False): a greyed field cannot be selected or
                # copied, and detail views are made of these (bounds, URLs).
                w.setReadOnly(True)
                self._looks_read_only(w)
            elif field.get("crs"):
                self._add_crs_picker(w)
            return w

        if ftype == "checkbox":
            w = QCheckBox()
            w.setChecked(bool(value))
            if read_only:
                w.setEnabled(False)
            return w

        if ftype == "combo":
            w = QComboBox()
            short_combo(w)
            for option in field.get("options", []):
                label, data = option if isinstance(option, tuple) else (option, None)
                w.addItem(label, data)
            if value:
                self._select(w, value)
            if read_only:
                w.setEnabled(False)
            return w

        if ftype == "spinbox":
            w = QSpinBox()
            number = None if value is None else int(value)
            low, high = field.get("min", 0), field.get("max", 99999)
            if number is not None:
                # Never narrower than the stored value: clamped, it was
                # written back by an untouched Save (200 cascaded connections
                # became 128, a 32x32 meta-tile 20x20).
                low, high = min(low, number), max(high, number)
            w.setRange(low, high)
            if number is not None:
                w.setValue(number)
            if read_only:
                w.setReadOnly(True)
            return w

        if ftype == "textarea" and field.get("code"):
            # QGIS's code editor: highlighting, line numbers, no wrapping in
            # the middle of an attribute, and the user's code font and
            # colours. It has no placeholder: a field's help says it.
            w = _CODE_EDITORS.get(field["code"], QgsCodeEditor)()
            w.setLineNumbersVisible(True)
            if value:
                w.setText(str(value))
            w.setReadOnly(read_only)
            self._grows(w, field, min_height=None, max_height=120)
            return w

        if ftype == "textarea":
            w = QPlainTextEdit()
            self._grows(w, field, min_height=None, max_height=120)
            if value:
                w.setPlainText(str(value))
            placeholder = field.get("placeholder")
            if placeholder:
                w.setPlaceholderText(placeholder)
            if read_only:
                w.setReadOnly(True)
                self._looks_read_only(w)
            return w

        if ftype == "file":
            # QGIS's own: a Browse button, and a file dropped on it is taken.
            w = QgsFileWidget()
            w.setDialogTitle(field["label"])
            w.setFilter(field.get("filter", ""))
            if value:
                w.setFilePath(str(value))
            if field.get("placeholder"):
                w.lineEdit().setPlaceholderText(field["placeholder"])
            w.setReadOnly(read_only)
            return w

        if ftype == "list":
            w = QgsListWidget(QMetaType.Type.QString)
            w.setList([str(item) for item in (value or [])])
            view = w.findChild(QTableView)
            if view is not None:  # a lone "Value" header over a list of words
                view.horizontalHeader().hide()
            w.setReadOnly(read_only)
            self._grows(w, field, max_height=160)
            # QGIS's own .ui sets 300 px, wider than the form's field column:
            # its add and remove buttons were pushed out of sight.
            w.setMinimumWidth(0)
            return w

        if ftype == "keyvalue":
            w = QgsKeyValueWidget()
            # An empty parameter comes back from the library as None: blank,
            # not the text "None".
            w.setMap(
                {str(k): "" if v is None else str(v) for k, v in (value or {}).items()}
            )
            self._fit_keys(w)
            w.setReadOnly(read_only)
            self._grows(w, field, max_height=200)
            w.setMinimumWidth(0)  # as for "list": its buttons stay in view
            return w

        if ftype == "table":
            w = ListTable(
                field["columns"],
                field.get("choices", ()),
                ordered=field.get("ordered", False),
                read_only=read_only,
                unique=field.get("unique", False),
            )
            w.set_rows(value or [])
            # Uncapped by default: a table is its tab's content and fills it;
            # a cap left the spare height between the table and its help.
            self._grows(w, field, min_height=160)
            return w

        if ftype == "layer":
            # QGIS's own picker: the layers' icons, the project as it is
            # now, and two layers of one name are still two entries.
            w = QgsMapLayerComboBox()
            short_combo(w)
            if field.get("raster_files"):
                w.setFilters(Qgis.LayerFilter.RasterLayer)
                # A WMS or XYZ layer has no file to send.
                w.setExcludedProviders(
                    [
                        key
                        for key in QgsProviderRegistry.instance().providerList()
                        if key != "gdal"
                    ]
                )
            else:
                # QGIS reads and writes SLD for these two kinds only.
                # wrapped, or PyQt5 hands an int to the deprecated overload
                w.setFilters(
                    Qgis.LayerFilters(
                        Qgis.LayerFilter.VectorLayer | Qgis.LayerFilter.RasterLayer
                    )
                )
            w.setShowCrs(field.get("show_crs", False))
            if field.get("allow_empty"):
                w.setAllowEmptyLayer(True)
                w.setLayer(value)  # None: nothing picked, Save asks for one
            elif value is not None:
                w.setLayer(value)
            w.setEnabled(not read_only)
            return w

        if ftype == "extent":
            w = QgsExtentWidget(None, QgsExtentWidget.WidgetStyle.CondensedStyle)
            w.setNullValueAllowed(True, self.tr("Not set"))
            w.clear()
            if field.get("canvas") is not None:
                # Not drawn on it: the form is modal, and hiding it to draw
                # would end it.
                w.setMapCanvas(field["canvas"], False)
            w.setEnabled(not read_only)
            return w

        if ftype == "image":
            w = QLabel(field.get("placeholder", ""))
            w.setWordWrap(True)
            w.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            w.setMaximumHeight(field.get("max_height", 240))
            # The text states (placeholder, explanation) read as hints; a
            # pixmap ignores the colour.
            w.setStyleSheet(f"color: {hint_colour(self.palette())};")
            return w

        # A typo in a field spec must not become a silent text box.
        raise ValueError(f"Unknown field type {ftype!r} for {key!r}")

    def _add_crs_picker(self, edit):
        """A button in the box that fills it from QGIS's CRS picker.

        The code stays typed text: GeoServer declares codes QGIS does not
        know (EPSG:900913), which a picker-only field would lose.
        """
        action = edit.addAction(
            icon("pick-crs"), QLineEdit.ActionPosition.TrailingPosition
        )
        action.setToolTip(self.tr("Pick a CRS"))

        def pick():
            dialog = QgsProjectionSelectionDialog(self)
            code = edit.text().strip()
            current = QgsCoordinateReferenceSystem(
                code if ":" in code else f"EPSG:{code}"
            )
            if current.isValid():
                dialog.setCrs(current)
            if dialog.exec():
                edit.setText(dialog.crs().authid())

        action.triggered.connect(pick)

    @staticmethod
    def _fit_keys(widget):
        """Widen a key/value table's key column to its longest key.

        It stayed at a fixed width while the value took the rest: "Batch
        inse…" even in a wide dialog.
        """
        view = widget.findChild(QTableView)
        if view is not None:
            view.resizeColumnToContents(0)

    @staticmethod
    def _grows(widget, field, min_height=100, max_height=None):
        """Height bounds of a list or a text box; "max_height": None uncaps.

        Uncapped, it takes the height a user gives the dialog: a form row
        only grows when its widget expands. Capped, it does not ask for
        more, or its row would take the height anyway and drift its help
        away from it. The minimum keeps a few rows in view; a smaller
        dialog scrolls instead of squeezing the list down to its header.
        """
        min_height = field.get("min_height", min_height)
        if min_height:
            widget.setMinimumHeight(min_height)
        max_height = field.get("max_height", max_height)
        policy = widget.sizePolicy()
        if max_height:
            widget.setMaximumHeight(max_height)
            # Not Preferred: a widget with a layout still expands when a
            # child does, as ListTable's table does.
            policy.setVerticalPolicy(QSizePolicy.Policy.Maximum)
        else:
            policy.setVerticalPolicy(QSizePolicy.Policy.Expanding)
        widget.setSizePolicy(policy)

    @staticmethod
    def _looks_read_only(widget):
        """Draw a read-only box as text: no frame, the window's background.

        An input box invites typing; in a detail view nothing can be typed,
        and every field looked editable anyway. Selecting and copying still
        work, which is why these are read-only rather than disabled.
        """
        if isinstance(widget, QLineEdit):
            widget.setFrame(False)
        else:
            widget.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        widget.setStyleSheet("background: transparent;")

    def get_values(self):
        """Return a dict of field key -> current value."""
        result = {}
        for field in self._fields:
            key = field["key"]
            widget = self._widgets[key]
            ftype = field.get("type", "text")

            if ftype == "image":
                continue  # a picture, not a value
            if ftype == "text":
                # A password goes as typed: a space at its edge is part of it.
                text = widget.text()
                result[key] = text if field.get("echo_password") else text.strip()
            elif ftype == "checkbox":
                result[key] = widget.isChecked()
            elif ftype == "combo":
                result[key] = self._combo_value(widget)
            elif ftype == "spinbox":
                result[key] = widget.value()
            elif ftype == "textarea" and isinstance(widget, QgsCodeEditor):
                result[key] = widget.text().strip()
            elif ftype == "textarea":
                result[key] = widget.toPlainText().strip()
            elif ftype == "file":
                result[key] = widget.filePath().strip()
            elif ftype == "list":
                # A row added and left empty holds a NULL, whose str() is
                # "NULL": it was saved as a keyword.
                result[key] = [
                    item.strip()
                    for item in widget.list()
                    if isinstance(item, str) and item.strip()
                ]
            elif ftype == "keyvalue":
                # A value never typed is None, which went out as "None".
                result[key] = {
                    k.strip(): v if isinstance(v, str) else ""
                    for k, v in widget.map().items()
                    if isinstance(k, str) and k.strip()
                }
            elif ftype == "table":
                result[key] = widget.rows()
            elif ftype == "layer":
                result[key] = widget.currentLayer()
            elif ftype == "extent":
                box = widget.outputExtent()
                corners = (
                    box.xMinimum(),
                    box.yMinimum(),
                    box.xMaximum(),
                    box.yMaximum(),
                )
                result[key] = (
                    ", ".join(repr(value) for value in corners)
                    if widget.isValid()
                    else ""
                )
        return result

    def set_field_visible(self, key, visible):
        """Show or hide a field by key.

        :param key: field key identifier.
        :param visible: True to show, False to hide.
        """
        if key in self._row_widgets:
            label, wrapper = self._row_widgets[key]
            label.setVisible(visible)
            wrapper.setVisible(visible)
            # The dialog keeps the page's old size hints: a store type whose
            # fields are wider (WFS, Other...) was cut off at the right. No
            # page yet while the form is being built.
            if key in self._field_page:
                self._field_page[key].updateGeometry()
        if visible:
            self._hidden_keys.discard(key)
        else:
            self._hidden_keys.add(key)

    def set_extent_crs(self, key, authid):
        """The CRS an "extent" field's area is given in; a picked one follows."""
        widget = self._widgets[key]
        was_set = widget.isValid()
        widget.setOutputCrs(QgsCoordinateReferenceSystem(authid or ""))
        if not was_set:
            # QGIS then shows the empty box transformed, a 300-digit number.
            widget.clear()

    def set_image(self, key, pixmap, text=""):
        """Show a picture in an "image" field, or, without one, the text that
        says why there is none. Painting a label, it is safe after close."""
        label = self._widgets[key]
        if pixmap is None or pixmap.isNull():
            label.setPixmap(QPixmap())
            label.setText(text)
            return
        if pixmap.height() > label.maximumHeight():
            pixmap = pixmap.scaledToHeight(
                label.maximumHeight(), Qt.TransformationMode.SmoothTransformation
            )
        label.setPixmap(pixmap)
        # The picture usually lands after the form is shown. Without a minimum
        # the label keeps the one-line height of its placeholder text, and the
        # form does not grow to show the rest.
        label.setMinimumHeight(pixmap.height())
        label.setToolTip(text)

    def set_values(self, values):
        """Fill fields after the form opened: text, code, a list, a table, a
        key/value table, or a combo by its value (a viewer's picked entry)."""
        for key, value in values.items():
            widget = self._widgets[key]
            if isinstance(widget, QPlainTextEdit):
                widget.setPlainText(value)
            elif isinstance(widget, QgsCodeEditor):
                widget.setText(value)
            elif isinstance(widget, ListTable):
                widget.set_rows(value)
            elif isinstance(widget, QgsListWidget):
                widget.setList(list(value))
            elif isinstance(widget, QgsKeyValueWidget):
                widget.setMap(dict(value))
                self._fit_keys(widget)
            elif isinstance(widget, QComboBox):
                self._select(widget, value)
            else:
                widget.setText(value)
                widget.setCursorPosition(0)

    def on_value_changed(self, key, callback):
        """Call callback(value) when a combo changes: its value, not its label."""
        widget = self._widgets[key]
        widget.currentIndexChanged.connect(
            lambda _index: callback(self._combo_value(widget))
        )

    @staticmethod
    def _combo_value(combo):
        """A combo's value: the option's own when it has one, else its text."""
        data = combo.currentData()
        return combo.currentText() if data is None else data

    @staticmethod
    def _select(combo, value):
        """Show the option of this value, or of this text."""
        index = combo.findData(value)
        if index < 0:
            index = combo.findText(str(value))
        if index >= 0:
            combo.setCurrentIndex(index)

    def get_widget(self, key):
        """Return the widget for a field by key.

        :param key: field key identifier.
        :return: the widget, or None if key not found.
        """
        return self._widgets.get(key)

    def showEvent(self, event):  # noqa: N802 (Qt's own spelling)
        """Open tall enough for the wrapped description and help text.

        A top-level window ignores height-for-width: its size comes from the
        size hint, which assumes one line per label. On a high-DPI screen the
        rows were then squeezed and the help text cut off. Up to the screen:
        a taller form scrolls.
        """
        super().showEvent(event)
        if self._opened_with is None:
            # After the tab wired and filled the form, before any typing.
            self._opened_with = self.get_values()
        needed = self.needed_height()
        screen = self.screen().availableGeometry().height() if self.screen() else needed
        if self.height() < needed:
            self.resize(self.width(), min(needed, screen))
        self._focus_first_field()

    def _focus_first_field(self):
        """Put the cursor in the first field there is to type in.

        It sat on the tab bar or on nothing: typing did nothing.
        """
        page = self._tabs.currentWidget() if self._tabs else None
        for field in self._fields:
            key = field["key"]
            if (
                (page is not None and self._field_page.get(key) is not page)
                or key in self._hidden_keys
                or field.get("read_only")
                or field.get("type") == "image"
            ):
                continue
            widget = self._widgets[key]
            target = widget.picker if isinstance(widget, ListTable) else widget
            if target.isEnabled():
                target.setFocus()
                return

    def needed_height(self):
        """The height that shows the whole form, at the current width."""
        layout = self.layout()
        # Without a wrapped description, totalHeightForWidth() is -1.
        if layout.hasHeightForWidth():
            needed = layout.totalHeightForWidth(self.width())
        else:
            needed = layout.totalSizeHint().height()
        # A page's hint is its form at the form's own width. At the page's,
        # wrapped help can need more, and the form would open scrolled.
        # (Not height-for-width on the page: the dialog's layout would take
        # that as its minimum, and a short screen could not scroll it.)
        pages = set(self._field_page.values())
        if pages:
            current = self._tabs.currentWidget() if self._tabs else next(iter(pages))
            width = current.viewport().width()
            forms = [page.widget() for page in pages]
            hinted = max(form.sizeHint().height() for form in forms)
            wraps = max(max(form.heightForWidth(width), 0) for form in forms)
            needed += max(wraps - hinted, 0)
        return needed

    def exec(self):
        """Run the form modally, then free it once its caller is done.

        Not WA_DeleteOnClose: QDialog.exec() deletes such a dialog before it
        returns, and the get_values() every caller runs next read a dead
        widget. deleteLater() runs once control is back in the event loop,
        after the caller's waits and questions. Without either, every form
        opened stayed alive for the whole QGIS session, its layer combos
        still following the project. The late landings (a legend, a task
        list) check sip.isdeleted first.
        """
        try:
            return super().exec()
        finally:
            self.deleteLater()

    def keyPressEvent(self, event):  # noqa: N802 (Qt's own spelling)
        if event.key() == Qt.Key.Key_Escape:
            self._on_cancel()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):  # noqa: N802 (Qt's own spelling)
        """The title bar's close button asks like Esc; close() stays quiet."""
        if event.spontaneous() and not self._may_discard():
            event.ignore()
            return
        super().closeEvent(event)

    def _on_cancel(self):
        """Esc or Cancel: ask before an edit is thrown away.

        Esc closed a style's editor with its changes gone, without a word.
        A form with nothing to save (a viewer) or nothing changed just closes.
        Only the user's own Esc, Cancel and window close button ask: close()
        from the code goes through reject().
        """
        if self._may_discard():
            self.reject()

    def _may_discard(self):
        """True when closing loses nothing, or the user said Discard."""
        saves = self._button_box.button(QDialogButtonBox.StandardButton.Ok).isVisible()
        return (
            not saves
            or self._opened_with is None
            or self.get_values() == self._opened_with
            or QMessageBox.question(
                self,
                self.windowTitle(),
                self.tr("Discard your changes?"),
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            == QMessageBox.StandardButton.Discard
        )

    def add_button(self, text):
        """A button beside Save and Cancel for an action of the form's own
        (Show the log, Stop all); the caller connects its clicked signal."""
        save = self._button_box.button(QDialogButtonBox.StandardButton.Ok)
        hidden = save.isHidden()
        button = self._button_box.addButton(
            text, QDialogButtonBox.ButtonRole.ActionRole
        )
        save.setHidden(hidden)  # addButton() shows every button again
        return button

    def hide_save_button(self):
        """Hide the Save button, leaving only Cancel (for view-only dialogs)."""
        self._button_box.button(QDialogButtonBox.StandardButton.Ok).setVisible(False)
        self._button_box.button(QDialogButtonBox.StandardButton.Cancel).setText(
            self.tr("Close")
        )

    def _on_accept(self):
        """Validate required fields before accepting."""
        self._validation_label.hide()
        # Reset styles
        for field in self._fields:
            widget = self._widgets[field["key"]]
            # Read-only boxes keep their text look, and the image keeps the
            # hint colour of its placeholder.
            if not field.get("read_only") and field.get("type") != "image":
                widget.setStyleSheet("")

        values = self.get_values()
        for field in self._fields:
            key = field["key"]
            # Not applicable to the current form, or an image: no value.
            if key in self._hidden_keys or key not in values:
                continue
            value = values[key]
            # A URL field keeps the dialog open on a bad value: checked after
            # it closed, the edits were thrown away with the error.
            bad_url = (
                field.get("url")
                and isinstance(value, str)
                and value.strip()
                and not value.strip().startswith(("http://", "https://"))
            )
            # A name picked but not added: Save used to drop it without a word.
            pending = field.get("type") == "table" and self._widgets[key].pending()
            if (field.get("required") and not value) or bad_url or pending:
                # Bring the offending field on screen: it may sit on another
                # tab, or below the fold of a scrolled page.
                widget = self._widgets[key]
                if self._tabs is not None:
                    self._tabs.setCurrentWidget(self._field_page[key])
                self._field_page[key].ensureWidgetVisible(widget)
                widget.setFocus()
                widget.setStyleSheet(
                    f"border: 1px solid {invalid_field_colour(self.palette())};"
                )
                if pending:
                    widget.picker.setFocus()
                    text = self.tr(
                        "'{}' is picked in '{}' but not added. Press Add, or clear it."
                    ).format(pending, field["label"])
                elif bad_url:
                    text = self.tr("'{}' must start with http:// or https://.")
                elif field.get("type") in ("combo", "layer") and widget.count() == 0:
                    text = self.tr("'{}' has nothing to choose from.")
                else:
                    text = self.tr("'{}' is required.")
                if not pending:
                    text = text.format(field["label"])
                self._validation_label.setText(text)
                self._validation_label.show()
                return

        if self._validate is not None:
            try:
                self._validate(values)
            except Abandoned:
                return  # the user stopped waiting: the form stays as it is
            except Exception as error:  # a refusal: say it, keep the input
                self._validation_label.setText(str(error) or type(error).__name__)
                self._validation_label.show()
                return
        self.accept()
