"""Build a SQLite search index from the parsed dataset.

SQLite ships with Python, so this gives us real full-text search (FTS5 with
BM25 ranking and Porter stemming) without asking reviewers to run
Elasticsearch. If the local SQLite was built without FTS5, search degrades to
substring matching over a plain text column instead of failing.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Iterable

from .charades import ISSUES, Clip, Dataset

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE action_class (
    id TEXT PRIMARY KEY, label TEXT NOT NULL, verb TEXT NOT NULL, object TEXT NOT NULL
);

CREATE TABLE clip (
    id TEXT NOT NULL UNIQUE,
    split TEXT NOT NULL,
    subject TEXT NOT NULL,
    scene TEXT NOT NULL,
    scene_detail TEXT NOT NULL,
    quality INTEGER,
    relevance INTEGER,
    verified INTEGER,
    length REAL,
    coverage REAL NOT NULL,
    n_actions INTEGER NOT NULL,
    n_issues INTEGER NOT NULL,
    script TEXT NOT NULL,
    objects TEXT NOT NULL,        -- JSON list
    descriptions TEXT NOT NULL,   -- JSON list
    segments TEXT NOT NULL,       -- JSON list of {class_id, label, verb, object, start, end}
    issues TEXT NOT NULL,         -- JSON list of issue codes
    raw_actions TEXT NOT NULL,
    search_text TEXT NOT NULL,    -- lowercase haystack for the non-FTS fallback
    has_video INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX clip_scene ON clip(scene);
-- Covers the result-count/total-hours aggregate without reading wide rows.
CREATE INDEX clip_length ON clip(length, n_actions);

CREATE TABLE segment (
    clip_rowid INTEGER NOT NULL, class_id TEXT NOT NULL, verb TEXT NOT NULL,
    object TEXT NOT NULL, start REAL NOT NULL, "end" REAL NOT NULL
);
CREATE INDEX segment_clip ON segment(clip_rowid);
CREATE INDEX segment_class ON segment(class_id, clip_rowid);
CREATE INDEX segment_verb ON segment(verb, clip_rowid);
CREATE INDEX segment_object ON segment(object, clip_rowid);

CREATE TABLE clip_issue (clip_rowid INTEGER NOT NULL, code TEXT NOT NULL);
CREATE INDEX clip_issue_code ON clip_issue(code, clip_rowid);

-- Every distinct (unstemmed) word, used to expand the half-typed last word
-- of a query ("cooki" -> "cooking") before it hits the stemmed FTS index.
CREATE TABLE vocab (term TEXT PRIMARY KEY, n INTEGER NOT NULL) WITHOUT ROWID;
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE clip_fts USING fts5(
    id, scene, actions, objects, script, descriptions,
    tokenize = 'porter unicode61'
);
"""

# Letters and digits; "_" is a separator, matching the FTS5 unicode61 tokenizer.
WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def fts5_available() -> bool:
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False


def _segment_json(clip: Clip) -> str:
    return json.dumps(
        [
            {"class_id": s.class_id, "label": s.label, "verb": s.verb,
             "object": s.object, "start": s.start, "end": s.end}
            for s in clip.segments
        ],
        separators=(",", ":"),
    )


def _fts_fields(clip: Clip):
    return (
        clip.id,
        " ".join(filter(None, [clip.scene, clip.scene_detail])),
        " ; ".join(f"{s.class_id} {s.label}" for s in clip.segments),
        " ".join(clip.objects),
        clip.script,
        " ".join(clip.descriptions),
    )


def build_index(dataset: Dataset, db_path: Path, use_fts: bool = True) -> dict:
    """Write a fresh index to ``db_path`` atomically and return summary stats."""
    use_fts = use_fts and fts5_available()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(db_path.name + ".building")
    if tmp_path.exists():
        tmp_path.unlink()

    conn = sqlite3.connect(str(tmp_path))
    try:
        conn.executescript(SCHEMA)
        if use_fts:
            conn.executescript(FTS_SCHEMA)
        conn.executemany(
            "INSERT INTO action_class VALUES (?, ?, ?, ?)",
            [(c.id, c.label, c.verb, c.object) for c in dataset.taxonomy.classes.values()],
        )
        vocab: Counter = Counter()
        for clip in dataset.clips:
            fts_fields = _fts_fields(clip)
            haystack = " ".join(fts_fields).lower()
            vocab.update(WORD_RE.findall(haystack))
            cur = conn.execute(
                "INSERT INTO clip (id, split, subject, scene, scene_detail, quality, relevance,"
                " verified, length, coverage, n_actions, n_issues, script, objects,"
                " descriptions, segments, issues, raw_actions, search_text)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    clip.id, clip.split, clip.subject, clip.scene, clip.scene_detail,
                    clip.quality, clip.relevance,
                    None if clip.verified is None else int(clip.verified),
                    clip.length, clip.coverage, len(clip.segments), len(clip.issues),
                    clip.script, json.dumps(clip.objects), json.dumps(clip.descriptions),
                    _segment_json(clip), json.dumps(clip.issues), clip.raw_actions, haystack,
                ),
            )
            rowid = cur.lastrowid
            conn.executemany(
                "INSERT INTO segment VALUES (?, ?, ?, ?, ?, ?)",
                [(rowid, s.class_id, s.verb, s.object, s.start, s.end) for s in clip.segments],
            )
            conn.executemany(
                "INSERT INTO clip_issue VALUES (?, ?)", [(rowid, code) for code in clip.issues]
            )
            if use_fts:
                conn.execute(
                    "INSERT INTO clip_fts (rowid, id, scene, actions, objects, script, descriptions)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (rowid, *fts_fields),
                )
        conn.executemany("INSERT INTO vocab VALUES (?, ?)", vocab.items())

        summary = {
            "schema_version": SCHEMA_VERSION,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "fts": "1" if use_fts else "0",
            "clips": str(len(dataset.clips)),
            "segments": str(sum(len(c.segments) for c in dataset.clips)),
            "parse_warnings": json.dumps(dataset.warnings),
            "issue_labels": json.dumps(ISSUES),
        }
        conn.executemany("INSERT INTO meta VALUES (?, ?)", summary.items())
        if use_fts:
            conn.execute("INSERT INTO clip_fts(clip_fts) VALUES ('optimize')")
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    os.replace(tmp_path, db_path)  # a running server never sees a half-built index
    return summary


def mark_videos(db_path: Path, clip_ids: Iterable[str]) -> int:
    """Flag which clips have a playable sample video on disk."""
    ids = sorted(set(clip_ids))
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE clip SET has_video = 0")
        conn.executemany("UPDATE clip SET has_video = 1 WHERE id = ?", [(i,) for i in ids])
        conn.commit()
        return conn.execute("SELECT COUNT(*) FROM clip WHERE has_video = 1").fetchone()[0]
    finally:
        conn.close()
