# Charades Label Explorer

A small internal-style tool that ingests the **Charades** video dataset's label
format and lets you browse, search and QA its annotations.

> Work in progress: this README is filled in as each section lands.

## Dataset: Charades (AI2, 2016)

9,848 crowd-sourced videos of people doing everyday indoor activities, each
annotated with **temporally localized action labels** (157 classes, 66,500
segments with start/end times), a scene category, free-text descriptions,
interacted objects, and annotator quality/relevance ratings.

- Source: <https://prior.allenai.org/projects/charades>
- Annotations: `Charades.zip` (3.5 MB), SHA-256 pinned in
  [`explorer/config.py`](explorer/config.py)

### Datasets considered

| Dataset | Labels | Why not |
|---|---|---|
| Kinetics-400 | 1 action label per 10 s YouTube clip | Metadata is thin: one label, fixed 10 s duration; many YouTube videos are gone |
| UCF101 / HMDB51 | Class = folder name | Only one class per clip; HMDB51's official download link returned a 5 KB page |
| ActivityNet 1.3 | Temporal segments | Official annotation host did not respond |
| **Charades** | **Temporal segments + scene + objects + descriptions + ratings** | **Chosen** |

## Setup

```bash
./setup.sh   # downloads + verifies the annotations into ./data (gitignored)
```
