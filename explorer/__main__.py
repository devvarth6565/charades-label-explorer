"""Command dispatcher: ``python3 -m explorer <command> [args]``."""
from __future__ import annotations

import sys

USAGE = """usage: python3 -m explorer <command> [options]

commands:
  setup     download + verify the dataset and build the search index
"""


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    command, rest = argv[0], argv[1:]
    if command == "setup":
        from .setup_data import main as run
    else:
        print(f"unknown command: {command}\n\n{USAGE}", file=sys.stderr)
        return 2
    return run(rest) or 0


if __name__ == "__main__":
    sys.exit(main())
