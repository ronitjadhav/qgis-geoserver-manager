#! python3  # noqa E265

"""
Usage from the repo root folder:

.. code-block:: bash

    # for whole tests
    python -m unittest tests.qgis.test_resource_form
    # for specific test
    python -m unittest tests.qgis.test_resource_form.TestResourceFormDialog.test_required_field_on_other_tab_blocks_save
"""

# standard library
from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtWidgets import QDialog
from qgis.testing import start_app, unittest

# project
from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog

start_app()

# ############################################################################
# ########## Classes #############
# ################################

FIELDS = [
    {"key": "name", "label": "Name", "type": "text", "required": True},
    # Second group -> rendered on a second tab, hidden while "General" is active
    {
        "key": "host",
        "label": "Host",
        "type": "text",
        "required": True,
        "group": "Connection",
    },
    {
        "key": "token",
        "label": "Token",
        "type": "text",
        "required": True,
        "visible": False,
        "group": "Connection",
    },
]


class TestResourceFormDialog(unittest.TestCase):
    """Validation must depend on what the form asks for, not on which tab is
    currently on screen: Qt reports every widget on an inactive tab as hidden.
    """

    def test_a_required_field_blocks_save_on_any_tab_unless_hidden(self):
        dlg = ResourceFormDialog(title="New", fields=FIELDS)
        dlg.show()  # "General" is the active tab, "Connection" is not
        dlg.get_widget("name").setText("some-name")

        dlg._on_accept()  # host is empty and required
        self.assertFalse(dlg.result())

        dlg.get_widget("host").setText("localhost")
        dlg.set_field_visible("token", True)
        dlg._on_accept()  # token shown and empty -> blocked
        self.assertFalse(dlg.result())

        dlg.set_field_visible("token", False)
        dlg._on_accept()  # token hidden -> not applicable
        self.assertTrue(dlg.result())


class TestReadOnlyAndWideFields(unittest.TestCase):
    """Review of 2026-09-23: read-only boxes looked editable, and a style's
    definition sat squeezed beside its label."""

    def test_a_read_only_box_reads_as_text(self):
        from qgis.PyQt.QtWidgets import QPlainTextEdit

        dlg = ResourceFormDialog(
            title="t",
            fields=[
                {"key": "srs", "label": "SRS", "type": "text", "read_only": True},
                {
                    "key": "abstract",
                    "label": "Abstract",
                    "type": "textarea",
                    "read_only": True,
                },
                {"key": "name", "label": "Name", "type": "text"},
            ],
        )
        self.assertFalse(dlg.get_widget("srs").hasFrame())
        self.assertEqual(
            dlg.get_widget("abstract").frameShape(), QPlainTextEdit.Shape.NoFrame
        )
        self.assertTrue(dlg.get_widget("name").hasFrame())  # editable stays a box

    def test_a_wide_code_field_spans_the_form_without_wrapping(self):
        from qgis.gui import QgsCodeEditorHTML
        from qgis.PyQt.Qsci import QsciScintilla

        dlg = ResourceFormDialog(
            title="t",
            fields=[
                {
                    "key": "body",
                    "label": "Definition",
                    "type": "textarea",
                    "wide": True,
                    "code": "xml",
                }
            ],
            values={"body": "<sld/>"},
        )
        label, _row = dlg._row_widgets["body"]
        self.assertTrue(label.isHidden())
        body = dlg.get_widget("body")
        # QGIS's editor: highlighted, the user's code colours and font.
        self.assertIsInstance(body, QgsCodeEditorHTML)
        self.assertEqual(body.wrapMode(), QsciScintilla.WrapMode.WrapNone)
        self.assertEqual(dlg.get_values()["body"], "<sld/>")
        dlg.set_values({"body": "<other/>"})
        self.assertEqual(dlg.get_values()["body"], "<other/>")

    def test_passwords_and_files_use_qgis_widgets_and_a_file_can_be_required(self):
        # A password can be checked with QGIS's eye toggle, and a file
        # dropped on its box is taken.
        from qgis.gui import QgsFileWidget, QgsPasswordLineEdit

        dlg = ResourceFormDialog(
            title="t",
            fields=[
                {"key": "pw", "label": "P", "type": "text", "echo_password": True},
                {"key": "f", "label": "F", "type": "file", "filter": "SLD (*.sld)"},
            ],
            values={"pw": "secret", "f": "/tmp/a.sld"},
        )
        self.assertIsInstance(dlg.get_widget("pw"), QgsPasswordLineEdit)
        self.assertIsInstance(dlg.get_widget("f"), QgsFileWidget)
        self.assertEqual(dlg.get_values(), {"pw": "secret", "f": "/tmp/a.sld"})

        required = ResourceFormDialog(
            title="t",
            fields=[{"key": "file", "label": "File", "type": "file", "required": True}],
        )
        required.show()
        required._on_accept()
        self.assertFalse(required.result())  # empty + required -> blocked
        required.get_widget("file").setFilePath("/tmp/a.sld")
        self.assertEqual(required.get_values()["file"], "/tmp/a.sld")
        required._on_accept()
        self.assertTrue(required.result())

    def test_a_read_only_text_is_copyable_not_greyed(self):
        form = ResourceFormDialog(
            title="t",
            fields=[{"key": "url", "label": "URL", "type": "text", "read_only": True}],
            values={"url": "http://x"},
        )
        widget = form.get_widget("url")
        self.assertTrue(widget.isReadOnly())
        self.assertTrue(widget.isEnabled())


class TestResourceFormHeight(unittest.TestCase):
    """The form opens tall enough for its wrapped text."""

    def test_wrapped_help_text_is_not_clipped(self):
        # A top-level window ignores height-for-width, so a form whose
        # description and help text wrap opened too short and squeezed its rows.
        from qgis.PyQt.QtWidgets import QApplication

        help_text = "A hint long enough to wrap onto a second and a third line. " * 2
        fields = [
            {"key": "name", "label": "Name", "type": "text"},
            {"key": "a", "label": "Isolated", "type": "checkbox", "help": help_text},
            {"key": "b", "label": "Default", "type": "checkbox", "help": help_text},
            {"key": "own", "label": "Own", "type": "checkbox", "group": "WMS"},
        ] + [
            {"key": f"w{i}", "label": "Title", "type": "text", "group": "WMS"}
            for i in range(12)
        ]
        dlg = ResourceFormDialog(
            title="t", description="A description that wraps. " * 6, fields=fields
        )
        # The workspace form hides its WMS fields until "Own" is ticked.
        for i in range(12):
            dlg.set_field_visible(f"w{i}", False)
        # How short it opened depended on the scale factor; at 2x it was
        # clipped badly. Opening at the minimum size shows it at any scale.
        dlg.resize(dlg.minimumSizeHint())
        dlg.show()
        QApplication.processEvents()
        needed = dlg.layout().totalHeightForWidth(dlg.width())
        self.assertGreaterEqual(dlg.height(), needed)
        dlg.close()

    def test_a_form_without_a_description_opens_unscrolled(self):
        # Its layout has no height-for-width, and the height needed was -1.
        from qgis.PyQt.QtWidgets import QApplication

        fields = [{"key": f"f{i}", "label": "Field", "type": "text"} for i in range(8)]
        dlg = ResourceFormDialog(title="t", fields=fields)
        dlg.resize(dlg.minimumSizeHint())
        dlg.show()
        self.addCleanup(dlg.close)
        QApplication.processEvents()
        self.assertEqual(dlg._field_page["f0"].verticalScrollBar().maximum(), 0)


class TestResourceFormResize(unittest.TestCase):
    """A resized form scrolls or grows; it never squeezes or overlaps."""

    def show(self, fields, size=None):
        from qgis.PyQt.QtWidgets import QApplication

        dlg = ResourceFormDialog(title="t", fields=fields)
        dlg.show()
        if size:
            dlg.resize(*size)
        QApplication.processEvents()
        self.addCleanup(dlg.close)
        return dlg

    def test_hidden_rows_leave_no_gap(self):
        # A datastore form hides the other types' parameters: Qt 5 kept
        # their row spacing, and the tab opened on a blank band.
        fields = [
            {"key": f"h{i}", "label": "Hidden", "type": "text", "visible": False}
            for i in range(10)
        ] + [{"key": "shown", "label": "Shown", "type": "text"}]
        dlg = self.show(fields)
        self.assertLess(dlg.get_widget("shown").parentWidget().y(), 20)

    def test_a_tall_form_scrolls_instead_of_squeezing(self):
        # Laid out straight in the dialog, it could not be shorter than all
        # its rows, and wrapped help was drawn over the next row.
        fields = [
            {"key": f"f{i}", "label": "Field", "type": "text", "help": "A hint"}
            for i in range(30)
        ]
        dlg = self.show(fields, size=(460, 300))
        page = dlg._field_page["f0"]
        self.assertEqual(dlg.height(), 300)
        self.assertGreater(page.verticalScrollBar().maximum(), 0)
        field = dlg.get_widget("f0")
        self.assertGreaterEqual(field.height(), field.sizeHint().height())

    def test_a_list_grows_with_the_dialog_and_keeps_its_help_close(self):
        fields = [
            {
                "key": "rows",
                "label": "Rows",
                "type": "table",
                "columns": [{"label": "Name"}],
                "help": "Uncapped: takes the height.",
            },
            {"key": "words", "label": "Words", "type": "list", "help": "Capped."},
        ]
        dlg = self.show(fields, size=(700, 900))
        table, words = dlg.get_widget("rows"), dlg.get_widget("words")
        self.assertGreater(table.height(), 300)
        wrapper = words.parentWidget().layout()
        help_label = wrapper.itemAt(1).widget()
        self.assertLess(help_label.y() - words.geometry().bottom(), 10)

    def test_a_long_choice_does_not_widen_the_form(self):
        # A long layer or style name once set the whole dialog's width.
        fields = [{"key": "c", "label": "C", "type": "combo", "options": ["x" * 300]}]
        dlg = self.show(fields)
        self.assertLess(dlg.minimumSizeHint().width(), 600)

    def test_a_missing_field_below_the_fold_is_scrolled_into_view(self):
        fields = [
            {"key": f"f{i}", "label": "Field", "type": "text"} for i in range(30)
        ] + [{"key": "last", "label": "Last", "type": "text", "required": True}]
        dlg = self.show(fields, size=(460, 300))
        page = dlg._field_page["last"]
        dlg._on_accept()
        last = dlg.get_widget("last")
        top = last.mapTo(page.viewport(), last.rect().topLeft()).y()
        self.assertTrue(0 <= top < page.viewport().height())


class TestKeyedOptions(unittest.TestCase):
    """A combo shows a label and hands back its value, so the label can be
    translated without breaking the code that compares it."""

    def test_the_value_comes_back_and_the_label_is_shown(self):
        seen = []
        dlg = ResourceFormDialog(
            title="t",
            fields=[
                {
                    "key": "source",
                    "label": "Source",
                    "type": "combo",
                    "options": [("Une table", "table"), ("Une couche", "qgis")],
                }
            ],
        )
        combo = dlg.get_widget("source")
        dlg.on_value_changed("source", seen.append)
        self.assertEqual(combo.currentText(), "Une table")
        self.assertEqual(dlg.get_values()["source"], "table")
        dlg.set_values({"source": "qgis"})
        self.assertEqual(combo.currentText(), "Une couche")
        self.assertEqual(seen, ["qgis"])

    def test_plain_options_still_hand_back_their_text(self):
        dlg = ResourceFormDialog(
            title="t",
            fields=[{"key": "t", "label": "T", "type": "combo", "options": ["a", "b"]}],
            values={"t": "b"},
        )
        self.assertEqual(dlg.get_values()["t"], "b")


class TestListValues(unittest.TestCase):
    """What a field hands back: lists, tables, numbers, text."""

    def form(self, fields, values=None):
        return ResourceFormDialog(title="t", fields=fields, values=values)

    def test_an_empty_row_is_not_a_keyword_named_null(self):
        from qgis.PyQt.QtWidgets import QToolButton

        dlg = self.form(
            [{"key": "k", "label": "K", "type": "list"}], {"k": ["roads", "  "]}
        )
        # + pressed, then Esc: QGIS's row holds a NULL, whose str() is "NULL"
        dlg.get_widget("k").findChild(QToolButton, "addButton").click()
        self.assertEqual(dlg.get_values()["k"], ["roads"])

    def test_a_value_never_typed_is_blank_not_none(self):
        from qgis.core import NULL

        dlg = self.form([{"key": "p", "label": "P", "type": "keyvalue"}])
        dlg.get_widget("p").setMap({"STYLES": NULL, "": "x"})
        self.assertEqual(dlg.get_values()["p"], {"STYLES": ""})

    def test_an_empty_parameter_opens_blank(self):
        # GeoServer writes one without a value; the library reads it as None.
        dlg = self.form(
            [{"key": "p", "label": "P", "type": "keyvalue"}],
            {"p": {"Session startup SQL": None, "max connections": 10}},
        )
        self.assertEqual(
            dlg.get_values()["p"],
            {"Session startup SQL": "", "max connections": "10"},
        )

    def test_a_parameter_name_is_not_cut_off(self):
        from qgis.PyQt.QtWidgets import QTableView

        dlg = self.form(
            [{"key": "p", "label": "P", "type": "keyvalue"}],
            {"p": {"Expose primary keys": "false", "a": "b"}},
        )
        view = dlg.get_widget("p").findChild(QTableView)
        needed = view.fontMetrics().horizontalAdvance("Expose primary keys")
        self.assertGreaterEqual(view.horizontalHeader().sectionSize(0), needed)

    def test_a_name_picked_but_not_added_stops_save(self):
        dlg = self.form(
            [
                {
                    "key": "t",
                    "label": "Styles",
                    "type": "table",
                    "choices": ["a"],
                    "columns": [{"label": "Style"}],
                }
            ]
        )
        dlg.get_widget("t").picker.setCurrentText("a")
        dlg._on_accept()
        self.assertFalse(dlg.result())
        self.assertIn("'a' is picked in 'Styles'", dlg._validation_label.text())

    def test_a_unique_list_does_not_take_a_name_twice(self):
        dlg = self.form(
            [
                {
                    "key": "t",
                    "label": "T",
                    "type": "table",
                    "unique": True,
                    "columns": [{"label": "Gridset"}],
                }
            ],
            {"t": ["EPSG:4326"]},
        )
        table = dlg.get_widget("t")
        table.picker.setCurrentText("EPSG:4326")
        table._add_picked()
        self.assertEqual(table.rows(), ["EPSG:4326"])

    def test_a_stored_number_outside_the_range_comes_back_as_it_was(self):
        # Clamped, an untouched Save rewrote it: a zoom of 45 as 40 in a
        # table, a 32x32 meta-tile, 200 cascaded connections in a spin box.
        spin = {"label": "Z", "type": "spin", "min": 0, "max": 40}
        dlg = self.form(
            [
                {
                    "key": "t",
                    "label": "T",
                    "type": "table",
                    "columns": [{"label": "Gridset"}, spin, spin],
                },
                {"key": "n", "label": "N", "type": "spinbox", "min": 1, "max": 20},
            ],
            {"t": [["big", 3, 45], ["low", -3, None]], "n": 32},
        )
        self.assertEqual(dlg.get_values()["t"], [["big", 3, 45], ["low", -3, None]])
        self.assertEqual(dlg.get_values()["n"], 32)

    def test_a_password_keeps_its_edge_spaces(self):
        dlg = ResourceFormDialog(
            title="t",
            fields=[
                {"key": "user", "label": "User", "type": "text"},
                {
                    "key": "password",
                    "label": "Password",
                    "type": "text",
                    "echo_password": True,
                },
            ],
        )
        dlg.get_widget("user").setText(" admin ")
        dlg.get_widget("password").setText(" s3cret ")
        self.assertEqual(dlg.get_values(), {"user": "admin", "password": " s3cret "})


class TestKeysAndWheel(unittest.TestCase):
    """Review of 2026-09-24: each failed before its fix."""

    def test_enter_in_a_list_picker_adds_the_row_and_keeps_the_form(self):
        # The key also reached the dialog's default button: Save.
        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtTest import QTest
        from qgis.PyQt.QtWidgets import QApplication

        dlg = ResourceFormDialog(
            title="t",
            fields=[
                {
                    "key": "rows",
                    "label": "R",
                    "type": "table",
                    "choices": ["a"],
                    "columns": [{"label": "Name"}],
                }
            ],
        )
        dlg.show()
        self.addCleanup(dlg.close)
        edit = dlg.get_widget("rows").picker.lineEdit()
        edit.setText("a")
        QTest.keyClick(edit, Qt.Key.Key_Return)
        QApplication.processEvents()
        self.assertEqual(dlg.get_values()["rows"], ["a"])
        self.assertTrue(dlg.isVisible())

    def test_a_form_opens_with_the_cursor_in_its_first_field(self):
        from qgis.PyQt.QtWidgets import QApplication

        dlg = ResourceFormDialog(
            title="t",
            fields=[
                {"key": "ro", "label": "R", "type": "text", "read_only": True},
                {"key": "name", "label": "Name", "type": "text"},
            ],
        )
        dlg.show()
        self.addCleanup(dlg.close)
        QApplication.processEvents()
        self.assertIs(dlg.focusWidget(), dlg.get_widget("name"))

    def test_scrolling_over_a_combo_scrolls_the_form_not_the_combo(self):
        # A layer's projection policy changed under the cursor, and Save sent it.
        from qgis.PyQt.QtCore import QPoint, QPointF, Qt
        from qgis.PyQt.QtGui import QWheelEvent
        from qgis.PyQt.QtWidgets import QApplication

        def wheel(widget):
            event = QWheelEvent(
                QPointF(5, 5),
                QPointF(widget.mapToGlobal(QPoint(5, 5))),
                QPoint(0, 0),
                QPoint(0, -120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.NoScrollPhase,
                False,
            )
            QApplication.sendEvent(widget, event)
            QApplication.processEvents()

        texts = [{"key": f"t{i}", "label": "T", "type": "text"} for i in range(15)]
        combo = {"key": "c", "label": "C", "type": "combo", "options": ["A", "B"]}
        dlg = ResourceFormDialog(title="t", fields=texts + [combo] + texts[:0])
        dlg.resize(460, 300)
        dlg.show()
        self.addCleanup(dlg.close)
        wheel(dlg._field_page["c"].viewport())  # scrolling the form...
        wheel(dlg.get_widget("c"))  # ...when the cursor reaches the combo
        self.assertEqual(dlg.get_values()["c"], "A")

    def test_fields_shown_later_widen_the_dialog_instead_of_being_cut(self):
        # A datastore's WFS or Other... fields went off the right edge.
        from qgis.PyQt.QtWidgets import QApplication

        from tests.qgis.sync_dialog import SyncDialog

        tab = SyncDialog()
        dlg = ResourceFormDialog(title="t", fields=tab._datastore_fields(["topp"]))
        dlg.show()
        self.addCleanup(dlg.close)
        dlg.resize(dlg.minimumSizeHint())
        QApplication.processEvents()
        tab._on_type_changed(dlg, "Other...")
        for _ in range(3):
            QApplication.processEvents()
        page = dlg._field_page["name"]
        self.assertGreaterEqual(
            page.viewport().width(), page.widget().minimumSizeHint().width()
        )


class TestValidateBeforeClosing(unittest.TestCase):
    """A save's refusal (a taken name) came after the form closed, and the
    input was lost: a whole PostGIS form, its password included."""

    def form(self, validate):
        dlg = ResourceFormDialog(
            title="t",
            fields=[
                {"key": "name", "label": "Name", "type": "text", "required": True},
                {"key": "pw", "label": "P", "type": "text", "echo_password": True},
            ],
            values={"name": "roads", "pw": "secret"},
            validate=validate,
        )
        dlg.show()
        self.addCleanup(dlg.close)
        return dlg

    def test_a_refusal_keeps_the_form_open_with_its_input(self):
        def taken(values):
            raise ValueError(f"'{values['name']}' already exists.")

        dlg = self.form(taken)
        dlg._on_accept()
        self.assertFalse(dlg.result())
        self.assertTrue(dlg.isVisible())
        self.assertEqual(dlg._validation_label.text(), "'roads' already exists.")
        self.assertEqual(dlg.get_values(), {"name": "roads", "pw": "secret"})

    def test_a_cancelled_wait_keeps_the_form_open_and_says_nothing(self):
        from geoserver_manager.toolbelt.rest import Abandoned

        def stopped(values):
            raise Abandoned()

        dlg = self.form(stopped)
        dlg._on_accept()
        self.assertFalse(dlg.result())
        self.assertTrue(dlg._validation_label.isHidden())

    def test_a_missing_field_is_named_in_words(self):
        form = ResourceFormDialog(
            title="t",
            fields=[{"key": "name", "label": "Name", "type": "text", "required": True}],
        )
        form._on_accept()
        self.assertFalse(form._validation_label.isHidden())
        self.assertEqual(form._validation_label.text(), "'Name' is required.")

    def test_an_empty_combo_says_there_is_nothing_to_pick(self):
        form = ResourceFormDialog(
            title="t",
            fields=[
                {
                    "key": "table",
                    "label": "Table",
                    "type": "combo",
                    "options": [],
                    "required": True,
                }
            ],
        )
        form._on_accept()
        self.assertEqual(
            form._validation_label.text(), "'Table' has nothing to choose from."
        )

    def test_a_bad_url_keeps_the_dialog_open(self):
        form = ResourceFormDialog(
            title="t",
            fields=[{"key": "url", "label": "URL", "type": "text", "url": True}],
            values={"url": "ftp://example.org"},
        )
        form._on_accept()
        self.assertNotEqual(form.result(), QDialog.DialogCode.Accepted)
        self.assertIn("http://", form._validation_label.text())


class TestEscapeAsksFirst(unittest.TestCase):
    """Esc closed a style's editor and dropped the edit without a word."""

    def form(self):
        dlg = ResourceFormDialog(
            title="t",
            fields=[{"key": "body", "label": "B", "type": "textarea", "code": "xml"}],
            values={"body": "<sld/>"},
        )
        dlg.show()
        self.addCleanup(dlg.done, 0)
        return dlg

    def answer(self, button):
        from unittest.mock import patch

        from qgis.PyQt.QtWidgets import QMessageBox

        asked = []
        patcher = patch.object(
            QMessageBox, "question", lambda *a: asked.append(a) or button
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return asked

    def close_button(self, dlg):
        """The title bar's X: on Qt6, QWindow.close() is not a spontaneous close."""
        from qgis.PyQt.QtCore import QCoreApplication
        from qgis.PyQt.QtGui import QCloseEvent

        QCoreApplication.sendEvent(dlg.windowHandle(), QCloseEvent())

    def test_the_escape_key_asks_and_stays_on_cancel(self):
        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtTest import QTest
        from qgis.PyQt.QtWidgets import QMessageBox

        asked = self.answer(QMessageBox.StandardButton.Cancel)
        dlg = self.form()
        dlg.set_values({"body": "<changed/>"})
        QTest.keyClick(dlg, Qt.Key.Key_Escape)
        self.assertEqual(len(asked), 1)
        self.assertTrue(dlg.isVisible())

    def test_the_window_close_button_asks_too(self):
        from qgis.PyQt.QtWidgets import QMessageBox

        asked = self.answer(QMessageBox.StandardButton.Cancel)
        dlg = self.form()
        dlg.set_values({"body": "<changed/>"})
        self.close_button(dlg)
        self.assertEqual(len(asked), 1)
        self.assertTrue(dlg.isVisible())

    def test_discard_closes_it_and_close_stays_quiet(self):
        from qgis.PyQt.QtWidgets import QMessageBox

        asked = self.answer(QMessageBox.StandardButton.Discard)
        for close in (self.close_button, lambda form: form._on_cancel()):
            dlg = self.form()
            dlg.set_values({"body": "<changed/>"})
            close(dlg)
            self.assertFalse(dlg.isVisible())
        quiet = self.form()
        quiet.set_values({"body": "<changed/>"})
        quiet.close()
        self.assertFalse(quiet.isVisible())
        self.assertEqual(len(asked), 2)

    def test_a_viewer_with_a_button_of_its_own_stays_a_viewer(self):
        # The seed tasks viewer's Stop all showed Save again, and its polled
        # text then made Close ask to discard changes.
        from qgis.PyQt.QtWidgets import QDialogButtonBox, QMessageBox

        asked = self.answer(QMessageBox.StandardButton.Cancel)
        viewer = self.form()
        viewer.hide_save_button()
        viewer.add_button("Stop all")
        save = viewer._button_box.button(QDialogButtonBox.StandardButton.Ok)
        self.assertFalse(save.isVisible())
        viewer.set_values({"body": "<polled/>"})
        self.close_button(viewer)
        self.assertFalse(viewer.isVisible())
        self.assertEqual(asked, [])

    def test_an_untouched_form_closes_without_asking(self):
        from qgis.PyQt.QtWidgets import QMessageBox

        asked = self.answer(QMessageBox.StandardButton.Cancel)
        dlg = self.form()
        dlg._on_cancel()
        self.assertFalse(dlg.isVisible())
        self.assertEqual(asked, [])


class TestCrsPicker(unittest.TestCase):
    def test_the_button_fills_the_code_from_qgis_crs_picker(self):
        from unittest.mock import patch

        from qgis.core import QgsCoordinateReferenceSystem

        from geoserver_manager.gui import dlg_resource_form

        shown = []

        class Picker:
            def __init__(self, parent):
                pass

            def setCrs(self, crs):  # noqa: N802
                shown.append(crs.authid())

            def exec(self):
                return True

            def crs(self):
                return QgsCoordinateReferenceSystem("EPSG:3857")

        dlg = ResourceFormDialog(
            title="t",
            fields=[{"key": "srs", "label": "SRS", "type": "text", "crs": True}],
            values={"srs": "4326"},  # a bare code, as the publish form takes it
        )
        edit = dlg.get_widget("srs")
        (action,) = edit.actions()
        with patch.object(dlg_resource_form, "QgsProjectionSelectionDialog", Picker):
            action.trigger()
        self.assertEqual(shown, ["EPSG:4326"])  # opened on the current code
        self.assertEqual(dlg.get_values()["srs"], "EPSG:3857")

    def test_a_code_qgis_does_not_know_can_still_be_typed(self):
        dlg = ResourceFormDialog(
            title="t",
            fields=[{"key": "srs", "label": "SRS", "type": "text", "crs": True}],
            values={"srs": "EPSG:900913"},
        )
        self.assertEqual(dlg.get_values()["srs"], "EPSG:900913")


class TestExtentField(unittest.TestCase):
    def test_an_area_reads_as_its_corners_in_the_forms_crs(self):
        from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle
        from qgis.PyQt.QtWidgets import QLineEdit

        dlg = ResourceFormDialog(
            title="t", fields=[{"key": "area", "label": "Area", "type": "extent"}]
        )
        self.assertEqual(dlg.get_values()["area"], "")
        dlg.set_extent_crs("area", "EPSG:4326")
        self.assertEqual(dlg.get_widget("area").findChild(QLineEdit).text(), "")
        dlg.get_widget("area").setOutputExtentFromUser(
            QgsRectangle(1.5, 2, 3, 4), QgsCoordinateReferenceSystem("EPSG:4326")
        )
        self.assertEqual(dlg.get_values()["area"], "1.5, 2.0, 3.0, 4.0")


class TestLayersNamedAlike(unittest.TestCase):
    def setUp(self):
        self.project = QgsProject.instance()
        self.project.removeAllMapLayers()

    def tearDown(self):
        self.project.removeAllMapLayers()

    def test_two_layers_named_alike_are_two_entries_that_both_resolve(self):
        # A picker of labels handed the first layer to whoever picked the
        # second; QGIS's layer combo holds the layers themselves.
        first = QgsVectorLayer("Point?crs=EPSG:4326", "roads", "memory")
        second = QgsVectorLayer("Point?crs=EPSG:4326", "roads", "memory")
        self.project.addMapLayers([first, second])
        form = ResourceFormDialog(
            title="t", fields=[{"key": "layer", "label": "L", "type": "layer"}]
        )
        combo = form.get_widget("layer")
        self.assertEqual(combo.count(), 2)
        picked = set()
        for index in range(2):
            combo.setCurrentIndex(index)
            picked.add(form.get_values()["layer"].id())
        self.assertEqual(picked, {first.id(), second.id()})


class TestLongTextOpensAtItsStart(unittest.TestCase):
    def test_a_long_value_shows_its_beginning(self):
        # A long title or URL used to open scrolled to its end.
        dlg = ResourceFormDialog(
            title="t",
            fields=[{"key": "url", "label": "URL", "type": "text"}],
            values={"url": "http://example.org/" + "x" * 300},
        )
        self.assertEqual(dlg.get_widget("url").cursorPosition(), 0)
        dlg.set_values({"url": "http://other.example.org/" + "y" * 300})
        self.assertEqual(dlg.get_widget("url").cursorPosition(), 0)


class TestActionButton(unittest.TestCase):
    def test_a_form_can_carry_a_button_of_its_own(self):
        # Show the log, Stop all: two tabs reached into the button box.
        from qgis.PyQt.QtWidgets import QDialogButtonBox

        dlg = ResourceFormDialog(
            title="t", fields=[{"key": "n", "label": "N", "type": "text"}]
        )
        button = dlg.add_button("Show the log")
        self.assertIn(button, dlg._button_box.buttons())
        self.assertEqual(
            dlg._button_box.buttonRole(button),
            QDialogButtonBox.ButtonRole.ActionRole,
        )


class TestFormLifetime(unittest.TestCase):
    """A form is freed once its caller is done with it, and not before.

    QDialog.exec() deletes a WA_DeleteOnClose dialog before it returns, so
    the get_values() that every caller runs next read a dead widget. The
    form frees itself with deleteLater() instead, which runs once control
    is back in the event loop: after the caller's waits and questions.
    """

    def test_values_are_readable_after_exec_and_the_form_is_freed_later(self):
        from qgis.PyQt import sip
        from qgis.PyQt.QtCore import QEventLoop, QTimer
        from qgis.PyQt.QtWidgets import QApplication, QWidget

        # A child of the main dialog, with a layer combo that follows the
        # project: what stayed alive for the whole QGIS session.
        parent = QWidget()
        self.addCleanup(parent.deleteLater)
        dlg = ResourceFormDialog(
            title="t",
            fields=[
                {"key": "name", "label": "Name", "type": "text"},
                {"key": "layer", "label": "L", "type": "layer", "allow_empty": True},
            ],
            values={"name": "typed"},
            parent=parent,
        )
        seen = []
        loop = QEventLoop()

        def read():
            try:
                return dlg.get_values()["name"]
            except RuntimeError as error:  # a dead widget: the test fails
                return str(error)

        def caller():  # as a button's handler: inside event delivery
            try:
                QTimer.singleShot(0, dlg.accept)
                dlg.exec()
                seen.append(read())
                QApplication.processEvents()  # what a wait for GeoServer does
                seen.append(read())
                seen.append(sip.isdeleted(dlg))
            finally:
                loop.quit()

        QTimer.singleShot(0, caller)
        loop.exec()
        QApplication.processEvents()

        self.assertEqual(seen, ["typed", "typed", False])
        self.assertTrue(sip.isdeleted(dlg))
        self.assertEqual(parent.findChildren(ResourceFormDialog), [])


class TestImageField(unittest.TestCase):
    """The form dialog's 'image' type: a picture set later, never a value."""

    def dialog(self):
        return ResourceFormDialog(
            title="t",
            fields=[
                {
                    "key": "legend",
                    "label": "Legend",
                    "type": "image",
                    "placeholder": "Loading…",
                    "max_height": 100,
                },
                {"key": "name", "label": "Name", "type": "text"},
            ],
        )

    def test_placeholder_then_a_picture_scaled_to_the_cap(self):
        from qgis.PyQt.QtGui import QPixmap

        dlg = self.dialog()
        label = dlg.get_widget("legend")
        self.assertEqual(label.text(), "Loading…")
        tall = QPixmap(20, 300)
        tall.fill()
        dlg.set_image("legend", tall)
        self.assertEqual(label.text(), "")
        self.assertEqual(label.pixmap().height(), 100)

    def test_a_picture_landing_after_show_gets_its_full_height(self):
        # The legend lands while the form is open. The label was laid out for
        # one line of placeholder text and kept that height, so only the first
        # row of a legend showed.
        from qgis.PyQt.QtGui import QPixmap
        from qgis.PyQt.QtWidgets import QApplication

        dlg = self.dialog()
        dlg.show()
        QApplication.processEvents()
        legend = QPixmap(20, 80)
        legend.fill()
        dlg.set_image("legend", legend)
        QApplication.processEvents()
        self.assertGreaterEqual(dlg.get_widget("legend").height(), 80)
        dlg.close()

    def test_no_picture_means_an_explanation(self):
        dlg = self.dialog()
        dlg.set_image("legend", None, "No such style")
        self.assertEqual(dlg.get_widget("legend").text(), "No such style")

    def test_it_is_not_a_value(self):
        dlg = self.dialog()
        dlg.get_widget("name").setText("x")
        self.assertEqual(dlg.get_values(), {"name": "x"})


# ############################################################################
# ####### Stand-alone run ########
# ################################
if __name__ == "__main__":
    unittest.main()
