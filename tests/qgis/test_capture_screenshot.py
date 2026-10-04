#! python3  # noqa E265

"""
The documentation's screenshots, as scripts/capture_screenshot.py takes them.

Usage from the repo root folder:

.. code-block:: bash

    QT_QPA_PLATFORM=offscreen python -m unittest tests.qgis.test_capture_screenshot
"""

import importlib.util
import shutil
import tempfile
from pathlib import Path

from qgis.PyQt.QtCore import QCoreApplication
from qgis.testing import start_app, unittest

from tests.qgis.sync_dialog import SyncDialog

start_app()

SCRIPT = Path(__file__).parents[2] / "scripts" / "capture_screenshot.py"


def capture_script():
    spec = importlib.util.spec_from_file_location("capture_screenshot", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(shutil.which("lrelease"), "the script compiles with lrelease")
class TestAShotReadsACountAsQgisDoes(unittest.TestCase):
    """Every table's line once read "(out of 8 item(s))"."""

    def test_the_shots_dialog_finds_the_english_plural_forms(self):
        with tempfile.TemporaryDirectory() as folder:
            translator = capture_script().english_translator(folder)
            QCoreApplication.installTranslator(translator)
            try:
                # The script's dialog is a SyncDialog, whose class is tr()'s context.
                text = SyncDialog().tr(
                    "%n item(s) could not be listed: {names}. Details in the QGIS "
                    "log (GeoServer Manager tab).",
                    None,
                    2,
                )
            finally:
                QCoreApplication.removeTranslator(translator)
        self.assertTrue(text.startswith("2 items could not be listed"), text)
