#! python3  # noqa E265
"""ensure_dependencies on a Python the bundled library cannot run on."""

from unittest.mock import patch

import requests
from qgis.testing import start_app, unittest

from geoserver_manager.toolbelt import dependencies

start_app()


class TestOldPython(unittest.TestCase):
    def test_python_3_9_says_so_instead_of_suggesting_pip(self):
        """geoservercloud does not import on 3.9, so pip install cannot help."""
        shown = []
        old = (3, 9, 18, "final", 0)
        with patch.object(dependencies.sys, "version_info", old):
            with patch.object(
                dependencies.QMessageBox,
                "critical",
                lambda parent, title, text: shown.append(text),
            ):
                with patch.object(
                    dependencies,
                    "_try_import",
                    lambda logger=None: self.fail("tried to import"),
                ):
                    self.assertFalse(dependencies.ensure_dependencies())
        self.assertEqual(len(shown), 1)
        self.assertIn("3.10", shown[0])
        self.assertIn("Python 3.9", shown[0])
        self.assertNotIn("pip", shown[0])


class TestRequestsWithSimplejson(unittest.TestCase):
    def test_a_text_answer_keeps_its_status(self):
        """With simplejson installed, requests' JSONDecodeError is simplejson's,
        not json's, and the library's `except` missed it: a 404 raised."""
        from geoservercloud.services.restservice import RestService

        class LikeSimplejsons(requests.exceptions.InvalidJSONError, ValueError):
            pass

        def refuse(response, **kwargs):
            raise LikeSimplejsons("Expecting value")

        response = requests.Response()
        response.status_code = 404
        response._content = b"No such workspace: 'x' found"
        with patch.object(requests.exceptions, "JSONDecodeError", LikeSimplejsons):
            with patch.object(requests.Response, "json", refuse):
                dependencies.widen_json_errors()
                self.assertEqual(
                    RestService.deserialize_response(response, None),
                    ("No such workspace: 'x' found", 404),
                )
        dependencies.widen_json_errors()  # back to the real class


if __name__ == "__main__":
    unittest.main()
