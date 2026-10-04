#! python3  # noqa E265

"""
The connection check and the transport under it: what the probe says for each
answer a server or a proxy gives, how redirects are refused, and how QGIS's own
proxy reaches every request of the plugin.

Usage from the repo root folder:

.. code-block:: bash

    QT_QPA_PLATFORM=offscreen python -m unittest tests.qgis.test_probe
"""

from unittest.mock import patch

from qgis.testing import start_app, unittest

from tests.qgis.sync_dialog import FakePrefs, SyncDialog

start_app()


class TestRedirects(unittest.TestCase):
    """A local server that sends /r301/..., /r302/... and /r307/... on to /gs/...

    Only a 307 or 308 resends a write whole. After a 301 `requests` resends a
    PUT without its body, after a 302 as a GET, and the server's 200 passed
    for a save that worked.
    """

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        received = cls.received = []

        class Handler(BaseHTTPRequestHandler):
            def answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                received.append((self.command, self.path, self.rfile.read(length)))
                first, _, rest = self.path[1:].partition("/")
                if first in ("r301", "r302", "r307", "sso"):
                    # "sso": a proxy that sends every request to its login page
                    self.send_response(302 if first == "sso" else int(first[1:]))
                    self.send_header(
                        "Location", "/login" if first == "sso" else f"/gs/{rest}"
                    )
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b'{"workspaces": ""}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_PUT = answer

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.received.clear()

    def upload(self, code):
        import io

        from geoservercloud.services.restclient import RestClient

        from geoserver_manager.toolbelt.rest import ProgressReader, raw_rest

        client = RestClient(f"{self.base}/r{code}", ("admin", "geoserver"))
        body = ProgressReader(io.BytesIO(b"0123456789"), 10)
        return raw_rest(client, "put", "/rest/file.gpkg", data=body)

    def test_the_probe_refuses_a_url_that_redirects_and_names_where_to(self):
        from geoserver_manager.toolbelt.probe import probe

        status, message = probe(f"{self.base}/r301", ("admin", "geoserver"), True)
        self.assertEqual(status, "Redirected")
        self.assertIn(f"{self.base}/gs.", message)  # the address to put instead
        self.assertEqual(len(self.received), 1)  # not followed

    def test_a_redirect_to_something_else_is_not_the_rest_api(self):
        from geoserver_manager.toolbelt.probe import probe

        status, message = probe(f"{self.base}/sso", ("admin", "geoserver"), True)
        self.assertEqual(status, "Not a GeoServer REST endpoint")
        self.assertIn(f"{self.base}/login", message)

    def test_a_307_resends_the_upload_whole(self):
        self.assertEqual(self.upload(307).status_code, 200)
        self.assertEqual(
            self.received[-1], ("PUT", "/gs/rest/file.gpkg", b"0123456789")
        )

    def test_a_301_or_302_is_refused_instead_of_reading_as_saved(self):
        for code, resent in ((301, ("PUT", b"")), (302, ("GET", b""))):
            with self.subTest(code=code):
                self.received.clear()
                with self.assertRaises(RuntimeError) as caught:
                    self.upload(code)
                # What reached the server: the upload without its body, or a read.
                method, path, body = self.received[-1]
                self.assertEqual((method, body), resent)
                self.assertIn(f"HTTP {code}", str(caught.exception))
                self.assertIn(f"{self.base}/gs/rest/file.gpkg", str(caught.exception))


def without_proxy_variables(test):
    """Nothing of this machine's own proxy variables, and all restored after."""
    import os

    from geoserver_manager.toolbelt import probe as probe_module

    environment = patch.dict(os.environ)
    environment.start()
    test.addCleanup(environment.stop)
    for scheme in ("http", "https", "all", "no"):
        for name in (f"{scheme}_proxy", f"{scheme.upper()}_PROXY"):
            os.environ.pop(name, None)
    exported = patch.object(probe_module, "_exported", {}, create=True)
    exported.start()
    test.addCleanup(exported.stop)


def set_qgis_proxy(test, port, user="", password="", excludes=()):
    """What QGIS's Options, Network tab, leaves behind: its fallback proxy."""
    from qgis.core import QgsNetworkAccessManager
    from qgis.PyQt.QtNetwork import QNetworkProxy

    manager = QgsNetworkAccessManager.instance()
    manager.setFallbackProxyAndExcludes(
        QNetworkProxy(
            QNetworkProxy.ProxyType.HttpProxy, "127.0.0.1", port, user, password
        ),
        list(excludes),
        [],
    )
    test.addCleanup(manager.setFallbackProxyAndExcludes, QNetworkProxy(), [], [])


def logged(work):
    """The plugin's log lines while work() runs, and its result.

    From its logger: on QGIS 4.0 the message log's messageReceived signal
    carries no message logged with notifyUser=False.
    """
    from geoserver_manager.toolbelt.log_handler import PlgLogger

    with patch.object(PlgLogger, "log") as log:
        result = work()
    return result, [call.args[0] for call in log.call_args_list]


class FakeGeoServerCase(unittest.TestCase):
    """A local GeoServer at /geoserver, also a forward proxy to gs.rv4.invalid.

    As measured on 2.28.5: a wrong password gets a 401 and a valid account
    without REST rights a 403, and the web interface sends the probe of
    .../web/?0 on to ./?0&0/rest/workspaces.json. No resolver knows the
    .invalid host, so a request for it lands only through the proxy.
    """

    TARGET = "http://gs.rv4.invalid/geoserver"

    @classmethod
    def setUpClass(cls):
        import base64
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        cls.seen = []  # (Proxy-Authorization, absolute URL) of each proxied GET
        cls.proxy_login = None  # "user:password" the proxy asks for, or None

        def basic(login):
            return "Basic " + base64.b64encode(login.encode()).decode()

        cls.basic = staticmethod(basic)

        class Handler(BaseHTTPRequestHandler):
            def reply(self, code, body=b"", headers=()):
                self.send_response(code)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_CONNECT(self):
                self.reply(407, headers=[("Proxy-Authenticate", "Basic")])

            def do_GET(self):
                path = self.path
                if path.startswith("http://"):
                    login = self.headers.get("Proxy-Authorization")
                    cls.seen.append((login, path))
                    if cls.proxy_login and login != basic(cls.proxy_login):
                        self.reply(407, headers=[("Proxy-Authenticate", "Basic")])
                        return
                    path = path[len("http://gs.rv4.invalid") :]
                path, _, query = path.partition("?")
                account = self.headers.get("Authorization")
                if path.startswith("/geoserver/rest/"):
                    if account == basic("norest:pw"):
                        self.reply(403, b"<html><title>Forbidden</title></html>")
                    elif account != basic("admin:geoserver"):
                        self.reply(401, headers=[("WWW-Authenticate", "Basic")])
                    elif path == "/geoserver/rest/workspaces.json":
                        self.reply(200, b'{"workspaces": ""}')
                    elif path == "/geoserver/rest/resource/logs/geoserver.log":
                        self.reply(200, b"first\nlast\n")
                    else:
                        self.reply(404, b"<html><title>Not Found</title></html>")
                elif "/ows/" in path:
                    self.reply(200, b"<ows:ExceptionReport/>")
                elif path.startswith("/geoserver/web") and query:
                    self.reply(302, headers=[("Location", f"./?0&{query}")])
                elif path == "/geoserver/":
                    self.reply(302, headers=[("Location", "/geoserver/index.html")])
                else:
                    self.reply(404, b"<html><title>Not Found</title></html>")

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.seen.clear()
        type(self).proxy_login = None
        without_proxy_variables(self)

    def check(self, url, auth=("admin", "geoserver")):
        from geoserver_manager.toolbelt.probe import probe

        return probe(url, auth, True)


class TestConnectionCheck(FakeGeoServerCase):
    # G4-1
    def test_a_403_is_not_called_a_wrong_password(self):
        base = f"{self.base}/geoserver"
        wrong = self.check(base, ("admin", "nope"))
        forbidden = self.check(base, ("norest", "pw"))
        self.assertEqual(wrong[0], "Authentication failed")
        self.assertIn("password", wrong[1])
        self.assertEqual(forbidden[0], "Not allowed")
        self.assertNotIn("Check your username and password", forbidden[1])
        self.assertIn("REST API (HTTP 403)", forbidden[1])

    # G3-6
    def test_a_pasted_page_address_names_the_base_url(self):
        base = f"{self.base}/geoserver"
        self.assertIsNone(self.check(base))
        pasted = ["/web", "/web/?0", "/web/wicket/bookmarkable/x?1", "/rest/", "/ows"]
        for address in [base + tail for tail in pasted + ["/#x"]] + [self.base]:
            with self.subTest(address=address):
                status, message = self.check(address)
                self.assertEqual(status, "Not the base URL")
                self.assertIn(f"answers at {base}. Put that address", message)
        # A wrong password too: the base URL first, then the password.
        self.assertEqual(
            self.check(base + "/web/", ("admin", "nope"))[0], "Not the base URL"
        )

    def test_a_guess_where_the_rest_api_does_not_answer_is_not_offered(self):
        # A workspace's own OWS address: .../geoserver/topp is no base URL.
        status, message = self.check(f"{self.base}/geoserver/topp/ows")
        self.assertEqual(status, "Not a GeoServer REST endpoint")
        self.assertNotIn("answers at", message)
        self.assertEqual(self.check(f"{self.base}/other")[0], "HTTP error 404")

    # G3-2
    def test_a_proxy_that_refuses_is_named_and_its_reason_logged(self):
        import os

        os.environ["HTTP_PROXY"] = os.environ["HTTPS_PROXY"] = self.base
        type(self).proxy_login = "proxyuser:secret"
        # http: the proxy answers 407 itself; https: it refuses the CONNECT.
        lines = []
        for url in (self.TARGET, "https://gs.rv4.invalid/geoserver"):
            with self.subTest(url=url):
                (status, message), lines = logged(lambda: self.check(url))
                self.assertEqual(status, "Proxy refused")
                self.assertIn("Network tab", message)
        self.assertIn("407", " ".join(lines))

    def test_an_unreachable_server_logs_what_requests_said(self):
        url = "http://127.0.0.1:1/geoserver"  # nothing listens on port 1
        (status, _message), lines = logged(lambda: self.check(url))
        self.assertEqual(status, "Server unreachable")
        self.assertTrue(
            any(f"{url}: " in line and "Max retries" in line for line in lines), lines
        )

    # G3-3
    def test_an_untrusted_certificate_names_requests_ca_bundle(self):
        import requests

        error = requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")
        with patch("requests.get", side_effect=error):
            (status, message), lines = logged(
                lambda: self.check("https://gs.example.org/geoserver")
            )
        self.assertEqual(status, "Certificate not trusted")
        self.assertIn("REQUESTS_CA_BUNDLE", message)
        self.assertIn("only as a last resort", message)
        self.assertNotIn("is the server running", message)
        self.assertIn("Verify the server", message)  # points at the setting
        self.assertTrue(any("CERTIFICATE_VERIFY_FAILED" in line for line in lines))


class TestQgisProxy(FakeGeoServerCase):
    """QGIS's proxy (Options, Network tab) reached no request of the plugin."""

    # G3-2
    def test_it_carries_the_probe_the_library_and_the_log_tail(self):
        from geoservercloud.services.restclient import RestClient

        set_qgis_proxy(self, self.port, "proxyuser", "p@ss w")
        type(self).proxy_login = "proxyuser:p@ss w"
        SyncDialog()._build_client(FakePrefs(self.TARGET).settings)
        self.assertIsNone(self.check(self.TARGET))
        # The library's own client, whatever another test left in sys.modules.
        client = RestClient(self.TARGET, ("admin", "geoserver"))
        self.assertEqual(client.get("/rest/workspaces.json").status_code, 200)
        log = "/rest/resource/logs/geoserver.log"
        self.assertEqual(SyncDialog._log_tail(client, log), "first\nlast")
        self.assertEqual(
            self.seen,
            [
                (self.basic("proxyuser:p@ss w"), f"{self.TARGET}{path}")
                for path in ("/rest/workspaces.json", "/rest/workspaces.json", log)
            ],
        )

    def test_a_proxy_set_outside_qgis_wins_and_ours_goes_with_qgis(self):
        import os

        set_qgis_proxy(self, self.port)
        os.environ["HTTPS_PROXY"] = "http://users-own.invalid:3128"
        dlg = SyncDialog()
        dlg._build_client(FakePrefs(self.TARGET).settings)
        self.assertEqual(os.environ["HTTP_PROXY"], f"http://127.0.0.1:{self.port}")
        self.assertEqual(os.environ["HTTPS_PROXY"], "http://users-own.invalid:3128")
        # A URL QGIS excludes: what was exported goes, the user's own stays.
        set_qgis_proxy(self, self.port, excludes=["http://gs.rv4.invalid"])
        dlg._build_client(FakePrefs(self.TARGET).settings)
        self.assertNotIn("HTTP_PROXY", os.environ)
        self.assertEqual(os.environ["HTTPS_PROXY"], "http://users-own.invalid:3128")

    def test_a_probe_goes_where_its_url_would_and_writes_nothing(self):
        # Test connection: the dialog's requests read the environment meanwhile.
        import os

        from geoserver_manager.toolbelt.probe import (
            probe,
            proxies_for,
            use_qgis_proxy,
        )

        set_qgis_proxy(self, 1)  # the saved server's proxy, which is gone
        use_qgis_proxy("http://saved.rv4.invalid/geoserver")
        set_qgis_proxy(self, self.port, excludes=[self.base])
        before = dict(os.environ)
        # Around the exported proxy (None cancels it), then through QGIS's.
        for url in (f"{self.base}/geoserver", self.TARGET):
            with self.subTest(url=url):
                answer = probe(url, ("admin", "geoserver"), True, proxies_for(url))
                self.assertIsNone(answer)
        proxied = [url for _login, url in self.seen]
        self.assertEqual(proxied, [f"{self.TARGET}/rest/workspaces.json"])
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(os.environ["HTTP_PROXY"], "http://127.0.0.1:1")

    def test_unload_takes_back_what_was_exported(self):
        # A disabled plugin left the password there, and a reload's fresh
        # module took the leftovers for the user's own, never to update them.
        import os

        from geoserver_manager.plugin_main import GeoServerManagerPlugin
        from tests.qgis.test_layer_tree import FakeIface

        before = dict(os.environ)
        set_qgis_proxy(self, self.port, "proxyuser", "secret")
        SyncDialog()._build_client(FakePrefs(self.TARGET).settings)
        self.assertIn(":secret@", os.environ["HTTP_PROXY"])
        plugin = GeoServerManagerPlugin(FakeIface())
        plugin.initGui()
        plugin.unload()
        self.assertEqual(dict(os.environ), before)

    def test_unload_keeps_a_variable_changed_since(self):
        import os

        from geoserver_manager.toolbelt.probe import forget_qgis_proxy

        set_qgis_proxy(self, self.port)
        SyncDialog()._build_client(FakePrefs(self.TARGET).settings)
        os.environ["HTTPS_PROXY"] = "http://users-own.invalid:3128"
        forget_qgis_proxy()
        self.assertNotIn("HTTP_PROXY", os.environ)
        self.assertEqual(os.environ["HTTPS_PROXY"], "http://users-own.invalid:3128")


if __name__ == "__main__":
    unittest.main()
