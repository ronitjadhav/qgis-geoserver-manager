#! python3  # noqa E265

"""
A dialog whose tab loads run inline, for tests that assert on the table.

`GeoServerMainDialog` loads every tab in a `QgsTask`, so `_load_x()` returns
before a single row exists. Tests that care about *what* is loaded use the
fetch seam directly (`_fetch_x_rows()` is a plain function returning
`(rows, failures)`); tests that drive a whole loader use this subclass, which
runs the fetch on the calling thread and renders it immediately. Tests that
care about the threading itself use the real dialog; see
`tests/qgis/test_dlg_main.py::TestBackgroundLoading`.
"""

from qgis.PyQt.QtCore import QCoreApplication, QTimer
from qgis.PyQt.QtWidgets import QApplication, QMessageBox

from geoserver_manager.gui.dlg_main import GeoServerMainDialog
from geoserver_manager.toolbelt.preferences import PlgSettingsStructure
from geoserver_manager.toolbelt.rest import PartlySaved


class SyncDialog(GeoServerMainDialog):
    """Loads tabs, and uploads, synchronously, reporting failures the way the real one does."""

    def tr(self, text, disambiguation=None, n=-1):
        # PyQt names the context after the instance's class: use the real one's.
        return QCoreApplication.translate(
            "GeoServerMainDialog", text, disambiguation, n
        )

    def _launch_task(
        self,
        slot,
        failure_message,
        work,
        on_success,
        on_cancel,
        busy_text=None,
        quiet=False,
        on_done=None,
    ):
        # No task, so nothing to cancel and no progress to report: work() gets
        # None where the real dialog passes the running task. Both _run_in_task
        # and _run_upload come through here.
        try:
            result = work(None)
        except PartlySaved as e:  # as the real dialog: saved, then a step failed
            self.show_warning_message(str(e))
            self._reload_current_tab()
            if on_done is not None:
                on_done("done")
            return
        except Exception as e:
            detail = self._error_text(e)
            self.show_error_message(f"{failure_message}: {detail}")
            if on_done is not None:
                on_done("failed")
            return
        on_success(result)
        if on_done is not None:
            on_done("done")


def ended(task):
    """True once the task manager is done with the task."""
    try:
        return task.status() in (task.TaskStatus.Complete, task.TaskStatus.Terminated)
    except RuntimeError:  # the task manager deleted it: it has ended
        return True


class FakePrefs:
    """What the dialog reads off PlgOptionsManager: one connection, no profiles.

    The settings are the real structure, so a field added to it reaches every test.
    """

    def __init__(
        self,
        url="http://gs.example.org/geoserver",
        credentials=("admin", "geoserver"),
        auth_cfg_id="",
    ):
        self.settings = PlgSettingsStructure(
            geoserver_url=url,
            geoserver_username=credentials[0],
            geoserver_password=credentials[1],
            geoserver_auth_cfg_id=auth_cfg_id,
        )

    def get_plg_settings(self):
        return self.settings

    def get_profiles(self):
        return []

    def active_profile_name(self):
        return ""

    def get_value_from_key(self, *args, **kwargs):
        return None

    def set_value_from_key(self, *args, **kwargs):
        return True


def answer_next_box(button, seen):
    """Press `button` on the next modal message box, noting its text and format."""
    tries = []

    def answer():
        box = QApplication.activeModalWidget()
        if not isinstance(box, QMessageBox):
            tries.append(1)
            if len(tries) < 100:
                QTimer.singleShot(10, answer)
            return
        seen.append((box.text(), box.textFormat()))
        box.button(button).click()

    QTimer.singleShot(0, answer)
