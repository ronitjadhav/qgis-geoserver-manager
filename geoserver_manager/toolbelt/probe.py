#! python3  # noqa: E265

"""
One bounded request that says whether a GeoServer REST API answers here.

Used by the main dialog before its first table, and by the Settings page's
*Test connection* button, with the fields as typed, saved or not.

TODO(#1): one of the two requests in the plugin that do not go through the
library (the other streams the server log, see tab_server._log_tail).
`RestClient` hardcodes `timeout=TIMEOUT` (120 s) and takes no
timeout argument, and this is the request the user waits for, so it uses
`requests` directly with PROBE_TIMEOUT: a dead host must cost 10 s, not two
minutes.

Also `use_qgis_proxy`, which hands QGIS's proxy to every `requests` call the
plugin makes, the library's included, and `forget_qgis_proxy`, which takes it
back at unload.
"""

import os
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import requests
from qgis.core import Qgis, QgsNetworkAccessManager
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtNetwork import QNetworkProxy
from requests.exceptions import ProxyError, SSLError

from geoserver_manager.toolbelt.log_handler import PlgLogger

PROBE_TIMEOUT = 10
ENDPOINT = "/rest/workspaces.json"
# What GeoServer serves below its base URL: a pasted address often ends in one.
_BELOW_BASE = {"web", "rest", "ows", "wms", "wfs", "wcs", "wmts", "gwc"}
# The variables use_qgis_proxy wrote, to tell them from the user's own.
_exported = {}

translate = QCoreApplication.translate


def qgis_proxy(url):
    """{scheme: proxy URL} for url's REST calls, from QGIS's Network options.

    Empty when QGIS would send them through no HTTP proxy of its own: none
    is set, url is excluded, or QGIS uses the system's, which `requests`
    reads itself. A caching proxy carries plain HTTP only, as in Qt.
    """
    if not url:
        return {}
    manager = QgsNetworkAccessManager.instance()
    proxy = manager.fallbackProxy()
    schemes = {
        QNetworkProxy.ProxyType.HttpProxy: ("http", "https"),
        QNetworkProxy.ProxyType.HttpCachingProxy: ("http",),
    }.get(proxy.type(), ())
    rest = f"{url.rstrip('/')}/rest/"
    excluded = manager.excludeList() + manager.noProxyList()
    if (
        not schemes
        or not proxy.hostName()
        or manager.useSystemProxy()
        or any(p.strip() and rest.startswith(p.strip()) for p in excluded)
    ):
        return {}
    login = ""
    if proxy.user():
        login = quote(proxy.user(), safe="")
        if proxy.password():
            login += ":" + quote(proxy.password(), safe="")
        login += "@"
    port = f":{proxy.port()}" if proxy.port() else ""
    return {scheme: f"http://{login}{proxy.hostName()}{port}" for scheme in schemes}


def proxies_for(url):
    """What use_qgis_proxy(url) would put in effect, as requests' proxies.

    For a request that must not write the environment, which the dialog's
    running requests read (Test connection). None cancels a variable written
    here. A scheme is left out where `requests` would read the user's own
    variable, or the system's proxy (Windows, macOS), as the client does.
    """
    wanted = qgis_proxy(url)
    proxies = {}
    for scheme in ("http", "https"):
        name = f"{scheme.upper()}_PROXY"
        current = os.environ.get(name) or os.environ.get(name.lower())
        if current and current != _exported.get(name):
            continue
        if wanted.get(scheme) or name in _exported:
            proxies[scheme] = wanted.get(scheme)
    return proxies


def use_qgis_proxy(url):
    """Send the plugin's requests to url through QGIS's proxy. GUI thread.

    TODO(#1): `RestClient` makes its own `requests` calls and takes no
    proxies, so the proxy reaches it, the probe and the log tail through the
    HTTP_PROXY and HTTPS_PROXY variables `requests` reads on every call. One
    the user set outside QGIS is left alone; one written here goes when
    QGIS's proxy no longer covers url, and at unload.
    """
    for scheme, proxy in proxies_for(url).items():
        name = f"{scheme.upper()}_PROXY"
        if proxy:
            os.environ[name] = _exported[name] = proxy
        else:
            os.environ.pop(name, None)
            del _exported[name]


def forget_qgis_proxy():
    """Take back what use_qgis_proxy wrote, unless it changed since. For unload().

    Left behind, the proxy and its password stay in QGIS's environment for
    other plugins and child processes. A reload also starts _exported afresh,
    so the new module would take them for the user's own and never update them.
    """
    for name, value in _exported.items():
        if os.environ.get(name) == value:
            del os.environ[name]
    _exported.clear()


def _get(url, auth, verify_tls, proxies):
    # Not followed: a 301 or 302 resends a write as a GET, or bodiless.
    return requests.get(
        f"{url.rstrip('/')}{ENDPOINT}",
        auth=auth,
        timeout=PROBE_TIMEOUT,
        verify=verify_tls,
        proxies=proxies,
        allow_redirects=False,
    )


def _lists_workspaces(response):
    if response.status_code >= 300:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and "workspaces" in payload


def _base_url(url, auth, verify_tls, proxies):
    """GeoServer's base URL when url is another address of it, else None.

    A pasted address often ends in a page below the base URL (the web
    interface's …/geoserver/web/?0, a REST or OWS URL) or lacks the usual
    /geoserver. The guess counts only where the REST API answers: a
    workspace's own …/geoserver/topp/ows would give a wrong one.
    """
    parts = urlsplit(url)
    segments = parts.path.rstrip("/").split("/")
    cut = next((i for i, s in enumerate(segments) if s.lower() in _BELOW_BASE), None)
    if cut is not None:
        path = "/".join(segments[:cut])
    elif segments == [""]:
        path = "/geoserver"
    elif parts.query or parts.fragment:
        path = "/".join(segments)
    else:
        return None
    guess = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    try:
        response = _get(guess, auth, verify_tls, proxies)
    except OSError:
        return None
    # A 401 or a 403 is the REST API asking for another account.
    if response.status_code in (401, 403) or _lists_workspaces(response):
        return guess
    return None


def _log_failure(url, error):
    """What requests said: the status line alone does not tell which failure."""
    PlgLogger.log(
        f"Connection check of {url}: {error}", log_level=Qgis.MessageLevel.Warning
    )


def _proxy_refused(url):
    return (
        translate("ConnectionProbe", "Proxy refused"),
        translate(
            "ConnectionProbe",
            "The proxy did not pass the request on to {url}: it is unreachable, "
            "cannot reach the server, or wants a user name and password (HTTP "
            "407). Check the proxy in QGIS's Options, on the Network tab.",
        ).format(url=url),
    )


def probe(url, auth, verify_tls, proxies=None):
    """Return None when GeoServer answered, else (status, message) to show.

    :param auth: (username, password), HTTP Basic, as the library sends it.
    :param proxies: requests' own argument, see proxies_for; by default the
        environment's, which use_qgis_proxy set for the saved connection.
    """
    endpoint = f"{url.rstrip('/')}{ENDPOINT}"
    try:
        response = _get(url, auth, verify_tls, proxies)
    except ProxyError as e:
        # Before OSError too: the proxy failed, not GeoServer.
        _log_failure(url, e)
        return _proxy_refused(url)
    except SSLError as e:
        # Before OSError (it is a ConnectionError): a private-CA or
        # self-signed certificate used to read as "is the server running?"
        _log_failure(url, e)
        return (
            translate("ConnectionProbe", "Certificate not trusted"),
            translate(
                "ConnectionProbe",
                "{url} presented a TLS certificate this machine does not trust. "
                "The plugin does not read QGIS's certificate store. For a private "
                "CA, set the REQUESTS_CA_BUNDLE environment variable (QGIS's "
                "Options, System tab) to a file holding that CA and the usual "
                "ones, then restart QGIS. Untick \"Verify the server's TLS "
                'certificate" in Settings only as a last resort.',
            ).format(url=url),
        )
    except OSError as e:
        # ConnectionError / Timeout: refused, unreachable, wrong host, or a
        # host that swallows the SYN; that one gives up after PROBE_TIMEOUT.
        _log_failure(url, e)
        return (
            translate("ConnectionProbe", "Server unreachable"),
            translate(
                "ConnectionProbe",
                "Cannot reach GeoServer at {url}. Is the server running?",
            ).format(url=url),
        )
    except Exception as e:
        return (
            translate("ConnectionProbe", "Connection error"),
            translate("ConnectionProbe", "Connection failed: {}").format(e),
        )

    code = response.status_code
    if code == 407:
        # Over plain HTTP the proxy answers itself; over https it is a ProxyError.
        return _proxy_refused(url)
    if code == 401:
        return (
            translate("ConnectionProbe", "Authentication failed"),
            translate(
                "ConnectionProbe",
                "Authentication failed. Check your username and password in Settings.",
            ),
        )
    if code == 403:
        # GeoServer answers a wrong password with a 401, never a 403.
        return (
            translate("ConnectionProbe", "Not allowed"),
            translate(
                "ConnectionProbe",
                "This account may not use GeoServer's REST API (HTTP 403). The "
                "user name and password are probably right: a wrong one gets a "
                "401. Ask a GeoServer administrator to grant it REST access "
                "(rest.properties), or to check the proxy in front of GeoServer.",
            ),
        )
    if _lists_workspaces(response):
        return None
    base = _base_url(url, auth, verify_tls, proxies)
    if base:
        return (
            translate("ConnectionProbe", "Not the base URL"),
            translate(
                "ConnectionProbe",
                "{url} is not GeoServer's base URL: its REST API answers at "
                "{base}. Put that address in Settings.",
            ).format(url=url, base=base),
        )
    if 300 <= code < 400 and response.headers.get("Location"):
        target = urljoin(endpoint, response.headers["Location"])
        if target.endswith(ENDPOINT):
            # The same API at another address: the usual http:// to https://.
            return (
                translate("ConnectionProbe", "Redirected"),
                translate(
                    "ConnectionProbe",
                    "{url} redirects to {target}. Put that address in Settings: "
                    "a save sent through a redirect can arrive empty, or as a read.",
                ).format(url=url, target=target[: -len(ENDPOINT)]),
            )
        return (
            translate("ConnectionProbe", "Not a GeoServer REST endpoint"),
            translate(
                "ConnectionProbe",
                "{url} redirects to {target}, not to the GeoServer REST API (a "
                "login page?). Check the URL, or the proxy in front of it.",
            ).format(url=url, target=target),
        )
    if code >= 400:
        # The URL usually points at something that is not a GeoServer REST
        # endpoint at all.
        return (
            translate("ConnectionProbe", "HTTP error {}").format(code),
            translate(
                "ConnectionProbe",
                "GeoServer returned HTTP {code} for {url}. Check the URL in Settings.",
            ).format(code=code, url=url),
        )
    # An SSO / reverse-proxy login page answers 200 with HTML. Without
    # this check it showed a green "Connected" and empty tables.
    return (
        translate("ConnectionProbe", "Not a GeoServer REST endpoint"),
        translate(
            "ConnectionProbe",
            "{url} answered, but not with the GeoServer REST API (a login "
            "page?). Check the URL, or the proxy in front of it.",
        ).format(url=url),
    )
