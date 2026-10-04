#! python3  # noqa E265

"""
Keyboard shortcuts, theme-safe colours and the remembered tab.

Usage from the repo root folder:

.. code-block:: bash

    QT_QPA_PLATFORM=offscreen python -m unittest tests.qgis.test_ux
"""

import time
from contextlib import contextmanager
from unittest.mock import patch

from qgis.PyQt.QtCore import QCoreApplication, Qt, QTranslator
from qgis.PyQt.QtGui import QColor, QPalette
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QApplication
from qgis.testing import start_app, unittest

from geoserver_manager.gui import dlg_main
from geoserver_manager.gui.tab_gwc import GwcTabMixin
from geoserver_manager.gui.theme import (
    contrast_ratio,
    hint_colour,
    is_dark,
    status_colour,
)
from tests.qgis.sync_dialog import FakePrefs, SyncDialog

start_app()


class EnglishPlurals(QTranslator):
    """What the English .qm does with a counted message: "(s)" by the count."""

    def translate(self, context, source, disambiguation=None, n=-1):
        if n >= 0 and "(s)" in source:
            return source.replace("(s)", "" if n == 1 else "s")
        return source

    def isEmpty(self):  # noqa: N802 (Qt asks before consulting translate())
        return False


@contextmanager
def english_plurals():
    translator = EnglishPlurals()
    QCoreApplication.installTranslator(translator)
    try:
        yield
    finally:
        QCoreApplication.removeTranslator(translator)


def palette_for(window, text):
    """A palette with the given window and text colours."""
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(window))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(text))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(text))
    return palette


LIGHT = palette_for("#f0f0f0", "#202020")
DARK = palette_for("#232629", "#eff0f1")  # roughly QGIS's Night Mapping


class FakeGS:
    """Enough for a tab to load with no rows."""

    def get_workspaces(self):
        return ([], 200)

    def get_styles(self, workspace_name=None):
        return ([], 200)


class TestThemeColours(unittest.TestCase):
    """Literal red / green / gray is what this replaces."""

    def test_dark_and_light_palettes_are_told_apart(self):
        self.assertTrue(is_dark(DARK))
        self.assertFalse(is_dark(LIGHT))

    def test_every_status_colour_is_readable_on_its_background(self):
        # 3:1 is WCAG's floor for large or bold text, which the status line is.
        for palette, name in ((LIGHT, "light"), (DARK, "dark")):
            background = palette.color(QPalette.ColorRole.Window)
            for kind in ("ok", "error", "busy"):
                colour = status_colour(kind, palette)
                ratio = contrast_ratio(colour, background)
                self.assertGreaterEqual(
                    ratio, 3.0, f"{kind} on {name}: {colour} is {ratio:.1f}:1"
                )

    def test_the_colours_differ_between_the_two_themes(self):
        """A single pair cannot serve both, which was the bug."""
        for kind in ("ok", "error", "busy"):
            self.assertNotEqual(status_colour(kind, LIGHT), status_colour(kind, DARK))

    def test_ok_and_error_stay_distinguishable_from_each_other(self):
        for palette in (LIGHT, DARK):
            self.assertNotEqual(
                status_colour("ok", palette), status_colour("error", palette)
            )

    def test_neutral_leaves_the_palette_alone(self):
        self.assertIsNone(status_colour("neutral", LIGHT))
        self.assertIsNone(status_colour("anything else", DARK))

    def test_hint_text_is_visible_without_shouting(self):
        for palette, name in ((LIGHT, "light"), (DARK, "dark")):
            background = palette.color(QPalette.ColorRole.Window)
            ratio = contrast_ratio(hint_colour(palette), background)
            self.assertGreater(ratio, 1.5, f"hint on {name} is {ratio:.1f}:1")

    def test_a_half_alpha_placeholder_is_blended_not_drawn_as_the_text(self):
        # Qt's default: the text at alpha 128, which #rrggbb drew as the text.
        for window, text in (("#f0f0f0", "#202020"), ("#232629", "#eff0f1")):
            palette = palette_for(window, text)
            placeholder = QColor(text)
            placeholder.setAlpha(128)
            palette.setColor(QPalette.ColorRole.PlaceholderText, placeholder)
            hint = hint_colour(palette)
            self.assertNotEqual(hint, QColor(text).name())
            self.assertGreaterEqual(contrast_ratio(hint, window), 3.0, hint)

    def test_contrast_ratio_matches_the_wcag_extremes(self):
        self.assertAlmostEqual(contrast_ratio("#000000", "#ffffff"), 21.0, places=1)
        self.assertAlmostEqual(contrast_ratio("#777777", "#777777"), 1.0, places=1)


class TestStatusLine(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()

    def test_a_kind_becomes_a_stylesheet_colour(self):
        self.dlg._set_status("Connected: http://gs", "ok")
        self.assertIn("Connected", self.dlg.lbl_status.text())
        expected = status_colour("ok", self.dlg.palette())
        self.assertIn(expected, self.dlg.lbl_status.styleSheet())

    def test_neutral_clears_the_stylesheet_rather_than_guessing(self):
        self.dlg._set_status("something", "ok")
        self.dlg._set_status("plain", "neutral")
        self.assertEqual(self.dlg.lbl_status.styleSheet(), "")

    def test_a_live_theme_switch_recolours_it(self):
        # The status once kept the old theme's green, 2.4:1 when dark.
        self.dlg.setPalette(LIGHT)
        self.dlg._set_status("Connected: http://gs", "ok")
        self.assertIn(status_colour("ok", LIGHT), self.dlg.lbl_status.styleSheet())
        self.dlg.setPalette(DARK)
        expected = status_colour("ok", DARK)
        deadline = time.monotonic() + 5
        while (
            expected not in self.dlg.lbl_status.styleSheet()
            and time.monotonic() < deadline
        ):
            QCoreApplication.processEvents()
        self.assertIn(expected, self.dlg.lbl_status.styleSheet())
        background = DARK.color(QPalette.ColorRole.Window)
        self.assertGreaterEqual(contrast_ratio(expected, background), 3.0)


class TestKeyboardShortcuts(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        self.dlg.show_warning_message = lambda text: None
        self.dlg.show_error_message = lambda text: None
        self.dlg.show_success_message = lambda text: None
        self.refreshed = []
        self.dlg.refresh_ui = lambda show_message=False: self.refreshed.append(
            show_message
        )
        self.dlg.show()
        # Offscreen, a window is not active until it is told to be, and an
        # inactive window's widgets never report focus.
        QApplication.setActiveWindow(self.dlg)
        QApplication.processEvents()

    def tearDown(self):
        self.dlg.close()

    def table_with_rows(self, callback):
        """A loaded table, the way a tab loader leaves it."""
        self.dlg._setup_table(["Name", self.dlg.actions_column_label()])
        self.dlg._populate_rows([["ws1"], ["ws2"]])
        self.dlg._setup_delete_selected_button(callback)

    def test_f5_refreshes(self):
        QTest.keyClick(self.dlg, Qt.Key.Key_F5)
        self.assertEqual(self.refreshed, [True])

    def test_ctrl_f_jumps_to_the_search_box_and_selects_what_is_there(self):
        self.dlg.searchBox.setText("roads")
        self.dlg.resultsTable.setFocus()
        QApplication.processEvents()
        QTest.keyClick(self.dlg, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.dlg.searchBox.hasFocus())
        self.assertEqual(self.dlg.searchBox.selectedText(), "roads")

    def test_escape_clears_a_search_but_still_closes_an_empty_dialog(self):
        self.dlg.searchBox.setText("roads")
        QTest.keyClick(self.dlg, Qt.Key.Key_Escape)
        self.assertEqual(self.dlg.searchBox.text(), "")
        self.assertTrue(self.dlg.isVisible())  # cleared, not closed

        QTest.keyClick(self.dlg, Qt.Key.Key_Escape)
        self.assertFalse(self.dlg.isVisible())  # nothing to clear: Esc closes

    def test_delete_deletes_the_selection_when_the_table_has_focus(self):
        deleted = []
        self.table_with_rows(deleted.append)
        self.dlg.resultsTable.selectRow(1)
        self.dlg.resultsTable.setFocus()
        QApplication.processEvents()
        self.assertTrue(self.dlg.btn_delete_selected.isEnabled())

        QTest.keyClick(self.dlg.resultsTable, Qt.Key.Key_Delete)
        self.assertEqual(deleted, [[["ws2"]]])

    def test_delete_while_typing_in_the_search_box_deletes_nothing(self):
        """The dangerous one: Del must not reach the resources from a text field."""
        deleted = []
        self.table_with_rows(deleted.append)
        self.dlg.resultsTable.selectRow(0)
        self.dlg.searchBox.setText("ws")
        self.dlg.searchBox.setFocus()
        QApplication.processEvents()
        self.assertTrue(self.dlg.btn_delete_selected.isEnabled())  # a row is selected

        # Sent to the dialog, as it would arrive if the line edit let it
        # through: the handler itself has to refuse, not the line edit.
        QTest.keyClick(self.dlg, Qt.Key.Key_Delete)
        self.assertEqual(deleted, [])

        # And the ordinary path: the line edit consumes it to erase a character.
        QTest.keyClick(self.dlg.searchBox, Qt.Key.Key_Delete)
        self.assertEqual(deleted, [])

    def test_delete_does_nothing_when_the_button_is_disabled(self):
        deleted = []
        self.table_with_rows(deleted.append)
        self.dlg.resultsTable.clearSelection()
        self.dlg.resultsTable.setFocus()
        QApplication.processEvents()
        self.assertFalse(self.dlg.btn_delete_selected.isEnabled())

        QTest.keyClick(self.dlg.resultsTable, Qt.Key.Key_Delete)
        self.assertEqual(deleted, [])

    def test_the_shortcuts_are_written_in_the_tooltips(self):
        self.assertIn("F5", self.dlg.btn_refresh.toolTip())
        self.assertIn("Del", self.dlg.btn_delete_selected.toolTip())
        self.assertIn("Ctrl+F", self.dlg.searchBox.toolTip())

    def test_del_with_no_connection_refuses_instead_of_crashing(self):
        """The fifth dispatch point: Del while a Refresh has no client yet."""
        self.dlg.gs = None
        called = []
        warnings = []
        self.dlg.show_warning_message = warnings.append
        self.dlg._delete_selected_callback = called.append
        self.dlg.btn_delete_selected.setEnabled(True)  # a selection change does
        self.dlg.resultsTable.setFocus()
        QTest.keyClick(self.dlg, Qt.Key.Key_Delete)
        self.assertEqual(called, [])
        self.assertTrue(warnings, "the refusal must say so")


class TestRememberedTab(unittest.TestCase):
    """The dialog reopens where it was left, when that tab still exists."""

    class Settings:
        def __init__(self, stored=None):
            self.stored = stored or {}

        def get_value_from_key(self, key, default=None, exp_type=None):
            return self.stored.get(key, default)

        def set_value_from_key(self, key, value):
            self.stored[key] = value
            return True

        def get_plg_settings(self):
            raise AssertionError("not needed for this test")

        def save_from_object(self, settings):
            return True

    def dialog_with(self, stored):
        """A dialog built the way the plugin builds it, on stubbed settings.

        The settings have to be in place *during* __init__, which is when the
        navigation is built; assigning them afterwards would test nothing.
        """
        with patch.object(dlg_main, "PlgOptionsManager", lambda: self.Settings(stored)):
            return SyncDialog()

    def test_it_opens_on_the_remembered_tab(self):
        dlg = self.dialog_with({"last_tab": 3})
        self.assertEqual(dlg.navList.currentRow(), 3)

    def test_a_tab_that_no_longer_exists_falls_back_to_the_first(self):
        dlg = self.dialog_with({"last_tab": 99})
        self.assertEqual(dlg.navList.currentRow(), 0)

    def test_nonsense_in_the_settings_is_not_a_crash(self):
        for value in ("not a number", None, "", [1]):
            dlg = self.dialog_with({"last_tab": value})
            self.assertEqual(dlg.navList.currentRow(), 0, repr(value))

    def test_closing_writes_the_current_tab_down(self):
        dlg = self.dialog_with({})
        dlg.navList.setCurrentRow(2)
        dlg._store_settings()
        self.assertEqual(dlg.plg_settings.stored["last_tab"], 2)


# ############################################################################
# ##### Labels and empty states ##
# ################################


class TestPrimaryButtons(unittest.TestCase):
    """A form's primary button says what it does; only edits say Save."""

    def test_the_default_is_save_and_it_can_be_named(self):
        from qgis.PyQt.QtWidgets import QDialogButtonBox

        from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog

        plain = ResourceFormDialog(title="t", fields=[])
        named = ResourceFormDialog(title="t", fields=[], ok_label="Publish")
        ok = QDialogButtonBox.StandardButton.Ok
        self.assertEqual(plain._button_box.button(ok).text(), "Save")
        self.assertEqual(named._button_box.button(ok).text(), "Publish")

    def test_every_add_dialog_names_its_action_and_no_add_button_says_new(self):
        """Driven by TABS itself, so a new tab is checked without touching this."""
        import inspect

        from qgis.PyQt.QtWidgets import QDialog, QDialogButtonBox

        from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog

        opened = []

        class Recording(ResourceFormDialog):
            def exec(inner):
                opened.append(inner)
                return QDialog.DialogCode.Rejected

        class Response:
            status_code = 200
            text = ""

            def __init__(inner, payload):
                inner.payload = payload

            def json(inner):
                return inner.payload

        class Client:
            def get(inner, path, **kwargs):
                if path.startswith("/gwc/"):
                    return Response([])  # nothing cached, no gridsets
                if path.endswith("/layers.json"):  # one layer the cache can take
                    return Response({"layers": {"layer": [{"name": "topp:states"}]}})
                return Response({})

        class Endpoints:
            base_url = "/rest"

            def __getattr__(inner, name):
                return lambda *args, **kwargs: f"/rest/{name}.json"

        class GwcEndpoints:
            def __getattr__(inner, name):
                return lambda *args, **kwargs: f"/gwc/rest/{name}.json"

        class Rest:
            rest_client = Client()
            rest_endpoints = Endpoints()
            gwc_endpoints = GwcEndpoints()

        class FakeGS:
            rest_service = Rest()

            def get_workspaces(inner):
                return ([{"name": "topp"}], 200)

            def get_datastores(inner, workspace_name):
                return ([], 200)

            def __getattr__(inner, name):
                raise AttributeError(name)

        dlg = SyncDialog()
        dlg.gs = FakeGS()
        dlg.show_warning_message = dlg.show_error_message = lambda text: None
        dlg._all_layer_names = lambda: []
        dlg._all_group_names = lambda workspace_names=None: []
        dlg._style_choices = lambda workspace_name: []
        ok = QDialogButtonBox.StandardButton.Ok
        seen = {}
        without_add = []
        for _label, _icon, loader in type(dlg).TABS:
            getattr(dlg, loader)()
            if dlg.btn_add.isHidden():
                without_add.append(loader)  # nothing to add there
                continue
            label = dlg.btn_add.text()
            self.assertNotIn(
                "New", label, label
            )  # "Add a Workspace", not "Add a New …"
            self.assertRegex(label, r"^(Add|Publish|Create|Upload) ", label)
            module = inspect.getmodule(getattr(type(dlg), loader))
            before = len(opened)
            with patch.object(module, "ResourceFormDialog", Recording):
                dlg.btn_add.click()
            self.assertEqual(len(opened), before + 1, f"{loader}: no dialog opened")
            form = opened[-1]
            seen[label] = form._button_box.button(ok).text()
            self.assertEqual(
                form.windowTitle(), label
            )  # the dialog is named as the button
        self.assertEqual(without_add, ["_load_server"])
        self.assertEqual(len(seen), len(type(dlg).TABS) - len(without_add), seen)
        self.assertEqual(
            set(seen.values()), {"Create", "Publish", "Upload"}, seen
        )  # never the generic Save on a create


class TestEmptyStates(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = object()  # connected: the empty state is about the tab
        self.dlg._setup_table(["Name", self.dlg.actions_column_label()])

    def test_an_empty_tab_points_at_its_add_button(self):
        self.dlg._setup_add_button("Add a Workspace", "tip", lambda: None)
        self.dlg._populate_rows([])
        self.assertEqual(
            self.dlg.lbl_page_info.text(),
            "Nothing here yet. Start with 'Add a Workspace' above.",
        )

    def test_a_fruitless_filter_blames_the_filter(self):
        self.dlg._setup_add_button("Add a Workspace", "tip", lambda: None)
        self.dlg._populate_rows([["topp"], ["sf"]])
        self.dlg.searchBox.setText("zzz")
        self.dlg._apply_filter()
        self.assertEqual(
            self.dlg.lbl_page_info.text(),
            "Nothing matches 'zzz'. Esc clears the filter.",
        )

    def test_without_an_add_button_it_stays_plain(self):
        self.dlg.btn_add.setVisible(False)
        self.dlg._populate_rows([])
        self.assertEqual(self.dlg.lbl_page_info.text(), "No results")

    def test_the_table_says_not_connected_instead_of_no_results(self):
        self.dlg.gs = None
        self.dlg._reset_table_state()
        self.assertIn("Not connected", self.dlg.lbl_page_info.text())
        self.dlg._on_nav_changed(0)
        self.assertIn("Not connected", self.dlg.lbl_page_info.text())


class TestCountsTakePluralForms(unittest.TestCase):
    """Counts like "out of 1 items" and "1 of 1 tiles" had no plural forms."""

    def test_the_line_under_the_table(self):
        dlg = SyncDialog()
        dlg.gs = object()
        dlg._setup_table(["Name", dlg.actions_column_label()])
        with english_plurals():
            dlg._populate_rows([["topp"]])
            self.assertEqual(dlg.lbl_page_info.text(), "Results 1 to 1 (out of 1 item)")
            dlg._populate_rows([[f"ws{index}"] for index in range(25)])
            self.assertEqual(
                dlg.lbl_page_info.text(), "Results 1 to 20 (out of 25 items)"
            )

    def test_a_seed_task(self):
        with english_plurals():
            one = GwcTabMixin._seed_tasks_text({"long-array-array": [[1, 1, 0, 7, 1]]})
            many = GwcTabMixin._seed_tasks_text(
                {"long-array-array": [[3, 12, 0, 7, 1]]}
            )
        self.assertEqual(one, "Task 7: running, 1 of 1 tile")
        self.assertEqual(many, "Task 7: running, 3 of 12 tiles")

    def test_a_world_seed_beyond_a_c_int_still_reads(self):
        # Qt's count is a C int: 5 * 2**31 tiles raised OverflowError.
        total = 5 * 2**31
        with english_plurals():
            text = GwcTabMixin._seed_tasks_text(
                {"long-array-array": [[3, total, 0, 7, 1]]}
            )
        self.assertEqual(text, f"Task 7: running, 3 of {total} tiles")


class TestWindowTitleAndEnter(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.show_success_message = lambda text: None
        self.dlg.show_error_message = lambda text: None
        self.dlg.show_warning_message = lambda text: None

    def test_the_title_names_the_server_while_connected(self):
        class FakeGS:
            def get_workspaces(inner):
                return ([], 200)

        self.dlg.plg_settings = FakePrefs("https://maps.example.org:8443/geoserver")
        self.dlg._build_client = lambda settings: FakeGS()
        self.dlg._probe = lambda gs, url: None
        self.dlg._fetch_version_label = lambda gs: ""
        self.dlg.refresh_ui()  # SyncDialog runs the probe inline
        self.assertEqual(
            self.dlg.windowTitle(), "GeoServer Manager: maps.example.org:8443"
        )

        self.dlg._probe = lambda gs, url: ("Server unreachable", "gone")
        self.dlg.refresh_ui()
        self.assertEqual(self.dlg.windowTitle(), "GeoServer Manager")

    def test_enter_opens_the_selected_row(self):
        opened = []
        self.dlg.gs = object()
        self.dlg._setup_table(["Name", self.dlg.actions_column_label()])
        self.dlg._name_click_callback = opened.append
        self.dlg._populate_rows([["topp"], ["sf"]])
        self.dlg.show()
        QApplication.setActiveWindow(self.dlg)
        self.dlg.resultsTable.selectRow(1)
        self.dlg.resultsTable.setFocus()
        QApplication.processEvents()

        QTest.keyClick(self.dlg.resultsTable, Qt.Key.Key_Return)
        self.assertEqual(opened, [["sf"]])
        self.dlg.close()

    def test_enter_with_nothing_or_several_selected_does_nothing(self):
        opened = []
        self.dlg.gs = object()
        self.dlg._setup_table(["Name", self.dlg.actions_column_label()])
        self.dlg._name_click_callback = opened.append
        self.dlg._populate_rows([["topp"], ["sf"]])
        self.dlg.show()
        QApplication.setActiveWindow(self.dlg)
        self.dlg.resultsTable.clearSelection()
        self.dlg.resultsTable.setFocus()
        QApplication.processEvents()
        QTest.keyClick(self.dlg.resultsTable, Qt.Key.Key_Return)
        self.assertEqual(opened, [])
        self.dlg.close()


class TestSharedWorkspaceLink(unittest.TestCase):
    """One helper on the dialog serves every tab's Workspace column."""

    def test_a_workspace_opens_and_the_global_label_does_not(self):
        from geoserver_manager.gui.scope import GLOBAL, scope

        dlg = SyncDialog()
        opened = []
        dlg._show_workspace_info = opened.append
        dlg._open_workspace_from_row(["a_style", "topp"])
        dlg._open_workspace_from_row(["a_style", GLOBAL])
        dlg._open_workspace_from_row(["a_style", ""])
        self.assertEqual(opened, [["topp"]])
        self.assertIsNone(scope(GLOBAL))
        self.assertIsNone(scope(""))
        self.assertEqual(scope("topp"), "topp")

    def test_no_tab_keeps_a_private_copy(self):
        from pathlib import Path

        gui = Path(__file__).parents[2] / "geoserver_manager" / "gui"
        for path in gui.glob("tab_*.py"):
            self.assertNotIn("def _open_workspace_from", path.read_text(), path.name)


class TestSortableColumns(unittest.TestCase):
    """A header click sorts the row cache itself, so every index-based lookup
    (selection, Enter, Delete) sees the order on screen; Qt's own sorting stays
    off (invariant 2 in docs/development/invariants.md)."""

    ROWS = [["b", "topp"], ["a", None], ["c", "nurc"]]

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg._row_actions = [("delete", "Delete", lambda row: None)]
        self.columns = ["Name", "Workspace", self.dlg.actions_column_label()]
        self.dlg._setup_table(self.columns)
        self.dlg._populate_rows([list(row) for row in self.ROWS])
        self.table = self.dlg.resultsTable
        self.header = self.table.horizontalHeader()

    def click(self, column):
        self.header.sectionClicked.emit(column)

    def names(self):
        return [self.table.item(row, 0).text() for row in range(self.table.rowCount())]

    def test_a_click_sorts_a_second_reverses_another_column_starts_ascending(self):
        self.assertEqual(self.names(), ["b", "a", "c"])
        self.click(0)
        self.assertEqual(self.names(), ["a", "b", "c"])
        self.click(0)
        self.assertEqual(self.names(), ["c", "b", "a"])
        self.click(1)  # an empty cell sorts first, as ""
        self.assertEqual(self.names(), ["a", "c", "b"])

    def test_the_arrow_shows_the_column_and_the_direction(self):
        self.assertFalse(self.header.isSortIndicatorShown())
        self.click(1)
        self.click(1)
        self.assertTrue(self.header.isSortIndicatorShown())
        self.assertEqual(self.header.sortIndicatorSection(), 1)
        self.assertEqual(self.header.sortIndicatorOrder(), Qt.SortOrder.DescendingOrder)

    def test_delete_selected_and_enter_see_the_highlighted_row(self):
        self.click(0)
        self.table.selectRow(0)
        self.assertEqual(self.dlg._get_selected_rows(), [["a", None]])

    def test_qt_never_sorts_the_items_itself(self):
        self.click(0)
        self.assertFalse(self.table.isSortingEnabled())

    def test_the_actions_header_is_not_a_sort_key(self):
        self.click(1)
        self.click(2)
        self.assertEqual(self.dlg._sort, (1, False))
        self.assertEqual(self.header.sortIndicatorSection(), 1)

    def test_the_sort_survives_a_reload_and_not_a_tab_change(self):
        self.click(0)
        self.dlg._setup_table(self.columns)
        self.dlg._populate_rows([list(row) for row in self.ROWS])
        self.assertEqual(self.names(), ["a", "b", "c"])
        other_tab = ["Style Name", "Workspace", self.dlg.actions_column_label()]
        self.dlg._setup_table(other_tab)
        self.dlg._populate_rows([list(row) for row in self.ROWS])
        self.assertEqual(self.names(), ["b", "a", "c"])
        self.assertFalse(self.header.isSortIndicatorShown())

    def test_sorting_goes_back_to_the_first_page(self):
        self.dlg._populate_rows([[f"r{index:02d}", ""] for index in range(25)])
        self.dlg._page_next()
        self.assertEqual(self.dlg._current_page, 1)
        self.click(0)
        self.assertEqual(self.dlg._current_page, 0)


class TestNavTooltips(unittest.TestCase):
    def test_every_tab_explains_itself_on_hover(self):
        dlg = SyncDialog()
        labels = [label for label, _icon, _loader in dlg.TABS]
        self.assertEqual(set(dlg._tab_help()), set(labels))
        for row in range(dlg.navList.count()):
            item = dlg.navList.item(row)
            self.assertTrue(item.toolTip(), item.text())


class TestRowActionTooltips(unittest.TestCase):
    """A fourth element in a row action is its tooltip; without one, the label."""

    def test_tooltip_defaults_to_the_label_and_can_say_more(self):
        from qgis.PyQt.QtWidgets import QMenu

        dlg = SyncDialog()
        dlg._row_actions = [
            ("delete", "Delete", lambda row: None),
            (
                "preview-browser",
                "Preview",
                lambda row: None,
                "Preview: the browser may ask",
            ),
        ]
        widget = dlg._make_action_widget(["row"])
        actions = widget.findChild(QMenu).actions()
        self.assertEqual(
            [action.toolTip() for action in actions if not action.isSeparator()],
            ["Preview: the browser may ask", "Delete"],
        )


class TestNarrowWindow(unittest.TestCase):
    def test_the_navigation_is_never_narrower_than_its_entries(self):
        # At the window's minimum width the splitter gave the list 140 px,
        # less than "Coverage Stores", and it grew a horizontal scroll bar.
        dlg = SyncDialog()
        dlg.resize(dlg.minimumSize())
        dlg.show()
        QApplication.processEvents()
        self.addCleanup(dlg.close)
        self.assertFalse(dlg.navList.horizontalScrollBar().isVisible())


class TestEnterInTheSearchBox(unittest.TestCase):
    def test_it_selects_the_first_match_at_once_not_the_add_form(self):
        # btn_add was the dialog's default button: Enter opened "Add a Workspace".
        # Pressed within the search delay, Enter selected the unfiltered first row.
        dlg = SyncDialog()
        opened = []
        dlg._setup_add_button("Add", "", lambda: opened.append(True))
        dlg._setup_table(["Name"])
        dlg._populate_rows([["topp"], ["sf"]])
        dlg.show()
        self.addCleanup(dlg.close)
        dlg.searchBox.setFocus()
        QTest.keyClicks(dlg.searchBox, "sf")
        QTest.keyClick(dlg.searchBox, Qt.Key.Key_Return)
        self.assertEqual(dlg._get_selected_rows(), [["sf"]])
        QApplication.processEvents()
        self.assertEqual(opened, [])
        self.assertIs(dlg.focusWidget(), dlg.resultsTable)


if __name__ == "__main__":
    unittest.main()
