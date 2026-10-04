#! python3  # noqa: E265

"""Main plugin module."""

# standard
from functools import partial
from pathlib import Path

# PyQGIS
from qgis.core import Qgis, QgsSettings
from qgis.gui import QgisInterface, QgsGui
from qgis.PyQt.QtCore import (
    QCoreApplication,
    QEvent,
    QLocale,
    QObject,
    QTimer,
    QTranslator,
    QUrl,
)
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import QAction, QApplication, QMessageBox

# project
from geoserver_manager.__about__ import (
    DIR_PLUGIN_ROOT,
    __title__,
    __uri_homepage__,
)
from geoserver_manager.gui.dlg_main import GeoServerMainDialog
from geoserver_manager.gui.dlg_settings import PlgOptionsFactory
from geoserver_manager.gui.icons import icon
from geoserver_manager.gui.layer_tree import LayerTreeMenu
from geoserver_manager.toolbelt.log_handler import PlgLogger
from geoserver_manager.toolbelt.preferences import PlgOptionsManager
from geoserver_manager.toolbelt.probe import forget_qgis_proxy

# ############################################################################
# ########## Classes ###############
# ##################################


class _PaletteWatch(QObject):
    """Calls back when the watched window's palette or style changes.

    An event filter, not the application's palette-changed signal: Qt 6
    deprecated that signal and PyQt6 does not bind it, so connecting it
    stopped the plugin from loading on a PyQt6 QGIS.
    """

    def __init__(self, on_change, window):
        super().__init__(window)
        self._on_change = on_change

    def eventFilter(self, _watched, event):  # noqa: N802 (Qt's own spelling)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            self._on_change()
        return False


class GeoServerManagerPlugin:
    def __init__(self, iface: QgisInterface) -> None:
        """Constructor.

        :param iface: An interface instance that will be passed to this class which \
        provides the hook by which you can manipulate the QGIS application at run time.
        :type iface: QgsInterface
        """
        self.iface = iface
        self.log = PlgLogger().log
        self.plg_settings = PlgOptionsManager()
        self.main_dialog = None
        self.layer_tree_menu = None

        # translation
        # initialize the locale
        self.locale: str = QgsSettings().value("locale/userLocale", QLocale().name())[
            0:2
        ]
        # A locale the plugin has no translation for falls back to English's:
        # its plural forms, without which "%n layer(s)" read "(s)".
        candidates = [
            DIR_PLUGIN_ROOT / "resources" / "i18n" / f"{DIR_PLUGIN_ROOT.name}_{code}.qm"
            for code in (self.locale, "en")
        ]
        locale_path: Path = next(
            (path for path in candidates if path.exists()), candidates[0]
        )
        self.log(
            message=f"Translation: {self.locale}, {locale_path}",
            log_level=Qgis.MessageLevel.NoLevel,
        )
        self.translator = None
        if locale_path.exists():
            self.translator = QTranslator()
            self.translator.load(str(locale_path.resolve()))
            QCoreApplication.installTranslator(self.translator)

        # Ensure dependencies are available
        from geoserver_manager.toolbelt.dependencies import ensure_dependencies

        self.dependencies_available = ensure_dependencies()

    def initGui(self) -> None:  # noqa: N802
        """Set up plugin UI elements."""

        # settings page within the QGIS preferences menu
        self.options_factory = PlgOptionsFactory()
        self.iface.registerOptionsWidgetFactory(self.options_factory)

        # -- Actions
        self.action_help = QAction(
            icon("help", for_menu=True),
            self.tr("Help"),
            self.iface.mainWindow(),
        )
        self.action_help.triggered.connect(
            partial(QDesktopServices.openUrl, QUrl(__uri_homepage__))
        )

        self.action_settings = QAction(
            icon("settings", for_menu=True),
            self.tr("Settings"),
            self.iface.mainWindow(),
        )
        self.action_settings.triggered.connect(
            lambda: self.iface.showOptionsDialog(
                currentPage="mOptionsPage{}".format(__title__)
            )
        )

        self.action_main = QAction(
            icon("plugin"),
            __title__,
            self.iface.mainWindow(),
        )
        self.action_main.triggered.connect(self.run)

        # -- Toolbar
        self.iface.addToolBarIcon(self.action_main)

        # -- Menu
        self.iface.addPluginToMenu(__title__, self.action_main)
        self.iface.addPluginToMenu(__title__, self.action_settings)
        self.iface.addPluginToMenu(__title__, self.action_help)

        # -- Help menu

        # documentation
        self._help_separator = self.iface.pluginHelpMenu().addSeparator()
        self.action_help_plugin_menu_documentation = QAction(
            icon("plugin", for_menu=True),
            self.tr("{} documentation").format(__title__),
            self.iface.mainWindow(),
        )
        self.action_help_plugin_menu_documentation.triggered.connect(
            partial(QDesktopServices.openUrl, QUrl(__uri_homepage__))
        )

        self.iface.pluginHelpMenu().addAction(
            self.action_help_plugin_menu_documentation
        )

        window = self.iface.mainWindow()
        self._icon_refresh_timer = QTimer(window)
        self._icon_refresh_timer.setSingleShot(True)
        self._icon_refresh_timer.timeout.connect(self._refresh_menu_icons)
        self._palette_watch = _PaletteWatch(self._queue_menu_icon_refresh, window)
        window.installEventFilter(self._palette_watch)
        self._refresh_menu_icons()

        # -- Layer tree context menu: push / apply the clicked layer's style.
        # The connection is the main dialog's, which exists once run() has
        # opened it; until then the entries are disabled and say so.
        self.layer_tree_menu = LayerTreeMenu(
            self.iface, dialog=lambda: self.main_dialog, open_dialog=self.run
        )

        # -- Settings saved in QGIS's Options reach a dialog that is already open.
        QgsGui.instance().optionsChanged.connect(self._on_options_changed)

    def _on_options_changed(self):
        # Queued: the dialog's own Settings button reconnects once Options closes.
        QTimer.singleShot(0, self._reconnect_if_settings_changed)

    def _reconnect_if_settings_changed(self):
        """Reconnect a dialog whose client the saved settings no longer
        describe: its table and its links would name two servers."""
        dialog = self.main_dialog
        if dialog is None or dialog.gs is None:
            return  # nothing held: the next click connects with what is saved
        if not self._connected_as_saved(dialog, self.plg_settings.get_plg_settings()):
            dialog.refresh_ui(show_message=True)

    @staticmethod
    def _connected_as_saved(dialog, settings) -> bool:
        """True when the dialog holds a client built from `settings`."""
        return dialog.gs is not None and dialog.gs_connection == settings.connection()

    def _queue_menu_icon_refresh(self):
        # Once, after the window's children have the new palette too.
        self._icon_refresh_timer.start(0)

    def _refresh_menu_icons(self):
        window = self.iface.mainWindow()
        palette = window.palette() if window is not None else QApplication.palette()
        self.action_main.setIcon(icon("plugin", palette))
        self.action_help.setIcon(icon("help", palette, for_menu=True))
        self.action_settings.setIcon(icon("settings", palette, for_menu=True))
        self.action_help_plugin_menu_documentation.setIcon(
            icon("plugin", palette, for_menu=True)
        )

    def tr(self, message: str) -> str:
        """Get the translation for a string using Qt translation API.

        :param message: string to be translated.
        :type message: str

        :returns: Translated version of message.
        :rtype: str
        """
        return QCoreApplication.translate(self.__class__.__name__, message)

    def unload(self) -> None:
        """Cleans up when plugin is disabled/uninstalled."""
        try:
            QgsGui.instance().optionsChanged.disconnect(self._on_options_changed)
        except TypeError:
            pass  # already disconnected
        self.iface.mainWindow().removeEventFilter(self._palette_watch)
        self._palette_watch.deleteLater()
        self._icon_refresh_timer.stop()
        self._icon_refresh_timer.deleteLater()
        # -- The layer-tree hook first: left connected, it would fire into a
        # dead plugin after the next reload.
        if self.layer_tree_menu:
            self.layer_tree_menu.unload()
            self.layer_tree_menu = None

        # -- Close and destroy the main dialog (it is a child of the QGIS main
        # window, so dropping the reference alone would keep it alive)
        if self.main_dialog:
            self.main_dialog.unloading = True  # its close logs what runs on
            self.main_dialog.close()
            self.main_dialog.deleteLater()
            self.main_dialog = None
        # -- The proxy exported for the dialog's requests, its password included
        forget_qgis_proxy()

        # -- Clean up menu and toolbar
        self.iface.removeToolBarIcon(self.action_main)
        self.iface.removePluginMenu(__title__, self.action_main)
        self.iface.removePluginMenu(__title__, self.action_help)
        self.iface.removePluginMenu(__title__, self.action_settings)

        # -- Clean up preferences panel in QGIS settings
        self.iface.unregisterOptionsWidgetFactory(self.options_factory)

        # remove from QGIS help/extensions menu
        if self.action_help_plugin_menu_documentation:
            self.iface.pluginHelpMenu().removeAction(
                self.action_help_plugin_menu_documentation
            )
        if self._help_separator:
            self.iface.pluginHelpMenu().removeAction(self._help_separator)
        if self.translator:
            QCoreApplication.removeTranslator(self.translator)

        # remove actions
        del self.action_main
        del self.action_settings
        del self.action_help

    def run(self):
        """Open the main plugin dialog."""
        if not self.dependencies_available:
            # Try again, and say why once more: the explanation was shown once
            # at QGIS startup, and a toolbar click that does nothing says less.
            from geoserver_manager.toolbelt.dependencies import ensure_dependencies

            self.dependencies_available = ensure_dependencies()
            if not self.dependencies_available:
                return

        settings = self.plg_settings.get_plg_settings()

        if not settings.has_credentials():
            QMessageBox.information(
                self.iface.mainWindow(),
                self.tr("GeoServer Manager: first connection"),
                self.tr(
                    "Welcome to GeoServer Manager.\n\nThe settings page opens next. "
                    "Enter the URL, user name and password of your GeoServer there. "
                    "Test connection tells you whether they work before you save."
                ),
            )
            self.iface.showOptionsDialog(currentPage=f"mOptionsPage{__title__}")

            # Re-check in case they cancelled
            settings = self.plg_settings.get_plg_settings()
            if not settings.has_credentials():
                return

        dialog = self.main_dialog
        if dialog is None:
            dialog = self.main_dialog = GeoServerMainDialog(
                self.iface.mainWindow(), self.iface
            )
        if dialog.isVisible() and self._connected_as_saved(dialog, settings):
            # Open and connected as saved: the click brings it forward. It used
            # to reconnect and reload the tab, or refuse while an upload ran.
            dialog.raise_()
            dialog.activateWindow()
            return
        # refresh_ui only starts the connection probe. It runs in a QgsTask
        # and calls back when it lands, so the window paints straight away
        # even against a host that swallows the SYN (VPN down, firewall DROP).
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        dialog.refresh_ui()
