"""Every plugin icon must be registered, reviewable and correctly packaged."""

import copy
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from geoserver_manager.toolbelt.icon_catalog import RESOURCES, icon_spec, load_catalog
from scripts.build_icon_catalog import (
    main,
    render_gallery,
    scan_usage,
    validate_catalog,
)


class TestIconCatalogue(unittest.TestCase):
    def test_all_plugin_icons_are_registered_and_have_valid_artwork(self):
        usages, problems = scan_usage()
        self.assertTrue(usages)
        self.assertEqual(problems + validate_catalog(load_catalog(), usages), [])

    def test_check_needs_no_preview_and_generation_is_optional(self):
        with tempfile.TemporaryDirectory() as folder:
            preview = Path(folder) / "icon-catalog.html"
            with patch("scripts.build_icon_catalog.GALLERY", preview):
                with patch("sys.argv", ["build_icon_catalog.py", "--check"]):
                    with redirect_stdout(io.StringIO()):
                        main()
                self.assertFalse(preview.exists())
                with patch("sys.argv", ["build_icon_catalog.py"]):
                    with redirect_stdout(io.StringIO()):
                        main()
            self.assertEqual(list(Path(folder).iterdir()), [preview])
            page = preview.read_text()
            self.assertIn("<svg", page)
            self.assertEqual(page.count("<article "), len(load_catalog()["icons"]))

    def test_unknown_icon_cannot_silently_fall_back(self):
        with self.assertRaisesRegex(ValueError, "Unregistered icon"):
            icon_spec("unregistered-feature")
        problems = validate_catalog(
            load_catalog(), {"unregistered-feature": {"new.py"}}
        )
        self.assertTrue(any("Unregistered icon" in problem for problem in problems))

    def test_direct_qgis_icons_cannot_bypass_the_inventory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plugin = root / "geoserver_manager"
            plugin.mkdir()
            (plugin / "new_feature.py").write_text(
                'icon = QgsApplication.getThemeIcon("mActionHelpContents.svg")\n'
            )
            _, problems = scan_usage(root)
            self.assertTrue(
                any("use a catalogued icon()" in problem for problem in problems)
            )

    def test_new_svg_must_be_added_to_the_catalogue(self):
        catalog = copy.deepcopy(load_catalog())
        catalog["icons"] = {"workspaces": catalog["icons"]["workspaces"]}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            icons = root / "geoserver_manager/resources/icons"
            icons.mkdir(parents=True)
            source = (RESOURCES / "icons/workspaces.svg").read_text()
            (icons / "workspaces.svg").write_text(source)
            (icons / "forgotten.svg").write_text(source)
            problems = validate_catalog(catalog, {}, root)
            self.assertEqual(problems, ["Uncatalogued SVG: icons/forgotten.svg"])

    def test_pending_artwork_has_a_declared_fallback_and_visible_status(self):
        catalog = copy.deepcopy(load_catalog())
        catalog["icons"]["new-feature"] = {
            "label": "New feature",
            "category": "Utilities",
            "purpose": "A future feature whose symbol is not ready.",
            "status": "needs-custom",
            "fallback": "mActionHelpContents.svg",
            "notes": "Replace the temporary help symbol with a map and a clock.",
        }
        self.assertEqual(validate_catalog(catalog, {}), [])
        self.assertIn('data-status="needs-custom"', render_gallery(catalog, {}))
        del catalog["icons"]["new-feature"]["notes"]
        self.assertTrue(validate_catalog(catalog, {}))
