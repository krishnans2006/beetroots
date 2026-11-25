"""Replay API payloads in the CLI and its child workers without a network."""

import io
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ytmusicapi import YTMusic

fixtures = json.loads(Path(os.environ["BEETROOTS_TEST_RESPONSES"]).read_text())


def playback(key):
    with open(os.environ["BEETROOTS_TEST_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps(key) + "\n")
    if delay := fixtures.get("delays", {}).get(key):
        time.sleep(delay)
    return fixtures["responses"].get(key, 404)


def open_response(request, *, timeout):
    parts = urlsplit(request.full_url)
    params = {k: v[0] for k, v in parse_qs(parts.query).items()}
    key = parts.path
    if key == "/ws/2/recording":
        if "query" in params:
            key += "?" + params["query"].replace(r"\-", "-")
        else:
            field = next(
                field for field in ("artist", "release", "work") if field in params
            )
            key += f"?{field}={params[field]}:{params['offset']}"
    elif key == "/ws/2/release":
        key += f"?{params.get('recording', params.get('track'))}:{params['offset']}"
    elif key == "/ws/2/url":
        key += "?" + params["resource"]
    elif key.startswith("/ws/2/recording/") and "annotation" in params.get("inc", ""):
        key += "?canonical"
    if key == "/api/token":
        assert request.data == b"grant_type=client_credentials"
        assert request.get_header("Authorization").startswith("Basic ")
    if key.startswith("/v1/tracks/"):
        assert request.get_header("Authorization") == "Bearer fixture-token"
    value = playback(key)
    if value == 404 and "query" in params:
        value = {"count": 0, parts.path.rsplit("/", 1)[-1] + "s": []}
    if isinstance(value, int):
        raise urllib.error.HTTPError(
            request.full_url, value, "Fixture response", {}, None
        )
    return io.BytesIO(json.dumps(value).encode())


def watch(self, *, videoId, limit):
    assert limit == 1
    return playback(f"watch:{videoId}")


def album(self, browse_id):
    return playback(f"album:{browse_id}")


urllib.request.urlopen = open_response
YTMusic.get_watch_playlist = watch
YTMusic.get_album = album
