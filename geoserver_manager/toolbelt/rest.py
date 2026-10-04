#! python3  # noqa: E265

"""
What the dialog needs from the REST client that geoservercloud does not offer:
a raw call that raises with GeoServer's own explanation, and a request body
that reports its progress and stops when asked.

No QGIS import: the unit suite runs this on a plain Python.
"""

import html
import re


def summarise_body(text, limit=300):
    """One line of a response body, for a banner and a log line.

    GeoServer puts the reason in the body ("Unable to delete layer referenced
    by layer group …"), so the first line is kept, cut at `limit`. A Tomcat
    error page or a proxy's login page is markup that explains nothing: an
    HTML or XML body is reduced to its <title> when it has one, else to its
    text.
    """
    text = (text or "").strip()
    if not text:
        return ""
    if text.startswith("<"):
        # Tomcat's page carries GeoServer's reason as its "Message" line
        # ("Invalid style: … (line 1, column 18)"); the title only says 400.
        message = re.search(r"<b>Message</b>(.*?)</p>", text, re.I | re.S)
        if message and message.group(1).strip():
            text = " ".join(html.unescape(message.group(1)).split())
            return text[:limit]
        title = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
        text = title.group(1) if title else re.sub(r"<[^>]+>", " ", text)
        # Plain text, as the Message line: "&lt;/Rule&gt;" reached the log as is.
        text = " ".join(html.unescape(text).split())
    return text.splitlines()[0][:limit] if text else ""


def raw_rest(client, method, path, accept=(), **kwargs):
    """Call the library's REST client directly, for what it has no method for.

    Raises RuntimeError carrying GeoServer's response body on any HTTP error,
    so the message the user sees has the same shape as `_check`'s. `accept`
    lists the error statuses to return instead: a 404 that means "none of
    its own" (a workspace's service settings) is an answer, not a failure.
    `accept` can only name what the library lets through, a GET or DELETE
    404 and a POST 409: the library raises on any other error status itself.
    A write that a 301, 302 or 303 redirected raises too: `requests` resent
    it without its body, or as a GET. Every caller is a library gap: list it
    in issue #1 and mark it TODO(#1). Module-level so a worker thread can
    hold the client it was given instead of reading `dialog.gs`, which a
    Refresh clears mid-flight.
    """
    response = getattr(client, method)(path, **kwargs)
    if method != "get":
        # TODO(#1): the library's writes follow a redirect and cannot be told not to.
        for hop in getattr(response, "history", ()):
            if hop.status_code in (301, 302, 303):
                raise RuntimeError(
                    f"HTTP {hop.status_code}: redirected to {response.url}, where "
                    "it arrived without its body or as a read. Put the address "
                    "the server redirects to in Settings."
                )
    if response.status_code >= 400 and response.status_code not in accept:
        raise RuntimeError(
            f"HTTP {response.status_code}: {summarise_body(response.text)}"
        )
    return response


class PartlySaved(Exception):
    """A save whose first step happened and a later one failed.

    Its text says what was saved and what was not. _run_action shows it as a
    warning and reloads the tab: a plain "Failed to create" had hidden that
    the resource existed, and a retry then said "already exists".
    """


class Abandoned(Exception):
    """The user stopped waiting (the waiting box's Cancel).

    `write` is set when what they stopped waiting for was a save: it runs on
    in its thread, so the change may still land.
    """

    def __init__(self, write=False):
        super().__init__()
        self.write = write


class UploadCancelled(Exception):
    """Raised inside ProgressReader.read() when the caller asked to stop.

    `requests` lets it out of put() unchanged and urllib3 closes the socket on
    the way, so the transfer stops there instead of running to the end.
    """


class ProgressReader:
    """A file-like body for a streaming PUT that reports and can be stopped.

    `requests` streams anything with read(); __len__ is what gives the request
    its Content-Length. Each read() reports the whole percent sent so far
    through on_progress (QgsTask.setProgress is thread-safe, so the task's
    own method fits), and raises UploadCancelled when is_cancelled() says so.
    """

    def __init__(self, handle, total, on_progress=None, is_cancelled=None):
        self._handle = handle
        self._total = total
        self._on_progress = on_progress
        self._is_cancelled = is_cancelled
        self.sent = 0
        self._reported = None

    def __len__(self):
        return self._total

    def __iter__(self):
        # What makes `requests` note the body's start and rewind to it on a
        # 307 or 308: a body with read() alone was resent as nothing.
        return iter(lambda: self.read(8192), b"")

    def tell(self):
        return self.sent

    def seek(self, offset, whence=0):
        """Rewind, which is all `requests` needs: a 307 or 308 redirect makes
        it resend the body from the start. After a 301 or 302 it sends no
        body at all (or a GET), which raw_rest refuses."""
        if offset != 0 or whence != 0:
            raise OSError("an upload body can only be rewound to its start")
        self._handle.seek(0)
        self.sent = 0
        self._reported = None

    def read(self, size=-1):
        if self._is_cancelled is not None and self._is_cancelled():
            raise UploadCancelled()
        chunk = self._handle.read(size)
        self.sent += len(chunk)
        if self._on_progress is not None:
            percent = int(100 * self.sent / self._total) if self._total else 100
            if percent != self._reported:
                self._reported = percent
                self._on_progress(percent)
        return chunk
