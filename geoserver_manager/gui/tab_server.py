#! python3  # noqa: E265

"""
Server tab: the settings that belong to the whole GeoServer, not to one
resource. Contact, global settings, the four services, logging with the log
itself, and the catalog's reload and reset. One row each; a click opens it.

Used as a mixin for GeoServerMainDialog.

TODO(#1): the library has none of these calls (row 60), so every read and
write here is a raw REST call. Measured on 2.28.5: the service settings
merge a partial PUT, but the global settings, the contact and the logging
REPLACE the stored object (a PUT of the proxy URL alone wiped the contact),
so those three are sent back whole, with the form's fields merged in.
"""

import threading
from collections import deque

import requests
from qgis.core import Qgis
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtWidgets import QDialog, QMessageBox

from geoserver_manager.gui.dlg_resource_form import ResourceFormDialog
from geoserver_manager.toolbelt.payload import changed, keyword_list, words
from geoserver_manager.toolbelt.rest import Abandoned, summarise_body

# Strings are looked up in this file's own context: self.tr() would resolve
# against GeoServerMainDialog instead (see docs/development/architecture.md).
translate = QCoreApplication.translate

SERVICES = ("wms", "wfs", "wcs", "wmts")

# GeoServer's logging profiles, as its web admin lists them.
LOG_LEVELS = (
    "DEFAULT_LOGGING",
    "PRODUCTION_LOGGING",
    "QUIET_LOGGING",
    "VERBOSE_LOGGING",
    "GEOTOOLS_DEVELOPER_LOGGING",
    "GEOSERVER_DEVELOPER_LOGGING",
    "TEST_LOGGING",
)

# The log viewer keeps the end of the file only: GeoServer serves it whole
# (no Range support, gzip, no length), and a production log can be large.
_LOG_TAIL_BYTES = 256 * 1024
_LOG_TAIL_LINES = 500

# (form key, REST key) of each form, for payload.changed().
_CONTACT_KEYS = (
    ("person", "contactPerson"),
    ("position", "contactPosition"),
    ("organization", "contactOrganization"),
    ("email", "contactEmail"),
    ("phone", "contactVoice"),
    ("address", "address"),
    ("city", "addressCity"),
    ("state", "addressState"),
    ("postal_code", "addressPostalCode"),
    ("country", "addressCountry"),
    ("online_resource", "onlineResource"),
    ("welcome", "welcome"),
)
_GLOBAL_KEYS = (
    ("proxy_base_url", "proxyBaseUrl"),
    ("use_headers_proxy", "useHeadersProxyURL"),
    ("charset", "charset"),
    ("num_decimals", "numDecimals"),
    ("verbose", "verbose"),
    ("verbose_exceptions", "verboseExceptions"),
)
_SERVICE_KEYS = (
    ("enabled", "enabled"),
    ("title", "title"),
    ("abstract", "abstrct"),  # sic: GeoServer's own spelling
    ("maintainer", "maintainer"),
    ("online_resource", "onlineResource"),
    ("fees", "fees"),
    ("access_constraints", "accessConstraints"),
)
_WFS_KEYS = _SERVICE_KEYS + (("max_features", "maxFeatures"),)
_LOGGING_KEYS = (
    ("level", "level"),
    ("location", "location"),
    ("stdout", "stdOutLogging"),
)

# The web admin's page for each row, under /web/wicket/bookmarkable/.
_WEB_PAGES = {
    "contact": "org.geoserver.web.admin.ContactPage",
    "global": "org.geoserver.web.admin.GlobalSettingsPage",
    "wms": "org.geoserver.wms.web.WMSAdminPage",
    "wfs": "org.geoserver.wfs.web.WFSAdminPage",
    "wcs": "org.geoserver.wcs.web.WCSAdminPage",
    "wmts": "org.geoserver.gwc.web.wmts.WMTSAdminPage",
    "logging": "org.geoserver.web.admin.LogPage",
    "catalog": "org.geoserver.web.admin.StatusPage",
}


def _keys_of(kind):
    """The (form key, REST key) pairs of one row's form."""
    return {
        "contact": _CONTACT_KEYS,
        "global": _GLOBAL_KEYS,
        "logging": _LOGGING_KEYS,
        "wfs": _WFS_KEYS,
    }.get(kind, _SERVICE_KEYS)


class ServerTabMixin:
    """Mixin that adds the server-wide settings to the main dialog."""

    def _server_sections(self):
        """(kind, row label) of every row, in table order."""
        return [
            ("contact", translate("ServerTabMixin", "Contact")),
            ("global", translate("ServerTabMixin", "Global settings")),
            *((service, service.upper()) for service in SERVICES),
            ("logging", translate("ServerTabMixin", "Logging")),
            ("catalog", translate("ServerTabMixin", "Catalog")),
        ]

    def _server_kind(self, row_data):
        """The kind behind a row, found by its label."""
        return dict((label, kind) for kind, label in self._server_sections()).get(
            row_data[0]
        )

    # -- Load -----------------------------------------------------------------

    def _load_server(self):
        """Arm the Server tab, then fetch its rows in the background."""
        # Nothing to create or delete here: the header buttons go.
        self.btn_add.setVisible(False)
        self.btn_delete_selected.setVisible(False)
        self._delete_selected_callback = None
        self._name_click_callback = self._show_server_section
        self._extra_click_callbacks = {}
        self._row_actions = [
            (
                "preview-browser",
                translate("ServerTabMixin", "Open in the web interface"),
                self._open_server_page,
            ),
        ]
        self._setup_table(
            [
                translate("ServerTabMixin", "Name"),
                translate("ServerTabMixin", "Summary"),
                self.actions_column_label(),
            ]
        )
        self._path_columns = ()  # fixed rows, no names in the paths
        self._start_load(
            translate("ServerTabMixin", "Failed to load the server settings"),
            self._fetch_server_rows,
        )

    def _fetch_server_rows(self, task=None):
        """(rows, failures) for the table. Runs in a worker thread."""
        sections = self._server_sections()
        reads = [kind for kind, _label in sections if kind != "catalog"]
        results = dict(zip(reads, self._fan_out(self._server_read, reads, task)))
        rows, failures = [], []
        for kind, label in sections:
            if kind == "catalog":
                summary = translate(
                    "ServerTabMixin", "Reload the configuration, or reset the caches"
                )
            else:
                payload, error = results[kind]
                if error:
                    failures.append((label, error))
                    summary = "-"
                else:
                    summary = self._server_summary(kind, payload)
            rows.append([label, summary])
        return rows, failures

    def _server_path(self, kind):
        """The REST path of one row's settings."""
        base = self.gs.rest_service.rest_endpoints.base_url
        if kind in SERVICES:
            return f"{base}/services/{kind}/settings.json"
        return {
            "contact": f"{base}/settings/contact.json",
            "global": f"{base}/settings.json",
            "logging": f"{base}/logging.json",
        }[kind]

    def _server_read(self, kind):
        """One row's settings, unwrapped. Raises on HTTP errors."""
        payload = self._raw_rest("get", self._server_path(kind)).json()
        if kind == "global":
            return payload.get("global", {}).get("settings", {})
        return payload.get(kind, {})

    @staticmethod
    def _server_summary(kind, settings):
        """The Summary cell of one row. Pure."""
        if kind == "contact":
            parts = [
                settings.get("contactPerson"),
                settings.get("contactOrganization"),
            ]
            return ", ".join(part for part in parts if part) or "-"
        if kind == "global":
            proxy = settings.get("proxyBaseUrl")
            return (
                translate("ServerTabMixin", "Proxy base URL: {}").format(proxy)
                if proxy
                else translate("ServerTabMixin", "No proxy base URL")
            )
        if kind == "logging":
            return settings.get("level") or "-"
        state = (
            translate("ServerTabMixin", "On")
            if settings.get("enabled", True)
            else translate("ServerTabMixin", "Off")
        )
        return f"{state}: {settings.get('title') or '-'}"

    # -- Open -----------------------------------------------------------------

    def _server_url(self):
        """The server's own URL, as the connection has it."""
        return self.plg_settings.get_plg_settings().geoserver_url.rstrip("/")

    def _capabilities_url(self, service):
        """The service's GetCapabilities URL, as a client would ask for it."""
        if service == "wmts":
            return f"{self._server_url()}/gwc/service/wmts?REQUEST=GetCapabilities"
        return (
            f"{self._server_url()}/ows?service={service.upper()}"
            "&request=GetCapabilities"
        )

    def _open_server_page(self, row_data):
        """Open the web admin's page for the row."""
        page = _WEB_PAGES[self._server_kind(row_data)]
        self._open_in_browser(f"{self._server_url()}/web/wicket/bookmarkable/{page}")

    def _show_server_section(self, row_data):
        """Open one row's form."""
        kind = self._server_kind(row_data)
        if kind == "catalog":
            self._reload_or_reset_catalog()
            return
        settings = self._fetch(
            lambda: self._server_read(kind),
            translate("ServerTabMixin", "Failed to load '{}'").format(row_data[0]),
        )
        if settings is None:
            return
        before = self._server_form_values(kind, settings)
        if kind in SERVICES:
            before["capabilities"] = self._capabilities_url(kind)
        dlg = ResourceFormDialog(
            title=row_data[0],
            description=self._server_description(kind),
            fields=self._server_fields(kind, before),
            values=before,
            parent=self,
        )
        if kind == "logging":
            self._add_log_button(dlg)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        after = dlg.get_values()
        saved = []
        # A write: a Cancel on the waiting box says the PUT may still land,
        # and the tab reloads once it answers. _fetch is for reads.
        if (
            self._run_action(
                lambda: saved.append(
                    self._wait_for_save(
                        lambda: self._save_server_section(kind, before, after)
                    )
                ),
                translate("ServerTabMixin", "Failed to save '{}'").format(row_data[0]),
            )
            and saved[0]
        ):
            self.show_success_message(
                translate("ServerTabMixin", "'{}' saved.").format(row_data[0])
            )
            self._load_server()

    def _server_description(self, kind):
        if kind in SERVICES:
            return translate(
                "ServerTabMixin",
                "The server-wide service. A workspace can override it with "
                "settings of its own.",
            )
        if kind == "logging":
            return translate(
                "ServerTabMixin",
                "How much GeoServer writes to its log, and where.",
            )
        return None

    # -- Forms ----------------------------------------------------------------

    @staticmethod
    def _server_form_values(kind, settings):
        """Prefill for one row's form, from what GeoServer returned. Pure."""
        values = {key: settings.get(rest_key) for key, rest_key in _keys_of(kind)}
        if kind in SERVICES:
            values["keywords"] = keyword_list(settings.get("keywords"))
            values["enabled"] = settings.get("enabled", True) is not False
        for key, value in values.items():
            if value is None:
                values[key] = ""
            elif isinstance(value, str):
                # As the form gives it back, or an untouched Save is an edit.
                values[key] = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if kind == "global":
            # 8 only when unset: "or 8" also turned a stored 0 into 8.
            decimals = settings.get("numDecimals")
            values["num_decimals"] = 8 if decimals in (None, "") else int(decimals)
            for key in ("use_headers_proxy", "verbose", "verbose_exceptions"):
                values[key] = bool(values[key])
        if kind == "logging":
            values["stdout"] = bool(settings.get("stdOutLogging"))
        if kind == "wfs":
            values["max_features"] = int(settings.get("maxFeatures") or 0)
        return values

    def _server_fields(self, kind, current=None):
        """Field definitions for one row's form.

        :param current: the form's prefill. A logging profile GeoServer has
            beyond the built-in ones is offered too: the combo would otherwise
            select DEFAULT_LOGGING, and an untouched Save sent it.
        """

        def text(key, label, **extra):
            return {"key": key, "label": label, "type": "text", **extra}

        def check(key, label, **extra):
            return {"key": key, "label": label, "type": "checkbox", **extra}

        if kind == "contact":
            address = translate("ServerTabMixin", "Address")
            return [
                text("person", translate("ServerTabMixin", "Contact person")),
                text("position", translate("ServerTabMixin", "Position")),
                text("organization", translate("ServerTabMixin", "Organization")),
                text("email", translate("ServerTabMixin", "Email")),
                text("phone", translate("ServerTabMixin", "Phone")),
                text("online_resource", translate("ServerTabMixin", "Web site")),
                {
                    "key": "welcome",
                    "label": translate("ServerTabMixin", "Welcome message"),
                    "type": "textarea",
                    "help": translate(
                        "ServerTabMixin", "Shown on GeoServer's home page"
                    ),
                },
                text("address", translate("ServerTabMixin", "Street"), group=address),
                text("city", translate("ServerTabMixin", "City"), group=address),
                text(
                    "state",
                    translate("ServerTabMixin", "State or province"),
                    group=address,
                ),
                text(
                    "postal_code",
                    translate("ServerTabMixin", "Postal code"),
                    group=address,
                ),
                text("country", translate("ServerTabMixin", "Country"), group=address),
            ]
        if kind == "global":
            return [
                text(
                    "proxy_base_url",
                    translate("ServerTabMixin", "Proxy base URL"),
                    placeholder="https://maps.example.org/geoserver",
                    help=translate(
                        "ServerTabMixin",
                        "The public address GeoServer writes into capabilities "
                        "documents, when it sits behind a proxy",
                    ),
                ),
                check(
                    "use_headers_proxy",
                    translate("ServerTabMixin", "Use headers for the proxy URL"),
                    help=translate(
                        "ServerTabMixin",
                        "Build it from the request's X-Forwarded headers instead",
                    ),
                ),
                text("charset", translate("ServerTabMixin", "Character set")),
                {
                    "key": "num_decimals",
                    "label": translate("ServerTabMixin", "Decimals"),
                    "type": "spinbox",
                    "min": 0,
                    "max": 20,
                    "help": translate(
                        "ServerTabMixin", "In GML and GeoJSON coordinates"
                    ),
                },
                check("verbose", translate("ServerTabMixin", "Verbose output")),
                check(
                    "verbose_exceptions",
                    translate("ServerTabMixin", "Verbose exceptions"),
                    help=translate(
                        "ServerTabMixin",
                        "Java stack traces in service errors: for debugging only",
                    ),
                ),
            ]
        if kind == "logging":
            return [
                {
                    "key": "level",
                    "label": translate("ServerTabMixin", "Profile"),
                    "type": "combo",
                    "options": list(LOG_LEVELS)
                    + [
                        level
                        for level in [(current or {}).get("level")]
                        if level and level not in LOG_LEVELS
                    ],
                },
                text(
                    "location",
                    translate("ServerTabMixin", "Log file"),
                    help=translate("ServerTabMixin", "Relative to the data directory"),
                ),
                check(
                    "stdout", translate("ServerTabMixin", "Also log to standard output")
                ),
            ]
        fields = [
            check("enabled", translate("ServerTabMixin", "Enabled")),
            text(
                "capabilities",
                translate("ServerTabMixin", "Capabilities URL"),
                read_only=True,
                help=translate(
                    "ServerTabMixin", "What a client such as QGIS connects to"
                ),
            ),
            text("title", translate("ServerTabMixin", "Title")),
            {
                "key": "abstract",
                "label": translate("ServerTabMixin", "Abstract"),
                "type": "textarea",
            },
            {
                "key": "keywords",
                "label": translate("ServerTabMixin", "Keywords"),
                "type": "list",
            },
        ]
        if kind == "wfs":
            fields.append(
                {
                    "key": "max_features",
                    "label": translate("ServerTabMixin", "Maximum features"),
                    "type": "spinbox",
                    "min": 0,
                    "max": 2147483647,
                    "help": translate("ServerTabMixin", "Per GetFeature request"),
                }
            )
        contact = translate("ServerTabMixin", "Contact")
        fields += [
            text(
                "maintainer", translate("ServerTabMixin", "Maintainer"), group=contact
            ),
            text(
                "online_resource",
                translate("ServerTabMixin", "Web site"),
                group=contact,
            ),
            text("fees", translate("ServerTabMixin", "Fees"), group=contact),
            text(
                "access_constraints",
                translate("ServerTabMixin", "Access constraints"),
                group=contact,
            ),
        ]
        return fields

    # -- Save -----------------------------------------------------------------

    def _save_server_section(self, kind, before, after):
        """PUT what changed. False when nothing did. Runs in a worker."""
        if kind in SERVICES:
            body = changed(before, after, _keys_of(kind))
            if words(after.get("keywords")) != words(before.get("keywords")):
                body["keywords"] = {"string": words(after.get("keywords"))}
            if not body:
                return False
            # A service merges a partial PUT (measured on 2.28.5).
            self._raw_rest("put", self._server_path(kind), json={kind: body})
            return True

        edits = changed(before, after, _keys_of(kind))
        if not edits:
            return False
        if kind == "global" and edits.get("proxyBaseUrl") == "":
            edits["proxyBaseUrl"] = None  # unset, not an empty URL
        # These three REPLACE the stored object: read it again, merge, send
        # it whole, so nothing the form does not show is lost.
        path = self._server_path(kind)
        current = self._raw_rest("get", path).json()
        if kind == "global":
            current.setdefault("global", {}).setdefault("settings", {}).update(edits)
        else:
            current.setdefault(kind, {}).update(edits)
        self._raw_rest("put", path, json=current)
        return True

    # -- Log ------------------------------------------------------------------

    def _add_log_button(self, dlg):
        """A Show the log button on the logging form."""
        button = dlg.add_button(translate("ServerTabMixin", "Show the log"))
        button.clicked.connect(
            lambda: self._show_server_log(dlg.get_values().get("location"), dlg)
        )

    def _show_server_log(self, location, form):
        """The end of GeoServer's log file, read in the background.

        `form` is the modal logging form: a refusal, a failure or a log with
        nothing in it is said in a box over it, since the message bar sits
        behind it until it closes.
        """
        location = (location or "logs/geoserver.log").strip()
        if location.startswith("/") or ":" in location.split("/")[0]:
            # The REST resource API only reaches the data directory.
            QMessageBox.warning(
                form,
                form.windowTitle(),
                translate(
                    "ServerTabMixin",
                    "GeoServer writes the log to {}, outside its data directory, "
                    "where the REST API cannot read it. Open the file on the server.",
                ).format(location),
            )
            return
        client = self.gs.rest_service.rest_client
        path = "{}/resource/{}".format(
            self.gs.rest_service.rest_endpoints.base_url, location
        )
        # Cancel stops the download: the read went on to the end of a file
        # of hundreds of MB after the box was gone.
        stop = threading.Event()
        try:
            tail = self._wait_for(
                lambda: self._log_tail(client, path, stop=stop), stop=stop
            )
        except Abandoned:
            return  # the user pressed Cancel: they know
        except Exception as error:
            detail = self._error_text(error)
            if not detail.startswith("HTTP 404"):
                problem = "{}: {}".format(
                    translate("ServerTabMixin", "Failed to read the log"), detail
                )
                self.log(problem, log_level=Qgis.MessageLevel.Critical)
                QMessageBox.warning(form, form.windowTitle(), problem)
                return
            tail = ""  # no such file: "Undefined resource path." (measured)
        if not tail.strip():
            QMessageBox.information(
                form,
                form.windowTitle(),
                translate(
                    "ServerTabMixin",
                    "GeoServer has written nothing to {}. A server that logs only to "
                    "its standard output, as GeoServer Cloud does, keeps no log file: "
                    "read that output where the deployment collects it.",
                ).format(location),
            )
            return
        dlg = ResourceFormDialog(
            title=translate("ServerTabMixin", "GeoServer Log"),
            description=translate(
                "ServerTabMixin", "The last {} lines, newest at the bottom."
            ).format(_LOG_TAIL_LINES),
            fields=[
                {
                    "key": "log",
                    "label": translate("ServerTabMixin", "Log"),
                    "type": "textarea",
                    "read_only": True,
                    "code": True,
                    "wide": True,
                    "min_height": 420,
                    "max_height": None,  # grow with the dialog
                }
            ],
            values={"log": tail},
            parent=self,
        )
        dlg.hide_save_button()
        log = dlg.get_widget("log")
        log.verticalScrollBar().setValue(log.verticalScrollBar().maximum())
        dlg.exec()

    @staticmethod
    def _log_tail(client, path, keep=_LOG_TAIL_BYTES, lines=_LOG_TAIL_LINES, stop=None):
        """The last lines of a server file, streamed. Runs in a worker.

        TODO(#1): the library's client reads a body whole (row 60), and
        GeoServer offers no Range, so this streams the file itself and keeps
        only its end in memory: a production log can be hundreds of MB.
        `stop`, a threading.Event, ends the download between two chunks.
        """
        with requests.get(
            f"{client.url}{path}",
            auth=client.auth,
            verify=client.verifytls,
            stream=True,
            timeout=30,
        ) as response:
            if response.status_code >= 400:
                raise RuntimeError(
                    f"HTTP {response.status_code}: {summarise_body(response.text)}"
                )
            chunks, size = deque(), 0
            for chunk in response.iter_content(64 * 1024):
                if stop is not None and stop.is_set():
                    raise Abandoned()  # leaving the block closes the connection
                chunks.append(chunk)
                size += len(chunk)
                while size - len(chunks[0]) >= keep:
                    size -= len(chunks.popleft())
        text = b"".join(chunks).decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])

    # -- Catalog --------------------------------------------------------------

    def _reload_or_reset_catalog(self):
        """Reload the catalog from disk, or reset its caches, after asking."""
        reload_label = translate("ServerTabMixin", "Reload")
        reset_label = translate("ServerTabMixin", "Reset")
        dlg = ResourceFormDialog(
            title=translate("ServerTabMixin", "Catalog"),
            description=translate(
                "ServerTabMixin",
                "Reload reads the whole configuration from the data directory again, "
                "for changes made outside GeoServer; a large catalog takes a while. "
                "Reset drops the caches of stores, feature types and styles, so "
                "GeoServer reads them again.",
            ),
            fields=[
                {
                    "key": "action",
                    "label": translate("ServerTabMixin", "Do"),
                    "type": "combo",
                    "options": [(reload_label, "reload"), (reset_label, "reset")],
                }
            ],
            parent=self,
            ok_label=translate("ServerTabMixin", "Run"),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        reload = dlg.get_values()["action"] == "reload"
        path = "{}/{}".format(
            self.gs.rest_service.rest_endpoints.base_url,
            "reload" if reload else "reset",
        )

        # A write in a worker: a reload of a large catalog takes a while, and
        # a Cancel on the waiting box does not stop it, so _run_action says so.
        if self._run_action(
            # TODO(#1): no reload or reset in the library (row 60).
            lambda: self._wait_for_save(lambda: self._raw_rest("post", path)),
            (
                translate("ServerTabMixin", "Failed to reload the catalog")
                if reload
                else translate("ServerTabMixin", "Failed to reset the caches")
            ),
        ):
            self.show_success_message(
                translate("ServerTabMixin", "Catalog reloaded.")
                if reload
                else translate("ServerTabMixin", "Caches reset.")
            )
