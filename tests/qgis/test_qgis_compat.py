#! python3  # noqa E265

"""
The QGIS running the suite accepts the plugin's version range. CI runs it
under QGIS 3.40 and QGIS 4, where the range in metadata.txt is the only gate:
QGIS 4 ignores supportsQt6.

Usage from the repo root folder:

.. code-block:: bash

    QT_QPA_PLATFORM=offscreen python -m unittest tests.qgis.test_qgis_compat
"""

import importlib.util
from pathlib import Path

from qgis.core import QgsApplication
from qgis.testing import start_app, unittest

from geoserver_manager import __about__

start_app()


class TestThisQgisAcceptsThePlugin(unittest.TestCase):
    def test_the_plugin_manager_calls_this_qgis_compatible(self):
        """QGIS 4 disabled the plugin as incompatible while the ceiling was 3.99."""
        # the plugin manager's check alone: importing its package made the suite crash
        folder = Path(QgsApplication.pkgDataPath(), "python", "pyplugin_installer")
        spec = importlib.util.spec_from_file_location(
            "version_compare", folder / "version_compare.py"
        )
        check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check)

        general = __about__.__plugin_md__["general"]
        low = general["qgisminimumversion"]
        high = general["qgismaximumversion"]
        self.assertTrue(
            check.isCompatible(check.pyQgisVersion(), low, high),
            f"QGIS {check.pyQgisVersion()} is outside {low} to {high}",
        )


if __name__ == "__main__":
    unittest.main()
