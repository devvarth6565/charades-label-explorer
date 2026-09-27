"""HTTP downloads that work on a bare Python 3 install, plus remote zip access.

Why not just ``urllib.request.urlopen``?  Python from python.org on macOS
ships without a usable CA store until the user runs "Install
Certificates.command", so plain HTTPS fails with CERTIFICATE_VERIFY_FAILED on
a fresh Mac.  Instead of asking reviewers to fix their machine, the client
tries transports in order and keeps the first one that works:

1. urllib with Python's default trust store
2. urllib with the OS CA bundle (``/etc/ssl/cert.pem`` on macOS, ...)
3. the system ``curl``, which uses the OS trust store

TLS verification is never switched off, and every payload is integrity
checked anyway (SHA-256 for the annotation archive, CRC-32 for each video
pulled out of the remote zip).
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import ssl
import struct
import subprocess
import tempfile
import time
import urllib.request
import zipfile
import zlib
from functools import partial
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

USER_AGENT = "charades-label-explorer/1.0 (+python-urllib)"

_CA_BUNDLES = (
    "/etc/ssl/cert.pem",  # macOS, Alpine
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL
)

Response = Tuple[int, Dict[str, str], bytes]
Transport = Callable[[str, str, Dict[str, str], Optional[int]], Response]


class DownloadError(RuntimeError):
    pass


class HttpClient:
    """GET/HEAD with optional byte ranges over the first transport that works."""

    def __init__(self, timeout: float = 60, retries: int = 3):
        self.timeout = timeout
        self.retries = retries
        self.transport_name: Optional[str] = None
        self._transport: Optional[Transport] = None

    def get(self, url: str, byte_range: Optional[Tuple[int, int]] = None) -> bytes:
        headers = {}
        max_bytes = None
        if byte_range is not None:
            start, end = byte_range
            headers["Range"] = f"bytes={start}-{end}"
            # A server that ignores Range would send the whole 16 GB archive;
            # refuse any response larger than what we asked for.
            max_bytes = end - start + 1
        status, _, body = self._request("GET", url, headers, max_bytes)
        if byte_range is not None and status != 206:
            raise DownloadError(f"server ignored the Range header (HTTP {status})")
        return body

    def content_length(self, url: str) -> int:
        _, headers, _ = self._request("HEAD", url, {}, None)
        if "content-length" not in headers:
            raise DownloadError(f"no Content-Length for {url}")
        return int(headers["content-length"])

    def _request(self, method, url, headers, max_bytes) -> Response:
        headers = {"User-Agent": USER_AGENT, **headers}
        if self._transport is None:
            errors = []
            for name, transport in self._transports():
                try:
                    response = transport(method, url, headers, max_bytes)
                except Exception as exc:  # try the next transport
                    errors.append(f"  - {name}: {exc}")
                    continue
                self._transport, self.transport_name = transport, name
                return response
            raise DownloadError(f"could not fetch {url}\n" + "\n".join(errors))

        for attempt in range(1, self.retries + 1):
            try:
                return self._transport(method, url, headers, max_bytes)
            except Exception as exc:
                if attempt == self.retries:
                    raise DownloadError(f"{url}: {exc}") from exc
                time.sleep(1.5 * attempt)
        raise AssertionError("unreachable")

    def _transports(self) -> Iterator[Tuple[str, Transport]]:
        yield "urllib (default CA store)", partial(self._via_urllib, None)
        for bundle in _CA_BUNDLES:
            if os.path.exists(bundle):
                context = ssl.create_default_context(cafile=bundle)
                yield f"urllib ({bundle})", partial(self._via_urllib, context)
        if shutil.which("curl"):
            yield "curl", self._via_curl

    def _via_urllib(self, context, method, url, headers, max_bytes) -> Response:
        request = urllib.request.Request(url, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=self.timeout, context=context) as resp:
            response_headers = {k.lower(): v for k, v in resp.headers.items()}
            length = int(response_headers.get("content-length") or 0)
            if max_bytes is not None and length > max_bytes:
                raise DownloadError(f"response is {length} bytes, expected <= {max_bytes}")
            body = b"" if method == "HEAD" else resp.read()
            return resp.status, response_headers, body

    def _via_curl(self, method, url, headers, max_bytes) -> Response:
        with tempfile.TemporaryDirectory() as tmp:
            head_path, body_path = Path(tmp, "head"), Path(tmp, "body")
            cmd = [
                "curl", "--silent", "--show-error", "--location", "--fail",
                "--max-time", str(int(self.timeout)),
                "--dump-header", str(head_path), "--output", str(body_path),
            ]
            if method == "HEAD":
                cmd.append("--head")
            if max_bytes is not None:
                cmd += ["--max-filesize", str(max_bytes)]
            for key, value in headers.items():
                cmd += ["--header", f"{key}: {value}"]
            cmd.append(url)
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise DownloadError(f"curl exited {proc.returncode}: {proc.stderr.strip()}")
            status, response_headers = _parse_header_dump(head_path.read_text("latin-1"))
            body = b"" if method == "HEAD" or not body_path.exists() else body_path.read_bytes()
            return status, response_headers, body


def _parse_header_dump(text: str) -> Tuple[int, Dict[str, str]]:
    """Parse ``curl --dump-header`` output, keeping the last block (after redirects)."""
    blocks = [b for b in text.replace("\r\n", "\n").split("\n\n") if b.strip()]
    lines = blocks[-1].strip().split("\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return status, headers


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(client: HttpClient, url: str, dest: Path, expected_sha256: str) -> None:
    """Download ``url`` to ``dest`` atomically, verifying its SHA-256."""
    data = client.get(url)
    actual = sha256_bytes(data)
    if actual != expected_sha256:
        raise DownloadError(
            f"checksum mismatch for {url}\n  expected {expected_sha256}\n  got      {actual}"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, dest)


# --------------------------------------------------------------------------
# Remote zip: list and extract members of a huge zip over HTTP range requests
# --------------------------------------------------------------------------

RangeFetcher = Callable[[int, int], bytes]  # inclusive (start, end) -> bytes


class _RangeFile(io.RawIOBase):
    """Seekable read-only file whose reads become range requests."""

    def __init__(self, fetch: RangeFetcher, size: int):
        self._fetch = fetch
        self._size = size
        self._pos = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def readinto(self, buffer) -> int:
        n = min(len(buffer), self._size - self._pos)
        if n <= 0:
            return 0
        data = self._fetch(self._pos, self._pos + n - 1)
        buffer[: len(data)] = data
        self._pos += len(data)
        return len(data)


class RemoteZip:
    """Read a zip's central directory and single members without the whole file.

    ``zipfile`` does the directory parsing (including zip64) over a
    range-backed file object.  Members are then fetched with one range
    request each, which also makes them safe to download in parallel.
    """

    _LOCAL_HEADER_SIZE = 30

    def __init__(self, fetch: RangeFetcher, size: int):
        self._fetch = fetch
        self._size = size
        raw = io.BufferedReader(_RangeFile(fetch, size), buffer_size=1 << 20)
        self._zip = zipfile.ZipFile(raw)

    @classmethod
    def from_url(cls, client: HttpClient, url: str) -> "RemoteZip":
        size = client.content_length(url)
        return cls(lambda start, end: client.get(url, (start, end)), size)

    def infolist(self) -> List[zipfile.ZipInfo]:
        return self._zip.infolist()

    def read(self, info: zipfile.ZipInfo) -> bytes:
        start = info.header_offset
        # The local header's "extra" field can differ in length from the one
        # in the central directory, so over-fetch a little and parse it.
        guess = self._LOCAL_HEADER_SIZE + len(info.filename.encode()) + 1024
        end = min(start + guess + info.compress_size, self._size) - 1
        blob = self._fetch(start, end)
        if blob[:4] != b"PK\x03\x04":
            raise zipfile.BadZipFile(f"bad local header for {info.filename}")
        name_len, extra_len = struct.unpack("<HH", blob[26:30])
        data_start = self._LOCAL_HEADER_SIZE + name_len + extra_len
        data_end = data_start + info.compress_size
        if len(blob) < data_end:
            blob += self._fetch(start + len(blob), start + data_end - 1)
        payload = blob[data_start:data_end]

        if info.compress_type == zipfile.ZIP_STORED:
            data = payload
        elif info.compress_type == zipfile.ZIP_DEFLATED:
            data = zlib.decompress(payload, -zlib.MAX_WBITS)
        else:
            raise zipfile.BadZipFile(f"unsupported compression {info.compress_type}")

        if len(data) != info.file_size or zlib.crc32(data) & 0xFFFFFFFF != info.CRC:
            raise zipfile.BadZipFile(f"CRC/size mismatch for {info.filename}")
        return data
