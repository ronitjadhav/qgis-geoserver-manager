# Conventions

The rules of this code base. The [architecture](architecture.md) and
[invariants](invariants.md) pages say what the pieces are. This page says how
to work on them, and links to the page that teaches each practice.

- **python-geoservercloud first, always.** Before you write a GeoServer call,
  look for the library method in `geoservercloud/geoservercloud.py` of the
  bundled wheel. Use it, even when a raw request would be shorter. When the
  method does not exist, or cannot do what is needed, do three things.
  1. **Update [issue #1](https://github.com/ronitjadhav/qgis-geoserver-manager/issues/1) first.**
     Add a row with the call site, the REST verb and path, and the library API
     you would want. That issue is the work list for the library; a gap that is
     not in it is never fixed upstream.
  2. Then, and only then, work around it here through `self._raw_rest(...)`,
     never a bare `rest_client` call, with a `TODO(#1)` comment at the call
     site.
  3. When the library gains the method and the bundled wheel is bumped, replace
     the workaround, drop the `TODO(#1)`, and tick the row.

  The same applies to behaviour the plugin papers over: `_check`,
  `_resource_exists`, the datastore merge. Those are library gaps too, and
  they are listed in #1. We depend on this library; the fastest way to make
  the plugin better is to make the library better. A `TODO(#1)` stays in place
  until the library has the method and the workaround is gone.
- **The global-or-workspace scope lives in `gui/scope.py`** (the `GLOBAL`
  label and `scope()`), because styles, layer groups and the dialog's own
  workspace-link helper all need it. `_layer_uri()` is still reached from
  `tab_layers.py` through the shared dialog class; lift it the same way when a
  third caller appears.
- **One `_open_workspace_from_row()` on the dialog** serves every tab's
  Workspace column (column 1) and skips the global label. A tab points its
  `_extra_click_callbacks` at it instead of writing its own.
- **A form's primary button says what it does.**
  `ResourceFormDialog(..., ok_label="Create" | "Publish" | "Upload" | "Apply" | "Set style")`;
  only an *edit* keeps the default "Save". An Add button's label starts with a
  verb and never says "New" (`Add a Workspace`, `Publish a Layer`,
  `Upload a Style`). The form it opens has the same words in its title.
  `tests/qgis/test_ux.py` checks every tab for both rules.
- **An empty table says why.** `_empty_state_text()` tells apart a fruitless
  filter ("Nothing matches 'x'. Esc clears the filter."), an empty resource
  type ("Nothing here yet. Start with 'Add a Workspace' above.") and the plain
  fallback. It reads `btn_add.isHidden()`, not `isVisible()`; see
  [invariant 8](invariants.md).
- **Colours come from the palette**, never from a literal. `gui/theme.py`
  maps "ok", "error" and "busy", and the hint and invalid-field colours, onto
  the widget's own palette, with a light or a dark variant.
  `tests/qgis/test_ux.py` asserts that each one clears WCAG's 3:1 contrast
  floor against the window colour in both themes. A prettier colour that
  cannot be read fails the suite.
- **Every hook into QGIS is undone in `unload()`.** `LayerTreeMenu` connects
  `QgsLayerTreeView.contextMenuAboutToShow` in `initGui` and disconnects it in
  `unload()`, before the dialog is destroyed. This repository is developed
  with plugin_reloader, and a hook left behind fires into the dead plugin on
  the next reload. `tests/qgis/test_layer_tree.py` drives `initGui` then
  `unload` on a fake `iface` and checks that the menu stops appearing. The
  menu never acts silently: a push confirms the target and the style name, a
  pull asks which style when there are several. Without a connection it
  disables its entries and says why, instead of opening a form that
  complains.
- **Keyboard: F5, Ctrl+F, Esc and Del** live in
  `GeoServerMainDialog.keyPressEvent`, not in `QShortcut`, because each one
  has to know where the focus is. Del deletes only while the *table* has the
  focus; in the search box the same key erases a character. Esc clears the
  search only when there is one, so it still closes the dialog otherwise.
- **Nothing the dialog shows comes from a cache.** A list is fetched on every
  tab switch and every Refresh. An edit form fetches the object when it opens.
  A picker fetches its options when the form opens. A workspace-name cache
  once survived a Refresh and left the datastore form's combo stale; it was
  removed, not given a TTL. One extra GET is always cheaper than a stale view.
- **The smallest change that removes demonstrated friction.** No abstraction
  with one implementation, no setting for a value that never changes. Add a
  service layer only when a non-GUI caller needs the API.
- **A string in a tab mixin uses `translate("<MixinClass>", "…")`, never
  `self.tr()`.** In a mixin, `self.tr` carries the instance's context, so
  every lookup missed. The [translations](translation.md) page says which
  call each module uses, why, and what `test_i18n.py` checks. Run
  `python scripts/update_translations.py` after you change a user-visible
  string.
- **Every plugin icon goes through the registry.** Use the IDs of
  `resources/icons/catalog.json` through `gui/icons.py`, `TABS` and
  `_row_actions`. Read the [icon style guide](icon-style-guide.md) before you
  add artwork. Register pending artwork as `needs-custom` with a QGIS
  `fallback` and `notes`. Run `python scripts/build_icon_catalog.py --check`
  to validate usage and SVGs. The optional local preview goes to the ignored
  `build/icon-catalog.html`.
- **The icon and the screenshots are generated, not edited by hand.** Every
  brand asset is rendered from `resources/images/geoserver_manager.svg`. That
  includes the `resources/images/default_icon.png` that `metadata.txt` points at,
  and the website's logo and favicon; `docs/branding.md` is the guide.
  `scripts/capture_screenshot.py` takes every screenshot of the README and the
  user guide from the real dialog, against the docker sandbox, off screen. That
  is each tab, the main forms, the preview and the settings page. Run it
  again when a tab or a form changes, or the screenshots show an interface
  that no longer exists.
- **Messages.** A user-facing outcome goes to the dialog's message bar
  (`show_*_message`); the details go to the QGIS log
  (`self.log(..., log_level=Qgis.MessageLevel.Critical)`). `_run_action` does
  both. An error or a warning **stays until closed** (duration 0): it says
  what to do next, and used to be gone in 5 s. A success fades. A response
  body reaches a banner only through `toolbelt.rest.summarise_body` (first
  line, 300 characters, markup reduced to its title). A Tomcat error page or a
  proxy login page is not an explanation.
- **Reopening after Close must work.** `closeEvent` sets `_closing`, so a late
  task finish stays away from dying widgets. `showEvent` resets it, whatever
  reopened the dialog (the layer tree's *Publish* shows it without
  reconnecting). `refresh_ui()` stops the running load before it drops
  `self.gs`, and a finished task frees its slot even while closing. Without
  that, the dialog worked once per QGIS session; `test_cancel_and_threads.py`
  closes and reopens.
- **A tab label gets a tooltip** from `_tab_help()`, in GeoServer's words
  ("WMS and WMTS stores that proxy another server's layers"). The label
  itself stays a logic key ([invariant 11](invariants.md)).
- **Qt6-compatible enums only**: `Qt.CursorShape.WaitCursor`,
  `QDialog.DialogCode.Accepted`, `QMessageBox.StandardButton.Yes`; never the
  unscoped PyQt5 spellings. CI runs a PyQt6 checker.
- **Every fix ships with a test that fails without it.** Run the test against
  the old code once to prove that it does. The [testing](testing.md) page says
  how the suites are built, how a load is tested, and which fakes to reuse.
- **Commit messages** have a conventional prefix (`fix:`, `feat:`,
  `refactor:`, `chore:`, `ci:`, `docs:`) and a body that says *why*.
  Pre-commit runs ruff, ruff-format, black, isort, flake8 with flake8-qgis,
  and the hygiene hooks on every commit. If black rewrites a file, the commit
  aborts: add the file again and commit again.
- **Documentation ships with the change, in the same commit.** The docs are
  not a follow-up task. A change that reaches the user and leaves the docs
  stale is unfinished. The [documentation](documentation.md) page lists what
  to update for each kind of change.
- **No em dashes, and short sentences.** This holds in code, comments, docstrings,
  docs, commit messages and every user-facing string. Use a comma, a period, a
  colon, a semicolon or parentheses instead of an em dash. Split a long
  sentence in two instead of joining two clauses with a dash.

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

Test anything that touches the library contract against a real server: the
[docker sandbox](environment.md) (`docker compose up -d`). The `crypt1:`
password encoding, the datastore edit merge and the `enabled` handling were
all confirmed against it, and a fake server cannot show those. The same page
says how to load the plugin into QGIS from the working tree.

CI, in `.github/workflows/`, has four workflows:

- The linter runs flake8 and the PyQt6 check.
- The tester runs the unit tests on Python 3.12, and the QGIS suite in the
  `qgis/qgis:3.40` and `qgis/qgis:4.0` containers.
- The documentation job builds the site with Sphinx and deploys it to GitHub
  Pages.
- Package and release builds a zip on each push to main, and a release on a
  tag.
