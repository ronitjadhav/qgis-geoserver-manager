#! python3  # noqa E265

"""
The plugin's logger: what it keeps, and what it reads to decide.

Usage from the repo root folder:

.. code-block:: bash

    QT_QPA_PLATFORM=offscreen python -m unittest tests.qgis.test_log_handler
"""

from types import SimpleNamespace
from unittest.mock import patch

from qgis.core import Qgis
from qgis.testing import start_app, unittest

from geoserver_manager.toolbelt import log_handler, preferences
from geoserver_manager.toolbelt.log_handler import PlgLogger

start_app()


class TestPushDuration(unittest.TestCase):
    """A pushed warning once faded after 6 s, an error after 9 s."""

    @staticmethod
    def pushed(level):
        durations = []
        bar = SimpleNamespace(pushMessage=lambda **kw: durations.append(kw["duration"]))
        fake_iface = SimpleNamespace(messageBar=lambda: bar)
        with patch.object(log_handler, "iface", fake_iface):
            PlgLogger.log("message", log_level=level, push=True)
        return durations[0]

    def test_warnings_and_errors_stay_until_closed(self):
        self.assertEqual(self.pushed(Qgis.MessageLevel.Warning), 0)
        self.assertEqual(self.pushed(Qgis.MessageLevel.Critical), 0)

    def test_good_news_still_fades(self):
        self.assertGreater(self.pushed(Qgis.MessageLevel.Info), 0)
        self.assertGreater(self.pushed(Qgis.MessageLevel.Success), 0)


class TestLogGate(unittest.TestCase):
    def test_a_warning_is_kept_without_reading_the_settings(self):
        # Every call read the whole settings block (QgsSettings and the
        # environment) to learn debug_mode, even for a message kept anyway.
        reads = []

        def read_settings():
            reads.append(1)
            return preferences.PlgSettingsStructure()

        with patch.object(
            preferences.PlgOptionsManager, "get_plg_settings", read_settings
        ):
            PlgLogger.log("kept", log_level=Qgis.MessageLevel.Warning)
            PlgLogger.log("kept", log_level=Qgis.MessageLevel.Critical)
            self.assertEqual(reads, [])
            PlgLogger.log(
                "dropped without debug mode", log_level=Qgis.MessageLevel.Info
            )
            self.assertEqual(reads, [1])

    def test_a_level_passed_by_position_is_refused(self):
        # It landed in the application slot and the message stayed at Info:
        # dropped without debug mode, a TypeError from QGIS with it.
        with self.assertRaises(TypeError):
            PlgLogger.log("lost", Qgis.MessageLevel.Critical)


if __name__ == "__main__":
    unittest.main()
