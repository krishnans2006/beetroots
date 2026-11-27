"""Convert a custom YouTube Music My Activity export to ListenBrainz imports."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from beetroots import Matcher, MatcherConfig, TrackInput, __version__
from beetroots.evidence import YOUTUBE_ID, duration_ms, provider_ids

if __package__:
    from . import listenbrainz
else:
    import listenbrainz


def text(value: object) -> str:
    return value.strip() if isinstance(value, str) and "\0" not in value else ""


def event_timestamp(row: dict) -> int:
    micros = row.get("timestampMicroseconds")
    if type(micros) is int or (
        isinstance(micros, str) and micros.isascii() and micros.isdecimal()
    ):
        timestamp = int(micros) // 1_000_000
    else:
        when = datetime.fromisoformat(text(row.get("timeUTC")))
        if when.tzinfo is None:
            raise ValueError("event timestamp requires a timezone")
        timestamp = int(when.timestamp())
    if not listenbrainz.MIN_LISTENED_AT <= timestamp <= int(time.time()):
        raise ValueError("event timestamp is outside the ListenBrainz date range")
    return timestamp


def youtube_play(row: dict, number: int) -> tuple[TrackInput, dict]:
    video_id = text(row.get("videoId"))
    url = text(row.get("url"))
    _, url_id = provider_ids(url)
    if not YOUTUBE_ID.fullmatch(video_id):
        video_id = url_id or ""
    if not YOUTUBE_ID.fullmatch(video_id):
        raise ValueError("missing or invalid YouTube video ID")
    if url_id and url_id != video_id:
        raise ValueError("YouTube video ID disagrees with the event URL")
    origin = f"https://music.youtube.com/watch?v={video_id}"

    track: TrackInput = {
        "record_id": number,
        "source": "youtube_music",
        "video_title": text(row.get("title")),
        "youtube_video_id": video_id,
        "source_urls": [origin],
        "listened_at": event_timestamp(row),
    }
    channels = row.get("channels")
    if isinstance(channels, list):
        names = [
            name
            for channel in channels
            if isinstance(channel, dict) and (name := text(channel.get("name")))
        ]
        if names:
            track["uploader"] = names[0]
    length = duration_ms(row.get("durationLabel"))
    if length and length <= listenbrainz.MAX_DURATION_MS:
        track["duration_ms"] = length

    context = {
        "media_player": text(row.get("app")) or "YouTube Music",
        "music_service": "music.youtube.com",
        "origin_url": origin,
        "submission_client": "Beetroots YouTube Music Importer",
        "submission_client_version": __version__,
    }
    devices = row.get("devices")
    if isinstance(devices, list) and (
        names := [device for value in devices if (device := text(value))]
    ):
        context["youtube_devices"] = names
    progress = row.get("videoProgressPercent")
    if type(progress) is int and 0 <= progress <= 100:
        context["youtube_progress_percent"] = progress
    return track, context


def listens(rows, matcher, args):
    prepared = {}
    for number, row in enumerate(rows, args.start_row):
        if not isinstance(row, dict):
            print(f"Row {number}: expected a history object", file=sys.stderr)
            continue
        try:
            track, context = youtube_play(row, number)
        except (ValueError, OverflowError, OSError) as exc:
            print(f"Row {number}: {exc}", file=sys.stderr)
            continue
        identity = (
            track["youtube_video_id"],
            track["video_title"],
            track.get("uploader"),
            track.get("duration_ms"),
        )
        tags = prepared.get(identity)
        if tags is None:
            result = matcher.match(track)
            tags = result.tags_for(
                track, min_confidence=args.min_confidence, require_song_agreement=True
            )
            # Event time and playback context do not affect recording identity.
            if not any(issue["code"] == "lookup_error" for issue in result.issues):
                prepared[identity] = tags
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
        "--start-row", type=int, default=1, help="one-based first history event"
    )
    parser.add_argument("--limit", type=int, help="maximum history events to process")
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=listenbrainz.DEFAULT_MIN_CONFIDENCE,
        help="minimum confidence for applying proposed tags (default: %(default)s)",
    )
    parser.add_argument("--config", type=Path, help="Beetroots MatcherConfig JSON file")
    parser.add_argument("--cache", type=Path, help="Beetroots lookup cache")
    args = parser.parse_args()
    if args.start_row < 1 or (args.limit is not None and args.limit < 1):
        parser.error("--start-row and --limit must be positive")
    if not 0 <= args.min_confidence <= 1:
        parser.error("--min-confidence must be between 0 and 1")
    try:
        history = json.loads(args.history.read_text(encoding="utf-8-sig"))
        if not isinstance(history, dict) or not isinstance(
            rows := history.get("events"), list
        ):
            raise TypeError("expected a YouTube Music export with an events array")
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
        f"{'Accepted' if args.submit else 'Prepared'} {delivered} listens from {len(rows)} history events",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
