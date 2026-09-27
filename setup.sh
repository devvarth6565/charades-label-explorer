#!/usr/bin/env bash
# One-time setup: downloads the Charades annotations (3.5 MB, checksum-verified)
# and builds the local search index. Needs only bash + Python 3.8+; no pip.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 not found. Install Python 3.8 or newer." >&2
  exit 1
fi
python3 - <<'PY'
import sys
if sys.version_info < (3, 8):
    sys.exit("error: Python 3.8+ required, found %s" % sys.version.split()[0])
PY

exec python3 -m explorer setup "$@"
