#! python3  # noqa: E265

"""
Datastore tab: load, create, edit, delete datastores.

Used as a mixin for GeoServerMainDialog.
"""

from urllib.parse import quote

from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtWidgets import QDialog

from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog
from geoserver_manager.gui.scope import PENDING
from geoserver_manager.toolbelt.rest import PartlySaved

# Datastore types this form has dedicated fields for. Every other type
# opens in the generic editor: a table of its parameters and their values.
_SHAPEFILE = "Shapefile"
_SHAPEFILE_DIRECTORY = "Directory of spatial files (shapefiles)"
_GEOPACKAGE = "GeoPackage"
_WFS = "Web Feature Server (NG)"
# The fields each of those types shows (_on_type_changed). Adding a type is
# one entry here plus its save path.
_TYPE_FIELDS = {
    "PostGIS": ("pg_host", "pg_port", "pg_db", "pg_user", "pg_password", "pg_schema"),
    "PostGIS (JNDI)": ("jndi_reference", "pg_schema"),
    "PMTiles": ("pmtiles_url",),
    _SHAPEFILE: ("file_url", "charset", "spatial_index"),
    _SHAPEFILE_DIRECTORY: ("file_url", "charset"),  # no spatial index for a folder
    _GEOPACKAGE: ("gpkg_database", "gpkg_read_only", "gpkg_expose_pk"),
    _WFS: (
        "wfs_url",
        "wfs_user",
        "wfs_password",
        "wfs_timeout",
        "wfs_max_features",
        "wfs_lenient",
    ),
}
# Any other GeoServer datastore type (Properties, Oracle, SQL Server, CSV...):
# its name typed as GeoServer knows it, and its parameters in a key/value table.
_OTHER = "Other..."
_TYPE_FIELDS[_OTHER] = ("custom_type", "raw_params")
_SUPPORTED_TYPES = list(_TYPE_FIELDS)

# GeoServer prefixes every parameter of a cascaded WFS store with its factory.
_WFS_KEY = "WFSDataStoreFactory:"
_WFS_URL, _WFS_USER, _WFS_PASSWORD = (
    _WFS_KEY + "GET_CAPABILITIES_URL",
    _WFS_KEY + "USERNAME",
    _WFS_KEY + "PASSWORD",
)

# The connection parameters each typed form owns. The edit form lists every
# other one under "Other parameters", so none of them needs the web UI; the
# namespace stays hidden, since GeoServer sets it from the workspace.
_OWNED_PARAMS = {
    "PostGIS": ("host", "port", "database", "user", "passwd", "schema", "dbtype"),
    "PostGIS (JNDI)": ("jndiReferenceName", "schema", "dbtype"),
    "PMTiles": ("pmtiles",),
    "Shapefile": ("url", "charset", "create spatial index"),
    "Directory of spatial files (shapefiles)": ("url", "charset"),
    "GeoPackage": ("database", "dbtype", "read_only", "Expose primary keys"),
    "Web Feature Server (NG)": (
        _WFS_URL,
        _WFS_USER,
        _WFS_PASSWORD,
        _WFS_KEY + "TIMEOUT",
        _WFS_KEY + "MAXFEATURES",
        _WFS_KEY + "LENIENT",
    ),
}
_HIDDEN_PARAMS = ("namespace",)

# GeoServer picks the GeoPackage factory by this connection parameter, so it
# travels with every GeoPackage store the form saves.
_GEOPACKAGE_DBTYPE = "geopkg"

# Stands in for password-like values in the generic editor; never sent back as-is
_MASKED = "••••"

# Every field that belongs to one of those types, in form order.
_TYPE_SPECIFIC_FIELDS = tuple(
    dict.fromkeys(key for keys in _TYPE_FIELDS.values() for key in keys)
)


# Every user-visible string in this file goes through translate() with this
# file's own class as the context. self.tr() cannot: pylupdate extracts it
# under DatastoreTabMixin, but at runtime self.tr is QObject.tr with the context of the
# *instance's* class, GeoServerMainDialog. QDialog precedes the mixins in the
# MRO, so every lookup would miss. A wrapper function would not be extracted
# at all (pylupdate only understands a literal context), hence the repetition.
translate = QCoreApplication.translate


# Words that make a parameter secret, matched on its letters only: cloud
# range readers name theirs "…secret-access-key" or "…account.key".
_SECRET_WORDS = (
    "passwd",
    "password",
    "secret",
    "token",
    "accesskey",
    "accountkey",
    "apikey",
    "credential",
)


def _shown(key, value):
    """A parameter as the form shows it: secrets masked, an empty one blank."""
    if _is_masked(key, value):
        return _MASKED
    return "" if value is None else str(value)


def _is_masked(key, value):
    """A secret's value, unless it is an on/off flag: PMTiles'
    "…use-default-credentials-provider" is true or false, not a secret."""
    return _is_secret(key) and str(value).strip().lower() not in ("true", "false")


def _is_secret(key):
    """A parameter to mask: `passwd`, `WFSDataStoreFactory:PASSWORD`, an access
    or account key, a secret or a token; not a bare `key`, which would hide
    "Expose primary keys"."""
    letters = "".join(c for c in key.lower() if c.isalpha())
    return any(word in letters for word in _SECRET_WORDS)


def _as_int(value, default):
    """An integer parameter as GeoServer returned it, or the default."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class DatastoreTabMixin:
    """Mixin that adds datastore CRUD methods to the main dialog."""

    def _load_datastores(self):
        """Arm the Datastores tab, then fetch its rows in the background."""
        self._setup_add_button(
            translate("DatastoreTabMixin", "Add a Datastore"),
            translate(
                "DatastoreTabMixin", "Connect a database or a file on the server"
            ),
            self._add_datastore,
        )
        self._setup_delete_selected_button(self._delete_selected_datastores)
        self._name_click_callback = self._show_datastore_info
        self._extra_click_callbacks = {
            translate("DatastoreTabMixin", "Workspace"): self._open_workspace_from_row
        }
        self._row_actions = [
            (
                "update-from-source",
                translate("DatastoreTabMixin", "Reset"),
                self._reset_datastore,
                translate(
                    "DatastoreTabMixin",
                    "Reset: GeoServer re-reads the store, after its tables or "
                    "files changed.",
                ),
            ),
            (
                "delete",
                translate("DatastoreTabMixin", "Delete"),
                self._delete_datastore,
                translate(
                    "DatastoreTabMixin",
                    "Delete: remove the store, its feature types and their layers "
                    "(asks first). The data itself stays.",
                ),
            ),
        ]
        self._setup_table(
            [
                translate("DatastoreTabMixin", "Name"),
                translate("DatastoreTabMixin", "Workspace"),
                translate("DatastoreTabMixin", "Type"),
                translate("DatastoreTabMixin", "Enabled"),
                self.actions_column_label(),
            ]
        )
        self._row_detail = lambda row: self._datastore_summary(row[1], row[0])
        self._detail_columns = (2, 3)
        self._start_load(
            translate("DatastoreTabMixin", "Failed to load datastores"),
            self._fetch_datastore_rows,
        )

    def _fetch_datastore_rows(self, task=None):
        """(rows, failures) for the Datastores table. Runs in a worker thread."""
        ws_names = self._get_workspace_names()
        listed = self._fan_out(self._datastore_names, ws_names, task)
        pairs = [
            (ws_name, ds_name)
            for ws_name, (ds_names, error) in zip(ws_names, listed)
            if error is None
            for ds_name in ds_names
        ]
        # Type and Enabled follow for the page shown: one GET per store.
        rows = [[ds_name, ws_name, PENDING, PENDING] for ws_name, ds_name in pairs]
        failures = [(ws, err) for ws, (_names, err) in zip(ws_names, listed) if err]
        return rows, failures

    def _datastore_names(self, workspace_name):
        """Return the datastore names of one workspace. Raises on HTTP errors."""
        return [
            self._name_of(ds)
            for ds in self._fetch_list(self.gs.get_datastores, workspace_name)
        ]

    def _datastore_summary(self, workspace_name, datastore_name):
        """(type, enabled) for the list view. Raises on HTTP errors."""
        detail = self._check(self._get_datastore(workspace_name, datastore_name))
        if not isinstance(detail, dict):
            return ("-", "-")
        return (detail.get("type") or "-", self._yes_no(detail.get("enabled", True)))

    def _get_datastore(self, workspace_name, datastore_name):
        """gs.get_datastore(), for a store without a type too.

        TODO(#1): DataStore.from_get_response_payload() reads "type" without
        a default, so get_datastore() raises KeyError('type') for a store
        GeoServer writes without one: 4 of the 5 demo stores on 2.27, or one
        POSTed without it (measured). Such a store works, so it is read raw
        and given the type None.
        """
        try:
            return self.gs.get_datastore(workspace_name, datastore_name)
        except KeyError:
            from geoservercloud.models.datastore import DataStore

            response = self._raw_rest(
                "get", self._datastore_path(workspace_name, datastore_name)
            )
            content = response.json()
            content["dataStore"].setdefault("type", None)
            store = DataStore.from_get_response_payload(content)
            return store.asdict(), response.status_code

    def _datastore_fields(self, workspace_names, edit_mode=False, typed=True):
        """Return datastore form field definitions with type-specific params.

        :param workspace_names: list of workspace names for the combo box.
        :param edit_mode: editing an existing datastore. The workspace is
            fixed, and a password left blank keeps the stored one.
        :param typed: the store's type has a form of its own. The generic
            editor shows every parameter already, so it gets no Advanced tab.
        """
        # One page: the fields of the picked type show right under Type. On a
        # tab of their own, picking PostGIS meant going to look for them.
        fields = [
            {
                "key": "name",
                "label": translate("DatastoreTabMixin", "Name"),
                "type": "text",
                "required": True,
                # A rename is one PUT on the old path (invariant 6): measured to
                # keep the store's layers, groups and tile cache.
                "help": (
                    translate(
                        "DatastoreTabMixin",
                        "Renaming keeps its layers; only the store's own name changes.",
                    )
                    if edit_mode
                    else None
                ),
            },
            {
                "key": "workspace",
                "label": translate("DatastoreTabMixin", "Workspace"),
                "type": "combo",
                "options": workspace_names,
                "required": True,
                "read_only": edit_mode,
                "help": translate(
                    "DatastoreTabMixin", "The workspace this datastore belongs to"
                ),
            },
            {
                "key": "type",
                "label": translate("DatastoreTabMixin", "Type"),
                "type": "combo",
                # GeoServer's own type names, and "Other..." in the user's
                # language: it was both the label and the key.
                "options": [
                    (
                        (
                            translate("DatastoreTabMixin", "Other...")
                            if kind == _OTHER
                            else kind
                        ),
                        kind,
                    )
                    for kind in _SUPPORTED_TYPES
                ],
                "required": True,
            },
            {
                "key": "description",
                "label": translate("DatastoreTabMixin", "Description"),
                "type": "text",
                "placeholder": translate("DatastoreTabMixin", "Optional description"),
            },
            # --- PostGIS fields ---
            {
                "key": "pg_host",
                "label": translate("DatastoreTabMixin", "Host"),
                "type": "text",
                "required": True,
                "placeholder": "localhost",
            },
            {
                "key": "pg_port",
                "label": translate("DatastoreTabMixin", "Port"),
                "type": "spinbox",
                "default": 5432,
                "min": 1,
                "max": 65535,
            },
            {
                "key": "pg_db",
                "label": translate("DatastoreTabMixin", "Database"),
                "type": "text",
                "required": True,
            },
            {
                "key": "pg_user",
                "label": translate("DatastoreTabMixin", "User"),
                "type": "text",
                "required": True,
            },
            {
                "key": "pg_password",
                "label": translate("DatastoreTabMixin", "Password"),
                "type": "text",
                "required": not edit_mode,
                "echo_password": True,
                "help": (
                    translate(
                        "DatastoreTabMixin", "Leave empty to keep the stored password"
                    )
                    if edit_mode
                    else None
                ),
            },
            {
                "key": "pg_schema",
                "label": translate("DatastoreTabMixin", "Schema"),
                "type": "text",
                "default": "public",
            },
            # --- JNDI fields ---
            {
                "key": "jndi_reference",
                "label": translate("DatastoreTabMixin", "JNDI Reference"),
                "type": "text",
                "required": True,
                "placeholder": "java:comp/env/jdbc/mydb",
                "visible": False,
                "help": translate(
                    "DatastoreTabMixin", "JNDI name of the database connection pool"
                ),
            },
            # --- "Other...": any GeoServer type, by its own name ---
            {
                "key": "custom_type",
                "label": translate("DatastoreTabMixin", "GeoServer type"),
                "type": "text",
                "visible": False,
                "placeholder": translate("DatastoreTabMixin", "e.g. Properties"),
                "help": translate(
                    "DatastoreTabMixin",
                    "The type exactly as GeoServer names it (its web UI lists "
                    "them under New data source). Some need an extension.",
                ),
            },
            # --- any other type: shown read-only, since the form cannot edit it ---
            {
                "key": "raw_params",
                "label": translate("DatastoreTabMixin", "Connection parameters"),
                "type": "keyvalue",
                "visible": False,
                "max_height": 320,
                "help": (
                    translate(
                        "DatastoreTabMixin",
                        "Exactly as GeoServer stores them. A parameter you remove "
                        "is removed on the server; a masked password (••••) is "
                        "kept as it is unless you replace it.",
                    )
                    if edit_mode
                    else translate(
                        "DatastoreTabMixin",
                        "The type's connection parameters, named as GeoServer's "
                        "documentation of that type names them.",
                    )
                ),
            },
            # --- PMTiles fields ---
            {
                "key": "pmtiles_url",
                "label": translate("DatastoreTabMixin", "PMTiles URL"),
                "type": "text",
                "required": True,
                "placeholder": "file:///mnt/data/tiles.pmtiles",
                "visible": False,
                "help": translate(
                    "DatastoreTabMixin",
                    "Path or URL to the PMTiles file (file://, s3://, gs://, http(s)://)",
                ),
            },
            # --- Shapefile / directory of shapefiles ---
            {
                "key": "file_url",
                "label": translate("DatastoreTabMixin", "File or folder"),
                "type": "text",
                "required": True,
                "visible": False,
                "placeholder": "file:data/shapefiles/states.shp",
                "help": translate(
                    "DatastoreTabMixin",
                    "A path on the GeoServer machine: relative to its data "
                    "directory (file:data/…) or absolute (file:///…). A single "
                    ".shp, or the folder holding them for a directory store.",
                ),
            },
            {
                "key": "charset",
                "label": translate("DatastoreTabMixin", "Attribute charset"),
                "type": "text",
                "visible": False,
                "placeholder": translate(
                    "DatastoreTabMixin", "Leave empty for GeoServer's default"
                ),
                "help": translate(
                    "DatastoreTabMixin",
                    "How the .dbf attribute text is encoded: UTF-8, or "
                    "ISO-8859-1, which is what GeoServer assumes",
                ),
            },
            {
                "key": "spatial_index",
                "label": translate("DatastoreTabMixin", "Create a spatial index"),
                "type": "checkbox",
                "default": True,
                "visible": False,
                "help": translate(
                    "DatastoreTabMixin", "Writes a .qix file next to the data, once"
                ),
            },
            # --- GeoPackage ---
            {
                "key": "gpkg_database",
                "label": translate("DatastoreTabMixin", "GeoPackage file"),
                "type": "text",
                "required": True,
                "visible": False,
                "placeholder": "file:data/ne/natural_earth.gpkg",
                "help": translate(
                    "DatastoreTabMixin",
                    "A path on the GeoServer machine. To publish a .gpkg from "
                    "this computer instead, use Publish a Layer on the Layers "
                    "tab, which uploads it.",
                ),
            },
            {
                "key": "gpkg_read_only",
                "label": translate("DatastoreTabMixin", "Read-only"),
                "type": "checkbox",
                "default": False,
                "visible": False,
                "help": translate(
                    "DatastoreTabMixin",
                    "Recommended when nothing writes to the file: GeoServer "
                    "then serves it without taking write locks",
                ),
            },
            {
                "key": "gpkg_expose_pk",
                "label": translate("DatastoreTabMixin", "Expose primary keys"),
                "type": "checkbox",
                "default": False,
                "visible": False,
                "help": translate(
                    "DatastoreTabMixin",
                    "Publish the tables' primary key as an attribute",
                ),
            },
            # --- Web Feature Server (NG): a remote WFS cascaded as a datastore ---
            {
                "key": "wfs_url",
                "label": translate("DatastoreTabMixin", "GetCapabilities URL"),
                "type": "text",
                "required": True,
                "visible": False,
                "placeholder": "https://example.com/geoserver/wfs?service=WFS&request=GetCapabilities",
                "help": translate(
                    "DatastoreTabMixin",
                    "The remote WFS's capabilities document. Its feature types can "
                    "then be published as layers of this server (Publish a Layer, "
                    "a table in a datastore).",
                ),
            },
            {
                "key": "wfs_user",
                "label": translate("DatastoreTabMixin", "User"),
                "type": "text",
                "visible": False,
                "placeholder": translate(
                    "DatastoreTabMixin", "Leave empty for a public service"
                ),
            },
            {
                "key": "wfs_password",
                "label": translate("DatastoreTabMixin", "Password"),
                "type": "text",
                "echo_password": True,
                "visible": False,
                "help": (
                    translate(
                        "DatastoreTabMixin", "Leave empty to keep the stored password"
                    )
                    if edit_mode
                    else None
                ),
            },
            {
                "key": "wfs_timeout",
                "label": translate("DatastoreTabMixin", "Timeout (ms)"),
                "type": "spinbox",
                "default": 3000,
                "min": 0,
                "max": 3600000,
                "visible": False,
            },
            {
                "key": "wfs_max_features",
                "label": translate("DatastoreTabMixin", "Max features"),
                "type": "spinbox",
                "default": 0,
                "min": 0,
                "max": 100000000,
                "visible": False,
                "help": translate("DatastoreTabMixin", "0 means no limit"),
            },
            {
                "key": "wfs_lenient",
                "label": translate("DatastoreTabMixin", "Lenient parsing"),
                "type": "checkbox",
                "default": True,
                "visible": False,
                "help": translate(
                    "DatastoreTabMixin",
                    "Tolerate responses that do not match the remote's schema exactly",
                ),
            },
        ]
        if edit_mode:
            # GeoServer disables a store whose connection failed at startup;
            # this is the switch back. Creating one always enables it.
            fields.insert(
                4,
                {
                    "key": "enabled",
                    "label": translate("DatastoreTabMixin", "Enabled"),
                    "type": "checkbox",
                    "default": True,
                    "help": translate(
                        "DatastoreTabMixin",
                        "A disabled store serves none of its layers. GeoServer "
                        "disables one itself when its connection fails at startup.",
                    ),
                },
            )
        if edit_mode and typed:
            # Every parameter the typed fields do not own. Not in Add, nor
            # for the generic editor: an empty Advanced tab either way.
            fields.append(
                {
                    "key": "other_params",
                    "label": translate("DatastoreTabMixin", "Other parameters"),
                    "type": "keyvalue",
                    "group": translate("DatastoreTabMixin", "Advanced"),
                    "max_height": 320,
                    "help": translate(
                        "DatastoreTabMixin",
                        "What the General tab does not show (pool sizes, "
                        "timeouts, Loose bbox, ...). A parameter you remove is "
                        "removed on the server; a masked password (••••) is kept "
                        "unless you replace it.",
                    ),
                },
            )
        return fields

    @staticmethod
    def _wfs_params(values, stored=None):
        """Connection parameters for a cascaded WFS store, from the typed fields.

        `stored` is the server's current map on an edit: a blank password keeps
        the stored one. GeoServer accepts its own `crypt1:` value back
        (measured on 2.28.5: a store still connected after the round trip),
        and a blank user means no credentials at all.

        TODO(#1): upstream as create_wfs_datastore(ws, name, capabilities_url,
        user=None, password=None, timeout=3000, max_features=0, lenient=True),
        next to create_pg_datastore() (row 41).
        """
        params = {
            _WFS_URL: (values.get("wfs_url") or "").strip(),
            _WFS_KEY + "TIMEOUT": str(int(values.get("wfs_timeout") or 0)),
            _WFS_KEY + "MAXFEATURES": str(int(values.get("wfs_max_features") or 0)),
            _WFS_KEY + "LENIENT": str(bool(values.get("wfs_lenient", True))).lower(),
        }
        user = (values.get("wfs_user") or "").strip()
        if user:
            params[_WFS_USER] = user
            password = values.get("wfs_password") or (stored or {}).get(_WFS_PASSWORD)
            if password:
                params[_WFS_PASSWORD] = password
        return params

    @staticmethod
    def _file_store_params(ds_type, values):
        """Connection parameters for a file-based store, from the typed fields.

        Only the keys the form owns: the caller merges them onto whatever
        GeoServer already has (invariant 3), so an unmentioned parameter like
        `namespace` or `fetch size` survives an edit.

        TODO(#1): upstream as create_shapefile_datastore() /
        create_geopackage_datastore(), next to create_pg_datastore(). The
        library has typed creators for PostGIS, JNDI and PMTiles only, so
        these go through the generic create_datastore().
        """
        if ds_type == _GEOPACKAGE:
            return {
                "database": values.get("gpkg_database", ""),
                # How GeoServer picks the GeoPackage factory.
                "dbtype": _GEOPACKAGE_DBTYPE,
                "read_only": str(bool(values.get("gpkg_read_only"))).lower(),
                "Expose primary keys": str(bool(values.get("gpkg_expose_pk"))).lower(),
            }
        params = {"url": values.get("file_url", "")}
        charset = (values.get("charset") or "").strip()
        if charset:
            # Left empty, GeoServer applies its own default rather than a
            # blank charset the shapefile reader would choke on.
            params["charset"] = charset
        if ds_type == _SHAPEFILE:
            params["create spatial index"] = str(
                bool(values.get("spatial_index"))
            ).lower()
        return params

    def _on_type_changed(self, dlg, new_type):
        """Show the form fields of the selected datastore type, hide the rest."""
        wanted = _TYPE_FIELDS.get(new_type, ())
        for key in _TYPE_SPECIFIC_FIELDS:
            dlg.set_field_visible(key, key in wanted)

    def _add_datastore(self):
        """Open a form dialog to create a new datastore."""
        workspace_names = self._fetch(
            self._get_workspace_names,
            translate("DatastoreTabMixin", "Failed to load the workspaces"),
        )
        if workspace_names is None:
            return
        if not workspace_names:
            self.show_warning_message(
                translate(
                    "DatastoreTabMixin",
                    "No workspaces available. Create a workspace first.",
                )
            )
            return

        dlg = ResourceFormDialog(
            title=translate("DatastoreTabMixin", "Add a Datastore"),
            description=translate(
                "DatastoreTabMixin",
                "A datastore is where GeoServer reads a layer's data from: a "
                "database, or a file on its own machine.",
            ),
            fields=self._datastore_fields(workspace_names),
            parent=self,
            ok_label=translate("DatastoreTabMixin", "Create"),
            validate=self._form_check(self._check_new_datastore),
        )
        self._wire_type_combo(dlg)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        values = dlg.get_values()
        if self._run_action(
            lambda: self._wait_for_save(
                lambda: self._create_datastore_from_values(values)
            ),
            translate("DatastoreTabMixin", "Failed to create datastore '{}'").format(
                values["name"]
            ),
        ):
            self.show_success_message(
                translate("DatastoreTabMixin", "Datastore '{}' created.").format(
                    values["name"]
                )
            )
            self._warn_if_reaches_nothing(values)
            self._load_datastores()

    def _show_generic_editor(self, dlg, ds_type):
        """Edit a datastore type the form has no dedicated fields for.

        Properties, CSV, Oracle, …: the real type, locked; the typed fields
        hidden; the stored connection parameters in the key/value table. The
        save path is the same merge as for typed stores, so GeoServer still
        validates the result.
        """
        combo = dlg.get_widget("type")
        if combo is not None:
            combo.addItem(ds_type)
            combo.setCurrentText(ds_type)
            combo.setEnabled(False)
        for key in _TYPE_SPECIFIC_FIELDS:
            dlg.set_field_visible(key, False)
        dlg.set_field_visible("raw_params", True)

    @staticmethod
    def _parse_params(pairs):
        """The form's key/value pairs as a dict, keys and values stripped,
        except a secret's value: its edge spaces are part of it."""
        return {
            str(key).strip(): (
                str(value) if _is_masked(str(key), value) else str(value).strip()
            )
            for key, value in (pairs or {}).items()
            if str(key).strip()
        }

    def _wire_type_combo(self, dlg, initial_type=None, locked=False):
        """Show only the connection fields that belong to the selected type.

        The combo is wired after the dialog exists because the handler needs
        the dialog itself. `locked` is for edit mode: the type is displayed
        but cannot be changed.
        """
        type_combo = dlg.get_widget("type")
        if type_combo is None:
            return
        dlg.on_value_changed("type", lambda kind: self._on_type_changed(dlg, kind))
        self._on_type_changed(dlg, initial_type or dlg.get_values()["type"])
        if locked:
            type_combo.setEnabled(False)

    def _set_workspace_namespace(self, ws, name):
        """Give a store its workspace's namespace URI, when it has another.

        TODO(#1): create_pg_datastore(), create_jndi_datastore() and
        create_pmtiles_datastore() send namespace=http://{ws}, whatever the
        workspace's own URI (row 64); a store created without one gets the
        workspace's (measured on 2.28.5), as the web interface does. So a
        workspace with its own URI got a store serving another namespace.
        Merged onto the stored parameters, as every other store save is.
        """
        uri = self._namespace_uri(ws)
        if not uri or uri == f"http://{ws}":
            return  # what the library sent already
        detail = self._check(self.gs.get_datastore(ws, name))
        params = dict(self._connection_params(detail))
        if params.get("namespace") == uri:
            return
        params["namespace"] = uri
        self._check(
            self.gs.create_datastore(
                workspace_name=ws,
                datastore_name=name,
                datastore_type=detail.get("type"),
                connection_parameters=params,
                description=detail.get("description"),
                enabled=bool(detail.get("enabled", True)),
            )
        )

    def _check_new_datastore(self, values):
        """Refuse, before anything is sent, a datastore the Add form cannot
        create. Reads only: the form runs it before it closes."""
        ws, name = values["workspace"], values["name"]
        # The name goes into a REST path: refuse what a URL would eat.
        self._require_safe_name(name)
        if values["type"] == _OTHER and not (values.get("custom_type") or "").strip():
            raise ValueError(
                translate("DatastoreTabMixin", "Give the GeoServer type name.")
            )
        # create_* upserts, so an existing name would overwrite a live store
        if self._resource_exists(self._get_datastore, ws, name):
            raise ValueError(
                translate(
                    "DatastoreTabMixin",
                    "Datastore '{}' already exists in workspace '{}'.",
                ).format(name, ws)
            )

    def _create_datastore_from_values(self, values):
        """Call the appropriate library method based on the datastore type."""
        ws = values["workspace"]
        name = values["name"]
        ds_type = values["type"]
        description = values.get("description") or None
        self._check_new_datastore(values)

        if ds_type == "PostGIS":
            self._check(
                self.gs.create_pg_datastore(
                    workspace_name=ws,
                    datastore_name=name,
                    pg_host=values.get("pg_host", ""),
                    pg_port=int(values.get("pg_port", 5432)),
                    pg_db=values.get("pg_db", ""),
                    pg_user=values.get("pg_user", ""),
                    pg_password=values.get("pg_password", ""),
                    pg_schema=values.get("pg_schema", "public") or "public",
                    description=description,
                )
            )
        elif ds_type == "PostGIS (JNDI)":
            self._check(
                self.gs.create_jndi_datastore(
                    workspace_name=ws,
                    datastore_name=name,
                    jndi_reference=values.get("jndi_reference", ""),
                    pg_schema=values.get("pg_schema", "public") or "public",
                    description=description,
                )
            )
        elif ds_type == "PMTiles":
            self._check(
                self.gs.create_pmtiles_datastore(
                    workspace_name=ws,
                    datastore_name=name,
                    pmtiles_url=values.get("pmtiles_url", ""),
                    description=description,
                )
            )
        if ds_type in ("PostGIS", "PostGIS (JNDI)", "PMTiles"):
            try:
                self._set_workspace_namespace(ws, name)
            except Exception as error:  # the store itself is created
                raise PartlySaved(
                    translate(
                        "DatastoreTabMixin",
                        "Datastore '{}' is created, but its namespace could not be "
                        "set to the workspace's: {}",
                    ).format(name, self._error_text(error))
                ) from error
            return
        if ds_type in (_SHAPEFILE, _SHAPEFILE_DIRECTORY, _GEOPACKAGE, _WFS):
            params = (
                self._wfs_params(values)
                if ds_type == _WFS
                else self._file_store_params(ds_type, values)
            )
            self._check(
                self.gs.create_datastore(
                    workspace_name=ws,
                    datastore_name=name,
                    datastore_type=ds_type,
                    connection_parameters=params,
                    description=description,
                )
            )
        elif ds_type == _OTHER:
            custom = values["custom_type"].strip()
            self._check(
                self.gs.create_datastore(
                    workspace_name=ws,
                    datastore_name=name,
                    datastore_type=custom,
                    connection_parameters=self._parse_params(values.get("raw_params")),
                    description=description,
                )
            )

    def _save_datastore_edit(self, values, before, old_name):
        """The edit form's Save: read the store again, apply what was changed.

        `before` is what the form gave back untouched. Another client may
        have edited, renamed or deleted the store since the form opened:
        their edits survive, and a store that is gone is refused, because
        create_datastore() POSTs on a 404 and recreated it empty.
        """
        detail, status = self._get_datastore(values["workspace"], old_name)
        if status == 404:
            raise RuntimeError(
                translate(
                    "DatastoreTabMixin",
                    "Datastore '{}' is no longer on the server: it was deleted or "
                    "renamed since the form opened. Refresh the list.",
                ).format(old_name)
            )
        detail = self._check((detail, status))
        self._update_datastore_from_values(
            values,
            detail,
            self._connection_params(detail),
            old_name=old_name,
            before=before,
        )

    def _update_datastore_from_values(
        self, values, detail, conn_params, old_name=None, before=None
    ):
        """Save an edit without discarding server-side configuration.

        The typed create_* helpers post a fixed connection-parameter template
        and GeoServer applies it by REPLACING the stored map, so editing just a
        description used to drop pool settings, Loose bbox, the real namespace
        and the PMTiles range-reader config, and force enabled=true on a
        disabled store. Merge the fields the form owns onto what the server
        actually has, and keep its own type and enabled flag.

        `before` is what the form gave back untouched. With it, `detail` is
        the store read again at Save, and only the fields the user changed
        are applied to it: a charset or a disable another client saved
        meanwhile is kept.

        A changed name is first applied by one PUT on the old path, which
        GeoServer treats as a rename (measured on 2.28.5: the feature types,
        layers, groups and tile cache follow); create_datastore() would upsert
        a second store instead (invariant 6).

        TODO(#1): upstream as update_datastore(...) that merges server-side,
        and rename_datastore() (row 56).
        """
        if not isinstance(detail, dict):
            raise RuntimeError(
                translate(
                    "DatastoreTabMixin",
                    "GeoServer did not report this datastore's type, so it cannot "
                    "be updated safely.",
                )
            )
        # None for a store without one: the PUT sends null, and GeoServer
        # keeps it without a type (measured on 2.28.5).
        ds_type = detail.get("type")
        merged = self._merged_params(ds_type, values, conn_params)
        # The form's own checkbox wins; without one (older callers, tests) the
        # server's flag is kept.
        enabled = (
            values["enabled"] if "enabled" in values else detail.get("enabled", True)
        )
        # "" clears it; None leaves the key out of the PUT, and GeoServer
        # keeps what it had (measured on 2.28.5).
        description = values.get("description") or ""
        if before is not None:
            merged = self._changes_onto(
                conn_params, self._merged_params(ds_type, before, conn_params), merged
            )
            if values.get("enabled") == before.get("enabled"):
                enabled = detail.get("enabled", True)
            if description == (before.get("description") or ""):
                description = None
        renamed = bool(old_name and values["name"] != old_name)
        if renamed:
            self._rename_datastore(values["workspace"], old_name, values["name"])

        if isinstance(enabled, str):  # .json gives a bool, but do not assume
            enabled = enabled.strip().lower() == "true"

        try:
            self._check(
                self.gs.create_datastore(
                    workspace_name=values["workspace"],
                    datastore_name=values["name"],
                    datastore_type=ds_type,
                    connection_parameters=merged,
                    description=description,
                    enabled=bool(enabled),
                )
            )
        except Exception as error:
            if not renamed:
                raise
            # The table still showed the old name, whose row now answered 404.
            raise PartlySaved(
                translate(
                    "DatastoreTabMixin",
                    "Datastore renamed to '{}', but the rest of the edit was not "
                    "saved: {}",
                ).format(values["name"], self._error_text(error))
            ) from error

    @staticmethod
    def _changes_onto(current, untouched, edited):
        """`current` with what differs between `untouched` and `edited`:
        a changed or added key set, a removed one dropped."""
        result = dict(current)
        for key, value in edited.items():
            if key not in untouched or untouched[key] != value:
                result[key] = value
        for key in untouched.keys() - edited.keys():
            result.pop(key, None)
        return result

    def _merged_params(self, ds_type, values, conn_params):
        """The connection parameters the form's values make of `conn_params`."""
        merged = dict(conn_params)
        if ds_type == "PostGIS":
            merged.update(
                {
                    "host": values.get("pg_host", ""),
                    "port": self._kept_port(conn_params.get("port"), values),
                    "database": values.get("pg_db", ""),
                    "user": values.get("pg_user", ""),
                    # Blank keeps the stored (encrypted) value; see _wfs_params.
                    "passwd": values.get("pg_password")
                    or conn_params.get("passwd", ""),
                    "schema": values.get("pg_schema", "public") or "public",
                }
            )
        elif ds_type == "PostGIS (JNDI)":
            merged.update(
                {
                    "jndiReferenceName": values.get("jndi_reference", ""),
                    "schema": values.get("pg_schema", "public") or "public",
                }
            )
        elif ds_type == "PMTiles":
            merged["pmtiles"] = values.get("pmtiles_url", "")
        elif ds_type in (_SHAPEFILE, _SHAPEFILE_DIRECTORY, _GEOPACKAGE):
            merged.update(self._file_store_params(ds_type, values))
            if ds_type != _GEOPACKAGE and not (values.get("charset") or "").strip():
                # Blank means "GeoServer's default": the merge kept the old one.
                merged.pop("charset", None)
        elif ds_type == _WFS:
            params = self._wfs_params(values, conn_params)
            merged.update(params)
            for key in (_WFS_USER, _WFS_PASSWORD):
                if key not in params:  # credentials cleared in the form
                    merged.pop(key, None)
        else:
            # Generic editor: what the user left in the table is the whole map
            # (a removed row removes its key); an untouched row keeps its value.
            edited = self._parse_params(values.get("raw_params"))
            merged = {
                key: self._kept(conn_params, key, value)
                for key, value in edited.items()
            }

        if ds_type in _OWNED_PARAMS and "other_params" in values:
            merged = self._merge_other_params(
                merged, conn_params, ds_type, values["other_params"]
            )
        return merged

    @staticmethod
    def _kept_port(stored, values):
        """The port to save: the stored text unless the form changed it.

        The spinbox can only show a number, so "${PG_PORT}" (a parametrised
        port) was prefilled as 5432 and saved as 5432 by an unrelated edit.
        """
        port = int(values.get("pg_port", 5432))
        if stored is not None and _as_int(stored, 5432) == port:
            return stored
        return port

    @staticmethod
    def _masked(params):
        """The parameters to show: secrets as the mask, never their value."""
        return {key: _shown(key, value) for key, value in sorted(params.items())}

    @staticmethod
    def _kept(conn_params, key, value):
        """What to send for an edited parameter: the stored value while the
        row still shows what the form prefilled, else what was typed.

        The table only holds text. An untouched row sent back as text turned
        an empty parameter into "None", which GeoServer then ran as the
        session startup SQL of every connection, and a number into a string.
        """
        if key in conn_params and value == _shown(key, conn_params[key]).strip():
            return conn_params[key]
        return value

    @staticmethod
    def _other_params(ds_type, conn_params):
        """The parameters a typed form does not own, secrets masked."""
        skip = set(_OWNED_PARAMS.get(ds_type, ())) | set(_HIDDEN_PARAMS)
        return DatastoreTabMixin._masked(
            {key: value for key, value in conn_params.items() if key not in skip}
        )

    def _merge_other_params(self, merged, conn_params, ds_type, pairs):
        """Apply the Other parameters table onto the merged map.

        A removed row removes the key, a masked value keeps the stored one,
        and a key the typed fields own is theirs: those are written last.
        """
        owned = set(_OWNED_PARAMS[ds_type]) | set(_HIDDEN_PARAMS)
        shown = {key for key in conn_params if key not in owned}
        edited = self._parse_params(pairs)
        result = {
            key: value
            for key, value in merged.items()
            if key not in shown or key in edited
        }
        for key, value in edited.items():
            if key in owned:
                continue
            result[key] = self._kept(conn_params, key, value)
        return result

    def _datastore_path(self, workspace_name, datastore_name):
        """The store's REST path, its segments quoted (see _addressable)."""
        return self.gs.rest_service.rest_endpoints.datastore(
            quote(workspace_name, safe=""), quote(datastore_name, safe="")
        )

    def _check_datastore_rename(self, workspace_name, old_name, new_name):
        """Refuse, before any request, a new name a URL would eat or one
        taken: a taken name would leave two stores fighting over one path.
        Reads only: the edit form runs it before it closes, the rename again."""
        if new_name == old_name:
            return
        self._require_safe_name(new_name)
        if self._resource_exists(self._get_datastore, workspace_name, new_name):
            raise ValueError(
                translate(
                    "DatastoreTabMixin",
                    "Datastore '{}' already exists in workspace '{}'.",
                ).format(new_name, workspace_name)
            )

    def _rename_datastore(self, workspace_name, old_name, new_name):
        """Rename a datastore: one PUT with the new name on the old path.

        TODO(#1): no rename in the library (row 56).
        """
        self._check_datastore_rename(workspace_name, old_name, new_name)
        self._raw_rest(
            "put",
            self._datastore_path(workspace_name, old_name),
            json={"dataStore": {"name": new_name}},
        )

    @staticmethod
    def _connection_params(detail):
        """The datastore's connectionParameters as a plain dict."""
        if not isinstance(detail, dict):
            return {}
        entry = detail.get("connectionParameters", {}).get("entry", {})
        return entry if isinstance(entry, dict) else {}

    @staticmethod
    def _datastore_form_values(ws_name, ds_name, ds_type, detail, conn_params):
        """Prefill for the edit form, from what GeoServer returned.

        The type is passed through as-is; for a type without dedicated fields
        the caller (_show_generic_editor) adds it to the combo and locks it.
        """
        return {
            "workspace": ws_name,
            "name": ds_name,
            "type": ds_type,
            "description": (
                detail.get("description", "") if isinstance(detail, dict) else ""
            ),
            "enabled": (
                str(detail.get("enabled", True)).strip().lower() == "true"
                if isinstance(detail, dict)
                else True
            ),
            # PostGIS
            "pg_host": conn_params.get("host", ""),
            # GeoServer allows `${PG_PORT}` here (environment parametrisation).
            "pg_port": _as_int(conn_params.get("port"), 5432),
            "pg_db": conn_params.get("database", ""),
            "pg_user": conn_params.get("user", ""),
            # Never prefilled: GeoServer returns it encrypted ("crypt1:…") or
            # not at all, and writing that back would replace the real password.
            "pg_password": "",
            "pg_schema": conn_params.get("schema", "public"),
            # JNDI
            "jndi_reference": conn_params.get("jndiReferenceName", ""),
            # PMTiles
            "pmtiles_url": conn_params.get("pmtiles", ""),
            # Shapefile and directory of shapefiles
            "file_url": conn_params.get("url", ""),
            "charset": conn_params.get("charset", ""),
            "spatial_index": str(
                conn_params.get("create spatial index", "true")
            ).lower()
            == "true",
            # GeoPackage
            "gpkg_database": conn_params.get("database", ""),
            "gpkg_read_only": str(conn_params.get("read_only", "false")).lower()
            == "true",
            "gpkg_expose_pk": str(
                conn_params.get("Expose primary keys", "false")
            ).lower()
            == "true",
            # Cascaded WFS; the password is never prefilled (see pg_password)
            "wfs_url": conn_params.get(_WFS_URL, ""),
            "wfs_user": conn_params.get(_WFS_USER, ""),
            "wfs_password": "",
            "wfs_timeout": _as_int(conn_params.get(_WFS_KEY + "TIMEOUT"), 3000),
            "wfs_max_features": _as_int(conn_params.get(_WFS_KEY + "MAXFEATURES"), 0),
            "wfs_lenient": str(conn_params.get(_WFS_KEY + "LENIENT", "true")).lower()
            == "true",
            # Typed stores: what their form does not own; secrets masked
            "other_params": DatastoreTabMixin._other_params(ds_type, conn_params),
            # Generic editor for types without dedicated fields; secrets masked
            "raw_params": DatastoreTabMixin._masked(conn_params),
        }

    def _show_datastore_info(self, row_data):
        """Open a form dialog to view/edit an existing datastore."""
        ds_name, ws_name, ds_type = row_data[0], row_data[1], row_data[2]
        detail = self._fetch(
            lambda: self._check(self._get_datastore(ws_name, ds_name)),
            translate("DatastoreTabMixin", "Failed to load datastore details"),
        )
        if detail is None:
            return

        # The row's Type cell can be "-" after a transient GET failure at list
        # time; the detail we just fetched is authoritative, "-" for no type.
        if isinstance(detail, dict):
            ds_type = detail.get("type") or "-"
        conn_params = self._connection_params(detail)
        values = self._datastore_form_values(
            ws_name, ds_name, ds_type, detail, conn_params
        )
        editable = ds_type in _OWNED_PARAMS

        dlg = ResourceFormDialog(
            title=translate("DatastoreTabMixin", "Datastore '{}'").format(ds_name),
            description=(
                translate(
                    "DatastoreTabMixin",
                    "Change the connection or the description; Save keeps every "
                    "parameter this form does not show.",
                )
                if editable
                else translate(
                    "DatastoreTabMixin",
                    "Datastore type '{}' has no dedicated form. Edit its connection "
                    "parameters directly.",
                ).format(ds_type)
            ),
            # Edit mode locks the workspace, so its own name is all the combo needs.
            fields=self._datastore_fields([ws_name], edit_mode=True, typed=editable),
            values=values,
            parent=self,
            validate=self._form_check(
                lambda after: self._check_datastore_rename(
                    ws_name, ds_name, after["name"]
                )
            ),
        )
        if editable:
            self._wire_type_combo(dlg, initial_type=values["type"], locked=True)
        else:
            self._show_generic_editor(dlg, ds_type)
        before = dlg.get_values()  # as the form gives it back untouched
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        values = dlg.get_values()
        if self._run_action(
            lambda: self._wait_for_save(
                lambda: self._save_datastore_edit(values, before, ds_name)
            ),
            translate("DatastoreTabMixin", "Failed to update datastore '{}'").format(
                values["name"]
            ),
        ):
            self.show_success_message(
                translate("DatastoreTabMixin", "Datastore '{}' saved.").format(
                    values["name"]
                )
            )
            self._warn_if_reaches_nothing(values)
            self._load_datastores()

    def _warn_if_reaches_nothing(self, values):
        """Listing the store's unpublished tables makes GeoServer connect: a
        wrong host, password or path shows now, not at the first layer."""
        ws, name = values["workspace"], values["name"]
        self._warn_if_store_unreachable(name, lambda: self._available_tables(ws, name))

    def _reset_datastore(self, row_data):
        """Make GeoServer re-read the store: a changed schema, new tables, a
        rotated password on the database side. TODO(#1): no reset in the
        library (row 54): POST .../reset (measured on 2.28.5)."""
        name, ws_name = row_data[0], row_data[1]
        path = self._datastore_path(ws_name, name)
        if self._run_action(
            lambda: self._wait_for_save(
                lambda: self._raw_rest("post", path.removesuffix(".json") + "/reset")
            ),
            translate("DatastoreTabMixin", "Failed to reset '{}'").format(name),
        ):
            self.show_success_message(
                translate(
                    "DatastoreTabMixin", "'{}' reset: GeoServer re-reads it."
                ).format(name)
            )

    def _delete_datastore(self, row_data):
        """Delete a single datastore after confirmation."""
        self._delete_selected_datastores([row_data])

    def _delete_selected_datastores(self, selected_rows):
        """Delete one or more datastores after confirmation."""
        self._delete_many(
            [
                (
                    f"{row[1]}:{row[0]}",
                    lambda ws=row[1], ds=row[0]: self._do_delete_datastore(ws, ds),
                )
                for row in selected_rows
            ],
            self._load_datastores,
            ask=self._one_or_many(
                translate(
                    "DatastoreTabMixin",
                    "Are you sure you want to delete datastore '{}'?",
                ),
                lambda n: translate(
                    "DatastoreTabMixin",
                    "Are you sure you want to delete %n datastore(s)?",
                    None,
                    n,
                ),
            ),
            done=self._one_or_many(
                translate("DatastoreTabMixin", "Datastore '{}' deleted."),
                lambda n: translate(
                    "DatastoreTabMixin", "%n datastore(s) deleted.", None, n
                ),
            ),
            # _do_delete_datastore sends recurse=true
            cascade=translate(
                "DatastoreTabMixin", "Every layer published from it is deleted too."
            ),
        )

    def _do_delete_datastore(self, workspace_name, datastore_name):
        """Execute the REST DELETE for a datastore (recurse=true removes feature types too)."""
        # TODO(#1): upstream as delete_datastore(ws, ds, recurse=True); the library
        # has none. Workaround: DELETE /workspaces/{ws}/datastores/{ds}.json?recurse=true
        path = self._datastore_path(workspace_name, datastore_name)
        self._raw_rest("delete", path, params={"recurse": "true"})
