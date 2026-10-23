# Testing the plugin

Two suites, split by what they need:

| Folder | Needs | What lives there |
| :----- | :---- | :--------------- |
| `tests/unit` | plain Python | the pure helpers: REST payload shapes, SLD sniffing, QGIS export naming, the metadata contract |
| `tests/qgis` | the QGIS Python (headless is fine) | the dialog and every tab, driven through `qgis.testing.start_app()` |

```bash
python -m pip install -U -r requirements/testing.txt
```

## Run them

```bash
python -m pytest tests/unit

QT_QPA_PLATFORM=offscreen python -m pytest tests/qgis
```

`QT_QPA_PLATFORM=offscreen` lets the widget tests run without a display. CI
sets the same variable. CI runs the suite three times: in the `qgis/qgis:3.40`
container (Qt5, the oldest QGIS supported), in `qgis/qgis:4.0` (Qt6) and in
`qgis/qgis:stable`, the latest release. A test that depends on the QGIS version
says so in its skip reason. QGIS
4 ignores `supportsQt6`, so there `test_qgis_compat` checks that the version
range of `metadata.txt` lets it load the plugin. No `qgis/qgis` image has a
Qt6 build of QGIS 3.40 to 3.44, so no job covers those. Without pytest,
`unittest` works as well:

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m unittest discover -s tests/qgis -t .
QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m unittest tests.qgis.test_dlg_main
```

Did you change a user-visible string? Re-extract the translations before you
run the suite. Otherwise the translation test fails on the string the `.ts`
file lacks:

```bash
python scripts/update_translations.py
```

## Against a real GeoServer

The two suites never reach a server. `tests/qgis/test_live_geoserver.py` does.
It drives every tab through the handler a click reaches, with the real form
filled and saved. Then it reads the result back from GeoServer. It runs only when
`GSM_LIVE_URL` names a GeoServer with its demo data. That GeoServer must reach a
PostGIS as host `postgis`, which the [docker sandbox](environment.md) gives:

```bash
GSM_LIVE_URL=http://localhost:8080/geoserver QT_QPA_PLATFORM=offscreen \
    python -m pytest tests/qgis/test_live_geoserver.py
```

Everything it creates is in the workspace `gsm_live`, which it deletes at the
end. The server-wide settings it changes are set back. The *Live GeoServer*
workflow runs it every Monday against the latest 2.x and 3.x releases of
GeoServer. A release that changes an answer shows within a week. Run the
workflow by hand before a minor release, from the *Actions* tab. Its
*versions* field takes other image tags, such as `2.27.6`.

The same workflow runs the suite against the latest 2.x and 3.x releases of
GeoServer Cloud. Each runs twice: with its catalog in a data directory, and
in PostgreSQL. `.github/cloud/run_live.sh` does it all, from Cloud's own compose
files, and runs the same way by hand (Docker, and about 6 GB of memory):

```bash
GATEWAY_PORT=9193 .github/cloud/run_live.sh 3.0.2 pgconfig
```

For a catalog without the demo data, `GSM_LIVE_SHAPES` and `GSM_LIVE_DEM`
name where GeoServer finds the sf shapefiles and DEM.

When a new GeoServer series comes out, the first step fails: the dialog warns
that the series is not tested. Run the suite against it and fix what fails.
Then add the series to `_TESTED_SERIES` in `gui/dlg_main.py`, and to the table
of supported versions in the user guide.

## How the tests are written

- **A fix ships with a test that fails without it.** Run the new test against
  the old code once to prove that it fails. Do not trust that it would.
- **A GUI test is headless.** `from qgis.testing import start_app, unittest`,
  then `start_app()`. Instantiate `GeoServerMainDialog()` directly and drive
  its methods.
- **No server is required.** A tab's fetch is a plain function that returns
  `(rows, failures)`, and `dlg.gs` is assigned a fake client. Only what a fake
  cannot show is confirmed against the [docker sandbox](environment.md): how
  GeoServer encrypts a stored password, or what a partial PUT merges.
- **Loads are asynchronous.** To test what is loaded, call the seam:
  `rows, failures = dlg._fetch_x_rows()`. To drive a whole loader, use
  `tests/qgis/sync_dialog.SyncDialog`, which runs the fetch inline, so
  `dlg._load_x(); dlg._all_rows` still works. The threading itself is tested
  against the real dialog, with `spin_until()` from `test_dlg_main.py`
  (`TestBackgroundLoading`). Those tests fail if a load ever moves back onto
  the GUI thread.
- **The title bar's close button is
  `QCoreApplication.sendEvent(dlg.windowHandle(), QCloseEvent())`.** On Qt6,
  `dlg.windowHandle().close()` is not the spontaneous close the button sends.
  A test built on it passes on QGIS 3.40 and fails on QGIS 4.
- **Tests may import the real `geoservercloud` models**, because
  `tests/qgis/__init__.py` puts the bundled wheels on `sys.path`, under pytest
  and unittest alike. That is how the payload shapes the plugin depends on
  stay locked to the library; see `test_library_contract.py`.
- **Shared fakes live in `tests/qgis/sync_dialog.py`.** A dialog that needs a
  connection gets `dlg.plg_settings = FakePrefs(...)`, built on the real
  settings structure. So a new setting reaches every test at once. Use it, and
  `ended`, before you write a local copy.
- No coverage is collected: nothing read the report, and it cost a third of
  the run.
