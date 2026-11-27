"""Matcher tests using invented metadata and synthetic provider responses."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

from beets.autotag.hooks import TrackInfo

from beetroots import Matcher, MatcherConfig, MatchResult
from beetroots.engine import Candidate, compatible_recordings
from beetroots.identity import agrees
from roots import listenbrainz, spotify


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

    def test_video_display_evidence_requires_recording_agreement(self):
        watch = self.responses[f"watch:{YOUTUBE}"]["tracks"][1]
        watch.update(
            title="Signal",
            artists=[{"name": "The Act"}, {"name": "Guest"}],
            album=None,
            videoType="MUSIC_VIDEO_TYPE_OMV",
        )
        self.responses[f"/ws/2/url?https://www.youtube.com/watch?v={DELETED}"] = {
            "relations": [
                {"type": "free streaming", "recording": {"id": LIVE}},
                {"type": "free streaming", "recording": {"id": STUDIO}, "ended": True},
            ]
        }
        self.save_responses()
        official = {
            "video_title": "The Act - Signal (Official Lyric Video)",
            "uploader": "TheActVEVO",
            "youtube_video_id": YOUTUBE,
            "duration_ms": 244000,
        }
        uncredited = {
            "video_title": "The Act - Signal (Fan Edit)",
            "uploader": "Fan Uploads",
            "duration_ms": 244000,
        }
        with self.matcher() as matcher:
            match = matcher.match(official)
            self.assertEqual(
                match.tags_for(official, min_confidence=0, require_song_agreement=True)[
                    "recording_mbid"
                ],
                STUDIO,
            )
            mismatch = matcher.match({**official, "duration_ms": 325000})
            self.assertIsNone(mismatch.recording_mbid)
            fallback = matcher.match(uncredited)
            self.assertIsNone(fallback.recording_mbid)
            self.assertEqual(
                fallback.tags_for(uncredited, min_confidence=0),
                {"artist": "Fan Uploads", "title": uncredited["video_title"]},
            )
            linked = matcher.match(
                {
                    **uncredited,
                    "video_title": "Concert footage from the Arena",
                    "youtube_video_id": DELETED,
                }
            )
            self.assertEqual(linked.recording_mbid, LIVE)
            self.assertEqual(linked.provenance["youtube_recording_link"], LIVE)

    def test_video_uses_verified_track_title_for_the_version(self):
        watch = self.responses[f"watch:{YOUTUBE}"]["tracks"][1]
        watch.update(
            title="Signal (Radio Edit)",
            album={"name": "Signal Album", "id": "fixture-album"},
            videoType="MUSIC_VIDEO_TYPE_ATV",
        )
        release = self.responses[f"/ws/2/release/{EARLY}"]
        release["media"][0]["tracks"][0]["title"] = (
            "Signal (Radio Edit) (Album Version)"
        )
        self.save_responses()
        with self.matcher() as matcher:
            result = matcher.match(
                {
                    "video_title": "Signal (Radio Edit)",
                    "youtube_video_id": YOUTUBE,
                    "duration_ms": 244000,
                }
            )
        self.assertEqual(result.recording_mbid, STUDIO)
        self.assertEqual(result.edition["release_track_mbid"], FIRST)

    def test_custom_youtube_import_uses_history_events(self):
        history = self.path / "history.json"
        event = {
            "timestampMicroseconds": "1700000000123456",
            "videoId": YOUTUBE,
            "title": "Album Song",
            "app": "YouTube Music",
            "channels": [{"name": "Release - Topic"}],
            "devices": ["Web"],
            "durationLabel": "4:04",
            "videoProgressPercent": 10,
        }
        history.write_text(
            json.dumps(
                {
                    "run": {"validation": {"events": [event]}},
                    "events": [
                        event,
                        "malformed",
                        event,
                        {**event, "timestampMicroseconds": "1700000300123456"},
                    ],
                }
            )
        )
        config = self.path / "config.json"
        config.write_text(json.dumps(asdict(self.config)))
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "roots.youtube-custom",
                str(history),
                "--config",
                str(config),
                "--start-row",
                "2",
                "--batch-size",
                "1",
            ],
            capture_output=True,
            check=True,
        )
        requests = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(r["listen_type"] == "import" for r in requests))
        payloads = [r["payload"][0] for r in requests]
        self.assertEqual([p["listened_at"] for p in payloads], [1700000000, 1700000300])
        metadata = payloads[0]["track_metadata"]
        self.assertEqual(metadata["artist_name"], "The Act")
        self.assertEqual(metadata["track_name"], "Album Song")
        info = metadata["additional_info"]
        self.assertEqual(info["recording_mbid"], ALBUM_REC)
        self.assertEqual(info["duration_ms"], 244000)
        self.assertEqual(info["youtube_progress_percent"], 10)
        self.assertNotIn("duration_played_ms", info)
        self.assertIn(b"Row 2: expected a history object", result.stderr)

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
        self.responses[
            '/ws/2/recording?recording:"Signal" AND (artistname:"The Act" OR creditname:"The Act")'
        ] = {
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

    def test_version_suffixes_leave_the_recording_title_intact(self):
        title = "Fixture Recording Alpha"
        artist = "Fixture Performer"
        for suffix in (
            " - Pop Version",
            " - (Remix)",
            " - Live Fixture Tour 2099",
            " (live studio ver.)",
            " - Bonus Track",
            " (Theme from Fixture Film)",
        ):
            with self.subTest(suffix=suffix):
                self.assertTrue(agrees([title + suffix], artist, [title], [artist]))
        self.assertFalse(
            agrees(
                [f"{title} / Fixture Recording Beta"],
                artist,
                ["Fixture Recording Beta"],
                [artist],
            )
        )

    def test_modifier_letter_apostrophe_in_credit(self):
        title = "Fixture Recording Alpha"
        self.assertTrue(agrees([title], "Fixture'A", [title], ["FixtureʻA"]))
        self.assertTrue(
            agrees(["Fixture ™ Theme"], "Act", ["Fixture (TM) Theme"], ["Act"])
        )

    def test_spotify_uses_source_release_credits_and_submits_catalog_tags(self):
        edition = self.responses[f"/ws/2/release/{ALBUM_RELEASE}"]
        appearance = edition["media"][0]["tracks"][0]
        appearance["artist-credit"] = [artist("Fixture Composer", OTHER)]
        self.responses[
            "/ws/2/recording?recording:(album song) release:(provider album)"
        ] = {"recordings": [{"id": ALBUM_REC}]}
        self.save_responses()
        matcher = self.matcher()
        source = {
            "source": "spotify",
            "title": "Album Song",
            "artist": "Fixture Composer",
            "album": "Provider Album",
            "listened_at": 1704067200,
        }
        good = spotify.import_tags(source, matcher.match(source), 0)
        info = listenbrainz.listen(source, good, {})["track_metadata"][
            "additional_info"
        ]
        self.assertEqual(info["recording_mbid"], ALBUM_REC)
        self.assertEqual(info["track_mbid"], ALBUM_TRACK)
        self.assertEqual(good["artist"], "The Act")

        different_album = {**source, "artist": "The Act", "album": "Source Reissue"}
        tags = spotify.import_tags(different_album, matcher.match(different_album), 0)
        metadata = listenbrainz.listen(different_album, tags, {})["track_metadata"]
        self.assertEqual(metadata["release_name"], "Provider Album")
        self.assertEqual(metadata["additional_info"]["recording_mbid"], ALBUM_REC)
        self.assertEqual(metadata["additional_info"]["release_mbid"], ALBUM_RELEASE)
        self.assertEqual(metadata["additional_info"]["track_mbid"], ALBUM_TRACK)

        wrong_title = {**source, "title": "Different Song"}
        self.assertNotIn(
            "recording_mbid",
            spotify.import_tags(wrong_title, matcher.match(wrong_title), 0),
        )

    def test_import_roots_choose_the_same_catalog_recording_for_radio_and_album(self):
        album = self.responses[f"/ws/2/recording/{STUDIO}"]
        radio = {
            **recording(EXACT, "Signal (Radio Edit)", length=230000),
            "first-release-date": "1999",
            "isrcs": ["USABC0123457"],
        }
        album.update(
            isrcs=[ISRC],
            disambiguation="studio recording from a television performance",
        )
        self.responses[f"/ws/2/recording/{STUDIO}?canonical"].update(album)
        self.responses[f"/ws/2/recording/{EXACT}"] = radio
        video = {
            **recording(OLD, "Signal (Official Music Video)", length=243000),
            "video": True,
        }
        longer = {
            **recording(ACOUSTIC, "Signal", length=249000),
            "first-release-date": "1998",
            "isrcs": [ISRC],
        }
        self.responses[f"/ws/2/recording/{OLD}"] = video
        self.responses[f"/ws/2/recording/{ACOUSTIC}"] = longer
        self.responses[f"/ws/2/recording?recording:(signal) arid:({ARTIST})"] = {
            "recordings": [video, radio, longer, album]
        }
        spotify_url = f"https://open.spotify.com/track/{SPOTIFY}"
        self.responses[f"/ws/2/url?{spotify_url}"] = {
            "relations": [{"recording": {"id": EXACT}}],
        }
        self.responses[f"/v1/tracks/{SPOTIFY}"].update(
            name=radio["title"], duration_ms=227000, album={"name": "Single"}
        )
        youtube_url = f"https://www.youtube.com/watch?v={DELETED}"
        self.responses[f"/ws/2/url?{youtube_url}"] = {
            "relations": [{"recording": {"id": OLD}, "type": "free streaming"}],
        }
        self.save_responses()
        matcher = self.matcher()
        spotify_row = {
            "ts": "2024-01-01T00:00:07Z",
            "ms_played": 7000,
            "spotify_track_uri": f"spotify:track:{SPOTIFY}",
            "master_metadata_track_name": radio["title"],
            "master_metadata_album_artist_name": "The Act",
            "master_metadata_album_album_name": "Single",
        }
        youtube_row = {
            "timestampMicroseconds": "1704067200000000",
            "videoId": DELETED,
            "title": "Signal",
            "channels": [{"name": "The Act - Topic"}],
            "durationLabel": "4:03",
            "videoProgressPercent": 10,
        }
        args = Namespace(
            start_row=1,
            include_incognito=False,
            include_ip=False,
            min_confidence=listenbrainz.DEFAULT_MIN_CONFIDENCE,
        )
        youtube = import_module("roots.youtube-custom")
        _, from_spotify = next(spotify.listens([spotify_row], matcher, args))
        _, from_youtube = next(youtube.listens([youtube_row], matcher, args))
        for payload in (from_spotify, from_youtube):
            self.assertEqual(payload["listened_at"], 1704067200)
            metadata = payload["track_metadata"]
            self.assertEqual(metadata["track_name"], "Signal")
            self.assertEqual(metadata["artist_name"], "The Act feat. Guest")
            self.assertEqual(metadata["release_name"], "Signal Album")
            info = metadata["additional_info"]
            self.assertEqual(info["recording_mbid"], STUDIO)
            self.assertEqual(info["isrc"], ISRC)
            self.assertEqual(info["track_mbid"], FIRST)
            self.assertEqual(info["duration_ms"], 244000)
        spotify_info = from_spotify["track_metadata"]["additional_info"]
        youtube_info = from_youtube["track_metadata"]["additional_info"]
        self.assertEqual(spotify_info["origin_url"], spotify_url)
        self.assertEqual(spotify_info["duration_played_ms"], 7000)
        self.assertEqual(youtube_info["youtube_progress_percent"], 10)
        track_input, _ = spotify.spotify_play(spotify_row, 1, False)
        result = matcher.match(track_input)
        self.assertEqual(result.provenance["source_recording_mbid"], EXACT)
        track_input, _ = youtube.youtube_play(youtube_row, 1)
        result = matcher.match(track_input)
        self.assertEqual(result.provenance["source_recording_mbid"], OLD)

    def test_spotify_withholds_a_different_named_mix(self):
        mix = recording(ACOUSTIC, "Signal", disambiguation="club mix")
        self.responses[f"/ws/2/recording/{ACOUSTIC}"] = mix
        self.responses[
            "/ws/2/recording?artist:(the act) recording:(signal) alias:(signal)"
        ] = {"recordings": [mix]}
        self.save_responses()
        source = {
            "source": "spotify",
            "title": "Signal (Night Mix)",
            "artist": "The Act",
        }
        result = self.matcher().match(source)
        self.assertEqual(
            spotify.import_tags(source, result, 0),
            {"title": source["title"], "artist": source["artist"]},
        )

    def test_audio_video_comparison_checks_both_titles_for_a_live_version(self):
        audio = Candidate(
            TrackInfo(title="Signal Live", track_id=STUDIO, length=244),
            recording(STUDIO, "Signal Live"),
        )
        video = Candidate(
            TrackInfo(title="Signal (Live)", track_id=LIVE, length=244),
            {**recording(LIVE, "Signal (Live)"), "video": True},
        )
        self.assertFalse(compatible_recordings(audio, video))
        self.assertFalse(compatible_recordings(video, audio))

    def test_listen_uses_source_tags_and_duration_only_on_rejection(self):
        track_input = {
            "title": "Signal (Radio Edit)",
            "artist": "The Act",
            "album": "Single",
            "duration_ms": 230000,
            "listened_at": 1704067200,
        }
        result = MatchResult(
            None,
            "matched",
            "",
            recording_mbid=STUDIO,
            confidence=0.9,
            proposed_tags={
                "recording_mbid": STUDIO,
                "title": "Signal",
                "artist": "The Act feat. Guest",
            },
        )
        accepted = result.tags_for(track_input, min_confidence=0.8)
        payload = listenbrainz.listen(track_input, accepted, {})["track_metadata"]
        self.assertNotIn("release_name", payload)
        self.assertNotIn("duration_ms", payload["additional_info"])
        rejected = result.tags_for(track_input, min_confidence=0.95)
        payload = listenbrainz.listen(track_input, rejected, {})["track_metadata"]
        self.assertEqual(payload["track_name"], track_input["title"])
        self.assertEqual(payload["release_name"], track_input["album"])
        self.assertEqual(payload["additional_info"], {"duration_ms": 230000})

    def test_stream_recording_id_is_independent_of_the_source_edition(self):
        original = {**recording(STUDIO, "Signal"), "first-release-date": "2001"}
        remaster = {
            **recording(OLD, "Signal (2020 Remaster)", length=244300),
            "first-release-date": "2020",
        }
        clean = {
            **recording(EXACT, "Signal", disambiguation="clean"),
            "first-release-date": "1999",
        }
        candidates = [remaster, clean, original]
        for rec, edition_id, album, date, track_id in (
            (original, EARLY, "Signal Album", "2001", FIRST),
            (remaster, REISSUE, "Best of The Act", "2020", REISSUE_TRACK),
            (clean, LATE, "Signal Clean Edition", "1999", LATE_TRACK),
        ):
            recording_path = f"/ws/2/recording/{rec['id']}"
            self.responses[recording_path] = rec
            self.responses[f"{recording_path}?canonical"] = rec
            edition = release(edition_id, album, date, [track(track_id, 1, rec)])
            self.responses[f"/ws/2/release/{edition_id}"] = edition
            self.responses[f"/ws/2/release?{rec['id']}:0"] = {
                "release-count": 1,
                "releases": [edition],
            }
        self.responses[
            "/ws/2/recording?artist:(the act) recording:(signal) alias:(signal)"
        ] = {"recordings": candidates}
        self.responses[f"/ws/2/recording?recording:(signal) arid:({ARTIST})"] = {
            "recordings": candidates
        }
        self.responses[f"watch:{YOUTUBE}"]["tracks"][1].update(
            title="Signal",
            album={"name": "Signal Album (Deluxe Edition)", "id": "fixture-album"},
            videoType="MUSIC_VIDEO_TYPE_ATV",
        )
        self.save_responses()
        spotify_source = {
            "source": "spotify",
            "title": "Signal - 2020 Remaster",
            "artist": "The Act",
            "album": "Best of The Act",
            "elapsed_ms": 311,
        }
        youtube_source = {
            "video_title": "Signal",
            "uploader": "The Act - Topic",
            "duration_ms": 245000,
            "youtube_video_id": YOUTUBE,
        }
        matcher = self.matcher()
        from_spotify = matcher.match(spotify_source)
        from_youtube = matcher.match(youtube_source)
        self.assertEqual(from_spotify.recording_mbid, STUDIO)
        self.assertEqual(from_youtube.recording_mbid, STUDIO)
        self.assertEqual(from_youtube.edition["release_track_mbid"], FIRST)
        clean_source = {
            **spotify_source,
            "title": "Signal",
            "album": "Signal Clean Edition",
        }
        self.assertEqual(matcher.match(clean_source).recording_mbid, EXACT)
        self.assertEqual(
            matcher.match({**spotify_source, "recording_mbid": OLD}).recording_mbid, OLD
        )

    def test_stream_match_uses_featured_credit_and_full_duration(self):
        credits = [
            CREDITS[0],
            {**CREDITS[1], "joinphrase": " & "},
            artist("Second Guest", OTHER),
        ]
        original = recording(STUDIO, "Signal", credits)
        mix = recording(EXACT, "Signal", [artist("The Act", ARTIST)], length=237000)
        for rec in (original, mix):
            self.responses[f"/ws/2/recording/{rec['id']}"] = rec
            self.responses[f"/ws/2/recording/{rec['id']}?canonical"] = rec
        self.responses[
            "/ws/2/recording?artist:(the act) recording:(signal) alias:(signal)"
        ] = {"recordings": [mix, original]}
        watch = self.responses[f"watch:{YOUTUBE}"]["tracks"][1]
        watch.update(title="Signal", videoType="MUSIC_VIDEO_TYPE_ATV")
        watch.pop("album")
        self.save_responses()
        matcher = self.matcher()
        spotify_source = {
            "source": "spotify",
            "title": "Signal (feat. Guest of an Ensemble and Second Guest)",
            "artist": "The Act",
            "elapsed_ms": 500,
        }
        youtube_source = {
            "video_title": "Signal",
            "youtube_video_id": YOUTUBE,
            "uploader": "The Act - Topic",
            "duration_ms": 244000,
        }
        self.assertEqual(matcher.match(spotify_source).recording_mbid, STUDIO)
        self.assertEqual(matcher.match(youtube_source).recording_mbid, STUDIO)
        wrong_feature = {
            **spotify_source,
            "title": "Signal (feat. Guest of an Ensemble and Uncredited Performer)",
        }
        self.assertEqual(matcher.match(wrong_feature).status, "insufficient_evidence")

    def test_stream_uses_exact_album_when_normalized_titles_collide(self):
        intended = recording(STUDIO, "Signal")
        other = recording(EXACT, "Signal", [artist("The Act", ARTIST)])
        for rec, edition_id, album, track_id in (
            (intended, EARLY, "Signal Album (Expanded Edition)", FIRST),
            (other, LATE, "Signal Album (Deluxe Edition)", LATE_TRACK),
        ):
            self.responses[f"/ws/2/recording/{rec['id']}"] = rec
            self.responses[f"/ws/2/recording/{rec['id']}?canonical"] = rec
            edition = release(edition_id, album, "2001", [track(track_id, 1, rec)])
            self.responses[f"/ws/2/release/{edition_id}"] = edition
            self.responses[f"/ws/2/release?{rec['id']}:0"] = {
                "release-count": 1,
                "releases": [edition],
            }
        self.responses[
            "/ws/2/recording?artist:(the act) recording:(signal) alias:(signal)"
        ] = {"recordings": [other, intended]}
        self.save_responses()
        result = self.matcher().match(
            {
                "source": "spotify",
                "title": "Signal",
                "artist": "The Act",
                "album": "Signal Album (Expanded Edition)",
            }
        )
        self.assertEqual(result.recording_mbid, STUDIO)
        self.assertEqual(result.edition["release_track_mbid"], FIRST)

    def test_unique_title_corroborated_isrc_prioritizes_recording(self):
        self.responses[f"/ws/2/isrc/{ISRC}"] = {
            "recordings": [{"id": EXACT}],
        }
        self.save_responses()
        matcher = self.matcher()
        source = {"title": "Signal", "artist": "The Act", "isrcs": [ISRC]}
        result = matcher.match(source)
        self.assertEqual(result.recording_mbid, EXACT)
        self.assertEqual(result.provenance["isrc_recording_link"], EXACT)
        wrong = matcher.match({**source, "title": "Different Song"})
        self.assertNotIn("isrc_recording_link", wrong.provenance)

    def test_secondary_catalog_numbers_preserve_work_and_movement_identity(self):
        artist = "Fixture Performer"
        work = "Fixture Chamber Work, Op. 901"
        plain = f"{work}: I. Fixture Movement"
        for prefix in ("B.", "RV"):
            with self.subTest(prefix=prefix):
                numbered = f"{work}, {prefix} 271: I. Fixture Movement"
                self.assertTrue(agrees([numbered], artist, [plain], [artist]))
                self.assertTrue(agrees([plain], artist, [numbered], [artist]))
                for candidate in (
                    f"{work}, {prefix} 272: I. Fixture Movement",
                    f"{work}: II. Fixture Movement",
                    "Fixture Chamber Work, Op. 902: I. Fixture Movement",
                ):
                    self.assertFalse(agrees([numbered], artist, [candidate], [artist]))

    def test_spotify_recording_link_requires_title_corroboration(self):
        track_id = "00aaaaaaaaaaaaaaaaaaaa"
        url = f"https://open.spotify.com/track/{track_id}"
        self.responses[f"/ws/2/url?{url}"] = {
            "relations": [{"recording": {"id": STUDIO}}],
        }
        self.save_responses()
        matcher = self.matcher()
        track = {"title": "Signal", "artist": "Different Credit", "source_urls": [url]}
        result = matcher.match(track)
        self.assertEqual(result.recording_mbid, STUDIO)
        self.assertEqual(
            result.tags_for(track, min_confidence=0, require_song_agreement=True).get(
                "recording_mbid"
            ),
            STUDIO,
        )
        wrong = {**track, "title": "Different Song", "artist": "The Act"}
        result = matcher.match(wrong)
        self.assertNotIn(
            "recording_mbid",
            result.tags_for(wrong, min_confidence=0, require_song_agreement=True),
        )
        remaster = {
            **track,
            "source": "spotify",
            "title": "Signal - 2011 Remaster",
            "artist": "The Act",
        }
        result = matcher.match(remaster)
        tags = spotify.import_tags(
            remaster, result, listenbrainz.DEFAULT_MIN_CONFIDENCE
        )
        self.assertEqual(result.confidence, 1.0)
        self.assertEqual(tags["recording_mbid"], STUDIO)
        self.assertEqual(tags["artist"], "The Act feat. Guest")

        live = {**remaster, "title": "Signal (Live at the Arena)"}
        result = matcher.match(live)
        self.assertNotIn("recording_mbid", spotify.import_tags(live, result, 0))
        self.responses[f"/ws/2/url?{url}"]["relations"].append(
            {"recording": {"id": EXACT}}
        )
        self.save_responses()
        result = self.matcher(cache=self.path / "ambiguous.sqlite3").match(track)
        self.assertNotIn(
            "recording_mbid",
            result.tags_for(track, min_confidence=0, require_song_agreement=True),
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

    def test_indexed_credit_without_join_phrase_survives_lookup_failure(self):
        hit = recording(STUDIO, "Signal")
        hit["artist-credit"] = [dict(credit) for credit in hit["artist-credit"]]
        hit["artist-credit"][-1].pop("joinphrase")
        self.responses[
            "/ws/2/recording?artist:(the act) recording:(signal) alias:(signal)"
        ] = {"recordings": [hit]}
        self.responses[f"/ws/2/recording/{STUDIO}"] = 503
        self.save_responses()
        result = self.matcher().match({"artist": "The Act", "title": "Signal"})
        self.assertEqual(result.recording_mbid, STUDIO)
        self.assertEqual(result.status, "matched")
        self.assertFalse(
            any(i["path"].startswith("candidate.extract") for i in result.issues)
        )


if __name__ == "__main__":
    unittest.main()
