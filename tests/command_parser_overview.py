#!/usr/bin/env python3
"""Print the generic parser view of a shell command as JSON."""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from command_parser import BashCommandParser


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Show how command_parser.py interprets a shell command."
    )
    parser.add_argument(
        "command",
        help="Complete shell command passed as one quoted argument",
    )
    parser.add_argument(
        "--engine",
        choices=("bashlex", "shlex"),
        help="Force a parser engine instead of using automatic fallback",
    )
    args = parser.parse_args()

    result = BashCommandParser.parse(args.command, force_engine=args.engine)
    print(json.dumps(result.to_dict(), indent=2))


if __name__ == "__main__":
    main()
