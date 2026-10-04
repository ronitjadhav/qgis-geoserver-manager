#! python3  # noqa: E265

"""
Workspace tab: load, create, edit, delete workspaces.

Used as a mixin for GeoServerMainDialog.
"""

import re

from qgis.core import Qgis
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtWidgets import QDialog

from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog
from geoserver_manager.gui.scope import GLOBAL
from geoserver_manager.toolbelt.payload import keyword_list, words
from geoserver_manager.toolbelt.rest import PartlySaved

# GeoServer spells the WMS abstract "abstrct" in its JSON, a typo old enough to
# be API. Keywords and the SRS list arrive wrapped as {"string": [...]}.
_ABSTRACT = "abstrct"

# The services a workspace can override besides WMS, which has a richer
# group of its own. Measured on 2.28.5: GET is a 404 without own settings, a
# PUT creates or merges them, DELETE falls back to the global ones.
OTHER_SERVICES = ("wfs", "wcs", "wmts")

# An XML NCName, as GeoServer's own workspace pages require (XMLNameValidator).
_XML_NAME = re.compile(r"[^\W\d][\w.\-]*")


# Every user-visible string in this file goes through translate() with this
# file's own class as the context. self.tr() cannot: pylupdate extracts it
# under WorkspaceTabMixin, but at runtime self.tr is QObject.tr with the context of the
# *instance's* class, GeoServerMainDialog. QDialog precedes the mixins in the
# MRO, so every lookup would miss. A wrapper function would not be extracted
# at all (pylupdate only understands a literal context), hence the repetition.
translate = QCoreApplication.translate


class WorkspaceTabMixin:
    """Mixin that adds workspace CRUD methods to the main dialog."""

    def _load_workspaces(self):
        """Arm the Workspaces tab, then fetch its rows in the background."""
        self._setup_add_button(
            translate("WorkspaceTabMixin", "Add a Workspace"),
            translate(
                "WorkspaceTabMixin",
                "Create a workspace to hold stores, layers and styles",
            ),
            self._add_workspace,
        )
        self._setup_delete_selected_button(self._delete_selected_workspaces)
        self._name_click_callback = self._show_workspace_info
        self._extra_click_callbacks = {}
        self._row_actions = [
            (
                "delete",
                translate("WorkspaceTabMixin", "Delete"),
                self._delete_workspace,
                translate(
                    "WorkspaceTabMixin",
                    "Delete: remove the workspace and everything in it, stores, "
                    "layers and styles (asks first).",
                ),
            ),
        ]
        self._setup_table(
            [
                translate("WorkspaceTabMixin", "Name"),
                translate("WorkspaceTabMixin", "Default"),
                self.actions_column_label(),
            ]
        )
        self._start_load(
            translate("WorkspaceTabMixin", "Failed to load workspaces"),
            self._fetch_workspace_rows,
        )

    def _fetch_workspace_rows(self, task=None):
        """(rows, failures) for the Workspaces table. Runs in a worker thread."""
        workspaces = self._fetch_list(self.gs.get_workspaces)
        # Shown in the list so the server's truth is visible at a glance:
        # GeoServer always has exactly one default and it cannot be unset.
        failures = []
        default = None
        # No workspace, no default: GeoServer answers default.json with a 404.
        if workspaces:
            try:
                default = self._default_workspace_name()
            except Exception as error:
                # Unknown, and said so: every row read "No" without a word.
                failures.append(
                    (translate("WorkspaceTabMixin", "the default workspace"), error)
                )
        rows = [
            # A boolean cell, like every other: Yes / No, translated.
            [name, None if default is None else self._yes_no(name == default)]
            for name in (self._name_of(ws) for ws in workspaces)
        ]
        return rows, failures

    def _workspace_fields(self, is_default=False, with_wms=False):
        """Return workspace form field definitions.

        :param is_default: the workspace being edited already is GeoServer's
            default; the checkbox is then shown ticked and locked, because the
            REST API has no "unset default", only "set another one".
        :param with_wms: append the WMS service group. Only for an existing
            workspace: its settings can only be PUT once it exists.
        """
        fields = [
            {
                "key": "name",
                "label": translate("WorkspaceTabMixin", "Name"),
                "type": "text",
                "required": True,
            },
            {
                "key": "uri",
                "label": translate("WorkspaceTabMixin", "Namespace URI"),
                "type": "text",
                "placeholder": translate(
                    "WorkspaceTabMixin", "http://{name}, if empty"
                ),
                "help": translate(
                    "WorkspaceTabMixin",
                    "What the workspace's features are qualified with in WFS and GML "
                    "(xmlns). Unique, unless the workspace is isolated.",
                ),
            },
            {
                "key": "isolated",
                "label": translate("WorkspaceTabMixin", "Isolated Workspace"),
                "type": "checkbox",
                "default": False,
                "help": translate(
                    "WorkspaceTabMixin",
                    "Its layers are served only under the workspace's own URLs "
                    "(…/{name}/wms), so another workspace may share its namespace URI",
                ),
            },
            {
                "key": "set_default",
                "label": translate("WorkspaceTabMixin", "Default Workspace"),
                "type": "checkbox",
                "default": False,
                "read_only": is_default,
                "help": (
                    translate(
                        "WorkspaceTabMixin",
                        "This is GeoServer's default workspace. There is always "
                        "exactly one and it cannot be unset. To change it, tick "
                        "Default on another workspace.",
                    )
                    if is_default
                    else translate(
                        "WorkspaceTabMixin",
                        "Make this GeoServer's default workspace (replaces the current one)",
                    )
                ),
            },
        ]
        if not with_wms:
            return fields
        return (
            fields
            + self._wms_fields()
            + [f for service in OTHER_SERVICES for f in self._service_fields(service)]
        )

    # -- WMS service settings --------------------------------------------------

    def _wms_fields(self):
        """The WMS group: one workspace's own WMS service settings."""
        group = translate("WorkspaceTabMixin", "WMS")
        return [
            {
                "key": "wms_own",
                "label": translate("WorkspaceTabMixin", "Own WMS settings"),
                "type": "checkbox",
                "group": group,
                "help": translate(
                    "WorkspaceTabMixin",
                    "Untick to fall back to GeoServer's global WMS settings. The "
                    "workspace's own are then removed.",
                ),
            },
            {
                "key": "wms_enabled",
                "label": translate("WorkspaceTabMixin", "Service enabled"),
                "type": "checkbox",
                "group": group,
                "help": translate(
                    "WorkspaceTabMixin", "Serve WMS for this workspace at all"
                ),
            },
            {
                "key": "wms_title",
                "label": translate("WorkspaceTabMixin", "Title"),
                "type": "text",
                "group": group,
            },
            {
                "key": "wms_abstract",
                "label": translate("WorkspaceTabMixin", "Abstract"),
                "type": "textarea",
                "group": group,
            },
            {
                "key": "wms_keywords",
                "label": translate("WorkspaceTabMixin", "Keywords"),
                "type": "list",
                "group": group,
            },
            {
                "key": "wms_srs",
                "label": translate("WorkspaceTabMixin", "SRS list"),
                "type": "list",
                "group": group,
                "help": translate(
                    "WorkspaceTabMixin",
                    "EPSG codes without the prefix (4326, 3857). Empty "
                    "advertises every SRS GeoServer knows.",
                ),
            },
            {
                "key": "wms_max_rendering_time",
                "label": translate("WorkspaceTabMixin", "Max rendering time (s)"),
                "type": "spinbox",
                "min": 0,
                "max": 86400,
                "group": group,
                "help": translate("WorkspaceTabMixin", "0 means no limit"),
            },
            {
                "key": "wms_max_rendering_errors",
                "label": translate("WorkspaceTabMixin", "Max rendering errors"),
                "type": "spinbox",
                "min": 0,
                "max": 1000000,
                "group": group,
                "help": translate("WorkspaceTabMixin", "0 means no limit"),
            },
            {
                "key": "wms_default_locale",
                "label": translate("WorkspaceTabMixin", "Default locale"),
                "type": "text",
                "group": group,
                "help": translate(
                    "WorkspaceTabMixin",
                    "Language of the internationalised title and abstract, e.g. en",
                ),
            },
        ]

    def _wms_settings_path(self, workspace_name):
        return self.gs.rest_service.rest_endpoints.workspace_wms_settings(
            workspace_name
        )

    def _wms_settings(self, workspace_name):
        """One workspace's WMS settings, or None when it has none of its own.

        TODO(#1): upstream: WmsSettings models neither the title, the
        abstract, the keywords nor the SRS list, so
        get_workspace_wms_settings() cannot show what this form is for. One
        raw GET answers both questions: a 404 means "none of its own".
        """
        response = self._raw_rest(
            "get", self._wms_settings_path(workspace_name), accept=(404,)
        )
        if response.status_code == 404:
            return None
        return response.json().get("wms") or {}

    @classmethod
    def _wms_form_values(cls, settings, overall=None):
        """Prefill for the WMS group; settings is None for "no own settings".

        Then the fields show the global WMS settings, like the other services:
        a new override began at no rendering limits and an empty title.
        """
        present = settings is not None
        settings = settings if present else (overall or {})
        return {
            "wms_own": present,
            "wms_enabled": bool(settings.get("enabled", True)),
            "wms_title": settings.get("title") or "",
            "wms_abstract": settings.get(_ABSTRACT) or "",
            "wms_keywords": keyword_list(settings.get("keywords")),
            "wms_srs": keyword_list(settings.get("srs")),
            "wms_max_rendering_time": int(settings.get("maxRenderingTime") or 0),
            "wms_max_rendering_errors": int(settings.get("maxRenderingErrors") or 0),
            "wms_default_locale": settings.get("defaultLocale") or "",
        }

    def _on_wms_own_changed(self, dlg, own):
        """The WMS fields only matter when the workspace keeps its own settings."""
        for field in self._wms_fields()[1:]:
            dlg.set_field_visible(field["key"], own)

    def _apply_wms_settings(self, workspace_name, values, existed, before=None):
        """Create, update or remove one workspace's own WMS settings.

        TODO(#1): see _wms_settings; put_workspace_wms_settings() cannot
        carry the title, abstract, keywords or SRS list, so this PUTs the
        settings path itself.
        """
        path = self._wms_settings_path(workspace_name)
        if not values["wms_own"]:
            if existed:
                self._raw_rest("delete", path)
            return
        self._put_service_settings(
            path,
            "wms",
            self._wms_payload(workspace_name, values),
            self._wms_payload(workspace_name, before) if existed and before else None,
        )

    @staticmethod
    def _wms_payload(workspace_name, values):
        """The WMS settings the form's values make. Pure."""
        return {
            "workspace": {"name": workspace_name},
            "name": "WMS",
            "enabled": values["wms_enabled"],
            "title": values["wms_title"],
            _ABSTRACT: values["wms_abstract"],
            "keywords": {"string": words(values["wms_keywords"])},
            "srs": {"string": words(values["wms_srs"])},
            "maxRenderingTime": values["wms_max_rendering_time"],
            "maxRenderingErrors": values["wms_max_rendering_errors"],
            # Empty, never null: GeoServer's LocaleConverter throws an NPE on
            # a null defaultLocale (500, "Cannot invoke String.indexOf … in is
            # null"), while "" is accepted and reads back as no locale.
            "defaultLocale": values["wms_default_locale"].strip(),
        }

    def _put_service_settings(self, path, service, settings, untouched=None):
        """PUT one service's settings of a workspace. Runs in a worker.

        A partial PUT merges: GeoServer keeps every field this form does not
        model (watermark, buffers, metadata links, …), verified on 2.28.5,
        and the same PUT creates the settings when the workspace has none
        (a POST there answers 405). `untouched` is what the form would send
        had nothing been edited, None for settings it creates: then only the
        fields that differ go, and nothing when none do, so another client's
        edits survive. Settings removed since the form opened are refused:
        a PUT would recreate them with the edited fields alone (measured).
        TODO(#1): the library reaches none of these paths (rows 25, 61).
        """
        if untouched is not None:
            edits = {
                key: value
                for key, value in settings.items()
                if untouched.get(key) != value
            }
            if not edits:
                return
            if self._raw_rest("get", path, accept=(404,)).status_code == 404:
                raise RuntimeError(
                    translate(
                        "WorkspaceTabMixin",
                        "its own {} settings were removed since the form opened. "
                        "Open the workspace again to set them.",
                    ).format(service.upper())
                )
            settings = {
                "workspace": settings["workspace"],
                "name": settings["name"],
                **edits,
            }
        self._raw_rest("put", path, json={service: settings})

    # -- WFS, WCS and WMTS service settings -------------------------------------

    def _service_fields(self, service):
        """One service's group: whether the workspace overrides it, and how."""
        group = service.upper()
        fields = [
            {
                "key": f"{service}_own",
                "label": translate("WorkspaceTabMixin", "Own {} settings").format(
                    group
                ),
                "type": "checkbox",
                "group": group,
                "help": translate(
                    "WorkspaceTabMixin",
                    "Untick to fall back to GeoServer's global settings. Ticked, "
                    "the form starts from the global ones.",
                ),
            },
            {
                "key": f"{service}_enabled",
                "label": translate("WorkspaceTabMixin", "Service enabled"),
                "type": "checkbox",
                "group": group,
            },
            {
                "key": f"{service}_title",
                "label": translate("WorkspaceTabMixin", "Title"),
                "type": "text",
                "group": group,
            },
            {
                "key": f"{service}_abstract",
                "label": translate("WorkspaceTabMixin", "Abstract"),
                "type": "textarea",
                "group": group,
            },
            {
                "key": f"{service}_keywords",
                "label": translate("WorkspaceTabMixin", "Keywords"),
                "type": "list",
                "group": group,
            },
        ]
        if service == "wfs":
            fields.append(
                {
                    "key": "wfs_max_features",
                    "label": translate("WorkspaceTabMixin", "Maximum features"),
                    "type": "spinbox",
                    "min": 0,
                    "max": 2147483647,
                    "group": group,
                    "help": translate("WorkspaceTabMixin", "Per GetFeature request"),
                }
            )
        return fields

    def _service_settings_path(self, service, workspace_name):
        """TODO(#1): no per-workspace WFS/WCS/WMTS settings in the library
        (row 61)."""
        base = self.gs.rest_service.rest_endpoints.base_url
        return f"{base}/services/{service}/workspaces/{workspace_name}/settings.json"

    def _service_settings(self, service, workspace_name):
        """(own settings or None, the global ones or {}). Runs in a worker.

        The global ones are what the form starts from when the workspace has
        none of its own, so they are read only then.
        """
        response = self._raw_rest(
            "get", self._service_settings_path(service, workspace_name), accept=(404,)
        )
        own = None if response.status_code == 404 else response.json().get(service)
        if own is not None:
            return own, {}
        return None, self._global_service_settings(service)

    def _global_service_settings(self, service):
        """One service's global settings. TODO(#1): out of the library's
        reach, as on the Server tab (row 60)."""
        base = self.gs.rest_service.rest_endpoints.base_url
        payload = self._raw_rest("get", f"{base}/services/{service}/settings.json")
        return payload.json().get(service) or {}

    @classmethod
    def _service_form_values(cls, service, own, overall):
        """Prefill for one service group. Pure.

        Without own settings the fields show the global ones, so ticking
        "Own settings" starts from what the workspace inherits: a fresh WFS
        override would otherwise begin at maxFeatures 0.
        """
        settings = own if own is not None else overall
        values = {
            f"{service}_own": own is not None,
            f"{service}_enabled": settings.get("enabled", True) is not False,
            f"{service}_title": settings.get("title") or "",
            f"{service}_abstract": settings.get(_ABSTRACT) or "",
            f"{service}_keywords": keyword_list(settings.get("keywords")),
        }
        if service == "wfs":
            values["wfs_max_features"] = int(settings.get("maxFeatures") or 0)
        return values

    def _on_service_own_changed(self, dlg, service, own):
        for field in self._service_fields(service)[1:]:
            dlg.set_field_visible(field["key"], own)

    def _apply_service_settings(
        self, service, workspace_name, values, existed, before=None
    ):
        """Create, update or remove one workspace's own settings for a service."""
        path = self._service_settings_path(service, workspace_name)
        if not values[f"{service}_own"]:
            if existed:
                self._raw_rest("delete", path)
            return
        self._put_service_settings(
            path,
            service,
            self._service_payload(service, workspace_name, values),
            (
                self._service_payload(service, workspace_name, before)
                if existed and before
                else None
            ),
        )

    @staticmethod
    def _service_payload(service, workspace_name, values):
        """One service's settings the form's values make. Pure."""
        settings = {
            "workspace": {"name": workspace_name},
            "name": service.upper(),
            "enabled": values[f"{service}_enabled"],
            "title": values[f"{service}_title"],
            _ABSTRACT: values[f"{service}_abstract"],
            "keywords": {"string": words(values[f"{service}_keywords"])},
        }
        if service == "wfs":
            settings["maxFeatures"] = values["wfs_max_features"]
        return settings

    # -- Namespace ---------------------------------------------------------------

    def _namespace_path(self, workspace_name):
        """TODO(#1): no namespace calls in the library (row 61)."""
        base = self.gs.rest_service.rest_endpoints.base_url
        return f"{base}/namespaces/{workspace_name}.json"

    def _namespace_uri(self, workspace_name):
        payload = self._raw_rest("get", self._namespace_path(workspace_name)).json()
        return (payload.get("namespace") or {}).get("uri") or ""

    def _put_namespace_uri(self, workspace_name, uri, isolated):
        """Set the URI. `isolated` goes with it: without it GeoServer stores
        false, and the workspace is no longer isolated (measured on 2.28.5)."""
        self._raw_rest(
            "put",
            self._namespace_path(workspace_name),
            json={"namespace": {"uri": uri, "isolated": isolated}},
        )

    def _default_workspace_name(self):
        """Name of GeoServer's default workspace. Raises when it cannot be read.

        TODO(#1): upstream as get_default_workspace(); no getter exists.
        """
        base = self.gs.rest_service.rest_endpoints.base_url
        payload = self._raw_rest("get", f"{base}/workspaces/default.json").json()
        return payload.get("workspace", {}).get("name")

    def _set_default_workspace(self, name):
        """Set the GeoServer default workspace.

        TODO(#1): upstream as set_default_workspace(). The library's
        create_workspace(set_default_workspace=True) only sets a client-side
        attribute and never calls the server. Workaround: PUT
        /rest/workspaces/default.json
        """
        path = f"{self.gs.rest_service.rest_endpoints.base_url}/workspaces/default.json"
        self._raw_rest("put", path, json={"workspace": {"name": name}})

    def _put_workspace(self, old_name, new_name, isolated):
        """Update a workspace in place, a rename when the names differ.

        TODO(#1): upstream as update_workspace(name, new_name=..., isolated=...).
        The library has no update and no rename, and create_workspace() on an
        existing name costs a POST that answers 409 before it PUTs. Workaround:
        one PUT to /rest/workspaces/{old_name}.
        """
        from geoservercloud.models.workspace import Workspace

        path = self.gs.rest_service.rest_endpoints.workspace(old_name)
        self._raw_rest("put", path, json=Workspace(new_name, isolated).put_payload())

    def _check_new_workspace(self, values):
        """Refuse a name a URL would eat, one GeoServer's own form refuses,
        or one taken. Reads only: the Add form runs it before it closes, the
        save again."""
        name = values["name"]
        self._require_safe_name(name)
        # A space half-created one (500), and rows carry GLOBAL as the global scope.
        if name == GLOBAL or not _XML_NAME.fullmatch(name):
            raise ValueError(
                translate(
                    "WorkspaceTabMixin",
                    "'{}' cannot be a workspace name: it must start with a letter "
                    "or '_', then hold only letters, digits, '_', '-' and '.', as "
                    "GeoServer's own form requires.",
                ).format(name)
            )
        # create_workspace upserts, so an existing name would silently
        # reconfigure the live workspace and report it as created
        if self._resource_exists(self.gs.get_workspace, name):
            raise ValueError(
                translate("WorkspaceTabMixin", "Workspace '{}' already exists.").format(
                    name
                )
            )

    def _check_workspace_rename(self, old_name, values):
        """A new name is checked as a new workspace's is: GeoServer refuses a
        rename onto a taken name with a 500 and changes nothing. Reads only:
        the edit form runs it before it closes, the save again."""
        if values["name"] != old_name:
            self._check_new_workspace(values)

    def _save_workspace(self, values, old_name=None, before=None):
        """Create (old_name None) or update a workspace from form values.

        Runs in a worker (_wait_for), so it shows nothing: it returns the
        warning to show once it is back, or None. `before` is what the edit
        form gave back untouched: the workspace is then PUT only when its
        name or its isolation changed.
        """
        name = values["name"]
        if old_name is None:
            self._check_new_workspace(values)
            self._check(self.gs.create_workspace(name, isolated=values["isolated"]))
            # A workspace administrator may create one it is not shown (measured).
            if not self._resource_exists(self.gs.get_workspace, name):
                raise PartlySaved(
                    translate(
                        "WorkspaceTabMixin",
                        "Workspace '{}' was created, but this account cannot see or "
                        "manage it: GeoServer lists only the workspaces an account "
                        "administers. Ask an administrator to grant it, or to "
                        "delete it.",
                    ).format(name)
                )
            if (values.get("uri") or "").strip():
                try:
                    self._put_namespace_uri(
                        name, values["uri"].strip(), values["isolated"]
                    )
                except Exception as error:
                    raise PartlySaved(
                        translate(
                            "WorkspaceTabMixin",
                            "Workspace '{}' created, but its namespace URI could "
                            "not be set: {}",
                        ).format(name, self._error_text(error))
                    ) from error
        else:
            # One PUT, rename or not (create_workspace would POST, get a 409,
            # then PUT).
            self._check_workspace_rename(old_name, values)
            if (
                before is None
                or name != old_name
                or values["isolated"] != before.get("isolated")
            ):
                self._put_workspace(old_name, name, values["isolated"])
        if values["set_default"]:
            # Separate from the save: a 403 here must not report the (already
            # successful) create or rename as failed, nor skip the reload.
            try:
                self._set_default_workspace(name)
            except Exception as e:
                reason = self._error_text(e)
                self.log(
                    f"Set default workspace error: {reason}",
                    log_level=Qgis.MessageLevel.Warning,
                )
                return translate(
                    "WorkspaceTabMixin",
                    "Workspace '{}' saved, but it could not be made the default: {}",
                ).format(name, reason)
        return None

    def _add_workspace(self):
        """Open a form dialog to create a new workspace."""
        dlg = ResourceFormDialog(
            title=translate("WorkspaceTabMixin", "Add a Workspace"),
            description=translate(
                "WorkspaceTabMixin",
                "A workspace groups stores, layers and styles under one name, "
                "which also prefixes its layers (workspace:layer).",
            ),
            fields=self._workspace_fields(),
            parent=self,
            ok_label=translate("WorkspaceTabMixin", "Create"),
            validate=self._form_check(self._check_new_workspace),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        values = dlg.get_values()
        warning = []
        if self._run_action(
            lambda: warning.append(
                self._wait_for_save(lambda: self._save_workspace(values))
            ),
            translate("WorkspaceTabMixin", "Failed to create workspace '{}'").format(
                values["name"]
            ),
        ):
            self.show_success_message(
                translate("WorkspaceTabMixin", "Workspace '{}' created.").format(
                    values["name"]
                )
            )
            if warning[0]:
                self.show_warning_message(warning[0])
            self._load_workspaces()

    def _workspace_details(self, name):
        """What the edit form shows, read in one worker call: the workspace,
        its settings per service (the global ones only where it has none of
        its own), whether it is the default, and its namespace URI."""
        wms = self._wms_settings(name)
        try:
            default = self._default_workspace_name()
        except Exception:  # best-effort prefill: unknown reads as unticked
            default = None
        return (
            self._check(self.gs.get_workspace(name)),
            wms,
            default,
            self._namespace_uri(name),
            {
                service: self._service_settings(service, name)
                for service in OTHER_SERVICES
            },
            None if wms is not None else self._global_service_settings("wms"),
        )

    def _show_workspace_info(self, row_data):
        """Open a form dialog to view/edit an existing workspace and its WMS."""
        old_name = row_data[0]
        fetched = self._fetch(
            lambda: self._workspace_details(old_name),
            translate("WorkspaceTabMixin", "Failed to load workspace details"),
        )
        if fetched is None:
            return
        detail, wms_settings, default_name, uri, services, overall_wms = fetched

        is_default = default_name == old_name
        values = {
            "name": old_name,
            "isolated": (
                bool(detail.get("isolated", False))
                if isinstance(detail, dict)
                else False
            ),
            "set_default": is_default,
            "uri": uri,
        }
        values.update(self._wms_form_values(wms_settings, overall_wms))
        for service, (own, overall) in services.items():
            values.update(self._service_form_values(service, own, overall))
        dlg = ResourceFormDialog(
            title=translate("WorkspaceTabMixin", "Workspace '{}'").format(old_name),
            description=translate(
                "WorkspaceTabMixin",
                "Rename it, change its namespace URI, toggle isolation, make it "
                "the default, or give it its own WMS, WFS, WCS or WMTS settings. "
                "Save applies all of it at once.",
            ),
            fields=self._workspace_fields(is_default=is_default, with_wms=True),
            values=values,
            parent=self,
            validate=self._form_check(
                lambda after: self._check_workspace_rename(old_name, after)
            ),
        )
        own = dlg.get_widget("wms_own")
        own.toggled.connect(lambda checked: self._on_wms_own_changed(dlg, checked))
        self._on_wms_own_changed(dlg, own.isChecked())
        for service in OTHER_SERVICES:
            box = dlg.get_widget(f"{service}_own")
            box.toggled.connect(
                lambda checked, service=service: self._on_service_own_changed(
                    dlg, service, checked
                )
            )
            self._on_service_own_changed(dlg, service, box.isChecked())
        before = dlg.get_values()  # as the form gives it back untouched
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        values = dlg.get_values()
        # The default's own box is locked and ticked: not a request to set it.
        values["set_default"] = values["set_default"] and not is_default
        had_wms = wms_settings is not None
        had = {service: own is not None for service, (own, _) in services.items()}
        warning = []
        if self._run_action(
            # Up to ten requests: in a worker, so a hung server cannot freeze QGIS.
            lambda: warning.append(
                self._wait_for_save(
                    lambda: self._save_workspace_and_wms(
                        values, old_name, had_wms, had, uri, before
                    )
                )
            ),
            translate("WorkspaceTabMixin", "Failed to update workspace '{}'").format(
                values["name"]
            ),
        ):
            self.show_success_message(
                translate("WorkspaceTabMixin", "Workspace '{}' saved.").format(
                    values["name"]
                )
            )
            if warning[0]:
                self.show_warning_message(warning[0])
            # Reachable from the datastore tab, so reload whatever is on screen
            self._reload_current_tab()

    def _save_workspace_and_wms(
        self, values, old_name, had_wms, had_services=None, old_uri=None, before=None
    ):
        """Save the workspace, then its namespace and services, in that order.

        A rename has to land first: the settings live under the workspace's
        (new) name, and a rename keeps the URI (both measured on 2.28.5).
        `before` is what the form gave back untouched: with it, only what
        the user changed is sent.
        """
        warning = self._save_workspace(values, old_name=old_name, before=before)
        name = values["name"]
        uri = (values.get("uri") or "").strip()
        try:
            if old_uri is not None and uri != old_uri:
                # Emptied: back to GeoServer's own default, not left as it was.
                self._put_namespace_uri(
                    name, uri or f"http://{name}", values["isolated"]
                )
            self._apply_wms_settings(name, values, had_wms, before)
            for service in OTHER_SERVICES:
                if f"{service}_own" in values:
                    self._apply_service_settings(
                        service,
                        name,
                        values,
                        (had_services or {}).get(service, False),
                        before,
                    )
        except Exception as error:
            message = translate(
                "WorkspaceTabMixin",
                "Workspace '{}' saved, but its namespace or service settings "
                "were not: {}",
            ).format(name, self._error_text(error))
            # A failed "make default" is only a warning, and would be lost here.
            raise PartlySaved(f"{message} {warning}" if warning else message) from error
        return warning

    def _delete_workspace(self, row_data):
        """Delete a single workspace after confirmation."""
        self._delete_selected_workspaces([row_data])

    def _delete_selected_workspaces(self, selected_rows):
        """Delete one or more workspaces after confirmation."""
        self._delete_many(
            [
                (row[0], lambda n=row[0]: self._check(self.gs.delete_workspace(n)))
                for row in selected_rows
            ],
            self._load_workspaces,
            ask=self._one_or_many(
                translate(
                    "WorkspaceTabMixin",
                    "Are you sure you want to delete workspace '{}'?",
                ),
                lambda n: translate(
                    "WorkspaceTabMixin",
                    "Are you sure you want to delete %n workspace(s)?",
                    None,
                    n,
                ),
            ),
            done=self._one_or_many(
                translate("WorkspaceTabMixin", "Workspace '{}' deleted."),
                lambda n: translate(
                    "WorkspaceTabMixin", "%n workspace(s) deleted.", None, n
                ),
            ),
            # delete_workspace() sends recurse=true
            cascade=translate(
                "WorkspaceTabMixin",
                "Everything it contains is deleted too: datastores, coverage "
                "stores, cascaded stores, layers, layer groups and styles.",
            ),
        )
