"""Shared HTTP response cache and provider adapters."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import cached_property, partial
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from beetsplug._utils.musicbrainz import MusicBrainzAPI
from beetsplug._utils.requests import HTTPNotFoundError

from .api import MatcherConfig
from .schema import VERSION


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class LookupFailure(Exception):
    def __init__(self, detail: str, status: int | None = None):
        super().__init__(detail)
        self.status = status


class LookupCache:
    def __init__(self, path: str, refresh: bool):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=60)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS responses "
            "(key TEXT PRIMARY KEY, fetched REAL NOT NULL, value TEXT NOT NULL)"
        )
        self.since = time.time() if refresh else 0

    def get(self, key: str, fetch: Callable[[], dict | None]) -> dict | None:
        row = self.db.execute(
            "SELECT value FROM responses WHERE key = ? AND fetched >= ?",
            (key, self.since),
        ).fetchone()
        if row is not None:
            return json.loads(row[0])
        value = fetch()
        with self.db:
            self.db.execute(
                "INSERT INTO responses VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET fetched=excluded.fetched, value=excluded.value",
                (key, time.time(), json.dumps(value)),
            )
        return value


class MusicBrainzLimiter:
    def __init__(self, path: str, interval: float):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=60, isolation_level=None)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS requests (host TEXT PRIMARY KEY, started REAL NOT NULL)"
        )
        self.interval = interval

    @contextmanager
    def request(self, host: str) -> Iterator[None]:
        if not self.interval:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT started FROM requests WHERE host = ?", (host,)
            ).fetchone()
            if row:
                time.sleep(
                    max(0, min(self.interval, row[0] + self.interval - time.time()))
                )
            self.db.execute(
                "INSERT INTO requests VALUES (?, ?) "
                "ON CONFLICT(host) DO UPDATE SET started=excluded.started",
                (host, time.time()),
            )
            yield
        finally:
            self.db.commit()


def json_request(
    url: str, timeout: float, *, headers: dict | None = None, data: bytes | None = None
) -> dict | None:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 404:
            return None
        raise LookupFailure(f"HTTP {exc.code}", exc.code) from exc
    except (OSError, ValueError) as exc:
        raise LookupFailure(type(exc).__name__) from exc
    if not isinstance(payload, dict):
        raise LookupFailure("Expected a JSON object from the provider")
    return payload


ALL_RELATIONSHIPS = (
    "area-rels+artist-rels+event-rels+genre-rels+instrument-rels+label-rels+"
    "place-rels+recording-rels+release-rels+release-group-rels+series-rels+url-rels+work-rels"
)
PUBLIC_FIELDS = "aliases+annotation+tags+genres"
RECORDING_CORE = "artists+artist-credits+aliases+isrcs+work-rels"
RELEASE_CORE = "recordings+artist-credits+release-groups+labels+discids"
ENTITY_INCLUDES = {
    "recording": f"{RECORDING_CORE}+releases+release-groups+ratings+work-level-rels",
    "release": RELEASE_CORE,
    "release-group": "artist-credits+ratings",
    "artist": "ratings",
    "work": "ratings",
    "label": "ratings",
    "area": "",
    "instrument": "ratings",
    "place": "",
}


class MusicBrainz(MusicBrainzAPI):
    """Use the beets search API with a shared cache and retain raw JSON."""

    def __init__(
        self, settings: MatcherConfig, cache: LookupCache, limiter: MusicBrainzLimiter
    ):
        self.root = settings.musicbrainz_url
        self.cache = cache
        self.limiter = limiter
        self.timeout = settings.timeout
        self.user_agent = f"Beetroots/{VERSION}"
        if settings.user_agent_contact:
            self.user_agent += f" ({settings.user_agent_contact})"

    @property
    def api_root(self) -> str:
        return self.root

    def raw(self, endpoint: str, **params: str | int) -> dict | None:
        params["fmt"] = "json"
        url = f"{self.root}/{endpoint}?{urlencode(sorted(params.items()))}"

        def fetch() -> dict | None:
            with self.limiter.request(urlsplit(self.root).hostname):
                return json_request(
                    url, self.timeout, headers={"User-Agent": self.user_agent}
                )

        return self.cache.get(url, fetch)

    def get_json(self, url: str, *, params: dict) -> dict:
        resource = url.removeprefix(f"{self.root}/")
        payload = self.raw(resource, **params)
        if payload is None:
            raise HTTPNotFoundError()
        return payload

    def recording_core(self, mbid: str) -> dict | None:
        return self.raw(f"recording/{mbid}", inc=RECORDING_CORE)

    def recordings_by_credit(
        self,
        artist: str,
        title: str,
        *,
        limit: int,
        duration: tuple[int, int] | None = None,
        video: bool | None = None,
    ) -> list[dict]:
        def phrase(value: str) -> str:
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

        credit = phrase(artist)
        query = f"recording:{phrase(title)} AND (artistname:{credit} OR creditname:{credit})"
        if duration:
            lower, upper = duration
            query += f" AND dur:[{lower} TO {upper}]"
        if video is not None:
            query += f" AND video:{str(video).lower()}"
        result = self.raw("recording", query=query, limit=limit)
        if result is None:
            raise HTTPNotFoundError()
        return result["recordings"]

    def release_core(self, mbid: str) -> dict | None:
        return self.raw(f"release/{mbid}", inc=RELEASE_CORE)

    def entity(self, kind: str, mbid: str) -> dict | None:
        parts = (ENTITY_INCLUDES[kind], PUBLIC_FIELDS, ALL_RELATIONSHIPS)
        return self.raw(f"{kind}/{mbid}", inc="+".join(filter(None, parts)))

    def pages(self, entity: str, **params: str) -> Iterator[dict]:
        offset = 0
        count_key = f"{entity}-count"
        while True:
            page = self.raw(entity, limit=100, offset=offset, **params)
            if page is None:
                raise LookupFailure("MusicBrainz returned HTTP 404 for a page", 404)
            entries = page[f"{entity}s"]
            yield from entries
            offset += len(entries)
            count = page.get(count_key, page.get("count"))
            if count is not None and offset >= count:
                return
            if not entries:
                if count is not None:
                    raise LookupFailure("Paging ended before all results were returned")
                return
            if count is None and len(entries) < 100:
                return

    def search_pages(self, **filters: str) -> Iterator[dict]:
        query = " ".join(
            self.format_search_term(key, value) for key, value in filters.items()
        )
        yield from self.pages("recording", query=query)


class Providers:
    def __init__(self, settings: MatcherConfig, cache: LookupCache):
        self.settings = settings
        self.cache = cache
        self.token: tuple[str, float] | None = None

    @cached_property
    def youtube(self):
        import requests
        from ytmusicapi import YTMusic

        session = requests.Session()
        session.request = partial(session.request, timeout=self.settings.timeout)
        return YTMusic(requests_session=session)

    def spotify(self, track_id: str) -> dict | None:
        settings = self.settings

        def fetch() -> dict | None:
            for attempt in range(2):
                if self.token is None or self.token[1] <= time.monotonic():
                    credentials = (
                        f"{settings.spotify_client_id}:{settings.spotify_client_secret}"
                    )
                    auth = base64.b64encode(credentials.encode()).decode()
                    payload = json_request(
                        "https://accounts.spotify.com/api/token",
                        settings.timeout,
                        headers={
                            "Authorization": f"Basic {auth}",
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                        data=b"grant_type=client_credentials",
                    )
                    if not payload or not payload.get("access_token"):
                        raise LookupFailure("No Spotify access token")
                    self.token = (
                        payload["access_token"],
                        time.monotonic() + payload["expires_in"],
                    )
                try:
                    return json_request(
                        f"https://api.spotify.com/v1/tracks/{track_id}",
                        settings.timeout,
                        headers={"Authorization": f"Bearer {self.token[0]}"},
                    )
                except LookupFailure as exc:
                    if exc.status != 401 or attempt:
                        raise
                    self.token = None
            raise AssertionError("Unreachable Spotify token retry")

        return self.cache.get(
            f"spotify:{digest(settings.spotify_client_id)}:{track_id}", fetch
        )

    def watch(self, video_id: str) -> dict | None:
        def fetch() -> dict | None:
            try:
                payload = self.youtube.get_watch_playlist(videoId=video_id, limit=1)
                return next(
                    (
                        track
                        for track in payload.get("tracks") or []
                        if track.get("videoId") == video_id
                    ),
                    None,
                )
            except Exception as exc:
                raise LookupFailure(type(exc).__name__) from exc

        return self.cache.get(f"youtube-music:watch:{video_id}", fetch)

    def youtube_album(self, album_id: str) -> dict | None:
        def fetch() -> dict | None:
            try:
                return self.youtube.get_album(album_id)
            except Exception as exc:
                raise LookupFailure(type(exc).__name__) from exc

        return self.cache.get(f"youtube-music:album:{album_id}", fetch)
