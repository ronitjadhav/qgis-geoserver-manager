#! python3  # noqa: E265

"""
Plugin settings form integrated into QGIS 'Options' menu.
"""

# standard
import ipaddress
import platform
from functools import partial
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlparse

# PyQGIS
from qgis.core import Qgis
from qgis.gui import QgsOptionsPageWidget, QgsOptionsWidgetFactory
from qgis.PyQt import uic
from qgis.PyQt.QtCore import QEvent, QSize, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QWidget,
)

# project
from geoserver_manager.__about__ import (
    __title__,
    __uri_homepage__,
    __uri_tracker__,
    __version__,
)
from geoserver_manager.gui.icons import icon
from geoserver_manager.gui.theme import hint_colour, status_colour
from geoserver_manager.toolbelt.log_handler import PlgLogger
from geoserver_manager.toolbelt.preferences import (
    PlgOptionsManager,
    PlgSettingsStructure,
)
from geoserver_manager.toolbelt.probe import probe, proxies_for

# ############################################################################
# ########## Classes ###############
# ##################################


class ConfigOptionsPage(QgsOptionsPageWidget):
    """Settings form embedded into QGIS 'options' menu."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.log: Callable = PlgLogger().log
        self.plg_settings = PlgOptionsManager()

        # load UI and set objectName
        uic.loadUi(Path(__file__).parent / f"{Path(__file__).stem}.ui", self)
        self.setObjectName("mOptionsPage{}".format(__title__))
        self._icon_refresh_timer = QTimer(self)
        self._icon_refresh_timer.setSingleShot(True)
        self._icon_refresh_timer.timeout.connect(self._refresh_icons)
        self.initGui()

    def initGui(self) -> None:  # noqa: N802
        """Set up UI elements."""
        report_context_message: str = quote(
            "> Reported from plugin settings\n\n"
            f"- operating system: {platform.system()} "
            f"{platform.release()}_{platform.version()}\n"
            f"- QGIS: {Qgis.QGIS_VERSION}\n"
            f"- plugin version: {__version__}\n"
        )

        # header
        self.lbl_title.setText(
            self.tr("{title} - Version {version}").format(
                title=__title__, version=__version__
            )
        )

        # customization
        self._refresh_icons()
        self.btn_help.pressed.connect(
            partial(QDesktopServices.openUrl, QUrl(__uri_homepage__))
        )

        self.btn_report.pressed.connect(
            partial(
                QDesktopServices.openUrl,
                QUrl(
                    f"{__uri_tracker__}new/?"
                    "template=10_bug_report.yml"
                    f"&about-info={report_context_message}"
                ),
            )
        )

        self.btn_reset.pressed.connect(self.on_reset_settings)

        self.btn_test_connection.clicked.connect(self.test_connection)
        self._build_profile_row()
        self._build_encryption_row()
        # A result describes the fields as they were; editing any of them ends it.
        for field in (self.txt_gs_url, self.txt_gs_username, self.txt_gs_password):
            field.textChanged.connect(self.lbl_test_result.clear)
        self.opt_verify_tls.toggled.connect(self.lbl_test_result.clear)

        # load previously saved settings
        self.load_settings()

    def _refresh_icons(self):
        self.btn_help.setIcon(icon("help", self.btn_help.palette()))
        self.btn_report.setIcon(icon("report-issue", self.btn_report.palette()))
        self.btn_reset.setIcon(icon("reset-settings", self.btn_reset.palette()))
        for button in (self.btn_help, self.btn_report, self.btn_reset):
            button.setIconSize(QSize(20, 20))

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            if hasattr(self, "_icon_refresh_timer"):
                self._icon_refresh_timer.start(0)

    @staticmethod
    def _password_travels_in_clear(url: str, username: str, password: str) -> bool:
        """True when saving these would put a password on the wire unencrypted.

        The plugin authenticates with HTTP Basic, so over `http://` the
        password is readable by anything on the path. Loopback is exempt:
        those requests never leave the machine, and a warning on every local
        sandbox (this repo ships one) is a warning nobody reads.
        """
        if not (username or password):
            return False
        parsed = urlparse((url or "").strip())
        if parsed.scheme != "http":
            return False
        host = (parsed.hostname or "").casefold()
        if host == "localhost" or host.endswith(".localhost"):
            return False
        try:
            return not ipaddress.ip_address(host).is_loopback
        except ValueError:
            # A host name the plugin cannot resolve to a loopback address.
            return True

    def apply(self) -> None:
        """Save settings from UI to QgsSettings + QgsAuthManager."""
        settings: PlgSettingsStructure = self.plg_settings.get_plg_settings()

        # misc
        settings.debug_mode = self.opt_debug.isChecked()
        settings.version = __version__

        self._commit_fields()
        # Profiles edited but not shown: their connection and credentials,
        # through the same checks as the shown one.
        for profile in self._profiles:
            name = profile["name"]
            if name == self._shown or not self._is_dirty(name):
                continue
            stored = PlgSettingsStructure(
                geoserver_url=profile["url"],
                geoserver_auth_cfg_id=profile["auth_cfg_id"],
                geoserver_verify_tls=profile["verify_tls"],
                geoserver_username=profile.get("username", ""),
                geoserver_password=profile.get("password", ""),
            )
            self._store_connection(stored, profile=name, **self._buffers[name])
            self._keep(profile, stored, self._buffers[name]["encrypted"])
        # The shown profile goes through `settings`, the connection everything
        # reads, so it becomes the active one: with its own auth config, never
        # the one of the profile that was active before.
        shown = self._profile(self._shown)
        active = self.plg_settings.active_profile_name()
        if shown is not None:
            settings.geoserver_url = shown["url"]
            settings.geoserver_auth_cfg_id = shown["auth_cfg_id"]
            settings.geoserver_username = shown.get("username", "")
            settings.geoserver_password = shown.get("password", "")
            # Untouched, the checkbox is not read below: the previous
            # profile's setting was kept, and then saved into this one.
            settings.geoserver_verify_tls = bool(shown.get("verify_tls", True))
        elif active and self._profile(active) is None:
            # The active profile was removed, and no other is shown: nothing is
            # left to connect with.
            settings.geoserver_url = ""
            settings.geoserver_auth_cfg_id = ""
            settings.geoserver_username = settings.geoserver_password = ""  # nosec B105
        # QGIS calls apply() on every options page for any OK. An untouched
        # profile is left alone: its fields hold ("", "") when the master
        # password prompt was dismissed, and "both blank" means "forget the
        # credentials", which deleted them for an unrelated OK.
        if shown is None or self._is_dirty(shown["name"]):
            self._store_connection(
                settings,
                profile=shown["name"] if shown else None,
                url=self.txt_gs_url.text(),
                username=self.txt_gs_username.text(),
                password=self.txt_gs_password.text(),
                verify_tls=self.opt_verify_tls.isChecked(),
                encrypted=self.opt_encrypt.isChecked(),
            )

        # dump settings into QgsSettings
        self.plg_settings.save_from_object(settings)
        if shown is None and settings.geoserver_url:
            # No profile yet: the first save makes one, named after the host.
            shown = {"name": urlparse(settings.geoserver_url).netloc or "GeoServer"}
            self._profiles.append(shown)
        if shown is not None:
            self._keep(shown, settings, self.opt_encrypt.isChecked())
        for auth_cfg_id in self._removed_auth:
            PlgSettingsStructure(geoserver_auth_cfg_id=auth_cfg_id).remove_credentials()
        self._removed_auth = []
        self.plg_settings.save_profiles(self._profiles)
        self.plg_settings.set_value_from_key(
            "active_profile", shown["name"] if shown else ""
        )

        if __debug__:
            self.log(
                message="DEBUG - Settings successfully saved.",
                log_level=Qgis.MessageLevel.NoLevel,
            )

    def _store_connection(
        self,
        settings,
        url,
        username,
        password,
        verify_tls,
        profile=None,
        encrypted=False,
    ):
        """Validate one profile's URL and store its credentials, into `settings`.

        `settings` is the active connection for the shown profile, or a
        stand-in built for another edited one; both are updated in place.
        `encrypted` puts the credentials in QgsAuthManager; else they are
        plain text in the settings, the default, and an auth config the
        plugin made for them before is removed. With more than one profile,
        every warning says which one it is about.
        """

        def warn(message, level=Qgis.MessageLevel.Warning):
            if profile and len(self._profiles) > 1:
                message = self.tr("Connection '{}': {}").format(profile, message)
            self.log(message=message, log_level=level, push=True)

        settings.geoserver_verify_tls = verify_tls

        # geoserver URL (not sensitive, stored in QgsSettings)
        url = url.strip()
        problem = self._url_problem(url) if url else None
        if problem:
            # apply() cannot stop the options dialog from closing, so keep the
            # previous URL and warn: dropping out here would also discard the
            # credentials the user just typed.
            warn(f"{problem} {self.tr('URL not saved.')}")
        else:
            settings.geoserver_url = url

        # credentials: plain text in the settings, or encrypted in QgsAuthManager
        loaded = self._loaded.get(profile) if profile else None
        # Blank fields forget the stored credentials only when they showed
        # some: with the master password declined they read blank, and an
        # edit of the URL alone deleted the credentials.
        showed_some = loaded is None or bool(loaded["username"] or loaded["password"])
        if not (username or password):
            if showed_some:
                if settings.geoserver_auth_cfg_id:
                    settings.remove_credentials()
                    settings.geoserver_auth_cfg_id = ""
                settings.geoserver_username = ""
                settings.geoserver_password = ""  # nosec B105
        elif not encrypted:
            # The plain store: an auth config the plugin made for them goes.
            if settings.geoserver_auth_cfg_id:
                settings.remove_credentials()
                settings.geoserver_auth_cfg_id = ""
            settings.geoserver_username = username
            settings.geoserver_password = password
        else:
            auth_cfg_id = settings.save_credentials(username, password)
            if auth_cfg_id:
                settings.geoserver_auth_cfg_id = auth_cfg_id
                settings.geoserver_username = ""
                settings.geoserver_password = ""  # nosec B105
            else:
                # Typically the user dismissed the master password prompt
                warn(
                    self.tr(
                        "QGIS did not store the user name and password in its "
                        "authentication database. Set the master password, then save "
                        "again."
                    ),
                    Qgis.MessageLevel.Critical,
                )

        # The saved URL, not the typed one: a refused one can hold user:pass@.
        saved_url = settings.geoserver_url
        if self._password_travels_in_clear(saved_url, username, password):
            # Informs rather than refuses: apply() cannot stop the options
            # dialog from closing, and a plain-HTTP server on a trusted
            # network is a legitimate setup. The settings are saved either way.
            warn(
                self.tr(
                    "{url} is plain HTTP, so every request sends the password "
                    "unencrypted. Use https:// if the server offers it."
                ).format(url=saved_url)
            )

    def _url_problem(self, url):
        """What is wrong with a GeoServer URL, or None. One check, two callers
        (Save and Test connection), so their messages cannot drift apart."""
        parsed = urlparse(url)
        # urlparse lowers the scheme: HTTP:// is a URL every request accepts.
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return self.tr("The GeoServer URL must start with http:// or https://.")
        if parsed.username or parsed.password:
            # user:pass@host would surface in the window title, the status
            # line, every error banner and the persistent QGIS log.
            return self.tr(
                "Take the user name and password out of the URL: the fields "
                "below carry them."
            )
        return None

    def test_connection(self) -> None:
        """Probe the server with the fields as typed, saved or not."""
        url = self.txt_gs_url.text().strip()
        problem = self._url_problem(url)
        if problem:
            self._show_test_result(problem, "error")
            return
        auth = (self.txt_gs_username.text(), self.txt_gs_password.text())
        self._show_test_result(self.tr("Testing…"), "busy")
        QApplication.processEvents()  # paint the line before the blocking probe
        # Blocks the Options dialog for up to PROBE_TIMEOUT (10 s)
        # against a dead host; QgsTask.fromFunction plus a deleted-widget guard
        # if that ever hurts.
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            # Not use_qgis_proxy: the dialog's requests read the environment.
            problem = probe(
                url, auth, self.opt_verify_tls.isChecked(), proxies_for(url)
            )
        finally:
            QApplication.restoreOverrideCursor()
        if problem is None:
            self._show_test_result(self.tr("Connected. GeoServer answered."), "ok")
        else:
            _status, message = problem
            self._show_test_result(message, "error")

    def _show_test_result(self, text: str, kind: str) -> None:
        """Show the outcome under the button, in a colour this theme can carry."""
        self.lbl_test_result.setText(text)
        colour = status_colour(kind, self.palette())
        self.lbl_test_result.setStyleSheet(f"color: {colour};" if colour else "")

    def load_settings(self) -> None:
        """Load options from QgsSettings + QgsAuthManager into UI form."""
        # The saved value, not the environment's: an OK would store that one.
        self.opt_debug.setChecked(
            bool(self.plg_settings.get_value_from_key("debug_mode", False, bool))
        )

        # Profiles: edits are kept per profile until Save (or dropped by
        # Cancel); a removed profile's credentials go only on Save.
        self._profiles = [dict(profile) for profile in self.plg_settings.get_profiles()]
        self._buffers, self._loaded, self._removed_auth = {}, {}, []
        active = self.plg_settings.active_profile_name()
        self._shown = None
        self._fill_profile_combo(
            active or (self._profiles[0]["name"] if self._profiles else None)
        )

    def on_reset_settings(self) -> None:
        """Reset settings to default values, every profile included."""
        count = len(self.plg_settings.get_profiles())
        if count and not self._confirm_reset(count):
            return
        for profile in self.plg_settings.get_profiles():
            PlgSettingsStructure(
                geoserver_auth_cfg_id=profile.get("auth_cfg_id", "")
            ).remove_credentials()
        # Remove the auth config if it exists
        current = self.plg_settings.get_plg_settings()
        current.remove_credentials()

        default_settings: PlgSettingsStructure = PlgSettingsStructure()
        self.plg_settings.save_from_object(default_settings)
        self.plg_settings.save_profiles([])
        self.plg_settings.set_value_from_key("active_profile", "")
        self.load_settings()

    def _confirm_reset(self, count) -> bool:
        """Reset saves at once: Cancel on the options dialog cannot undo it."""
        answer = QMessageBox.question(
            self,
            self.tr("Reset settings"),
            self.tr(
                "Remove %n saved connection(s) and their stored passwords? This "
                "cannot be undone.",
                None,
                count,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    # -- Server profiles -------------------------------------------------

    def _build_profile_row(self) -> None:
        """A Profile row above the URL: pick, add, remove."""
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        self.cmb_profile = QComboBox(row)
        self.cmb_profile.setToolTip(
            self.tr(
                "The server connection shown below. Saving makes it the active one."
            )
        )
        self.btn_profile_add = QPushButton(self.tr("Add…"), row)
        self.btn_profile_remove = QPushButton(self.tr("Remove"), row)
        layout.addWidget(self.cmb_profile, 1)
        layout.addWidget(self.btn_profile_add)
        layout.addWidget(self.btn_profile_remove)
        self.btn_profile_remove.setToolTip(
            self.tr("Removes the shown connection when you save. Cancel keeps it.")
        )
        self.formLayout.insertRow(0, QLabel(self.tr("Connection"), self), row)
        # Which profile the dialog connects to, and what Save changes: the list
        # alone does not say, and saving another one switches servers.
        self.lbl_profile_note = QLabel(self)
        self.lbl_profile_note.setWordWrap(True)
        self.lbl_profile_note.setStyleSheet(f"color: {hint_colour(self.palette())};")
        self.formLayout.insertRow(1, self.lbl_profile_note)
        # Built after the .ui, the row came last on Tab, after Reset.
        chain = (
            self.cmb_profile,
            self.btn_profile_add,
            self.btn_profile_remove,
            self.txt_gs_url,
            self.txt_gs_username,
            self.txt_gs_password,
            self.opt_verify_tls,
            self.btn_test_connection,
            self.btn_help,
            self.btn_report,
            self.opt_debug,
            self.btn_reset,
        )
        for first, then in zip(chain, chain[1:]):
            QWidget.setTabOrder(first, then)
        self.cmb_profile.currentTextChanged.connect(self._on_profile_changed)
        self.btn_profile_add.clicked.connect(self._add_profile)
        self.btn_profile_remove.clicked.connect(self._remove_profile)

    def _build_encryption_row(self) -> None:
        """Under the password: where it is kept. Plain text by default."""
        self.opt_encrypt = QCheckBox(
            self.tr("Keep the password in QGIS's encrypted authentication database"),
            self,
        )
        self.opt_encrypt.setToolTip(
            self.tr(
                "Ticked, QGIS asks for its master password once per session. It does "
                "not ask when your system keyring holds that password (Settings, "
                "Options, Authentication)."
            )
        )
        self.lbl_encrypt_note = QLabel(
            self.tr(
                "Unticked, the password is plain text in your QGIS settings and "
                "in the layers the plugin adds."
            ),
            self,
        )
        self.lbl_encrypt_note.setWordWrap(True)
        self.lbl_encrypt_note.setStyleSheet(f"color: {hint_colour(self.palette())};")
        row, _role = self.formLayout.getWidgetPosition(self.txt_gs_password)
        self.formLayout.insertRow(row + 1, self.lbl_encrypt_note)
        self.formLayout.insertRow(row + 1, "", self.opt_encrypt)
        QWidget.setTabOrder(self.txt_gs_password, self.opt_encrypt)
        QWidget.setTabOrder(self.opt_encrypt, self.opt_verify_tls)

    def _profile(self, name):
        return next((p for p in self._profiles if p["name"] == name), None)

    def _fields(self) -> dict:
        return {
            "url": self.txt_gs_url.text(),
            "username": self.txt_gs_username.text(),
            "password": self.txt_gs_password.text(),
            "verify_tls": self.opt_verify_tls.isChecked(),
            "encrypted": self.opt_encrypt.isChecked(),
        }

    def _commit_fields(self) -> None:
        """Keep what the fields say for the profile they show."""
        if self._shown is not None:
            self._buffers[self._shown] = self._fields()

    def _is_dirty(self, name) -> bool:
        return name in self._buffers and self._buffers[name] != self._loaded.get(name)

    @staticmethod
    def _keep(profile, settings, encrypted) -> None:
        """Copy a saved connection back into its profile entry."""
        profile.update(
            url=settings.geoserver_url,
            auth_cfg_id=settings.geoserver_auth_cfg_id,
            verify_tls=bool(settings.geoserver_verify_tls),
            encrypted=bool(encrypted),
            username=settings.geoserver_username,
            password=settings.geoserver_password,
        )

    def _fill_profile_combo(self, select) -> None:
        self.cmb_profile.blockSignals(True)
        self.cmb_profile.clear()
        self.cmb_profile.addItems([p["name"] for p in self._profiles])
        if not self._profiles:
            # What an empty list means. Qt 5's combo placeholder does not
            # render in every style, so it is an item, disabled with the combo;
            # no profile has this name, so nothing can select or save it.
            self.cmb_profile.addItem(self.tr("None yet. Saving creates one."))
        self.cmb_profile.setEnabled(bool(self._profiles))
        self.cmb_profile.setCurrentText(select or "")
        self.cmb_profile.blockSignals(False)
        self.btn_profile_remove.setEnabled(bool(self._profiles))
        self._show_profile(select if self._profile(select) else None)

    def _show_profile(self, name) -> None:
        """Fill the fields from a profile's edits, else from what is stored."""
        self._shown = name
        profile = self._profile(name)
        if name in self._buffers:
            values = self._buffers[name]
        elif profile is not None:
            username, password = PlgSettingsStructure(
                geoserver_auth_cfg_id=profile.get("auth_cfg_id", ""),
                geoserver_username=profile.get("username", ""),
                geoserver_password=profile.get("password", ""),
            ).get_credentials()
            values = {
                "url": profile.get("url", ""),
                "username": username,
                "password": password,
                "verify_tls": bool(profile.get("verify_tls", True)),
                "encrypted": PlgOptionsManager.profile_encrypted(profile),
            }
            self._loaded[name] = dict(values)
        else:
            values = {
                "url": "",
                "username": "",
                "password": "",  # nosec B105
                "verify_tls": True,
                "encrypted": False,
            }
        self.txt_gs_url.setText(values["url"])
        self.txt_gs_username.setText(values["username"])
        self.txt_gs_password.setText(values["password"])
        self.opt_verify_tls.setChecked(values["verify_tls"])
        self.opt_encrypt.setChecked(values["encrypted"])
        note = self._profile_note(name)
        self.lbl_profile_note.setText(note)
        self.lbl_profile_note.setVisible(bool(note))  # no blank row without one

    def _profile_note(self, shown) -> str:
        active = self.plg_settings.active_profile_name()
        if shown is None:
            return ""
        if shown == active:
            return self.tr("The active connection: the dialog connects to it.")
        if active and self._profile(active) is not None:
            return self.tr("Active: {}. Saving makes '{}' active instead.").format(
                active, shown
            )
        return self.tr("Saving makes '{}' the active connection.").format(shown)

    def _on_profile_changed(self, name) -> None:
        self._commit_fields()
        self._show_profile(name if self._profile(name) else None)

    def _add_profile(self) -> None:
        name, ok = QInputDialog.getText(
            self, self.tr("Add a Connection"), self.tr("Name of the new connection:")
        )
        name = name.strip()
        if not ok or not name:
            return
        if self._profile(name) is not None:
            QMessageBox.warning(
                self,
                self.tr("Add a Connection"),
                self.tr("A connection named '{}' already exists.").format(name),
            )
            return
        typed = self._fields() if self._shown is None else None
        self._commit_fields()
        self._profiles.append(
            {
                "name": name,
                "url": "",
                "auth_cfg_id": "",
                "verify_tls": True,
                "encrypted": False,
            }
        )
        if typed is not None:
            # No profile yet: what was typed is this first profile's, and
            # naming it must not blank the fields.
            self._buffers[name] = typed
        self._fill_profile_combo(name)

    def _remove_profile(self) -> None:
        """Drop the shown profile; its credentials go when the page is saved."""
        profile = self._profile(self._shown)
        if profile is None:
            return
        self._profiles.remove(profile)
        self._buffers.pop(profile["name"], None)
        if profile.get("auth_cfg_id"):
            self._removed_auth.append(profile["auth_cfg_id"])
        self._shown = None
        # Keep the active profile on screen (Save makes the shown one active),
        # so removing another one does not quietly switch servers.
        active = self.plg_settings.active_profile_name()
        if self._profile(active) is None:
            active = self._profiles[0]["name"] if self._profiles else None
        self._fill_profile_combo(active)


class PlgOptionsFactory(QgsOptionsWidgetFactory):
    """Factory for options widget."""

    def __init__(self) -> None:
        super().__init__()

    def icon(self) -> QIcon:
        return icon("plugin")

    def createWidget(self, parent: QWidget) -> ConfigOptionsPage:  # noqa: N802
        return ConfigOptionsPage(parent)

    def title(self) -> str:
        return __title__

    def helpId(self) -> str:  # noqa: N802
        return __uri_homepage__
