"""Parse the Charades label format into plain Python records.

The annotation archive contains:

``Charades_v1_{train,test}.csv``
    One row per video. Multi-valued fields are ``;``-separated, and
    ``actions`` holds ``"<class> <start s> <end s>"`` triplets, e.g.
    ``c092 11.90 21.20;c147 0.00 12.60``.
``Charades_v1_classes.txt``        ``c008 Opening a door``
``Charades_v1_mapping.txt``        ``c008 o012 v012`` (action -> object, verb)
``Charades_v1_objectclasses.txt``  ``o012 door``
``Charades_v1_verbclasses.txt``    ``v012 open``

Parsing is lenient: a bad entry never drops a clip. Instead the problem is
recorded as a QA issue on that clip so it can be filtered for and fixed.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

SPLITS = ("train", "test")
REQUIRED_COLUMNS = (
    "id", "subject", "scene", "quality", "relevance", "verified",
    "script", "objects", "descriptions", "actions", "length",
)

# Profiling the release showed ~29% of segments end up to 1.55 s after the
# video's `length` (systematic timing jitter). Only flag clear overruns.
OVERRUN_TOLERANCE_S = 2.0

ISSUES = {
    "no_actions": "No action segments labeled",
    "unverified": "Annotator could not verify the video matches its script",
    "inverted_segment": "A segment ends before it starts",
    "segment_overrun": f"A segment ends more than {OVERRUN_TOLERANCE_S:g}s after the video",
    "unknown_class": "An action class is missing from the taxonomy",
    "malformed_segment": "An action entry could not be parsed",
    "missing_rating": "Quality or relevance rating is missing",
}


@dataclass(frozen=True)
class ActionClass:
    id: str
    label: str
    verb: str
    object: str


@dataclass(frozen=True)
class Taxonomy:
    classes: Dict[str, ActionClass]
    verbs: Dict[str, str]
    objects: Dict[str, str]


@dataclass
class Segment:
    class_id: str
    label: str
    verb: str
    object: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return abs(self.end - self.start)


@dataclass
class Clip:
    id: str
    split: str
    subject: str
    scene: str
    scene_detail: str
    quality: Optional[int]
    relevance: Optional[int]
    verified: Optional[bool]
    script: str
    objects: List[str]
    descriptions: List[str]
    segments: List[Segment]
    length: Optional[float]
    raw_actions: str
    issues: List[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        """Fraction of the video covered by at least one labeled segment."""
        if not self.length or not self.segments:
            return 0.0
        spans = sorted(
            (max(0.0, min(s.start, s.end)), min(self.length, max(s.start, s.end)))
            for s in self.segments
        )
        covered, cur_start, cur_end = 0.0, None, None
        for start, end in spans:
            if cur_end is None or start > cur_end:
                if cur_end is not None:
                    covered += cur_end - cur_start
                cur_start, cur_end = start, end
            else:
                cur_end = max(cur_end, end)
        if cur_end is not None:
            covered += cur_end - cur_start
        return round(min(1.0, covered / self.length), 4)


@dataclass
class Dataset:
    taxonomy: Taxonomy
    clips: List[Clip]
    warnings: List[str]


# --------------------------------------------------------------------------


def _read_id_file(path: Path) -> Dict[str, str]:
    """Read ``<id> <text>`` lines, e.g. ``c008 Opening a door``."""
    entries = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.strip().partition(" ")
        if key:
            entries[key] = value.strip()
    return entries


def load_taxonomy(raw_dir: Path) -> Taxonomy:
    verbs = _read_id_file(raw_dir / "Charades_v1_verbclasses.txt")
    objects = _read_id_file(raw_dir / "Charades_v1_objectclasses.txt")
    objects = {k: ("" if v == "None" else v) for k, v in objects.items()}
    labels = _read_id_file(raw_dir / "Charades_v1_classes.txt")

    mapping: Dict[str, Tuple[str, str]] = {}
    for line in (raw_dir / "Charades_v1_mapping.txt").read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 3:
            mapping[parts[0]] = (parts[1], parts[2])

    classes = {}
    for class_id, label in labels.items():
        object_id, verb_id = mapping.get(class_id, ("", ""))
        classes[class_id] = ActionClass(
            id=class_id,
            label=label,
            verb=verbs.get(verb_id, ""),
            object=objects.get(object_id, ""),
        )
    return Taxonomy(classes=classes, verbs=verbs, objects=objects)


_SCENE_RE = re.compile(r"^(.*?)\s*\((.*)\)\s*$")


def split_scene(raw: str) -> Tuple[str, str]:
    """``"Entryway (A hall that ...)"`` -> ``("Entryway", "A hall that ...")``."""
    raw = raw.strip()
    match = _SCENE_RE.match(raw)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return raw, ""


def _split_multi(value: str) -> List[str]:
    return [part.strip() for part in value.split(";") if part.strip()]


def _to_int(value: str) -> Optional[int]:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return None


def _to_float(value: str) -> Optional[float]:
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return None


def parse_actions(raw: str, taxonomy: Taxonomy) -> Tuple[List[Segment], Set[str]]:
    segments: List[Segment] = []
    issues: Set[str] = set()
    for entry in _split_multi(raw):
        parts = entry.split()
        start = end = None
        if len(parts) == 3:
            start, end = _to_float(parts[1]), _to_float(parts[2])
        if start is None or end is None:
            issues.add("malformed_segment")
            continue
        class_id = parts[0]
        cls = taxonomy.classes.get(class_id)
        if cls is None:
            issues.add("unknown_class")
            cls = ActionClass(id=class_id, label=class_id, verb="", object="")
        if end < start:
            issues.add("inverted_segment")
        segments.append(Segment(class_id, cls.label, cls.verb, cls.object, start, end))
    segments.sort(key=lambda s: (min(s.start, s.end), s.end, s.class_id))
    return segments, issues


def parse_row(row: Dict[str, str], split: str, taxonomy: Taxonomy) -> Clip:
    segments, issues = parse_actions(row["actions"], taxonomy)
    scene, scene_detail = split_scene(row["scene"])
    verified = {"yes": True, "no": False}.get(row["verified"].strip().lower())
    clip = Clip(
        id=row["id"].strip(),
        split=split,
        subject=row["subject"].strip(),
        scene=scene or "Unknown",
        scene_detail=scene_detail,
        quality=_to_int(row["quality"]),
        relevance=_to_int(row["relevance"]),
        verified=verified,
        script=row["script"].strip(),
        objects=_split_multi(row["objects"]),
        descriptions=_split_multi(row["descriptions"]),
        segments=segments,
        length=_to_float(row["length"]),
        raw_actions=row["actions"].strip(),
    )
    if not segments:
        issues.add("no_actions")
    if verified is False:
        issues.add("unverified")
    if clip.quality is None or clip.relevance is None:
        issues.add("missing_rating")
    if clip.length and any(max(s.start, s.end) > clip.length + OVERRUN_TOLERANCE_S for s in segments):
        issues.add("segment_overrun")
    clip.issues = [code for code in ISSUES if code in issues]  # stable order
    return clip


def iter_split(path: Path, split: str, taxonomy: Taxonomy) -> Iterator[Clip]:
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.name}: missing columns {missing}")
        for row in reader:
            yield parse_row(row, split, taxonomy)


def load_dataset(raw_dir: Path) -> Dataset:
    taxonomy = load_taxonomy(raw_dir)
    clips: List[Clip] = []
    warnings: List[str] = []
    seen: Set[str] = set()
    for split in SPLITS:
        path = raw_dir / f"Charades_v1_{split}.csv"
        for clip in iter_split(path, split, taxonomy):
            if not clip.id:
                warnings.append(f"{path.name}: row without an id skipped")
                continue
            if clip.id in seen:
                warnings.append(f"{path.name}: duplicate id {clip.id} skipped")
                continue
            seen.add(clip.id)
            clips.append(clip)
    return Dataset(taxonomy=taxonomy, clips=clips, warnings=warnings)
