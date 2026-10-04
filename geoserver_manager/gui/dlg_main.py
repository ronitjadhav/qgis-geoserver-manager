#! python3  # noqa: E265

"""
Main plugin dialog: a GeoServer resource browser.

Left panel: navigation tabs, one per resource type (see TABS).
Right panel: search bar + results table for the selected tab.

Every load runs in a QgsTask: a loader arms the GUI and hands a fetch function
to _start_load, which returns immediately and renders the rows when they land.
"""

import html
import re
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

from qgis.core import Qgis, QgsApplication, QgsTask
from qgis.gui import QgsMessageBar
from qgis.PyQt import sip, uic
from qgis.PyQt.QtCore import (
    QByteArray,
    QCoreApplication,
    QEvent,
    QObject,
    QSize,
    Qt,
    QThread,
    QTimer,
)
from qgis.PyQt.QtGui import QPalette
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QTableWidgetItem,
    QWidget,
)

from geoserver_manager.__about__ import __title__
from geoserver_manager.gui.icons import icon
from geoserver_manager.gui.scope import GLOBAL, PENDING, global_label, scope
from geoserver_manager.gui.tab_cascaded import CascadedStoreTabMixin
from geoserver_manager.gui.tab_coveragestores import CoverageStoreTabMixin
from geoserver_manager.gui.tab_datastores import DatastoreTabMixin
from geoserver_manager.gui.tab_gwc import GwcTabMixin
from geoserver_manager.gui.tab_layergroups import LayerGroupTabMixin
from geoserver_manager.gui.tab_layers import LayerTabMixin
from geoserver_manager.gui.tab_server import ServerTabMixin
from geoserver_manager.gui.tab_styles import StyleTabMixin
from geoserver_manager.gui.tab_workspaces import WorkspaceTabMixin
from geoserver_manager.gui.theme import status_colour
from geoserver_manager.toolbelt.log_handler import PlgLogger
from geoserver_manager.toolbelt.payload import as_list, name_of, unwrap
from geoserver_manager.toolbelt.preferences import PlgOptionsManager
from geoserver_manager.toolbelt.probe import probe, use_qgis_proxy
from geoserver_manager.toolbelt.rest import (
    Abandoned,
    PartlySaved,
    raw_rest,
    summarise_body,
)

# Listing a nested resource needs one GET per parent plus one per item. Eight
# parallel requests keep that bearable. They run inside a _FetchTask, so they
# never block the GUI thread.
_MAX_PARALLEL_REQUESTS = 8
# Shared by every fan-out: each page flip added eight to those still running.
_REQUEST_SLOTS = threading.BoundedSemaphore(_MAX_PARALLEL_REQUESTS)
_UNSAFE_IN_NAMES = "/?#%\\"
# A cell longer than this gets its whole text as a tooltip: columns share the
# width, so a list of gridsets or a long title is cut short on screen.
_ELIDED_AFTER = 24

# A read that answers within this many seconds never shows the waiting box, so
# a healthy server looks exactly as it did when reads ran inline.
_WAIT_BEFORE_BOX = 0.3

# The GeoServer series the plugin is tested with; another one gets a warning.
_TESTED_SERIES = (2, 28)
# The jar every GeoServer Cloud service is built on, then Cloud's own version.
_CLOUD_JAR = "gs-cloud-base-spring-boot-"


def banner_html(text):
    """Text for a QgsMessageBar, which renders HTML, shown as it is written.

    Unescaped, "</Rule>" in a GeoServer error vanished, a bare "<" cut the
    rest of the line, and an <a href> in an answer became a live link.
    """
    return html.escape(str(text), quote=False).replace("\n", "<br>")


class _Stop:
    """Stands in for a QgsTask where a loop only asks isCanceled().

    _fan_out skips what has not started once its task is cancelled; a
    waited-for fan-out has no task, so the waiting box's Cancel sets this
    instead. A read task hands one to its thread too (_FetchTask._read), so
    that thread never touches the task, and passes on the progress set here.
    """

    def __init__(self, event):
        self._event = event
        self.progress = None

    def isCanceled(self):  # noqa: N802 (QgsTask's spelling)
        return self._event.is_set()

    def setProgress(self, value):  # noqa: N802
        self.progress = value


class _ReadThread(QThread):
    """The worker of one _wait_for read.

    A QThread, not a Python thread: QGIS's network access manager and the
    WMS/WFS providers start Qt timers (their own timeouts), which a thread
    without a Qt event dispatcher cannot run. Running threads are kept in
    _RUNNING, because a QThread collected while it runs aborts QGIS, and an
    abandoned read still runs to the library's timeout.
    """

    def __init__(self, work, write=False):
        super().__init__()
        self._work = work
        # True while a save's requests run: they read self.gs (see _wait_for).
        self.write = write
        _RUNNING.add(self)
        self.finished.connect(lambda: _RUNNING.discard(self))

    def run(self):
        self._work()


_RUNNING = set()


class _FetchTask(QgsTask):
    """Runs one dialog fetch off the GUI thread.

    QgsTask brings QGIS's own progress bar and Cancel button, and calls
    finished() back on the GUI thread (the only thread allowed to touch a
    widget). Whatever run() collects is handed to the callback untouched.

    A read (`write` False) runs in a thread of its own, and a cancel lets go
    of it: waited for here, a hung request held the task, so QGIS could not
    quit until the library's timeout. QGIS's exit cancels a read without
    asking, as it has nothing to lose. A write (an upload, a delete batch) is
    waited for, and QGIS asks before it cancels one.
    """

    def __init__(self, description, work, on_finished, write=False):
        flags = QgsTask.Flag.CanCancel
        if not write:
            flags |= QgsTask.Flag.CancelWithoutPrompt
        super().__init__(description, flags)
        self._work = work
        self._write = write
        self._on_finished = on_finished
        self._result = None
        self._error = None
        # Set by _cancel_load: tells "the user pressed Cancel" apart from
        # "another load superseded this one".
        self.user_cancelled = False
        # Set by _cancel_load for a load that a newer one replaced: that one
        # says nothing. Any other cancel (QGIS's task bar) says "cancelled".
        self.superseded = False

    def run(self):
        """Worker thread. No widget may be touched from here."""
        outcome = self._attempt(self) if self._write else self._read()
        if outcome is None:
            return False  # cancelled: its thread runs on, and is not heard
        self._result, self._error = outcome
        return self._error is None and not self.isCanceled()

    def _attempt(self, task):
        """(what the work returned, None), or (None, what it raised)."""
        try:
            return self._work(task), None
        except Exception as e:  # reported on the GUI thread, by _launch_task
            return None, e

    def _read(self):
        """The read's outcome, or None as soon as the task is cancelled."""
        stop, landed = threading.Event(), []
        stand_in = _Stop(stop)
        # A Python thread: a read is REST calls, with no Qt timer to run.
        worker = threading.Thread(
            target=lambda: landed.append(self._attempt(stand_in)), daemon=True
        )
        worker.start()
        shown = None
        while worker.is_alive():
            if self.isCanceled():
                stop.set()  # what it has not started yet, it never does
                return None
            worker.join(0.05)
            if stand_in.progress != shown:
                shown = stand_in.progress
                self.setProgress(shown)
        return landed[0]

    def finished(self, result):
        """Back on the GUI thread, whatever happened."""
        self._on_finished(self, result, self._result, self._error)


_WATCHES = set()  # a task no Python object refers to never runs (measured)


class _SaveWatch(QgsTask):
    """Holds QGIS's exit while a save the waiting box let go of runs on.

    The save's _ReadThread is not a task, and QGIS's exit counts only those:
    it quit at once and killed the save half way. This one cannot stop the
    save, so it offers no Cancel, and it ends when the save does.
    """

    def __init__(self, done):
        super().__init__(
            QCoreApplication.translate("GeoServerMainDialog", "{}: saving…").format(
                __title__
            ),
            QgsTask.Flag(0),
        )
        self._done = done
        _WATCHES.add(self)

    def run(self):
        self._done.wait()
        return True

    def finished(self, _result):
        _WATCHES.discard(self)


class GeoServerMainDialog(
    QDialog,
    WorkspaceTabMixin,
    DatastoreTabMixin,
    CoverageStoreTabMixin,
    CascadedStoreTabMixin,
    LayerTabMixin,
    LayerGroupTabMixin,
    StyleTabMixin,
    GwcTabMixin,
    ServerTabMixin,
):
    """Main dialog: a GeoServer resource browser."""

    def __init__(self, parent=None, iface=None):
        super().__init__(parent)
        self.iface = iface
        self.log = PlgLogger().log
        self.gs = None
        self.gs_connection = None  # what self.gs is built from, see _build_client
        self.gs_restricted = False  # the account administers only part of it
        self._untested_warned = set()  # (url, version) said to be untested
        self._status_kind = "neutral"  # re-applied when the theme changes

        uic.loadUi(Path(__file__).parent / "dlg_main.ui", self)
        self.plg_settings = PlgOptionsManager()

        # Inline message bar (sits above the splitter)
        self.message_bar = QgsMessageBar(self)
        self.message_bar.setSizePolicy(
            QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
        )
        self.message_bar_layout.insertWidget(0, self.message_bar)

        self.splitter.setSizes([160, 740])

        self._icon_refresh_timer = QTimer(self)
        self._icon_refresh_timer.setSingleShot(True)
        self._icon_refresh_timer.timeout.connect(self._refresh_icons)
        self._setup_nav()
        # Qt numbers the rows of the page (1 to 20 on every page), which read
        # as the position in the list while "Results 21 to 40" said otherwise.
        self.resultsTable.verticalHeader().setVisible(False)

        # Pagination state
        self._page_size = 20
        self._current_page = 0
        self._all_rows = []  # all fetched rows (list of list-of-str)
        self._filtered_rows = []  # rows after search filter
        self._columns = []  # headers of the table as set up
        self._path_columns = (0, 1)  # cells that go into REST paths: _addressable
        self._sort = None  # (column, descending) applied to _filtered_rows
        self._row_actions = []  # (icon, label, callback[, tooltip]) per row action
        self._name_click_callback = None  # callback(row_data) when name is clicked
        self._extra_click_callbacks = {}  # col_header -> callback(row_data)
        self._delete_selected_callback = (
            None  # callback(list[row_data]) for bulk delete
        )

        # Background loading state
        self._task = None  # the running _FetchTask, if any
        self._upload = None  # the running upload task, its own slot: _run_upload
        self._side = None  # a quiet side task (a dialog's legend): _run_quietly
        # The visible page's detail cells, fetched after the names: _fill_details
        self._detail = None
        self._detail_rows = []  # the rows the running _detail fill fetches
        self._row_detail = None  # row -> its detail cells; runs in a worker
        self._detail_columns = ()  # the columns _row_detail fills
        self._cell_display = {}  # column -> label for a value (see _setup_table)
        self._table_generation = 0  # bumped per table: a late fill is dropped
        # A running batch of deletes: its own slot, so a tab switch or F5 (which
        # supersede `_task`) cannot stop it half way without a word.
        self._delete = None
        self._closing = False  # set in closeEvent: a late finish must stay away
        self.unloading = False  # set by the plugin's unload: this close is for good
        self._reload_on_show = False  # a load Close dropped: showEvent reloads
        self._announce_after_load = None  # banner to show once rows have landed

        # Tooltips
        self.btn_close.setToolTip(self.tr("Close the dialog"))
        self.btn_refresh.setToolTip(self.tr("Refresh the list from the server (F5)"))
        self.btn_edit_credentials.setToolTip(
            self.tr("Open Settings to change the connection")
        )
        self.searchBox.setToolTip(
            self.tr(
                "Filter the list by name or any other column (Ctrl+F to go here, Esc "
                "to clear)"
            )
        )
        self.searchBox.setPlaceholderText(self.tr("Filter this list… (Ctrl+F)"))
        self.btn_page_first.setToolTip(self.tr("First page"))
        self.btn_page_prev.setToolTip(self.tr("Previous page"))
        self.btn_page_next.setToolTip(self.tr("Next page"))
        self.btn_page_last.setToolTip(self.tr("Last page"))
        self.btn_delete_selected.setToolTip(self.tr("Delete the selected rows (Del)"))

        # Signals
        self.btn_close.clicked.connect(self.close)
        self.btn_refresh.clicked.connect(self._on_refresh_clicked)
        self.btn_edit_credentials.clicked.connect(self._edit_credentials)
        self.cmb_profile.setToolTip(self.tr("Switch to another connection"))
        self.cmb_profile.textActivated.connect(self._switch_profile)
        self.navList.currentRowChanged.connect(self._on_nav_changed)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply_filter)
        self.searchBox.textChanged.connect(self._search_timer.start)
        self.btn_page_first.clicked.connect(self._page_first)
        self.btn_page_prev.clicked.connect(self._page_prev)
        self.btn_page_next.clicked.connect(self._page_next)
        self.btn_page_last.clicked.connect(self._page_last)
        self.resultsTable.itemSelectionChanged.connect(self._on_selection_changed)
        self.resultsTable.cellClicked.connect(self._on_cell_clicked)
        self.resultsTable.horizontalHeader().sectionClicked.connect(
            self._on_header_clicked
        )

        self._restore_settings()

    # -- Keyboard -----------------------------------------------------------

    def keyPressEvent(self, event):  # noqa: N802 (Qt's own spelling)
        """F5 refresh, Ctrl+F search, Esc clear, Enter open, Del delete.

        Handled here rather than with QShortcut so each key can look at where
        the focus is: Del must delete resources only when the *table* has it,
        never while the same key is erasing a character in the search box, and
        Esc must keep closing the dialog when there is no search to clear.
        """
        key = event.key()
        modifiers = event.modifiers()

        if key == Qt.Key.Key_F5:
            self.refresh_ui(show_message=True)
            return
        if key == Qt.Key.Key_F and modifiers & Qt.KeyboardModifier.ControlModifier:
            self.searchBox.setFocus()
            self.searchBox.selectAll()
            return
        if key == Qt.Key.Key_Escape and self.searchBox.text():
            self.searchBox.clear()
            return
        if (
            key in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and self.resultsTable.hasFocus()
            and self._name_click_callback is not None
        ):
            selected = self._get_selected_rows()
            if (
                len(selected) == 1
                and self._require_connection()
                and self._addressable(selected)
            ):
                self._name_click_callback(selected[0])
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Never the dialog's default button: Enter in the search box
            # opened the Add form. From the search, Enter goes to the results,
            # where the next one opens the row.
            if self.focusWidget() is self.searchBox:
                if self._search_timer.isActive():
                    # Its re-render would drop the row selected below.
                    self._search_timer.stop()
                    self._apply_filter()
                if self.resultsTable.rowCount():
                    self.resultsTable.setFocus()
                    self.resultsTable.selectRow(0)
            return
        if (
            key == Qt.Key.Key_Delete
            and self.resultsTable.hasFocus()
            and self.btn_delete_selected.isEnabled()
            and self._delete_selected_callback is not None
        ):
            # The fifth dispatch point (invariant 10): the button is re-enabled
            # by a selection change even while a Refresh has no client yet.
            rows = self._get_selected_rows()
            if self._require_connection() and self._addressable(rows):
                self._delete_selected_callback(rows)
            return
        super().keyPressEvent(event)

    # -- Settings persistence -----------------------------------------------

    def showEvent(self, event):  # noqa: N802 (Qt's own spelling)
        # Shown again after Close, by any route: the toolbar, or the layer
        # tree's Publish, which shows the dialog without reconnecting. Reset
        # only in refresh_ui, _closing stayed set there and every result that
        # landed afterwards (the table, the upload's title and style, the
        # next layer of a batch) was dropped as if the dialog were gone.
        self._closing = False
        super().showEvent(event)
        if self._reload_on_show:
            # Close dropped a load; the layer tree's Publish shows no new one.
            self._reload_on_show = False
            self._reload_current_tab()

    def closeEvent(self, event):
        # A running task would call back into widgets that are on their way out.
        # An upload is left to finish: stopping it mid-body would leave a
        # replaced store without its file (measured, see _run_upload), and the
        # task is visible in QGIS's own task bar with its own Cancel.
        self._closing = True
        if self.unloading:
            self._log_work_left_running()
        if self._task is not None:
            # Its finish may land after a show again, where it stays quiet.
            self._reload_on_show = True
        self._cancel_load()
        if self._side is not None:
            self._side.cancel()
        self._store_settings()
        super().closeEvent(event)

    def _log_work_left_running(self):
        """The plugin unloads: say in the log what runs on without the dialog.

        An upload, a delete batch and a save the waiting box let go of run to
        their end, but what was to follow them needs the dialog: a layer's
        title, keywords and style, a batch's next layers, the report, the
        reload. Nothing said so, and a batch just stopped.
        """
        left = (
            (
                self._upload is not None,
                QCoreApplication.translate(
                    "GeoServerMainDialog",
                    "The plugin was unloaded during an upload. The upload goes on, "
                    "but not its last steps (the layer's title, keywords and style) "
                    "nor the rest of the batch.",
                ),
            ),
            (
                self._delete is not None,
                QCoreApplication.translate(
                    "GeoServerMainDialog",
                    "The plugin was unloaded during a delete batch. It goes on to "
                    "the end without a report; a delete that fails is logged.",
                ),
            ),
            (
                any(thread.write for thread in _RUNNING),
                QCoreApplication.translate(
                    "GeoServerMainDialog",
                    "The plugin was unloaded during a save it had stopped waiting "
                    "for. The save goes on: open its tab again to see whether it "
                    "landed.",
                ),
            ),
        )
        for running, message in left:
            if running:
                self.log(message, log_level=Qgis.MessageLevel.Warning)

    def _store_settings(self):
        """Persist dialog geometry, splitter sizes and the open tab."""
        self.plg_settings.set_value_from_key("dialog_geometry", self.saveGeometry())
        self.plg_settings.set_value_from_key(
            "splitter_state", self.splitter.saveState()
        )
        self.plg_settings.set_value_from_key("last_tab", self.navList.currentRow())

    def _restore_settings(self):
        """Restore dialog geometry and splitter sizes."""
        geometry = self.plg_settings.get_value_from_key(
            "dialog_geometry", None, QByteArray
        )
        if geometry is not None:
            self.restoreGeometry(geometry)

        splitter_state = self.plg_settings.get_value_from_key(
            "splitter_state", None, QByteArray
        )
        if splitter_state is not None:
            self.splitter.restoreState(splitter_state)

    # -- Messages (inline banners) -----------------------------------------

    def show_success_message(self, text):
        self.message_bar.pushMessage(
            self.tr("Success"), banner_html(text), Qgis.MessageLevel.Success, 5
        )

    def show_error_message(self, text):
        self.message_bar.pushMessage(
            self.tr("Error"), banner_html(text), Qgis.MessageLevel.Critical, 0
        )

    def show_warning_message(self, text):
        self.message_bar.pushMessage(
            self.tr("Warning"), banner_html(text), Qgis.MessageLevel.Warning, 0
        )

    # -- Connection --------------------------------------------------------

    @staticmethod
    def _check(result):
        """Unpack a geoservercloud (content, status_code) tuple.

        The library's REST client raises for most HTTP errors, but deliberately
        lets three statuses through: 404 on GET/DELETE and 409 on POST. Those
        would otherwise read as success, so raise here too. TODO(#1).
        """
        content, status_code = result
        if status_code >= 400:
            raise RuntimeError(f"HTTP {status_code}: {summarise_body(str(content))}")
        return content

    def _build_client(self, settings):
        """Return a GeoServerCloud client from the saved settings, or None.

        The constructor makes no network call; _probe does the real check.
        """
        if not settings.has_credentials():
            self._set_status(self.tr("Not configured"), "error")
            self.show_warning_message(
                self.tr(
                    "GeoServer is not configured. Open Settings to add the URL, "
                    "user name and password."
                )
            )
            return None

        username, password = settings.get_credentials()
        if not username or not password:
            self._set_status(self.tr("Auth error"), "error")
            method = settings.auth_method()
            if not method:
                message = self.tr(
                    "The saved user name and password are no longer in the QGIS "
                    "authentication database. Open Settings and enter them again."
                )
            elif method != "Basic":
                message = self.tr(
                    "The saved authentication configuration uses {}, and "
                    "GeoServer Manager only sends a user name and password. Open "
                    "Settings and enter them."
                ).format(method)
            else:
                message = self.tr(
                    "QGIS did not give the user name and password from its "
                    "authentication database: its master password was probably "
                    "declined. Open Settings and save them again."
                )
            self.show_error_message(message)
            return None

        from geoservercloud import GeoServerCloud

        # TODO(#1): the client takes no proxies; requests reads the environment.
        use_qgis_proxy(settings.geoserver_url)
        # The same shape as settings.connection(): run() compares the two.
        self.gs_connection = (
            settings.geoserver_url,
            bool(settings.geoserver_verify_tls),
            username,
            password,
        )
        return GeoServerCloud(
            url=settings.geoserver_url,
            user=username,
            password=password,
            verifytls=settings.geoserver_verify_tls,
        )

    def _probe(self, gs, url):
        """Make one bounded request. Return None if it worked, else (status, message).

        The request itself lives in toolbelt/probe.py, shared with the Settings
        page; this reuses the client's own auth and TLS setting.
        """
        client = gs.rest_service.rest_client
        return probe(url, client.auth, client.verifytls)

    def _fetch_version_label(self, gs):
        """Best-effort 'GeoServer x.y.z' string for the status bar.

        Not required for a successful connection. If it fails, we still
        show "Connected" without the version suffix. GeoServer Cloud calls
        itself plain GeoServer ("2.28.5-SNAPSHOT"), so it is named from its
        manifest instead: "GeoServer Cloud 2.28.5.1".
        """
        try:
            info = self._check(gs.get_version())
        except Exception:
            return ""
        if not isinstance(info, dict):
            return ""
        for res in info.get("about", {}).get("resource", []):
            if res.get("@name", "").lower().startswith("geoserver"):
                version = res.get("Version", "")
                if not version:
                    return ""
                cloud = self._cloud_version(gs)
                return f"GeoServer Cloud {cloud}" if cloud else f"GeoServer {version}"
        return ""

    @staticmethod
    def _cloud_version(gs):
        """GeoServer Cloud's own version ("2.28.5.1"), or "" for a GeoServer."""
        try:
            # TODO(#1): the library reads /about/version, not the manifest.
            payload = raw_rest(
                gs.rest_service.rest_client,
                "get",
                f"{gs.rest_service.rest_endpoints.base_url}/about/manifest.json",
                params={"manifest": f"{_CLOUD_JAR}.*"},
            ).json()
        except Exception:  # best effort, as the version itself
            return ""
        for resource in unwrap(payload, "about", "resource"):
            name = str(resource.get("@name", "")) if isinstance(resource, dict) else ""
            if name.startswith(_CLOUD_JAR):
                return name[len(_CLOUD_JAR) :]
        return ""

    @staticmethod
    def _restricted(gs):
        """True when GeoServer refuses this account an administrator's read.

        Its REST lists only what an account administers: a read-only
        account's catalog tabs were empty and said "Nothing here yet". The
        catalog mode answers 403 to anyone but an administrator (measured on
        2.28.5); any other outcome is no verdict.
        """
        try:
            # TODO(#1): no library call says whether the account administers all.
            raw_rest(
                gs.rest_service.rest_client,
                "get",
                f"{gs.rest_service.rest_endpoints.base_url}/security/acl/catalog.json",
            )
        except Exception as error:
            response = getattr(error, "response", None)
            return getattr(response, "status_code", None) == 403
        return False

    def _set_status(self, text, kind="neutral", tooltip=""):
        """Show the connection state, in a colour this theme can carry.

        :param kind: "ok", "error", "busy" or "neutral", never a literal
            colour: `red` on a dark theme is what this replaces.
        """
        self.lbl_status.setText(text)
        self.lbl_status.setToolTip(tooltip)
        self._status_kind = kind
        self._paint_status()

    def _paint_status(self):
        """Colour the status for the palette in use; a theme switch repaints it."""
        colour = status_colour(self._status_kind, self.palette())
        sheet = f"color: {colour};" if colour else ""
        if self.lbl_status.styleSheet() != sheet:
            self.lbl_status.setStyleSheet(sheet)

    def _warn_if_untested(self, url, version):
        """Say once per server that its GeoServer is not the series tested.

        2.27.6 connected in green without a word, and some of its answers
        differ from 2.28's. `version` is the status line's label.
        """
        found = re.search(r"(\d+)\.(\d+)", version)
        if not found or tuple(map(int, found.groups())) == _TESTED_SERIES:
            return
        if (url, version) in self._untested_warned:
            return  # a Refresh reconnects: once is enough
        self._untested_warned.add((url, version))
        self.show_warning_message(
            self.tr(
                "GeoServer Manager is tested with GeoServer {tested}; this server "
                "runs {version}. Some features can work differently: see 'Supported "
                "GeoServer versions' in the user guide."
            ).format(tested="{}.{}".format(*_TESTED_SERIES), version=version)
        )

    # -- Public entry point ------------------------------------------------

    def refresh_ui(self, show_message=False):
        """Connect in the background, then reload the current tab.

        Returns as soon as the probe is on its way: nothing here waits for the
        server, so QGIS stays usable even when the host swallows the SYN.
        """
        if self._refuse_while_writing():
            return
        self._set_status(self.tr("Connecting…"), "busy")
        self.setWindowTitle(__title__)
        self._fill_profile_switcher()
        # Stop the running load *before* dropping the client its worker reads.
        self._cancel_load()
        self.gs = None
        # The rows on screen belong to the connection just dropped; the loader
        # re-arms these once the probe lands.
        self.btn_add.setEnabled(False)
        self.btn_delete_selected.setEnabled(False)
        settings = self.plg_settings.get_plg_settings()
        # Credentials come out of QgsAuthManager, so the client is built here on
        # the GUI thread; its constructor makes no network call.
        gs = self._build_client(settings)
        if gs is None:
            self._reset_table_state()
            return
        url = settings.geoserver_url

        def connect(task):
            problem = self._probe(gs, url)
            if problem:
                return problem, "", False
            return problem, self._fetch_version_label(gs), self._restricted(gs)

        def connected(result):
            problem, version, restricted = result
            if problem is not None:
                status, message = problem
                self._set_status(status, "error")
                self.show_error_message(message)
                self.log(
                    f"Connection check failed: {status}",
                    log_level=Qgis.MessageLevel.Critical,
                )
                # Clearing only the visible rows would leave the dead server's
                # data in the row cache, reachable through search and pagination.
                self._reset_table_state()
                return
            self.gs = gs
            self.gs_restricted = restricted
            self.setWindowTitle(f"{__title__}: {urlparse(url).netloc or url}")
            if restricted:
                status = self.tr("Connected as a non-administrator: {}").format(url)
                hint = self.tr(
                    "GeoServer lists only what this account administers. A tab can "
                    "show less than the server holds, and GeoServer refuses a change "
                    "outside it."
                )
            else:
                status, hint = self.tr("Connected: {}").format(url), ""
            if version:
                status += f" ({version})"
            self._set_status(status, "ok", hint)
            self._warn_if_untested(url, version)
            if show_message:
                # The rows are still on their way; _render_rows says so once
                # they land, rather than claiming it now.
                self._announce_after_load = self.tr("Resources loaded.")
            self._on_nav_changed(self.navList.currentRow())

        def probe_cancelled(task):
            # Cancel on the probe: without this the status kept "Connecting…"
            # and the old connection's rows stayed, with no client behind them.
            if task.superseded:
                return  # a newer refresh owns the status and the table
            self._set_status(self.tr("Not connected"), "error")
            self._reset_table_state()

        self._run_in_task(
            self.tr("Connection failed"), connect, connected, on_cancel=probe_cancelled
        )

    def _fill_profile_switcher(self):
        """The saved profiles next to the status line, the active one chosen.

        Hidden with fewer than two: one server needs no switch.
        """
        profiles = self.plg_settings.get_profiles()
        self.cmb_profile.blockSignals(True)
        self.cmb_profile.clear()
        self.cmb_profile.addItems([profile["name"] for profile in profiles])
        self.cmb_profile.setCurrentText(self.plg_settings.active_profile_name())
        self.cmb_profile.blockSignals(False)
        self.cmb_profile.setVisible(len(profiles) > 1)

    def _switch_profile(self, name):
        """Connect to another saved profile, then reload the open tab."""
        profile = next(
            (p for p in self.plg_settings.get_profiles() if p["name"] == name), None
        )
        if profile is None:
            return
        if self._refuse_while_writing():
            self._fill_profile_switcher()  # back to the profile still in use
            return
        self.plg_settings.activate_profile(profile)
        self.refresh_ui(show_message=True)

    def _refuse_while_writing(self):
        """True, with a warning, while a delete batch or an upload runs.

        Their remaining steps read self.gs: swapping the connection under them
        sent the rest of a delete batch (recurse=true) to the other server, and
        a publish's style and metadata too. So the connection waits instead.
        A save abandoned at the waiting box is the same: its thread goes on.
        """
        saving = any(thread.write for thread in _RUNNING)
        if self._delete is None and self._upload is None and not saving:
            return False
        self.show_warning_message(
            self.tr(
                "A delete, an upload or a save is still running on this server. "
                "Let it finish, or cancel it, before connecting again."
            )
        )
        return True

    # -- Background loading ------------------------------------------------

    def _run_in_task(
        self, failure_message, work, on_success, on_cancel=None, busy_text=None
    ):
        """Run work(task) off the GUI thread, then on_success(result) here.

        Only stateless REST reads belong in work: the client's wms / wmts
        attributes are shared state. A failed run reports itself and calls
        nothing; a cancelled one calls on_cancel(task) when given, else says
        so. This is why every loader resets the table *before* starting a
        task, so an empty table is what either outcome leaves behind. A new
        load supersedes the running one.
        """
        self._cancel_load()

        def cancelled(task):
            if on_cancel is not None:
                on_cancel(task)
            elif not task.superseded:
                # QGIS's task bar has a Cancel too: the table it leaves empty
                # read "Nothing here yet", as if the server had nothing.
                self._say_load_cancelled()

        self._launch_task(
            "_task", failure_message, work, on_success, cancelled, busy_text=busy_text
        )

    def _run_quietly(self, failure_message, work, on_success):
        """Run work(task) in a slot of its own, without the table's loading state.

        For a side fetch (a dialog's legend) that must neither supersede a
        running load nor turn Refresh into Cancel. A failure is still reported.
        """
        if self._side is not None:
            self._side.cancel()
        self._launch_task(
            "_side", failure_message, work, on_success, lambda task: None, quiet=True
        )

    def _run_upload(self, failure_message, work, on_success, on_cancel, on_done=None):
        """Stream a long PUT off the GUI thread, with progress and Cancel.

        `work(task)` runs in a worker: give it everything it needs as
        arguments: the REST client above all, because a Refresh clears
        `self.gs` while it runs. Hand `task.setProgress` /
        `task.isCanceled` to a `toolbelt.rest.ProgressReader` so the task bar
        moves and Cancel aborts the transfer instead of waiting for it. Unlike
        a load it is not superseded: a tab switch or F5 cancels `_task` only,
        and it does not touch the table. `on_success` reloads through
        `_reload_current_tab()` if it wants to, because the user may be on
        another tab by then. `on_cancel(task)` is where the caller says what
        the server was left with; measured for the raster upload, that is
        nothing for a new store and a store *without its file* for a replaced
        one, which is also why closing the dialog lets an upload finish.
        One upload at a time: a second is refused with a warning.
        """
        if not self._upload_slot_free():
            return False
        self._launch_task(
            "_upload",
            failure_message,
            work,
            on_success,
            on_cancel,
            busy_text=self.tr("Uploading…"),
            on_done=on_done,
        )
        return True

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
        """Park a _FetchTask in `slot` and start it.

        The slots are "_task" (loads), "_upload", "_delete", "_side" (a
        form's legend) and "_detail" (a page's detail cells).

        One slot's task never cancels another's. `finished` comes back on the
        GUI thread: a cancel (the user's, a superseding load's, or our own
        abort raised inside the worker) goes to on_cancel, an exception is
        reported, anything else is on_success(result). `on_done(outcome)`
        then runs once, with "done", "failed" or "cancelled", which is how a
        batch of uploads knows when to start the next one; a task landing
        after the dialog closed gets "cancelled", so the batch stops there
        and says what it did not start. After an unload nothing is called
        back, as the dialog is gone: the log says how the task ended.
        """

        def finished(task, ok, result, error):
            # Unloaded meanwhile: no widget is left, but the log says how it ended.
            gone = sip.isdeleted(self)
            if getattr(self, slot) is not task:
                # A newer task took over the slot: that one owns the table and
                # the Cancel button now.
                if slot == "_upload" and error is not None:
                    self.log(
                        f"{failure_message}: {self._error_text(error)}",
                        log_level=Qgis.MessageLevel.Critical,
                    )
                return
            # Free the slot even when the dialog is closing. A slot left
            # occupied is what kept a reopened dialog on "Cancel" for good.
            setattr(self, slot, None)
            # An upload whose request completed before a late Cancel is done:
            # reporting it cancelled told the user its data file was gone.
            completed = getattr(task, "completed", False)
            if self._closing or gone:
                if not quiet and not gone:
                    # Else a dialog shown again said Cancel and "Loading…".
                    self._set_loading(self._loading())
                if slot == "_task":
                    self._reload_on_show = True  # its rows were dropped
                if slot == "_upload" and task.isCanceled() and not completed:
                    # on_cancel needs the dialog, which is gone: say it in the log.
                    self.log(
                        QCoreApplication.translate(
                            "GeoServerMainDialog",
                            "{}: the upload was cancelled. If it replaced a store, "
                            "GeoServer kept the store but perhaps not its data file.",
                        ).format(failure_message),
                        log_level=Qgis.MessageLevel.Warning,
                    )
                elif error is not None:
                    self.log(
                        f"{failure_message}: {self._error_text(error)}",
                        log_level=Qgis.MessageLevel.Critical,
                    )
                elif slot == "_upload" and (completed or not task.isCanceled()):
                    # What runs on success (a layer's title, keywords and
                    # style) needs the dialog, which is gone: say so.
                    self.log(
                        QCoreApplication.translate(
                            "GeoServerMainDialog",
                            "An upload finished after the dialog was closed. Its "
                            "last steps (title, keywords, style) were not applied.",
                        ),
                        log_level=Qgis.MessageLevel.Warning,
                    )
                if on_done is not None and not gone:
                    on_done("cancelled")  # a batch stops, and says so
                return
            if not quiet:
                self._set_loading(self._loading())
            if task.isCanceled() and not completed:
                on_cancel(task)
                outcome = "cancelled"
            elif isinstance(error, PartlySaved):
                # The data is on the server and a later step failed: a plain
                # "Failed to publish" hid it, and a retry said "exists".
                self.show_warning_message(str(error))
                self.log(str(error), log_level=Qgis.MessageLevel.Warning)
                self._reload_current_tab()
                outcome = "done"
            elif error is not None:
                self._report_failure(failure_message, error)
                outcome = "failed"
            elif ok or completed:
                on_success(result)
                outcome = "done"
            else:
                outcome = "failed"
            if slot == "_task" and outcome != "done":
                # Else a later, unrelated load showed "Resources loaded."
                self._announce_after_load = None
            if on_done is not None:
                on_done(outcome)

        # The description is what QGIS's task bar shows while it runs: never
        # the failure message ("Failed to load styles") of work going fine.
        task = _FetchTask(
            busy_text or __title__,
            work,
            finished,
            write=slot in ("_upload", "_delete"),
        )
        # What a user Cancel runs at once, since it lets go of a load's task.
        task.on_cancel = on_cancel
        setattr(self, slot, task)
        if not quiet:
            self._set_loading(True, busy_text)
        QgsApplication.taskManager().addTask(task)

    def _start_load(self, failure_message, fetch):
        """Fetch a tab's rows in the background and render them when they land.

        `fetch(task) -> (rows, failures)` runs in a worker thread, so it must
        not touch a widget: everything it returns is rendered here instead.
        """
        self._run_in_task(
            failure_message, fetch, lambda result: self._render_rows(*result)
        )

    def _render_rows(self, rows, failures):
        """GUI side of a load: paint the rows, then report what could not load."""
        self._populate_rows(rows)
        self._report_partial_failures(failures)
        if self._announce_after_load:
            self.show_success_message(self._announce_after_load)
            self._announce_after_load = None

    def _cancel_load(self, user=False):
        """Cancel the running load, if any. `user` marks the Cancel button.

        The button stops a running upload first (that is the transfer the
        user sees a bar for), then a running delete batch, and only a user
        does: a superseding load (`user=False`) never touches either.
        """
        for slot in ("_upload", "_delete"):
            running = getattr(self, slot)
            if user and running is not None:
                running.user_cancelled = True
                running.cancel()
                return
        task = self._task
        if task is not None:
            task.user_cancelled = user
            task.superseded = not user
            if user:
                # cancel() only sets a flag, which the worker reads between
                # requests: a hung one kept the button on Cancel for up to two
                # minutes. Let go of the task first; its finish, late or
                # inside cancel() for one not started, finds the slot empty
                # and stays quiet.
                self._task = None
            task.cancel()
            if user:
                # Its finish finds the slot empty: its on_cancel runs here.
                self._set_loading(self._loading())
                task.on_cancel(task)
        # Its worker reads self.gs too (invariant 10): stopped with the load.
        if self._detail is not None:
            self._detail.cancel()

    def _say_load_cancelled(self):
        """The table stays as the cancelled load left it: say why it is empty."""
        self._set_loading(self._loading())
        if not self._loading() and not self._all_rows:
            self.lbl_page_info.setText(
                self.tr("Loading cancelled. Refresh to try again.")
            )
        self.show_warning_message(self.tr("Loading cancelled."))

    def _loading(self):
        """True while a background load, an upload or a delete batch runs."""
        return any(
            slot is not None for slot in (self._task, self._upload, self._delete)
        )

    def _set_loading(self, loading, busy_text=None):
        """Say that a task is running, and offer Cancel in place of Refresh."""
        self.btn_refresh.setText(self.tr("Cancel") if loading else self.tr("Refresh"))
        if not loading:
            tooltip = self.tr("Refresh the list from the server (F5)")
        elif self._upload is not None:
            tooltip = self.tr("Cancel the upload")
        elif self._delete is not None:
            tooltip = self.tr("Stop after the current one")
        else:
            tooltip = self.tr("Stop loading")
        self.btn_refresh.setToolTip(tooltip)
        if loading:
            if busy_text is None:
                # Say what is still running: a load that just ended must not
                # leave "Loading…" behind while an upload goes on.
                if self._task is not None:
                    busy_text = self.tr("Loading…")
                elif self._upload is not None:
                    busy_text = self.tr("Uploading…")
                else:
                    busy_text = self.tr("Working…")
            self.lbl_page_info.setText(busy_text)
        elif not self._all_rows:
            self.lbl_page_info.setText(self._empty_state_text())
        else:
            # An upload borrowed the label while the rows stayed on screen.
            self.lbl_page_info.setText(self._page_info_text())

    def _on_refresh_clicked(self):
        """One button: Refresh when idle, Cancel while loading."""
        if self._loading():
            self._cancel_load(user=True)
        else:
            self.refresh_ui(show_message=True)

    # -- Left navigation ---------------------------------------------------

    # One entry per tab: (label, plugin icon, loader method name). Adding a
    # resource type means adding a line here and a mixin with that loader.
    TABS = (
        ("Workspaces", "workspaces", "_load_workspaces"),
        ("Datastores", "datastores", "_load_datastores"),
        ("Coverage Stores", "coverage-stores", "_load_coverage_stores"),
        ("Cascaded Stores", "cascaded-stores", "_load_cascaded_stores"),
        ("Layers", "layers", "_load_layers"),
        ("Layer Groups", "layer-groups", "_load_layer_groups"),
        ("Styles", "styles", "_load_styles"),
        ("Tile Cache", "tile-cache", "_load_gwc_layers"),
        ("Server", "server", "_load_server"),
    )

    def _tab_titles(self):
        """Each tab's name as the navigation list shows it, translated.

        TABS keeps the English key (invariant 11): the list showed that key,
        so a French interface listed its tabs in English.
        """
        return {
            "Workspaces": self.tr("Workspaces"),
            "Datastores": self.tr("Datastores"),
            "Coverage Stores": self.tr("Coverage Stores"),
            "Cascaded Stores": self.tr("Cascaded Stores"),
            "Layers": self.tr("Layers"),
            "Layer Groups": self.tr("Layer Groups"),
            "Styles": self.tr("Styles"),
            "Tile Cache": self.tr("Tile Cache"),
            "Server": self.tr("Server"),
        }

    def _tab_help(self):
        """One line per tab for its tooltip: GeoServer's words, not REST's.

        Keyed by the TABS label, which stays untranslated (invariant 11).
        """
        return {
            "Workspaces": self.tr(
                "Namespaces that group stores, layers and styles; one is the default."
            ),
            "Datastores": self.tr(
                "Vector sources: the databases and files on the server that layers "
                "come from."
            ),
            "Coverage Stores": self.tr(
                "Raster sources: GeoTIFFs, COGs and image mosaics."
            ),
            "Cascaded Stores": self.tr(
                "WMS and WMTS stores that proxy another server's layers."
            ),
            "Layers": self.tr(
                "Everything published (vector, raster and cascaded), with its "
                "store and default style."
            ),
            "Layer Groups": self.tr("Several layers served as one, in drawing order."),
            "Styles": self.tr(
                "SLD, CSS, YSLD and MBStyle styles, global or per workspace."
            ),
            "Tile Cache": self.tr(
                "The tile cache: tiles per layer, gridset and format."
            ),
            "Server": self.tr(
                "Settings of the whole server: contact, services, logging, catalog."
            ),
        }

    def _setup_nav(self):
        """Build the navigation list on the left from TABS."""
        self.navList.clear()
        for label, icon_name, _loader in self.TABS:
            title = self._tab_titles().get(label, label)
            item = QListWidgetItem(icon(icon_name, self.navList.palette()), title)
            item.setToolTip(self._tab_help().get(label, ""))
            self.navList.addItem(item)
        # Never narrower than its longest entry: at the window's minimum
        # width the splitter gave it 140 px, and a scroll bar appeared.
        self.leftPanel.setMinimumWidth(
            max(140, self.navList.sizeHintForColumn(0) + 2 * self.navList.frameWidth())
        )
        if self.navList.count():
            # Reopen on the tab this profile left open, if it still exists:
            # TABS can gain and lose entries between versions.
            remembered = self.plg_settings.get_value_from_key("last_tab", 0, int)
            try:
                remembered = int(remembered)
            except (TypeError, ValueError):
                remembered = 0
            if not 0 <= remembered < self.navList.count():
                remembered = 0
            self.navList.setCurrentRow(remembered)

    def _reload_current_tab(self):
        """Reload whichever tab is selected.

        Use this instead of a specific loader when the caller may have been
        reached from another tab (e.g. editing a workspace from the datastore
        list), otherwise the table gets repainted with the wrong resource type.
        """
        self._on_nav_changed(self.navList.currentRow())

    def _reset_table_state(self):
        """Clear the table, its row cache and every per-tab callback.

        Loaders arm the callbacks and headers before they fetch, so a fetch that
        raises would otherwise leave the previous resource type's rows in the
        cache, reachable through the search box and the pagination buttons and
        wired to the new tab's row actions, i.e. Delete aimed at the wrong
        resource. Resetting both halves together is what keeps that impossible.
        """
        self.btn_add.setVisible(False)
        self.btn_delete_selected.setVisible(False)
        self._name_click_callback = None
        self._delete_selected_callback = None
        self._row_actions = []
        self._extra_click_callbacks = {}
        self._all_rows = []
        self._filtered_rows = []
        self._current_page = 0
        self.resultsTable.clearContents()
        self.resultsTable.setRowCount(0)
        self.lbl_page_number.setText("1")
        self.lbl_page_info.setText(self._empty_state_text())
        for button in (
            self.btn_page_first,
            self.btn_page_prev,
            self.btn_page_next,
            self.btn_page_last,
        ):
            button.setEnabled(False)

    def _on_nav_changed(self, index):
        """Load data for the selected navigation tab."""
        if index < 0:
            return
        if not self.gs:
            # Nothing to load from; the empty table says why instead of
            # looking like an empty server.
            self.lbl_page_info.setText(self._empty_state_text())
            return
        self.searchBox.clear()
        self._search_timer.stop()  # clear() may have armed it
        self._reset_table_state()
        getattr(self, self.TABS[index][2])()

    # -- Reusable table helpers --------------------------------------------

    def _setup_add_button(self, text, tooltip, callback):
        """Configure the header Add button for the current tab."""
        self.btn_add.setEnabled(True)
        self.btn_add.setText(text)
        self.btn_add.setToolTip(tooltip)
        self.btn_add.setVisible(True)
        try:
            self.btn_add.clicked.disconnect()
        except TypeError:
            pass
        # Guarded: the button stays armed while a refresh re-probes the server.
        self.btn_add.clicked.connect(lambda: self._require_connection() and callback())

    def _setup_delete_selected_button(self, callback, text=None):
        """Configure the header Delete Selected button for the current tab.

        :param text: the button's label when the action is not a delete
            (the Tile Cache tab only stops caching).
        """
        self.btn_delete_selected.setText(text or self.tr("Delete Selected"))
        self.btn_delete_selected.setVisible(True)
        self.btn_delete_selected.setEnabled(False)
        self._delete_selected_callback = callback
        try:
            self.btn_delete_selected.clicked.disconnect()
        except TypeError:
            pass

        def delete_selected():
            # Read once: a reload during _addressable's wait moves the rows.
            rows = self._get_selected_rows()
            if self._require_connection() and self._addressable(rows):
                callback(rows)

        self.btn_delete_selected.clicked.connect(delete_selected)

    def _setup_table(self, columns):
        """Reset the table with the given column headers."""
        # Qt's own sorting stays off: it would reorder the items but not
        # _filtered_rows, which every index-based lookup (selection, Enter, link
        # clicks) reads. Delete would act on a different resource than the one
        # highlighted. A header click sorts the rows themselves instead.
        self.resultsTable.setSortingEnabled(False)
        if list(columns) != self._columns:
            # Another resource type: its columns mean something else.
            self._sort = None
        self._columns = list(columns)
        # The cells that end up in REST paths: the name and the workspace.
        # A tab with others (the Layers tab's store) or none sets it after.
        self._path_columns = (0, 1)
        # A tab that lists names first sets these after, like _path_columns.
        self._row_detail = None
        self._detail_columns = ()
        # {column: value -> label} for cells that show a value the code
        # reads (the Layers type, a cached layer's id) in words.
        self._cell_display = {}
        self._table_generation += 1
        # Loaders call this before fetching, so drop the previous rows here too:
        # a fetch that raises must not leave them to be repainted under the new
        # headers (see _reset_table_state).
        self._all_rows = []
        self._filtered_rows = []
        self._current_page = 0
        self.resultsTable.clear()
        self.resultsTable.setColumnCount(len(columns))
        self.resultsTable.setHorizontalHeaderLabels(columns)
        self.resultsTable.setRowCount(0)
        header = self.resultsTable.horizontalHeader()
        actions = self.actions_column_label()
        for i in range(len(columns)):
            if columns[i] == actions:
                # Sized to its buttons in _show_page, once they exist. With
                # ResizeToContents, a real QGIS clipped them to the header's
                # width; offscreen it never did, so the trigger is not known.
                header.setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
            else:
                header.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
        self._show_sort_indicator()

    def actions_column_label(self):
        """The header text of the row-actions column.

        Every tab's last column is this one, and _setup_table recognises it by
        its text to size it to its buttons. The mixins take the label from
        here rather than translating "Actions" in their own context, so the
        two sides of that comparison cannot drift apart once a translation is
        installed (see invariant 11 in docs/development/invariants.md).
        """
        return self.tr("Actions")

    def _on_selection_changed(self):
        """Enable or disable the Delete Selected button based on selection."""
        has_selection = bool(self.resultsTable.selectionModel().selectedRows())
        self.btn_delete_selected.setEnabled(
            has_selection and self._delete_selected_callback is not None
        )
        self._refresh_action_icons()

    def _get_selected_rows(self):
        """Return the row data for all currently selected table rows.

        Assumes the table renders _filtered_rows in order; see _setup_table.
        """
        selected = []
        start = self._current_page * self._page_size
        for index in self.resultsTable.selectionModel().selectedRows():
            row_idx = start + index.row()
            if row_idx < len(self._filtered_rows):
                selected.append(self._filtered_rows[row_idx])
        return selected

    def _populate_rows(self, rows):
        """Store all rows and show the first page."""
        self._all_rows = rows
        self._current_page = 0
        self._apply_filter()

    def _apply_filter(self):
        """Filter _all_rows by the current search text, then show page."""
        search = self.searchBox.text().lower()
        if search:
            # What the cell shows, and what it holds: "raster" finds a
            # "RASTER" layer shown as "Raster", as it did before.
            self._filtered_rows = [
                row
                for row in self._all_rows
                if any(
                    search in text.lower()
                    for column, value in enumerate(row)
                    for text in (str(value), self._cell_label(column, value))
                )
            ]
        else:
            self._filtered_rows = list(self._all_rows)
        if (
            self._sort is not None
            and self._sort[0] in self._detail_columns
            and any(self._pending(row) for row in self._filtered_rows)
        ):
            # After a reload that column is pending again: the arrow stayed
            # on it while the rows sat in name order.
            self._sort = None
        if self._sort is not None:
            column, descending = self._sort

            def sort_key(row):
                # As shown: a cached layer sorts by its name, not its workspace.
                value = row[column] if column < len(row) else None
                return (
                    "" if value is None else self._cell_label(column, value)
                ).casefold()

            self._filtered_rows.sort(key=sort_key, reverse=descending)
        self._show_sort_indicator()
        self._current_page = 0
        self._show_page()

    def _on_header_clicked(self, column):
        """Sort the rows by this column; a second click reverses the order."""
        if self._row_actions and column == self.resultsTable.columnCount() - 1:
            # Not sortable: put the indicator back where _sort says, after Qt
            # moved it to the section clicked, and stay on this page.
            self._show_sort_indicator()
            return
        if column in self._detail_columns and not self._complete_rows(
            self._filtered_rows
        ):
            # Sorting on a column needs every row's value. Qt moved the arrow.
            self._show_sort_indicator()
            return
        same = self._sort is not None and self._sort[0] == column
        self._sort = (column, same and not self._sort[1])
        # Re-rendering also puts the indicator back where _sort says, after Qt
        # moved it to the section that was clicked.
        self._apply_filter()

    def _show_sort_indicator(self):
        """Draw the header arrow for _sort, or none."""
        header = self.resultsTable.horizontalHeader()
        header.setSortIndicatorShown(self._sort is not None)
        if self._sort is not None:
            column, descending = self._sort
            order = (
                Qt.SortOrder.DescendingOrder
                if descending
                else Qt.SortOrder.AscendingOrder
            )
            header.setSortIndicator(column, order)

    def _cell_label(self, column, value):
        """What a cell shows for the value its row holds."""
        if value is None:
            return "-"
        if value == GLOBAL:
            return global_label()
        shown = self._cell_display.get(column)
        return shown(str(value)) if shown else str(value)

    @property
    def _total_pages(self):
        """Total number of pages for the current filtered rows."""
        return max(
            1, (len(self._filtered_rows) + self._page_size - 1) // self._page_size
        )

    def _show_page(self):
        """Render the current page of _filtered_rows into the table."""
        total = len(self._filtered_rows)
        start = self._current_page * self._page_size
        end = min(start + self._page_size, total)
        page_rows = self._filtered_rows[start:end]
        # A selection is a row position: kept, it moved to other resources.
        self.resultsTable.clearSelection()

        data_col_count = self.resultsTable.columnCount()
        if self._row_actions:
            data_col_count -= 1  # last column is for action buttons

        self.resultsTable.setRowCount(len(page_rows))
        for row_idx, values in enumerate(page_rows):
            for col, val in enumerate(values):
                text = self._cell_label(col, val)
                item = QTableWidgetItem(text)
                # "(global)" in a Workspace column has nowhere to go, so it is
                # not drawn as a link (the click skips it too).
                is_link = self._cell_click_callback(col) is not None and (
                    col == 0 or scope("-" if val is None else str(val)) is not None
                )
                if is_link:
                    # Styled as a link; the click itself is handled by
                    # _on_cell_clicked. A real item (not a QPushButton) keeps
                    # the row selectable, so "Delete Selected" works here too.
                    item.setForeground(self.palette().link())
                    font = item.font()
                    font.setUnderline(True)
                    item.setFont(font)
                    item.setToolTip(
                        # Enter opens the row's own resource, not a workspace.
                        self.tr("Click to open (or select and press Enter)")
                        if col == 0
                        else self.tr("Click to open")
                    )
                elif len(text) > _ELIDED_AFTER:
                    # A narrow column cuts it ("EPSG:4326, EPSG:…"): the whole
                    # value on hover.
                    item.setToolTip(text)
                self.resultsTable.setItem(row_idx, col, item)
            if self._row_actions:
                self.resultsTable.setCellWidget(
                    row_idx, data_col_count, self._make_action_widget(values)
                )

        if self._row_actions:
            widgets = (
                self.resultsTable.cellWidget(row, data_col_count)
                for row in range(len(page_rows))
            )
            header = self.resultsTable.horizontalHeader()
            header.resizeSection(
                data_col_count,
                max(
                    [header.sectionSizeHint(data_col_count)]
                    + [widget.sizeHint().width() for widget in widgets if widget]
                ),
            )

        self._fill_details(page_rows)

        # Update pagination controls
        self.lbl_page_number.setText(str(self._current_page + 1))
        self.lbl_page_info.setText(self._page_info_text())

        self.btn_page_first.setEnabled(self._current_page > 0)
        self.btn_page_prev.setEnabled(self._current_page > 0)
        self.btn_page_next.setEnabled(self._current_page + 1 < self._total_pages)
        self.btn_page_last.setEnabled(self._current_page + 1 < self._total_pages)

    def _open_workspace_from_row(self, row_data):
        """The Workspace column links to the workspace: column 1 on every tab.

        Styles and layer groups can live in the global scope, whose label is
        not a workspace, so that one is a dead link rather than an error.
        """
        if scope(row_data[1]) is not None:
            self._show_workspace_info([row_data[1]])

    def _page_info_text(self):
        """The line under the table: which rows are shown, or why there are none."""
        total = len(self._filtered_rows)
        if total == 0:
            return self._empty_state_text()
        start = self._current_page * self._page_size
        return self.tr("Results {} to {} (out of %n item(s))", None, total).format(
            start + 1, min(start + self._page_size, total)
        )

    def _empty_state_text(self):
        """What an empty table should say: why it is empty, and what helps."""
        if self.gs is None:
            return self.tr("Not connected. Press Refresh (F5), or open Settings.")
        search = self.searchBox.text().strip()
        if search and self._all_rows:
            return self.tr("Nothing matches '{}'. Esc clears the filter.").format(
                search
            )
        if self.gs_restricted:
            # Not "Nothing here yet": the server may hold what it does not list.
            return self.tr("Nothing here that this account administers.")
        # isHidden(), not isVisible(): the latter is false for every widget of
        # a window that is not showing yet (invariant 8).
        if not self.btn_add.isHidden() and self.btn_add.text():
            return self.tr("Nothing here yet. Start with '{}' above.").format(
                self.btn_add.text()
            )
        return self.tr("No results")

    def _cell_click_callback(self, col):
        """The callback a click in this column triggers, or None."""
        if col == 0:
            return self._name_click_callback
        header = self.resultsTable.horizontalHeaderItem(col)
        return self._extra_click_callbacks.get(header.text() if header else "")

    def _on_cell_clicked(self, row, col):
        """Open the resource behind a link cell; other cells just select."""
        callback = self._cell_click_callback(col)
        if callback is None or not self._require_connection():
            return
        index = self._current_page * self._page_size + row
        if index >= len(self._filtered_rows):
            return
        # Read once: a reload during _addressable's wait replaces the rows.
        row_data = self._filtered_rows[index]
        if self._addressable([row_data]):
            callback(row_data)

    def _make_action_widget(self, row_data):
        """Keep frequent actions visible and give secondary actions readable labels."""
        widget = QWidget(self.resultsTable)
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)
        size = max(30, widget.fontMetrics().height() + 10)
        self.resultsTable.verticalHeader().setDefaultSectionSize(size + 4)
        quick_ids = {"add-to-qgis", "preview-map", "browse-resources", "publish-layer"}
        destructive_ids = {"delete", "clear-cache", "remove-cache"}
        secondary = []
        for action in self._row_actions:
            icon_name, label, callback = action[:3]
            tooltip = action[3] if len(action) > 3 else label
            if icon_name not in quick_ids or layout.count() >= 2:
                secondary.append(action)
                continue
            btn = QPushButton(widget)
            btn.setProperty("resourceIcon", icon_name)
            btn.setIcon(icon(icon_name, widget.palette()))
            btn.setIconSize(QSize(20, 20))
            btn.setAccessibleName(label)
            btn.setAccessibleDescription(tooltip)
            btn.setToolTip(tooltip)
            btn.setFixedSize(size, size)
            btn.clicked.connect(
                lambda _checked=False, cb=callback, row=row_data: (
                    self._require_connection() and self._addressable([row]) and cb(row)
                )
            )
            layout.addWidget(btn)
        if secondary:
            menu = QMenu(widget)
            menu.setAttribute(Qt.WidgetAttribute.WA_WindowPropagation, True)
            menu.setToolTipsVisible(True)
            ordinary = [a for a in secondary if a[0] not in destructive_ids]
            destructive = [a for a in secondary if a[0] in destructive_ids]
            for action in ordinary + destructive:
                if ordinary and destructive and action is destructive[0]:
                    menu.addSeparator()
                icon_name, label, callback = action[:3]
                entry = menu.addAction(
                    icon(icon_name, menu.palette(), for_menu=True), label
                )
                entry.setProperty("resourceIcon", icon_name)
                entry.setToolTip(action[3] if len(action) > 3 else label)
                entry.triggered.connect(
                    lambda _checked=False, cb=callback, row=row_data: (
                        self._require_connection()
                        and self._addressable([row])
                        and cb(row)
                    )
                )
            menu.aboutToShow.connect(lambda: self._refresh_action_menu(menu))
            btn = QPushButton(
                self.tr("More") if layout.count() else self.tr("Actions"), widget
            )
            btn.setObjectName("rowActionMenu")
            btn.setMenu(menu)
            btn.setMinimumHeight(size)
            description = self.tr("Actions for {}").format(row_data[0])
            btn.setAccessibleName(description)
            btn.setToolTip(description)
            layout.addWidget(btn)
        for button in widget.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.installEventFilter(self)
            self._style_action_button(button, widget.palette(), selected=False)
        return widget

    def eventFilter(self, watched, event):  # noqa: N802 (Qt's own spelling)
        # Row actions are never dialog defaults. Enter still activates the
        # focused control, without stealing Enter from search or the table.
        if (
            isinstance(watched, QPushButton)
            and event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        ):
            watched.click()
            return True
        return super().eventFilter(watched, event)

    @staticmethod
    def _style_action_button(button, palette, selected):
        # Resolve colours before applying QSS: palette() in a stylesheet can
        # retain the old theme, or the selected foreground after deselection.
        def colour(role):
            return palette.color(role).name()

        text = colour(
            QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
        )
        hover = colour(
            QPalette.ColorRole.Highlight
            if selected
            else QPalette.ColorRole.AlternateBase
        )
        border = colour(
            QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Mid
        )
        focus = colour(
            QPalette.ColorRole.HighlightedText
            if selected
            else QPalette.ColorRole.Highlight
        )
        pressed = colour(
            QPalette.ColorRole.Highlight if selected else QPalette.ColorRole.Button
        )
        disabled = palette.color(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text
        ).name()
        sheet = (
            f"QPushButton {{ background: transparent; color: {text};"
            " border: 1px solid transparent; border-radius: 4px; padding: 3px; }"
            "QPushButton#rowActionMenu { padding-right: 16px; }"
            "QPushButton::menu-indicator { subcontrol-position: right center; right: 4px; }"
            f"QPushButton:hover {{ background: {hover}; border-color: {border}; }}"
            f"QPushButton:focus {{ border: 2px solid {focus}; padding: 2px; }}"
            f"QPushButton:pressed {{ background: {pressed}; }}"
            f"QPushButton:disabled {{ color: {disabled}; }}"
        )
        if button.styleSheet() != sheet:
            button.setStyleSheet(sheet)

    @staticmethod
    def _refresh_action_menu(menu):
        for action in menu.actions():
            icon_name = action.property("resourceIcon")
            if icon_name:
                action.setIcon(icon(icon_name, menu.palette(), for_menu=True))

    def changeEvent(self, event):  # noqa: N802 (Qt's own spelling)
        """Recolour icons and the status after a theme change, keeping the data."""
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            if hasattr(self, "_icon_refresh_timer"):
                # Let the new palette propagate to children first. A styled
                # sidebar can have a different palette from the dialog.
                self._icon_refresh_timer.start(0)

    def _refresh_icons(self):
        """Use each widget's effective colours, retaining rows and selection."""
        for index, (_label, icon_name, _loader) in enumerate(self.TABS):
            item = self.navList.item(index)
            if item is not None:
                item.setIcon(icon(icon_name, self.navList.palette()))
        self._refresh_action_icons()
        # Its colour was the old theme's: Night Mapping's green was 2.4:1.
        self._paint_status()

    def _refresh_action_icons(self):
        # QPushButton does not inherit the table row's Selected icon mode.
        # Use its highlight foreground explicitly so small arrows stay visible.
        selected = {
            index.row() for index in self.resultsTable.selectionModel().selectedRows()
        }
        column = self.resultsTable.columnCount() - 1
        for row in range(self.resultsTable.rowCount()):
            widget = self.resultsTable.cellWidget(row, column)
            if widget is None:
                continue
            palette = self.resultsTable.palette()
            for menu in widget.findChildren(QMenu):
                self._refresh_action_menu(menu)
            for button in widget.findChildren(QPushButton):
                self._style_action_button(button, palette, selected=row in selected)
                icon_name = button.property("resourceIcon")
                if icon_name:
                    button.setIcon(icon(icon_name, palette, selected=row in selected))

    # -- Pagination slots --------------------------------------------------

    def _page_first(self):
        self._current_page = 0
        self._show_page()

    def _page_prev(self):
        if self._current_page > 0:
            self._current_page -= 1
            self._show_page()

    def _page_next(self):
        if self._current_page + 1 < self._total_pages:
            self._current_page += 1
            self._show_page()

    def _page_last(self):
        self._current_page = self._total_pages - 1
        self._show_page()

    # GeoServer's payload shapes, in one place (toolbelt/payload.py); the
    # class attributes win over any copy a mixin still carries.
    _name_of = staticmethod(name_of)
    _unwrap = staticmethod(unwrap)
    _as_list = staticmethod(as_list)

    def _yes_no(self, value):
        """A boolean cell: Yes / No, translated, never Python's True / False."""
        if isinstance(value, str):
            value = value.strip().lower() == "true"
        return self.tr("Yes") if value else self.tr("No")

    # -- Details, page by page ------------------------------------------

    def _pending(self, row):
        """True while a row's detail cells are not fetched yet."""
        return any(
            column < len(row) and row[column] == PENDING
            for column in self._detail_columns
        )

    def _apply_details(self, rows, results):
        """Write fetched cells into the rows, in place. Returns the failures.

        The row lists are shared by _all_rows, _filtered_rows and the row
        actions' closures, so each of them sees the filled row.
        """
        failures = []
        for row, (cells, error) in zip(rows, results):
            if error is not None:
                # Named as the tabs name them: ws:name, bare when global or
                # when the name is qualified already (the tile cache's).
                ws = scope(row[1]) if len(row) > 1 else None
                label = row[0] if ws is None or ":" in row[0] else f"{ws}:{row[0]}"
                failures.append((label, error))
                cells = ("-",) * len(self._detail_columns)
            for column, value in zip(self._detail_columns, cells):
                row[column] = value
        return failures

    def _repaint_details(self):
        """Refresh the visible detail cells, keeping the selection."""
        start = self._current_page * self._page_size
        for index in range(self.resultsTable.rowCount()):
            if start + index >= len(self._filtered_rows):
                break
            row = self._filtered_rows[start + index]
            for column in self._detail_columns:
                item = self.resultsTable.item(index, column)
                if item is not None and column < len(row):
                    text = self._cell_label(column, row[column])
                    item.setText(text)
                    # As _show_page does: a gridset list cut by its column
                    # had no hover text when it landed here.
                    item.setToolTip(text if len(text) > _ELIDED_AFTER else "")

    def _fill_details(self, rows):
        """Fetch the pending detail cells of these rows in the background.

        A tab used to GET every item for its summary columns before showing
        a single row: 10,000 requests for 200 workspaces of 50 stores, again
        on every tab switch. The names come first, then only the page shown.
        """
        todo = [row for row in rows if self._pending(row)]
        if not todo or self._row_detail is None or self.gs is None:
            # No client during a Refresh: the rows were read as failed, and
            # a warning listed them. The load that follows fills them.
            return
        if self._detail is not None:
            self._detail.cancel()  # a newer page, sort or filter: this one wins
        generation, detail = self._table_generation, self._row_detail

        def work(task):
            return self._fan_out(detail, todo, task)

        def landed(results):
            if generation != self._table_generation:
                return  # another tab's table now
            self._report_partial_failures(self._apply_details(todo, results))
            self._repaint_details()

        self._detail_rows = todo
        self._launch_task(
            "_detail",
            self.tr("Failed to load the details"),
            work,
            landed,
            lambda task: None,
            quiet=True,
        )

    def _complete_rows(self, rows):
        """Fetch the pending cells of these rows now, waiting. False on failure.

        What a row action or a sort on a detail column needs: the
        row's full values, not the marker.
        """
        todo = [row for row in rows if self._pending(row)]
        if not todo or self._row_detail is None:
            return True
        if not self._require_connection():
            return False
        fetching = {id(row) for row in todo}
        took_over = self._detail is not None and all(
            id(row) in fetching for row in self._detail_rows
        )
        if took_over:
            # The page's fill is after these rows too (a sort): GET them once.
            self._detail.cancel()
        detail, stop = self._row_detail, threading.Event()
        # The waiting box's Cancel starts no more GETs (a dropped sort read every row).
        results = self._fetch(
            lambda: self._fan_out(detail, todo, _Stop(stop)),
            self.tr("Failed to load the details"),
            stop=stop,
        )
        if results is None:
            if took_over and (self._detail is None or self._detail.isCanceled()):
                # The page shown still needs the cells its fill was after.
                start = self._current_page * self._page_size
                self._fill_details(self._filtered_rows[start : start + self._page_size])
            return False
        self._report_partial_failures(self._apply_details(todo, results))
        self._repaint_details()
        return True

    def _addressable(self, rows):
        """True unless a row's name cannot go into a REST path; then say so.

        A resource made elsewhere (the web UI, REST) can be called "a#b", and
        `requests` sends its path as ".../a": deleting "a#b" deleted "a" and
        reported success, and its edit form showed "a". So the rows are
        refused, as the Add forms refuse such names.
        Refused, not quoted; the library builds paths from raw names
        (issue #1 row 50), and quoting only the plugin's side would double-
        quote wherever it already does. Quote everywhere once the library does.

        A row whose details are still pending gets them first: its actions
        read them (the Layers tab's store and type).
        """
        if not self._complete_rows(rows):
            return False
        for row in rows:
            for column in self._path_columns:
                value = row[column] if column < len(row) else None
                if isinstance(value, str) and any(c in value for c in _UNSAFE_IN_NAMES):
                    self.show_warning_message(
                        self.tr(
                            "'{}' has a '/', '?', '#' or '%' in its name, so the "
                            "plugin cannot address it. Rename it in GeoServer's web "
                            "interface to manage it here."
                        ).format(value)
                    )
                    return False
        return True

    def _require_safe_name(self, name):
        """Refuse a name the REST paths cannot carry, before it reaches them.

        `/`, `?`, `#` and `%` change what a URL means. `requests` sends
        `datastores/a#b.json` as `datastores/a`, a different resource, and
        GeoServer itself does not stop them. Every Add form calls this first.
        """
        if not name or name != name.strip() or any(c in name for c in _UNSAFE_IN_NAMES):
            raise ValueError(
                self.tr(
                    "'{}' is not a valid name: it has a '/', '?', '#', '%', or a "
                    "space at the start or the end."
                ).format(name)
            )

    @staticmethod
    def _error_text(error):
        """One line for the user, including GeoServer's own explanation.

        The library calls raise_for_status(), and HTTPError stringifies to
        "500 Server Error:  for url: …", dropping the body, which is exactly
        where GeoServer puts the reason ("Unable to delete layer referenced by
        layer group 'tasmania'"). TODO(#1): a library that raised with the
        body would make this unnecessary.
        """
        # By name: requests raises its own subclass, of simplejson's when that
        # is installed, which is not json's.
        if isinstance(error, ValueError) and type(error).__name__ == "JSONDecodeError":
            # A .json() on a sign-in page, which a proxy or an SSO answers
            # with 200 once the session expires: "Expecting value: line 1
            # column 1 (char 0)" said nothing about it.
            return QCoreApplication.translate(
                "GeoServerMainDialog",
                "GeoServer answered with something that is not its REST API "
                "(a sign-in page?)",
            )
        response = getattr(error, "response", None)
        # One line, markup reduced to its title: a Tomcat stack trace is not an
        # explanation, and an XML error document still says something.
        summary = summarise_body(getattr(response, "text", ""))
        status = getattr(response, "status_code", None)
        if status is not None:
            # "HTTP 500: the reason", not "500 Server Error:  for url: <the
            # whole request URL>": the URL is noise in a banner, and the log
            # line carries the same text.
            return f"HTTP {status}: {summary}" if summary else f"HTTP {status}"
        if summary and summary not in str(error):
            return f"{error}: {summary}"
        return str(error)

    def _report_failure(self, failure_message, error):
        """A failed action: the same line in the banner and in the QGIS log."""
        detail = self._error_text(error)
        self.show_error_message(f"{failure_message}: {detail}")
        self.log(f"{failure_message}: {detail}", log_level=Qgis.MessageLevel.Critical)

    def _require_connection(self):
        """True when there is a client to talk to; otherwise say so and refuse.

        A loaded table outlives the connection it came from: refresh_ui()
        clears self.gs the moment it starts and the probe that sets it again
        runs in a QgsTask, so for that window (up to the probe's 10 s timeout against a
        server that has gone away) the rows and their buttons are still on
        screen and clickable. Every user-triggered action passes through here,
        which is why the check lives where actions are dispatched (the header
        buttons, the row actions, the link cells, Enter and Del; invariant 10)
        rather than in each of the methods behind them.
        """
        if self.gs is not None:
            return True
        self.show_warning_message(
            self.tr("Not connected to GeoServer. Press Refresh (F5) to connect.")
        )
        return False

    def _partly_saved(self, action, done):
        """Run what follows a step that already changed the server.

        What it raises becomes PartlySaved: `done` says what is saved, the
        error what was not, and _run_action warns and reloads.
        """
        try:
            action()
        except (Abandoned, PartlySaved):
            raise
        except Exception as error:
            raise PartlySaved(f"{done}: {self._error_text(error)}") from error

    def _run_action(self, action, failure_message):
        """Run a server action under a wait cursor and report if it fails.

        For the inline work that is left: a form's checks and export before an
        upload, a small edit. On an exception the message bar gets
        "<failure_message>: <error>", the QGIS log gets the same, and False
        comes back so the caller can stop.
        """
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            action()
            return True
        except PartlySaved as e:
            self.show_warning_message(str(e))
            self.log(str(e), log_level=Qgis.MessageLevel.Warning)
            self._reload_current_tab()
            return False
        except Abandoned as abandoned:
            # The user pressed Cancel: they know. A save goes on regardless.
            if abandoned.write:
                self.show_warning_message(
                    self.tr(
                        "Stopped waiting. GeoServer can still apply the change: the "
                        "tab reloads when it answers."
                    )
                )
            return False
        except Exception as e:
            self._report_failure(failure_message, e)
            return False
        finally:
            self.unsetCursor()

    def _fetch(self, action, failure_message, in_worker=True, stop=None):
        """_run_action for reads: return the value, or None after reporting.

        The read runs in a worker thread (see _wait_for), so a server that
        stopped answering cannot freeze QGIS. `in_worker=False` is for work
        on a live QGIS layer, which must stay on the GUI thread (invariant 9).
        """
        result = []
        run = (
            (lambda fn: self._wait_for(fn, stop=stop))
            if in_worker
            else (lambda fn: fn())
        )
        if self._run_action(lambda: result.append(run(action)), failure_message):
            return result[0]
        return None

    def _wait_for(self, action, write=False, stop=None):
        """Run action() in a worker thread and return its value, or raise its error.

        A fast answer returns under the wait cursor, as before. After
        _WAIT_BEFORE_BOX a modal "Waiting for GeoServer" box with Cancel
        appears, and the GUI thread keeps processing events until the read
        lands. The box being application-modal is what makes the nested event
        loop safe: no click can reach the dialog, a form or QGIS while a read
        is outstanding, so nothing can start a second one or clear `self.gs`.
        Cancel raises Abandoned. The request itself runs to the library's
        own timeout and its answer is dropped. `stop`, a threading.Event, is
        set on Cancel for work that can stop early (a fan-out). `write` marks
        a save: it still lands after a Cancel, so _run_action says so and the
        tab reloads once the thread ends, showing whether it did; until then
        a _SaveWatch keeps QGIS from quitting before it ends.

        A QObject result (a map layer) is moved to the GUI thread before it
        is handed back, because a layer built in a worker belongs to it.
        One thread per read; a pool only if reads ever overlap.
        """
        outcome = {}
        done = threading.Event()

        def work():
            try:
                value = action()
                if isinstance(value, QObject):
                    value.moveToThread(QCoreApplication.instance().thread())
                outcome["value"] = value
            except Exception as e:  # re-raised on the GUI thread, below
                outcome["error"] = e
            finally:
                # Here, not on finished: that one waits for the event loop.
                thread.write = False
                done.set()

        abandoned = threading.Event()
        thread = _ReadThread(work, write)
        if write:
            # Before start(): connected on Cancel, it missed a save just ended.
            thread.finished.connect(
                lambda: abandoned.is_set()
                and not sip.isdeleted(self)
                and self._reload_current_tab()
            )
        thread.start()
        if not done.wait(_WAIT_BEFORE_BOX):
            box = QProgressDialog(
                self.tr("Waiting for GeoServer…"),
                self.tr("Cancel"),
                0,
                0,
                # A form's own read must block the form, not only this dialog.
                QApplication.activeModalWidget() or self,
            )
            box.setWindowTitle(__title__)
            box.setWindowModality(Qt.WindowModality.ApplicationModal)
            box.setMinimumDuration(0)
            box.show()
            try:
                while not done.wait(0.05):
                    QCoreApplication.processEvents()
                    # A result that landed meanwhile is kept, Cancel or not.
                    if box.wasCanceled() and not done.is_set():
                        if stop is not None:
                            stop.set()
                        abandoned.set()
                        if write:
                            QgsApplication.taskManager().addTask(_SaveWatch(done))
                        raise Abandoned(write)
            finally:
                box.close()
                box.deleteLater()
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

    def _form_check(self, check):
        """A form's `validate`: check(values) behind the waiting box, before
        the form closes.

        A refusal (a name that is taken) then stays in the form, with what
        was typed: checked after it closed, a whole PostGIS form had to be
        typed again, its password included. check() only reads; the save
        runs after the form closed, as before, and checks again.
        """

        def validate(values):
            try:
                self._wait_for(lambda: check(values))
            except Abandoned:
                raise
            except Exception as error:
                # A check's refusal keeps its words; a sign-in page's .json() not.
                if (
                    isinstance(error, ValueError)
                    and type(error).__name__ != "JSONDecodeError"
                ):
                    raise
                raise ValueError(self._error_text(error)) from error

        return validate

    def _wait_for_save(self, action):
        """_wait_for for a write: after a Cancel it still lands, and says so."""
        return self._wait_for(action, write=True)

    def _raw_rest(self, method, path, **kwargs):
        """Call the REST client directly for what geoservercloud has no method for.

        Raises with GeoServer's own response body on any HTTP error, so the
        message the user sees is the same shape as _check's. Every caller is a
        library gap: list it in issue #1 and mark the call site TODO(#1).
        A worker thread calls `toolbelt.rest.raw_rest` with the client it was
        handed instead: `self.gs` is not its to read.
        """
        return raw_rest(self.gs.rest_service.rest_client, method, path, **kwargs)

    def _resource_exists(self, getter, *args):
        """True when a GET for the resource returns 200, False on 404.

        The library's create_* calls are upserts (POST, then PUT on conflict),
        so an "Add" form has to refuse a name that is already taken, otherwise
        it silently overwrites a live resource and reports success. TODO(#1):
        an exist_ok=False option upstream would make this unnecessary.
        """
        _, status_code = getter(*args)
        return status_code == 200

    def _fetch_list(self, api_method, *args):
        """Call a geoservercloud list endpoint and return the list.

        Raises on HTTP errors and on a payload that is not a list. A proxy
        login page or an error document must surface, not render as an empty
        table.
        """
        result = self._check(api_method(*args))
        if not isinstance(result, list):
            # A sign-in page came out as its whole markup in the banner.
            raise RuntimeError(
                QCoreApplication.translate(
                    "GeoServerMainDialog", "Unexpected response (not a JSON list): {}"
                ).format(summarise_body(str(result)))
            )
        return result

    def _get_workspace_names(self):
        """Workspace names for combo boxes, fetched from the server every time.

        Deliberately not cached: a cache here survived a Refresh on the
        Workspaces tab, so a workspace created in the web UI showed in the list
        but not in the datastore form's combo. One GET per dialog open is
        cheaper than a stale picker.
        """
        return [self._name_of(ws) for ws in self._fetch_list(self.gs.get_workspaces)]

    def _scoped_names(self, list_global, list_in, workspace_names, task=None):
        """(name, workspace label) pairs of a resource that lives globally or
        in a workspace: the global names first, listed by `list_global()`,
        then each workspace's, listed in parallel by `list_in(ws)`. A listing
        that fails is a failure beside the names, not the end of the listing:
        the global one too, which a rest.properties rule may refuse alone. So
        is a workspace named GLOBAL: its rows would be the global ones, and
        act on them. In a worker."""
        pairs, failures = [], []
        try:
            pairs = [(name, GLOBAL) for name in list_global()]
        except Exception as error:  # reported beside the names, as a workspace
            failures.append((global_label(), error))
        if GLOBAL in workspace_names:
            workspace_names = [ws for ws in workspace_names if ws != GLOBAL]
            failures.append(
                (
                    GLOBAL,
                    ValueError(
                        f"a workspace named {GLOBAL} reads as the global scope; "
                        "rename it on the Workspaces tab to manage it here"
                    ),
                )
            )
        for ws_name, (items, error) in zip(
            workspace_names, self._fan_out(list_in, workspace_names, task)
        ):
            if error is not None:
                failures.append((ws_name, error))
                continue
            pairs.extend((self._name_of(item), ws_name) for item in items)
        return pairs, failures

    @staticmethod
    def _fan_out(fn, items, task=None):
        """Run fn(item) for every item on a small thread pool.

        Returns [(result, error)] in input order. A worker that raises yields
        (None, exception) instead of aborting the whole listing, so one broken
        workspace cannot blank the table. Only stateless REST reads belong
        here: GeoServerCloud.wms / .wmts are shared state. At most
        _MAX_PARALLEL_REQUESTS items run at once in the whole plugin, so fn
        must not fan out itself.

        Given the running task, each finished item reports progress, and once
        it is cancelled an item not started yet is not run: it yields (None,
        CancelledError()). The requests already in flight still run
        to the end. Cancelling means "start no more", not "abort the sockets".
        """

        def cancelled():
            return task is not None and task.isCanceled()

        def guarded(item):
            # Asked per item, not per result: a slow first result hid the cancel.
            while not _REQUEST_SLOTS.acquire(timeout=0.05):
                if cancelled():
                    return (None, CancelledError())
            try:
                if cancelled():
                    return (None, CancelledError())
                return (fn(item), None)
            except Exception as e:  # reported by the caller, per item
                return (None, e)
            finally:
                _REQUEST_SLOTS.release()

        results = [None] * len(items)
        with ThreadPoolExecutor(max_workers=_MAX_PARALLEL_REQUESTS) as pool:
            index_of = {pool.submit(guarded, item): n for n, item in enumerate(items)}
            # As they finish, not in order: a slow first item held the bar at 0.
            for done, future in enumerate(as_completed(index_of), start=1):
                results[index_of[future]] = future.result()
                if task is not None:
                    # A fetch with several fan-outs sweeps the
                    # progress bar once per stage; the task bar can live with it.
                    task.setProgress(100 * done / len(items))
        return results

    @staticmethod
    def _wire_picker(dlg, picker, first, load):
        """A viewer's picker: choosing an entry fills the form with its details.

        `picker` is the key of the combo; `load(name)` returns the entry's
        values, or None once a failed read is reported, which leaves the
        fields as they are rather than blank ones claiming "Enabled: Yes".
        """

        def show(name):
            values = load(name) if name else None
            if values is not None:
                dlg.set_values(values)

        dlg.get_widget(picker).currentTextChanged.connect(show)
        show(first)

    def _warn_if_store_unreachable(self, name, read):
        """After a store was saved: make GeoServer open it, and say so if it cannot.

        GeoServer stores whatever URL, path or password it is given and only
        fails when a layer is listed or drawn. `read()` is a cheap GET that
        forces the connection (a store's unpublished tables, coverages or
        remote layers); it runs in the worker behind the waiting box.
        """
        try:
            self._wait_for(read)
        except Abandoned:
            return
        except Exception as error:  # the reason is what the user needs
            self.show_warning_message(
                self.tr("'{}' was saved, but GeoServer cannot read it: {}").format(
                    name, self._error_text(error)
                )
            )

    def _report_partial_failures(self, failures):
        """One warning banner for the items a listing could not fetch.

        :param failures: list of (label, exception); details go to the QGIS log.
        """
        if not failures:
            return
        for label, error in failures:
            # With GeoServer's reason: an HTTPError alone says only the URL.
            self.log(
                f"Could not list {label}: {self._error_text(error)}",
                log_level=Qgis.MessageLevel.Warning,
            )
        shown = ", ".join(label for label, _ in failures[:5])
        if len(failures) > 5:
            shown += ", …"
        self.show_warning_message(
            self.tr(
                "%n item(s) could not be listed: {names}. Details in the "
                "QGIS log (GeoServer Manager tab).",
                None,
                len(failures),
            ).format(names=shown)
        )

    @staticmethod
    def _one_or_many(one, many):
        """labels -> a whole sentence: one.format(the label), or many(count).

        The tab translates both with the name or the count in place ("Delete
        workspace '{}'?", "%n workspace(s) deleted."). Glued here from "delete",
        "workspace" and the name, a French sentence could not agree its words.
        """
        return lambda labels: (
            one.format(labels[0]) if len(labels) == 1 else many(len(labels))
        )

    def _confirm_delete(self, question, labels=(), cascade=""):
        """Ask before acting on resources; True when the user said yes.

        :param question: the whole sentence, translated by the tab.
        :param labels: the resources; for more than one, listed after it.
        :param cascade: what else the action takes with it. Both delete
            paths send recurse=true, so the user has to be told.
        """
        if len(labels) > 1:
            question += "\n\n" + "\n".join(f"  • {label}" for label in labels)
        # The separator lives here, so a translation cannot glue the sentences.
        parts = [question, cascade.strip(), self.tr("This action cannot be undone.")]
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            self.tr("Confirm"),
            "\n\n".join(part for part in parts if part),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            self,
        )
        # Qt guessed rich text from a name like "a<b>c", and named another one.
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.exec()
        confirmed = box.clickedButton() == box.button(QMessageBox.StandardButton.Yes)
        box.deleteLater()
        return confirmed

    def _delete_many(self, labeled_deletes, reload_fn, ask, done, cascade=""):
        """Confirm and run one or more deletions in a task, then reload the table.

        :param labeled_deletes: list of (label, zero-arg callable) pairs.
        :param reload_fn: called afterwards to refresh the table.
        :param ask: labels -> the confirmation's question, and `done`: labels
            -> the success banner, both whole sentences from _one_or_many().
        :param cascade: sentence naming what else goes, for the confirmation.

        The requests run off the GUI thread with progress and Cancel: fifty
        workspaces with recurse=true are minutes, not a wait cursor.
        """
        if not labeled_deletes:
            return
        if self._delete is not None:
            self.show_warning_message(
                self.tr("A delete is already running. Wait for it or cancel it.")
            )
            return
        labels = [label for label, _ in labeled_deletes]
        if not self._confirm_delete(ask(labels), labels, cascade):
            return

        # Shared with cancelled(): a Cancel after a failure used to drop it.
        errors = []

        def delete_all(task):
            for index, (label, delete_fn) in enumerate(labeled_deletes):
                if task is not None and task.isCanceled():
                    break
                try:
                    delete_fn()
                except Exception as e:
                    detail = self._error_text(e)
                    errors.append((label, detail))
                    # Logged here: a batch that ends after Close shows no banner.
                    self.log(f"{label}: {detail}", log_level=Qgis.MessageLevel.Critical)
                if task is not None:
                    task.setProgress(100 * (index + 1) / len(labeled_deletes))
            return errors

        def listed(errors):
            return "\n".join(f"{label}: {detail}" for label, detail in errors)

        def report(errors):
            if errors:
                self.show_error_message(
                    self.tr("These failed:\n{errors}").format(errors=listed(errors))
                )
            else:
                self.show_success_message(done(labels))
            reload_same_tab()

        def cancelled(_task):
            if errors:
                self.show_error_message(
                    self.tr(
                        "Cancelled; what was already done stays done. These "
                        "failed:\n{errors}"
                    ).format(errors=listed(errors))
                )
            else:
                self.show_warning_message(
                    self.tr("Cancelled. What was already done stays done.")
                )
            reload_same_tab()

        # The user may switch tabs while it runs; that tab loaded itself, and
        # this tab's loader would paint its rows under the other one's header.
        started_on = self.navList.currentRow()

        def reload_same_tab():
            # Not while a Refresh probes: a load would cancel the probe, and
            # the dialog stayed on "Connecting…" for good. Its landing loads.
            if self.navList.currentRow() == started_on and self.gs is not None:
                reload_fn()

        self._launch_task(
            "_delete",
            self.tr("The batch stopped"),
            delete_all,
            report,
            cancelled,
            busy_text=self.tr("Working…"),
        )

    # -- Dialog actions ----------------------------------------------------

    def _edit_credentials(self):
        """Open the plugin's settings page."""
        if self.iface:
            self.hide()
            self.iface.showOptionsDialog(currentPage=f"mOptionsPage{__title__}")
            self.refresh_ui(show_message=True)
            self.show()

    # -- Uploads shared by the tabs ------------------------------------------

    def _upload_slot_free(self):
        """True when an upload can start; else say so and return False.

        Callers check this *before* exporting: an export can take minutes and
        a refusal after it would leave the exported file behind.
        """
        if self._upload is None:
            return True
        self.show_warning_message(
            self.tr("An upload is already running. Wait for it or cancel it.")
        )
        return False

    def _upload_file(
        self,
        failure_message,
        client,
        url,
        source,
        params,
        headers,
        on_success,
        on_cancel,
        folder=None,
        after=None,
        on_done=None,
    ):
        """Stream one file to a REST path through _run_upload.

        Everything a worker needs is an argument (invariant 9): the client
        held now, the URL, the path of the file. `folder`, when given, is the
        temporary folder holding `source` and is removed however the upload
        ends, including a refusal because another upload runs. `after(client)`
        runs in the worker once the PUT succeeded (a metadata PUT). Returns
        False when nothing was started.
        """
        import shutil

        from geoserver_manager.toolbelt.rest import ProgressReader

        if self._upload is not None:
            if folder is not None:
                shutil.rmtree(folder, ignore_errors=True)
            return self._upload_slot_free()

        def work(task):
            try:
                with open(source, "rb") as handle:
                    body = ProgressReader(
                        handle,
                        source.stat().st_size,
                        on_progress=task.setProgress if task is not None else None,
                        is_cancelled=task.isCanceled if task is not None else None,
                    )
                    raw_rest(
                        client, "put", url, params=params, data=body, headers=headers
                    )
                if task is not None:
                    # The file is stored: a Cancel from now on comes too late,
                    # and a failing `after` is a PartlySaved, not a failure.
                    task.completed = True
                if after is not None:
                    after(client)
            finally:
                if folder is not None:
                    shutil.rmtree(folder, ignore_errors=True)

        def ended(outcome):
            # A task cancelled before it started never ran work's finally.
            if folder is not None:
                shutil.rmtree(folder, ignore_errors=True)
            if on_done is not None:
                on_done(outcome)

        return self._run_upload(
            failure_message, work, on_success, on_cancel, on_done=ended
        )

    def _report_cancelled_upload(self, kind, tab, exists, name):
        """Say what a cancelled upload left behind, measured on 2.28.5.

        An aborted first upload leaves nothing: no store, no file, whatever
        was already sent. An aborted *Replace* keeps the store, its layer and
        its configuration, but GeoServer has already deleted the previous
        file (a layer with no data behind it), so that one is a warning with
        the way out.

        :param kind: "datastore" / "coverage store", translated by the caller.
        :param tab: the tab to look at, translated by the caller.
        :param exists: zero-arg callable saying whether the store is there;
            anything it raises (a Refresh dropped the client) reads as unknown.
        """
        try:
            # Off the GUI thread: the user may have cancelled because the
            # server stopped answering, and this read would freeze QGIS.
            kept = self._wait_for(exists)
        except Exception:  # the report must not fail the cancel (or Cancel)
            kept = None
        if kept:
            message = self.tr(
                "Upload of '{name}' cancelled: GeoServer kept the {kind} and its "
                "layer, but had already removed their data file. Upload it again with "
                "Replace ticked, or delete the {kind}."
            )
        elif kept is None:
            message = self.tr(
                "Upload of '{name}' cancelled. Check the {tab} tab for what was left."
            )
        else:
            message = self.tr(
                "Upload of '{name}' cancelled. Nothing was left on the server."
            )
        self.show_warning_message(message.format(name=name, kind=kind, tab=tab))
