#!/usr/bin/env bash
# Run the explorer. Default: start the web UI on http://127.0.0.1:8000
#
#   ./run.sh                     web UI (HOST/PORT env vars override)
#   ./run.sh search [text] [...] search from the terminal (--help for filters)
#   ./run.sh show <CLIP_ID>      one clip with an ASCII action timeline
#   ./run.sh stats               dataset summary
#   ./run.sh test                run the unit tests
set -euo pipefail
cd "$(dirname "$0")"

command="${1:-serve}"
[ $# -gt 0 ] && shift

if [ "$command" = "test" ]; then
  exec python3 -m unittest discover -s tests -t . "$@"
fi

if [ ! -f "${EXPLORER_DATA_DIR:-data}/charades.db" ]; then
  echo "No index found. Run ./setup.sh first (downloads 3.5 MB, takes ~15 s)." >&2
  exit 1
fi

case "$command" in
  serve|search|show|stats) exec python3 -m explorer "$command" "$@" ;;
  -h|--help|help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) echo "unknown command: $command (try ./run.sh help)" >&2; exit 2 ;;
esac
