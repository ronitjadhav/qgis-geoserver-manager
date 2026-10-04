#! python3  # noqa E265

"""
Cancel, close and threading edges from the review: each test failed on the
code before its fix. These drive the real dialog and real QgsTasks, since
SyncDialog replaces exactly the machinery under test.

Usage from the repo root folder:

.. code-block:: bash

    QT_QPA_PLATFORM=offscreen python -m unittest tests.qgis.test_cancel_and_threads
"""

import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from qgis.core import Qgis, QgsApplication, QgsTask
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QApplication
from qgis.testing import start_app, unittest

from geoserver_manager.gui import dlg_main
from geoserver_manager.gui.dlg_main import GeoServerMainDialog
from geoserver_manager.plugin_main import GeoServerManagerPlugin
from geoserver_manager.toolbelt import log_handler
from geoserver_manager.toolbelt.rest import UploadCancelled
from tests.qgis.sync_dialog import FakePrefs, SyncDialog, ended
from tests.qgis.test_i18n import Spy
from tests.qgis.test_layer_tree import FakeIface

start_app()


def settle(until, timeout=10.0):
    """Process events until until() is true, or fail after timeout seconds."""
    deadline = time.monotonic() + timeout
    while not until():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        QApplication.processEvents()
        time.sleep(0.01)


class TestTasks(unittest.TestCase):
    def setUp(self):
        self.dlg = GeoServerMainDialog()
        self.addCleanup(self.dlg.close)
        self.messages = []
        self.dlg.show_error_message = lambda t: self.messages.append(("error", t))
        self.dlg.show_warning_message = lambda t: self.messages.append(("warning", t))
        self.dlg.show_success_message = lambda t: self.messages.append(("success", t))

    def test_a_cancel_after_the_upload_completed_is_a_success(self):
        # Reported as cancelled, it said the data file was gone and stopped
        # a batch, although the server had stored everything.
        outcome = []

        def work(task):
            task.completed = True  # what _upload_file marks after its PUT
            task.cancel()  # the user pressed Cancel just too late

        self.dlg._launch_task(
            "_upload",
            "Failed",
            work,
            lambda result: outcome.append("success"),
            lambda task: outcome.append("cancelled"),
            on_done=outcome.append,
        )
        settle(lambda: self.dlg._upload is None and outcome)
        self.assertEqual(outcome, ["success", "done"])

    def test_a_cancelled_delete_batch_still_reports_its_failures(self):
        def refused():
            raise RuntimeError("HTTP 403: referenced by layer group 'x'")

        def cancel_the_rest():
            self.dlg._delete.cancel()

        with patch.object(self.dlg, "_confirm_delete", return_value=True):
            self.dlg._delete_many(
                [("a", refused), ("b", cancel_the_rest), ("c", lambda: None)],
                lambda: None,
                ask=dlg_main.GeoServerMainDialog._one_or_many(
                    "Delete '{}'?", lambda n: ""
                ),
                done=dlg_main.GeoServerMainDialog._one_or_many(
                    "'{}' deleted.", lambda n: ""
                ),
            )
        settle(lambda: self.dlg._delete is None and self.messages)
        kinds = [kind for kind, _text in self.messages]
        self.assertIn("error", kinds)
        self.assertIn("referenced by layer group", self.messages[-1][1])

    def test_a_failed_load_forgets_the_pending_banner(self):
        # Else a later, unrelated load showed "Resources loaded."
        self.dlg._announce_after_load = "Resources loaded."

        def boom(task):
            raise RuntimeError("HTTP 500")

        self.dlg._launch_task("_task", "Failed", boom, lambda r: None, lambda t: None)
        settle(lambda: self.dlg._task is None)
        self.assertIsNone(self.dlg._announce_after_load)

    def test_the_task_bar_never_shows_the_failure_message(self):
        added = []
        with patch.object(QgsApplication.taskManager(), "addTask", added.append):
            self.dlg._launch_task(
                "_side",
                "Failed to load styles",
                lambda t: None,
                lambda r: None,
                lambda t: None,
                quiet=True,
            )
        self.dlg._side = None
        self.assertNotIn("Failed", added[0].description())

    def test_an_upload_cancelled_before_it_started_removes_its_export(self):
        # run() never ran, so neither did the finally that removes the
        # folder: a full copy of the layer stayed in the temp directory.
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        source = folder / "roads.gpkg"
        source.write_bytes(b"data")
        added = []
        with patch.object(QgsApplication.taskManager(), "addTask", added.append):
            self.dlg._upload_file(
                "Failed",
                None,
                "path",
                source,
                {},
                {},
                print,
                lambda task: None,
                folder=folder,
            )
        task = added[0]
        task.cancel()  # QGIS's task bar, before the thread pool started it
        task.finished(False)  # what the task manager then calls
        self.assertIsNone(self.dlg._upload)
        self.assertFalse(folder.exists())


class CancelledBox:
    """The waiting box, its Cancel pressed as soon as it shows."""

    def __init__(self, *args):
        pass

    def wasCanceled(self):  # noqa: N802
        return True

    def __getattr__(self, name):  # setWindowTitle, show, close...
        return lambda *args: None


class TestLifecycle(unittest.TestCase):
    """Review of 2026-09-24: each test failed on the code before its fix."""

    def setUp(self):
        self.dlg = GeoServerMainDialog()
        self.addCleanup(self.dlg.close)
        self.messages = []
        self.dlg.show_error_message = lambda t: self.messages.append(("error", t))
        self.dlg.show_warning_message = lambda t: self.messages.append(("warning", t))
        self.dlg.show_success_message = lambda t: self.messages.append(("success", t))
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def wait_box_cancels(self):
        for name, value in (
            ("QProgressDialog", CancelledBox),
            ("_WAIT_BEFORE_BOX", 0.01),
        ):
            patcher = patch.object(dlg_main, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_dialog_shown_again_after_close_takes_its_results(self):
        # The layer tree's Publish shows the dialog without reconnecting:
        # _closing stayed set from the last Close, and the table, the
        # upload's title and style and a batch's next layer were dropped.
        self.dlg.show()
        self.dlg.close()
        self.dlg.show()
        landed = []
        self.dlg._launch_task(
            "_task", "Failed", lambda t: "rows", landed.append, lambda t: None
        )
        settle(lambda: self.dlg._task is None)
        self.assertEqual(landed, ["rows"])

    def test_an_abandoned_save_holds_off_a_refresh_until_it_ends(self):
        # Its remaining requests read self.gs: a profile switch meanwhile
        # sent the rest of the save to the other server.
        self.wait_box_cancels()
        started = threading.Event()
        with self.assertRaises(dlg_main.Abandoned):
            self.dlg._wait_for_save(lambda: started.set() or self.release.wait(5))
        settle(started.is_set)
        self.assertTrue(self.dlg._refuse_while_writing())
        self.assertEqual(self.messages[-1][0], "warning")
        self.release.set()
        settle(lambda: not any(thread.write for thread in dlg_main._RUNNING))
        self.assertFalse(self.dlg._refuse_while_writing())

    def test_cancel_lets_go_of_a_hung_load_at_once(self):
        # cancel() only sets a flag: the button stayed on Cancel until the
        # request returned, up to two minutes.
        started = threading.Event()
        self.dlg._run_in_task(
            "Failed", lambda t: started.set() or self.release.wait(5), print
        )
        task = self.dlg._task
        settle(started.is_set)  # the request is on its way, and hangs
        self.dlg._on_refresh_clicked()  # Cancel
        self.assertIsNone(self.dlg._task)
        self.assertEqual(self.dlg.btn_refresh.text(), "Refresh")
        self.assertEqual(self.messages, [("warning", "Loading cancelled.")])
        self.release.set()
        settle(
            lambda: task.status()
            in (task.TaskStatus.Complete, task.TaskStatus.Terminated)
        )
        QApplication.processEvents()
        self.assertEqual(len(self.messages), 1)  # the late finish stays quiet

    def test_a_load_cancelled_from_the_task_bar_says_so(self):
        # The empty table read "Nothing here yet", as if the server had nothing.
        self.dlg._run_in_task("Failed", lambda t: self.release.wait(5), print)
        self.dlg._task.cancel()  # QGIS's task bar
        self.release.set()
        settle(lambda: self.dlg._task is None)
        self.assertIn(("warning", "Loading cancelled."), self.messages)
        self.assertIn("cancelled", self.dlg.lbl_page_info.text())

    def test_a_cancelled_save_says_it_may_still_land_and_reloads(self):
        # It ran on in its thread and changed the server without a word.
        self.wait_box_cancels()
        reloads = []
        self.dlg._reload_current_tab = lambda: reloads.append(True)
        saved = self.dlg._run_action(
            lambda: self.dlg._wait_for_save(lambda: self.release.wait(5)), "Failed"
        )
        self.assertFalse(saved)
        self.assertIn("may still apply the change", self.messages[-1][1])
        self.release.set()
        settle(lambda: reloads)

    def test_cancel_stops_the_requests_behind_a_sort(self):
        # The abandoned fan-out went on GETting every remaining row.
        self.wait_box_cancels()
        asked = []

        def detail(row):
            asked.append(row[0])
            self.release.wait(0.2)
            return ("x",)

        self.dlg.gs = object()
        self.dlg._row_detail, self.dlg._detail_columns = detail, (1,)
        rows = [[f"r{n}", dlg_main.PENDING] for n in range(100)]
        self.assertFalse(self.dlg._complete_rows(rows))
        time.sleep(1)
        self.assertLess(len(asked), 40)

    def late_cancel_box(self):
        """The waiting box, Cancel pressed in the event pass where the work lands."""
        release, before = self.release, set(dlg_main._RUNNING)

        class LateCancel(CancelledBox):
            def wasCanceled(self):  # noqa: N802
                release.set()
                for thread in dlg_main._RUNNING - before:
                    thread.wait(5000)
                return True

        for name, value in (
            ("QProgressDialog", LateCancel),
            ("_WAIT_BEFORE_BOX", 0.01),
        ):
            patcher = patch.object(dlg_main, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_read_that_lands_as_cancel_is_pressed_is_kept(self):
        # Its value was dropped for an Abandoned.
        self.late_cancel_box()
        value = self.dlg._wait_for(lambda: self.release.wait(5) and "landed")
        self.assertEqual(value, "landed")

    def test_a_save_that_lands_as_cancel_is_pressed_is_a_success(self):
        # "It may still apply the change" was said of a save already done,
        # and the reload, connected after its thread ended, never came.
        self.late_cancel_box()
        value = self.dlg._wait_for_save(lambda: self.release.wait(5) and "saved")
        self.assertEqual(value, "saved")

    def test_a_save_ending_while_its_cancel_is_handled_still_reloads(self):
        # The reload was connected to a finished() already emitted: never ran.
        self.wait_box_cancels()
        reloads = []
        self.dlg._reload_current_tab = lambda: reloads.append(True)
        release, before = self.release, set(dlg_main._RUNNING)

        class EndsTheSave:
            def set(inner):
                release.set()
                for thread in dlg_main._RUNNING - before:
                    thread.wait(5000)

        with self.assertRaises(dlg_main.Abandoned):
            self.dlg._wait_for(
                lambda: self.release.wait(5), write=True, stop=EndsTheSave()
            )
        settle(lambda: reloads, timeout=3)

    def test_a_fast_save_does_not_hold_off_a_refresh(self):
        # Its thread left _RUNNING only once the event loop turned, so every
        # later refresh_ui() in a headless run was refused as "still running".
        self.dlg._wait_for_save(lambda: None)
        self.assertFalse(self.dlg._refuse_while_writing())

    def test_a_delete_batch_ending_after_close_logs_its_failures(self):
        # Only its banner logged them, and a closed dialog shows none.
        logged = []
        self.dlg.log = lambda message, **kwargs: logged.append(message)

        def refused():
            self.release.wait(5)
            raise RuntimeError("HTTP 403: referenced by layer group 'x'")

        with patch.object(self.dlg, "_confirm_delete", return_value=True):
            self.dlg._delete_many(
                [("a", refused)],
                lambda: None,
                ask=dlg_main.GeoServerMainDialog._one_or_many(
                    "Delete '{}'?", lambda n: ""
                ),
                done=dlg_main.GeoServerMainDialog._one_or_many(
                    "'{}' deleted.", lambda n: ""
                ),
            )
        self.dlg.show()
        self.dlg.close()
        self.release.set()
        settle(lambda: self.dlg._delete is None)
        self.assertTrue(any("referenced by layer group" in m for m in logged), logged)

    def test_an_upload_cancelled_after_close_says_what_a_replace_leaves(self):
        # The log read "Failed to publish 'roads': ", and nothing said the
        # store may have lost its data file.
        logged = []
        self.dlg.log = lambda message, **kwargs: logged.append(message)

        def work(task):
            self.release.wait(5)
            if task.isCanceled():
                raise UploadCancelled()

        self.dlg._launch_task(
            "_upload", "Failed to publish 'roads'", work, print, print
        )
        self.dlg.show()
        self.dlg.close()
        self.dlg._upload.cancel()  # QGIS's task bar
        self.release.set()
        settle(lambda: self.dlg._upload is None)
        self.assertTrue(
            any("'roads'" in m and "data file" in m for m in logged), logged
        )

    def test_a_load_dropped_by_close_is_reloaded_when_shown_again(self):
        # Shown again without a refresh (the layer tree's Publish), it said
        # Cancel and "Loading…" over an empty table while nothing ran.
        self.dlg.show()
        self.dlg._run_in_task("Failed", lambda t: self.release.wait(5), print)
        self.dlg.close()
        self.release.set()
        settle(lambda: self.dlg._task is None)
        self.assertEqual(self.dlg.btn_refresh.text(), "Refresh")
        self.assertNotIn("Loading", self.dlg.lbl_page_info.text())
        reloads = []
        self.dlg._reload_current_tab = lambda: reloads.append(True)
        self.dlg.show()
        self.assertEqual(reloads, [True])
        self.dlg.close()
        self.dlg.show()
        self.assertEqual(reloads, [True])  # once, for the load it dropped

    def test_a_load_still_running_when_shown_again_is_reloaded(self):
        # Its request was in flight at Close and returned after the show: the
        # late finish stayed quiet, and the empty table said "Nothing here yet".
        started, landed = threading.Event(), []
        self.dlg.show()
        self.dlg._run_in_task(
            "Failed", lambda t: started.set() or self.release.wait(5), print
        )
        dropped = self.dlg._task
        settle(started.is_set)
        self.dlg.close()
        self.dlg._reload_current_tab = lambda: self.dlg._run_in_task(
            "Failed", lambda t: "rows", landed.append
        )
        self.dlg.show()
        self.release.set()
        settle(lambda: self.dlg._task is None and ended(dropped))
        QApplication.processEvents()
        self.assertEqual(landed, ["rows"])
        self.assertEqual(self.messages, [])  # the dropped load stays quiet
        self.assertEqual(self.dlg.btn_refresh.text(), "Refresh")

    def test_a_truncate_runs_off_the_gui_thread(self):
        # Nine writes ran on the GUI thread: a slow server froze QGIS.
        waited = []
        self.dlg._wait_for_save = lambda action: waited.append(action)
        with patch.object(self.dlg, "_confirm_delete", return_value=True):
            self.dlg._truncate_gwc_layer(["topp:states", "topp"])
        self.assertEqual(len(waited), 1)

    def test_a_cancelled_read_ends_at_once_though_its_request_hangs(self):
        # Its task ran on to the request's timeout, and QGIS could not quit.
        started = threading.Event()
        self.dlg._run_in_task(
            "Failed", lambda t: started.set() or self.release.wait(10), print
        )
        task = self.dlg._task
        settle(started.is_set)
        task.cancel()  # what QGIS's exit does
        settle(lambda: ended(task), timeout=2)  # the request still waits 10 s
        self.assertTrue(ended(task))

    def test_qgis_quits_without_asking_about_a_read_only(self):
        launched = []
        with patch.object(QgsApplication.taskManager(), "addTask", launched.append):
            self.dlg._run_in_task("Failed", lambda t: None, print)
            self.dlg._run_upload("Failed", lambda t: None, print, print)
        self.dlg._task = self.dlg._upload = None
        read, upload = launched
        self.assertTrue(read.flags() & QgsTask.Flag.CancelWithoutPrompt)
        self.assertFalse(upload.flags() & QgsTask.Flag.CancelWithoutPrompt)

    def test_an_abandoned_save_holds_qgis_open_until_it_ends(self):
        # QGIS's exit counts tasks only: it quit and the save was cut short.
        def watches():
            return [
                task
                for task in QgsApplication.taskManager().activeTasks()
                if task.description() == "GeoServer Manager: saving…"
            ]

        self.wait_box_cancels()
        before = watches()
        with self.assertRaises(dlg_main.Abandoned):
            self.dlg._wait_for_save(lambda: self.release.wait(5))
        new = [task for task in watches() if task not in before]
        self.assertEqual(len(new), 1)
        watch = new[0]
        self.assertFalse(watch.flags() & QgsTask.Flag.CancelWithoutPrompt)
        settle(lambda: watch.status() == QgsTask.TaskStatus.Running)
        watch.cancel()  # Yes to QGIS's "try canceling these active tasks?"
        time.sleep(0.3)
        QApplication.processEvents()
        self.assertFalse(ended(watch))  # the save cannot be stopped
        self.release.set()
        settle(lambda: ended(watch))

    def unloaded_dialog(self, logged):
        """A dialog that logs into `logged`, shown by a plugin about to unload."""
        plugin = GeoServerManagerPlugin(FakeIface())
        plugin.initGui()
        dialog = plugin.main_dialog = GeoServerMainDialog()
        dialog.log = lambda message, **kwargs: logged.append(message)
        return plugin, dialog

    def test_an_upload_ending_after_an_unload_says_how_in_the_log(self):
        # Its finish returned at once: its skipped last steps went unsaid.
        logged, outcomes = [], []
        plugin, dialog = self.unloaded_dialog(logged)
        dialog._launch_task(
            "_upload",
            "Failed to publish 'roads'",
            lambda task: self.release.wait(5),
            print,
            print,
            on_done=outcomes.append,
        )
        plugin.unload()
        sip.delete(dialog)  # what deleteLater does once QGIS turns the loop
        self.release.set()
        settle(lambda: dialog._upload is None, timeout=3)
        self.assertTrue(any("last steps" in m for m in logged), logged)
        self.assertEqual(outcomes, [])  # a batch's next step needs the dialog

    def test_an_unload_says_what_runs_on_without_the_dialog(self):
        # A batch stopped without a word, a delete batch ended unreported.
        class Saving:
            write = True  # a save the waiting box let go of

        saving = Saving()
        dlg_main._RUNNING.add(saving)
        self.addCleanup(dlg_main._RUNNING.discard, saving)
        logged = []
        plugin, dialog = self.unloaded_dialog(logged)
        dialog._upload, dialog._delete = object(), object()  # still running
        plugin.unload()
        dialog._upload = dialog._delete = None
        for said in ("during an upload", "during a delete batch", "during a save"):
            self.assertTrue(any(said in m for m in logged), (said, logged))


class TestReconnectWaitsForWrites(unittest.TestCase):
    def test_no_new_connection_while_a_delete_batch_runs(self):
        # Its remaining deletes read self.gs: they went to the other server.
        dlg = SyncDialog()
        client = dlg.gs = object()
        warnings = []
        dlg.show_warning_message = warnings.append
        dlg._delete = object()  # a delete batch is running
        dlg.refresh_ui()
        self.assertIs(dlg.gs, client)
        self.assertEqual(len(warnings), 1)

    def test_no_profile_switch_while_an_upload_runs(self):
        dlg = SyncDialog()
        dlg.show_warning_message = lambda text: None
        dlg._upload = object()
        with (
            patch.object(
                dlg.plg_settings,
                "get_profiles",
                return_value=[{"name": "B", "url": "https://b.example.org"}],
            ),
            patch.object(dlg.plg_settings, "activate_profile") as activate,
        ):
            dlg._switch_profile("B")
        activate.assert_not_called()


class FakeClient:
    def __init__(self, **kwargs):
        pass

    def get_workspaces(self):
        return ([{"name": "ws1"}], 200)

    def get_version(self):
        return ({}, 200)

    class rest_service:
        class rest_client:
            auth = ("a", "b")
            verifytls = True


def connected_dialog(test):
    """A real dialog whose connection lands through a fake client module."""
    fake = ModuleType("geoservercloud")
    fake.GeoServerCloud = FakeClient
    patcher = patch.dict(sys.modules, {"geoservercloud": fake})
    patcher.start()
    test.addCleanup(patcher.stop)
    dlg = GeoServerMainDialog()
    dlg.plg_settings = FakePrefs("http://localhost:8080/geoserver")
    dlg._probe = lambda gs, url: None
    dlg.show_error_message = dlg.show_warning_message = lambda text: None
    dlg.show_success_message = lambda text: None
    return dlg


class TestReopenAfterClose(unittest.TestCase):
    """The dialog used to work exactly once per QGIS session: closeEvent set
    _closing and nothing reset it, so the second connection never landed."""

    def test_close_then_reopen_connects_again(self):
        dlg = connected_dialog(self)
        dlg.show()
        dlg.refresh_ui()
        settle(lambda: dlg.gs is not None)
        dlg.close()
        QTest.qWait(50)
        dlg.show()
        dlg.refresh_ui()
        settle(lambda: dlg.gs is not None)  # it reconnected
        settle(lambda: dlg._task is None)
        self.assertEqual(dlg.btn_refresh.text(), "Refresh")
        dlg._closing = True
        dlg._cancel_load(user=True)


class TestQuietTask(unittest.TestCase):
    """A dialog's legend fetch must not turn the main Refresh into Cancel."""

    def test_run_quietly_leaves_the_loading_state_alone(self):
        dlg = connected_dialog(self)
        landed = []

        def work(task):
            time.sleep(0.2)
            return "png"

        dlg._run_quietly("legend", work, landed.append)
        self.assertIsNotNone(dlg._side)
        self.assertEqual(dlg.btn_refresh.text(), "Refresh")
        self.assertFalse(dlg._loading())
        settle(lambda: landed == ["png"])
        self.assertIsNone(dlg._side)


WORDS = dict(
    ask=GeoServerMainDialog._one_or_many("Delete '{}'?", lambda n: f"Delete {n}?"),
    done=GeoServerMainDialog._one_or_many(
        "'{}' deleted.", lambda n: f"{n} styles deleted."
    ),
)


class TestDeletesRunInATask(unittest.TestCase):
    def test_the_requests_leave_the_gui_thread(self):
        dlg = connected_dialog(self)
        dlg.gs = object()
        dlg._confirm_delete = lambda *args, **kwargs: True
        reloaded = []
        dlg._delete_many(
            [("a", lambda: time.sleep(0.2))], lambda: reloaded.append(1), **WORDS
        )
        self.assertIsNotNone(dlg._delete)  # still running when the call returned
        settle(lambda: reloaded == [1])

    def start(self, count=5, pause=0.1):
        dlg = connected_dialog(self)
        dlg.gs = object()
        dlg._confirm_delete = lambda *args, **kwargs: True
        self.done, self.reloaded, self.said = [], [], []
        dlg.show_success_message = dlg.show_warning_message = self.said.append
        dlg._delete_many(
            [
                (f"s{i}", lambda i=i: time.sleep(pause) or self.done.append(i))
                for i in range(count)
            ],
            lambda: self.reloaded.append(1),
            **WORDS,
        )
        return dlg

    def test_a_tab_switch_or_refresh_does_not_stop_it(self):
        """A load supersedes the load slot; a delete used to sit in it and
        stopped half way with no banner and no log line."""
        dlg = self.start()
        dlg._start_load("load failed", lambda task: ([], []))  # what a tab switch does
        settle(lambda: dlg._delete is None)
        self.assertEqual(self.done, [0, 1, 2, 3, 4])
        self.assertIn("5 styles deleted.", self.said)

    def test_it_reloads_only_the_tab_it_started_from(self):
        dlg = self.start(count=2)
        dlg.navList.blockSignals(True)
        dlg.navList.setCurrentRow(dlg.navList.currentRow() + 1)
        dlg.navList.blockSignals(False)
        settle(lambda: dlg._delete is None)
        self.assertEqual(self.reloaded, [])  # that tab's rows are its own

    def test_the_cancel_button_still_stops_it(self):
        dlg = self.start(count=20)
        settle(lambda: self.done)
        dlg._on_refresh_clicked()  # the Refresh button reads Cancel meanwhile
        settle(lambda: dlg._delete is None)
        self.assertLess(len(self.done), 20)
        self.assertTrue(any("Cancelled" in text for text in self.said))

    def test_a_second_batch_waits_for_the_first(self):
        dlg = self.start(count=3)
        dlg._delete_many([("x", lambda: None)], lambda: None, **WORDS)
        self.assertTrue(any("already running" in text for text in self.said))
        settle(lambda: dlg._delete is None)


class TestSignInPage(unittest.TestCase):
    """An expired SSO session answers 200 with a sign-in page."""

    def test_a_json_read_of_it_says_what_it_is(self):
        import json

        try:
            json.loads("<html><title>Sign in</title></html>")
        except ValueError as error:
            text = GeoServerMainDialog._error_text(error)
        self.assertIn("sign-in page", text)

    def test_a_list_read_of_it_shows_its_title_not_its_markup(self):
        page = "<html><head><title>Sign in</title></head><body>" + "x" * 500
        dlg = SyncDialog()
        with self.assertRaises(RuntimeError) as caught:
            dlg._fetch_list(lambda: (page, 200))
        self.assertIn("Sign in", str(caught.exception))
        self.assertNotIn("<html>", str(caught.exception))

    def test_a_list_read_of_it_is_reported_in_the_users_language(self):
        # The banner carried English prose whatever the locale.
        spy = Spy(["GeoServerMainDialog"])
        QCoreApplication.installTranslator(spy)
        self.addCleanup(QCoreApplication.removeTranslator, spy)
        with self.assertRaises(RuntimeError) as caught:
            SyncDialog()._fetch_list(lambda: ("<html>Sign in</html>", 200))
        self.assertTrue(
            str(caught.exception).startswith(
                "[GeoServerMainDialog] Unexpected response"
            ),
            str(caught.exception),
        )

    def test_a_form_check_that_meets_it_says_what_it_is(self):
        # "Expecting value: line 1 column 1 (char 0)" under the form.
        import json

        def check(values):
            return json.loads("<html><title>Sign in</title></html>")

        with self.assertRaises(ValueError) as caught:
            SyncDialog()._form_check(check)({})
        self.assertIn("sign-in page", str(caught.exception))

    def test_a_form_checks_own_refusal_keeps_its_words(self):
        def check(values):
            raise ValueError("Workspace 'topp' already exists.")

        with self.assertRaises(ValueError) as caught:
            SyncDialog()._form_check(check)({})
        self.assertEqual(str(caught.exception), "Workspace 'topp' already exists.")


class TestConnection(unittest.TestCase):
    def test_cancelling_the_probe_says_not_connected(self):
        # It stayed on "Connecting…" with the old rows and no client: the
        # Cancel button let go of the probe, so its on_cancel never ran.
        dlg = GeoServerMainDialog()
        self.addCleanup(dlg.close)
        dlg.show_warning_message = lambda text: None
        release = threading.Event()
        self.addCleanup(release.set)
        dlg._build_client = lambda settings: object()

        def hung_probe(gs, url):
            release.wait(5)  # a host that swallows the request

        dlg._probe = hung_probe
        dlg._populate_rows([["old", "ws"]])
        dlg.refresh_ui()
        task = dlg._task
        dlg._on_refresh_clicked()  # Cancel, while the probe hangs
        self.assertEqual(dlg.lbl_status.text(), "Not connected")
        self.assertEqual(dlg._all_rows, [])
        self.assertEqual(dlg.btn_refresh.text(), "Refresh")
        release.set()
        settle(
            lambda: task.status()
            in (task.TaskStatus.Complete, task.TaskStatus.Terminated)
        )
        QApplication.processEvents()
        self.assertEqual(dlg.lbl_status.text(), "Not connected")

    def test_a_superseded_probe_leaves_the_newer_refresh_alone(self):
        # Its cancel set "Not connected" over the new probe's "Connecting…".
        dlg = SyncDialog()
        captured = []
        dlg._run_in_task = lambda message, work, on_success, **kw: captured.append(
            kw["on_cancel"]
        )
        dlg._build_client = lambda settings: object()
        dlg.refresh_ui()
        dlg.refresh_ui()
        captured[0](SimpleNamespace(superseded=True))
        self.assertEqual(dlg.lbl_status.text(), "Connecting…")

    def test_a_failed_connection_check_is_logged_as_an_error(self):
        # The level went positionally into the logger's application slot,
        # and a message at the default level was dropped.
        dlg = SyncDialog()
        dlg.show_error_message = lambda text: None
        dlg._build_client = lambda settings: object()
        dlg._probe = lambda gs, url: ("Server unreachable", "Is it running?")
        with patch.object(log_handler, "QgsMessageLog") as message_log:
            dlg.refresh_ui()
        levels = [
            call.kwargs["level"]
            for call in message_log.logMessage.call_args_list
            if "Connection check failed" in call.kwargs["message"]
        ]
        self.assertEqual(levels, [Qgis.MessageLevel.Critical])

    def test_a_delete_ending_during_a_refresh_does_not_reload(self):
        # Its load would cancel the probe: "Connecting…" for good.
        dlg = SyncDialog()
        dlg.show_success_message = lambda text: None
        dlg.gs = None  # a Refresh is probing
        reloaded = []
        with patch.object(dlg, "_confirm_delete", return_value=True):
            dlg._delete_many(
                [("a", lambda: None)],
                lambda: reloaded.append(1),
                ask=dlg_main.GeoServerMainDialog._one_or_many(
                    "Delete '{}'?", lambda n: ""
                ),
                done=dlg_main.GeoServerMainDialog._one_or_many(
                    "'{}' deleted.", lambda n: ""
                ),
            )
        self.assertEqual(reloaded, [])


if __name__ == "__main__":
    unittest.main()
