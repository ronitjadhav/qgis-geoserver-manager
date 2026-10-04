#! python3  # noqa E265

"""
Translations: the context a string is extracted under has to be the context it
is looked up under at runtime, or every lookup misses silently.

Usage from the repo root folder:

.. code-block:: bash

    QT_QPA_PLATFORM=offscreen python -m unittest tests.qgis.test_i18n
"""

import ast
import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from qgis.PyQt.QtCore import QCoreApplication, QTranslator
from qgis.PyQt.QtWidgets import QHeaderView
from qgis.testing import start_app, unittest

from tests.qgis.sync_dialog import SyncDialog

start_app()

GUI = Path(__file__).parents[2] / "geoserver_manager" / "gui"
I18N = Path(__file__).parents[2] / "geoserver_manager" / "resources" / "i18n"

# Each tab mixin lives in its own file, so the file decides the context.
CONTEXTS = {
    "tab_workspaces.py": "WorkspaceTabMixin",
    "tab_datastores.py": "DatastoreTabMixin",
    "tab_coveragestores.py": "CoverageStoreTabMixin",
    "tab_cascaded.py": "CascadedStoreTabMixin",
    "tab_layers.py": "LayerTabMixin",
    "tab_layergroups.py": "LayerGroupTabMixin",
    "tab_styles.py": "StyleTabMixin",
    "tab_gwc.py": "GwcTabMixin",
    "tab_server.py": "ServerTabMixin",
}

# Every context a .ts may name: the mixins', and the classes and modules
# that translate under their own.
KNOWN_CONTEXTS = set(CONTEXTS.values()) | {
    "ConfigOptionsPage",
    "ConnectionProbe",
    "Dependencies",
    "GeoServerMainDialog",
    "GeoServerMainDialogBase",
    "GeoServerManagerPlugin",
    "LayerTreeMenu",
    "QgisExport",
    "Sld",
    "LayerPreviewDialog",
    "ListTable",
    "ResourceFormDialog",
    "Scope",
    "wdg_geoserver_manager_settings",
}

_CALL = re.compile(r"self\.tr\(")
_TRANSLATE = re.compile(r'\btranslate\(\s*"([^"]+)"')

# The leading literals pylupdate6 needs to extract a call: context, then text.
_LITERALS = {"translate": 2, "tr": 1}

# The one call it may skip: the plugin's own tr(), whose callers it extracts
# under GeoServerManagerPlugin.
NOT_EXTRACTED = {
    "plugin_main.py: QCoreApplication.translate(self.__class__.__name__, message)",
}


def _named(node):
    """translate, for both translate and QCoreApplication.translate."""
    return getattr(node, "attr", None) or getattr(node, "id", None)


def _is_text(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _aliases(tree):
    """Where translate or tr is used, other than called or bound to its own name.

    pylupdate6 extracts a call only by the name it is made under, so any other
    use hides the strings: t: object = translate, import ... as t, partial(tr).
    """
    kept = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            kept.add(node.func)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            # translate = QCoreApplication.translate keeps the name
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if all(_named(target) == _named(node.value) for target in targets):
                kept.add(node.value)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for name in node.names:
                if name.name in _LITERALS and name.asname not in (None, name.name):
                    yield node.lineno, name.name
        elif (
            isinstance(node, (ast.Name, ast.Attribute))
            and isinstance(node.ctx, ast.Load)
            and _named(node) in _LITERALS
            and node not in kept
        ):
            yield node.lineno, _named(node)


class Spy(QTranslator):
    """Answers like a .qm would, and records what Qt asked for.

    A translator that returns "" makes the string come back *empty* rather
    than falling back to its source, so anything outside the contexts under
    test is echoed unchanged.
    """

    def __init__(self, contexts):
        super().__init__()
        self.contexts = set(contexts)
        self.asked = []

    def translate(self, context, source, disambiguation=None, n=-1):
        self.asked.append((context, source))
        if context in self.contexts:
            return f"[{context}] {source}"
        return source

    def isEmpty(self):  # noqa: N802 (Qt asks before consulting translate())
        return False


class TestExtractionContextMatchesTheCode(unittest.TestCase):
    """Static half: every mixin string carries its own file's context."""

    def test_no_mixin_calls_self_tr_any_more(self):
        """self.tr() in a mixin resolves against GeoServerMainDialog, not it."""
        offenders = {}
        for filename in CONTEXTS:
            source = (GUI / filename).read_text()
            # the explanatory comment mentions self.tr(); code does not
            code = "\n".join(
                line
                for line in source.splitlines()
                if not line.lstrip().startswith("#")
            )
            found = _CALL.findall(code)
            if found:
                offenders[filename] = len(found)
        self.assertEqual(offenders, {})

    def test_every_translate_call_uses_its_own_files_context(self):
        wrong = []
        for filename, context in CONTEXTS.items():
            for found in _TRANSLATE.findall((GUI / filename).read_text()):
                if found != context:
                    wrong.append(f"{filename}: {found}")
        self.assertEqual(wrong, [])

    def test_each_context_is_a_class_that_exists_in_that_file(self):
        for filename, context in CONTEXTS.items():
            self.assertIn(f"class {context}:", (GUI / filename).read_text(), filename)

    def test_every_tab_mixin_file_is_covered_here(self):
        """A new tab must not slip past these checks."""
        on_disk = {path.name for path in GUI.glob("tab_*.py")}
        self.assertEqual(on_disk, set(CONTEXTS))

    def test_every_string_in_the_code_is_in_the_ts(self):
        """A string the extractor missed is a string nobody can translate.

        pylupdate5 silently skipped a translate() call that black had wrapped
        onto several lines, or whose text is written as adjacent literals:
        65 of 455 strings when measured. When this fails, regenerate the .ts
        with `python scripts/update_translations.py`.

        A call under a known context is checked whatever it is named: an
        alias (t = translate) or a wrapper (_tr(text)) hid every Server-tab
        label and connection message from pylupdate6, which only extracts
        translate() and tr() with literals. Under a new context too, a call of
        either without literals fails, and so does any other use of either
        than a call or a binding to its own name.
        """
        root = ElementTree.parse(I18N / "geoserver_manager_en.ts").getroot()
        extracted = {
            (context.find("name").text, message.find("source").text)
            for context in root.findall("context")
            for message in context.findall("message")
        }
        anywhere = {source for _, source in extracted}
        missing = []
        for path in sorted(GUI.parent.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for lineno, name in _aliases(tree):
                missing.append(f"{path.name}:{lineno} an alias of {name}()")
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _named(node.func)
                literals = [arg.value for arg in node.args[:2] if _is_text(arg)]
                first = node.args[0] if node.args else None
                context = first.value if isinstance(first, ast.Constant) else None
                needed = _LITERALS.get(name, 0)
                bare = sum(map(_is_text, node.args[:needed])) < needed
                call = f"{path.name}: {ast.unparse(node)}" if bare else ""
                if (bare and call not in NOT_EXTRACTED) or (
                    context in KNOWN_CONTEXTS
                    and (name != "translate" or len(literals) < 2)
                ):
                    missing.append(
                        f"{path.name}:{node.lineno} {name}() is not extracted"
                    )
                elif name == "translate" and len(literals) == 2:
                    if tuple(literals) not in extracted:
                        missing.append(f"{path.name}:{node.lineno} {literals[1]!r}")
                elif name == "tr" and literals and literals[0] not in anywhere:
                    missing.append(f"{path.name}:{node.lineno} {literals[0]!r}")
        self.assertEqual(missing, [])


class TestRuntimeContext(unittest.TestCase):
    """Behavioural half: with a translation installed, the strings change."""

    def setUp(self):
        self.spy = Spy(CONTEXTS.values())
        QCoreApplication.installTranslator(self.spy)
        self.dlg = SyncDialog()
        self.dlg.show_warning_message = lambda text: None
        self.dlg.show_error_message = lambda text: None
        self.dlg.show_success_message = lambda text: None

    def tearDown(self):
        QCoreApplication.removeTranslator(self.spy)

    def headers(self):
        table = self.dlg.resultsTable
        return [
            table.horizontalHeaderItem(column).text()
            for column in range(table.columnCount())
            if table.horizontalHeaderItem(column)
        ]

    def test_each_tab_uses_its_own_context(self):
        class FakeGS:
            def get_workspaces(inner):
                return ([], 200)

            def get_styles(inner, workspace_name=None):
                return ([], 200)

            def get_layer_groups(inner, workspace_name):
                return ([], 200)

            def __getattr__(inner, name):
                raise AttributeError(name)

        self.dlg.gs = FakeGS()
        self.dlg._load_workspaces()
        self.assertIn("[WorkspaceTabMixin] Name", self.headers())
        # Before the fix these read "Style Name": the lookup used
        # GeoServerMainDialog, where the string was never extracted.
        self.dlg._load_styles()
        self.assertIn("[StyleTabMixin] Name", self.headers())
        self.assertIn("[StyleTabMixin] Workspace", self.headers())

    def test_the_actions_column_still_resizes_when_translated(self):
        """Its label is compared, so both sides must share one context."""
        columns = ["Name", self.dlg.actions_column_label()]
        self.dlg._setup_table(columns)
        header = self.dlg.resultsTable.horizontalHeader()
        self.assertEqual(header.sectionResizeMode(1), QHeaderView.ResizeMode.Fixed)
        self.assertEqual(header.sectionResizeMode(0), QHeaderView.ResizeMode.Stretch)


class TestPluralForms(unittest.TestCase):
    """A count goes through Qt's plural forms, never "(s)".

    With no translation, Qt only fills in %n, so "%n layer(s)" would reach an
    English user as "3 layer(s)". The English .ts therefore carries the two
    English forms, and every locale carries all of its own.
    """

    def test_a_locale_without_a_translation_gets_englishs_plurals(self):
        # "%n layer(s)" read "(s)" in, say, a German QGIS: no .qm was loaded.
        import tempfile
        from unittest.mock import MagicMock, patch

        from geoserver_manager import plugin_main

        folder = Path(tempfile.mkdtemp()) / "geoserver_manager"
        (folder / "resources" / "i18n").mkdir(parents=True)
        english = folder / "resources" / "i18n" / "geoserver_manager_en.qm"
        english.write_bytes(b"")
        logged = []
        with (
            patch.object(plugin_main, "DIR_PLUGIN_ROOT", folder),
            patch.object(plugin_main.QgsSettings, "value", lambda *a: "de_DE"),
            patch.object(plugin_main.QCoreApplication, "installTranslator"),
            patch.object(
                plugin_main.PlgLogger, "log", lambda *a, **k: logged.append(k)
            ),
        ):
            plugin = plugin_main.GeoServerManagerPlugin(MagicMock())
        self.assertIsNotNone(plugin.translator)
        self.assertTrue(any(str(english) in str(k.get("message")) for k in logged))

    def test_every_plural_is_finished_in_every_shipped_locale(self):
        for path in sorted(I18N.glob("*.ts")):
            plurals = [
                message
                for message in ElementTree.parse(path).iter("message")
                if message.get("numerus") == "yes"
            ]
            self.assertTrue(plurals, f"{path.name}: no plural at all")
            for message in plurals:
                source = message.find("source").text
                translation = message.find("translation")
                forms = [form.text or "" for form in translation.iter("numerusform")]
                with self.subTest(locale=path.name, source=source):
                    self.assertNotEqual(translation.get("type"), "unfinished")
                    self.assertEqual(len(forms), 2)
                    for form in forms:
                        self.assertIn("%n", form)
                        self.assertNotIn("(s)", form)


class TestShippedFrenchLocale(unittest.TestCase):
    """The locale that proves the pipeline end to end."""

    @classmethod
    def setUpClass(cls):
        cls.path = I18N / "geoserver_manager_fr.ts"
        cls.tree = ElementTree.parse(cls.path)

    def test_it_declares_french(self):
        self.assertEqual(self.tree.getroot().get("language"), "fr")

    def test_it_carries_finished_translations_for_mixin_contexts(self):
        translated = {}
        for context in self.tree.getroot().findall("context"):
            name = context.find("name").text
            done = [
                message
                for message in context.findall("message")
                if (message.find("translation") is not None)
                and message.find("translation").get("type") != "unfinished"
                and (message.find("translation").text or "").strip()
            ]
            if done:
                translated[name] = len(done)

        self.assertTrue(translated, "no finished translation at all")
        # at least one real tab mixin, or the bug this fixes is unproven
        self.assertTrue(
            set(translated) & set(CONTEXTS.values()),
            f"nothing translated in a mixin context: {sorted(translated)}",
        )

    def test_every_context_it_names_still_exists_in_the_code(self):
        """Catches a .ts left behind by a renamed class."""
        named = {
            context.find("name").text
            for context in self.tree.getroot().findall("context")
        }
        self.assertEqual(named - KNOWN_CONTEXTS, set())


if __name__ == "__main__":
    unittest.main()
