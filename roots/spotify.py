"""Convert one Spotify streaming-history file to ListenBrainz imports."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from beetroots import Matcher, MatcherConfig, TrackInput, __version__

if __package__:
    from . import listenbrainz
else:
    import listenbrainz

DEFAULT_MIN_CONFIDENCE = listenbrainz.DEFAULT_MIN_CONFIDENCE
TRACK_URI = re.compile(r"spotify:track:([A-Za-z0-9]{22})")
METADATA = {
    "title": ("master_metadata_track_name", "trackName"),
    "artist": ("master_metadata_album_artist_name", "artistName"),
    "album": ("master_metadata_album_album_name", "albumName"),
}
NON_MUSIC = (
    "spotify_episode_uri",
    "episode_name",
    "episode_show_name",
    "audiobook_uri",
    "audiobook_title",
    "audiobook_chapter_uri",
    "audiobook_chapter_title",
)


def text(value: object) -> str:
    return value.strip() if isinstance(value, str) and "\0" not in value else ""


def spotify_play(row: dict, number: int, include_ip: bool) -> tuple[TrackInput, dict]:
    elapsed = row.get("ms_played", row.get("msPlayed"))
    if type(elapsed) is not int or not 0 < elapsed <= listenbrainz.MAX_DURATION_MS:
        raise ValueError("missing or invalid playback duration")
    end = text(row.get("ts") or row.get("endTime"))
    if "T" not in end and " " not in end:
        raise ValueError("missing or invalid stream-end timestamp")
    try:
        ended = datetime.fromisoformat(end)
    except ValueError as exc:
        raise ValueError("invalid stream-end timestamp") from exc
    start = (ended if ended.tzinfo else ended.replace(tzinfo=UTC)) - timedelta(
        milliseconds=elapsed
    )
    timestamp = int(start.timestamp())
    if not listenbrainz.MIN_LISTENED_AT <= timestamp <= int(time.time()):
        raise ValueError("playback start is outside the ListenBrainz date range")

    track: TrackInput = {
        "record_id": number,
        "source": "spotify",
        "listened_at": timestamp,
        "elapsed_ms": elapsed,
    }
    for field, (extended, basic) in METADATA.items():
        if value := text(row.get(extended)) or text(row.get(basic)):
            track[field] = value
    context = {
        "media_player": "Spotify",
        "music_service": "spotify.com",
        "submission_client": "Beetroots Spotify Importer",
        "submission_client_version": __version__,
    }
    if match := TRACK_URI.fullmatch(uri := text(row.get("spotify_track_uri"))):
        track["spotify_uri"] = uri
        context["spotify_id"] = context["origin_url"] = (
            f"https://open.spotify.com/track/{match[1]}"
        )
    for field in ("platform", "conn_country", "reason_start", "reason_end"):
        if value := text(row.get(field)):
            context[f"spotify_{field}"] = value
    for field in ("shuffle", "skipped", "offline", "incognito_mode"):
        if isinstance(value := row.get(field), bool):
            context[f"spotify_{field}"] = value
    if include_ip and (
        address := text(row.get("ip_addr") or row.get("ip_addr_decrypted"))
    ):
        try:
            context["spotify_ip_addr"] = str(ipaddress.ip_address(address))
        except ValueError:
            pass
    return track, context


def import_tags(track: TrackInput, result, min_confidence: float) -> dict:
    return result.tags_for(
        track, min_confidence=min_confidence, require_song_agreement=True
    )


def listens(rows, matcher, args):
    for number, row in enumerate(rows, args.start_row):
        if not isinstance(row, dict):
            print(f"Row {number}: expected a history object", file=sys.stderr)
            continue
        if (
            any(row.get(key) for key in NON_MUSIC)
            or row.get("ms_played", row.get("msPlayed")) == 0
        ):
            continue
        if row.get("incognito_mode") is True and not args.include_incognito:
            continue
        try:
            track, context = spotify_play(row, number, args.include_ip)
        except (ValueError, OverflowError, OSError) as exc:
            print(f"Row {number}: {exc}", file=sys.stderr)
            continue
        if not any(track.get(field) for field in ("title", "artist", "spotify_uri")):
            print(f"Row {number}: missing track metadata", file=sys.stderr)
            continue
        tags = import_tags(track, matcher.match(track), args.min_confidence)
        if payload := listenbrainz.listen(track, tags, context):
            yield number, payload
        else:
            print(
                f"Row {number}: missing artist or title after matching", file=sys.stderr
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("history", type=Path)
    parser.add_argument(
        "--submit",
        metavar="API_URL",
        help="post using LISTENBRAINZ_TOKEN; otherwise print JSON Lines",
    )
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument(
        "--start-row", type=int, default=1, help="one-based first history row"
    )
    parser.add_argument("--limit", type=int, help="maximum history rows to process")
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=DEFAULT_MIN_CONFIDENCE,
        help="minimum confidence for applying proposed tags (default: %(default)s)",
    )
    parser.add_argument("--include-incognito", action="store_true")
    parser.add_argument("--include-ip", action="store_true")
    parser.add_argument("--config", type=Path, help="Beetroots MatcherConfig JSON file")
    parser.add_argument("--cache", type=Path, help="Beetroots lookup cache")
    args = parser.parse_args()
    if args.start_row < 1 or (args.limit is not None and args.limit < 1):
        parser.error("--start-row and --limit must be positive")
    if not 0 <= args.min_confidence <= 1:
        parser.error("--min-confidence must be between 0 and 1")
    try:
        rows = json.loads(args.history.read_text(encoding="utf-8-sig"))
        if not isinstance(rows, list):
            raise TypeError("expected a JSON array of Spotify history entries")
        start = args.start_row - 1
        rows = rows[start : start + args.limit if args.limit is not None else None]
        config = (
            MatcherConfig(**json.loads(args.config.read_text(encoding="utf-8-sig")))
            if args.config
            else None
        )
        with Matcher(config, cache_path=args.cache) as matcher:
            delivered = listenbrainz.write_imports(
                listens(rows, matcher, args),
                batch_size=args.batch_size,
                url=args.submit,
                token=os.environ.get("LISTENBRAINZ_TOKEN", "").strip(),
            )
    except (OSError, RuntimeError, TypeError, ValueError, RecursionError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(
        f"{'Accepted' if args.submit else 'Prepared'} {delivered} listens from {len(rows)} history rows",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
