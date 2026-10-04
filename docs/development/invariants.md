# Invariants

These rules prevent regressions found in testing or observed against GeoServer. The
[architecture](architecture.md) page explains the pieces they name; the
[conventions](conventions.md) page refers to them by number.

1. **Row cache and tab callbacks are reset together.** `_setup_table` clears `_all_rows`
   and `_filtered_rows`; `_reset_table_state` clears those *and* every callback and the
   pagination buttons. A loader that fails mid-fetch must leave an empty table, never
   the previous type's rows under the new type's Delete handler. That was a real
   wrong-target delete (`tests/qgis/test_dlg_main.py` guards it).
2. **Qt's table sorting stays off** (`_setup_table` forces it). A header click sorts
   `_filtered_rows` itself (`_on_header_clicked`), so the order on screen *is* the order
   of the row cache. Rows are mapped back by index. If Qt reordered the items on its
   own, *Delete Selected* would act on a different resource than the one highlighted.
   The sort survives a reload of the same tab. It is dropped when the columns change,
   or when it is on a detail column whose cells are pending again after the reload.
3. **Edits merge onto what the server has.** GeoServer applies a datastore PUT by
   *replacing* the whole `connectionParameters` map. Never route an edit through the
   typed `create_*` helpers. Use `_update_datastore_from_values`, which overlays only
   the form's own keys onto the fetched params and keeps the server's `type`. The edit
   form's Save fetches them again (`_save_datastore_edit`) and applies only what the
   user changed, `enabled` included. So an edit that another client saved while the form was open survives. A store
   deleted or renamed meanwhile is refused, where `create_datastore` POSTed it back
   empty. The workspace and tile cache forms do the
   same. GeoServer ignores `enabled: false` on a POST (the store is created enabled,
   measured on 2.28.5). Only a PUT disables one, which is why the checkbox exists in
   edit mode only.
4. **Add refuses an existing name.** `create_workspace` and `create_datastore` are
   upserts (POST, then PUT on 409). Check `_resource_exists` first, or a live resource
   is silently reconfigured and reported "created".
5. **Never show a password.** GeoServer returns `passwd` and
   `WFSDataStoreFactory:PASSWORD` encrypted (`crypt1:…`) or not at all. Prefilled, the
   ciphertext would read as the password and get edited into garbage. GeoServer *does*
   accept its own ciphertext back. Measured on 2.28.5: a PostGIS store still listed its
   tables after the round trip, and stopped doing so with a wrong plaintext. So on edit
   the field is blank. Blank means **keep** (the stored value is sent back) and typed
   means **replace**. The encryption is randomised: the same plaintext saves as a
   different `crypt1:` value every time, so ciphertexts cannot be compared.
6. **A datastore rename is one PUT on the old path.** A save through `create_datastore`
   under the new name would upsert. It would duplicate the store, or overwrite
   whatever holds the new name. `_rename_datastore` refuses a taken name before any request;
   then the save goes to the new name.
7. **Delete confirmations name the cascade.** Both delete paths send `recurse=true`.
8. **`isVisible()` lies on inactive tab pages.** `ResourceFormDialog` tracks hidden
   fields in `_hidden_keys`. Validation uses that, not Qt, and switches to the tab that
   holds the offending field.
9. **A fetch never touches a widget.** It runs in a worker thread. Everything it learns
   comes back as `(rows, failures)`, and `_render_rows` renders it on the GUI thread. A
   cancelled or failed load renders nothing. That is safe only because the loader
   reset the table *before* it started the task, which is what keeps "no stale rows"
   true here too. An upload's `work(task)` is held to the same rule. The file, the paths and the
   REST client are arguments captured on the GUI side, and progress goes through
   `task.setProgress`.
10. **A loaded table outlives its connection.** `refresh_ui()` clears `self.gs` at once
   and re-probes in a task. So for up to `PROBE_TIMEOUT`, the rows on screen and their
   buttons belong to a client that is gone. Every user-triggered action therefore
   passes `_require_connection()`. That check lives at every place where an action is dispatched. The Add button
   and Delete Selected are such places. So are the row-action buttons and their
   More or Actions menu entries. So are the link-cell click, Enter on a row, and
   the Del key. A selection change re-enables the button while the probe runs. It never lives in
   the 20 methods behind them, so a new tab cannot forget it. A refresh also disables
   the header buttons at once; the loader re-arms them. This was a reported crash:
   `AttributeError: 'NoneType' object has no attribute 'get_workspaces'` from
   *Publish a Layer*.
   The tab fetchers read `self.gs` from their worker. That is safe for one reason only. The client changes solely after the running
   load is cancelled (`refresh_ui()` cancels, then clears), and a cancelled load
   renders nothing.
   Anything new that assigns `self.gs` must cancel the load first;
   `test_a_refresh_cancels_the_load_before_it_drops_the_client` guards it.
   A refresh does not cancel a delete batch or an upload, and their later steps read
   `self.gs`. A connection switch mid-batch sent the rest of a recursive delete to
   the other server. So `refresh_ui()` and the connection switch refuse, with a warning,
   while either runs. They also refuse while a save abandoned at the waiting box still
   runs in its thread (`_refuse_while_writing`, which looks for a `_ReadThread` with
   `write` set). The same dispatch points also pass `_addressable(rows)`. A row whose name holds
   `/ ? # %` is refused, because `requests` sends `datastores/a#b` as
   `datastores/a`, and the delete of "a#b" deleted "a".
11. **Nav labels in `TABS` are logic keys as well as text.** The `tr("Actions")` column
   and the `tr("Workspace")` key of `_extra_click_callbacks` must equal the header
   strings.
