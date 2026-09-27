import base64
import gzip
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from explorer import config
from explorer.charades import load_dataset
from explorer.index import build_index, mark_videos
from explorer.search import Store
from explorer.server import App, auth_from_env, make_server, parse_range

FIXTURES = Path(__file__).parent / "fixtures" / "charades_mini"
VIDEO_BYTES = bytes(range(256)) * 40  # 10,240 bytes of fake "video"


class ServerTestCase(unittest.TestCase):
    auth = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        db = cls.tmp / "index.db"
        build_index(load_dataset(FIXTURES), db)
        videos = cls.tmp / "videos"
        videos.mkdir()
        (videos / "AAA01.mp4").write_bytes(VIDEO_BYTES)
        mark_videos(db, ["AAA01"])
        app = App(Store(db), config.WEB_DIR, videos, auth=cls.auth, quiet=True)
        cls.server = make_server("127.0.0.1", 0, app)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp)

    def request(self, path, headers=None, method="GET"):
        req = urllib.request.Request(self.base + path, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()

    def get_json(self, path):
        status, _, body = self.request(path)
        return status, json.loads(body)


class ApiTest(ServerTestCase):
    def test_health(self):
        self.assertEqual(self.request("/healthz")[0], 200)

    def test_meta(self):
        status, meta = self.get_json("/api/meta")
        self.assertEqual(status, 200)
        self.assertEqual(meta["stats"]["clips"], 7)
        self.assertIn("sorts", meta)

    def test_search_with_filters(self):
        status, data = self.get_json("/api/clips?scene=Kitchen&verb=drink&sort=id")
        self.assertEqual(status, 200)
        self.assertEqual([c["id"] for c in data["items"]], ["AAA01", "AAA04", "BBB01"])
        self.assertIn("facets", data)

    def test_bad_param_is_400_with_message(self):
        status, data = self.get_json("/api/clips?min_quality=99")
        self.assertEqual(status, 400)
        self.assertIn("min_quality", data["error"])

    def test_clip_detail_and_404(self):
        status, clip = self.get_json("/api/clips/aaa02")
        self.assertEqual((status, clip["id"], clip["scene"]), (200, "AAA02", "Home Office / Study"))
        self.assertEqual(self.request("/api/clips/NOPE")[0], 404)
        self.assertEqual(self.request("/api/clips/..%2f..%2fetc")[0], 404)

    def test_export_csv(self):
        status, headers, body = self.request("/api/export.csv?split=test")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        lines = body.decode().strip().splitlines()
        self.assertTrue(lines[0].startswith("id,split,scene"))
        self.assertEqual(len(lines), 3)

    def test_export_json_ignores_paging(self):
        status, rows = self.get_json("/api/export.json?page_size=1&page=2")
        self.assertEqual((status, len(rows)), (200, 7))


class StaticTest(ServerTestCase):
    def test_index_and_security_headers(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Charades Label Explorer", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_kept_out_of_search_engines(self):
        status, headers, body = self.request("/robots.txt")
        self.assertEqual((status, body), (200, b"User-agent: *\nDisallow: /\n"))
        self.assertEqual(self.request("/")[1]["X-Robots-Tag"], "noindex, nofollow")

    def test_path_traversal_is_blocked(self):
        for path in ("/../explorer/config.py", "/%2e%2e/explorer/config.py", "/..%2fREADME.md"):
            self.assertEqual(self.request(path)[0], 404, path)

    def test_gzip_when_accepted(self):
        status, headers, body = self.request("/api/meta", {"Accept-Encoding": "gzip"})
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertEqual(json.loads(gzip.decompress(body))["stats"]["clips"], 7)

    def test_head_has_no_body(self):
        status, headers, body = self.request("/", method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertGreater(int(headers["Content-Length"]), 0)


class VideoTest(ServerTestCase):
    def test_full_file(self):
        status, headers, body = self.request("/videos/AAA01.mp4")
        self.assertEqual((status, body), (200, VIDEO_BYTES))
        self.assertEqual(headers["Accept-Ranges"], "bytes")

    def test_range_request(self):
        status, headers, body = self.request("/videos/AAA01.mp4", {"Range": "bytes=100-199"})
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Range"], f"bytes 100-199/{len(VIDEO_BYTES)}")
        self.assertEqual(body, VIDEO_BYTES[100:200])

    def test_open_ended_and_suffix_ranges(self):
        self.assertEqual(self.request("/videos/AAA01.mp4", {"Range": "bytes=10000-"})[2], VIDEO_BYTES[10000:])
        self.assertEqual(self.request("/videos/AAA01.mp4", {"Range": "bytes=-5"})[2], VIDEO_BYTES[-5:])

    def test_unsatisfiable_range(self):
        status, headers, _ = self.request("/videos/AAA01.mp4", {"Range": "bytes=999999-"})
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], f"bytes */{len(VIDEO_BYTES)}")

    def test_missing_video_is_404(self):
        self.assertEqual(self.request("/videos/AAA02.mp4")[0], 404)

    def test_parse_range(self):
        self.assertEqual(parse_range("bytes=0-0", 10), (0, 0))
        self.assertEqual(parse_range("bytes=5-100", 10), (5, 9))
        self.assertIsNone(parse_range("bytes=7-3", 10))
        self.assertIsNone(parse_range("items=0-1", 10))
        self.assertIsNone(parse_range("bytes=-0", 10))


class AuthTest(ServerTestCase):
    auth = "reviewer:s3cret"

    def basic(self, credentials):
        return {"Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()}

    def test_requires_credentials(self):
        status, headers, _ = self.request("/api/meta")
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers["WWW-Authenticate"])
        self.assertEqual(self.request("/api/meta", self.basic("reviewer:wrong"))[0], 401)
        self.assertEqual(self.request("/videos/AAA01.mp4")[0], 401)

    def test_accepts_valid_credentials(self):
        self.assertEqual(self.request("/api/meta", self.basic("reviewer:s3cret"))[0], 200)

    def test_health_check_stays_open(self):
        self.assertEqual(self.request("/healthz")[0], 200)

    def test_auth_from_env(self):
        self.assertIsNone(auth_from_env({}))
        self.assertEqual(auth_from_env({"EXPLORER_AUTH": "a:b"}), "a:b")
        self.assertEqual(auth_from_env({"EXPLORER_PASSWORD": "x+y/z="}), "reviewer:x+y/z=")
        self.assertEqual(auth_from_env({"EXPLORER_USER": "lab", "EXPLORER_PASSWORD": "p"}), "lab:p")
        with self.assertRaises(ValueError):
            auth_from_env({"EXPLORER_AUTH": "no-colon"})


if __name__ == "__main__":
    unittest.main()
