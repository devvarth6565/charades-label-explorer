"""Paths, dataset sources and runtime settings in one place."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

DATA_DIR = Path(os.environ.get("EXPLORER_DATA_DIR", ROOT / "data")).resolve()
RAW_DIR = DATA_DIR / "raw"
VIDEO_DIR = DATA_DIR / "videos"
DB_PATH = DATA_DIR / "charades.db"

# Official annotation release from the Allen Institute for AI (AI2).
# The SHA-256 is pinned so a changed or corrupted download fails loudly
# instead of being silently indexed.
ANNOTATIONS_URL = (
    "https://ai2-public-datasets.s3-us-west-2.amazonaws.com/charades/Charades.zip"
)
ANNOTATIONS_SHA256 = "c616913ef79c2ddde06d9c562eae57bb8901d459d7568a0d27bf09cbf33ae866"

# 16 GB archive of the videos scaled to 480p. We never download it whole:
# setup reads the zip's central directory with HTTP range requests and
# pulls out only a handful of sample clips (~1 MB each).
VIDEOS_ZIP_URL = (
    "https://ai2-public-datasets.s3-us-west-2.amazonaws.com/charades/Charades_v1_480.zip"
)

# Files we keep from the annotation archive (evaluation scripts and example
# submissions are skipped).
ANNOTATION_FILES = (
    "Charades_v1_train.csv",
    "Charades_v1_test.csv",
    "Charades_v1_classes.txt",
    "Charades_v1_objectclasses.txt",
    "Charades_v1_verbclasses.txt",
    "Charades_v1_mapping.txt",
    "license.txt",
    "README.txt",
)

DEFAULT_SAMPLE_VIDEOS = 16
