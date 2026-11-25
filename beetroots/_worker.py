"""Private line-oriented protocol used by Matcher."""

from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, replace
from urllib.parse import urlsplit

from beets import __version__ as beets_version
from beets import config as beets_config
from beets.autotag.distance import (
    Distance,
    get_track_length_grace,
    get_track_length_max,
)

from .api import MatcherConfig
from .engine import Engine, MatchRun
from .lookups import LookupCache, MusicBrainz, MusicBrainzLimiter, Providers, digest
from .schema import BEETS_VERSION, MatchResult, validate


def number(name: str, value: float, *, positive: bool = False) -> float:
    if (
        type(value) not in (float, int)
        or not math.isfinite(value)
        or value < 0
        or (positive and not value)
    ):
        raise ValueError(f"Invalid numeric configuration for {name}")
    return float(value)


def initialize(config: dict, cache_path: str, rate_path: str, refresh: bool) -> Engine:
    if beets_version != BEETS_VERSION:
        raise ValueError(
            f"Beetroots requires beets {BEETS_VERSION}, found {beets_version}"
        )
    settings = MatcherConfig(**config)
    if type(settings.search_limit) is not int or not 1 <= settings.search_limit <= 100:
        raise ValueError("search_limit must be an integer from 1 to 100")
    parsed = urlsplit(settings.musicbrainz_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise ValueError("musicbrainz_url must be an HTTP(S) API root")
    if type(settings.youtube_music) is not bool:
        raise ValueError("youtube_music must be boolean")
    for key in ("user_agent_contact", "spotify_client_id", "spotify_client_secret"):
        value = getattr(settings, key)
        if value is not None and (
            not isinstance(value, str) or any(char in value for char in "\r\n")
        ):
            raise ValueError(f"Invalid configuration for {key}")
    interval = number("musicbrainz_interval", settings.musicbrainz_interval)
    if parsed.hostname in {"musicbrainz.org", "www.musicbrainz.org"}:
        interval = max(interval, 1.0)
    settings = replace(
        settings,
        musicbrainz_url=settings.musicbrainz_url.rstrip("/"),
        musicbrainz_interval=interval,
        timeout=number("timeout", settings.timeout, positive=True),
    )

    beets_config.clear()
    beets_config.read(user=False)
    beets_config["raise_on_error"].set(True)
    beets_config["musicbrainz"]["search_limit"].set(settings.search_limit)
    weights = beets_config["match"]["distance_weights"]
    known_weights = set(weights.keys())
    for key, value in settings.distance_weights.items():
        if key not in known_weights:
            raise ValueError(f"Unknown beets distance weight {key!r}")
        weights[key].set(number(key, value))
    if settings.track_length_grace is not None:
        beets_config["match"]["track_length_grace"].set(
            number("track_length_grace", settings.track_length_grace)
        )
    if settings.track_length_max is not None:
        beets_config["match"]["track_length_max"].set(
            number("track_length_max", settings.track_length_max, positive=True)
        )
    effective = {
        **asdict(settings),
        "distance_weights": {
            key: float(value) for key, value in Distance._weights.items()
        },
        "track_length_grace": float(get_track_length_grace()),
        "track_length_max": float(get_track_length_max()),
        "raise_on_error": beets_config["raise_on_error"].get(bool),
        "beets": beets_version,
        "spotify_client_id": digest(settings.spotify_client_id)
        if settings.spotify_client_id and settings.spotify_client_secret
        else None,
    }
    del effective["spotify_client_secret"]
    cache = LookupCache(cache_path, refresh)
    limiter = MusicBrainzLimiter(rate_path, interval)
    return Engine(
        settings,
        MusicBrainz(settings, cache, limiter),
        Providers(settings, cache),
        digest(effective),
    )


def send(value: dict) -> None:
    print(json.dumps(value, sort_keys=True, allow_nan=False), flush=True)


def invalid(engine: Engine, track: object, error: str) -> MatchResult:
    record_id = track.get("record_id") if isinstance(track, dict) else None
    return MatchResult(
        record_id if type(record_id) in (str, int) else None,
        "invalid_input",
        engine.fingerprint,
        issues=[{"path": "input", "code": "invalid_input", "detail": error}],
    )


def reject_constant(token: str):
    raise ValueError(f"Invalid JSON constant {token}")


def handle(engine: Engine, line: str) -> MatchResult:
    track = None
    try:
        track = json.loads(line, parse_constant=reject_constant)
        json.dumps(track, allow_nan=False)
    except (ValueError, RecursionError):
        return invalid(engine, track, "Malformed JSON")
    if error := validate(track):
        return invalid(engine, track, error)
    run = MatchRun(engine, track)
    try:
        return run.run()
    except Exception as exc:  # noqa: BLE001
        # Keep the line protocol alive even if a provider returns data
        # outside its documented schema.
        print(f"Beetroots match failed ({type(exc).__name__})", file=sys.stderr)
        run.issue(
            "match", "lookup_error", f"Unable to complete match ({type(exc).__name__})"
        )
        return run.result("lookup_error")


def main() -> None:
    try:
        engine = initialize(**json.loads(sys.stdin.readline()))
    except Exception as exc:  # noqa: BLE001
        send({"error": f"Matcher initialization failed ({type(exc).__name__}): {exc}"})
        return
    send({"config_fingerprint": engine.fingerprint})
    for line in sys.stdin:
        send(asdict(handle(engine, json.loads(line))))


if __name__ == "__main__":
    main()
