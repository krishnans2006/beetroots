import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from beetroots import Matcher, MatcherConfig


def mbid(number):
    return f"00000000-0000-4000-8000-{number:012d}"


STUDIO, LIVE, OLD, POOR, EXACT, ISRC_ONLY, ALBUM_REC, ACOUSTIC = map(mbid, range(1, 9))
ARTIST, FEATURED, OTHER, WORK, LABEL, AREA = map(mbid, range(20, 26))
EARLY, LATE, LIVE_RELEASE, PREVIEW, REISSUE, CONFLICT, GROUP, ALBUM_RELEASE = map(
    mbid, range(30, 38)
)
FIRST, SECOND, LIVE_TRACK, LATE_TRACK, PREVIEW_TRACK, REISSUE_TRACK, ALBUM_TRACK = map(
    mbid, range(40, 47)
)
SPOTIFY = "0123456789abcdefghijkl"
YOUTUBE = "abcdefghijk"
DELETED = "lmnopqrstuv"
URL = "https://example.org/tracks/unique"
ISRC = "USABC0123456"
UNKNOWN = mbid(100)
UNAVAILABLE = mbid(101)


def artist(name, identifier, join=""):
    return {
        "name": name,
        "joinphrase": join,
        "artist": {"name": name, "sort-name": name, "id": identifier},
    }


CREDITS = [artist("The Act", ARTIST, " feat. "), artist("Guest", FEATURED)]


def recording(identifier, title, credits=CREDITS, length=244000, disambiguation=""):
    return {
        "id": identifier,
        "title": title,
        "artist-credit": credits,
        "length": length,
        "disambiguation": disambiguation,
    }


def track(identifier, position, rec):
    return {
        "id": identifier,
        "position": position,
        "number": str(position),
        "title": rec["title"],
        "recording": rec,
    }


def release(identifier, title, date, tracks, status="Official", credits=CREDITS):
    return {
        "id": identifier,
        "title": title,
        "date": date,
        "status": status,
        "artist-credit": credits,
        "release-group": {"id": GROUP, "primary-type": "Album"},
        "label-info": [
            {
                "catalog-number": "CAT-A",
                "label": {"id": LABEL, "name": "Fixture Records"},
            }
        ],
        "release-events": [
            {"date": date, "area": {"id": AREA, "iso-3166-1-codes": ["GB"]}}
        ],
        "media": [
            {
                "position": 1,
                "format": "CD",
                "track-count": len(tracks),
                "tracks": tracks,
            }
        ],
    }


def responses():
    studio = recording(STUDIO, "Signal", disambiguation="studio version")
    live = recording(LIVE, "Signal (Live)", disambiguation="live at the Arena")
    poor = recording(POOR, "zzz", [artist("zzz", OTHER)], length=None)
    exact = recording(
        EXACT, "Signal", length=180000, disambiguation="studio radio edit"
    )
    only = recording(ISRC_ONLY, "Unknown Title", [], length=None)
    album_rec = recording(ALBUM_REC, "Album Song", [artist("The Act", ARTIST)])
    acoustic = recording(ACOUSTIC, "Signal (Acoustic)", disambiguation="acoustic mix")
    early = release(
        EARLY,
        "Signal Album",
        "2001",
        [track(FIRST, 1, studio), track(SECOND, 2, studio)],
    )
    late = release(
        LATE,
        "Signal Album",
        "2001-01",
        [track(LATE_TRACK, 1, studio), track(mbid(47), 2, exact)],
    )
    live_release = release(
        LIVE_RELEASE, "At the Arena", "1999", [track(LIVE_TRACK, 1, live)]
    )
    preview = release(
        PREVIEW, "Preview", "1994", [track(PREVIEW_TRACK, 1, studio)], "Promotion"
    )
    reissue = release(REISSUE, "Signal Album", "", [track(REISSUE_TRACK, 1, studio)])
    conflict = release(CONFLICT, "Live Elsewhere", "", [track(mbid(48), 1, live)])
    album_release = release(
        ALBUM_RELEASE, "Provider Album", "2010", [track(ALBUM_TRACK, 1, album_rec)]
    )
    studio_full = {
        **studio,
        "releases": [{"id": EARLY}],
        "release-groups": [{"id": GROUP}],
        "first-release-date": "2001",
        "fixture-unmapped-field": {"preserve": True},
        "genres": [{"name": "electronic", "count": 7}],
        "aliases": [
            {
                "name": "The Signal",
                "locale": None,
                "primary": False,
                "type": "Search hint",
            }
        ],
        "relations": [
            {
                "type": "performance",
                "target-type": "work",
                "work": {"id": WORK, "title": "Signal"},
            }
        ],
    }
    data = {}
    for rec in (studio, live, poor, exact, only, album_rec, acoustic):
        data[f"/ws/2/recording/{rec['id']}"] = rec
        data[f"/ws/2/recording/{rec['id']}?canonical"] = (
            studio_full if rec["id"] == STUDIO else rec
        )
        data[f"/ws/2/release?{rec['id']}:0"] = {"release-count": 0, "releases": []}
    data[f"/ws/2/recording/{OLD}"] = studio
    data[f"/ws/2/recording/{UNAVAILABLE}"] = 503
    data[f"/ws/2/recording/{UNAVAILABLE}?canonical"] = 503
    for edition in (
        early,
        late,
        live_release,
        preview,
        reissue,
        conflict,
        album_release,
    ):
        data[f"/ws/2/release/{edition['id']}"] = edition
    for rec, editions in (
        (STUDIO, [early, late, preview, reissue]),
        (LIVE, [live_release, conflict]),
        (ALBUM_REC, [album_release]),
        (EXACT, [late]),
    ):
        for offset, edition in enumerate(editions):
            data[f"/ws/2/release?{rec}:{offset}"] = {
                "release-count": len(editions),
                "releases": [edition],
            }
    for kind, identifier, values in (
        ("artist", ARTIST, {"name": "The Act", "aliases": [{"name": "An alias"}]}),
        ("artist", FEATURED, {"name": "Guest"}),
        ("artist", OTHER, {"name": "zzz"}),
        ("release-group", GROUP, {"title": "Signal Album", "primary-type": "Album"}),
        (
            "work",
            WORK,
            {
                "title": "Signal",
                "relations": [
                    {
                        "type": "composer",
                        "target-type": "artist",
                        "artist": {
                            "id": ARTIST,
                            "name": "The Act",
                            "sort-name": "The Act",
                        },
                    },
                ],
            },
        ),
        ("label", LABEL, {"name": "Fixture Records"}),
        ("area", AREA, {"name": "United Kingdom"}),
    ):
        data[f"/ws/2/{kind}/{identifier}"] = {"id": identifier, **values}

    def text_search(query, records):
        data[f"/ws/2/recording?{query}"] = {
            "count": len(records),
            "recordings": records,
        }

    text_search(
        "artist:(the act feat. guest) recording:(signal) alias:(signal)",
        [studio, live, exact, acoustic],
    )
    text_search("artist:(the act) recording:(signal) alias:(signal)", [studio, live])
    text_search("artist:(the act, guest) recording:(signal) alias:(signal)", [studio])
    text_search("artist:(the act feat. guest)", [studio])
    text_search(
        "artist:(the act) recording:(album song) alias:(album song)", [album_rec]
    )
    text_search("artist:(aaa) recording:(aaa) alias:(aaa)", [poor])
    text_search("artist:(never) recording:(exists) alias:(exists)", [])
    text_search(
        "artist:(the act) recording:(signal \\(acoustic\\)) alias:(signal \\(acoustic\\))",
        [studio, acoustic],
    )
    text_search(f"tid:({SECOND})", [{**studio, "releases": [{"id": EARLY}]}])
    text_search(f"tid:({LIVE_TRACK})", [{**live, "releases": [{"id": LIVE_RELEASE}]}])
    text_search(f"arid:({ARTIST}) recording:(signal)", [studio])
    text_search(f"rgid:({GROUP})", [studio, live])
    data["/ws/2/recording?artist:(network) recording:(down) alias:(down)"] = 503
    data[f"/ws/2/isrc/{ISRC}"] = {"recordings": [only]}
    data["/ws/2/isrc/GBABC0123456"] = {"recordings": [studio, exact]}
    data[f"/ws/2/url?{URL}"] = {
        "relations": [{"target-type": "recording", "recording": {"id": ISRC_ONLY}}]
    }
    data["/api/token"] = {"access_token": "fixture-token", "expires_in": 3600}
    data[f"/v1/tracks/{SPOTIFY}"] = {
        "id": SPOTIFY,
        "name": "Signal",
        "artists": [{"name": "The Act"}, {"name": "Guest"}],
        "duration_ms": 244000,
        "album": {"name": "Signal Album", "release_date": "2001"},
        "track_number": 2,
    }
    data[f"watch:{YOUTUBE}"] = {
        "tracks": [
            {"videoId": "zyxwvutsrqp", "title": "Wrong video"},
            {
                "videoId": YOUTUBE,
                "title": "Album Song",
                "artists": [{"name": "The Act"}],
                "length": "4:04",
                "album": {"name": "Provider Album", "id": "fixture-album"},
            },
        ]
    }
    data["album:fixture-album"] = {"tracks": [{"videoId": YOUTUBE, "trackNumber": 1}]}
    data[f"watch:{DELETED}"] = {
        "tracks": [{"videoId": YOUTUBE, "title": "Wrong video"}]
    }
    return data


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.fixture = self.path / "responses.json"
        self.log = self.path / "requests.jsonl"
        self.responses = responses()
        self.save_responses()
        previous = os.environ.get("PYTHONPATH", "")
        env = patch.dict(
            os.environ,
            {
                "PYTHONPATH": os.pathsep.join(
                    [str(Path(__file__).parent / "fixtures"), previous]
                ),
                "XDG_CACHE_HOME": self.temp.name,
                "BEETROOTS_TEST_RESPONSES": str(self.fixture),
                "BEETROOTS_TEST_LOG": str(self.log),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.config = MatcherConfig(
            musicbrainz_url="https://fixture.invalid/ws/2",
            musicbrainz_interval=0,
            spotify_client_id="fixture-client",
            spotify_client_secret="fixture-secret",
        )
        self.cache = self.path / "cache.sqlite3"

    def save_responses(self, **extra):
        self.fixture.write_text(json.dumps({"responses": self.responses, **extra}))

    def matcher(self, config=None, cache=None, **kwargs):
        matcher = Matcher(
            config or self.config, cache_path=cache or self.cache, **kwargs
        )
        self.addCleanup(matcher.close)
        return matcher

    def cli(self, rows, *, refresh=False, cache=None):
        config = self.path / "config.json"
        config.write_text(json.dumps(asdict(self.config)))
        input_path = self.path / "input.jsonl"
        raw = b"".join(
            (json.dumps(row).encode() if not isinstance(row, bytes) else row) + b"\n"
            for row in rows
        )
        input_path.write_bytes(raw)
        command = [
            sys.executable,
            "-m",
            "beetroots",
            "match",
            "--config",
            str(config),
            "--cache",
            str(cache or self.cache),
        ]
        if refresh:
            command.append("--refresh")
        with input_path.open("rb") as source:
            result = subprocess.run(
                command, stdin=source, capture_output=True, check=True
            )
        self.assertEqual(input_path.read_bytes(), raw)
        self.assertEqual(result.stderr, b"")
        return [json.loads(line) for line in result.stdout.splitlines()]

    def compare_api_cli(self, rows):
        matcher = self.matcher()
        results = [asdict(matcher.match(row)) for row in rows]
        self.assertEqual(results, self.cli(rows))
        return results

    def test_authoritative_ids_and_paged_metadata(self):
        (
            result,
            by_track,
            consistent,
            conflict,
            wrong_release,
            malformed,
            missing,
            down,
        ) = self.compare_api_cli(
            [
                {
                    "record_id": "navidrome",
                    "recording_mbid": OLD,
                    "title": "Wrong",
                    "artist": "Someone else",
                },
                {"release_track_mbid": SECOND},
                {"recording_mbid": OLD, "release_track_mbid": SECOND},
                {"recording_mbid": STUDIO, "release_track_mbid": LIVE_TRACK},
                {"recording_mbid": STUDIO, "release_mbid": CONFLICT},
                {"recording_mbid": "invalid"},
                {"recording_mbid": UNKNOWN},
                {"recording_mbid": UNAVAILABLE},
            ]
        )
        self.assertEqual(
            (result["method"], result["recording_mbid"], result["confidence"]),
            ("recording_mbid", STUDIO, 1),
        )
        self.assertEqual(result["proposed_tags"]["title"], "Signal")
        self.assertEqual(result["proposed_tags"]["artist"], "The Act feat. Guest")
        self.assertEqual(result["proposed_tags"]["composers"], ["The Act"])
        self.assertEqual(result["proposed_tags"]["release_date"], "2001")
        self.assertEqual(result["proposed_tags"]["release_track_mbid"], FIRST)
        self.assertTrue(result["edition"]["inferred"])
        self.assertTrue(result["metadata"]["complete"])
        self.assertTrue(
            result["metadata"]["recording"]["fixture-unmapped-field"]["preserve"]
        )
        self.assertEqual(len(result["metadata"]["appearances"]), 5)
        self.assertTrue(
            all(
                a["recording_mbid"] == STUDIO for a in result["metadata"]["appearances"]
            )
        )
        self.assertEqual(
            result["metadata"]["entities"]["artist"][ARTIST]["aliases"],
            [{"name": "An alias"}],
        )
        self.assertEqual(result["metadata"], by_track["metadata"])
        self.assertEqual(by_track["method"], "release_track_mbid")
        self.assertEqual(by_track["proposed_tags"]["release_track_mbid"], SECOND)
        self.assertEqual(consistent["edition"], by_track["edition"])
        self.assertFalse(by_track["edition"]["inferred"])
        self.assertEqual(conflict["status"], "conflicting_ids")
        self.assertEqual(wrong_release["recording_mbid"], STUDIO)
        self.assertTrue(
            any(issue["code"] == "conflicting_ids" for issue in wrong_release["issues"])
        )
        self.assertEqual(malformed["issues"][0]["code"], "malformed_id")
        self.assertEqual(missing["status"], "unresolved_id")
        self.assertEqual(down["status"], "lookup_error")

    def test_provider_enrichment_and_unscorable_link_matches(self):
        spotify, youtube, deleted, unique_url, unique_isrc, supplied = (
            self.compare_api_cli(
                [
                    {"spotify_uri": f"spotify:track:{SPOTIFY}", "elapsed_ms": 7000},
                    {"source_urls": [f"https://music.youtube.com/watch?v={YOUTUBE}"]},
                    {"youtube_video_id": DELETED},
                    {"source_urls": [URL]},
                    {"isrcs": [ISRC]},
                    {
                        "spotify_track_id": SPOTIFY,
                        "artist": "The Act feat. Guest",
                        "release_date": "1901",
                        "elapsed_ms": 100,
                    },
                ]
            )
        )
        self.assertEqual(spotify["recording_mbid"], STUDIO)
        self.assertEqual(spotify["edition"]["release_track_mbid"], SECOND)
        self.assertNotIn("isrcs", spotify["proposed_tags"])
        self.assertEqual(spotify["provenance"]["input"]["elapsed_ms"], 7000)
        self.assertEqual(
            spotify["provenance"]["providers"]["spotify"][SPOTIFY]["fields"][
                "artist_credits"
            ],
            [{"name": "The Act"}, {"name": "Guest"}],
        )
        self.assertEqual(youtube["recording_mbid"], ALBUM_REC)
        self.assertEqual(
            youtube["provenance"]["providers"]["youtube_music"][YOUTUBE]["fields"][
                "track_number"
            ],
            1,
        )
        self.assertEqual(youtube["proposed_tags"]["duration_ms"], 244000)
        self.assertEqual(deleted["status"], "no_candidates")
        self.assertTrue(
            any(i["code"] == "provider_not_found" for i in deleted["issues"])
        )
        for result in (unique_url, unique_isrc):
            self.assertEqual(result["status"], "insufficient_evidence")
            self.assertIsNone(result["confidence"])
            self.assertIsNone(result["proposed_tags"])
        self.assertEqual(supplied["proposed_tags"]["release_date"], "2001")
        self.assertEqual(supplied["provenance"]["input"]["release_date"], "1901")
        self.assertEqual(
            supplied["provenance"]["providers"]["spotify"][SPOTIFY]["fields"][
                "release_date"
            ],
            "2001",
        )

    def test_scores_and_version_edition_constraints(self):
        (
            exact,
            artist_only,
            live,
            identified,
            zero,
            trusted,
            empty,
            down,
            tied,
            acoustic,
        ) = self.compare_api_cli(
            [
                {
                    "title": "Signal",
                    "artist": "The Act feat. Guest",
                    "elapsed_ms": 1000,
                },
                {"artist": "The Act feat. Guest"},
                {"title": "Signal", "artist": "The Act feat. Guest", "version": "live"},
                {
                    "title": "Signal",
                    "artist": "The Act feat. Guest",
                    "album": "At the Arena",
                },
                {"title": "aaa", "artist": "aaa"},
                {"recording_mbid": POOR},
                {"title": "exists", "artist": "Never"},
                {"title": "down", "artist": "Network"},
                {
                    "title": "Signal",
                    "artist": "The Act feat. Guest",
                    "isrcs": ["GBABC0123456"],
                },
                {"title": "Signal (Acoustic)", "artist": "The Act"},
            ]
        )
        self.assertEqual(exact["distance"], 0)
        self.assertEqual(artist_only["distance"], 0)
        self.assertEqual(live["recording_mbid"], LIVE)
        self.assertEqual(acoustic["recording_mbid"], ACOUSTIC)
        self.assertEqual(
            identified["edition"],
            {
                "release_mbid": LIVE_RELEASE,
                "release_track_mbid": LIVE_TRACK,
                "inferred": False,
            },
        )
        self.assertEqual(zero["distance"], 1)
        self.assertEqual(zero["confidence"], 0)
        self.assertEqual(zero["recording_mbid"], POOR)
        self.assertEqual(zero["metadata"], trusted["metadata"])
        self.assertEqual(zero["proposed_tags"], trusted["proposed_tags"])
        self.assertNotIn("duration_ms", zero["proposed_tags"])
        self.assertEqual(empty["status"], "no_candidates")
        self.assertIsNone(empty["confidence"])
        self.assertIsNone(empty["proposed_tags"])
        self.assertEqual(down["status"], "lookup_error")
        self.assertEqual(tied["recording_mbid"], STUDIO)
        self.assertEqual(
            tied["provenance"]["candidate_paths"], ["isrc:GBABC0123456", "text"]
        )

    def test_credit_search_and_song_agreement(self):
        self.responses[
            "/ws/2/recording?artist:(the act) recording:(signal) alias:(signal)"
        ] = {
            "recordings": [{"id": ALBUM_REC}],
        }
        self.responses["/ws/2/recording?artistname:(the act) recording:(signal)"] = {
            "recordings": [{"id": STUDIO}],
        }
        self.save_responses()
        matcher = self.matcher()
        source = {"artist": "The Act", "title": "Signal (from the soundtrack)"}
        result = matcher.match(source)
        self.assertEqual(result.recording_mbid, STUDIO)
        self.assertEqual(
            result.tags_for(source, min_confidence=0, require_song_agreement=True)[
                "recording_mbid"
            ],
            STUDIO,
        )
        self.assertNotIn(
            "recording_mbid",
            result.tags_for(
                {**source, "title": "Another Song"},
                min_confidence=0,
                require_song_agreement=True,
            ),
        )

    def test_stable_order_and_isolated_configuration(self):
        track = {
            "title": "Signal",
            "artist": "The Act feat. Guest",
            "duration_ms": 180000,
        }
        first = self.matcher()
        second = self.matcher(
            replace(self.config, distance_weights={"track_length": 0})
        )
        self.assertEqual(first.match(track).recording_mbid, EXACT)
        self.assertEqual(second.match(track).recording_mbid, STUDIO)
        self.assertEqual(first.match(track).recording_mbid, EXACT)
        self.assertNotEqual(first.config_fingerprint, second.config_fingerprint)
        generous = self.matcher(replace(self.config, track_length_grace=70))
        self.assertEqual(generous.match(track).recording_mbid, STUDIO)
        same = self.matcher(replace(self.config, track_length_grace=10.0))
        self.assertEqual(same.config_fingerprint, first.config_fingerprint)
        disabled = self.matcher(
            replace(self.config, distance_weights={"track_title": 0, "track_artist": 0})
        )
        result = disabled.match({"title": "Signal", "artist": "The Act feat. Guest"})
        self.assertEqual(result.status, "insufficient_evidence")
        self.assertIsNone(result.confidence)

    def test_supplemental_order_and_alias_deduplication(self):
        self.responses["/ws/2/isrc/GBABC0123456"]["recordings"].reverse()
        self.responses["/ws/2/recording?artist:(the act feat. guest)"]["recordings"] = [
            {"id": OLD}
        ]
        self.save_responses()
        supplemental_only, merged = self.compare_api_cli(
            [
                {"artist": "The Act & Guest", "isrcs": ["GBABC0123456"]},
                {"artist": "The Act feat. Guest", "isrcs": ["GBABC0123456"]},
            ]
        )
        self.assertEqual(supplemental_only["recording_mbid"], STUDIO)
        self.assertEqual(merged["recording_mbid"], STUDIO)
        self.assertEqual(
            merged["provenance"]["candidate_paths"], ["isrc:GBABC0123456", "text"]
        )

    def test_replay_when_worker_completion_order_changes(self):
        rows = [{"recording_mbid": STUDIO}, {"recording_mbid": LIVE}]

        def replay(slow):
            self.save_responses(delays={f"/ws/2/recording/{slow}": 0.2})
            matchers = [
                self.matcher(cache=self.path / f"{slow}-{index}.sqlite3")
                for index in range(2)
            ]
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(matcher.match, row)
                    for matcher, row in zip(matchers, rows)
                ]
                finished = [
                    future.result().recording_mbid for future in as_completed(futures)
                ]
                outputs = [future.result() for future in futures]
            return finished, outputs

        first_order, first = replay(STUDIO)
        second_order, second = replay(LIVE)
        self.assertEqual(first_order, [LIVE, STUDIO])
        self.assertEqual(second_order, [STUDIO, LIVE])
        self.assertEqual(first, second)

    def test_edition_fields_and_constraints(self):
        self.responses[f"/ws/2/recording?work={WORK}:0"] = {
            "recording-count": 1,
            "recordings": [{"id": STUDIO}],
        }
        self.save_responses()
        release_only, group, artist, work = self.compare_api_cli(
            [
                {"release_mbid": EARLY, "track_number": 2},
                {
                    "title": "Signal",
                    "release_group_mbid": GROUP,
                    "country": "GB",
                    "release_date": "2001-01",
                },
                {"title": "Signal", "artist_mbid": ARTIST},
                {"title": "Signal", "work_mbid": WORK},
            ]
        )
        self.assertEqual(release_only["edition"]["release_track_mbid"], SECOND)
        self.assertEqual(group["edition"]["release_mbid"], LATE)
        self.assertEqual(artist["recording_mbid"], STUDIO)
        self.assertEqual(work["recording_mbid"], STUDIO)

    def test_identity_and_canonical_retrieval_are_independent(self):
        trusted, weak = self.compare_api_cli(
            [
                {"recording_mbid": STUDIO, "release_mbid": EARLY},
                {
                    "title": "Completely different",
                    "artist": "Other band",
                    "release_mbid": EARLY,
                },
            ]
        )
        self.assertLess(weak["confidence"], 0.5)
        self.assertEqual(weak["edition"], trusted["edition"])
        self.assertEqual(weak["metadata"], trusted["metadata"])
        self.assertEqual(weak["proposed_tags"], trusted["proposed_tags"])

        self.responses[f"/ws/2/recording/{STUDIO}?canonical"] = 503
        self.save_responses()
        (down,) = self.cli([{"recording_mbid": STUDIO}], refresh=True)
        self.assertEqual(down["status"], "lookup_error")
        self.assertEqual(down["recording_mbid"], STUDIO)
        self.assertEqual(down["confidence"], 1)
        self.assertIsNone(down["proposed_tags"])

    def test_batch_refresh_and_failed_lookups(self):
        rows = [
            {"record_id": 10, "title": "Signal", "artist": "The Act feat. Guest"},
            b'{"invalid":\rrow}',
            {"record_id": 11, "title": 123},
            {"record_id": 12, "title": "Signal", "artist": "The Act feat. Guest"},
            {"record_id": 13, "artist": "Never", "title": "exists"},
            {"record_id": 14, "artist": "Network", "title": "down"},
        ]
        first = self.cli(rows)
        log_before = self.log.read_text().splitlines()
        self.assertEqual(self.cli(rows), first)
        added = self.log.read_text().splitlines()[len(log_before) :]
        self.assertEqual(
            [json.loads(row) for row in added],
            ["/ws/2/recording?artist:(network) recording:(down) alias:(down)"],
        )
        self.assertEqual(
            [result["record_id"] for result in first], [10, None, 11, 12, 13, 14]
        )
        self.assertEqual(
            [result["status"] for result in first],
            [
                "matched",
                "invalid_input",
                "invalid_input",
                "matched",
                "no_candidates",
                "lookup_error",
            ],
        )
        self.assertEqual(first[0]["metadata"], first[3]["metadata"])
        self.save_responses(delays={f"/ws/2/recording/{STUDIO}": 0.04})
        self.assertEqual(self.cli(rows, refresh=True), first)
        self.save_responses(delays={f"/ws/2/recording/{LIVE}": 0.04})
        self.assertEqual(self.cli(rows, cache=self.path / "cold.sqlite3"), first)

    def test_text_candidates_survive_supplemental_failure(self):
        self.responses["/ws/2/isrc/GBABC0123456"] = 503
        self.responses[f"/ws/2/recording/{LIVE}"] = 503
        self.save_responses()
        (result,) = self.compare_api_cli(
            [
                {
                    "title": "Signal",
                    "artist": "The Act feat. Guest",
                    "isrcs": ["GBABC0123456"],
                }
            ]
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["recording_mbid"], STUDIO)
        self.assertEqual(
            {issue["path"] for issue in result["issues"]},
            {
                "isrc:GBABC0123456",
                f"candidate.recording:{LIVE}",
            },
        )


if __name__ == "__main__":
    unittest.main()
