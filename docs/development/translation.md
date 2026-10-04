# Manage translations

## Requirements

```bash
python -m pip install PyQt6          # pylupdate6, the string extractor
sudo apt install qttools5-dev-tools  # lrelease and Qt Linguist
```

`pylupdate5` is not an option: it silently skips a `translate()` call that black
wrapped onto several lines, or whose text is written as adjacent string literals:
65 of the plugin's 455 strings when this was measured. `pylupdate6` parses the
Python itself. Qt's own `lupdate` cannot read Python at all.

## Workflow

1. Update the `.ts` files. Every `.ts` in `geoserver_manager/resources/i18n/` is
   updated in one go and existing translations are kept:

    ```bash
    python scripts/update_translations.py
    ```

    Run it after changing any user-visible string: `tests/qgis/test_i18n.py` fails
    when the code has a string that `geoserver_manager_en.ts` lacks. To add a
    locale, copy `geoserver_manager_en.ts` to `geoserver_manager_<locale>.ts`, set
    its `language` attribute, and run the script.

1. Translate, in Qt Linguist or directly in the `.ts` file:

    ```bash
    linguist geoserver_manager/resources/i18n/*.ts
    ```

1. Compile. CI does this when packaging, and the `.qm` files are gitignored:

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
tab hands `_delete_many` its own sentences through `_one_or_many`, the plural
one counted this way.

**The English `.ts` needs the English forms too.** With no translation, Qt
only puts the number in, so "%n layer(s)" would reach an English user as
"3 layer(s)". `tests/qgis/test_i18n.py` fails when a plural is left
unfinished in any shipped locale.

At startup the plugin loads `resources/i18n/geoserver_manager_<locale>.qm` for
QGIS's locale (`plugin_main.py`).
