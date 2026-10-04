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
sets the same variable. CI runs the suite twice: in the `qgis/qgis:3.40`
container (Qt5, the oldest QGIS supported) and in `qgis/qgis:4.0` (Qt6). QGIS
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
