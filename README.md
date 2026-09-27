# Charades Label Explorer

A small internal tool for browsing, searching and QA-ing the labels of
**[Charades](https://prior.allenai.org/projects/charades)**, a video dataset of
9,848 clips with 66,500 time-stamped action labels. It has a web UI and a
CLI, uses only the Python 3 standard library (no pip, no Node, no build
step), and searches with SQLite FTS5.

```bash
./setup.sh   # ~30 s: downloads + verifies the labels, builds the index, fetches 16 sample videos
./run.sh     # → http://127.0.0.1:8000
```

## Summary Card

| Section | What I did | Confidence | Files |
|---|---|:-:|---|
| **1. Dataset** | Picked **Charades** (AI2): real video with 157 action classes and start/end times, scene, objects, free-text descriptions and annotator ratings. `setup.sh` downloads the official 3.5 MB label archive and checks it against a pinned SHA-256. No data is committed, because the license forbids it. | 5 | [`config.py`](explorer/config.py), [`setup_data.py`](explorer/setup_data.py), [`fetch.py`](explorer/fetch.py) |
| **2. Ingest + summary** | Parser for the CSV and 4 taxonomy files, producing clean records (verb/object resolved, scene normalized) plus 7 automatic label-QA checks. Web table with a mini action timeline per clip; detail drawer with a Gantt chart of every segment; CLI table and ASCII timeline. | 5 | [`charades.py`](explorer/charades.py), [`web/`](web/), [`cli.py`](explorer/cli.py) |
| **3. Search + filter** | SQLite **FTS5** full-text search (BM25 ranking, stemming, phrases, search-as-you-type, highlighted matches) plus faceted filters with live counts (scene, action, verb, object, split, length, quality, verified, QA issue, has video). CSV/JSON export of any filtered view. | 5 | [`search.py`](explorer/search.py), [`index.py`](explorer/index.py) |
| **4. README** | This file: dataset choice, how to run, assumptions, design notes, time log, reflections. | 4 | [`README.md`](README.md) |
| **Bonus: deploy** | Dockerfile + Render blueprint. Live, no login needed: **<https://charades-label-explorer.onrender.com>**. Kept out of search engines; optional password lock built in. | 4 | [`Dockerfile`](Dockerfile), [`render.yaml`](render.yaml) |
| **Extras** | Plays sample videos with a playhead synced to the labels, fetched with HTTP range requests from inside the 16 GB video zip. 64 unit tests. CI on Ubuntu, macOS and a bare `python:3.8-slim`. | 4 | [`tests/`](tests/), [`ci.yml`](.github/workflows/ci.yml) |

---

## Quick start

Needs only **bash + Python 3.8+**. Setup needs network access; after that everything runs offline.

```bash
./setup.sh                 # labels (3.5 MB) + index + 16 sample videos (~27 MB)
./run.sh                   # web UI on http://127.0.0.1:8000 (next free port if busy)
```

Terminal use (same query engine as the web UI):

```bash
./run.sh search pour water --scene Kitchen          # full-text + filter
./run.sh search --action c092 --verb drink --min-length 20
./run.sh search --issue inverted_segment --format csv > flagged.csv
./run.sh show 46GP8                                 # one clip, ASCII action timeline
./run.sh stats                                      # dataset summary
./run.sh test                                       # 64 unit tests, offline
```

| Option | Effect |
|---|---|
| `./setup.sh --videos 0` (or `SAMPLE_VIDEOS=0`) | Skip sample videos (setup then takes ~5-15 s) |
| `./setup.sh --videos 50` | Fetch more sample videos (~1-2 MB each) |
| `PORT=9000 HOST=0.0.0.0 ./run.sh` | Bind elsewhere |
| `EXPLORER_AUTH=user:pass ./run.sh` | Require HTTP Basic auth (or `EXPLORER_PASSWORD=…`, user `reviewer`) |

## What you can do with it

- **Scan the dataset**: every clip in a table with its scene, duration, a
  mini timeline of all labeled segments (colored by verb), its labels, the
  annotator quality/relevance ratings, and QA flags.
- **Search**: free text across labels, scripts, annotator descriptions,
  objects, scenes and clip IDs. Words are ANDed, `"phrases"` stay together,
  and stemming means *cooks* finds *cooking*. Results are ranked by BM25 and
  show the matching text with highlights.
- **Filter**: facet filters show live counts for the current results, so you
  can see how many clips a filter would leave before clicking. Scene, split
  and QA issue are OR-filters; action, verb and object are AND-filters,
  because a clip has many of them. There are also duration, quality and
  verification filters.
- **Inspect a clip**: a drawer with a Gantt chart of every segment, all
  metadata, the script the actor was given, the annotators' descriptions, the
  objects, QA issues, and the raw label string exactly as it appears in the
  CSV. Use `j`/`k` to step through the results.
- **Watch labels against video**: for sample clips, a red playhead runs
  across the timeline while the video plays, and the labels active at that
  moment are highlighted. Clicking a segment jumps the video there. This is
  the fastest way to check whether a label is right.
- **Share and export**: every view is a URL (filters, page, open clip), and
  any filtered set exports as CSV/JSON. The `action_triplets` column
  round-trips to the source format.

### Label QA checks (results on the real data)

| Check | Clips flagged | Note |
|---|--:|---|
| `unverified` | 471 | Annotator could not confirm the video matches its script |
| `no_actions` | 223 | Video has no labeled segments at all |
| `missing_rating` | 84 | Quality or relevance rating is empty |
| `inverted_segment` | 4 | End is before start, e.g. `c071 18.00 10.00` in a 9 s clip (looks like a typo) |
| `segment_overrun` | 2 | Segment ends more than 2 s after the video |
| `unknown_class` / `malformed_segment` | 0 | Guards against bad or future files (covered by tests) |

I profiled the data before choosing thresholds. **29% of all segments end up
to 1.55 s after the clip's `length`.** That is a systematic offset, not an
annotation error, so flagging it would bury the real problems. The overrun
check uses a 2 s tolerance, and the UI clamps those segments for display.

## Dataset

**Charades v1** (Sigurdsson et al., *Hollywood in Homes*, 2016; Allen Institute for AI).
About 270 people filmed themselves acting out crowd-written scripts at home.
Other annotators then labeled each video with temporally localized actions.

| | |
|---|---|
| Clips | 9,848 (train 7,985 / test 1,863), 81 hours, avg 29.7 s |
| Labels | 66,500 action segments (`class start end`), 157 classes; each class maps to 1 of 33 verbs and 1 of 37 objects (or none) |
| Metadata | scene (16), subject ID, script, 1-4 free-text descriptions, objects, quality (1-7), relevance (1-7), verified flag, length |
| Format | 2 CSVs (`;`-separated multi-value fields) + 4 small taxonomy files |
| License | Non-commercial research use; **no redistribution** |

**Why this one.** The brief prefers video with real labels and metadata such
as duration and category. Charades gives the most to summarize: timed labels
(the core of video annotation work), a category (scene), duration, and
quality signals that make a QA view meaningful. The labels are a 3.5 MB
download from a stable official host, so setup is fast and reproducible.

**What else I looked at** (time-boxed search; all URLs checked from the command line):

| Dataset | Labels | Why not |
|---|---|---|
| Kinetics-400 | 1 action per 10 s YouTube clip | Thin metadata: one label, fixed 10 s duration, many videos deleted from YouTube |
| UCF101 / HMDB51 | Class = folder name | One class per clip, nothing else to summarize; the official HMDB51 link returned a 5 KB page instead of the archive |
| ActivityNet 1.3 | Temporal segments | The official annotation host did not respond |
| **Charades** | **Timed segments + scene + objects + descriptions + ratings** | **Chosen** |

## How it works

```
setup.sh ──► explorer/setup_data.py
               ├── fetch.py     download (SHA-256 pinned), TLS fallbacks, remote-zip range reads
               ├── charades.py  parse CSV + taxonomy → Clip records + QA flags
               └── index.py     SQLite: clip / segment / issue tables, FTS5 index, vocab table

run.sh ────► server.py  JSON API + static UI (web/)      ─┐
             cli.py     search / show / stats in terminal ─┴─► search.py  (one query layer)
```

| Endpoint | Returns |
|---|---|
| `GET /api/clips?q=&scene=&action=&verb=&object=&split=&verified=&min_length=&max_length=&min_quality=&issue=&has_video=&sort=&page=&page_size=` | page of clips + totals + facet counts |
| `GET /api/clips/<id>` | one clip with every annotation |
| `GET /api/meta` | dataset stats and filter vocabularies |
| `GET /api/export.csv?…` / `export.json?…` | all matching clips |
| `GET /videos/<id>.mp4` | sample video, with HTTP Range support for seeking |

### Design decisions

- **Standard library only.** The brief assumes only Python 3 and bash. On
  Debian/Ubuntu, `python3 -m venv` and `pip` are separate packages that are
  not installed by default, so any third-party dependency would break the
  "clean machine" rule.
- **SQLite FTS5 instead of Elasticsearch.** It uses the same idea
  (inverted index + BM25) but ships inside Python, so there is no service to
  run. If a Python build lacks FTS5, search falls back to substring matching
  instead of failing (tested).
- **Search-as-you-type despite stemming.** The index stores stems
  (*cooking* → *cook*), so a half-typed prefix like `cooki*` matches
  nothing. I keep a table of the original, unstemmed words and expand the
  last word before querying (`cooki` → `cooking OR cookies …`).
- **HTTPS on a fresh Mac.** Python from python.org fails HTTPS with
  `CERTIFICATE_VERIFY_FAILED` until *Install Certificates.command* is run.
  I hit this on my own machine. The downloader falls back to the OS CA
  bundle and then to `curl`, and never disables certificate checks.
- **Videos without a 16 GB download.** The zip's central directory (its file
  index) is read over HTTP range requests; each chosen video is then one
  range request, checked against its CRC-32. The client rejects any response
  larger than the range it asked for, so a server that ignores `Range` can't
  trigger a 16 GB download. The server also supports `Range`: these MP4s
  keep their index at the end of the file, so browsers must seek to play them.
- **Lenient parsing, strict reporting.** A bad row never crashes ingest or
  disappears. It becomes a QA flag you can filter on.
- **Security basics for an internal tool:** parameterized SQL, user text
  sanitized into FTS5 syntax, no `innerHTML` (dataset text can't become
  markup), a strict Content-Security-Policy, path-traversal checks, and
  constant-time auth comparison.

### Assumptions

- `length` in the CSV is the clip duration; segment times are seconds from
  the start of the video.
- Scene names are split from their parenthetical description:
  `Entryway (A hall that …)` becomes *Entryway* (the full text shows as a tooltip).
- The **Object** filter uses the 37 canonical objects from the action→object
  mapping, which is a controlled vocabulary. The free-form `objects` column
  (812 distinct values) is shown per clip and is searchable.
- Multiple descriptions are separated by `;` (as documented in the dataset
  README). The object class `None` means "no object".
- Only a sample of videos is fetched (license + size). Every clip's labels
  are fully browsable without video.

## Deployment (bonus)

> **Live instance:** <https://charades-label-explorer.onrender.com> (open, no login)  
> Free tier: if nobody has visited for 15 minutes, the first load takes ~1 minute while the server wakes up.

The demo is open so reviewers can use it straight from the link. Because the
Charades license allows evaluation use but not general re-hosting, it is kept
out of search engines (`robots.txt`, `X-Robots-Tag: noindex`) and serves only
the annotations plus 16 sample clips. To lock a deployment, set
`EXPLORER_PASSWORD` (user `reviewer`) or `EXPLORER_AUTH=user:password` for
HTTP Basic auth; `/healthz` always stays open for health checks.

- **Render (free tier):** New → Blueprint → select this repo. [`render.yaml`](render.yaml)
  runs `./setup.sh` at build time and `./run.sh serve` at start.
  Free instances sleep after 15 idle minutes; the first request then takes ~30-60 s.
- **Any container host** (Cloud Run, Fly.io, Railway): `docker build -t charades-explorer .`
  then `docker run -p 8080:8080 -e EXPLORER_AUTH=user:pass charades-explorer`.
  The image contains the data, so push it only to a private registry.

## Tests

`./run.sh test` runs 64 `unittest` tests in about 3 s, fully offline. They
use a hand-written fixture in the Charades format ([`tests/fixtures/`](tests/fixtures/charades_mini)),
because the license forbids committing real rows.

- **Parser:** quoted commas, empty or malformed or inverted segments,
  unknown classes, duplicate IDs, missing columns, coverage math.
- **Search:** every filter, AND/OR semantics, stemming, phrases, prefix
  expansion, hostile FTS syntax, pagination, facet counts, the non-FTS
  fallback.
- **Server:** routes, 400/404 handling, CSV export, gzip, CSP headers, path
  traversal, byte ranges (including 416), Basic auth, noindex/robots.txt.
- **Fetch:** remote-zip extraction with one request per member, CRC
  corruption detection, refusing servers that ignore `Range`, checksum
  verification.

CI ([`ci.yml`](.github/workflows/ci.yml)) runs `setup.sh` + `run.sh` from
scratch on Ubuntu and macOS, plus a bare `python:3.8-slim` container (no
curl), to prove the clean-machine requirement.

## Time log

<!-- Fill in your real times before submitting. -->

| Section | What | Time |
|---|---|--:|
| 1. Dataset | Search + compare candidates, profile Charades, checksum-pinned download | ~__ min |
| 2. Ingest + summary | Parser, QA checks, fixture tests | ~__ min |
| 3. Search + filter | SQLite FTS5 index, query layer, facets, CLI | ~__ min |
| 4. Web UI | API server, table, detail drawer, timelines | ~__ min |
| Extras | Sample videos via range requests, playback sync | ~__ min |
| 5. README + deploy | Docs, Dockerfile, Render, CI | ~__ min |

## Reflections

**What would I improve with two more hours?**

<!-- Rewrite in your own words before submitting. -->

I would fetch videos on demand, so any of the 9,848 clips can play the
moment its drawer opens instead of a fixed sample of 16. The range-request
code already does the hard part. Next I'd close the QA loop: let a reviewer
fix a flagged segment (drag its edges on the timeline) and export a corrected
CSV, which is what an internal labeling tool is ultimately for. I would also
add browser tests (e.g. Playwright) for the UI, which is currently only
tested through its API. Finally I'd make facet counting faster on large
result sets by computing the matching IDs once per request.

**What's one thing I didn't already know how to do, and how did I figure it out?**

> ✏️ **DRAFT, delete this line after rewriting it as your own experience.**

I didn't know you could pull one file out of a remote zip without
downloading it. I read how the zip format is laid out: a central directory
at the end lists every file's offset, and each file sits behind a small
local header. Python's `zipfile` accepts any seekable file object, so I
wrote a file object whose reads become HTTP range requests. I checked the
approach by extracting one video and comparing its CRC-32 against the one
stored in the directory. I then wrote a unit test that builds a zip in
memory, so the logic is tested without network access.

## Project layout

```
setup.sh, run.sh        the only entry points
explorer/               Python package (stdlib only)
  config.py             paths, URLs, pinned checksum
  fetch.py              downloads with TLS fallbacks, remote zip reader
  charades.py           label-format parser + QA checks
  index.py              SQLite schema, FTS5, vocab table
  search.py             query parsing, filters, facets, ranking
  server.py             HTTP API, static files, video ranges, auth
  cli.py, export.py     terminal interface, CSV export
web/                    index.html, app.js, style.css (no build step)
tests/                  64 unittest tests + synthetic fixture
Dockerfile, render.yaml deployment
```

*Data: Charades © 2016 Allen Institute for AI, used under its non-commercial
license and never redistributed here.*
