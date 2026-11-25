import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .api import Matcher, MatcherConfig


def main() -> None:
    parser = argparse.ArgumentParser(prog="beetroots")
    commands = parser.add_subparsers(dest="command", required=True)
    match = commands.add_parser("match", help="read and write one JSON object per line")
    match.add_argument(
        "--config", type=Path, help="JSON object of MatcherConfig settings"
    )
    match.add_argument("--cache", type=Path, help="path to the shared lookup cache")
    match.add_argument(
        "--refresh", action="store_true", help="refresh cached lookups used in this run"
    )
    args = parser.parse_args()
    try:
        config = (
            MatcherConfig(**json.loads(args.config.read_text(encoding="utf-8")))
            if args.config
            else None
        )
        matcher = Matcher(config, cache_path=args.cache, refresh=args.refresh)
    except (OSError, TypeError, ValueError) as exc:
        match.error(str(exc))
    with matcher:
        for line in sys.stdin.buffer:
            try:
                row = line.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError:
                row = ""
            result = matcher._match_json(row)
            print(
                json.dumps(asdict(result), allow_nan=False, sort_keys=True), flush=True
            )


if __name__ == "__main__":
    main()
