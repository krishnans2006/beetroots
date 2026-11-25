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

## Import Spotify history

Run the Spotify wrapper from the repository root with a single streaming-history JSON file. The default output is JSON Lines, with one ListenBrainz `import` request body per line. With Beetroots installed, you can also run `python roots/spotify.py` directly.

```sh
python -m roots.spotify data/Streaming_History_Audio_2020.json --limit 10 > listens.jsonl
```

To submit directly, set `LISTENBRAINZ_TOKEN` to your ListenBrainz token or Koito API key and pass the API URL.

```sh
python -m roots.spotify data/Streaming_History_Audio_2020.json \
  --submit http://localhost:4110/apis/listenbrainz/1
```

The wrapper accepts Spotify’s extended and basic history formats. It estimates playback start from the exported end time and sends elapsed playback as `duration_played_ms`. Full track duration comes from Beetroots. Beetroots applies a MusicBrainz proposal when the source title and credit agree with the recording. It accepts equivalent punctuation, known aliases, and common version suffixes. Spotify text supplies missing fields and covers rejected or unmatched results. The default confidence floor is **0.0**. Use `--min-confidence` to raise it while retaining the title and credit check. [Spotify export fields](https://support.spotify.com/us/article/understanding-your-data/) · [ListenBrainz request fields](https://listenbrainz.readthedocs.io/en/latest/users/json.html)

Podcasts, audiobooks, zero-playback rows, and malformed entries are skipped. Positive short plays are kept. Add `--include-incognito` to include private-session listens and `--include-ip` to carry IP addresses in the Spotify-specific request metadata. The demo Koito build accepts this context in the request and stores a subset of the submitted fields. The wrapper reports counts and errors on stderr.

Use `--start-row` and `--limit` to select a one-based range of source rows. In submission mode, the wrapper reports the range for each acknowledged batch and stops on a failed batch. Check those rows on the server before retrying a request with an unknown outcome.

### Spotify matching audit

The five exports were run through the wrapper against a local replay of the [November 17, 2026 canonical MusicBrainz snapshot](https://metabrainz.org/datasets/derived-dumps). Read-only indexed MusicBrainz searches and a second pass over disputed results supplied additional evidence. The replay validates the wrapper and Beetroots against a fixed catalog. Live search ordering and full release metadata can vary.

The old cutoff of 0.68 let an exact artist credit carry a different song title above the threshold. It also discarded valid ensemble credits that Spotify shortened to one artist. The importer now checks the title and credit separately before applying an MBID. The matcher also searches the credited artist individually when its ordinary search returns no same-song result. With the agreement check in place, raising the numeric cutoff dropped verified songs without removing any additional observed wrong-song tags in the replay.
