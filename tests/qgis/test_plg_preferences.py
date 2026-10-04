#! python3  # noqa E265

"""
Usage from the repo root folder:

.. code-block:: bash

    # for whole tests
    python -m unittest tests.qgis.test_plg_preferences
    # for specific test
    python -m unittest tests.qgis.test_plg_preferences.TestPlgPreferences.test_plg_preferences_structure
"""

# standard library
import os
from unittest.mock import patch

from qgis.core import QgsApplication, QgsAuthMethodConfig
from qgis.testing import start_app, unittest

# project
from geoserver_manager.__about__ import __version__
from geoserver_manager.toolbelt.preferences import (
    PREFIX_ENV_VARIABLE,
    PlgOptionsManager,
    PlgSettingsStructure,
)

start_app()


def auth_manager():
    """This test profile's QgsAuthManager, unlocked: nothing prompts."""
    manager = QgsApplication.authManager()
    if not manager.masterPasswordIsSet():
        manager.setMasterPassword("test-master-password", True)
    return manager


# ############################################################################
# ########## Classes #############
# ################################


class TestPlgPreferences(unittest.TestCase):
    def test_the_defaults(self):
        settings = PlgSettingsStructure()
        self.assertIs(settings.debug_mode, False)
        self.assertEqual(settings.version, __version__)
        # TLS verification defaults to on: nothing is silently insecure
        self.assertIs(settings.geoserver_verify_tls, True)

    def test_bool_env_variable(self):
        """Test settings with environment value."""
        manager = PlgOptionsManager()
        with patch.dict(
            os.environ, {f"{PREFIX_ENV_VARIABLE}DEBUG_MODE": "true"}, clear=True
        ):
            settings = manager.get_plg_settings()
            self.assertEqual(settings.debug_mode, True)

        with patch.dict(
            os.environ, {f"{PREFIX_ENV_VARIABLE}DEBUG_MODE": "false"}, clear=True
        ):
            settings = manager.get_plg_settings()
            self.assertEqual(settings.debug_mode, False)

        with patch.dict(
            os.environ, {f"{PREFIX_ENV_VARIABLE}DEBUG_MODE": "on"}, clear=True
        ):
            settings = manager.get_plg_settings()
            self.assertEqual(settings.debug_mode, True)

        with patch.dict(
            os.environ, {f"{PREFIX_ENV_VARIABLE}DEBUG_MODE": "off"}, clear=True
        ):
            settings = manager.get_plg_settings()
            self.assertEqual(settings.debug_mode, False)

        with patch.dict(
            os.environ, {f"{PREFIX_ENV_VARIABLE}DEBUG_MODE": "1"}, clear=True
        ):
            settings = manager.get_plg_settings()
            self.assertEqual(settings.debug_mode, True)

        with patch.dict(
            os.environ, {f"{PREFIX_ENV_VARIABLE}DEBUG_MODE": "0"}, clear=True
        ):
            settings = manager.get_plg_settings()
            self.assertEqual(settings.debug_mode, False)

        with patch.dict(
            os.environ,
            {f"{PREFIX_ENV_VARIABLE}DEBUG_MODE": "invalid_value"},
            clear=True,
        ):
            settings = manager.get_plg_settings()
            self.assertEqual(settings.debug_mode, False)


class TestServerProfiles(unittest.TestCase):
    """Saved connections, stored in this test profile's QgsSettings."""

    KEYS = (
        "debug_mode",
        "geoserver_url",
        "geoserver_auth_cfg_id",
        "geoserver_verify_tls",
        "profiles",
        "active_profile",
    )

    def setUp(self):
        from qgis.core import QgsSettings

        from geoserver_manager.__about__ import __title__

        self.settings = QgsSettings()
        self.group = __title__
        self.kept = {k: self.settings.value(f"{__title__}/{k}") for k in self.KEYS}
        for key in self.KEYS:
            self.settings.remove(f"{__title__}/{key}")
        self.addCleanup(self.restore)

    def restore(self):
        for key, value in self.kept.items():
            self.settings.remove(f"{self.group}/{key}")
            if value is not None:
                self.settings.setValue(f"{self.group}/{key}", value)

    def test_nothing_saved_means_no_profile(self):
        self.assertEqual(PlgOptionsManager.get_profiles(), [])
        self.assertEqual(PlgOptionsManager.active_profile_name(), "")

    def test_a_connection_saved_before_profiles_becomes_the_first(self):
        old = PlgSettingsStructure(
            geoserver_url="https://gs.example.org/geoserver",
            geoserver_auth_cfg_id="abc1234",
            geoserver_verify_tls=False,
        )
        PlgOptionsManager.save_from_object(old)
        self.assertEqual(
            PlgOptionsManager.get_profiles(),
            [
                {
                    "name": "gs.example.org",
                    "url": "https://gs.example.org/geoserver",
                    "auth_cfg_id": "abc1234",
                    "verify_tls": False,
                    "encrypted": True,
                }
            ],
        )
        self.assertEqual(PlgOptionsManager.active_profile_name(), "gs.example.org")

    def test_activating_a_profile_is_what_everything_else_reads(self):
        dev = {
            "name": "dev",
            "url": "http://localhost:8080/geoserver",
            "auth_cfg_id": "dev0001",
            "verify_tls": True,
        }
        prod = {
            "name": "prod",
            "url": "https://prod.example.org/geoserver",
            "auth_cfg_id": "prd0001",
            "verify_tls": False,
        }
        PlgOptionsManager.save_profiles([dev, prod])
        PlgOptionsManager.activate_profile(prod)

        current = PlgOptionsManager.get_plg_settings()
        self.assertEqual(current.geoserver_url, prod["url"])
        self.assertEqual(current.geoserver_auth_cfg_id, "prd0001")
        self.assertIs(current.geoserver_verify_tls, False)
        self.assertEqual(PlgOptionsManager.active_profile_name(), "prod")
        self.assertEqual(
            [p["name"] for p in PlgOptionsManager.get_profiles()], ["dev", "prod"]
        )

    def test_no_active_profile_is_not_configured(self):
        PlgOptionsManager.activate_profile(None)
        current = PlgOptionsManager.get_plg_settings()
        self.assertFalse(current.has_credentials())

    def test_activating_a_profile_copies_its_plain_credentials(self):
        dev = {
            "name": "dev",
            "url": "http://localhost:8080/geoserver",
            "auth_cfg_id": "",
            "verify_tls": True,
            "encrypted": False,
            "username": "admin",
            "password": "geoserver",
        }
        PlgOptionsManager.save_profiles([dev])
        PlgOptionsManager.activate_profile(dev)
        current = PlgOptionsManager.get_plg_settings()
        self.assertTrue(current.has_credentials())
        self.assertEqual(current.get_credentials(), ("admin", "geoserver"))
        PlgOptionsManager.activate_profile(None)
        self.assertEqual(
            PlgOptionsManager.get_plg_settings().get_credentials(), ("", "")
        )


class TestWhereThePasswordIsKept(unittest.TestCase):
    """Plain text in the settings is the default; the auth database is a choice."""

    def test_plain_credentials_are_read_without_the_auth_database(self):
        plain = PlgSettingsStructure(
            geoserver_url="https://gs.example.org/geoserver",
            geoserver_username="admin",
            geoserver_password="s3cret",
        )
        self.assertTrue(plain.has_credentials())
        with patch.object(
            QgsApplication, "authManager", side_effect=AssertionError("auth db read")
        ):
            self.assertEqual(plain.get_credentials(), ("admin", "s3cret"))

    def test_a_user_name_alone_is_not_a_credential(self):
        half = PlgSettingsStructure(
            geoserver_url="https://gs.example.org/geoserver", geoserver_username="admin"
        )
        self.assertFalse(half.has_credentials())

    def test_a_profile_saved_before_the_choice_is_encrypted_when_it_has_a_config(self):
        self.assertTrue(PlgOptionsManager.profile_encrypted({"auth_cfg_id": "id1"}))
        self.assertFalse(PlgOptionsManager.profile_encrypted({"auth_cfg_id": ""}))
        self.assertFalse(
            PlgOptionsManager.profile_encrypted(
                {"auth_cfg_id": "id1", "encrypted": False}
            )
        )

    def test_a_damaged_list_reads_as_empty_instead_of_raising(self):
        PlgOptionsManager.set_value_from_key("profiles", "[{not json")
        self.assertEqual(PlgOptionsManager.get_profiles(), [])

    PROD = {
        "name": "prod",
        "url": "https://prod.example.org/geoserver",
        "auth_cfg_id": "prd0001",
        "verify_tls": True,
    }

    def test_the_environment_cannot_pick_the_server(self):
        # It outlived a profile switch: the environment's server with the
        # new profile's credentials.
        PlgOptionsManager.activate_profile(self.PROD)
        environment = {
            f"{PREFIX_ENV_VARIABLE}GEOSERVER_URL": "http://env.example.org",
            f"{PREFIX_ENV_VARIABLE}GEOSERVER_AUTH_CFG_ID": "env0001",
        }
        with patch.dict(os.environ, environment):
            current = PlgOptionsManager.get_plg_settings()
        self.assertEqual(current.geoserver_url, self.PROD["url"])
        self.assertEqual(current.geoserver_auth_cfg_id, "prd0001")

    def test_a_profile_switch_does_not_store_the_environments_debug_mode(self):
        with patch.dict(os.environ, {f"{PREFIX_ENV_VARIABLE}DEBUG_MODE": "true"}):
            PlgOptionsManager.activate_profile(self.PROD)
        with patch.dict(os.environ):
            os.environ.pop(f"{PREFIX_ENV_VARIABLE}DEBUG_MODE", None)
            self.assertIs(PlgOptionsManager.get_plg_settings().debug_mode, False)


class TestAuthConfigs(unittest.TestCase):
    """Only a config the plugin made is written to or removed: another one
    may be what other QGIS connections log in with."""

    def setUp(self):
        self.manager = auth_manager()
        self.made = []
        self.addCleanup(self.remove_made)

    def remove_made(self):
        for auth_cfg_id in set(self.made) & set(self.manager.configIds()):
            self.manager.removeAuthenticationConfig(auth_cfg_id)

    def store(self, name, method, **config):
        auth_cfg = QgsAuthMethodConfig()
        auth_cfg.setName(name)
        auth_cfg.setMethod(method)
        for key, value in config.items():
            auth_cfg.setConfig(key, value)
        self.manager.storeAuthenticationConfig(auth_cfg)
        self.made.append(auth_cfg.id())
        return auth_cfg.id()

    def config_map(self, auth_cfg_id):
        auth_cfg = QgsAuthMethodConfig()
        self.manager.loadAuthenticationConfig(auth_cfg_id, auth_cfg, True)
        return auth_cfg.configMap()

    def save(self, auth_cfg_id):
        saved = PlgSettingsStructure(geoserver_auth_cfg_id=auth_cfg_id)
        new_id = saved.save_credentials("admin", "geoserver")
        self.made.append(new_id)
        return new_id

    def test_the_plugins_own_config_is_updated_in_place(self):
        own = self.save("")
        self.assertTrue(own)
        self.assertEqual(self.save(own), own)
        self.assertEqual(
            PlgSettingsStructure(geoserver_auth_cfg_id=own).get_credentials(),
            ("admin", "geoserver"),
        )

    def test_a_config_of_another_program_gets_a_new_one_beside_it(self):
        for other in (
            self.store("Header", "APIHeader", **{"X-Key": "abc"}),
            self.store("Intranet", "Basic", username="me", password="mine"),
        ):
            before = self.config_map(other)
            with self.subTest(other=before):
                new_id = self.save(other)
                self.assertNotIn(new_id, ("", other))
                self.assertEqual(self.config_map(other), before)
                self.assertEqual(
                    PlgSettingsStructure(geoserver_auth_cfg_id=new_id).auth_method(),
                    "Basic",
                )

    def test_a_config_that_is_gone_gets_a_new_one(self):
        # "Could not store the credentials... master password" on every save.
        new_id = self.save("gone123")
        self.assertNotIn(new_id, ("", "gone123"))

    def test_a_refused_store_hands_back_no_id(self):
        # PyQGIS returns (ok, config), and (False, config) is true.
        def refuse(auth_cfg, overwrite=False):
            auth_cfg.setId("never01")
            return False, auth_cfg

        with patch.object(self.manager, "storeAuthenticationConfig", refuse):
            self.assertEqual(self.save(""), "")

    def test_only_the_plugins_own_config_is_removed(self):
        own = self.save("")
        other = self.store("Header", "APIHeader", **{"X-Key": "abc"})
        for auth_cfg_id in (own, other):
            removed = PlgSettingsStructure(geoserver_auth_cfg_id=auth_cfg_id)
            removed.remove_credentials()
            self.assertEqual(removed.geoserver_auth_cfg_id, "")
        self.assertNotIn(own, self.manager.configIds())
        self.assertIn(other, self.manager.configIds())


# ############################################################################
# ####### Stand-alone run ########
# ################################
if __name__ == "__main__":
    unittest.main()
