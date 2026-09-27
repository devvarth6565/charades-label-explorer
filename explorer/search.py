"""Search and filter over the index. Shared by the web API and the CLI.

Filter semantics:

* ``q``: full-text over clip id, scene, action labels, objects, script and
  descriptions. Words are ANDed, ``"quoted phrases"`` are kept together, and
  the last word is treated as a prefix so results update while typing.
* ``scene``, ``split``, ``issue``: OR within the field (a clip has one
  scene). Their facet counts ignore their own filter, so you can see what
  selecting another value would add.
* ``action``, ``verb``, ``object``: AND (a clip has many actions), so each
  extra selection narrows the results.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .charades import ISSUES
from .index import WORD_RE

SORTS = {
    "relevance": "Best match",
    "id": "Clip ID",
    "longest": "Longest first",
    "shortest": "Shortest first",
    "most_actions": "Most actions",
    "fewest_actions": "Fewest actions",
    "quality": "Highest quality",
    "most_issues": "Most QA issues",
}
_ORDER_BY = {
    "id": "c.id",
    "longest": "c.length DESC, c.id",
    "shortest": "c.length ASC, c.id",
    "most_actions": "c.n_actions DESC, c.id",
    "fewest_actions": "c.n_actions ASC, c.id",
    "quality": "c.quality IS NULL, c.quality DESC, c.relevance DESC, c.id",
    "most_issues": "c.n_issues DESC, c.id",
}
# BM25 column weights: id, scene, actions, objects, script, descriptions.
# Ground-truth action labels outrank free-text script/descriptions.
_BM25 = "bm25(clip_fts, 10.0, 3.0, 4.0, 2.0, 1.0, 1.0)"
# Match markers for snippets; the client splits on these instead of trusting HTML.
MARK_START, MARK_END = "\x02", "\x03"

MAX_PAGE_SIZE = 100
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class QueryError(ValueError):
    """Invalid filter value; the message is safe to show to users."""


def _first(params: Mapping[str, Sequence[str]], key: str) -> str:
    values = params.get(key) or [""]
    return values[0].strip()


def _many(params: Mapping[str, Sequence[str]], key: str) -> List[str]:
    out: List[str] = []
    for value in params.get(key) or []:
        for part in value.split(","):
            if part.strip() and part.strip() not in out:
                out.append(part.strip())
    return out


def _number(params, key, cast, low=None, high=None):
    raw = _first(params, key)
    if raw == "":
        return None
    try:
        value = cast(raw)
    except ValueError:
        raise QueryError(f"{key} must be a number, got {raw!r}") from None
    if (low is not None and value < low) or (high is not None and value > high):
        raise QueryError(f"{key} must be between {low} and {high}")
    return value


@dataclass
class Query:
    q: str = ""
    scenes: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)
    verbs: List[str] = field(default_factory=list)
    objects: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    split: Optional[str] = None
    verified: Optional[bool] = None
    min_length: Optional[float] = None
    max_length: Optional[float] = None
    min_quality: Optional[int] = None
    has_video: bool = False
    sort: str = "relevance"
    page: int = 1
    page_size: int = 25

    @classmethod
    def from_params(cls, params: Mapping[str, Sequence[str]]) -> "Query":
        """Build from ``parse_qs``-style params, validating every value."""
        split = _first(params, "split").lower() or None
        if split not in (None, "train", "test"):
            raise QueryError("split must be 'train' or 'test'")

        verified_raw = _first(params, "verified").lower()
        if verified_raw in _TRUE:
            verified: Optional[bool] = True
        elif verified_raw in _FALSE:
            verified = False
        elif verified_raw in ("", "any"):
            verified = None
        else:
            raise QueryError("verified must be 'yes' or 'no'")

        issues = _many(params, "issue")
        unknown = [i for i in issues if i not in ISSUES and i not in ("any", "none")]
        if unknown:
            raise QueryError(f"unknown issue {unknown[0]!r}; expected one of {sorted(ISSUES)}")

        sort = _first(params, "sort") or "relevance"
        if sort not in SORTS:
            raise QueryError(f"sort must be one of {list(SORTS)}")

        return cls(
            # keep a trailing space: it tells us the last word is finished
            q=(params.get("q") or [""])[0].lstrip()[:200] if _first(params, "q") else "",
            scenes=_many(params, "scene"),
            actions=_many(params, "action"),
            verbs=_many(params, "verb"),
            objects=_many(params, "object"),
            issues=issues,
            split=split,
            verified=verified,
            min_length=_number(params, "min_length", float, 0, 1e6),
            max_length=_number(params, "max_length", float, 0, 1e6),
            min_quality=_number(params, "min_quality", int, 1, 7),
            has_video=_first(params, "has_video").lower() in _TRUE,
            sort=sort,
            page=_number(params, "page", int, 1, 10**6) or 1,
            page_size=_number(params, "page_size", int, 1, MAX_PAGE_SIZE) or 25,
        )


def _placeholders(n: int) -> str:
    return ", ".join("?" * n)


class Store:
    """Read-only access to the index. Safe to share across threads."""

    def __init__(self, db_path: Path):
        db_path = Path(db_path).resolve()
        if not db_path.exists():
            raise FileNotFoundError(f"index not found at {db_path}; run ./setup.sh first")
        self._uri = db_path.as_uri() + "?mode=ro"
        with closing(self._connect()) as conn:
            self.info = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            self.classes = {
                row["id"]: dict(row) for row in conn.execute("SELECT * FROM action_class")
            }
        self.fts = self.info.get("fts") == "1"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------------------------------------------------------------- text

    def _fts_expression(self, conn: sqlite3.Connection, text: str) -> Optional[str]:
        """Turn user input into a safe FTS5 query string (or None if empty)."""
        parts: List[Tuple[str, List[str]]] = []
        for match in re.finditer(r'"([^"]*)"?|(\S+)', text):
            if match.group(2) is not None:
                parts += [("word", [w]) for w in WORD_RE.findall(match.group(2).lower())]
            else:
                words = WORD_RE.findall(match.group(1).lower())
                if words:
                    parts.append(("phrase", words))
        if not parts:
            return None

        typing_last_word = parts[-1][0] == "word" and not text[-1:].isspace()
        clauses = ['"' + " ".join(words) + '"' for _, words in parts]
        if typing_last_word:
            clauses[-1] = self._prefix_clause(conn, parts[-1][1][0])
        return " AND ".join(clauses)

    def _prefix_clause(self, conn: sqlite3.Connection, partial: str) -> str:
        # The index is stemmed ("cooking" is stored as "cook"), so a raw prefix
        # like "cooki*" matches nothing. Expand it to real words first.
        rows = conn.execute(
            "SELECT term FROM vocab WHERE term > ? AND term < ? ORDER BY n DESC LIMIT 12",
            (partial, partial + "\U0010ffff"),
        ).fetchall()
        options = [f'"{partial}"*'] + [f'"{row[0]}"' for row in rows]
        return "(" + " OR ".join(options) + ")"

    def _where(self, query: Query, fts_expr: Optional[str], exclude=()) -> Tuple[str, List[Any]]:
        clauses: List[str] = []
        params: List[Any] = []

        if query.q:
            if self.fts:
                if fts_expr:
                    clauses.append("c.rowid IN (SELECT rowid FROM clip_fts WHERE clip_fts MATCH ?)")
                    params.append(fts_expr)
            else:
                for token in WORD_RE.findall(query.q.lower()):
                    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    clauses.append("c.search_text LIKE ? ESCAPE '\\'")
                    params.append(f"%{escaped}%")

        if query.scenes and "scene" not in exclude:
            clauses.append(f"c.scene IN ({_placeholders(len(query.scenes))})")
            params += query.scenes
        if query.split and "split" not in exclude:
            clauses.append("c.split = ?")
            params.append(query.split)
        for column, values in (("class_id", query.actions), ("verb", query.verbs),
                               ("object", query.objects)):
            for value in values:
                clauses.append(
                    f"EXISTS (SELECT 1 FROM segment s WHERE s.clip_rowid = c.rowid AND s.{column} = ?)"
                )
                params.append(value)
        if query.verified is not None:
            clauses.append("c.verified = ?")
            params.append(int(query.verified))
        if query.min_length is not None:
            clauses.append("c.length >= ?")
            params.append(query.min_length)
        if query.max_length is not None:
            clauses.append("c.length <= ?")
            params.append(query.max_length)
        if query.min_quality is not None:
            clauses.append("c.quality >= ?")
            params.append(query.min_quality)
        if query.has_video:
            clauses.append("c.has_video = 1")
        if query.issues and "issue" not in exclude:
            codes = [i for i in query.issues if i in ISSUES]
            options = []
            if "any" in query.issues:
                options.append("c.n_issues > 0")
            if "none" in query.issues:
                options.append("c.n_issues = 0")
            if codes:
                options.append(
                    "EXISTS (SELECT 1 FROM clip_issue i WHERE i.clip_rowid = c.rowid"
                    f" AND i.code IN ({_placeholders(len(codes))}))"
                )
                params += codes
            clauses.append("(" + " OR ".join(options) + ")")

        return ("WHERE " + " AND ".join(clauses)) if clauses else "", params

    def _ranked_select(self, query: Query, fts_expr: Optional[str]) -> Tuple[str, List[Any]]:
        where, params = self._where(query, fts_expr)
        if self.fts and fts_expr:
            join = (
                f"JOIN (SELECT rowid AS rid, {_BM25} AS score"
                " FROM clip_fts WHERE clip_fts MATCH ?) r ON r.rid = c.rowid"
            )
            order = "r.score, c.id" if query.sort == "relevance" else _ORDER_BY[query.sort]
            return f"SELECT c.rowid, c.* FROM clip c {join} {where} ORDER BY {order}", [fts_expr] + params
        order = _ORDER_BY.get(query.sort, "c.id")
        return f"SELECT c.rowid, c.* FROM clip c {where} ORDER BY {order}", params

    def _snippets(self, conn, fts_expr: str, rowids: List[int]) -> Dict[int, str]:
        """Highlighted match context, computed only for the rows on this page."""
        if not rowids:
            return {}
        rows = conn.execute(
            f"SELECT rowid, snippet(clip_fts, -1, '{MARK_START}', '{MARK_END}', '…', 14)"
            f" FROM clip_fts WHERE clip_fts MATCH ? AND rowid IN ({_placeholders(len(rowids))})",
            [fts_expr] + rowids,
        )
        return dict(rows.fetchall())

    # ----------------------------------------------------------- public API

    def search(self, query: Query, with_facets: bool = True) -> Dict[str, Any]:
        with closing(self._connect()) as conn:
            fts_expr = self._fts_expression(conn, query.q) if (self.fts and query.q) else None
            where, params = self._where(query, fts_expr)
            total, seconds, segments = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(c.length), 0), COALESCE(SUM(c.n_actions), 0)"
                f" FROM clip c {where}", params,
            ).fetchone()

            pages = max(1, math.ceil(total / query.page_size))
            page = min(query.page, pages)
            sql, sql_params = self._ranked_select(query, fts_expr)
            rows = conn.execute(
                f"{sql} LIMIT ? OFFSET ?",
                sql_params + [query.page_size, (page - 1) * query.page_size],
            ).fetchall()
            items = [self._row_to_clip(row, detail=False) for row in rows]
            if fts_expr:
                snippets = self._snippets(conn, fts_expr, [row["rowid"] for row in rows])
                for item, row in zip(items, rows):
                    item["snippet"] = snippets.get(row["rowid"])
            result = {
                "total": total,
                "page": page,
                "pages": pages,
                "page_size": query.page_size,
                "hours": round(seconds / 3600, 2),
                "segments": segments,
                "items": items,
            }
            if with_facets:
                result["facets"] = self._facets(conn, query, fts_expr)
            return result

    def iter_matches(self, query: Query) -> Iterator[Dict[str, Any]]:
        """All matching clips (no paging), in the query's sort order."""
        with closing(self._connect()) as conn:
            fts_expr = self._fts_expression(conn, query.q) if (self.fts and query.q) else None
            sql, params = self._ranked_select(query, fts_expr)
            for row in conn.execute(sql, params):
                yield self._row_to_clip(row, detail=True)

    def get_clip(self, clip_id: str) -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT c.* FROM clip c WHERE c.id = ?", (clip_id,)).fetchone()
            return self._row_to_clip(row, detail=True) if row else None

    def meta(self) -> Dict[str, Any]:
        """Dataset-wide stats and the vocabularies the UI needs for filters."""
        with closing(self._connect()) as conn:
            stats = dict(conn.execute(
                "SELECT COUNT(*) AS clips, ROUND(SUM(length) / 3600.0, 1) AS hours,"
                " SUM(n_actions) AS segments, COUNT(DISTINCT subject) AS subjects,"
                " COUNT(DISTINCT scene) AS scenes, SUM(verified = 1) AS verified,"
                " SUM(n_issues > 0) AS with_issues, SUM(has_video) AS with_video,"
                " ROUND(AVG(length), 1) AS avg_length, ROUND(AVG(n_actions), 2) AS avg_actions,"
                " SUM(split = 'train') AS train, SUM(split = 'test') AS test FROM clip"
            ).fetchone())
            class_counts = dict(conn.execute(
                "SELECT class_id, COUNT(DISTINCT clip_rowid) FROM segment GROUP BY class_id"
            ).fetchall())
            stats["classes"] = len(class_counts)

            def counts(sql: str) -> List[Dict[str, Any]]:
                return [{"value": v, "count": n} for v, n in conn.execute(sql)]

            issue_counts = dict(conn.execute("SELECT code, COUNT(*) FROM clip_issue GROUP BY code"))
            return {
                "stats": stats,
                "fts": self.fts,
                "built_at": self.info.get("built_at"),
                "classes": [
                    {**cls, "count": class_counts.get(cls["id"], 0)}
                    for cls in sorted(self.classes.values(), key=lambda c: c["id"])
                ],
                "scenes": counts("SELECT scene, COUNT(*) n FROM clip GROUP BY scene ORDER BY n DESC"),
                "verbs": counts(
                    "SELECT verb, COUNT(DISTINCT clip_rowid) n FROM segment WHERE verb != ''"
                    " GROUP BY verb ORDER BY n DESC"
                ),
                "objects": counts(
                    "SELECT object, COUNT(DISTINCT clip_rowid) n FROM segment WHERE object != ''"
                    " GROUP BY object ORDER BY n DESC"
                ),
                "issues": [
                    {"value": code, "label": label, "count": issue_counts.get(code, 0)}
                    for code, label in ISSUES.items()
                ],
                "sorts": [{"value": k, "label": v} for k, v in SORTS.items()],
            }

    # ------------------------------------------------------------ helpers

    def _facets(self, conn, query: Query, fts_expr: Optional[str]) -> Dict[str, List[Dict]]:
        def run(sql: str, column: str, exclude=()) -> List[Dict[str, Any]]:
            where, params = self._where(query, fts_expr, exclude)
            # Without filters, skip the IN (...) entirely: it is ~8x slower
            # than a plain GROUP BY over the whole table.
            match = f"{column} IN (SELECT c.rowid FROM clip c {where})" if where else "1"
            return [
                {"value": v, "count": n}
                for v, n in conn.execute(sql.format(match=match), params)
            ]

        per_clip = "COUNT(DISTINCT clip_rowid) n FROM segment WHERE {match}"
        issue_where, issue_params = self._where(query, fts_expr, {"issue"})
        any_issue, no_issue = conn.execute(
            "SELECT COALESCE(SUM(c.n_issues > 0), 0), COALESCE(SUM(c.n_issues = 0), 0)"
            f" FROM clip c {issue_where}", issue_params,
        ).fetchone()
        return {
            "scene": run(
                "SELECT scene, COUNT(*) n FROM clip WHERE {match} GROUP BY scene ORDER BY n DESC",
                "rowid", exclude={"scene"},
            ),
            "split": run(
                "SELECT split, COUNT(*) n FROM clip WHERE {match} GROUP BY split",
                "rowid", exclude={"split"},
            ),
            "issue": [{"value": "any", "count": any_issue}, {"value": "none", "count": no_issue}]
            + run(
                "SELECT code, COUNT(*) n FROM clip_issue WHERE {match} GROUP BY code ORDER BY n DESC",
                "clip_rowid", exclude={"issue"},
            ),
            "action": run(
                f"SELECT class_id, {per_clip} GROUP BY class_id ORDER BY n DESC", "clip_rowid"
            ),
            "verb": run(
                f"SELECT verb, {per_clip} AND verb != '' GROUP BY verb ORDER BY n DESC",
                "clip_rowid",
            ),
            "object": run(
                f"SELECT object, {per_clip} AND object != '' GROUP BY object ORDER BY n DESC",
                "clip_rowid",
            ),
        }

    @staticmethod
    def _row_to_clip(row: sqlite3.Row, detail: bool) -> Dict[str, Any]:
        clip = {
            "id": row["id"],
            "split": row["split"],
            "subject": row["subject"],
            "scene": row["scene"],
            "scene_detail": row["scene_detail"],
            "length": row["length"],
            "quality": row["quality"],
            "relevance": row["relevance"],
            "verified": None if row["verified"] is None else bool(row["verified"]),
            "coverage": row["coverage"],
            "n_actions": row["n_actions"],
            "script": row["script"],
            "objects": json.loads(row["objects"]),
            "segments": json.loads(row["segments"]),
            "issues": json.loads(row["issues"]),
            "has_video": bool(row["has_video"]),
            "snippet": None,
        }
        if detail:
            clip["descriptions"] = json.loads(row["descriptions"])
            clip["raw_actions"] = row["raw_actions"]
        return clip
