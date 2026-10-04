# Architecture

How the plugin is put together, and the helpers every tab goes through. Read
this and the [invariants](invariants.md) before changing code: they record
what the code cannot tell you. Python 3.12 (QGIS 3.40 and newer), PyQt5
**and** PyQt6 through `qgis.PyQt`.

## Layout

| Path | What lives there |
|---|---|
| `geoserver_manager/plugin_main.py` | QGIS entry point: `initGui` / `unload` / `run`. Shows the dialog, then connects. |
| `geoserver_manager/gui/dlg_main.py` | `GeoServerMainDialog(QDialog, <one mixin per tab>)`: nav list, results table, search, pagination, and every helper the tabs share |
| `geoserver_manager/gui/tab_workspaces.py`, `tab_datastores.py`, `tab_coveragestores.py`, `tab_cascaded.py`, `tab_layers.py`, `tab_layergroups.py`, `tab_styles.py`, `tab_gwc.py`, `tab_server.py` | One mixin per resource type (the Server tab: one row per server-wide setting, no Add or Delete): load / add / edit / delete (layers also: publish, add to QGIS, preview, set styles, push a style from QGIS, update from the data; layer groups also: add to QGIS, preview; datastores and coverage stores also: reset; coverage stores also: publish a coverage; cascaded stores also: publish / view a remote layer; styles also: apply to a QGIS layer, save to disk, copy, used by; tile cache: configure, seed and follow the tasks, truncate, stop caching) |
| `geoserver_manager/gui/dlg_resource_form.py` | `ResourceFormDialog`, a modal form built from a list of field dicts (see its module docstring for the field spec). A field uses QGIS's own widget wherever QGIS has one: `QgsFileWidget`, `QgsPasswordLineEdit`, `QgsCodeEditor*`, `QgsMapLayerComboBox`, `QgsExtentWidget`. Each page scrolls rather than squeeze |
| `geoserver_manager/gui/list_table.py` | `ListTable`, the form's `"table"` field: rows picked from the server's names, reordered, with typed columns (a style combo, a zoom spinbox). A list is never a text box to type into: plain strings are a `"list"` field (QGIS's `QgsListWidget`), pairs a `"keyvalue"` one (`QgsKeyValueWidget`); `ListTable` is only for what those two cannot do (order, picking, columns) |
| `geoserver_manager/gui/dlg_preview.py` | `LayerPreviewDialog`, a `QgsMapCanvas` showing one WMS layer of the server, with GetFeatureInfo on click; non-modal, nothing reaches the project |
| `geoserver_manager/gui/dlg_settings.py` | Options page: URL + credentials (plain text in QgsSettings by default; a per-profile box keeps them in `QgsAuthManager`, encrypted) and *Test connection*, which probes the fields as typed |
| `geoserver_manager/gui/layer_tree.py` | `LayerTreeMenu`, the *GeoServer Manager* submenu of the layer tree's context menu: push / apply the clicked layer's style through the main dialog's connection (`_push_qgis_style`, `_sld_of`, `_layer_styles`, all behind `_wait_for`), and *Publish to GeoServer*, which opens the dialog's publish form (`_publish_layer`, `_publish_layers`); outcomes go to `iface.messageBar()` |
| `geoserver_manager/toolbelt/` | `preferences` (QgsSettings, with the auth store as the per-profile option, and the saved server profiles: the active one is copied into the single-connection fields everything else reads), `log_handler`, `dependencies` (loads the bundled wheels), `env_var_parser`, `probe` (the bounded connection check the dialog and Settings share), `rest` (the raw REST call, `summarise_body` for banners, the streaming upload body; no QGIS import), `payload` (GeoServer's collection shapes, keywords and translatable titles, pure), `sld` and `qgis_export` (QGIS ↔ GeoServer conversions; they import QGIS, and never touch a widget) |
| `geoserver_manager/extras/*.whl` | Bundled `geoservercloud` (stripped, see the [GeoServer notes](geoserver-notes.md)) and `xmltodict`, added to `sys.path` at startup |
| `tests/unit/` | Runs without QGIS. `tests/qgis/` needs the QGIS Python (headless via `qgis.testing.start_app()`) |
| `docs/` | The site: Sphinx + MyST + Furo, deployed to GitHub Pages on every push to main. `usage/` is written for the user, `development/` for a contributor, `github_issue_roadmap.md` is the feature backlog that GitHub milestones mirror. **`development/geoserver-notes.md` holds the measured GeoServer and library facts this plugin depends on** |
| `scripts/` | `update_translations.py` (pylupdate6), `export_branding.py` (every brand asset from one SVG), `capture_screenshot.py` (every screenshot the README and the guide show, grabbed from the real dialog) |
| `docker-compose.yml` | Throwaway GeoServer 2.28.5 (`:8080`, admin/geoserver) + PostGIS, for testing against a real server |

## How the dialog works

- **Tabs are one registry.** `GeoServerMainDialog.TABS = ((label, icon, loader_name), …)`.
  `_setup_nav` builds the list from it and `_on_nav_changed` calls `getattr(self, loader)()`.
  A new resource type = one line in `TABS` + one mixin added to the class bases.
- **A loader** sets up the header buttons, `_name_click_callback`, `_extra_click_callbacks`,
  `_row_actions`, calls `_setup_table(columns)`, then hands a fetch function to
  `_start_load(failure_message, fetch)` and returns. Rows are plain lists of display strings;
  column 0 is the resource name. A `_row_actions` entry is `(icon, label, callback)`, plus an
  optional fourth element when the action's tooltip must say more than the label
  (the browser preview's login note).
  `_make_action_widget` keeps up to two frequent actions visible (add to QGIS,
  preview, browse and publish). Other actions get labels in a More or Actions
  menu, with destructive actions last and separated when needed. Both paths
  check the connection at activation and capture the row from the page render.
  Keep this grouping central; tabs only declare their existing action tuples.
- **Loads run off the GUI thread.** `_start_load` wraps the fetch in a `_FetchTask` (a
  `QgsTask`), so `_load_x()` returns before a single row exists: QGIS's task bar shows the
  progress, *Refresh* turns into *Cancel*, and `finished()` comes back on the GUI thread to
  render. A fetch is `_fetch_<x>_rows(task=None) -> (rows, failures)`; it runs in a worker,
  so it must not touch a widget, and it only gets at the task by passing it to `_fan_out`.
  A read's worker is a thread of its own that a cancel lets go of at once, so a hung request
  holds neither the task nor QGIS's exit, which cancels a read without asking
  (`CancelWithoutPrompt`). An upload and a delete batch are waited for, and QGIS asks first.
  A fetch lists **names only**: a column that needs one GET per row holds `scope.PENDING`,
  and the loader sets, after `_setup_table`, `self._row_detail = row -> cells` (a stateless read,
  run in a worker) and `self._detail_columns`. `_show_page` then fetches the pending cells of the
  20 rows on screen in the `_detail` slot, and writes them into the shared row lists in place.
  A row action, and a sort on a detail column, first complete the rows they need
  (`_complete_rows`, from `_addressable` and `_on_header_clicked`). A late fill checks
  `_table_generation`, so it never lands in another tab's table. The search box matches the
  cells loaded so far. Measured: the Styles tab with 147 styles went from 158 requests to 31.
  Mutations (add / edit) run under `_run_action`, with their requests in `_wait_for`: the
  user is waiting for the dialog they just confirmed, but a hung server must not freeze QGIS
  for the library's 120 s timeout. So the action passed to `_wait_for` makes requests only; a
  question (`_push_qgis_style`'s "Replace the style?") or a warning stays outside it, and a
  helper that runs in it returns its warning instead of showing it (`_save_workspace`).
  Deletes run in the `_delete` task slot. The exception is an upload
  (`_run_upload`): its body is a `toolbelt.rest.ProgressReader`, which moves the task bar from each
  `read()` and raises on Cancel so `requests` drops the connection mid-body. The work gets the REST
  client as an argument, because a Refresh clears `self.gs` while it runs (`toolbelt.rest.raw_rest`
  is `_raw_rest` for a client you hold).
- **The table is paginated in Python** (`_page_size = 20`, `_all_rows` → `_filtered_rows` → one page).
  `_get_selected_rows()` maps a selected view row back through `_filtered_rows` by index.
- **Server calls go through the helpers on the dialog**, never hand-rolled in a mixin:

  | Helper | Use it for |
  |---|---|
  | `_run_action(fn, failure_message) -> bool` | any mutation: wait cursor, banner + QGIS log on failure |
  | `_fetch(fn, failure_message) -> value \| None` | any read the UI needs before continuing: runs `fn` in a worker thread and waits behind an application-modal *Waiting for GeoServer* box (after 0.3 s) with Cancel, so a dead server cannot freeze QGIS. `in_worker=False` for work on a live QGIS layer. A map layer `fn` builds comes back moved to the GUI thread |
  | `_wait_for(fn) -> value` | the same wait without the reporting: a read inside a handler that does its own (the Publish form's combo refills). `fn` must not touch a widget, nor call `_wait_for` or `_fetch` itself |
  | `_form_check(check) -> validate` | a form's `validate`: `check(values)` behind the waiting box, on Save, before the form closes, so a refusal (a taken name) keeps what was typed. `check` only reads (`_check_new_datastore`, `_check_new_workspace`, `_check_group_rows`...); the save runs after the form closed and checks again. A pure check (a tile cache edit) is passed as `validate` directly |
  | `_wait_for_save(fn) -> value` | a save's requests under `_run_action`. A Cancel cannot stop a request already sent: the save still lands, so the banner says it may, and the tab reloads once it ends. Until then a task, *GeoServer Manager: saving…*, keeps QGIS from quitting before it ends |
  | `_check((content, status))` | unwrap a geoservercloud tuple; raises on ≥ 400 |
  | `_fetch_list(api_method, *args)` | a list endpoint; raises when the payload is not a list (a sign-in page), which must not render as an empty table |
  | `_resource_exists(getter, *args)` | pre-check before *Add* (the library upserts) |
  | `_raw_rest(method, path, **kw)` | endpoints the library lacks; raises with GeoServer's response body, except for a status in `accept=(404,)`, which comes back as the answer it is (a settings path with none of its own) |
  | `_name_of(item)` | the name of a list entry (dict or str) |
  | `_get_workspace_names()` | workspace names for combos: a fresh GET every call, deliberately uncached |
  | `_start_load(failure_message, fetch)` | a tab load: runs `fetch(task)` in a `QgsTask`, renders `(rows, failures)` when it lands |
  | `_run_in_task(failure_message, work, on_success, on_cancel=…, busy_text=…)` | the same for anything that is not rows (the connection probe); a load supersedes it. Deletes run in their own `_delete` slot through `_delete_many` |
  | `_run_quietly(failure_message, work, on_success)` | a side fetch (a dialog's legend) in its own slot: never supersedes a load, never turns Refresh into Cancel |
  | `_run_upload(failure_message, work, on_success, on_cancel)` | a long PUT: streams in its own task slot (`_upload`) with progress and Cancel; a load never supersedes it and it never touches the table: `on_success` reloads through `_reload_current_tab()`, `on_cancel` says what the server kept |
  | `_upload_file(failure_message, client, url, source, params, headers, on_success, on_cancel, folder=…, after=…, on_done=…)` | the one way a file leaves this machine: streams `source` through `_run_upload`, removes `folder` however it ends, runs `after(client)` in the worker for a follow-up PUT. Check `_upload_slot_free()` *before* exporting. `_report_cancelled_upload(kind, tab, exists, name)` is its `on_cancel`. `on_done(outcome)` runs once at the end with "done", "failed" or "cancelled": the batch publish (`_publish_layers`) starts its next layer from it, since only one upload runs at a time. Returns False when nothing started |
  | `_cancel_load(user=False)` | stop the running load; `user=True` is the Cancel button, which lets go of the task at once (a hung request only returns at its timeout) and explains itself in a banner. A load cancelled from QGIS's task bar says so too; only a superseded one stays quiet |
  | `_fan_out(fn, items, task=None) -> [(result, error)]` | parallel per-item GETs, eight at a time for the whole plugin (so `fn` never fans out itself); a failing item yields `(None, exc)` instead of aborting. With the task: progress per finished item, and once it is cancelled no item starts: it yields `(None, CancelledError())` |
  | `_scoped_names(list_global, list_in, workspace_names, task=None) -> (pairs, failures)` | the listing of a resource that is global or per workspace (styles, layer groups): `(name, workspace label)` pairs, the global ones first from `list_global()`, one `list_in(ws)` per workspace fanned out, a listing that fails (the global one too) reported beside the names |
  | `_report_partial_failures([(label, exc)])` | one warning banner + log lines for what a listing could not fetch |
  | `_delete_many([(label, fn)], reload_fn, ask, done, cascade=…)` | confirm + run in a task of its own slot (`_delete`), with progress, and report one or many deletions. A tab switch or F5 never stops it, Cancel does, one batch runs at a time, and it reloads only the tab it started from. `ask` and `done` are whole sentences from `_one_or_many(one, many)`: `one.format(name)` for a single resource, else `many(count)`, the tab's `n -> translate(ctx, "…%n layer(s)…", None, n)`, so each locale gets its plural forms ([translations](translation.md)); a tab whose action is not a delete (the tile cache's "stop caching") words them that way |
  | `_require_safe_name(name)` | every Add form, before any request: refuses `/ ? # %` and edge spaces; `requests` sends `datastores/a#b.json` as `datastores/a` |
  | `_yes_no(value)` | a boolean cell, translated, never Python's `True` / `False` |
  | `_unwrap` / `_as_list` / `_name_of` | GeoServer's collection shapes, from `toolbelt/payload.py`; no tab keeps its own copy |
  | `_reload_current_tab()` | after an action reachable from another tab |
  | `_wire_picker(dlg, picker, first, load)` | a read-only viewer with a picker (a store's coverages, its cascaded layers): choosing an entry fills the form through `ResourceFormDialog.set_values`; `load` returns None after a failed read, which leaves the fields alone |

- **Adding a layer to QGIS** (`LayerTabMixin._add_layer_to_qgis`): build the URI with `_layer_uri`
  (pure, tested), construct `QgsRasterLayer`/`QgsVectorLayer`, check `isValid()`, then
  `QgsProject.instance().addMapLayer()`. Never `iface.addRasterLayer()`, which pops QGIS's own modal on
  failure instead of our banner. Encrypted credentials travel as `authcfg=<geoserver_auth_cfg_id>`, resolved by the
  providers from `QgsAuthManager`, so a saved project contains no password; plain ones (the default)
  go in the source as `username` and `password`, which a saved project then holds. Note the plugin's TLS
  setting does not reach QGIS's providers; they use QGIS's own certificate handling.

## The server and the library

They are on their own page, [GeoServer and library notes](geoserver-notes.md), because they
are needed when touching one tab's server calls and not for every change.
**Read it before writing or changing any GeoServer call.** What is in it:

- Which verbs raise, which return `(content, status)`, and why `_check` exists.
- Which library methods upsert, which are missing, and what goes through `_raw_rest`.
- Per resource type: layers of every type, legends and previews, coverages,
  cascaded WMS and WMTS stores, layer groups, workspace WMS settings, the tile
  cache and its XML-only writes, file-based and cascaded WFS datastores.
- Publishing from QGIS: what a GeoPackage or GeoTIFF upload actually creates,
  what a cancelled upload leaves behind, and how SLD versions pick a content type.
- The bundled wheel: why it is stripped, and what to do on a version bump.
