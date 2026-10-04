#! python3  # noqa: E265

"""A styled sidebar can keep a different palette from the surrounding dialog."""

from unittest.mock import patch

from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor, QIcon, QPalette
from qgis.PyQt.QtWidgets import QApplication, QPushButton
from qgis.testing import start_app, unittest

from geoserver_manager.gui.dlg_main import GeoServerMainDialog

start_app()


class TestIconPalette(unittest.TestCase):
    def test_theme_change_uses_widget_palettes_and_preserves_the_table(self):
        dialog = GeoServerMainDialog()
        self.addCleanup(dialog.close)
        dialog._row_actions = [("preview-map", "Preview", lambda row: None)]
        dialog._setup_table(["Name", dialog.actions_column_label()])
        dialog._populate_rows([["roads"], ["rivers"]])
        dialog.resultsTable.selectRow(1)
        button = dialog.resultsTable.cellWidget(0, 1).findChild(QPushButton)

        # Stylesheets or a platform style can keep the sidebar light while
        # the enclosing dialog changes to a dark theme.
        sidebar = QPalette(dialog.navList.palette())
        sidebar.setColor(QPalette.ColorRole.Window, QColor("#f0f0f0"))
        sidebar.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
        sidebar.setColor(QPalette.ColorRole.Text, QColor("#182733"))
        dialog.navList.setPalette(sidebar)
        dark = QPalette(dialog.palette())
        dark.setColor(QPalette.ColorRole.Window, QColor("#232629"))
        dark.setColor(QPalette.ColorRole.Base, QColor("#1b1e20"))
        dark.setColor(QPalette.ColorRole.Text, QColor("#eff0f1"))

        with patch.object(dialog, "_start_load") as fetch:
            dialog.setPalette(dark)
            QApplication.processEvents()
            fetch.assert_not_called()

        self.assertEqual(dialog._get_selected_rows(), [["rivers"]])
        self.assertIs(
            button, dialog.resultsTable.cellWidget(0, 1).findChild(QPushButton)
        )
        for rendered, palette in (
            (dialog.navList.item(0).icon(), dialog.navList.palette()),
            (button.icon(), button.palette()),
        ):
            image = rendered.pixmap(QSize(20, 20), QIcon.Mode.Normal).toImage()
            expected = palette.color(QPalette.ColorRole.Text)
            # Compare visible stroke pixels. Antialiasing can round each
            # channel, so allow a small difference in the unpremultiplied RGB.
            matches = 0
            for x in range(image.width()):
                for y in range(image.height()):
                    pixel = image.pixelColor(x, y)
                    distance = sum(
                        abs(a - b)
                        for a, b in zip(pixel.getRgb()[:3], expected.getRgb()[:3])
                    )
                    matches += pixel.alpha() > 128 and distance < 10
            self.assertGreater(
                matches, 5, "The icon must use its own widget's text colour"
            )

    def test_selected_row_actions_use_the_selection_text_colour(self):
        dialog = GeoServerMainDialog()
        self.addCleanup(dialog.close)
        dialog._row_actions = [("add-to-qgis", "Add to QGIS", lambda row: None)]
        dialog._setup_table(["Name", dialog.actions_column_label()])
        dialog._populate_rows([["roads"]])
        button = dialog.resultsTable.cellWidget(0, 1).findChild(QPushButton)
        normal = button.icon().pixmap(QSize(20, 20)).toImage()

        dialog.resultsTable.selectRow(0)
        selected = button.icon().pixmap(QSize(20, 20)).toImage()
        expected = button.palette().color(QPalette.ColorRole.HighlightedText)
        self.assertNotEqual(normal, selected)
        for x in range(selected.width()):
            for y in range(selected.height()):
                pixel = selected.pixelColor(x, y)
                if pixel.alpha() > 128:
                    for actual, target in zip(
                        pixel.getRgb()[:3], expected.getRgb()[:3]
                    ):
                        self.assertLessEqual(abs(actual - target), 3)
        dialog.resultsTable.clearSelection()
        self.assertEqual(normal, button.icon().pixmap(QSize(20, 20)).toImage())
