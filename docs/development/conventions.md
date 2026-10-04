# Conventions

How code, strings, messages, tests, documentation and commits are done here.
The [architecture](architecture.md) and [invariants](invariants.md) pages say
what the pieces are; this page says how to work on them.

- **python-geoservercloud first, always.** Before writing any GeoServer call, look for the library
  method (`geoservercloud/geoservercloud.py` in the bundled wheel) and use it, even when a raw request
  would be shorter. If the method does not exist, or exists but cannot do what is needed:
  1. **Update [issue #1](https://github.com/ronitjadhav/qgis-geoserver-manager/issues/1) first**: add a row
     with the call site, the REST verb + path, and the library API you would want. That issue is the
     work list for maturing the library; a gap that is not in it will never be fixed upstream.
  2. Then, and only then, work around it here through `self._raw_rest(...)` (never a bare
     `rest_client` call) with a `TODO(#1)` comment at the call site.
  3. When the library gains the method and the bundled wheel is bumped, replace the workaround, drop the
     `TODO(#1)`, and tick the row.
  The same applies to behaviour the plugin has to paper over (`_check`, `_resource_exists`, the datastore
  merge): those are library gaps too, and they are listed in #1. We depend on this library; the fastest
  way to make the plugin better is to make the library better.
- The global-or-workspace scope (`GLOBAL` label + `scope()`) lives in `gui/scope.py`, now that styles, layer
  groups and the dialog's own workspace-link helper all need it. `_layer_uri()` is still reached from
  `tab_layers.py` through the shared dialog class; lift it the same way when a third caller appears.
- **One `_open_workspace_from_row()` on the dialog** serves every tab's Workspace column (column 1) and skips
  the global label. Tabs point their `_extra_click_callbacks` at it rather than writing their own.
- **A form's primary button says what it does.** `ResourceFormDialog(..., ok_label="Create" | "Publish" |
  "Upload" | "Apply" | "Set style")`; only an *edit* keeps the default "Save". Add-button labels start with a
  verb and never say "New" (`Add a Workspace`, `Publish a Layer`, `Upload a Style`), and the dialog they open is
  titled with the same words; `tests/qgis/test_ux.py` sweeps every tab for both rules.
- **An empty table says why.** `_empty_state_text()` distinguishes a fruitless filter ("Nothing matches 'x'.
  Esc clears the filter."), an empty resource type ("Nothing here yet. Start with 'Add a Workspace' above.") and
  the plain fallback. It reads `btn_add.isHidden()`, not `isVisible()`; see [invariant 8](invariants.md).
- **Colours come from the palette**, never from a literal: `gui/theme.py` maps "ok" / "error" / "busy" and
  the hint and invalid-field colours onto the widget's own palette, choosing a light- or dark-background
  variant. `tests/qgis/test_ux.py` asserts each one clears WCAG's 3:1 contrast floor against the window
  colour in both themes, so a prettier colour that cannot be read fails the suite.
- **Every hook into QGIS is undone in `unload()`.** `LayerTreeMenu` connects
  `QgsLayerTreeView.contextMenuAboutToShow` in `initGui` and disconnects it in `unload()` *before* the dialog
  is destroyed: plugin_reloader is how this repo is developed, and a hook left behind fires into the dead
  plugin on the next reload. `tests/qgis/test_layer_tree.py` drives `initGui` → `unload` on a fake `iface`
  and checks the menu stops appearing. The menu never acts silently: pushing confirms target and style name,
  pulling picks when there are several styles. It also disables its entries with the reason when the
  dialog has no connection, rather than opening a form that complains.
- **Keyboard: F5 / Ctrl+F / Esc / Del** live in `GeoServerMainDialog.keyPressEvent`, not in `QShortcut`,
  because each one has to know where the focus is: Del may only delete when the *table* has focus (the same
  key erases a character in the search box), and Esc clears the search only when there is one, so it still
  closes the dialog otherwise.
- **Nothing the dialog shows comes from a cache.** Lists are fetched on every tab switch and Refresh,
  edit dialogs fetch the object when they open, pickers fetch their options when the form opens. A
  workspace-name cache once survived a Refresh and left the datastore form's combo stale; it was removed
  rather than given a TTL. One extra GET is always cheaper than a stale view.
- **Smallest change that removes demonstrated friction.** No abstraction with one implementation, no
  config for a value that never changes. Add a service layer only when a non-GUI caller needs the API.
- A library gap carries `TODO(#1)` at the call site (see the first convention). Leave it in place until the
  library has the method and the workaround is gone.
- **Strings in a tab mixin use `translate("<MixinClass>", "…")`**, never `self.tr()`: `self.tr` in a mixin
  is `QObject.tr` with the *instance's* context, `GeoServerMainDialog`, while `pylupdate` extracts under the
  mixin's own class, so every lookup missed. Each mixin file aliases `translate = QCoreApplication.translate`
  and repeats its context at the call site, because `pylupdate` only understands a literal context. A wrapper
  function is not extracted at all (measured, not assumed). `GeoServerMainDialog`, `ResourceFormDialog` and
  the settings page are real QObject subclasses and keep `self.tr()`; a toolbelt module translates under its
  own literal context (`ConnectionProbe`, `QgisExport`), listed in `test_i18n.py`'s known contexts. A string
  that is *compared* rather than only displayed must come from one place: the row-actions column label is
  `self.actions_column_label()` on the dialog, so `_setup_table`'s comparison cannot drift from the header
  once a locale is installed.
  `tests/qgis/test_i18n.py` fails if a mixin goes back to `self.tr()`, if a `translate()` call names another
  file's context, if a new `tab_*.py` appears without being covered, or if the code has a string the
  `.ts` lacks. Extraction is `python scripts/update_translations.py` (pylupdate6, `pip install PyQt6`);
  run it after changing a user-visible string. Never pylupdate5: it silently skipped every `translate()`
  black wrapped onto several lines or wrote as adjacent literals: 65 of 455 strings when measured.
- **Every plugin icon goes through the registry.** Use IDs from
  `resources/icons/catalog.json` through `gui/icons.py`, `TABS` and `_row_actions`.
  Read the [icon style guide](icon-style-guide.md) before adding artwork.
  Register pending artwork as `needs-custom` with a QGIS `fallback` and `notes`.
  Run `python scripts/build_icon_catalog.py --check` to validate usage and SVGs.
  The optional local preview goes to ignored `build/icon-catalog.html`.
- **The icon and the screenshots are generated, never hand-edited.**
  `scripts/export_branding.py` renders every brand asset from
  `resources/images/geoserver_manager.svg`, including the
  `resources/images/default_icon.png` that `metadata.txt` points at, and the website's
  logo and favicon; `docs/branding.md` is the guide. `scripts/capture_screenshot.py`
  regrabs every screenshot of the README and the user guide (each tab, the main forms,
  the preview and the settings page) from the real dialog against the docker sandbox,
  off screen. Re-run it when a tab or a form changes, or the screenshots quietly start
  showing an interface that no longer exists.
- Messages: user-facing outcomes go to the dialog's message bar (`show_*_message`); details go to the QGIS
  log (`self.log(..., log_level=Qgis.MessageLevel.Critical)`). `_run_action` does both. Errors and warnings
  **stay until closed** (duration 0): they say what to do next, and were gone in 5 s before. Success fades.
  A response body reaches a banner only through `toolbelt.rest.summarise_body` (first line, 300 chars, markup
  reduced to its title): a Tomcat error page or a proxy login page is not an explanation.
- **Reopening after Close must work.** `closeEvent` sets `_closing` so a late task finish stays away from dying
  widgets; `showEvent` resets it, whatever reopened the dialog (the layer tree's *Publish* shows it without
  reconnecting), `refresh_ui()` stops the running load *before* dropping `self.gs`, and a finished task frees
  its slot even while closing. Without that the dialog worked once per QGIS session; `test_cancel_and_threads.py`
  closes and reopens.
- **Tab labels get a tooltip** from `_tab_help()` (GeoServer's words: "WMS and WMTS stores that proxy another
  server's layers"); the label itself stays a logic key ([invariant 11](invariants.md)).
- Qt6-compatible enums only: `Qt.CursorShape.WaitCursor`, `QDialog.DialogCode.Accepted`,
  `QMessageBox.StandardButton.Yes`; never the unscoped PyQt5 spellings. CI runs a PyQt6 checker.
- Every fix ships with a test that **fails without it**. Run the test against the old code once to prove
  it. Headless GUI tests: `from qgis.testing import start_app, unittest; start_app()` then instantiate
  `GeoServerMainDialog()` directly and drive its methods. Fake the server by assigning `dlg.gs = FakeGS()`.
  `tests/qgis/__init__.py` puts the bundled wheels on `sys.path`, so tests may also import the real
  `geoservercloud` models to lock payload shapes (see `test_library_contract.py`). A connected dialog
  gets `dlg.plg_settings = FakePrefs(...)` from `sync_dialog.py`, never a hand-rolled settings class.
- **Testing a load.** What is loaded: call the seam, `rows, failures = dlg._fetch_x_rows()`.
  A whole loader: use `tests/qgis/sync_dialog.SyncDialog`, which runs the fetch inline, so
  `dlg._load_x(); dlg._all_rows` still works. The threading itself: the real dialog plus
  `spin_until()` from `test_dlg_main.py` (`TestBackgroundLoading`): those tests all fail if
  a load ever goes back to running on the GUI thread.
- Commit messages: conventional prefix (`fix:`, `feat:`, `refactor:`, `chore:`, `ci:`, `docs:`), a body
  that says *why*. Pre-commit runs ruff, ruff-format, black, isort, flake8(+flake8-qgis) and the
  hygiene hooks on every commit; if black rewrites a file the commit aborts. Re-add and commit again.
- **Documentation ships with the change, in the same commit.** The docs are not a
  follow-up task; a change that reaches the user and leaves them stale is unfinished.
  What to touch:

  | Changed | Update |
  |---|---|
  | A tab, button, form or message | the matching section of `docs/usage/guide.md`, and `CHANGELOG.md` under *Unreleased* |
  | The dialog's layout, a tab or a form | the guide, plus `scripts/capture_screenshot.py` against the sandbox (it regrabs every image the README and the guide show) |
  | A new resource type or tab | a guide section, a row in the feature tables of `README.md` and `docs/index.md` |
  | Install or configuration | `docs/usage/installation.md` and the README's Configuration section |
  | A dev step, tool or command | the page under `docs/development/` that teaches it, and this page if a contributor would get it wrong |
  | The logo or a brand asset | `python3 scripts/export_branding.py`, never the exported files by hand |
  | A dependency or a workflow path filter | `.github/dependabot.yml` explains which workflow must see each requirements file; keep that mapping true |

  The site build must stay silent: `sphinx-build -b html -q docs docs/_build/html`
  prints nothing when it is healthy, so a warning is a broken link or an orphan page.
  CI builds it with `-W`, so a warning fails the documentation job.
  The site has no generated API reference because importing the plugin requires QGIS,
  which the documentation job does not install.
- **No em dashes, and prefer short sentences.** In code, comments, docstrings, docs, commit messages and
  every user-facing string, use commas, periods, colons, semicolons or parentheses instead of an em dash.
  Split a long sentence into two rather than joining two clauses with a dash.

## Before you push

```sh
python -m pip install -r requirements/development.txt -r requirements/testing.txt
pre-commit install

# lint + format exactly as CI does
pre-commit run -a
# tests (unit needs no QGIS; qgis needs the QGIS python, headless is fine)
python -m pytest tests/unit
QT_QPA_PLATFORM=offscreen python -m pytest tests/qgis
# after changing any user-visible string (needs pip install PyQt6); test_i18n fails otherwise
python scripts/update_translations.py
# build the zip qgis-plugin-ci would release
qgis-plugin-ci package 0.1.0 --allow-uncommitted-changes && rm geoserver_manager.0.1.0.zip
```

Run `docker compose up -d` for a real server to test against: GeoServer on :8080
(admin/geoserver) plus PostGIS, reachable *from GeoServer* as host `postgis`,
database / user / password `geoserver`. Use it for anything touching the library
contract: the `crypt1:` password encoding, the datastore edit merge and `enabled`
handling were all confirmed against it, and a fake server cannot show you those.

Load the plugin in QGIS by symlinking `geoserver_manager/` into a profile's `python/plugins/`
(`docs/development/environment.md`). Check which profile QGIS actually launches
(`profiles.ini` → `lastProfile`) before assuming `default`. The plugin is `experimental=True`, so
*Show also experimental plugins* must be on. `plugin_reloader` picks up code changes without a restart.

CI (`.github/workflows/`): linter (flake8 + PyQt6 check), tester (unit on 3.12, qgis suite in the
`qgis/qgis:3.40` (Qt5) and `qgis/qgis:4.0` (Qt6) containers), documentation (Sphinx → GitHub Pages),
package & release (zip on push to main; publish on tag, never exercised yet since no tag exists).
