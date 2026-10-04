#! python3  # noqa E265

"""
The naming rules a published layer has to obey, checked without QGIS.

Usage from the repo root folder:

.. code-block:: bash

    python -m unittest tests.unit.test_qgis_export
"""

import unittest

from geoserver_manager.toolbelt.qgis_export import geoserver_name


class TestGeoserverName(unittest.TestCase):
    """A QGIS layer name is whatever the user typed; a WFS type name is not."""

    def test_a_name_that_is_already_safe_is_left_alone(self):
        for name in ("tasmania_roads", "roads2024", "a.b-c", "_private", "_2024"):
            self.assertEqual(geoserver_name(name), name)

    def test_spaces_and_punctuation_become_single_underscores(self):
        self.assertEqual(geoserver_name("Roads (2024)"), "Roads_2024")
        self.assertEqual(geoserver_name("  spaced  out "), "spaced_out")
        self.assertEqual(geoserver_name("a/b\\c:d"), "a_b_c_d")

    def test_accents_are_folded_rather_than_replaced(self):
        # "Riviere" reads; "Rivi_re" does not.
        self.assertEqual(geoserver_name("Rivière Noire"), "Riviere_Noire")
        self.assertEqual(geoserver_name("ÄÖÜ layer"), "AOU_layer")
        self.assertEqual(geoserver_name("Žluťoučký"), "Zlutoucky")

    def test_a_leading_digit_gets_an_underscore_not_a_haircut(self):
        """An NCName may start with "_", so no character has to be lost."""
        self.assertEqual(geoserver_name("2024 data"), "_2024_data")
        # a leading dash or dot carries nothing, so it simply goes
        self.assertEqual(geoserver_name("-dash"), "dash")
        self.assertEqual(geoserver_name(".dot"), "dot")

    def test_a_name_with_nothing_usable_still_yields_a_name(self):
        self.assertEqual(geoserver_name(""), "layer")
        self.assertEqual(geoserver_name("___"), "layer")
        self.assertEqual(geoserver_name("你好"), "layer")
        self.assertEqual(geoserver_name(None), "layer")

    def test_the_result_is_always_usable(self):
        import re

        for raw in ("Roads (2024)", "Rivière", "2024", "", "你好", "a b/c", "___x"):
            name = geoserver_name(raw)
            self.assertRegex(name, r"^[A-Za-z_][A-Za-z0-9_.-]*$", f"from {raw!r}")
            self.assertNotIn("__", name)
            self.assertFalse(re.search(r"\s", name))


if __name__ == "__main__":
    unittest.main()
