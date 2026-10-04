# Manage translations

## Requirements

```bash
python -m pip install PyQt6          # pylupdate6, the string extractor
sudo apt install qttools5-dev-tools  # lrelease and Qt Linguist
```

`pylupdate5` is not an option. It silently skips a `translate()` call that
black wrapped onto several lines, or whose text is written as adjacent string
literals. That was 65 of the plugin's 455 strings when measured. `pylupdate6`
parses the Python itself. Qt's own `lupdate` cannot read Python at all.

## Which call to use

- **A tab mixin uses `translate("<MixinClass>", "…")`, never `self.tr()`.**
  In a mixin, `self.tr` is `QObject.tr` with the instance's context,
  `GeoServerMainDialog`. `pylupdate` extracts the string under the mixin's own
  class, so every lookup missed. Each mixin file aliases
  `translate = QCoreApplication.translate` and repeats its context at the call
  site, because `pylupdate` only understands a literal context. A wrapper
  function is not extracted at all (measured, not assumed).
- `GeoServerMainDialog`, `ResourceFormDialog` and the settings page are real
  `QObject` subclasses and keep `self.tr()`. A toolbelt module translates
  under its own literal context (`ConnectionProbe`, `QgisExport`), listed in
  the known contexts of `test_i18n.py`.
- A string that the code compares, rather than only shows, must come from one
  place. The row-actions column label is `self.actions_column_label()` on the
  dialog, so the comparison in `_setup_table` cannot drift from the header
  once a locale is installed.
- `tests/qgis/test_i18n.py` fails in four cases.
  - A mixin goes back to `self.tr()`.
  - A `translate()` call names another file's context.
  - A new `tab_*.py` appears without being covered.
  - The code has a string that the `.ts` file lacks.

## Workflow

1. Update the `.ts` files. Every `.ts` in `geoserver_manager/resources/i18n/`
   is updated in one go, and existing translations are kept:

    ```bash
    python scripts/update_translations.py
    ```

    Run it after you change any user-visible string: `tests/qgis/test_i18n.py`
    fails when the code has a string that `geoserver_manager_en.ts` lacks. To
    add a locale, copy `geoserver_manager_en.ts` to
    `geoserver_manager_<locale>.ts`, set its `language` attribute, and run the
    script.

1. Translate, in Qt Linguist or directly in the `.ts` file:

    ```bash
    linguist geoserver_manager/resources/i18n/*.ts
    ```

1. Compile. CI does this when it packages, and the `.qm` files are ignored by
   git:

    ```bash
    lrelease geoserver_manager/resources/i18n/*.ts
    ```

## Counts: Qt's plural forms, never "(s)"

A message that says how many of something there are goes through Qt's
plural forms. The count is the last argument of the call, and the text says
`%n` where the number goes:

```python
translate("WorkspaceTabMixin", "%n workspace(s)", None, n)
self.tr("%n item(s) could not be listed: {names}.", None, n)
```

`pylupdate6` marks such a message `numerus="yes"`, and each locale fills in
its own forms (English and French have two). Never build the plural from a
noun and "(s)" or `+ "s"`: that is right in no language. For the deletes, a
tab hands `_delete_many` its own sentences through `_one_or_many`, with the
plural one counted this way.

**The English `.ts` needs the English forms too.** With no translation, Qt
only puts the number in. So "%n layer(s)" would reach an English user as
"3 layer(s)". `tests/qgis/test_i18n.py` fails when a plural is left
unfinished in any shipped locale.

At startup the plugin loads `resources/i18n/geoserver_manager_<locale>.qm` for
QGIS's locale (`plugin_main.py`).
