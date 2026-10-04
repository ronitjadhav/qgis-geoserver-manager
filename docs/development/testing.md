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

`QT_QPA_PLATFORM=offscreen` is what lets the widget tests run without a
display; the same variable is how CI runs them. CI runs the suite twice: in
the `qgis/qgis:3.40` container (Qt5, the oldest QGIS supported) and in
`qgis/qgis:4.0` (Qt6). QGIS 4 ignores `supportsQt6`, so there
`test_qgis_compat` checks that the version range of `metadata.txt` lets it
load the plugin. No `qgis/qgis` image has a Qt6 build of QGIS 3.40 to 3.44,
so no job covers those. Without pytest, `unittest` works just as well:

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m unittest discover -s tests/qgis -t .
QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m unittest tests.qgis.test_dlg_main
```

Changed a user-visible string? Re-extract before running the suite, or the
translation test fails on the string the `.ts` file lacks:

```bash
python scripts/update_translations.py
```

## How the tests are written

- **A fix ships with a test that fails without it.** Run the new test against
  the old code once to prove it does, rather than trusting that it would.
- **No server is required.** A tab's fetch is a plain function returning
  `(rows, failures)`, and `dlg.gs` is assigned a fake client. Only the things a
  fake cannot show you, such as how GeoServer encrypts a stored password or
  what a partial PUT merges, are confirmed against the
  [docker sandbox](environment.md).
- **Loads are asynchronous.** A whole loader is driven through
  `tests/qgis/sync_dialog.SyncDialog`, which runs the fetch inline. The
  threading itself is tested against the real dialog, so those tests fail if a
  load ever moves back onto the GUI thread.
- **The title bar's close button is
  `QCoreApplication.sendEvent(dlg.windowHandle(), QCloseEvent())`.** On Qt6,
  `dlg.windowHandle().close()` is not the spontaneous close the button sends,
  so a test built on it passes on QGIS 3.40 and fails on QGIS 4.
- Tests may import the real `geoservercloud` models, because the package's
  `tests/qgis/__init__.py` puts the bundled wheels on `sys.path`, under pytest
  and unittest alike. That is how the payload shapes the plugin depends on
  stay locked to the library.
- **Shared fakes live in `tests/qgis/sync_dialog.py`.** A dialog that needs a
  connection gets `dlg.plg_settings = FakePrefs(...)`, built on the real
  settings structure, so a new setting reaches every test at once. Reach for
  it, and for `ended`, before writing a local copy.
- No coverage is collected: nothing read the report, and it cost a third of
  the run.
