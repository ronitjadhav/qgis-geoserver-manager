#! python3  # noqa E265

"""
The streaming upload body and the raw REST call, on a Python without QGIS.

Usage from the repo root folder:

.. code-block:: bash

    python -m unittest tests.unit.test_rest
"""

import io
import unittest

from geoserver_manager.toolbelt.rest import ProgressReader, UploadCancelled, raw_rest


class TestProgressReader(unittest.TestCase):
    DATA = bytes(range(256)) * 40  # 10 240 bytes

    def reader(self, **kwargs):
        return ProgressReader(io.BytesIO(self.DATA), len(self.DATA), **kwargs)

    def test_it_hands_the_bytes_through_and_knows_its_length(self):
        reader = self.reader()
        self.assertEqual(len(reader), len(self.DATA))  # requests' Content-Length
        chunks = iter(lambda: reader.read(4096), b"")
        self.assertEqual(b"".join(chunks), self.DATA)
        self.assertEqual(reader.sent, len(self.DATA))

    def test_progress_is_whole_percents_reported_once_each_ending_at_100(self):
        seen = []
        reader = self.reader(on_progress=seen.append)
        while reader.read(1000):
            pass
        self.assertEqual(seen[-1], 100)
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(len(seen), len(set(seen)))  # 8 KB chunks, not 8 KB signals
        self.assertTrue(all(isinstance(value, int) for value in seen))

    def test_a_cancel_raises_on_the_next_read_and_sends_nothing_more(self):
        stop = []
        reader = self.reader(is_cancelled=lambda: bool(stop))
        reader.read(1024)
        stop.append(True)
        with self.assertRaises(UploadCancelled):
            reader.read(1024)
        self.assertEqual(reader.sent, 1024)

    def test_an_empty_body_is_complete_at_once(self):
        seen = []
        reader = ProgressReader(io.BytesIO(b""), 0, on_progress=seen.append)
        self.assertEqual(reader.read(), b"")
        self.assertEqual(seen, [100])


class FakeClient:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text
        self.calls = []

    def put(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return self

    get = put


class TestRawRest(unittest.TestCase):
    def test_it_returns_the_response_and_passes_everything_through(self):
        client = FakeClient(201)
        response = raw_rest(client, "put", "/x", data=b"1", headers={"a": "b"})
        self.assertIs(response, client)
        self.assertEqual(client.calls, [("/x", {"data": b"1", "headers": {"a": "b"}})])

    def test_an_http_error_raises_with_geoservers_own_body(self):
        with self.assertRaises(RuntimeError) as caught:
            raw_rest(FakeClient(500, "Unable to delete layer"), "put", "/x")
        self.assertEqual(str(caught.exception), "HTTP 500: Unable to delete layer")

    def test_an_accepted_status_is_an_answer_not_a_failure(self):
        # A 404 on a workspace's settings path means "none of its own". A GET:
        # the library raises on a PUT's 404 before raw_rest sees it.
        client = FakeClient(404, "No such settings")
        self.assertIs(raw_rest(client, "get", "/x", accept=(404,)), client)
        self.assertEqual(client.calls, [("/x", {})])  # accept is not sent on
        with self.assertRaises(RuntimeError):
            raw_rest(FakeClient(500), "get", "/x", accept=(404,))


if __name__ == "__main__":
    unittest.main()
