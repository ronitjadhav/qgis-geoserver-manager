#! python3  # noqa E265

"""
The Server tab. Measured on 2.28.5: a service's settings merge a partial PUT,
but the global settings, the contact and the logging replace the stored
object, so those are sent whole.

Usage from the repo root folder:

.. code-block:: bash

    QT_QPA_PLATFORM=offscreen python -m unittest tests.qgis.test_tab_server
"""

import copy
import threading
from unittest.mock import patch

from qgis.PyQt.QtWidgets import QDialog, QPushButton
from qgis.testing import start_app, unittest

from geoserver_manager.gui import tab_server
from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog
from geoserver_manager.gui.tab_server import ServerTabMixin
from geoserver_manager.toolbelt.rest import Abandoned
from tests.qgis.sync_dialog import SyncDialog

start_app()

CONTACT = {
    "contactPerson": "Claudius Ptolomaeus",
    "contactOrganization": "OSGeo",
    "addressCity": "Alexandria",
    "welcome": "Hello",
}
GLOBAL = {
    "global": {
        "settings": {
            "id": "SettingsInfoImpl-1",
            "contact": CONTACT,
            "charset": "UTF-8",
            "numDecimals": 8,
            "verbose": False,
        },
        "jai": {"tileThreads": 7},
        "updateSequence": 42,
    }
}
WFS = {
    "wfs": {
        "enabled": True,
        "title": "GeoServer Web Feature Service",
        "abstrct": "The reference implementation",
        "keywords": {"string": ["WFS", "GEOSERVER"]},
        "maxFeatures": 1000000,
        "gml": {"entry": []},
    }
}
LOGGING = {
    "logging": {
        "level": "DEFAULT_LOGGING",
        "location": "logs/geoserver.log",
        "stdOutLogging": True,
    }
}


class Response:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return copy.deepcopy(self._payload)


class FakeGS:
    def __init__(self, broken=()):
        self.calls = []
        payloads = {
            "/rest/settings.json": GLOBAL,
            "/rest/settings/contact.json": {"contact": CONTACT},
            "/rest/logging.json": LOGGING,
            "/rest/services/wfs/settings.json": WFS,
            "/rest/services/wms/settings.json": {
                "wms": {"enabled": False, "title": "WMS"}
            },
            "/rest/services/wcs/settings.json": {"wcs": {"title": "WCS"}},
        }
        outer = self

        class Client:
            url, auth, verifytls = "http://gs", ("u", "p"), True

            def get(inner, path, **kwargs):
                outer.calls.append(("GET", path, kwargs))
                if any(path.endswith(f"/{name}/settings.json") for name in broken):
                    return Response("boom", 500)
                return Response(
                    payloads.get(path, {}), 200 if path in payloads else 404
                )

            def put(inner, path, **kwargs):
                outer.calls.append(("PUT", path, kwargs))
                return Response("")

            def post(inner, path, **kwargs):
                outer.calls.append(("POST", path, kwargs))
                return Response("")

        class Endpoints:
            base_url = "/rest"

        class Rest:
            rest_client = Client()
            rest_endpoints = Endpoints()

        self.rest_service = Rest()

    def puts(self):
        return [call for call in self.calls if call[0] == "PUT"]


class TestServerTab(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS(broken=("wmts",))
        self.warnings = []
        self.dlg.show_warning_message = self.warnings.append

    def test_one_row_per_section_and_an_unreadable_one_costs_one_warning(self):
        self.dlg._load_server()
        self.assertTrue(self.dlg.btn_delete_selected.isHidden())  # nothing to delete
        rows = {row[0]: row[1] for row in self.dlg._all_rows}
        self.assertEqual(
            list(rows),
            [
                "Contact",
                "Global settings",
                "WMS",
                "WFS",
                "WCS",
                "WMTS",
                "Logging",
                "Catalog",
            ],
        )
        self.assertEqual(rows["Contact"], "Claudius Ptolomaeus, OSGeo")
        self.assertEqual(rows["Global settings"], "No proxy base URL")
        self.assertEqual(rows["WMS"], "Off: WMS")
        self.assertEqual(rows["WFS"], "On: GeoServer Web Feature Service")
        self.assertEqual(rows["WMTS"], "-")
        self.assertEqual(rows["Logging"], "DEFAULT_LOGGING")
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("WMTS", self.warnings[0])


class TestServerSaves(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()

    def edit(self, kind, **changes):
        settings = self.dlg._server_read(kind)
        before = ServerTabMixin._server_form_values(kind, settings)
        after = dict(before, **changes)
        return self.dlg._save_server_section(kind, before, after)

    def test_a_service_sends_only_what_changed(self):
        self.assertTrue(self.edit("wfs", title="Features", keywords=["A", "B", ""]))
        ((_verb, path, kwargs),) = self.dlg.gs.puts()
        self.assertEqual(path, "/rest/services/wfs/settings.json")
        self.assertEqual(
            kwargs["json"],
            {"wfs": {"title": "Features", "keywords": {"string": ["A", "B"]}}},
        )

    def test_a_service_without_max_features_never_sends_one(self):
        self.edit("wms", title="Maps")
        self.assertNotIn("maxFeatures", self.dlg.gs.puts()[0][2]["json"]["wms"])

    def test_the_global_settings_go_back_whole_with_the_edit(self):
        # A PUT of the proxy URL alone wiped the contact and the charset.
        self.edit("global", proxy_base_url="https://maps.example.org/geoserver")
        ((_verb, path, kwargs),) = self.dlg.gs.puts()
        self.assertEqual(path, "/rest/settings.json")
        sent = kwargs["json"]["global"]
        self.assertEqual(
            sent["settings"]["proxyBaseUrl"], "https://maps.example.org/geoserver"
        )
        self.assertEqual(sent["settings"]["contact"], CONTACT)
        self.assertEqual(sent["settings"]["charset"], "UTF-8")
        self.assertEqual(sent["jai"], {"tileThreads": 7})

    def test_a_blank_proxy_url_unsets_it(self):
        self.dlg.gs = FakeGS()
        GLOBAL["global"]["settings"]["proxyBaseUrl"] = "https://old.example.org"
        try:
            self.edit("global", proxy_base_url="")
        finally:
            del GLOBAL["global"]["settings"]["proxyBaseUrl"]
        sent = self.dlg.gs.puts()[0][2]["json"]["global"]["settings"]
        self.assertIsNone(sent["proxyBaseUrl"])

    def test_the_contact_goes_back_whole(self):
        # A PUT of the person alone cleared the city.
        self.edit("contact", person="Hypatia")
        sent = self.dlg.gs.puts()[0][2]["json"]["contact"]
        self.assertEqual(sent["contactPerson"], "Hypatia")
        self.assertEqual(sent["addressCity"], "Alexandria")
        self.assertEqual(sent["welcome"], "Hello")

    def test_the_logging_goes_back_whole(self):
        # A PUT of the level alone switched standard-output logging off.
        self.edit("logging", level="VERBOSE_LOGGING")
        sent = self.dlg.gs.puts()[0][2]["json"]["logging"]
        self.assertEqual(sent["level"], "VERBOSE_LOGGING")
        self.assertIs(sent["stdOutLogging"], True)

    def test_nothing_changed_sends_nothing(self):
        for kind in ("wfs", "global", "contact", "logging"):
            self.assertFalse(self.edit(kind))
        self.assertEqual(self.dlg.gs.puts(), [])


def _cancel_writes(action, write=False, stop=None):
    """A _wait_for whose waiting box is cancelled on every write."""
    if write:
        raise Abandoned(write)
    return action()


class TestServerFormSaves(unittest.TestCase):
    """A save is a write: a Cancel on the waiting box says the PUT may still
    land and reloads the tab. Through _fetch, the read helper, it was silent
    and the Summary column kept the old value."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = FakeGS()
        self.warnings, self.successes = [], []
        self.dlg.show_warning_message = self.warnings.append
        self.dlg.show_success_message = self.successes.append

    def open_and_save(self, row, **changes):
        class Saving(ResourceFormDialog):
            def exec(inner):
                return QDialog.DialogCode.Accepted

            def get_values(inner):
                return dict(super().get_values(), **changes)

        with patch.object(tab_server, "ResourceFormDialog", Saving):
            self.dlg._show_server_section(row)

    def test_a_change_is_put_and_reported(self):
        self.open_and_save(["WFS", "-"], title="Features")
        ((_verb, path, _kwargs),) = self.dlg.gs.puts()
        self.assertEqual(path, "/rest/services/wfs/settings.json")
        self.assertEqual(self.successes, ["'WFS' saved."])

    def test_what_the_form_normalises_is_not_an_edit(self):
        # The form strips its text and gives CRLF lines back as LF; the
        # prefill kept both, so an untouched Save sent a PUT and said saved.
        stored = {
            "wms": {"enabled": True, "title": "Maps ", "abstrct": "One\r\ntwo"},
            "contact": {"contactPerson": " Hypatia", "welcome": "Hi\r\nthere\n"},
        }
        self.dlg._server_read = lambda kind: copy.deepcopy(stored[kind])
        self.open_and_save(["WMS", "-"])
        self.open_and_save(["Contact", "-"])
        self.assertEqual(self.dlg.gs.puts(), [])
        self.assertEqual(self.successes, [])

    def test_zero_decimals_is_shown_as_zero(self):
        values = ServerTabMixin._server_form_values("global", {"numDecimals": 0})
        self.assertEqual(values["num_decimals"], 0)

    def test_a_custom_logging_profile_stays_selected(self):
        dlg = SyncDialog()
        before = {"level": "MY_LOGGING", "location": "logs/x.log", "stdout": True}
        form = ResourceFormDialog(
            title="t", fields=dlg._server_fields("logging", before), values=before
        )
        self.assertEqual(form.get_values()["level"], "MY_LOGGING")

    def test_no_log_file_field_when_the_server_has_none(self):
        # GeoServer 3.0.1 answers a PUT of `location` with 200 and drops it.
        dlg = SyncDialog()
        keys = {
            settings.get("location"): [
                field["key"]
                for field in dlg._server_fields(
                    "logging", ServerTabMixin._server_form_values("logging", settings)
                )
            ]
            for settings in (
                {"level": "DEFAULT_LOGGING", "stdOutLogging": True},
                LOGGING["logging"],
            )
        }
        self.assertEqual(keys[None], ["level", "stdout"])
        self.assertEqual(keys["logs/geoserver.log"], ["level", "location", "stdout"])

    def test_a_cancelled_save_or_reload_says_the_change_may_still_land(self):
        self.dlg._wait_for = _cancel_writes
        self.open_and_save(["WFS", "-"], title="Features")
        self.assertEqual(self.successes, [])
        self.assertEqual(
            len([w for w in self.warnings if "can still apply" in w]), 1, self.warnings
        )

        class Choosing(ResourceFormDialog):
            def exec(inner):
                return QDialog.DialogCode.Accepted

            def get_values(inner):
                return {"action": "reload"}

        with patch.object(tab_server, "ResourceFormDialog", Choosing):
            self.dlg._reload_or_reset_catalog()
        self.assertEqual(self.successes, [])
        self.assertEqual(
            len([w for w in self.warnings if "can still apply" in w]), 2, self.warnings
        )


def log_form(dlg, location):
    """A logging form with its Show the log button, as the user clicks it."""
    form = ResourceFormDialog(
        title="Logging",
        fields=[{"key": "location", "label": "Location", "type": "text"}],
        values={"location": location},
    )
    dlg._add_log_button(form)
    [button] = [b for b in form.findChildren(QPushButton) if b.text() == "Show the log"]
    return form, button


class TestLogAndCatalog(unittest.TestCase):
    def test_a_log_outside_the_data_directory_is_said_not_requested(self):
        # Said over the logging form: a banner sat behind it, out of reach.
        dlg = SyncDialog()
        warnings = []
        dlg.show_warning_message = warnings.append
        dlg._fetch = dlg._wait_for = lambda *args, **kwargs: self.fail("no request")
        form, button = log_form(dlg, "/var/log/geoserver.log")
        with patch.object(tab_server, "QMessageBox", create=True) as box:
            button.click()
        box.warning.assert_called_once()
        parent, _title, text = box.warning.call_args.args
        self.assertIs(parent, form)
        self.assertIn("outside its data directory", text)
        self.assertEqual(warnings, [])

    def test_the_log_keeps_only_its_end(self):
        class Streamed:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def iter_content(self, size):
                for number in range(5000):
                    yield f"line {number}\n".encode()

        class Client:
            url, auth, verifytls = "http://gs", ("u", "p"), True

        with patch.object(tab_server.requests, "get", return_value=Streamed()) as get:
            tail = ServerTabMixin._log_tail(Client(), "/rest/resource/x", 2000, 10)
        self.assertEqual(tail.splitlines()[-1], "line 4999")
        self.assertEqual(len(tail.splitlines()), 10)
        self.assertTrue(get.call_args.kwargs["stream"])

    def test_cancel_stops_the_download(self):
        # The whole file kept streaming after the waiting box was cancelled.
        stop = threading.Event()
        served = []

        class Streamed:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def iter_content(self, size):
                for number in range(5000):
                    served.append(number)
                    if number == 2:
                        stop.set()
                    yield b"x\n"

        class Client:
            url, auth, verifytls = "http://gs", ("u", "p"), True

        with patch.object(tab_server.requests, "get", return_value=Streamed()):
            with self.assertRaises(Abandoned):
                ServerTabMixin._log_tail(Client(), "/rest/resource/x", stop=stop)
        self.assertEqual(len(served), 3)

    def test_a_log_that_cannot_be_read_is_said_over_the_form(self):
        # The banner went to the message bar, behind the modal logging form.
        dlg = SyncDialog()
        dlg.gs = FakeGS()
        banners = []
        dlg.show_error_message = dlg.show_warning_message = banners.append
        form, button = log_form(dlg, "logs/geoserver.log")

        class Refused:
            status_code, text = 403, "Access denied"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with (
            patch.object(tab_server.requests, "get", return_value=Refused()),
            patch.object(tab_server, "QMessageBox", create=True) as box,
        ):
            button.click()
        box.warning.assert_called_once()
        parent, _title, text = box.warning.call_args.args
        self.assertIs(parent, form)
        self.assertIn("HTTP 403: Access denied", text)
        self.assertEqual(banners, [])

    def show_log(self, answer):
        """Click Show the log over a server whose log GET gives `answer`:
        (the box patched in, the log viewer patched in)."""
        dlg = SyncDialog()
        dlg.gs = FakeGS()
        dlg.show_error_message = dlg.show_warning_message = self.fail
        form, button = log_form(dlg, "logs/geoserver.log")
        with (
            patch.object(tab_server.requests, "get", return_value=answer),
            patch.object(tab_server, "QMessageBox", create=True) as box,
            patch.object(tab_server, "ResourceFormDialog") as viewer,
        ):
            button.click()
        return form, box, viewer

    def test_a_missing_log_file_is_explained_not_reported_as_a_404(self):
        # GeoServer Cloud writes no log file (reported on its pgconfig
        # backend): "Failed to read the log: HTTP 404: Undefined resource path."
        class Missing:
            status_code, text = 404, "Undefined resource path."

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        form, box, viewer = self.show_log(Missing())
        box.warning.assert_not_called()
        viewer.assert_not_called()
        box.information.assert_called_once()
        parent, _title, text = box.information.call_args.args
        self.assertIs(parent, form)
        self.assertIn("GeoServer has written nothing to logs/geoserver.log", text)
        self.assertIn("standard output", text)

    def test_an_empty_log_is_explained_not_shown_as_an_empty_dialog(self):
        # On its datadir backend GeoServer Cloud leaves a 0-byte file there.
        class Empty:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def iter_content(self, size):
                return iter(())

        _form, box, viewer = self.show_log(Empty())
        viewer.assert_not_called()
        box.information.assert_called_once()
        self.assertIn("written nothing", box.information.call_args.args[2])

    def test_cancel_on_the_waiting_box_stops_that_download(self):
        # _log_tail stops on its event; nothing checked that the waiting
        # box's Cancel sets that same one.
        dlg = SyncDialog()
        dlg.gs = FakeGS()
        form, button = log_form(dlg, "logs/geoserver.log")
        downloads = []

        def tail(client, path, stop=None, **kwargs):
            downloads.append(stop)
            return ""

        def cancelled(action, write=False, stop=None):
            action()  # the download has started
            if stop is not None:
                stop.set()
            raise Abandoned()

        dlg._log_tail = tail
        dlg._wait_for = cancelled
        with patch.object(tab_server, "QMessageBox", create=True) as box:
            button.click()
        [stop] = downloads
        self.assertTrue(stop is not None and stop.is_set())
        box.warning.assert_not_called()

    def test_reload_and_reset_post_to_their_endpoint(self):
        dlg = SyncDialog()
        dlg.gs = FakeGS()
        dlg.show_success_message = lambda text: None
        for choice, endpoint in (("reload", "/rest/reload"), ("reset", "/rest/reset")):

            class Choosing(ResourceFormDialog):
                def exec(inner):
                    return QDialog.DialogCode.Accepted

                def get_values(inner, choice=choice):
                    return {"action": choice}

            with patch.object(tab_server, "ResourceFormDialog", Choosing):
                dlg._reload_or_reset_catalog()
            self.assertEqual(dlg.gs.calls[-1][:2], ("POST", endpoint))


if __name__ == "__main__":
    unittest.main()
