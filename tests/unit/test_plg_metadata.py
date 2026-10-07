#! python3  # noqa E265

"""
Usage from the repo root folder:

.. code-block:: bash
    # for whole tests
    python -m unittest tests.unit.test_plg_metadata
    # for specific test
    python -m unittest tests.unit.test_plg_metadata.TestPluginMetadata.test_the_metadata_is_found_and_its_versions_parse
"""

# standard library
import unittest

# 3rd party
from packaging.version import parse

# project
from geoserver_manager import __about__

# ############################################################################
# ########## Classes #############
# ################################


class TestPluginMetadata(unittest.TestCase):
    """Test about module"""

    def test_the_metadata_is_found_and_its_versions_parse(self):
        self.assertTrue(__about__.PLG_METADATA_FILE.is_file())
        self.assertTrue(__about__.DIR_PLUGIN_ROOT.is_dir())
        self.assertLessEqual(len(__about__.__title_clean__), len(__about__.__title__))
        parse(__about__.__version__)  # raises InvalidVersion if it is not one

        min_version_parsed = parse(
            __about__.__plugin_md__.get("general").get("qgisminimumversion")
        )
        max_version_parsed = parse(
            __about__.__plugin_md__.get("general").get("qgismaximumversion")
        )
        self.assertLessEqual(min_version_parsed, max_version_parsed)

    def test_every_qt6_qgis_may_load_the_plugin(self):
        """Qt6 builds of 3.40 to 3.44 read supportsQt6, QGIS 4 only the range.

        QGIS 4 ignores supportsQt6, so a 3.99 ceiling disabled the plugin there.
        """
        general = __about__.__plugin_md__.get("general")
        self.assertEqual(general.get("supportsqt6"), "True")
        self.assertGreaterEqual(parse(general.get("qgismaximumversion")), parse("4.0"))

    def test_no_nameless_plugin_dependency(self):
        """QGIS splits plugin_dependencies on commas and keeps blanks.

        An empty value was a dependency named "": installing the zip opened
        the Plugin Dependencies Manager with an empty row to "Fix manually".
        """
        deps = __about__.__plugin_md__.get("general").get("plugin_dependencies")
        self.assertTrue(deps is None or all(dep.strip() for dep in deps.split(",")))


# ############################################################################
# ####### Stand-alone run ########
# ################################
if __name__ == "__main__":
    unittest.main()
