"""CSV export shared by the CLI and the web API."""
from __future__ import annotations

import csv
from typing import IO, Iterable

CSV_COLUMNS = (
    "id", "split", "scene", "length_s", "quality", "relevance", "verified",
    "n_actions", "coverage", "actions", "action_triplets", "objects", "script", "issues",
)


def write_csv(rows: Iterable[dict], stream: IO[str]) -> int:
    """Write clips as CSV; ``action_triplets`` round-trips to the source format."""
    writer = csv.writer(stream)
    writer.writerow(CSV_COLUMNS)
    count = 0
    for c in rows:
        writer.writerow([
            c["id"], c["split"], c["scene"], c["length"], c["quality"], c["relevance"],
            {True: "yes", False: "no", None: ""}[c["verified"]], c["n_actions"], c["coverage"],
            "; ".join(f"{s['label']} [{s['start']:.1f}-{s['end']:.1f}s]" for s in c["segments"]),
            ";".join(f"{s['class_id']} {s['start']:.2f} {s['end']:.2f}" for s in c["segments"]),
            ";".join(c["objects"]), c["script"], ";".join(c["issues"]),
        ])
        count += 1
    return count
