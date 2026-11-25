"""Build and send ListenBrainz import requests."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterable, Iterator
from urllib.parse import urlsplit

import requests

from beetroots import TrackInput, __version__

ENDPOINT = "/submit-listens"
MAX_LISTENS = 1_000
MAX_LISTEN_BYTES = 10_240
MAX_REQUEST_BYTES = 10_240_000
MAX_DURATION_MS = 2_073_600_000
MIN_LISTENED_AT = 1_033_430_400
PREFIX = b'{"listen_type":"import","payload":['
SUFFIX = b"]}"


def listen(track: TrackInput, tags: dict, context: dict) -> dict | None:
    if not (tags.get("artist") and tags.get("title")):
        return None
    metadata = {"artist_name": tags["artist"], "track_name": tags["title"]}
    if album := tags.get("album"):
        metadata["release_name"] = album
    info = dict(context)
    for field in (
        "artist_mbids",
        "recording_mbid",
        "release_mbid",
        "release_group_mbid",
    ):
        if value := tags.get(field):
            info[field] = value
    for field, key in (
        ("release_track_mbid", "track_mbid"),
        ("album_artist", "release_artist_name"),
    ):
        if value := tags.get(field):
            info[key] = value
    for field, key in (
        ("artist_credits", "artist_names"),
        ("album_artists", "release_artist_names"),
    ):
        if credits := tags.get(field):
            names = [credit.get("name") for credit in credits]
            if all(names):
                info[key] = names
    for field, key in (("isrcs", "isrc"), ("labels", "label")):
        if values := tags.get(field):
            info[key] = values[0]
    if work := tags.get("work_mbid"):
        info["work_mbids"] = [work]
    number = tags.get("track_number_text") or tags.get("track_number")
    if number is not None:
        info["tracknumber"] = str(number)
    duration = track.get("duration_ms")
    if type(duration) is not int or not 0 < duration <= MAX_DURATION_MS:
        duration = tags.get("duration_ms")
    if type(duration) is int and 0 < duration <= MAX_DURATION_MS:
        info["duration_ms"] = duration
    if genres := [g for g in tags.get("genres", ()) if 0 < len(g) <= 64]:
        info["tags"] = genres[:50]
    if elapsed := track.get("elapsed_ms"):
        info["duration_played_ms"] = elapsed
    metadata["additional_info"] = info
    return {"listened_at": track["listened_at"], "track_metadata": metadata}


def submit_url(url: str) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "expected an HTTP(S) ListenBrainz API URL without credentials, query or fragment"
        )
    path = parts.path.rstrip("/")
    if not path or path.endswith("/listenbrainz"):
        path += "/1"
    return parts._replace(
        path=path if path.endswith(ENDPOINT) else path + ENDPOINT
    ).geturl()


def batches(
    listens: Iterable[tuple[int, dict]],
    limit: int,
) -> Iterator[tuple[int, int, int, bytes]]:
    rows: list[tuple[int, bytes]] = []
    overhead = len(PREFIX) + len(SUFFIX)
    size = overhead

    def request() -> tuple[int, int, int, bytes]:
        return (
            rows[0][0],
            rows[-1][0],
            len(rows),
            PREFIX + b",".join(body for _, body in rows) + SUFFIX,
        )

    for row, listen in listens:
        try:
            body = json.dumps(
                listen, ensure_ascii=False, separators=(",", ":")
            ).encode()
        except UnicodeEncodeError:
            print(f"Row {row}: invalid Unicode in listen metadata", file=sys.stderr)
            continue
        if len(body) > MAX_LISTEN_BYTES:
            print(
                f"Row {row}: listen exceeds the ListenBrainz size limit",
                file=sys.stderr,
            )
            continue
        increment = len(body) + bool(rows)
        if rows and size + increment >= MAX_REQUEST_BYTES:
            yield request()
            rows.clear()
            size = overhead
        size += len(body) + bool(rows)
        rows.append((row, body))
        if len(rows) == limit:
            yield request()
            rows.clear()
            size = overhead
    if rows:
        yield request()


def write_imports(
    listens: Iterable[tuple[int, dict]],
    *,
    batch_size: int,
    url: str | None = None,
    token: str = "",
) -> int:
    """Print request bodies or post them, returning the acknowledged listen count."""
    if not 1 <= batch_size <= MAX_LISTENS:
        raise ValueError(f"--batch-size must be between 1 and {MAX_LISTENS}")
    if url is not None:
        url = submit_url(url)
        if (
            not token
            or not token.isascii()
            or any(not 33 <= ord(c) <= 126 for c in token)
        ):
            raise ValueError(
                "--submit requires a valid LISTENBRAINZ_TOKEN environment variable"
            )
    headers = {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
        "User-Agent": f"Beetroots/{__version__} (https://github.com/krishnans2006/beetroots)",
    }
    next_request = 0.0
    delivered = 0
    with requests.Session() as session:
        for first, last, count, body in batches(listens, batch_size):
            if url is None:
                sys.stdout.buffer.write(body + b"\n")
                sys.stdout.buffer.flush()
            else:
                for attempt in range(2):
                    time.sleep(max(0, next_request - time.monotonic()))
                    next_request = time.monotonic() + 1
                    try:
                        response = session.post(
                            url,
                            data=body,
                            headers=headers,
                            timeout=(10, 90),
                            allow_redirects=False,
                        )
                    except requests.RequestException as exc:
                        raise RuntimeError(
                            f"rows {first}-{last} after {delivered} acknowledged listens: {type(exc).__name__}; receipt is unknown"
                        ) from exc
                    status = response.status_code
                    if (
                        status == 429
                        or response.headers.get("X-RateLimit-Remaining") == "0"
                    ):
                        reset = response.headers.get(
                            "X-RateLimit-Reset-In"
                        ) or response.headers.get("Retry-After", "")
                        delay = (
                            int(reset)
                            if reset.isascii() and reset.isdecimal() and len(reset) <= 5
                            else 1
                        )
                        next_request = max(next_request, time.monotonic() + delay)
                        if status == 429 and attempt == 0:
                            continue
                    if not 200 <= status < 300:
                        raise RuntimeError(
                            f"rows {first}-{last} after {delivered} acknowledged listens: HTTP {status}"
                        )
                    try:
                        acknowledged = response.json()
                    except ValueError:
                        acknowledged = None
                    if (
                        not isinstance(acknowledged, dict)
                        or acknowledged.get("status") != "ok"
                    ):
                        raise RuntimeError(
                            f"rows {first}-{last} after {delivered} acknowledged listens: server omitted its acknowledgment"
                        )
                    print(
                        f"Accepted {count} listens from rows {first}-{last}",
                        file=sys.stderr,
                    )
                    break
            delivered += count
    return delivered
