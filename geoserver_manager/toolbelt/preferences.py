#! python3  # noqa: E265

"""
Plugin settings.
"""

# standard
import json
from dataclasses import asdict, dataclass, fields
from urllib.parse import urlparse

# PyQGIS
from qgis.core import QgsApplication, QgsAuthMethodConfig, QgsSettings

# package
import geoserver_manager.toolbelt.log_handler as log_hdlr
from geoserver_manager.__about__ import __title__, __version__
from geoserver_manager.toolbelt.env_var_parser import EnvVarParser

# ############################################################################
# ########## Classes ###############
# ##################################

# QGIS_GEOSERVER_MANAGER_DEBUG_MODE=true overrides debug mode, and nothing else.
PREFIX_ENV_VARIABLE = "QGIS_GEOSERVER_MANAGER_"
# The name of every auth config the plugin creates, and the only kind it removes.
AUTH_CFG_NAME = "GeoServer Manager"


@dataclass
class PlgSettingsStructure:
    """Plugin settings structure and defaults values."""

    # global
    debug_mode: bool = False
    version: str = __version__

    # geoserver connection
    geoserver_url: str = ""
    geoserver_auth_cfg_id: str = ""
    # Off only for a private-CA / self-signed server you trust: the usual
    # on-prem case; default on so nothing is silently insecure.
    geoserver_verify_tls: bool = True
    # The credentials as plain text, the default: QGIS then never asks for
    # its master password. Empty when they are in the auth database instead
    # (the settings page's "Keep the password encrypted" box).
    geoserver_username: str = ""
    geoserver_password: str = ""

    def has_credentials(self) -> bool:
        """Check if GeoServer URL and credentials, stored either way, are set."""
        return bool(
            self.geoserver_url
            and (
                self.geoserver_auth_cfg_id
                or (self.geoserver_username and self.geoserver_password)
            )
        )

    def get_credentials(self) -> tuple:
        """The username and password: from QgsAuthManager when an auth config
        is set, else the plain-text ones.

        :return: (username, password) tuple, empty strings if unavailable.
        """
        if not self.geoserver_auth_cfg_id:
            return (self.geoserver_username, self.geoserver_password)
        auth_mgr = QgsApplication.authManager()
        auth_cfg = QgsAuthMethodConfig()
        if auth_mgr.loadAuthenticationConfig(
            self.geoserver_auth_cfg_id, auth_cfg, True
        ):
            config_map = auth_cfg.configMap()
            return (
                config_map.get("username", ""),
                config_map.get("password", ""),
            )
        return ("", "")

    def auth_method(self) -> str:
        """The auth config's method ("Basic"), or "" when it is gone.

        Needs no master password, so it tells a declined one from the rest.
        """
        return QgsApplication.authManager().configAuthMethodKey(
            self.geoserver_auth_cfg_id
        )

    def connection(self) -> tuple:
        """What a client is built from: URL, TLS setting and credentials."""
        return (
            self.geoserver_url,
            bool(self.geoserver_verify_tls),
            *self.get_credentials(),
        )

    def save_credentials(self, username: str, password: str) -> str:
        """Store username/password in QgsAuthManager (encrypted).

        Updates the auth config when the plugin made it. One that is gone,
        of another method or another program's is left as it is, and a new
        one created.

        :param username: GeoServer username.
        :param password: GeoServer password.
        :return: the auth config ID, or an empty string if the auth database
            refused the write (e.g. the master password was not entered).
        """
        auth_mgr = QgsApplication.authManager()
        auth_cfg = QgsAuthMethodConfig()

        if self._is_own_config():
            # PyQGIS returns (ok, config): the tuple alone is always true.
            auth_mgr.loadAuthenticationConfig(
                self.geoserver_auth_cfg_id, auth_cfg, True
            )
            if not auth_cfg.isValid():  # the master password was declined
                return ""
            auth_cfg.setConfig("username", username)
            auth_cfg.setConfig("password", password)
            if not auth_mgr.updateAuthenticationConfig(auth_cfg):
                return ""
            return self.geoserver_auth_cfg_id

        # Create a new auth config
        auth_cfg.setName(AUTH_CFG_NAME)
        auth_cfg.setMethod("Basic")
        auth_cfg.setConfig("username", username)
        auth_cfg.setConfig("password", password)
        stored, _ = auth_mgr.storeAuthenticationConfig(auth_cfg)
        if not stored:
            return ""
        return auth_cfg.id()

    def remove_credentials(self) -> None:
        """Remove the auth config from QgsAuthManager, if the plugin made it.

        Another config may be what other QGIS connections log in with.
        """
        if self.geoserver_auth_cfg_id:
            if self._is_own_config():
                QgsApplication.authManager().removeAuthenticationConfig(
                    self.geoserver_auth_cfg_id
                )
            self.geoserver_auth_cfg_id = ""

    def _is_own_config(self) -> bool:
        """Whether the auth config is one the plugin made: Basic, under its name.

        Read without the secrets, so no master password is asked for.
        """
        if self.auth_method() != "Basic":
            return False  # gone, or another method
        auth_cfg = QgsAuthMethodConfig()
        QgsApplication.authManager().loadAuthenticationConfig(
            self.geoserver_auth_cfg_id, auth_cfg, False
        )
        return auth_cfg.name() == AUTH_CFG_NAME


class PlgOptionsManager:
    @staticmethod
    def get_plg_settings() -> PlgSettingsStructure:
        """Load and return plugin settings as a dictionary. \
        Useful to get user preferences across plugin logic.

        :return: plugin settings
        :rtype: PlgSettingsStructure
        """
        # retrieve settings from QGIS/Qt
        settings = QgsSettings()
        settings.beginGroup(__title__)

        # map settings values to preferences object
        li_settings_values = [
            settings.value(key=i.name, defaultValue=i.default, type=i.type)
            for i in fields(PlgSettingsStructure)
        ]

        # instanciate new settings object
        options = PlgSettingsStructure(*li_settings_values)
        options.debug_mode = EnvVarParser.get_env_var(
            f"{PREFIX_ENV_VARIABLE}DEBUG_MODE", options.debug_mode
        )

        settings.endGroup()

        return options

    @staticmethod
    def get_value_from_key(key: str, default=None, exp_type=None):
        """Load and return a single plugin QSettings value by key.

        :return: plugin settings value matching key
        """
        settings = QgsSettings()
        settings.beginGroup(__title__)

        try:
            out_value = settings.value(key=key, defaultValue=default, type=exp_type)
        except Exception as err:
            log_hdlr.PlgLogger.log(
                message="Error occurred trying to get settings: {}.Trace: {}".format(
                    key, err
                )
            )
            out_value = None

        settings.endGroup()

        return out_value

    @classmethod
    def set_value_from_key(cls, key: str, value) -> bool:
        """Set plugin QSettings value using the key.

        :param key: QSettings key
        :type key: str
        :param value: value to set
        :type value: depending on the settings
        :return: operation status
        :rtype: bool
        """
        settings = QgsSettings()
        settings.beginGroup(__title__)

        try:
            settings.setValue(key, value)
            out_value = True
        except Exception as err:
            log_hdlr.PlgLogger.log(
                message="Error occurred trying to set settings: {}.Trace: {}".format(
                    key, err
                )
            )
            out_value = False

        settings.endGroup()

        return out_value

    @classmethod
    def save_from_object(cls, plugin_settings_obj: PlgSettingsStructure):
        """Save plugin settings from a dataclass object to QgsSettings.

        :param plugin_settings_obj: settings object to persist.
        """
        for k, v in asdict(plugin_settings_obj).items():
            cls.set_value_from_key(k, v)

    # -- Server profiles -----------------------------------------------
    # A profile is {"name", "url", "auth_cfg_id", "verify_tls", "encrypted",
    # "username", "password"}: the credentials in one auth config of
    # QgsAuthManager when "encrypted", else as plain text in the entry. The
    # list is JSON under "profiles". The active one is also copied into the
    # geoserver_* fields, which is all the rest of the plugin reads, so only
    # the settings page and the dialog's switcher know that profiles exist.

    @classmethod
    def get_profiles(cls) -> list:
        """The saved profiles, in order. A connection saved before profiles
        existed comes back as the first one, named after its host."""
        raw = cls.get_value_from_key("profiles", "", str) or ""
        try:
            profiles = json.loads(raw) if raw else []
        except ValueError:
            profiles = []
        profiles = [
            profile
            for profile in profiles
            if isinstance(profile, dict) and profile.get("name")
        ]
        if not profiles:
            current = cls.get_plg_settings()
            if current.geoserver_url:
                profiles = [
                    {
                        "name": urlparse(current.geoserver_url).netloc
                        or current.geoserver_url,
                        "url": current.geoserver_url,
                        "auth_cfg_id": current.geoserver_auth_cfg_id,
                        "verify_tls": bool(current.geoserver_verify_tls),
                        "encrypted": bool(current.geoserver_auth_cfg_id),
                    }
                ]
        return profiles

    @staticmethod
    def profile_encrypted(profile) -> bool:
        """Whether a profile keeps its password in the auth database.

        A profile saved before the choice existed has no flag: it is
        encrypted when it holds an auth config, as every profile then did.
        """
        return bool(profile.get("encrypted", profile.get("auth_cfg_id")))

    @classmethod
    def save_profiles(cls, profiles: list) -> bool:
        """Store the profile list as it is, active one included."""
        return cls.set_value_from_key("profiles", json.dumps(profiles))

    @classmethod
    def active_profile_name(cls) -> str:
        """The name of the profile the dialog connects to, or ""."""
        name = cls.get_value_from_key("active_profile", "", str) or ""
        profiles = cls.get_profiles()
        if not name and profiles:
            # Migrated from a single connection: that one is what is active.
            current = cls.get_plg_settings()
            if profiles[0]["url"] == current.geoserver_url:
                name = profiles[0]["name"]
        return name if any(p["name"] == name for p in profiles) else ""

    @classmethod
    def activate_profile(cls, profile) -> None:
        """Make `profile` the connection everything reads; None clears it,
        which the dialog shows as "Not configured"."""
        # Not save_from_object(): it would store the debug mode the environment set.
        cls.set_value_from_key("geoserver_url", profile["url"] if profile else "")
        cls.set_value_from_key(
            "geoserver_auth_cfg_id", profile["auth_cfg_id"] if profile else ""
        )
        cls.set_value_from_key(
            "geoserver_verify_tls",
            bool(profile.get("verify_tls", True)) if profile else True,
        )
        for key in ("username", "password"):
            cls.set_value_from_key(
                f"geoserver_{key}", profile.get(key, "") if profile else ""
            )
        cls.set_value_from_key("active_profile", profile["name"] if profile else "")
