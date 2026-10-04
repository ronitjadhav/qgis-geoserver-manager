#! python3  # noqa E265

"""
Usage from the repo root folder:

.. code-block:: bash

    # for whole tests
    python -m unittest tests.qgis.test_dlg_main
    # for specific test
    python -m unittest tests.qgis.test_dlg_main.TestTableState.test_failed_load_cannot_repaint_previous_rows
"""

# standard library
import json
import threading
import time
from concurrent.futures import CancelledError
from types import SimpleNamespace
from unittest.mock import patch

import requests
from qgis.core import Qgis
from qgis.gui import QgsMessageBar
from qgis.PyQt.QtCore import QCoreApplication, QEventLoop, QObject, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTextEdit,
)
from qgis.testing import start_app, unittest

# project
from geoserver_manager.gui.dlg_main import GeoServerMainDialog
from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog
from geoserver_manager.gui.layer_tree import LayerTreeMenu
from geoserver_manager.toolbelt.rest import PartlySaved, UploadCancelled, summarise_body
from tests.qgis.sync_dialog import FakePrefs, SyncDialog, answer_next_box, ended

start_app()

# ############################################################################
# ########## Classes #############
# ################################


class TestTableState(unittest.TestCase):
    """The table is paginated in Python and selections are mapped back by row
    index, so the row cache and what is on screen must never disagree.
    """

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg._setup_table(["Workspace Name", "Actions"])
        self.dlg._populate_rows([[f"ws{i:02d}"] for i in range(25)])

    def test_selection_maps_to_the_right_row_across_pages(self):
        self.assertEqual(self.dlg._total_pages, 2)

        self.dlg.resultsTable.selectRow(3)
        self.assertEqual(self.dlg._get_selected_rows(), [["ws03"]])

        self.dlg._page_next()
        self.dlg.resultsTable.selectRow(0)
        self.assertEqual(self.dlg._get_selected_rows(), [["ws20"]])

    def test_filter_narrows_rows_and_resets_to_first_page(self):
        self.dlg._page_next()
        self.dlg.searchBox.setText("ws1")
        self.dlg._apply_filter()

        self.assertEqual(len(self.dlg._filtered_rows), 10)
        self.assertEqual(self.dlg._current_page, 0)
        self.assertEqual(self.dlg._total_pages, 1)

    def test_a_click_on_the_actions_header_keeps_the_page(self):
        """The Actions column is not sortable: a click there re-rendered the
        page anyway, which went back to the first one."""
        self.dlg._row_actions = [("delete", "Delete", lambda row: None)]
        self.dlg._page_next()

        self.dlg._on_header_clicked(1)

        self.assertEqual(self.dlg._current_page, 1)
        self.assertIsNone(self.dlg._sort)

    def test_failed_load_cannot_repaint_previous_rows(self):
        """A loader arms the new tab's callbacks and headers, then fetches. If
        that fetch raises, the previous resource type's rows must be gone -
        otherwise search or pagination repaints them under the new tab's
        delete handler, and Delete targets the wrong resource.
        """
        # what a loader does before its (here: failing) network call
        self.dlg._setup_table(["Datastore Name", "Workspace", "Type", "Actions"])

        self.assertEqual(self.dlg._all_rows, [])
        self.assertEqual(self.dlg._filtered_rows, [])
        self.assertEqual(self.dlg._current_page, 0)

        # the debounced search timer fires on every tab switch
        self.dlg._apply_filter()
        self.assertEqual(self.dlg.resultsTable.rowCount(), 0)
        self.assertEqual(self.dlg._get_selected_rows(), [])

    def test_reset_clears_callbacks_and_pagination(self):
        self.dlg._name_click_callback = lambda row: None
        self.dlg._delete_selected_callback = lambda rows: None
        self.dlg._extra_click_callbacks = {"Workspace": lambda row: None}
        self.dlg._row_actions = [("delete", "Delete", lambda row: None)]

        self.dlg._reset_table_state()

        self.assertIsNone(self.dlg._name_click_callback)
        self.assertIsNone(self.dlg._delete_selected_callback)
        self.assertEqual(self.dlg._extra_click_callbacks, {})
        self.assertEqual(self.dlg._row_actions, [])
        self.assertFalse(self.dlg.btn_page_next.isEnabled())
        self.assertFalse(self.dlg.btn_delete_selected.isVisible())


class TestTableLooks(unittest.TestCase):
    """Review of 2026-09-23: what the table draws, and what it does not."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg._extra_click_callbacks = {"Workspace": lambda row: None}
        self.dlg._name_click_callback = lambda row: None
        self.dlg._setup_table(["Name", "Workspace", "Gridsets"])
        self.dlg._populate_rows(
            [
                ["a", "(global)", "EPSG:4326"],
                ["b", "topp", "EPSG:4326, EPSG:900913, WebMercatorQuad"],
            ]
        )

    def item(self, row, col):
        return self.dlg.resultsTable.item(row, col)

    def test_global_is_plain_text_a_workspace_a_link(self):
        self.assertFalse(self.item(0, 1).font().underline())
        self.assertTrue(self.item(1, 1).font().underline())
        self.assertTrue(self.item(0, 0).font().underline())
        self.assertNotIn("Enter", self.item(1, 1).toolTip())  # Enter opens the row

    def test_a_long_cell_shows_all_of_it_on_hover(self):
        self.assertEqual(
            self.item(1, 2).toolTip(), "EPSG:4326, EPSG:900913, WebMercatorQuad"
        )
        self.assertEqual(self.item(0, 2).toolTip(), "")

    def test_no_row_numbers_that_restart_on_every_page(self):
        self.assertTrue(self.dlg.resultsTable.verticalHeader().isHidden())

    def test_a_boolean_cell_reads_as_words(self):
        self.assertEqual(self.dlg._yes_no(True), "Yes")
        self.assertEqual(self.dlg._yes_no("true"), "Yes")
        self.assertEqual(self.dlg._yes_no(False), "No")
        self.assertEqual(self.dlg._yes_no("False"), "No")
        self.assertEqual(self.dlg._yes_no(None), "No")


class TestListingTolerance(unittest.TestCase):
    """One broken workspace must cost one warning, not the whole table."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.warnings = []
        self.dlg.show_warning_message = self.warnings.append
        self.dlg.show_error_message = lambda t: self.fail(f"unexpected error: {t}")

        class FakeGS:
            def get_workspaces(inner):
                return ([{"name": "ok1"}, {"name": "broken"}, {"name": "ok2"}], 200)

            def get_datastores(inner, ws):
                if ws == "broken":
                    raise RuntimeError("HTTP 500: boom")
                return ([{"name": f"{ws}_ds"}], 200)

            def get_datastore(inner, ws, ds):
                return ({"type": "PostGIS", "enabled": True}, 200)

        self.dlg.gs = FakeGS()

    def test_fan_out_keeps_order_and_captures_errors(self):
        def fn(x):
            if x == 2:
                raise ValueError("two")
            return x * 10

        results = self.dlg._fan_out(fn, [1, 2, 3])
        self.assertEqual([r for r, _ in results], [10, None, 30])
        self.assertIsInstance(results[1][1], ValueError)

    def test_one_failing_workspace_leaves_the_others_and_warns_once(self):
        self.dlg._load_datastores()

        names = sorted(row[0] for row in self.dlg._all_rows)
        self.assertEqual(names, ["ok1_ds", "ok2_ds"])
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("broken", self.warnings[0])

    def test_report_partial_failures_is_silent_when_nothing_failed(self):
        self.dlg._report_partial_failures([])
        self.assertEqual(self.warnings, [])


# ############################################################################
# ####### Probe fakes ############
# ################################


class ProbeResponse:
    """What requests.get gives the probe."""

    def __init__(self, status_code=200, payload=None, html=False):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"workspaces": ""}
        self._html = html

    def json(self):
        if self._html:
            raise ValueError("no JSON object could be decoded")
        return self._payload


class ProbeGS:
    """Only what _probe reads off the client: its auth and TLS setting."""

    class rest_service:
        class rest_client:
            auth = ("admin", "geoserver")
            verifytls = True


def patched_requests_get(response=None, raises=None, recorder=None):
    """A stand-in for requests.get that records its kwargs."""

    def fake_get(url, **kwargs):
        if recorder is not None:
            recorder.append((url, kwargs))
        if raises is not None:
            raise raises
        return response if response is not None else ProbeResponse()

    return fake_get


class TestNonJsonResponses(unittest.TestCase):
    """A proxy login page answers 200 with HTML; that is not 'Connected'."""

    def setUp(self):
        self.dlg = SyncDialog()

    def test_probe_gives_up_long_before_the_librarys_timeout(self):
        """A host that swallows the SYN must not hold the dialog for 120 s."""
        calls = []
        with patch("requests.get", patched_requests_get(recorder=calls)):
            self.dlg._probe(ProbeGS(), "http://gs/geoserver/")
        url, kwargs = calls[0]
        self.assertEqual(url, "http://gs/geoserver/rest/workspaces.json")
        self.assertLessEqual(kwargs["timeout"], 10)
        self.assertEqual(kwargs["auth"], ("admin", "geoserver"))
        self.assertIs(kwargs["verify"], True)


class TestSafeNames(unittest.TestCase):
    def test_path_breaking_characters_are_refused_before_any_request(self):
        dlg = SyncDialog()
        for bad in ("a/b", "a?b", "a#b", "a%b", " a", "a ", ""):
            with self.assertRaises(ValueError, msg=bad):
                dlg._require_safe_name(bad)
        for ok in ("states", "my store", "roads_2024", "a.b-c", "Straße"):
            dlg._require_safe_name(ok)


class TestLinkCells(unittest.TestCase):
    """Name cells open the resource but must stay real, selectable items."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = object()  # a click needs a connection; see TestConnectionGuard
        self.opened = []
        self.dlg._name_click_callback = self.opened.append
        self.dlg._extra_click_callbacks = {
            "Workspace": lambda row: self.opened.append(("ws", row))
        }
        self.dlg._setup_table(["Name", "Workspace", "Type", "Actions"])
        self.dlg._row_actions = [("delete", "Delete", lambda r: None)]
        self.dlg._populate_rows([[f"ds{i:02d}", "topp", "PostGIS"] for i in range(25)])

    def test_link_cells_are_items_not_widgets(self):
        table = self.dlg.resultsTable
        self.assertIsNone(table.cellWidget(0, 0))
        self.assertEqual(table.item(0, 0).text(), "ds00")
        self.assertTrue(table.item(0, 0).font().underline())  # styled as a link
        self.assertFalse(table.item(0, 2).font().underline())  # plain data cell

    def test_rows_are_selectable_by_clicking_any_cell(self):
        self.dlg.resultsTable.setCurrentCell(
            3, 0
        )  # what a mouse press on the name does
        self.assertEqual(self.dlg._get_selected_rows(), [["ds03", "topp", "PostGIS"]])
        self.assertTrue(
            self.dlg.btn_delete_selected.isEnabled()
            or self.dlg._delete_selected_callback is None
        )

    def test_click_on_a_link_cell_opens_the_row_resource(self):
        self.dlg._on_cell_clicked(1, 0)
        self.assertEqual(self.opened, [["ds01", "topp", "PostGIS"]])
        self.dlg._on_cell_clicked(1, 1)  # extra link column
        self.assertEqual(self.opened[-1], ("ws", ["ds01", "topp", "PostGIS"]))

    def test_click_on_a_plain_cell_does_nothing(self):
        self.dlg._on_cell_clicked(1, 2)
        self.assertEqual(self.opened, [])

    def test_click_respects_the_current_page(self):
        self.dlg._page_next()
        self.dlg._on_cell_clicked(0, 0)
        self.assertEqual(self.opened, [["ds20", "topp", "PostGIS"]])

    def reload_during_the_wait(self, rows):
        """_addressable, with a reload landing behind its waiting box."""
        self.dlg._filtered_rows = [["other", "topp", "PostGIS"]] * 25
        return True

    def test_a_click_opens_the_row_it_was_on_after_a_wait(self):
        # The row was read again after the wait: another resource opened.
        self.dlg._addressable = self.reload_during_the_wait
        self.dlg._on_cell_clicked(1, 0)
        self.assertEqual(self.opened, [["ds01", "topp", "PostGIS"]])

    def test_delete_selected_acts_on_the_rows_it_checked(self):
        deleted = []
        self.dlg._setup_delete_selected_button(deleted.append)
        self.dlg.resultsTable.selectRow(2)
        self.dlg._addressable = self.reload_during_the_wait
        self.dlg.btn_delete_selected.click()
        self.assertEqual(deleted, [[["ds02", "topp", "PostGIS"]]])

    def test_a_selection_does_not_move_to_other_rows(self):
        # Kept by position across a page, a sort or a filter, Delete
        # Selected then acted on whatever moved into the highlighted rows.
        self.dlg._setup_delete_selected_button(lambda rows: None)
        table = self.dlg.resultsTable
        table.selectRow(3)
        self.assertTrue(self.dlg.btn_delete_selected.isEnabled())
        self.dlg._page_next()
        self.assertEqual(self.dlg._get_selected_rows(), [])
        self.assertFalse(self.dlg.btn_delete_selected.isEnabled())
        table.selectRow(0)
        self.dlg._on_header_clicked(0)
        self.assertEqual(self.dlg._get_selected_rows(), [])
        table.selectRow(0)
        self.dlg.searchBox.setText("ds1")
        self.dlg._apply_filter()
        self.assertEqual(self.dlg._get_selected_rows(), [])


class TestRowsWhoseNameBreaksAPath(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.warnings = []
        self.dlg.show_warning_message = self.warnings.append

    def test_a_name_with_a_hash_is_refused_with_a_reason(self):
        # requests sends ".../datastores/a#b" as ".../datastores/a": deleting
        # "a#b" deleted "a", and reported success.
        self.dlg._setup_table(["Name", "Workspace"])
        for name in ("a#b", "a?b", "a%b", "a/b"):
            self.assertFalse(self.dlg._addressable([[name, "topp"]]), name)
        self.assertFalse(self.dlg._addressable([["roads", "w#x"]]))
        self.assertTrue(self.dlg._addressable([["roads", "topp"]]))
        self.assertIn("a#b", self.warnings[0])

    def test_a_click_on_such_a_row_opens_nothing(self):
        opened = []
        self.dlg._setup_table(["Name", "Workspace"])
        self.dlg._name_click_callback = opened.append
        self.dlg.gs = object()
        self.dlg._populate_rows([["a#b", "topp"]])
        self.dlg._on_cell_clicked(0, 0)
        self.assertEqual(opened, [])

    def test_only_the_cells_that_go_into_paths_are_checked(self):
        # The Server tab's summaries hold URLs; its paths hold no names.
        self.dlg._setup_table(["Name", "Summary"])
        self.dlg._path_columns = ()
        self.assertTrue(
            self.dlg._addressable([["Global settings", "Proxy base URL: https://x/"]])
        )


class TestTlsVerification(unittest.TestCase):
    """A certificate problem is named as such, and the setting reaches the client."""

    def test_build_client_passes_the_setting_through(self):
        import sys
        import types

        seen = {}

        class FakeGeoServerCloud:
            def __init__(inner, **kwargs):
                seen.update(kwargs)

        fake = types.ModuleType("geoservercloud")
        fake.GeoServerCloud = FakeGeoServerCloud

        class Settings:
            geoserver_url = "https://gs.example.org"
            geoserver_verify_tls = False

            def has_credentials(inner):
                return True

            def get_credentials(inner):
                return ("admin", "secret")

        with patch.dict(sys.modules, {"geoservercloud": fake}):
            SyncDialog()._build_client(Settings())
        self.assertIs(seen["verifytls"], False)
        self.assertEqual(seen["url"], "https://gs.example.org")


class TestUnreadableCredentials(unittest.TestCase):
    """Every reason was "the master password was probably declined"."""

    def setUp(self):
        from qgis.core import QgsApplication

        self.manager = QgsApplication.authManager()
        if not self.manager.masterPasswordIsSet():
            self.manager.setMasterPassword("test-master-password", True)
        self.made = []
        self.addCleanup(
            lambda: [self.manager.removeAuthenticationConfig(i) for i in self.made]
        )

    def settings(self, auth_cfg_id):
        from geoserver_manager.toolbelt.preferences import PlgSettingsStructure

        return PlgSettingsStructure(
            geoserver_url="https://gs.example.org/geoserver",
            geoserver_auth_cfg_id=auth_cfg_id,
            geoserver_verify_tls=False,
        )

    def refused(self, auth_cfg_id):
        dlg = SyncDialog()
        errors = []
        dlg.show_error_message = errors.append
        self.assertIsNone(dlg._build_client(self.settings(auth_cfg_id)))
        self.assertEqual(len(errors), 1)
        return errors[0]

    def test_a_config_that_is_gone_is_said_to_be_gone(self):
        message = self.refused("gone123")
        self.assertIn("no longer", message)
        self.assertNotIn("master password", message)

    def test_a_config_of_another_method_is_named(self):
        from qgis.core import QgsAuthMethodConfig

        other = QgsAuthMethodConfig()
        other.setName("Header")
        other.setMethod("APIHeader")
        other.setConfig("X-Key", "abc")
        self.manager.storeAuthenticationConfig(other)
        self.made.append(other.id())
        message = self.refused(other.id())
        self.assertIn("APIHeader", message)
        self.assertNotIn("master password", message)

    def test_the_client_records_what_the_saved_settings_compare_with(self):
        # run() and a change in QGIS's Options compare the two to reconnect.
        own = self.settings("").save_credentials("admin", "geoserver")
        self.made.append(own)
        settings = self.settings(own)
        dlg = SyncDialog()
        self.assertIsNotNone(dlg._build_client(settings))
        self.assertEqual(dlg.gs_connection, settings.connection())


class TestErrorText(unittest.TestCase):
    """GeoServer's explanation must reach the user, not just the status line."""

    def test_body_is_appended(self):
        error = http_error(
            500, "Unable to delete layer referenced by layer group 'tasmania'"
        )
        text = GeoServerMainDialog._error_text(error)
        self.assertEqual(
            text,
            "HTTP 500: Unable to delete layer referenced by layer group 'tasmania'",
        )
        self.assertNotIn("for url", text)  # the request URL is noise here

    def test_html_error_pages_are_skipped(self):
        error = http_error(401, "<!doctype html><html><title>401</title></html>")
        self.assertNotIn("doctype", GeoServerMainDialog._error_text(error))

    def test_plain_exceptions_pass_through(self):
        self.assertEqual(GeoServerMainDialog._error_text(ValueError("nope")), "nope")


# ############################################################################
# ###### Background loading ######
# ################################


class SlowGS:
    """A server that answers correctly, but slowly."""

    def __init__(self, latency=0.2, workspaces=50):
        self.latency = latency
        self.names = [f"ws{index:02d}" for index in range(workspaces)]
        outer = self

        class Response:
            status_code = 200
            text = ""

            def json(inner):  # the default workspace, read raw
                return {"workspace": {"name": outer.names[0]}}

        class Client:
            def get(inner, path, **kwargs):
                time.sleep(outer.latency)
                return Response()

        class Endpoints:
            base_url = "/rest"

        class Rest:
            rest_client = Client()
            rest_endpoints = Endpoints()

        self.rest_service = Rest()

    def get_workspaces(self):
        time.sleep(self.latency)
        return ([{"name": name} for name in self.names], 200)

    def get_datastores(self, workspace_name):
        time.sleep(self.latency)
        return ([{"name": f"{workspace_name}_store"}], 200)

    def get_datastore(self, workspace_name, datastore_name):
        time.sleep(self.latency)
        return ({"type": "PostGIS", "enabled": True}, 200)


def spin_until(predicate, timeout_ms=20000):
    """Run the event loop until predicate(); return how often a timer fired.

    A tick proves the GUI thread was free to process events while the load was
    running, which is the whole point of moving loads into a QgsTask.
    """
    ticks = []
    loop = QEventLoop()

    def tick():
        ticks.append(1)
        if predicate():
            loop.quit()

    heartbeat = QTimer()
    heartbeat.setInterval(20)
    heartbeat.timeout.connect(tick)
    guard = QTimer()
    guard.setSingleShot(True)
    guard.setInterval(timeout_ms)
    guard.timeout.connect(loop.quit)
    heartbeat.start()
    guard.start()
    loop.exec()
    heartbeat.stop()
    guard.stop()
    return len(ticks)


class TestBackgroundLoading(unittest.TestCase):
    """The real dialog, the real task manager: loads must not block the GUI."""

    def setUp(self):
        self.dlg = GeoServerMainDialog()  # not SyncDialog: the point is the task
        self.warnings, self.errors = [], []
        self.dlg.show_warning_message = self.warnings.append
        self.dlg.show_error_message = self.errors.append
        self.dlg.show_success_message = lambda text: None

    def tearDown(self):
        self.dlg._closing = True
        self.dlg._cancel_load()

    def test_a_slow_load_keeps_the_dialog_responsive(self):
        """50 workspaces at 200 ms a request: the event loop keeps running."""
        self.dlg.gs = SlowGS(latency=0.2, workspaces=50)
        self.dlg._load_workspaces()

        self.assertEqual(self.dlg._all_rows, [])  # nothing blocks, nothing yet
        self.assertEqual(self.dlg.lbl_page_info.text(), "Loading…")
        self.assertEqual(self.dlg.btn_refresh.text(), "Cancel")

        ticks = spin_until(lambda: bool(self.dlg._all_rows))
        self.assertGreater(ticks, 1)
        self.assertEqual(len(self.dlg._all_rows), 50)
        self.assertEqual(self.dlg.btn_refresh.text(), "Refresh")
        self.assertEqual(self.errors, [])

    def test_the_refresh_button_cancels_and_leaves_no_rows(self):
        self.dlg.gs = SlowGS(latency=0.05, workspaces=20)
        self.dlg._load_datastores()  # fans out over every workspace
        self.assertTrue(self.dlg._loading())

        self.dlg._on_refresh_clicked()  # the same button, now Cancel
        spin_until(lambda: not self.dlg._loading())
        self.assertEqual(self.dlg._all_rows, [])  # never stale rows
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("cancelled", self.warnings[0].lower())
        self.assertEqual(self.dlg.btn_refresh.text(), "Refresh")

    def test_a_failed_fetch_reports_and_leaves_no_rows(self):
        class BrokenGS(SlowGS):
            def get_workspaces(inner):
                raise RuntimeError("HTTP 500: boom")

        self.dlg.gs = BrokenGS(latency=0)
        self.dlg._load_workspaces()
        spin_until(lambda: not self.dlg._loading())

        self.assertEqual(self.dlg._all_rows, [])
        self.assertEqual(len(self.errors), 1)
        self.assertIn("boom", self.errors[0])
        # the empty state explains itself now: nothing loaded, here is Add
        self.assertIn("Nothing here yet", self.dlg.lbl_page_info.text())

    def test_a_refresh_cancels_the_load_before_it_drops_the_client(self):
        """A fetcher reads self.gs from its worker, which is safe
        only because the client never changes under a load that still owns
        the table: refresh_ui cancels it first.
        """
        self.dlg.gs = SlowGS(latency=0.05, workspaces=50)
        self.dlg._load_datastores()
        spin_until(lambda: self.dlg._task.progress() > 0)
        stale = self.dlg._task

        self.dlg._build_client = lambda settings: None  # the refresh fails
        self.dlg.refresh_ui()

        self.assertIsNone(self.dlg.gs)
        self.assertTrue(stale.isCanceled())
        spin_until(lambda: not self.dlg._loading())
        self.assertEqual(self.dlg._all_rows, [])
        self.assertEqual(self.errors, [])  # the worker's AttributeError is dropped

    def test_a_load_that_lands_after_close_touches_nothing(self):
        """The task outlives the dialog; its callback must stay away."""
        self.dlg.gs = SlowGS(latency=0.05, workspaces=20)
        self.dlg._load_datastores()
        task = self.dlg._task
        self.dlg.close()

        # Wait for the task itself to end. Waiting for it to be cancelled *and*
        # still parked in the slot timed out (20 s) whenever it had finished and
        # freed the slot first, which is the normal case.
        spin_until(lambda: ended(task), timeout_ms=5000)
        self.assertTrue(ended(task))
        self.assertTrue(self.dlg._closing)
        self.assertEqual(self.dlg._all_rows, [])
        self.assertEqual(self.warnings, [])  # not even a banner

    def test_a_second_load_supersedes_the_first(self):
        self.dlg.gs = SlowGS(latency=0.05, workspaces=30)
        self.dlg._load_datastores()
        first = self.dlg._task
        self.dlg._load_workspaces()  # e.g. the user switched tabs
        self.assertIsNot(self.dlg._task, first)
        self.assertTrue(first.isCanceled())

        spin_until(lambda: bool(self.dlg._all_rows))
        self.assertEqual(len(self.dlg._all_rows), 30)  # the workspace rows
        self.assertEqual(self.warnings, [])  # superseding is not "cancelled"

    def test_refresh_does_not_wait_for_the_probe(self):
        """plugin_main shows the dialog and calls this; it must return at once."""

        self.dlg.plg_settings = FakePrefs()
        self.dlg._build_client = lambda settings: SlowGS(latency=0)
        probed = []

        def slow_probe(gs, url):
            time.sleep(0.3)
            probed.append(url)
            return None

        self.dlg._probe = slow_probe
        self.dlg._fetch_version_label = lambda gs: "GeoServer 2.28.5"

        started = time.monotonic()
        self.dlg.refresh_ui()
        self.assertLess(time.monotonic() - started, 0.2)  # the probe is still running
        self.assertEqual(self.dlg.lbl_status.text(), "Connecting…")

        ticks = spin_until(lambda: bool(probed) and self.dlg.gs is not None)
        self.assertGreater(ticks, 1)
        self.assertIn("Connected", self.dlg.lbl_status.text())
        self.assertIn("2.28.5", self.dlg.lbl_status.text())


class TestUploadTask(unittest.TestCase):
    """_run_upload: a mutation in a task, in its own slot, with its own Cancel."""

    def setUp(self):
        self.dlg = GeoServerMainDialog()
        self.warnings, self.errors, self.done, self.cancelled = [], [], [], []
        self.dlg.show_warning_message = self.warnings.append
        self.dlg.show_error_message = self.errors.append
        self.dlg.show_success_message = lambda text: None
        self.dlg._setup_table(["Name", "Actions"])
        self.dlg._populate_rows([["a"], ["b"]])
        self.release = threading.Event()  # what a blocked work() waits on

    def tearDown(self):
        self.release.set()
        self.dlg._closing = True
        self.dlg._cancel_load(user=True)
        self.dlg._cancel_load()

    def upload(self, work):
        return self.dlg._run_upload(
            "Upload failed", work, self.done.append, self.cancelled.append
        )

    def blocked(self, task):
        self.release.wait(10)
        return "sent"

    def test_the_work_runs_off_the_gui_thread_and_lands_back_on_it(self):
        threads = {}

        def work(task):
            threads["worker"] = threading.current_thread()
            task.setProgress(50)
            return "sent"

        self.assertTrue(self.upload(work))
        self.assertEqual(self.dlg.btn_refresh.text(), "Cancel")
        self.assertEqual(self.dlg.lbl_page_info.text(), "Uploading…")

        spin_until(lambda: self.done)
        self.assertEqual(self.done, ["sent"])
        self.assertIsNot(threads["worker"], threading.main_thread())
        self.assertFalse(self.dlg._loading())
        self.assertEqual(self.dlg.btn_refresh.text(), "Refresh")
        # the rows stayed on screen; the label goes back to them
        self.assertTrue(self.dlg.lbl_page_info.text().startswith("Results 1 to 2 "))
        self.assertEqual(self.errors, [])

    def test_a_load_started_meanwhile_does_not_cancel_it(self):
        self.upload(self.blocked)
        self.dlg.gs = SlowGS(latency=0, workspaces=3)
        self.dlg._load_workspaces()  # supersedes a *load*, never the upload
        spin_until(lambda: self.dlg._task is None)

        self.assertEqual(len(self.dlg._all_rows), 3)
        self.assertIsNotNone(self.dlg._upload)
        self.assertFalse(self.dlg._upload.isCanceled())
        self.release.set()
        spin_until(lambda: self.done)
        self.assertEqual((self.done, self.cancelled), (["sent"], []))

    def test_a_refresh_meanwhile_leaves_the_upload_running(self):
        """F5 drops the client and re-probes; the upload holds its own."""
        self.upload(self.blocked)
        self.dlg.refresh_ui()
        self.assertIsNotNone(self.dlg._upload)
        self.assertFalse(self.dlg._upload.isCanceled())
        self.release.set()
        spin_until(lambda: self.done)
        self.assertEqual(self.done, ["sent"])

    def test_cancel_from_the_refresh_button_calls_on_cancel_only(self):
        started = threading.Event()

        def work(task):
            started.set()
            while not task.isCanceled():
                time.sleep(0.01)
            raise UploadCancelled()  # what ProgressReader does on the next read

        self.upload(work)
        started.wait(10)
        self.dlg._on_refresh_clicked()  # the same button, now Cancel
        spin_until(lambda: self.cancelled)

        self.assertEqual(len(self.cancelled), 1)
        self.assertTrue(self.cancelled[0].user_cancelled)
        self.assertEqual((self.done, self.errors), ([], []))
        self.assertEqual(self.dlg.btn_refresh.text(), "Refresh")

    def test_a_failing_upload_reports_and_calls_nothing(self):
        def work(task):
            raise RuntimeError("HTTP 500: boom")

        self.upload(work)
        spin_until(lambda: not self.dlg._loading())
        self.assertEqual(len(self.errors), 1)
        self.assertIn("boom", self.errors[0])
        self.assertEqual((self.done, self.cancelled), ([], []))

    def test_closing_the_dialog_still_ends_what_waits_on_the_upload(self):
        """A batch starts its next layer from on_done: closed before the
        upload landed, the rest of the batch was dropped without a word."""
        outcomes = []
        self.dlg._run_upload(
            "Upload failed",
            self.blocked,
            self.done.append,
            self.cancelled.append,
            on_done=outcomes.append,
        )
        self.dlg._closing = True
        self.release.set()
        spin_until(lambda: outcomes, timeout_ms=5000)
        self.assertEqual(outcomes, ["cancelled"])
        self.assertEqual(self.done, [])  # its GUI side needs the dialog

    def test_a_second_upload_is_refused_while_one_runs(self):
        self.assertTrue(self.upload(self.blocked))
        self.assertFalse(self.upload(lambda task: "never"))
        self.assertIn("already running", self.warnings[0])
        self.release.set()
        spin_until(lambda: self.done)
        self.assertEqual(self.done, ["sent"])


class TestFanOutProgress(unittest.TestCase):
    """_fan_out is where progress is reported and a cancel is noticed."""

    class FakeTask:
        def __init__(self, cancel_after=None):
            self.reported = []
            self.cancel_after = cancel_after

        def setProgress(self, value):
            self.reported.append(round(value))

        def isCanceled(self):
            return (
                self.cancel_after is not None
                and len(self.reported) >= self.cancel_after
            )

    def test_progress_is_reported_per_finished_item(self):
        task = self.FakeTask()
        results = GeoServerMainDialog._fan_out(lambda n: n * 2, [1, 2, 3, 4], task)
        self.assertEqual(task.reported, [25, 50, 75, 100])
        self.assertEqual(results, [(2, None), (4, None), (6, None), (8, None)])

    def test_a_cancel_starts_nothing_more(self):
        asked = []
        task = self.FakeTask(cancel_after=0)  # cancelled before it starts
        results = GeoServerMainDialog._fan_out(asked.append, [1, 2, 3, 4], task)
        self.assertEqual(asked, [])
        self.assertEqual(len(results), 4)
        for result, error in results:
            self.assertIsNone(result)
            self.assertIsInstance(error, CancelledError)

    def run_behind_a_slow_first_item(self, count, task):
        """Fan out `count` quick items whose first one hangs until released."""
        release = threading.Event()
        asked = []

        def fn(n):
            asked.append(n)
            if n == 0:
                release.wait(5)
            else:
                time.sleep(0.002)
            return n

        worker = threading.Thread(
            target=GeoServerMainDialog._fan_out, args=(fn, list(range(count)), task)
        )
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(release.set)
        return asked

    def test_a_slow_first_item_does_not_hide_a_cancel(self):
        # Behind a slow first item, a cancel still ran every queued item.
        cancelled = threading.Event()
        task = SimpleNamespace(
            isCanceled=cancelled.is_set, setProgress=lambda value: None
        )
        asked = self.run_behind_a_slow_first_item(2000, task)
        time.sleep(0.1)
        cancelled.set()
        at_cancel = len(asked)
        time.sleep(0.5)
        self.assertLess(len(asked) - at_cancel, 16)

    def test_progress_moves_while_the_first_item_is_slow(self):
        # The bar stayed at 0 while 63 of 64 workspaces were listed.
        task = self.FakeTask()
        self.run_behind_a_slow_first_item(64, task)
        deadline = time.monotonic() + 3
        while len(task.reported) < 63 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(task.reported), 63)


# ############################################################################
# ##### Connection guard #########
# ################################


class TestConnectionGuard(unittest.TestCase):
    """A table outlives its connection; its buttons must not crash.

    Reported from QGIS 3.44: clicking *Publish a Layer* raised
    AttributeError: 'NoneType' object has no attribute 'get_workspaces'.
    refresh_ui() clears self.gs immediately and probes in a QgsTask, so for
    that window the loaded rows and their armed buttons are still clickable.
    """

    class FakeGS:
        class rest_service:
            """Enough for the Layers tab's GET /rest/layers.json: no layers."""

            class rest_endpoints:
                base_url = "http://gs/rest"

            class rest_client:
                @staticmethod
                def get(path, **kwargs):
                    class Response:
                        status_code = 200
                        text = ""

                        def json(inner):
                            return {"layers": ""}

                    return Response()

        def get_workspaces(inner):
            return ([{"name": "topp"}], 200)

        def get_datastores(inner, workspace_name):
            return ([], 200)

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = self.FakeGS()
        self.warnings = []
        self.dlg.show_warning_message = self.warnings.append
        self.dlg.show_error_message = lambda text: self.fail(f"unexpected: {text}")
        self.dlg.show_success_message = lambda text: None
        # If the guard ever breaks, a row action would reach the modal delete
        # confirmation and hang the suite instead of failing it.
        self.dlg._confirm_delete = lambda question, labels=(), cascade="": False
        self.dlg._load_layers()  # arms the buttons, as a loaded tab does
        self.dlg.gs = None  # what refresh_ui() does while it re-probes

    def test_the_add_button_explains_itself_instead_of_raising(self):
        self.dlg.btn_add.click()
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("Not connected", self.warnings[0])

    def test_a_row_action_explains_itself_too(self):
        from geoserver_manager.gui import tab_layers

        class Rejecting(ResourceFormDialog):
            """A broken guard would open a modal here and hang the suite."""

            def exec(inner):
                return QDialog.DialogCode.Rejected

        widget = self.dlg._make_action_widget(["tasmania_roads", "topp", "taz_shapes"])
        button = widget.findChildren(QPushButton)[0]
        self.assertTrue(button.toolTip().startswith("Add to QGIS"))  # the first action
        with patch.object(tab_layers, "ResourceFormDialog", Rejecting):
            button.click()
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("Refresh", self.warnings[0])

    def test_delete_selected_is_refused(self):
        deleted = []
        self.dlg._setup_delete_selected_button(deleted.append)
        self.dlg._populate_rows([["a"], ["b"]])
        self.dlg.resultsTable.selectRow(0)
        self.dlg.btn_delete_selected.click()
        self.assertEqual(deleted, [])
        self.assertTrue(self.warnings)

    def test_a_link_cell_click_is_refused(self):
        opened = []
        self.dlg._name_click_callback = opened.append
        self.dlg._populate_rows([["tasmania_roads", "topp"]])
        self.dlg._on_cell_clicked(0, 0)
        self.assertEqual(opened, [])
        self.assertTrue(self.warnings)

    def test_everything_works_again_once_connected(self):
        self.dlg.gs = self.FakeGS()
        opened = []
        self.dlg._name_click_callback = opened.append
        self.dlg._populate_rows([["tasmania_roads", "topp"]])
        self.dlg._on_cell_clicked(0, 0)
        self.assertEqual(opened, [["tasmania_roads", "topp"]])
        self.assertEqual(self.warnings, [])

    def test_a_refresh_disarms_the_header_buttons_at_once(self):
        """Prevention, not just a catch: the buttons go grey immediately."""

        self.dlg.gs = self.FakeGS()
        self.dlg._load_layers()
        self.assertTrue(self.dlg.btn_add.isEnabled())

        self.dlg.plg_settings = FakePrefs()
        self.dlg._build_client = lambda settings: self.FakeGS()
        self.dlg._probe = lambda gs, url: None
        self.dlg._fetch_version_label = lambda gs: ""
        self.dlg._run_in_task = (
            lambda message, work, on_success, **kwargs: None
        )  # still in flight

        self.dlg.refresh_ui()
        self.assertIsNone(self.dlg.gs)
        self.assertFalse(self.dlg.btn_add.isEnabled())
        self.assertFalse(self.dlg.btn_delete_selected.isEnabled())


class TestProfileSwitcher(unittest.TestCase):
    """The saved profiles next to the status line."""

    def setUp(self):
        outer = self
        self.activated, self.refreshed = [], []

        class Profiles:
            profiles = [{"name": "dev"}]
            active = "dev"

            def get_profiles(inner):
                return [dict(p) for p in inner.profiles]

            def active_profile_name(inner):
                return inner.active

            def activate_profile(inner, profile):
                outer.activated.append(profile["name"])
                inner.active = profile["name"]

            def get_plg_settings(inner):
                from geoserver_manager.toolbelt.preferences import PlgSettingsStructure

                return PlgSettingsStructure()

            def get_value_from_key(inner, *args, **kwargs):
                return None

            def set_value_from_key(inner, *args, **kwargs):
                return True

        self.prefs = Profiles()
        self.dlg = SyncDialog()
        self.dlg.plg_settings = self.prefs
        self.dlg._build_client = lambda settings: None  # no probe in these tests

    def test_one_profile_shows_no_switcher(self):
        self.dlg.refresh_ui()
        self.assertTrue(self.dlg.cmb_profile.isHidden())

    def test_two_profiles_show_it_on_the_active_one(self):
        self.prefs.profiles = [{"name": "dev"}, {"name": "prod"}]
        self.prefs.active = "prod"
        self.dlg.refresh_ui()
        self.assertFalse(self.dlg.cmb_profile.isHidden())
        self.assertEqual(self.dlg.cmb_profile.currentText(), "prod")
        self.assertEqual(self.activated, [])  # filling it switches nothing

    def test_choosing_one_activates_it_and_reconnects(self):
        self.prefs.profiles = [{"name": "dev"}, {"name": "prod"}]
        self.dlg.refresh_ui()
        refresh = self.dlg.refresh_ui
        self.dlg.refresh_ui = lambda show_message=False: (
            self.refreshed.append(show_message),
            refresh(show_message),
        )
        self.dlg.cmb_profile.textActivated.emit("prod")
        self.assertEqual(self.activated, ["prod"])
        self.assertEqual(self.refreshed, [True])
        self.assertEqual(self.dlg.cmb_profile.currentText(), "prod")


class TestBusyLabel(unittest.TestCase):
    def test_an_ended_load_does_not_leave_loading_behind_an_upload(self):
        dlg = SyncDialog()
        dlg._task, dlg._upload = None, object()  # the upload is what still runs
        dlg._set_loading(True)
        self.assertEqual(dlg.lbl_page_info.text(), "Uploading…")
        dlg._upload, dlg._delete = None, object()
        dlg._set_loading(True)
        self.assertEqual(dlg.lbl_page_info.text(), "Working…")
        dlg._delete = None


class TestReadsOffTheGuiThread(unittest.TestCase):
    """_fetch: a form opener's read must not freeze QGIS."""

    def setUp(self):
        self.dlg = GeoServerMainDialog()
        self.errors = []
        self.dlg.show_error_message = self.errors.append
        self.dlg.show_warning_message = lambda text: None

    def test_a_slow_read_keeps_the_event_loop_running(self):
        ticks = []
        heartbeat = QTimer()
        heartbeat.setInterval(20)
        heartbeat.timeout.connect(lambda: ticks.append(1))
        heartbeat.start()
        value = self.dlg._fetch(lambda: time.sleep(1) or "answer", "failed")
        heartbeat.stop()
        self.assertEqual(value, "answer")
        # Inline, the sleep would hold the GUI thread and no tick could fire.
        self.assertGreater(len(ticks), 10)

    def test_cancel_returns_at_once_and_reports_nothing(self):
        release = threading.Event()

        def press_cancel():
            box = QApplication.activeModalWidget()
            self.assertIsInstance(box, QProgressDialog)
            box.cancel()

        QTimer.singleShot(500, press_cancel)
        started = time.monotonic()
        value = self.dlg._fetch(lambda: release.wait(10) and "late", "failed")
        release.set()
        self.assertIsNone(value)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.errors, [])

    def test_a_fast_read_shows_no_box(self):
        with patch("geoserver_manager.gui.dlg_main.QProgressDialog") as box:
            self.assertEqual(self.dlg._fetch(lambda: 42, "failed"), 42)
        box.assert_not_called()

    def test_the_error_of_the_read_is_reported(self):
        def fail():
            raise RuntimeError("HTTP 500")

        self.assertIsNone(self.dlg._fetch(fail, "Failed to load"))
        self.assertEqual(self.errors, ["Failed to load: HTTP 500"])

    def test_an_object_built_in_the_worker_comes_back_on_the_gui_thread(self):
        built = self.dlg._fetch(QObject, "failed")
        self.assertIs(built.thread(), QCoreApplication.instance().thread())

    def test_work_on_a_qgis_layer_can_stay_on_the_gui_thread(self):
        where = self.dlg._fetch(threading.current_thread, "failed", in_worker=False)
        self.assertIs(where, threading.main_thread())


# ############################################################################
# ####### Messages as written ####
# ################################


def http_error(status, body, url="http://gs/rest/x"):
    """What the library raises: HTTPError, the body only on its response."""
    response = requests.Response()
    response.status_code = status
    response._content = body.encode()
    return requests.exceptions.HTTPError(
        f"{status} Server Error:  for url: {url}", response=response
    )


def banner_text(bar):
    """The newest banner of a message bar, as the user reads it."""
    return bar.currentItem().findChild(QTextEdit).toPlainText()


class TestBannersShowTheirTextAsWritten(unittest.TestCase):
    """QgsMessageBar renders HTML, and no banner is HTML."""

    def setUp(self):
        self.dlg = SyncDialog()

    def test_markup_in_a_banner_is_shown_as_written(self):
        shown = {
            "error": 'Failed to save: matching end-tag "</Rule>".',
            "warning": "Invalid input '<', expected a number",
            "success": "Style 'rv2_a<b>c' deleted.",
            # each failure of a batch keeps its line
            "batch": "These failed:\na: HTTP 500: x\nb: HTTP 404: y",
        }
        for kind, text in shown.items():
            with self.subTest(kind=kind):
                level = "error" if kind == "batch" else kind
                getattr(self.dlg, f"show_{level}_message")(text)
                self.assertIn(text, banner_text(self.dlg.message_bar))

    def test_a_link_in_a_servers_answer_is_not_a_link(self):
        self.dlg.show_error_message("x <a href='https://evil.example/x'>click</a>")
        browser = self.dlg.message_bar.currentItem().findChild(QTextEdit)
        self.assertIn("<a href='https://evil.example/x'>", browser.toPlainText())
        self.assertNotIn("<a ", browser.toHtml())

    def test_the_layer_tree_menus_banner_too(self):
        bar = QgsMessageBar()
        menu = SimpleNamespace(iface=SimpleNamespace(messageBar=lambda: bar))
        LayerTreeMenu._say(
            menu, 'Push failed: end-tag "</Rule>".', Qgis.MessageLevel.Critical
        )
        self.assertIn('end-tag "</Rule>".', banner_text(bar))

    def test_an_error_page_reduced_to_its_text_reads_as_plain_text(self):
        # Escaped for the banner now, so an entity left in would show as is.
        page = "<html><body><h1>Invalid</h1><p>end-tag &lt;/Rule&gt; &amp; more</p>"
        self.assertEqual(summarise_body(page), "Invalid end-tag </Rule> & more")
        titled = "<html><head><title>Error 400 &quot;Bad&quot; &amp; odd</title></head>"
        self.assertEqual(summarise_body(titled), 'Error 400 "Bad" & odd')
        self.dlg._report_failure("Failed to save", http_error(400, page))
        self.assertIn(
            "HTTP 400: Invalid end-tag </Rule> & more",
            banner_text(self.dlg.message_bar),
        )

    def test_errors_and_warnings_are_sticky_success_fades(self):
        """An error explains what to do; it must not vanish before it is read."""
        durations = {}
        self.dlg.message_bar.pushMessage = lambda title, text, level, duration: (
            durations.__setitem__(title, duration)
        )
        self.dlg.show_error_message("x")
        self.dlg.show_warning_message("y")
        self.dlg.show_success_message("z")
        self.assertEqual(durations["Error"], 0)
        self.assertEqual(durations["Warning"], 0)
        self.assertEqual(durations["Success"], 5)


class TestPartlySaved(unittest.TestCase):
    """A save whose first step happened and a later one failed said only
    "Failed", and a retry then met "already exists"."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.messages = []
        self.dlg.show_warning_message = lambda t: self.messages.append(("warning", t))
        self.dlg.show_error_message = lambda t: self.messages.append(("error", t))
        self.reloaded = []
        self.dlg._reload_current_tab = lambda: self.reloaded.append(1)

    def test_it_is_a_warning_and_the_tab_reloads(self):
        def action():
            raise PartlySaved("Workspace 'w' created, but its namespace URI was not.")

        self.assertFalse(self.dlg._run_action(action, "Failed to create 'w'"))
        self.assertEqual(self.messages[0][0], "warning")
        self.assertEqual(self.reloaded, [1])


class TestDeleteMany(unittest.TestCase):
    """The shared confirm-and-delete every tab goes through."""

    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = type("GS", (), {"delete_workspace": lambda s, n: ("", 200)})()
        self.dlg._load_workspaces = lambda: None
        self.asked = []
        self.banners = []
        self.dlg.show_success_message = self.banners.append
        self.dlg.show_error_message = self.banners.append

    def ask(self, run):
        # The real box, answered Yes: it is built plain text, not by warning().
        seen = []
        answer_next_box(QMessageBox.StandardButton.Yes, seen)
        run()
        self.asked += [text for text, _format in seen]

    def test_the_sentences_are_whole_so_a_translation_can_agree(self):
        # Glued from "delete", "workspace" and the name, French could not
        # agree its words or order them.
        words = dict(
            ask=self.dlg._one_or_many("Supprimer l'espace '{}' ?", lambda n: f"{n} ?"),
            done=self.dlg._one_or_many("Espace '{}' supprimé.", lambda n: f"{n}."),
        )
        self.ask(
            lambda: self.dlg._delete_many(
                [("topp", lambda: None)], lambda: None, **words
            )
        )
        self.assertTrue(self.asked[0].startswith("Supprimer l'espace 'topp' ?"))
        self.assertEqual(self.banners, ["Espace 'topp' supprimé."])

    def test_failures_name_the_item_and_the_reason(self):
        def boom():
            raise RuntimeError("HTTP 403: referenced by layer group 'x'")

        self.dlg.gs = type(
            "GS",
            (),
            {"delete_workspace": lambda s, n: boom() if n == "a" else ("", 200)},
        )()
        self.ask(lambda: self.dlg._delete_selected_workspaces([["a", ""], ["b", ""]]))
        self.assertEqual(len(self.banners), 1)
        self.assertIn("a: HTTP 403: referenced by layer group 'x'", self.banners[0])


class TestPartialFailuresLogTheReason(unittest.TestCase):
    """The banner once sent the user to a log line without the reason."""

    def test_a_library_error_is_logged_with_geoservers_explanation(self):
        dlg = SyncDialog()
        logged = []
        dlg.log = lambda message, **kwargs: logged.append(message)
        dlg.show_warning_message = lambda text: None
        error = http_error(
            500,
            "Could not load wms store: rv2_wms2",
            url="http://gs/rest/workspaces/sf/wmsstores/rv2_wms2/wmslayers.json",
        )
        dlg._report_partial_failures([("sf:rv2_wms2", error)])
        self.assertEqual(
            logged,
            [
                "Could not list sf:rv2_wms2: HTTP 500: Could not load wms store: rv2_wms2"
            ],
        )


class TestNamesHoldingTagsStayPlain(unittest.TestCase):
    """Qt guesses rich text, so 'rv2_a<b>c' was named 'rv2_ac'."""

    def test_the_delete_confirmation_names_the_resource_as_it_is(self):
        dlg = SyncDialog()
        seen = []
        answer_next_box(QMessageBox.StandardButton.No, seen)
        confirmed = dlg._confirm_delete(
            "Are you sure you want to delete style 'rv2_a<b>c'?",
            ["rv2_a<b>c"],
            "The style file is removed.",
        )
        self.assertFalse(confirmed)
        text, text_format = seen[0]
        self.assertEqual(text_format, Qt.TextFormat.PlainText)
        self.assertEqual(
            text,
            "Are you sure you want to delete style 'rv2_a<b>c'?\n\n"
            "The style file is removed.\n\nThis action cannot be undone.",
        )

    def test_yes_confirms_and_no_does_not(self):
        dlg = SyncDialog()
        seen = []
        answer_next_box(QMessageBox.StandardButton.Yes, seen)
        self.assertTrue(dlg._confirm_delete("Delete 'topp'?"))
        answer_next_box(QMessageBox.StandardButton.No, seen)
        self.assertFalse(dlg._confirm_delete("Delete 'topp'?"))

    def test_a_form_title_holding_a_tag_is_plain_text(self):
        form = ResourceFormDialog(
            title="Style 'rv2_a<b>c'",
            fields=[{"key": "name", "label": "Name", "type": "text"}],
        )
        titles = [
            label
            for label in form.findChildren(QLabel)
            if label.text() == "Style 'rv2_a<b>c'"
        ]
        self.assertEqual(len(titles), 1)
        self.assertEqual(titles[0].textFormat(), Qt.TextFormat.PlainText)

    def test_a_description_help_and_refusal_holding_a_tag_are_plain_text(self):
        def refuse(values):
            raise ValueError("A style named 'rv2_a<b>c' already exists.")

        form = ResourceFormDialog(
            title="Add a Style",
            fields=[
                {
                    "key": "name",
                    "label": "Name",
                    "type": "text",
                    "help": "Copied from 'rv2_a<b>c'.",
                }
            ],
            description="In workspace 'rv2_a<b>c'.",
            validate=refuse,
        )
        form._on_accept()
        formats = {
            label.text(): label.textFormat() for label in form.findChildren(QLabel)
        }
        for text in (
            "In workspace 'rv2_a<b>c'.",
            "Copied from 'rv2_a<b>c'.",
            "A style named 'rv2_a<b>c' already exists.",
        ):
            with self.subTest(text=text):
                self.assertEqual(formats.get(text), Qt.TextFormat.PlainText)


# ############################################################################
# ####### What the server is #####
# ################################


def fake_server(version="2.28.5", manifest="", acl_status=200, seen=None):
    """A client for the connection's reads: the version, the manifest, the ACL.

    Its rest_client behaves as the library's: a GET raises for any error but
    404, which comes back as the answer it is.
    """

    class Client:
        def get(inner, path, params=None, **kwargs):
            if seen is not None:
                seen.append((path, params))
            response = requests.Response()
            response.status_code = 200
            if path.endswith("/about/manifest.json"):
                about = {"resource": {"@name": manifest}} if manifest else ""
                response._content = json.dumps({"about": about}).encode()
            elif path.endswith("/security/acl/catalog.json"):
                response.status_code = acl_status
                response._content = b'{"mode": "HIDE"}'
                if acl_status >= 400 and acl_status != 404:
                    raise http_error(
                        acl_status, "Administrative privileges required", path
                    )
            return response

    class GS:
        rest_service = SimpleNamespace(
            rest_client=Client(), rest_endpoints=SimpleNamespace(base_url="/rest")
        )

        def get_version(inner):
            resource = [{"@name": "GeoServer", "Version": version}]
            return ({"about": {"resource": resource}}, 200)

        def get_workspaces(inner):
            return ([], 200)

    return GS()


class ConnectingDialog(SyncDialog):
    """refresh_ui() against a fake server, the probe answering at once."""

    URL = "https://maps.example.org/geoserver"

    def __init__(self, gs):
        super().__init__()
        url = self.URL

        self.plg_settings = FakePrefs(url, credentials=("reader", "secret"))
        self._build_client = lambda settings: gs
        self._probe = lambda client, probed_url: None
        self._on_nav_changed = lambda index: None  # no tab to load here
        self.warnings = []
        self.show_warning_message = self.warnings.append


class TestAnAccountThatAdministersOnlyPart(unittest.TestCase):
    """A read-only account once read 'Connected' and 'Nothing here yet'."""

    def test_the_status_says_so_and_an_empty_tab_does_not_blame_the_server(self):
        dlg = ConnectingDialog(fake_server(acl_status=403))
        dlg.refresh_ui()
        self.assertEqual(
            dlg.lbl_status.text(),
            "Connected as a non-administrator: "
            "https://maps.example.org/geoserver (GeoServer 2.28.5)",
        )
        self.assertIn("administers", dlg.lbl_status.toolTip())
        dlg._setup_table(["Name", dlg.actions_column_label()])
        dlg._setup_add_button("Add a Workspace", "tip", lambda: None)
        dlg._populate_rows([])
        self.assertEqual(
            dlg.lbl_page_info.text(),
            "Nothing here that this account administers.",
        )

    def test_an_administrator_sees_it_as_before(self):
        dlg = ConnectingDialog(fake_server(acl_status=200))
        dlg.refresh_ui()
        self.assertEqual(
            dlg.lbl_status.text(),
            "Connected: https://maps.example.org/geoserver (GeoServer 2.28.5)",
        )
        self.assertEqual(dlg.lbl_status.toolTip(), "")
        dlg._setup_table(["Name", dlg.actions_column_label()])
        dlg._setup_add_button("Add a Workspace", "tip", lambda: None)
        dlg._populate_rows([])
        self.assertEqual(
            dlg.lbl_page_info.text(),
            "Nothing here yet. Start with 'Add a Workspace' above.",
        )

    def test_only_a_refusal_is_a_verdict(self):
        # A server without the endpoint, or one that cannot be read, is no proof.
        for status in (404, 500):
            with self.subTest(status=status):
                dlg = ConnectingDialog(fake_server(acl_status=status))
                dlg.refresh_ui()
                self.assertTrue(dlg.lbl_status.text().startswith("Connected: "))


class TestWhatTheServerRuns(unittest.TestCase):
    """The version was once shown, never checked, and Cloud unnamed."""

    def test_geoserver_cloud_is_named_from_its_manifest(self):
        seen = []
        gs = fake_server(
            version="2.28.5-SNAPSHOT",
            manifest="gs-cloud-base-spring-boot-2.28.5.1",
            seen=seen,
        )
        self.assertEqual(
            SyncDialog()._fetch_version_label(gs), "GeoServer Cloud 2.28.5.1"
        )
        # Only the jar it looks for, not the whole 190 KB manifest.
        self.assertIn(
            ("/rest/about/manifest.json", {"manifest": "gs-cloud-base-spring-boot-.*"}),
            seen,
        )

    def test_a_plain_geoserver_keeps_its_label(self):
        label = SyncDialog()._fetch_version_label(fake_server(version="2.28.5"))
        self.assertEqual(label, "GeoServer 2.28.5")

    def test_another_series_is_said_once_per_server(self):
        dlg = ConnectingDialog(fake_server(version="2.27.6"))
        dlg.refresh_ui()
        self.assertEqual(len(dlg.warnings), 1, dlg.warnings)
        self.assertIn("GeoServer 2.27.6", dlg.warnings[0])
        self.assertIn("2.28", dlg.warnings[0])
        dlg.refresh_ui()  # F5 reconnects: no second warning for the same server
        self.assertEqual(len(dlg.warnings), 1, dlg.warnings)

    def test_the_tested_series_says_nothing_cloud_included(self):
        for gs in (
            fake_server(version="2.28.5"),
            fake_server(version="2.28-SNAPSHOT"),
            fake_server(
                version="2.28.5-SNAPSHOT",
                manifest="gs-cloud-base-spring-boot-2.28.5.1",
            ),
        ):
            dlg = ConnectingDialog(gs)
            dlg.refresh_ui()
            self.assertEqual(dlg.warnings, [])


# ############################################################################
# ####### Stand-alone run ########
# ################################
if __name__ == "__main__":
    unittest.main()
