"""Entry point behind ./setup.sh: fetch and verify the dataset, then index it."""
from __future__ import annotations

import argparse
import sys
import time
import zipfile
from pathlib import Path

from . import config
from .charades import load_dataset
from .fetch import DownloadError, HttpClient, download, sha256_file
from .index import build_index


def log(message: str) -> None:
    print(f"[setup] {message}", flush=True)


def fetch_annotations(client: HttpClient, force: bool = False) -> None:
    archive = config.DATA_DIR / "Charades.zip"
    if not force and archive.exists() and sha256_file(archive) == config.ANNOTATIONS_SHA256:
        log(f"annotations: using cached {archive.name} (checksum OK)")
    else:
        log(f"annotations: downloading {config.ANNOTATIONS_URL}")
        download(client, config.ANNOTATIONS_URL, archive, config.ANNOTATIONS_SHA256)
        log(f"annotations: {archive.stat().st_size / 1e6:.1f} MB, SHA-256 verified "
            f"(via {client.transport_name})")

    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    wanted = set(config.ANNOTATION_FILES)
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            # Only write known file names, never paths taken from the archive.
            name = Path(member.filename).name
            if name in wanted:
                (config.RAW_DIR / name).write_bytes(zf.read(member))
                wanted.discard(name)
    if wanted:
        raise DownloadError(f"archive is missing expected files: {sorted(wanted)}")
    log(f"annotations: extracted {len(config.ANNOTATION_FILES)} files to {config.RAW_DIR}")


def build(db_path: Path = config.DB_PATH) -> None:
    started = time.time()
    dataset = load_dataset(config.RAW_DIR)
    for warning in dataset.warnings:
        log(f"warning: {warning}")
    summary = build_index(dataset, db_path)
    search = "SQLite FTS5 (BM25 ranking)" if summary["fts"] == "1" else "substring fallback (no FTS5)"
    log(f"index: {summary['clips']} clips, {summary['segments']} action segments -> "
        f"{db_path.name} in {time.time() - started:.1f}s; search: {search}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="setup.sh", description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download even if cached")
    args = parser.parse_args(argv)

    started = time.time()
    client = HttpClient()
    try:
        fetch_annotations(client, force=args.force)
        build()
    except DownloadError as exc:
        log(f"ERROR: {exc}")
        log("Check your network connection and re-run ./setup.sh")
        return 1
    log(f"done in {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
