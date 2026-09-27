import io
import os
import random
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from explorer.fetch import (
    DownloadError, HttpClient, RemoteZip, _parse_header_dump, download, sha256_bytes,
)


def make_zip():
    rng = random.Random(7)
    files = {
        "Charades_v1_480/AAA01.mp4": bytes(rng.getrandbits(8) for _ in range(50_000)),
        "Charades_v1_480/AAA02.mp4": b"hello world " * 2_000,  # compressible
        "Charades_v1_480/EMPTY.mp4": b"",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(zipfile.ZipInfo("Charades_v1_480/"), b"")
        for name, data in files.items():
            method = zipfile.ZIP_DEFLATED if name.endswith("AAA02.mp4") else zipfile.ZIP_STORED
            zf.writestr(name, data, compress_type=method)
    return buffer.getvalue(), files


class RemoteZipTest(unittest.TestCase):
    def setUp(self):
        self.blob, self.files = make_zip()
        self.calls = []

    def fetch(self, start, end):
        self.calls.append((start, end))
        return self.blob[start:end + 1]

    def test_reads_members_with_one_request_each(self):
        archive = RemoteZip(self.fetch, len(self.blob))
        infos = {i.filename: i for i in archive.infolist()}
        for name, data in self.files.items():
            self.calls.clear()
            self.assertEqual(archive.read(infos[name]), data, name)
            self.assertEqual(len(self.calls), 1, name)

    def test_corrupted_member_fails_crc(self):
        archive = RemoteZip(self.fetch, len(self.blob))
        info = next(i for i in archive.infolist() if i.filename.endswith("AAA01.mp4"))
        corrupt = bytearray(self.blob)
        corrupt[info.header_offset + 200] ^= 0xFF
        bad = RemoteZip(lambda s, e: bytes(corrupt[s:e + 1]), len(corrupt))
        with self.assertRaises(zipfile.BadZipFile):
            bad.read(info)


class _Handler(BaseHTTPRequestHandler):
    body = bytes(range(256)) * 4

    def do_GET(self):
        if self.path == "/ignores-range":
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.body)))
            self.end_headers()
            self.wfile.write(self.body)
            return
        rng = self.headers.get("Range")
        if rng:
            start, end = (int(x) for x in rng.split("=")[1].split("-"))
            chunk = self.body[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(self.body)}")
        else:
            chunk = self.body
            self.send_response(200)
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()

    def log_message(self, *args):
        pass


class HttpClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_range_and_length(self):
        client = HttpClient(timeout=5, retries=1)
        self.assertEqual(client.get(self.base + "/f", (10, 19)), _Handler.body[10:20])
        self.assertEqual(client.content_length(self.base + "/f"), len(_Handler.body))

    def test_refuses_server_that_ignores_range(self):
        # Protects against accidentally downloading a 16 GB archive.
        with self.assertRaises(DownloadError):
            HttpClient(timeout=5, retries=1).get(self.base + "/ignores-range", (0, 9))

    def test_download_verifies_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp, "out.bin")
            with self.assertRaisesRegex(DownloadError, "checksum mismatch"):
                download(HttpClient(timeout=5, retries=1), self.base + "/f", dest, "0" * 64)
            self.assertFalse(dest.exists())
            download(HttpClient(timeout=5, retries=1), self.base + "/f", dest, sha256_bytes(_Handler.body))
            self.assertEqual(dest.read_bytes(), _Handler.body)
            self.assertEqual(os.listdir(tmp), ["out.bin"])  # no .part left behind

    def test_parse_curl_header_dump_keeps_last_block(self):
        dump = "HTTP/1.1 301 Moved\r\nLocation: /x\r\n\r\nHTTP/2 206\r\ncontent-length: 10\r\n\r\n"
        self.assertEqual(_parse_header_dump(dump), (206, {"content-length": "10"}))


if __name__ == "__main__":
    unittest.main()
