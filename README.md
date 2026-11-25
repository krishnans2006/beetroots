# Beetroots

[Beets](https://github.com/beetbox/beets), but with the roots.

A multi-tool that adapts Beets for tagging in a variety of situations.

It's goal is simple: use the available information as best as possible to identify authoritative sources (e.g., MusicBrainz IDs), evaluate confidence, and return the corrected tags. It generalizes beets away from just music files and into a more general tagging tool for things like scrobbles and exported listening history.

## Install

Beetroots supports Python 3.12 through 3.14 and uses beets 2.14.1.

```sh
python -m pip install .
```

## Match a listen

```python
from beetroots import Matcher, TrackInput

listen: TrackInput = {
    "record_id": "listen-42",
    "title": "Teardrop",
    "artist": "Massive Attack",
}

with Matcher() as matcher:
    result = matcher.match(listen)

print(result.recording_mbid, result.confidence)
print(result.proposed_tags)
```

The CLI accepts the same `TrackInput` fields. It prints one result per input line in order, including malformed lines.

```sh
printf '%s\n' '{"record_id":"listen-42","title":"Teardrop","artist":"Massive Attack"}' |
  beetroots match
beetroots match --config matcher.json --cache lookups.sqlite3 --refresh < listens.jsonl > matches.jsonl
```

- Use `recording_mbid` for a recording ID and `release_track_mbid` for the ID of a particular appearance. A verified ID gives the result a confidence of `1.0`. If the verified IDs point to different recordings, the result reports `conflicting_ids`. A conflicting release is reported in `issues` alongside the verified recording.
- Supply ordered `artist_credits` as objects with `name`, optional `join_phrase`, and optional `artist_mbid`. The `artist` field takes a display string. Use `duration_ms` for the full track length and `elapsed_ms` for playback progress.
- Spotify track IDs, URIs, and URLs use client credentials from `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` or the equivalent `MatcherConfig` fields. YouTube enrichment uses unauthenticated YouTube Music by default.
- Results include the input and provider evidence in `provenance`. The `metadata` field holds the canonical recording, linked MusicBrainz payloads, and verified release-track appearances. The `edition` field identifies the appearance used for `proposed_tags` and whether it was inferred. The caller can save the result and decide when to apply the proposed tags.
- A scored match has `confidence = 1 - distance`, including zero-confidence matches. An unverified, unscorable input has null confidence and null proposed tags. Lookup failures appear in `issues`. When a failure leaves some canonical metadata unavailable, `metadata.complete` is false.

## Configure and replay

Use a `MatcherConfig` in Python or pass a JSON object with the same fields via `--config`. For example, this changes the relative weights of title and duration.

```json
{
  "distance_weights": {"track_title": 4.0, "track_length": 1.0},
  "track_length_grace": 5,
  "search_limit": 10
}
```

Each matcher runs beets in a dedicated process. Results include its matcher version and effective configuration fingerprint. The default cache is `$XDG_CACHE_HOME/beetroots/lookups.sqlite3` or `~/.cache/beetroots/lookups.sqlite3`. Workers using the same cache reuse successful and empty responses. Temporary failures can be retried on the next match. Pass `refresh=True` to `Matcher` or `--refresh` to refresh the lookups used in a run. Workers share the MusicBrainz request limiter within the same cache home.

Run the recorded-response tests with `python -m unittest discover -s tests -v`.
