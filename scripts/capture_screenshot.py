"""Capture the documentation's screenshots, connected to the local sandbox.

Start the sandbox first, so the shots show GeoServer's demo data:

    docker compose up -d
    QT_QPA_PLATFORM=offscreen QT_SCALE_FACTOR=2 python3 scripts/capture_screenshot.py

Every tab, the main forms, the map preview and the settings page land in
docs/static/screenshots/, and the Layers tab also in
docs/static/screenshot-layers.png for the home page. Pass `--only layers` (a
shot's name, repeatable) to redo just some of them.

The dialog is grabbed off screen, so nothing else on the desktop lands in the
image and the result is the same on every machine. `QT_SCALE_FACTOR=2` renders
it at twice the size, which stays sharp on a high-resolution display. Each PNG
is reduced to 256 colours afterwards: a flat interface looks the same and the
file stays under pre-commit's 500 KB ceiling.

The English translation is compiled from its .ts with lrelease
(`sudo apt install qttools5-dev-tools`) and installed first, as the plugin
loads it in any locale without its own: without it a count reads "item(s)".

Loads run in a task in the real dialog, so this uses the tests' `SyncDialog`,
which runs the same fetch on the calling thread. The widgets are the real ones.
Forms are modal, so `exec()` is replaced by a grab that closes the form
without saving: nothing on the server changes.
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUTPUT_DIR = ROOT / "docs/static/screenshots"
HOME_SHOT = ROOT / "docs/static/screenshot-layers.png"
ENGLISH_TS = ROOT / "geoserver_manager/resources/i18n/geoserver_manager_en.ts"
DEFAULT_URL = "http://localhost:8080/geoserver"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="geoserver")
    parser.add_argument("--only", action="append", help="a shot name, repeatable")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    return parser.parse_args()


def save(widget, path):
    """Grab a widget into a 256-colour PNG."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    widget.grab().save(str(path))
    with Image.open(path) as shot:
        flat = shot.convert("RGB").quantize(colors=256, dither=Image.NONE)
        flat.save(path, optimize=True)
    print(f"{path} ({path.stat().st_size // 1024} KB)")


def settle(app, seconds=0.3):
    """Let Qt paint, and let a map canvas finish rendering."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def english_translator(folder):
    """The plugin's English translation, compiled into `folder` by lrelease."""
    from qgis.PyQt.QtCore import QTranslator

    lrelease = shutil.which("lrelease")
    if lrelease is None:
        raise SystemExit("lrelease is missing: sudo apt install qttools5-dev-tools")
    compiled = Path(folder) / "geoserver_manager_en.qm"
    subprocess.run(
        [lrelease, "-silent", str(ENGLISH_TS), "-qm", str(compiled)], check=True
    )
    translator = QTranslator()
    if not translator.load(str(compiled)):
        raise SystemExit(f"Could not load {compiled}")
    return translator


def main():
    args = parse_args()

    from qgis.PyQt.QtCore import QCoreApplication, Qt
    from qgis.testing import start_app

    # The test app does not enable Qt5's high-DPI icons like QGIS does.
    # Qt6 enables them by default and no longer needs this attribute.
    high_dpi = getattr(Qt.ApplicationAttribute, "AA_UseHighDpiPixmaps", None)
    if high_dpi is not None:
        QCoreApplication.setAttribute(high_dpi)
    app = start_app()
    # Both kept to the end: Qt reads the compiled file while it translates.
    i18n = tempfile.TemporaryDirectory()
    translator = english_translator(i18n.name)
    QCoreApplication.installTranslator(translator)

    from geoserver_manager.toolbelt.dependencies import ensure_dependencies

    ensure_dependencies()

    from qgis.PyQt.QtWidgets import QDialog

    from geoserver_manager.gui.dlg_preview import LayerPreviewDialog
    from geoserver_manager.gui.dlg_settings import ConfigOptionsPage
    from geoserver_manager.toolbelt.preferences import PlgSettingsStructure
    from tests.qgis.sync_dialog import SyncDialog

    class ScreenshotDialog(SyncDialog):
        """The real dialog, with the credentials passed in rather than stored."""

        def _build_client(self, settings):
            from geoservercloud import GeoServerCloud

            return GeoServerCloud(url=args.url, user=args.user, password=args.password)

    dialog = ScreenshotDialog()
    dialog.plg_settings.get_plg_settings = lambda: PlgSettingsStructure(
        geoserver_url=args.url, geoserver_auth_cfg_id="screenshot"
    )
    dialog.resize(args.width, args.height)
    dialog.show()
    app.processEvents()
    dialog.refresh_ui()
    settle(app)
    if dialog.gs is None:
        raise SystemExit(f"Not connected to {args.url}. Is the sandbox running?")
    print(dialog.lbl_status.text())

    # A form's exec() grabs it into `target` and closes it unsaved.
    target = {}

    def grab_instead_of_exec(form):
        form.show()
        settle(app)
        # The offscreen screen is 400 px tall at 2x, which caps the form's own
        # growth; a real screen does not, so grow it here the same way.
        form.resize(form.width(), form.needed_height())
        if target.get("prepare"):
            target.pop("prepare")(form)
        settle(app)
        save(form, target["path"])
        form.close()
        return QDialog.DialogCode.Rejected

    QDialog.exec = grab_instead_of_exec
    QDialog.exec_ = grab_instead_of_exec

    def tab(index):
        dialog.navList.setCurrentRow(index)
        settle(app)

    def row(name):
        return next(r for r in dialog._all_rows if r[0] == name)

    def form(name, open_form, prepare=None):
        target["path"] = args.output_dir / f"{name}.png"
        target["prepare"] = prepare
        open_form()

    def preview():
        # The demo layers are public: no auth config, which this run never stores.
        dialog.plg_settings.get_plg_settings = lambda: PlgSettingsStructure(
            geoserver_url=args.url
        )
        dialog._preview_layer(row("states"))
        window = dialog.findChildren(LayerPreviewDialog)[-1]
        window.resize(900, 650)
        settle(app, 4)
        save(window, args.output_dir / "layer-preview.png")
        window.close()

    def settings():
        page = ConfigOptionsPage(None)
        page.show()
        settle(app)
        page.resize(900, page.sizeHint().height())
        settle(app)
        save(page, args.output_dir / "settings.png")

    tabs = (
        (
            "workspaces",
            0,
            [("workspace-edit", lambda: dialog._show_workspace_info(row("topp")))],
        ),
        ("datastores", 1, [("datastore-add", dialog._add_datastore)]),
        ("coverage-stores", 2, [("coverage-store-add", dialog._add_coverage_store)]),
        ("cascaded-stores", 3, [("cascaded-store-add", dialog._add_cascaded_store)]),
        (
            "layers",
            4,
            [
                ("layer-publish", dialog._publish_layer),
                ("layer-details", lambda: dialog._show_layer_info(row("states"))),
                ("layer-preview", None),
            ],
        ),
        (
            "layer-groups",
            5,
            [
                ("layer-group-add", dialog._add_layer_group),
                (
                    "layer-group-edit",
                    lambda: dialog._show_layer_group_info(row("tasmania")),
                ),
            ],
        ),
        (
            "styles",
            6,
            [("style-edit", lambda: dialog._show_style_info(row("population")))],
        ),
        (
            "tile-cache",
            7,
            [
                (
                    "tile-cache-edit",
                    lambda: dialog._show_gwc_layer_info(row("topp:states")),
                ),
                (
                    "tile-cache-seed",
                    lambda: dialog._seed_gwc_layer(row("topp:states")),
                ),
            ],
        ),
        (
            "server",
            8,
            [("server-service", lambda: dialog._show_server_section(row("WFS")))],
        ),
    )

    # The first workspace, cite, has no datastore: pick one with tables.
    prepare = {
        "layer-publish": lambda f: f.get_widget("workspace").setCurrentText("sf"),
        # The layers and their styles are what an edit is mostly about.
        "layer-group-edit": lambda f: f._tabs.setCurrentIndex(1),
    }

    wanted = set(args.only or [])
    for name, index, forms in tabs:
        names = {name, *(f for f, _ in forms)}
        if wanted and not wanted & names:
            continue
        tab(index)
        # An empty list (the demo has no cascaded store) shows nothing useful.
        if (not wanted or name in wanted) and dialog._all_rows:
            save(dialog, args.output_dir / f"{name}.png")
        for form_name, open_form in forms:
            if wanted and form_name not in wanted:
                continue
            if form_name == "layer-preview":
                preview()
            else:
                form(form_name, open_form, prepare.get(form_name))
    if not wanted or "settings" in wanted:
        settings()

    # Only a real run replaces the home page's image, not a scratch one.
    layers_shot = args.output_dir / "layers.png"
    if args.output_dir == OUTPUT_DIR and layers_shot.exists():
        shutil.copyfile(layers_shot, HOME_SHOT)


if __name__ == "__main__":
    main()
