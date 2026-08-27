#!/usr/bin/env python3
"""Small schema-aware CLI used to contrast CLI and shell-parser views."""

import argparse
import json


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze")
    analyze.add_argument("-t", "--timeout", type=int, required=True)
    analyze.add_argument("subject")

    print(json.dumps(vars(parser.parse_args()), sort_keys=True))


if __name__ == "__main__":
    main()
