"""JSON API + static files + video streaming on the standard library.

Routes
  GET /                       web UI (web/index.html and its assets)
  GET /api/meta               dataset stats + filter vocabularies
  GET /api/clips?...          search/filter (see explorer/search.py for params)
  GET /api/clips/<id>         one clip with full annotations
  GET /api/export.csv?...     all matching clips as CSV (same params)
  GET /api/export.json?...    all matching clips as JSON
  GET /videos/<id>.mp4        sample video, with HTTP Range support for seeking
  GET /healthz                liveness probe (never behind auth)

Set EXPLORER_AUTH="user:password" to require HTTP Basic auth, e.g. when
deploying (the Charades license does not allow publicly re-hosting the data).
"""
from __future__ import annotations

import argparse
import base64
import errno
import gzip
import hmac
import io
import json
import mimetypes
import os
import re
import sys
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from . import __version__, config
from .export import write_csv
from .search import Query, QueryError, Store

CLIP_ID_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")
VIDEO_PATH_RE = re.compile(r"^/videos/([A-Za-z0-9]{1,16})\.mp4$")
EXPORT_LIMIT = 20000
CHUNK = 64 * 1024

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; "
        "script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    ),
}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".mp4": "video/mp4",
}
COMPRESSIBLE = ("text/", "application/json", "image/svg+xml")


class App:
    def __init__(self, store: Store, web_dir: Path, video_dir: Path,
                 auth: Optional[str] = None, quiet: bool = False):
        self.store = store
        self.web_dir = web_dir.resolve()
        self.video_dir = video_dir.resolve()
        self.auth = auth
        self.quiet = quiet
        self.meta_json = json.dumps(store.meta()).encode()


def parse_range(header: str, size: int) -> Optional[Tuple[int, int]]:
    """Parse a single ``bytes=`` range. Returns inclusive (start, end) or None if invalid."""
    match = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", header or "")
    if not match or size == 0:
        return None
    first, last = match.groups()
    if first == "" and last == "":
        return None
    if first == "":  # suffix range: last N bytes
        length = int(last)
        if length == 0:
            return None
        return max(0, size - length), size - 1
    start = int(first)
    end = size - 1 if last == "" else min(int(last), size - 1)
    if start >= size or end < start:
        return None
    return start, end


class Handler(BaseHTTPRequestHandler):
    server_version = f"CharadesExplorer/{__version__}"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    # -------------------------------------------------------------- dispatch

    def do_GET(self) -> None:
        self._dispatch(head=False)

    def do_HEAD(self) -> None:
        self._dispatch(head=True)

    def _dispatch(self, head: bool) -> None:
        started = time.time()
        self._head = head
        self._status = 0
        url = urlsplit(self.path)
        path = url.path
        try:
            if path == "/healthz":
                self._send_bytes(b"ok\n", "text/plain; charset=utf-8")
            elif not self._authorized():
                self._send_json({"error": "authentication required"}, HTTPStatus.UNAUTHORIZED,
                                {"WWW-Authenticate": 'Basic realm="Charades Label Explorer", charset="UTF-8"'})
            elif path == "/api/meta":
                self._send_bytes(self.app.meta_json, "application/json")
            elif path == "/api/clips":
                query = Query.from_params(parse_qs(url.query, keep_blank_values=True))
                self._send_json(self.app.store.search(query))
            elif path.startswith("/api/clips/"):
                self._clip(path[len("/api/clips/"):])
            elif path in ("/api/export.csv", "/api/export.json"):
                self._export(path.rsplit(".", 1)[1], url.query)
            elif path.startswith("/api/"):
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            elif VIDEO_PATH_RE.match(path):
                self._video(VIDEO_PATH_RE.match(path).group(1).upper())
            else:
                self._static(path)
        except QueryError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away (common when a <video> seeks)
        except Exception:  # never leak internals; log them instead
            traceback.print_exc()
            if not self._status:
                self._send_json({"error": "internal server error"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        finally:
            if not self.app.quiet:
                elapsed = (time.time() - started) * 1000
                sys.stderr.write(f"{self.command} {self.path} -> {self._status} ({elapsed:.0f} ms)\n")

    # ---------------------------------------------------------------- routes

    def _clip(self, clip_id: str) -> None:
        clip = self.app.store.get_clip(clip_id.upper()) if CLIP_ID_RE.match(clip_id) else None
        if clip is None:
            self._send_json({"error": f"no clip with id {clip_id!r}"}, HTTPStatus.NOT_FOUND)
        else:
            self._send_json(clip)

    def _export(self, fmt: str, raw_query: str) -> None:
        params = parse_qs(raw_query, keep_blank_values=True)
        params.pop("page", None)
        params.pop("page_size", None)
        query = Query.from_params(params)
        rows = []
        for i, row in enumerate(self.app.store.iter_matches(query)):
            if i >= EXPORT_LIMIT:
                break
            rows.append(row)
        headers = {"Content-Disposition": f'attachment; filename="charades-clips.{fmt}"'}
        if fmt == "csv":
            buffer = io.StringIO()
            write_csv(rows, buffer)
            self._send_bytes(buffer.getvalue().encode(), "text/csv; charset=utf-8", headers=headers)
        else:
            self._send_json(rows, headers=headers)

    def _video(self, clip_id: str) -> None:
        path = self.app.video_dir / f"{clip_id}.mp4"
        if not path.is_file():
            self._send_json({"error": "no sample video for this clip"}, HTTPStatus.NOT_FOUND)
            return
        size = path.stat().st_size
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
        range_header = self.headers.get("Range")
        if range_header:
            byte_range = parse_range(range_header, size)
            if byte_range is None:
                self._send_bytes(b"", "text/plain", HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                                 {**headers, "Content-Range": f"bytes */{size}"})
                return
            start, end = byte_range
            status = HTTPStatus.PARTIAL_CONTENT
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        else:
            start, end, status = 0, size - 1, HTTPStatus.OK

        length = end - start + 1
        self._start(status, "video/mp4", length, headers)
        if self._head:
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (self.app.web_dir / relative).resolve()
        inside = os.path.commonpath([str(target), str(self.app.web_dir)]) == str(self.app.web_dir)
        if not inside or not target.is_file():
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        content_type = CONTENT_TYPES.get(target.suffix) or (
            mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        )
        self._send_bytes(target.read_bytes(), content_type, headers={"Cache-Control": "no-cache"})

    # --------------------------------------------------------------- helpers

    def _authorized(self) -> bool:
        if not self.app.auth:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            supplied = base64.b64decode(header[6:], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(supplied.encode(), self.app.auth.encode())

    def _start(self, status: int, content_type: str, length: int,
               headers: Optional[Dict[str, str]] = None) -> None:
        self._status = int(status)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        for key, value in {**SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(key, value)
        self.end_headers()

    def _send_bytes(self, body: bytes, content_type: str, status: int = HTTPStatus.OK,
                    headers: Optional[Dict[str, str]] = None) -> None:
        headers = dict(headers or {})
        if (len(body) > 1024 and content_type.startswith(COMPRESSIBLE)
                and "gzip" in self.headers.get("Accept-Encoding", "")):
            body = gzip.compress(body, compresslevel=5)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        self._start(status, content_type, len(body), headers)
        if not self._head:
            self.wfile.write(body)

    def _send_json(self, payload, status: int = HTTPStatus.OK,
                   headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self._send_bytes(body, "application/json", status, headers)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # replaced by the one-line log in _dispatch


def make_server(host: str, port: int, app: App) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.app = app  # type: ignore[attr-defined]
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="run.sh serve", description="Start the web UI.")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=None, help="default: $PORT or 8000")
    parser.add_argument("--quiet", action="store_true", help="no request log")
    args = parser.parse_args(argv)

    try:
        store = Store(config.DB_PATH)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    auth = os.environ.get("EXPLORER_AUTH") or None
    if auth and ":" not in auth:
        print("error: EXPLORER_AUTH must look like user:password", file=sys.stderr)
        return 1
    app = App(store, config.WEB_DIR, config.VIDEO_DIR, auth=auth, quiet=args.quiet)

    explicit = args.port is not None or "PORT" in os.environ
    port = args.port if args.port is not None else int(os.environ.get("PORT", 8000))
    server = None
    for candidate in ([port] if explicit else range(port, port + 10)):
        try:
            server = make_server(args.host, candidate, app)
            break
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            print(f"port {candidate} is busy", file=sys.stderr)
    if server is None:
        print("error: no free port; set PORT=<number>", file=sys.stderr)
        return 1

    host, port = server.server_address[:2]
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    stats = store.meta()["stats"]
    print(f"\n  Charades Label Explorer  ->  http://{shown}:{port}\n")
    print(f"  {stats['clips']:,} clips · {stats['segments']:,} segments · "
          f"{stats['with_video']} sample videos · search: {'FTS5' if store.fts else 'substring'}"
          f"{' · basic auth ON' if auth else ''}")
    print("  Ctrl+C to stop\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0
