#! python3  # noqa E265

"""
A tab lists names first, then fetches the summary columns of the
page on screen only, not every row's before showing any.

Usage from the repo root folder:

.. code-block:: bash

    QT_QPA_PLATFORM=offscreen python -m unittest tests.qgis.test_page_details
"""

import threading
import time
from unittest.mock import patch

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QApplication
from qgis.testing import start_app, unittest

from geoserver_manager.gui.dlg_main import GeoServerMainDialog
from geoserver_manager.gui.scope import PENDING
from tests.qgis.sync_dialog import SyncDialog, ended

start_app()


def spin(seconds):
    """Let the event loop and the task manager run for a while."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)


class TestPageDetails(unittest.TestCase):
    def setUp(self):
        self.dlg = SyncDialog()
        self.dlg.gs = object()  # connected: without a client nothing is filled
        self.asked = []
        self.dlg._setup_table(["Name", "Workspace", "Type"])
        self.dlg._row_detail = lambda row: self.asked.append(row[0]) or (
            f"type of {row[0]}",
        )
        self.dlg._detail_columns = (2,)
        self.rows = [[f"store{n:02}", "topp", PENDING] for n in range(45)]

    def test_only_the_page_shown_is_fetched(self):
        # 10,000 GETs for 200 workspaces of 50 stores, before a single row.
        self.dlg._populate_rows(self.rows)
        self.assertEqual(len(self.asked), self.dlg._page_size)
        self.assertEqual(self.rows[0][2], "type of store00")
        self.assertEqual(self.rows[-1][2], PENDING)
        self.assertEqual(self.dlg.resultsTable.item(0, 2).text(), "type of store00")

    def test_the_next_page_fetches_its_own(self):
        self.dlg._populate_rows(self.rows)
        self.dlg._page_next()
        self.assertEqual(len(self.asked), 2 * self.dlg._page_size)
        self.dlg._page_prev()  # already there: nothing asked again
        self.assertEqual(len(self.asked), 2 * self.dlg._page_size)

    def test_sorting_on_a_detail_column_completes_every_row(self):
        self.dlg._populate_rows(self.rows)
        self.dlg._on_header_clicked(2)
        self.assertEqual(len(self.asked), 45)
        self.assertNotIn(PENDING, [row[2] for row in self.rows])

    def test_a_row_action_gets_the_rows_details_first(self):
        # The Layers tab's actions read the type and the store.
        self.dlg._populate_rows(self.rows)
        last = self.rows[-1]
        self.assertTrue(self.dlg._addressable([last]))
        self.assertEqual(last[2], "type of store44")

    def test_a_fill_for_a_table_that_is_gone_is_dropped(self):
        landed = []

        def launch(slot, message, work, on_success, on_cancel, **kwargs):
            landed.append((work, on_success))

        self.dlg._launch_task = launch
        self.dlg._populate_rows(self.rows)
        (work, on_success) = landed[0]
        results = work(None)
        self.dlg._setup_table(["Name", "Other"])  # the user moved on
        on_success(results)
        self.assertEqual(self.rows[0][2], PENDING)

    def test_nothing_is_fetched_while_a_refresh_has_no_client(self):
        # Paging during F5 read every row as failed: dashes, and a warning
        # listing healthy rows.
        warnings = []
        self.dlg.show_warning_message = warnings.append
        self.dlg.gs = None
        self.dlg._populate_rows(self.rows)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.rows[0][2], PENDING)
        self.assertEqual(warnings, [])

    def test_a_sort_on_a_detail_column_is_dropped_when_its_cells_are_pending(self):
        # After a reload the arrow stayed on the column while the rows sat
        # in name order.
        self.dlg._populate_rows(self.rows)
        self.dlg._on_header_clicked(2)
        self.assertEqual(self.dlg._sort, (2, False))
        reloaded = [[f"store{n:02}", "topp", PENDING] for n in range(45)]
        self.dlg._populate_rows(reloaded)
        self.assertIsNone(self.dlg._sort)

    def test_a_refused_sort_puts_the_arrow_back(self):
        # Qt moves the arrow before the click reaches the dialog: it stayed
        # on the detail column while the rows kept their order.
        self.dlg.show_warning_message = lambda text: None
        self.dlg._populate_rows(self.rows)
        self.dlg._on_header_clicked(0)
        header = self.dlg.resultsTable.horizontalHeader()
        self.dlg.gs = None  # a Refresh is probing: the sort is refused
        header.setSortIndicator(2, Qt.SortOrder.AscendingOrder)  # what Qt does
        self.dlg._on_header_clicked(2)
        self.assertEqual(self.dlg._sort, (0, False))
        self.assertEqual(header.sortIndicatorSection(), 0)

    def hold_the_fills(self):
        """Park each page fill in its slot, running, as a slow server would."""
        fills = []

        class Fill:
            cancelled = False

            def cancel(inner):
                inner.cancelled = True

            def isCanceled(inner):  # noqa: N802
                return inner.cancelled

        def launch(slot, message, work, on_success, on_cancel, **kwargs):
            fills.append(Fill())
            setattr(self.dlg, slot, fills[-1])

        self.dlg._launch_task = launch
        return fills

    def test_a_sort_takes_over_the_page_fill_rather_than_repeat_it(self):
        # The page's rows were fetched twice: by its fill, then by the sort.
        fills = self.hold_the_fills()
        self.dlg._populate_rows(self.rows)
        self.dlg._on_header_clicked(2)
        self.assertTrue(fills[0].cancelled)
        self.assertEqual(sorted(self.asked), [row[0] for row in self.rows])

    def test_a_cancelled_sort_gives_the_page_its_fill_back(self):
        fills = self.hold_the_fills()
        self.dlg._populate_rows(self.rows)
        with patch.object(self.dlg, "_fetch", return_value=None):  # Cancel
            self.dlg._on_header_clicked(2)
        self.assertEqual(len(fills), 2)
        self.assertFalse(fills[1].cancelled)

    def test_a_row_action_leaves_the_page_fill_running(self):
        fills = self.hold_the_fills()
        self.dlg._populate_rows(self.rows)
        self.assertTrue(self.dlg._addressable([self.rows[0]]))
        self.assertFalse(fills[0].cancelled)  # the other rows still need it

    def test_a_long_detail_cell_has_its_whole_text_on_hover(self):
        long_text = "EPSG:4326, EPSG:900913, WebMercatorQuad, GlobalCRS84Pixel"
        self.dlg._row_detail = lambda row: (long_text,)
        self.dlg._populate_rows(self.rows)
        self.assertEqual(self.dlg.resultsTable.item(0, 2).toolTip(), long_text)

    def test_a_row_that_cannot_be_read_gets_dashes_and_one_warning(self):
        warnings = []
        self.dlg.show_warning_message = warnings.append

        def detail(row):
            if row[0] == "store03":
                raise RuntimeError("HTTP 500: boom")
            return ("ok",)

        self.dlg._row_detail = detail
        self.dlg._populate_rows(self.rows)
        self.assertEqual(self.rows[3][2], "-")
        self.assertEqual(len(warnings), 1)
        self.assertIn("topp:store03", warnings[0])


class TestPagingOnASlowServer(unittest.TestCase):
    """The real dialog and task manager: fills that a newer page superseded."""

    def test_paging_keeps_eight_requests_in_flight_and_frees_the_tasks(self):
        # Each flip added eight requests and a QGIS task to those still running.
        dlg = GeoServerMainDialog()
        self.addCleanup(dlg.close)
        dlg.show_warning_message = lambda text: None
        dlg.gs = object()
        release = threading.Event()
        self.addCleanup(release.set)
        lock, running, peak = threading.Lock(), [0], [0]

        def detail(row):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            release.wait(5)  # a server too busy to answer
            with lock:
                running[0] -= 1
            return ("type",)

        dlg._setup_table(["Name", "Workspace", "Type"])
        dlg._row_detail, dlg._detail_columns = detail, (2,)
        dlg._populate_rows([[f"s{n:03}", "topp", PENDING] for n in range(200)])
        fills = [dlg._detail]
        for _page in range(5):
            spin(0.2)
            dlg._page_next()
            fills.append(dlg._detail)
        spin(0.3)
        self.assertLessEqual(peak[0], 8)
        self.assertEqual([ended(fill) for fill in fills[:-1]], [True] * 5)


if __name__ == "__main__":
    unittest.main()
