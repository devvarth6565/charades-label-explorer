"""Entry point behind ./setup.sh: fetch and verify the dataset, index it, and
pull a few sample videos so the UI can play clips next to their labels."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List

from . import config
from .charades import load_dataset
from .fetch import DownloadError, HttpClient, RemoteZip, download, sha256_file
from .index import build_index, mark_videos


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


def pick_sample_clips(db_path: Path, count: int) -> List[str]:
    """Deterministic, varied sample: the best-annotated clip of each scene in
    turn, plus one clip with QA flags so the QA view can be seen on video."""
    if count <= 0:
        return []
    conn = sqlite3.connect(str(db_path))
    try:
        good = conn.execute(
            "SELECT id, scene FROM clip WHERE verified = 1 AND quality >= 6 AND relevance >= 6"
            " AND n_issues = 0 AND n_actions >= 4 AND length BETWEEN 15 AND 40"
            " ORDER BY scene, n_actions DESC, id"
        ).fetchall()
        flagged = conn.execute(
            "SELECT id FROM clip WHERE n_issues > 0 AND n_actions > 0 ORDER BY n_issues DESC, id LIMIT 1"
        ).fetchall()
    finally:
        conn.close()

    by_scene = {}
    for clip_id, scene in good:
        by_scene.setdefault(scene, []).append(clip_id)
    picked: List[str] = [row[0] for row in flagged][: 1 if count >= 4 else 0]
    round_ = 0
    while len(picked) < count and any(len(ids) > round_ for ids in by_scene.values()):
        for scene in sorted(by_scene):
            if round_ < len(by_scene[scene]) and len(picked) < count:
                picked.append(by_scene[scene][round_])
        round_ += 1
    return picked


def fetch_sample_videos(client: HttpClient, clip_ids: List[str]) -> None:
    config.VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    missing = [c for c in clip_ids if not (config.VIDEO_DIR / f"{c}.mp4").exists()]
    if clip_ids and not missing:
        log(f"videos: all {len(clip_ids)} sample videos already present")
    if missing:
        started = time.time()
        log("videos: reading the index of the 16 GB video archive via HTTP range requests "
            "(nothing else is downloaded)")
        archive = RemoteZip.from_url(client, config.VIDEOS_ZIP_URL)
        members = {
            Path(info.filename).stem: info
            for info in archive.infolist() if info.filename.endswith(".mp4")
        }
        log(f"videos: {len(members):,} videos in the archive; fetching {len(missing)} "
            f"(~{sum(members[c].compress_size for c in missing if c in members) / 1e6:.0f} MB)")

        def fetch(clip_id: str) -> int:
            data = archive.read(members[clip_id])  # CRC-32 checked inside
            target = config.VIDEO_DIR / f"{clip_id}.mp4"
            tmp = target.with_suffix(".mp4.part")
            tmp.write_bytes(data)
            os.replace(tmp, target)
            return len(data)

        done = 0
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(fetch, c): c for c in missing if c in members}
            for future in as_completed(futures):
                clip_id = futures[future]
                done += 1
                try:
                    size = future.result()
                    log(f"videos: [{done}/{len(futures)}] {clip_id}.mp4 {size / 1e6:.1f} MB")
                except Exception as exc:  # one bad video must not fail setup
                    log(f"videos: [{done}/{len(futures)}] {clip_id}.mp4 skipped ({exc})")
        log(f"videos: done in {time.time() - started:.1f}s")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="setup.sh", description=__doc__)
    parser.add_argument(
        "--videos", type=int, metavar="N",
        default=int(os.environ.get("SAMPLE_VIDEOS", config.DEFAULT_SAMPLE_VIDEOS)),
        help=f"sample videos to fetch, ~1 MB each (default {config.DEFAULT_SAMPLE_VIDEOS}; 0 to skip)",
    )
    parser.add_argument("--force", action="store_true", help="re-download the annotations even if cached")
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

    # Videos are a nice-to-have: failures are reported but never fail setup.
    try:
        fetch_sample_videos(client, pick_sample_clips(config.DB_PATH, args.videos))
    except Exception as exc:
        log(f"videos: skipped ({exc}); the tool works fully without them")
    on_disk = [p.stem for p in config.VIDEO_DIR.glob("*.mp4")] if config.VIDEO_DIR.exists() else []
    marked = mark_videos(config.DB_PATH, on_disk)

    log(f"done in {time.time() - started:.1f}s ({marked} clips have a playable sample video)")
    log("next: ./run.sh   (then open http://127.0.0.1:8000)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
