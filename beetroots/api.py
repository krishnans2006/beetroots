from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock
from typing import Self

from .schema import MatchResult, TrackInput


def default_cache_path() -> Path:
    cache_home = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(cache_home) / "beetroots" / "lookups.sqlite3"


@dataclass(frozen=True)
class MatcherConfig:
    distance_weights: dict[str, float] = field(default_factory=dict)
    track_length_grace: float | None = None
    track_length_max: float | None = None
    search_limit: int = 5
    musicbrainz_url: str = "https://musicbrainz.org/ws/2"
    musicbrainz_interval: float = 1.0
    user_agent_contact: str = "https://github.com/krishnans2006/beetroots"
    timeout: float = 15.0
    spotify_client_id: str | None = field(default=None, repr=False)
    spotify_client_secret: str | None = field(default=None, repr=False)
    youtube_music: bool = True


class Matcher:
    """A persistent worker with isolated beets configuration and a shared lookup cache."""

    def __init__(
        self,
        config: MatcherConfig | None = None,
        *,
        cache_path: str | Path | None = None,
        refresh: bool = False,
    ) -> None:
        self._lock = Lock()
        settings = asdict(config or MatcherConfig())
        for key in ("spotify_client_id", "spotify_client_secret"):
            if settings[key] is None:
                settings[key] = os.environ.get(key.upper())
        request = json.dumps(
            {
                "config": settings,
                "cache_path": str(
                    Path(cache_path).expanduser()
                    if cache_path is not None
                    else default_cache_path()
                ),
                "rate_path": str(
                    default_cache_path().with_name("musicbrainz-rate.sqlite3")
                ),
                "refresh": refresh,
            },
            allow_nan=False,
        )
        self._process = subprocess.Popen(
            [sys.executable, "-m", "beetroots._worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            response = self._exchange(request)
            if "error" in response:
                raise ValueError(response["error"])
            self.config_fingerprint: str = response["config_fingerprint"]
        except BaseException:
            self.close()
            raise

    def _exchange(self, request: str) -> dict:
        process = self._process
        if process.stdin.closed:
            raise RuntimeError("Matcher is closed")
        process.stdin.write(request + "\n")
        process.stdin.flush()
        response = process.stdout.readline()
        if not response:
            raise RuntimeError("Beetroots worker exited without a response")
        return json.loads(response)

    def _match_json(self, row: str) -> MatchResult:
        with self._lock:
            return MatchResult(**self._exchange(json.dumps(row)))

    def match(self, track: TrackInput) -> MatchResult:
        try:
            row = json.dumps(track, allow_nan=False)
        except (TypeError, ValueError, OverflowError):
            row = "null"
        return self._match_json(row)

    def close(self) -> None:
        with self._lock:
            process = self._process
            if process.stdin.closed:
                return
            process.stdin.close()
            process.stdout.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
