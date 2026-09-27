"""Terminal interface: ``./run.sh search|show|stats``.

Uses the same Store/Query as the web API, so both always agree.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
from typing import Dict, List, Optional

from . import config
from .charades import ISSUES
from .search import MARK_END, MARK_START, SORTS, Query, QueryError, Store

_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _style(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if _COLOR else text


def dim(text: str) -> str:
    return _style(text, "2")


def bold(text: str) -> str:
    return _style(text, "1")


def warn(text: str) -> str:
    return _style(text, "33")


def _fit(text: str, width: int) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def _open_store() -> Store:
    try:
        return Store(config.DB_PATH)
    except FileNotFoundError as exc:
        sys.exit(f"error: {exc}")


def _resolve_actions(store: Store, values: List[str]) -> List[str]:
    """Accept class ids (c092) or words from the label ("window")."""
    resolved = []
    for value in values:
        if re.fullmatch(r"c\d{3}", value.lower()):
            resolved.append(value.lower())
            continue
        matches = [c for c in store.classes.values() if value.lower() in c["label"].lower()]
        if len(matches) == 1:
            resolved.append(matches[0]["id"])
        elif not matches:
            sys.exit(f"error: no action class matches {value!r}")
        else:
            options = "\n".join(f"  {c['id']}  {c['label']}" for c in matches[:15])
            sys.exit(f"error: {value!r} matches {len(matches)} action classes; pick one id:\n{options}")
    return resolved


# ------------------------------------------------------------------ search


def _query_from_args(store: Store, args: argparse.Namespace, page_size: int) -> Query:
    params: Dict[str, List[str]] = {
        "q": [" ".join(args.text)],
        "scene": args.scene or [],
        "action": _resolve_actions(store, args.action or []),
        "verb": args.verb or [],
        "object": args.object or [],
        "issue": args.issue or [],
        "split": [args.split or ""],
        "verified": [{True: "yes", False: "no", None: ""}[args.verified]],
        "min_length": [str(args.min_length) if args.min_length is not None else ""],
        "max_length": [str(args.max_length) if args.max_length is not None else ""],
        "min_quality": [str(args.min_quality) if args.min_quality is not None else ""],
        "has_video": ["1" if args.has_video else ""],
        "sort": [args.sort],
        "page": [str(args.page)],
        "page_size": [str(page_size)],
    }
    try:
        return Query.from_params(params)
    except QueryError as exc:
        sys.exit(f"error: {exc}")


def cmd_search(args: argparse.Namespace) -> int:
    store = _open_store()
    if args.format in ("json", "csv"):
        query = _query_from_args(store, args, page_size=1)
        rows = list(store.iter_matches(query))[: args.limit] if args.limit else list(store.iter_matches(query))
        if args.format == "json":
            json.dump(rows, sys.stdout, indent=2)
            print()
        else:
            write_csv(rows, sys.stdout)
        return 0

    query = _query_from_args(store, args, page_size=args.limit or 20)
    result = store.search(query, with_facets=False)
    width = shutil.get_terminal_size((120, 20)).columns
    fixed = 5 + 2 + 5 + 2 + 18 + 2 + 7 + 2 + 3 + 2 + 3 + 2 + 5
    actions_width = max(20, width - fixed - 2)

    header = (f"{'ID':5}  {'Split':5}  {'Scene':18}  {'Length':>7}  {'#':>3}  "
              f"{'Q':>3}  {'Flags':5}  Actions")
    print(bold(header))
    for clip in result["items"]:
        labels = ", ".join(dict.fromkeys(s["label"] for s in clip["segments"])) or dim("(none)")
        flags = ("!" * min(len(clip["issues"]), 3)) if clip["issues"] else ""
        quality = "-" if clip["quality"] is None else str(clip["quality"])
        print(
            f"{clip['id']:5}  {clip['split']:5}  {_fit(clip['scene'], 18):18}  "
            f"{clip['length'] or 0:6.1f}s  {clip['n_actions']:>3}  {quality:>3}  "
            f"{warn(f'{flags:5}') if flags else ' ' * 5}  {_fit(labels, actions_width)}"
        )
        if clip.get("snippet"):
            snippet = clip["snippet"].replace(MARK_START, "\x1b[1;36m" if _COLOR else "[")
            snippet = snippet.replace(MARK_END, "\x1b[0m" if _COLOR else "]")
            print(dim("       match: ") + snippet.replace("\n", " ")[: width + 20])

    shown = len(result["items"])
    start = (result["page"] - 1) * result["page_size"] + 1 if shown else 0
    print(dim(
        f"\n{start}-{start + shown - 1 if shown else 0} of {result['total']:,} clips "
        f"({result['hours']} h, {result['segments']:,} segments). "
        f"Page {result['page']}/{result['pages']}. Use --page N, or `./run.sh show <ID>`."
    ))
    return 0


def write_csv(rows: List[dict], stream) -> None:
    writer = csv.writer(stream)
    writer.writerow([
        "id", "split", "scene", "length_s", "quality", "relevance", "verified",
        "n_actions", "coverage", "actions", "action_triplets", "objects", "script",
        "issues",
    ])
    for c in rows:
        writer.writerow([
            c["id"], c["split"], c["scene"], c["length"], c["quality"], c["relevance"],
            {True: "yes", False: "no", None: ""}[c["verified"]], c["n_actions"], c["coverage"],
            "; ".join(f"{s['label']} [{s['start']:.1f}-{s['end']:.1f}s]" for s in c["segments"]),
            ";".join(f"{s['class_id']} {s['start']:.2f} {s['end']:.2f}" for s in c["segments"]),
            ";".join(c["objects"]), c["script"], ";".join(c["issues"]),
        ])


# -------------------------------------------------------------------- show


def _timeline_bar(start: float, end: float, length: float, width: int) -> str:
    lo, hi = sorted((start, end))
    a = int(round(max(0.0, min(lo, length)) / length * width))
    b = int(round(max(0.0, min(hi, length)) / length * width))
    b = max(b, a + 1)
    return "·" * a + "█" * (b - a) + "·" * max(0, width - b)


def cmd_show(args: argparse.Namespace) -> int:
    store = _open_store()
    clip = store.get_clip(args.clip_id.upper())
    if clip is None:
        print(f"error: no clip with id {args.clip_id!r}", file=sys.stderr)
        return 1
    if args.format == "json":
        json.dump(clip, sys.stdout, indent=2)
        print()
        return 0

    length = clip["length"] or max([s["end"] for s in clip["segments"]] + [1.0])
    verified = {True: "yes", False: warn("NO"), None: "?"}[clip["verified"]]
    print(bold(f"{clip['id']}  {clip['scene']}") + dim(f"  ({clip['scene_detail']})" if clip["scene_detail"] else ""))
    print(f"{length:.2f} s · {clip['split']} split · subject {clip['subject']} · "
          f"quality {clip['quality'] or '-'}/7 · relevance {clip['relevance'] or '-'}/7 · "
          f"verified {verified} · {clip['coverage']:.0%} of the video labeled")
    print(f"\n{bold('Script')}       {clip['script']}")
    for i, desc in enumerate(clip["descriptions"]):
        print(f"{bold('Description') if i == 0 else '':12} {desc}")
    print(f"{bold('Objects')}      {', '.join(clip['objects']) or '-'}")

    width = shutil.get_terminal_size((120, 20)).columns
    label_w = min(40, max([len(s["label"]) for s in clip["segments"]] + [10]))
    bar_w = max(20, width - label_w - 5 - 18)
    print(f"\n{bold('Actions')} ({len(clip['segments'])})")
    axis = f"0s{'':{bar_w - 2 - len(f'{length:.1f}s')}}{length:.1f}s"
    print(f"{'':{label_w + 6}} {dim(axis)}")
    for s in clip["segments"]:
        print(f"{s['class_id']}  {_fit(s['label'], label_w):{label_w}} "
              f"{_timeline_bar(s['start'], s['end'], length, bar_w)} "
              f"{s['start']:5.1f}-{s['end']:5.1f}s")
    if not clip["segments"]:
        print(dim("  (no action segments)"))

    if clip["issues"]:
        print(f"\n{warn('QA issues')}")
        for code in clip["issues"]:
            print(f"  - {code}: {ISSUES[code]}")
    print(dim(f"\nraw actions: {clip['raw_actions'] or '(empty)'}"))
    return 0


# ------------------------------------------------------------------- stats


def cmd_stats(args: argparse.Namespace) -> int:
    store = _open_store()
    meta = store.meta()
    st = meta["stats"]
    if args.format == "json":
        json.dump(meta, sys.stdout, indent=2)
        print()
        return 0

    def bars(title: str, entries: List[dict], label=lambda e: e["value"], top: Optional[int] = 10):
        entries = entries[:top] if top else entries
        if not entries:
            return
        peak = max(e["count"] for e in entries) or 1
        label_w = min(42, max(len(label(e)) for e in entries))
        print(f"\n{bold(title)}")
        for e in entries:
            bar = "█" * max(1, int(28 * e["count"] / peak)) if e["count"] else ""
            print(f"  {_fit(label(e), label_w):{label_w}}  {e['count']:>6,}  {dim(bar)}")

    print(bold("Charades v1") + f"  {st['clips']:,} clips · {st['hours']} h of video · "
          f"{st['segments']:,} action segments · {st['classes']} action classes")
    print(f"{st['scenes']} scenes · {st['subjects']} subjects · train {st['train']:,} / test {st['test']:,} · "
          f"avg {st['avg_length']} s and {st['avg_actions']} actions per clip")
    print(f"verified {st['verified']:,} ({st['verified'] / st['clips']:.1%}) · "
          f"{st['with_issues']:,} clips with QA issues · {st['with_video']} sample videos · "
          f"search: {'FTS5' if meta['fts'] else 'substring fallback'}")
    bars("Scenes", meta["scenes"], top=None)
    top_actions = sorted(meta["classes"], key=lambda c: -c["count"])
    bars("Most frequent actions (clips containing)", [{"value": f"{c['id']} {c['label']}", "count": c["count"]} for c in top_actions])
    bars("QA issues", meta["issues"], label=lambda e: e["value"], top=None)
    return 0


# -------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run.sh", description="Charades Label Explorer (CLI)")
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="search and filter clips",
                       description="Search clips. Example: ./run.sh search pour --scene Kitchen --verb drink")
    s.add_argument("text", nargs="*", help="full-text query (script, descriptions, labels, objects)")
    s.add_argument("--scene", action="append", help="scene name, repeatable (OR)")
    s.add_argument("--action", action="append", help="action class id or label words, repeatable (AND)")
    s.add_argument("--verb", action="append", help="verb, e.g. open, repeatable (AND)")
    s.add_argument("--object", action="append", help="object, e.g. door, repeatable (AND)")
    s.add_argument("--issue", action="append", help=f"QA issue: any, none, or one of {', '.join(ISSUES)}")
    s.add_argument("--split", choices=["train", "test"])
    v = s.add_mutually_exclusive_group()
    v.add_argument("--verified", dest="verified", action="store_true", default=None)
    v.add_argument("--unverified", dest="verified", action="store_false")
    s.add_argument("--min-length", type=float, metavar="SEC")
    s.add_argument("--max-length", type=float, metavar="SEC")
    s.add_argument("--min-quality", type=int, choices=range(1, 8), metavar="1-7")
    s.add_argument("--has-video", action="store_true", help="only clips with a downloaded sample video")
    s.add_argument("--sort", choices=list(SORTS), default="relevance")
    s.add_argument("--limit", type=int, default=None, help="rows to print (default 20; all for csv/json)")
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--format", choices=["table", "json", "csv"], default="table")
    s.set_defaults(func=cmd_search)

    sh = sub.add_parser("show", help="show one clip with an ASCII action timeline")
    sh.add_argument("clip_id")
    sh.add_argument("--format", choices=["text", "json"], default="text")
    sh.set_defaults(func=cmd_show)

    st = sub.add_parser("stats", help="dataset summary")
    st.add_argument("--format", choices=["text", "json"], default="text")
    st.set_defaults(func=cmd_stats)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BrokenPipeError:  # e.g. piped into `head`
        return 0
