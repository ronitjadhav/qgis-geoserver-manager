"""Compact row actions retain their targets, connection guards and keyboard access."""

from unittest.mock import patch

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor, QPalette
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QApplication, QMenu, QPushButton
from qgis.testing import start_app, unittest

from tests.qgis.sync_dialog import SyncDialog

start_app()


class TestRowActions(unittest.TestCase):
    def setUp(self):
        self.dialog = SyncDialog()
        self.addCleanup(self.dialog.close)
        self.dialog.gs = object()
        self.called = []
        self.dialog._row_actions = [
            (name, label, lambda row, name=name: self.called.append((name, row)))
            for name, label in (
                ("add-to-qgis", "Add to QGIS"),
                ("preview-map", "Preview"),
                ("preview-browser", "Preview in a browser"),
                ("styles", "Set style"),
                ("push-style", "Push style from QGIS"),
                ("delete", "Delete"),
            )
        ]
        self.dialog._setup_table(["Name", self.dialog.actions_column_label()])
        self.dialog._populate_rows([["roads"], ["rivers"]])

    def widget(self, row=0):
        return self.dialog.resultsTable.cellWidget(row, 1)

    def menu(self, row=0):
        menu = self.widget(row).findChild(QMenu)
        self.assertIsNotNone(menu, "Secondary actions need a labelled menu")
        return menu

    def test_frequent_actions_stay_visible_and_delete_is_separated(self):
        buttons = self.widget().findChildren(QPushButton)
        self.assertEqual(len(buttons), 3)
        self.assertEqual(
            [button.property("resourceIcon") for button in buttons[:2]],
            ["add-to-qgis", "preview-map"],
        )
        self.assertEqual(buttons[2].text(), "More")
        actions = self.menu().actions()
        self.assertEqual(
            [action.text() for action in actions if not action.isSeparator()],
            ["Preview in a browser", "Set style", "Push style from QGIS", "Delete"],
        )
        self.assertTrue(actions[-2].isSeparator())
        for button in buttons:
            self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.StrongFocus)
            self.assertFalse(button.autoDefault())
            self.assertTrue(button.accessibleName())

    def test_menu_action_targets_its_row_after_sort_filter_and_pagination(self):
        self.dialog._populate_rows([[f"road-{i:02d}"] for i in range(25)])
        self.dialog._on_header_clicked(0)
        self.dialog._on_header_clicked(0)
        self.dialog._page_next()
        expected = self.dialog._filtered_rows[20]
        self.menu().actions()[-1].trigger()
        self.assertEqual(self.called, [("delete", expected)])
        self.dialog.searchBox.setText("road-17")
        self.dialog._apply_filter()
        self.menu().actions()[1].trigger()
        self.assertEqual(self.called[-1], ("styles", ["road-17"]))

    def test_menu_action_rechecks_connection_at_activation(self):
        action = self.menu().actions()[-1]
        self.dialog.gs = None
        with patch.object(self.dialog, "show_warning_message") as warning:
            action.trigger()
        self.assertEqual(self.called, [])
        warning.assert_called_once()

    def test_delete_menu_still_reaches_the_confirmation(self):
        self.dialog._row_actions = [("delete", "Delete", self.dialog._delete_workspace)]
        self.dialog._populate_rows([["roads"]])
        with patch.object(
            self.dialog, "_confirm_delete", return_value=False
        ) as confirm:
            self.menu().actions()[0].trigger()
        confirm.assert_called_once()

    def test_menu_only_rows_are_labelled_and_quick_actions_do_not_steal_selection(self):
        self.dialog.resultsTable.selectRow(1)
        self.widget().findChildren(QPushButton)[0].click()
        self.assertEqual(self.called, [("add-to-qgis", ["roads"])])
        self.assertEqual(self.dialog._get_selected_rows(), [["rivers"]])
        self.dialog._row_actions = [("delete", "Delete", lambda row: None)]
        self.dialog._populate_rows([["roads"]])
        buttons = self.widget().findChildren(QPushButton)
        self.assertEqual([button.text() for button in buttons], ["Actions"])
        self.assertFalse(self.menu().actions()[0].isSeparator())

    def test_enter_activates_focused_quick_action_without_closing_dialog(self):
        self.dialog.show()
        QApplication.setActiveWindow(self.dialog)
        button = self.widget().findChildren(QPushButton)[0]
        button.setFocus()
        QApplication.processEvents()
        QTest.keyClick(button, Qt.Key.Key_Return)
        self.assertEqual(self.called, [("add-to-qgis", ["roads"])])
        self.assertTrue(self.dialog.isVisible())

    def test_keyboard_opens_menu_and_escape_only_dismisses_the_menu(self):
        self.dialog.show()
        QApplication.setActiveWindow(self.dialog)
        button = self.widget().findChildren(QPushButton)[-1]
        button.setFocus()
        menu = self.menu()
        seen = []

        def dismiss():
            seen.append(menu.isVisible())
            QTest.keyClick(menu, Qt.Key.Key_Escape)

        QTimer.singleShot(50, dismiss)
        QTest.keyClick(button, Qt.Key.Key_Return)
        QTest.qWait(60)
        self.assertEqual(seen, [True])
        self.assertFalse(menu.isVisible())
        self.assertTrue(self.dialog.isVisible())
        self.assertEqual(self.called, [])

    def test_all_tabs_keep_at_most_two_quick_actions_and_label_secondary_actions(self):
        for _label, _icon, loader in self.dialog.TABS:
            with self.subTest(tab=_label), patch.object(self.dialog, "_start_load"):
                getattr(self.dialog, loader)()
                widget = self.dialog._make_action_widget(["resource"])
                buttons = widget.findChildren(QPushButton)
                quick = [b for b in buttons if b.property("resourceIcon")]
                self.assertLessEqual(len(quick), 2)
                self.assertTrue(widget.findChild(QMenu))
                self.assertTrue(
                    all(b.property("resourceIcon") != "delete" for b in quick)
                )

    def test_menu_and_button_text_follow_theme_changes_and_deselection(self):
        self.dialog.show()
        QApplication.processEvents()
        dark = QPalette(self.dialog.palette())
        dark.setColor(QPalette.ColorRole.Window, QColor("#232629"))
        dark.setColor(QPalette.ColorRole.Base, QColor("#1b1e20"))
        dark.setColor(QPalette.ColorRole.Text, QColor("#eff0f1"))
        dark.setColor(QPalette.ColorRole.ButtonText, QColor("#eff0f1"))
        self.dialog.setPalette(dark)
        QApplication.processEvents()
        button = self.widget().findChildren(QPushButton)[-1]
        self.dialog.resultsTable.selectRow(0)
        self.dialog.resultsTable.clearSelection()
        QApplication.processEvents()
        self.assertEqual(
            button.palette().color(QPalette.ColorRole.ButtonText), QColor("#eff0f1")
        )
        self.assertEqual(
            self.menu().palette().color(QPalette.ColorRole.Text), QColor("#eff0f1")
        )
        self.assertEqual(
            self.menu().palette().color(QPalette.ColorRole.Window), QColor("#232629")
        )


class TestActionsColumnWidth(unittest.TestCase):
    def test_the_column_fits_buttons_a_theme_makes_wider(self):
        # A regression guard: in QGIS the buttons were clipped to the header's
        # width. Offscreen that never reproduced, so this passes on the old
        # code too; it pins the fix's contract, not the original trigger.
        dialog = SyncDialog()
        self.addCleanup(dialog.close)
        dialog.setStyleSheet("QPushButton { font-size: 30px; }")
        dialog._row_actions = [("delete", "Delete", lambda row: None)]
        dialog._setup_table(["Name", dialog.actions_column_label()])
        dialog._populate_rows([["roads"], ["rivers"]])
        dialog.show()
        QApplication.processEvents()
        table = dialog.resultsTable
        self.assertGreaterEqual(
            table.horizontalHeader().sectionSize(1),
            table.cellWidget(0, 1).sizeHint().width(),
        )
