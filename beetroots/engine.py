"""Identify a recording before retrieving its canonical metadata."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from beets.autotag import Source
from beets.autotag.distance import get_track_length_grace
from beets.autotag.hooks import TrackInfo
from beets.library import Item
from beetsplug.musicbrainz import MusicBrainzPlugin

from .evidence import (
    EDITION_FIELDS,
    ISRC,
    MBID,
    SPOTIFY_ID,
    YOUTUBE_ID,
    Appearance,
    appearances,
    comparison,
    display_credit,
    edition_match,
    edition_order,
    provider_fields,
    provider_ids,
    recording_versions,
    version_evidence,
    version_match,
)
from .identity import agrees, appearance_names, base_title, names_from_credits
from .lookups import LookupFailure
from .metadata import artist_ids, collect_metadata, proposed_tags
from .schema import MatchIssue, MatchResult, TrackInput
from .video import VideoEvidence, compatible_version, length_difference


@dataclass
class Candidate:
    info: TrackInfo
    recording: dict
    paths: set[str] = field(default_factory=set)


def merge_recordings(preferred: dict, fallback: dict) -> dict:
    return {
        **fallback,
        **{
            key: value
            for key, value in preferred.items()
            if value or key not in fallback
        },
    }


class MatchRun:
    def __init__(self, engine, track: TrackInput):
        self.engine = engine
        self.mb = engine.mb
        self.plugin = engine.plugin
        self.track = track
        self.evidence = {
            key: value for key, value in track.items() if value is not None
        }
        self.issues: list[MatchIssue] = []
        self.provenance: dict = {"input": track, "providers": {}}
        self.aliases: dict[str, str] = {}
        self.browse_releases: dict[str, dict] = {}
        self.spotify_recordings: set[str] = set()
        self.isrc_recordings: set[str] = set()
        self.video_recordings: set[str] = set()
        self.video: VideoEvidence | None = None

    def issue(self, path: str, code: str, detail: str) -> None:
        issue = MatchIssue(path=path, code=code, detail=detail)
        if issue not in self.issues:
            self.issues.append(issue)

    def lookup(self, path, fun, *args, **kwargs):
        try:
            return fun(*args, **kwargs)
        except (LookupFailure, KeyError, TypeError, ValueError, IndexError) as exc:
            self.lookup_issue(path, exc)
            return None

    def pages(self, path, iterator):
        try:
            yield from iterator
        except (LookupFailure, KeyError, TypeError, ValueError, IndexError) as exc:
            self.lookup_issue(path, exc)

    def lookup_issue(self, path: str, exc: Exception) -> None:
        detail = (
            str(exc)
            if isinstance(exc, LookupFailure)
            else f"Unexpected provider data ({type(exc).__name__})"
        )
        self.issue(path, "lookup_error", detail)

    def result(self, status, **values) -> MatchResult:
        return MatchResult(
            self.track.get("record_id"),
            status,
            self.engine.fingerprint,
            provenance=self.provenance,
            issues=self.issues,
            **values,
        )

    def normalize_mbids(self) -> None:
        for key in (
            "recording_mbid",
            "release_track_mbid",
            "release_mbid",
            "release_group_mbid",
            "work_mbid",
            "artist_mbid",
        ):
            if key in self.evidence:
                self.evidence[key] = self.parse_mbid(key, self.evidence[key])
        artist_mbids = [
            *self.evidence.get("artist_mbids", []),
            *(
                credit["artist_mbid"]
                for credit in self.evidence.get("artist_credits", [])
                if "artist_mbid" in credit
            ),
        ]
        self.evidence["artist_mbids"] = list(
            dict.fromkeys(
                mbid
                for value in artist_mbids
                if (mbid := self.parse_mbid("artist_mbids", value))
            )
        )

    def parse_mbid(self, key, value):
        if not MBID.fullmatch(value):
            self.issue(key, "malformed_id", f"Invalid MusicBrainz identifier {value!r}")
            return None
        return value.lower()

    def entity(self, kind: str, mbid: str) -> dict | None:
        path = f"metadata.{kind}:{mbid}"
        before = len(self.issues)
        payload = self.lookup(path, self.mb.entity, kind, mbid)
        if not payload or not payload.get("id"):
            if len(self.issues) == before:
                self.issue(path, "unresolved_id", "MusicBrainz entity unavailable")
            return None
        return payload

    def entities(self, kind: str, identifiers: set[str]) -> dict[str, dict]:
        found = {}
        for identifier in sorted(identifiers):
            if payload := self.entity(kind, identifier):
                found[payload["id"]] = payload
        return found

    def track_info(self, normalized: dict) -> TrackInfo:
        recording = {
            "title": None,
            "length": None,
            "disambiguation": "",
            **normalized,
        }
        if not recording.get("artist_credit"):
            return TrackInfo(
                track_id=recording["id"],
                title=recording["title"],
                length=(recording["length"] / 1000 if recording["length"] else None),
                artist=None,
                trackdisambig=recording["disambiguation"],
                data_source=self.plugin.data_source,
            )
        # Indexed search commonly omits the last join phrase. A hit can
        # supply candidate evidence when the detailed lookup is unavailable.
        recording["artist_credit"] = [
            {"joinphrase": "", **credit} for credit in recording["artist_credit"]
        ]
        return self.plugin.track_info(recording)

    def resolve_recording(self, path: str, mbid: str) -> dict | None:
        before = len(self.issues)
        payload = self.lookup(path, self.mb.recording_core, mbid)
        if payload and payload.get("id"):
            self.aliases[mbid] = payload["id"]
            return payload
        if len(self.issues) == before:
            self.issue(path, "unresolved_id", "MusicBrainz recording unavailable")
        return None

    def recording_appearances(self, release: dict, mbid: str):
        for occurrence in appearances(release):
            raw_id = (occurrence.track.get("recording") or {}).get("id")
            if raw_id and self.aliases.get(raw_id, raw_id) == mbid:
                yield occurrence

    def release_track(self, mbid: str) -> tuple[str, Appearance] | None:
        hits = list(
            self.pages("release_track_mbid.search", self.mb.search_pages(tid=mbid))
        )
        releases = set()
        verified = {}
        for hit in hits:
            for release in hit.get("releases") or []:
                releases.add(release["id"])
        if self.evidence.get("release_mbid"):
            releases.add(self.evidence["release_mbid"])

        def verify(release_ids: set[str]) -> None:
            for release_id in sorted(release_ids):
                release = self.lookup(
                    f"release_track_mbid.tracklist:{release_id}",
                    self.mb.release_core,
                    release_id,
                )
                if not release:
                    continue
                for occurrence in appearances(release):
                    if occurrence.track_id != mbid:
                        continue
                    recording_id = (occurrence.track.get("recording") or {}).get("id")
                    if recording_id and (
                        recording := self.resolve_recording(
                            "release_track_mbid.recording", recording_id
                        )
                    ):
                        verified[recording["id"]] = occurrence

        verify(releases)
        if hits and not verified:
            browse = self.pages(
                "release_track_mbid.releases", self.mb.pages("release", track=mbid)
            )
            verify({release["id"] for release in browse} - releases)
        if len(verified) == 1:
            return next(iter(verified.items()))
        if verified:
            self.issue(
                "release_track_mbid",
                "conflicting_ids",
                "Track occurrence refers to multiple recordings",
            )
        else:
            self.issue(
                "release_track_mbid",
                "unresolved_id",
                "Exact track MBID could not be verified in a release tracklist",
            )
        return None

    def supplied_release(self) -> dict | None:
        mbid = self.evidence.get("release_mbid")
        if not mbid:
            return None
        before = len(self.issues)
        release = self.lookup("release_mbid", self.mb.release_core, mbid)
        if release and release.get("id"):
            self.evidence["release_mbid"] = release["id"]
            self.browse_releases[release["id"]] = release
            return release
        if len(self.issues) == before:
            self.issue(
                "release_mbid", "unresolved_id", "MusicBrainz release unavailable"
            )
        return None

    def enrich(self) -> None:
        spotify_ids = []
        youtube_ids = []
        urls = list(self.evidence.get("source_urls", []))
        if uri := self.evidence.get("spotify_uri"):
            spotify_ids.append(uri.removeprefix("spotify:track:"))
        if spotify := self.evidence.get("spotify_track_id"):
            spotify_ids.insert(0, spotify)
        if youtube := self.evidence.get("youtube_video_id"):
            youtube_ids.append(youtube)
        for url in urls:
            try:
                spotify, youtube = provider_ids(url)
            except ValueError:
                self.issue(
                    "source_urls", "malformed_url", f"Invalid source URL {url!r}"
                )
                continue
            if spotify:
                spotify_ids.append(spotify)
            if youtube:
                youtube_ids.append(youtube)
        video_input = "video_title" in self.evidence
        isrcs = list(self.evidence.get("isrcs", []))
        providers = self.engine.providers
        for provider, ids, pattern, fetch, enabled in (
            (
                "spotify",
                spotify_ids,
                SPOTIFY_ID,
                providers.spotify,
                self.engine.spotify_enabled,
            ),
            (
                "youtube_music",
                youtube_ids,
                YOUTUBE_ID,
                providers.watch,
                self.engine.settings.youtube_music,
            ),
        ):
            metadata = {}
            for identifier in dict.fromkeys(ids):
                if not pattern.fullmatch(identifier):
                    self.issue(
                        provider,
                        "malformed_id",
                        f"Invalid provider identifier {identifier!r}",
                    )
                    continue
                if provider == "spotify":
                    urls.append(f"https://open.spotify.com/track/{identifier}")
                else:
                    urls.append(f"https://www.youtube.com/watch?v={identifier}")
                if not enabled:
                    continue
                path = f"{provider}:{identifier}"
                before = len(self.issues)
                raw = self.lookup(path, fetch, identifier)
                if raw is None:
                    if len(self.issues) == before:
                        self.issue(
                            path, "provider_not_found", "Provider track unavailable"
                        )
                    continue
                fetched = self.lookup(path, provider_fields, raw, provider)
                if fetched is None:
                    continue
                supplemental = {}
                if provider == "youtube_music" and (
                    album_id := (raw.get("album") or {}).get("id")
                ):
                    album = (
                        self.lookup(f"{path}.album", providers.youtube_album, album_id)
                        or {}
                    )
                    track = next(
                        (
                            t
                            for t in album.get("tracks") or []
                            if t.get("videoId") == identifier
                        ),
                        None,
                    )
                    if track:
                        supplemental = provider_fields(
                            {**track, "album": album}, provider
                        )
                metadata[identifier] = {
                    "fields": {**supplemental, **fetched},
                    "response": raw,
                }
                fields = metadata[identifier]["fields"]
                if video_input and provider == "youtube_music":
                    isrcs.extend(fields.get("isrcs", []))
                    continue
                for key, value in fields.items():
                    if key == "isrcs":
                        isrcs.extend(value)
                    elif not self.evidence.get(key) and not (
                        key == "artist_credits" and self.evidence.get("artist")
                    ):
                        self.evidence[key] = value
            if metadata:
                self.provenance["providers"][provider] = metadata
        if video_input:
            metadata = self.provenance["providers"].get("youtube_music", {})
            tags = self.provenance["source_tags"] = {}
            self.video = VideoEvidence.apply(
                self.evidence, next(iter(metadata.values()), {}), tags
            )
        self.evidence["source_urls"] = list(
            dict.fromkeys(
                url for url in urls if url.startswith(("https://", "http://"))
            )
        )
        self.evidence["isrcs"] = list(
            dict.fromkeys(code.replace("-", "").upper() for code in isrcs)
        )

    def constraint_ids(self) -> None:
        for key, kind in (
            ("release_group_mbid", "release-group"),
            ("artist_mbid", "artist"),
            ("artist_mbids", "artist"),
            ("work_mbid", "work"),
        ):
            request = self.evidence.get(key)
            identifiers = (
                request if isinstance(request, list) else [request] if request else []
            )
            resolved = []
            for identifier in identifiers:
                before = len(self.issues)
                payload = self.lookup(
                    f"{key}:{identifier}", self.mb.raw, f"{kind}/{identifier}"
                )
                if payload and payload.get("id"):
                    resolved.append(payload["id"])
                else:
                    if len(self.issues) == before:
                        self.issue(
                            f"{key}:{identifier}",
                            "unresolved_id",
                            "MusicBrainz entity unavailable",
                        )
                    resolved.append(identifier)
            if identifiers:
                self.evidence[key] = (
                    resolved if isinstance(request, list) else resolved[0]
                )

    def release_appearances(self, mbid: str) -> list[Appearance]:
        releases = dict(self.browse_releases)
        for hit in self.pages(
            f"edition.releases:{mbid}",
            self.mb.pages(
                "release",
                recording=mbid,
                inc="recordings+artist-credits+release-groups+labels",
            ),
        ):
            if hit.get("media") and any(
                medium.get("tracks")
                or medium.get("pregap")
                or medium.get("data-tracks")
                for medium in hit["media"]
            ):
                releases[hit["id"]] = hit
            elif hit["id"] not in releases:
                path = f"edition.release:{hit['id']}"
                if release := self.lookup(path, self.mb.release_core, hit["id"]):
                    releases[release["id"]] = release
        return [
            occurrence
            for release in releases.values()
            for occurrence in self.recording_appearances(release, mbid)
        ]

    def edition_values(self) -> dict:
        return {
            key: self.evidence[key]
            for key in EDITION_FIELDS
            if self.evidence.get(key)
            or (key == "track_number" and self.evidence.get(key) == 0)
        }

    def filter_editions(
        self, options: dict[str, list[Appearance]], values: dict
    ) -> tuple[dict[str, list[Appearance]], Counter[str]]:
        identified: Counter[str] = Counter()
        for key, value in values.items():
            compared = {
                mbid: [
                    (
                        appearance,
                        edition_match(
                            key,
                            value,
                            appearance.fields()[key],
                            normalize_album_ampersand=self.video is not None,
                        ),
                    )
                    for appearance in choices
                ]
                for mbid, choices in options.items()
            }
            if not any(
                flag is True for choices in compared.values() for _, flag in choices
            ):
                continue
            options = {
                mbid: [appearance for appearance, flag in choices if flag is not False]
                for mbid, choices in compared.items()
                if not choices or any(flag is not False for _, flag in choices)
            }
            identified.update(
                appearance.track_id
                for choices in compared.values()
                for appearance, flag in choices
                if flag is True
            )
        return options, identified

    def supplemental(self, titles: list[str]) -> dict[str, list[tuple[str, dict]]]:
        hits = defaultdict(list)

        def add(path: str, recordings) -> None:
            for recording in recordings:
                if (
                    recording
                    and (identifier := recording.get("id"))
                    and MBID.fullmatch(identifier)
                ):
                    hits[identifier].append((path, recording))

        for code in self.evidence.get("isrcs", []):
            if not ISRC.fullmatch(code):
                self.issue("isrcs", "malformed_id", f"Invalid ISRC {code!r}")
                continue
            path = f"isrc:{code}"
            response = self.lookup(
                path, self.mb.raw, f"isrc/{code}", inc="artist-credits"
            )
            if response:
                recordings = response.get("recordings") or []
                add(path, recordings)
                self.isrc_recordings.update(
                    recording["id"]
                    for recording in recordings
                    if recording and MBID.fullmatch(recording.get("id", ""))
                )
        for url in self.evidence.get("source_urls", []):
            path = f"url:{url}"
            response = self.lookup(
                path,
                self.mb.raw,
                "url",
                resource=url,
                inc="recording-rels+release-rels+release-group-rels",
            )
            for relation in (response or {}).get("relations") or []:
                if recording := relation.get("recording"):
                    add(path, [recording])
                    stream_id = url.removeprefix("https://open.spotify.com/track/")
                    if (
                        SPOTIFY_ID.fullmatch(stream_id)
                        and not relation.get("ended")
                        and MBID.fullmatch(recording.get("id", ""))
                    ):
                        self.spotify_recordings.add(recording["id"])
                    if (
                        self.video
                        and relation.get("type") in {"free streaming", "streaming"}
                        and not relation.get("ended")
                        and MBID.fullmatch(recording.get("id", ""))
                        and provider_ids(url)[1]
                    ):
                        self.video_recordings.add(recording["id"])
                elif release := relation.get("release"):
                    if payload := self.lookup(
                        path, self.mb.release_core, release["id"]
                    ):
                        self.browse_releases[payload["id"]] = payload
                        add(
                            path,
                            (
                                entry.track.get("recording")
                                for entry in appearances(payload)
                            ),
                        )
                elif group := relation.get("release-group"):
                    add(path, self.pages(path, self.mb.search_pages(rgid=group["id"])))
        release_id = self.evidence.get("release_mbid")
        if release := self.browse_releases.get(release_id):
            add(
                "release_mbid",
                (entry.track.get("recording") for entry in appearances(release)),
            )
        if group_id := self.evidence.get("release_group_mbid"):
            add(
                "release_group_mbid",
                self.pages(
                    "release_group_mbid.search", self.mb.search_pages(rgid=group_id)
                ),
            )
        for mbid in dict.fromkeys(
            filter(
                None, [self.evidence.get("artist_mbid"), *self.evidence["artist_mbids"]]
            )
        ):
            filters = {"arid": mbid}
            if titles:
                filters["recording"] = titles[0]
                path = f"artist_mbid.search:{mbid}"
                add("artist_mbid", self.pages(path, self.mb.search_pages(**filters)))
            else:
                path = f"artist_mbid.browse:{mbid}"
                add(
                    "artist_mbid",
                    self.pages(
                        path,
                        self.mb.pages("recording", artist=mbid, inc="artist-credits"),
                    ),
                )
        if work_id := self.evidence.get("work_mbid"):
            add(
                "work_mbid",
                self.pages(
                    "work_mbid.browse",
                    self.mb.pages("recording", work=work_id, inc="artist-credits"),
                ),
            )
        edition_filters = {
            field: str(self.evidence[key])
            for key, field in (
                ("album", "release"),
                ("release_date", "date"),
                ("country", "country"),
                ("format", "format"),
                ("disc_number", "position"),
                ("track_number", "tnum"),
            )
            if self.evidence.get(key)
        }
        if titles and edition_filters:
            results = self.lookup(
                "edition.search",
                self.mb.search,
                "recording",
                {
                    "recording": titles[0],
                    **edition_filters,
                },
                limit=self.engine.settings.search_limit,
            )
            add("edition.search", results or [])
        release_filters = {
            field: self.evidence[key]
            for key, field in (
                ("label", "label"),
                ("catalog_number", "catno"),
                ("barcode", "barcode"),
            )
            if self.evidence.get(key)
        }
        if release_filters and not release_id:
            if album := self.evidence.get("album"):
                release_filters["release"] = album
            results = self.lookup(
                "edition.release_search",
                self.mb.search,
                "release",
                release_filters,
                limit=self.engine.settings.search_limit,
            )
            for hit in results or []:
                if payload := self.lookup(
                    "edition.tracklist", self.mb.release_core, hit["id"]
                ):
                    self.browse_releases[payload["id"]] = payload
                    add(
                        "edition.release_search",
                        (
                            entry.track.get("recording")
                            for entry in appearances(payload)
                        ),
                    )
        return hits

    def find_candidates(self, source: Source, titles: list[str]) -> list[Candidate]:
        text_hits: dict[str, list[tuple[str, dict]]] = {}

        def add_hits(hits: list[dict], path: str) -> None:
            for hit in hits:
                identifier = hit.get("id")
                if identifier and MBID.fullmatch(identifier):
                    text_hits.setdefault(identifier, []).append((path, hit))

        search_titles = [] if self.video and not self.video.artist else titles or [""]
        for title in search_titles:
            if not title and not source.artist:
                continue
            results = self.lookup(
                "text.search",
                self.plugin._get_candidates,
                "track",
                source.items,
                source.artist,
                title,
                False,
            )
            add_hits(results or [], "text")
        # A Spotify artist can be a single performer from a longer MusicBrainz
        # credit. The ordinary combined-credit search can miss that recording.
        artist = self.video.artist if self.video else source.artist
        if (
            artist
            and titles
            and not any(
                agrees(
                    titles,
                    artist,
                    [hit.get("title")],
                    [
                        display_credit(hit.get("artist-credit"), "joinphrase"),
                        *names_from_credits(hit.get("artist-credit") or []),
                    ],
                )
                for hits in text_hits.values()
                for _, hit in hits
            )
        ):
            path = "text.artistname"
            results = self.lookup(
                path,
                self.mb.recordings_by_credit,
                artist,
                base_title(titles[0]),
                limit=self.engine.settings.search_limit,
            )
            add_hits(results or [], path)
        return self.resolve_candidates(text_hits, self.supplemental(titles))

    def duration_candidates(
        self, artist: str, title: str, length: float, *, video: bool | None = None
    ) -> list[Candidate]:
        path = "text.duration"
        grace = get_track_length_grace()
        bounds = (max(0, int((length - grace) * 1000)), int((length + grace) * 1000))
        results = self.lookup(
            path,
            self.mb.recordings_by_credit,
            artist,
            base_title(title),
            limit=self.engine.settings.search_limit,
            duration=bounds,
            video=video,
        )
        return self.resolve_candidates(
            {
                mbid: [(path, hit)]
                for hit in results or []
                if (mbid := hit.get("id")) and MBID.fullmatch(mbid)
            },
            {},
        )

    def resolve_candidates(self, text_hits, supplemental_hits) -> list[Candidate]:
        candidates: dict[str, Candidate] = {}
        text_order = []
        for identifier in dict.fromkeys([*text_hits, *sorted(supplemental_hits)]):
            records = text_hits.get(identifier, []) + supplemental_hits.get(
                identifier, []
            )
            before = len(self.issues)
            raw = self.lookup(
                f"candidate.recording:{identifier}", self.mb.recording_core, identifier
            )
            if raw is None and len(self.issues) == before:
                self.issue(
                    f"candidate.recording:{identifier}",
                    "unresolved_id",
                    "MusicBrainz recording unavailable",
                )
                continue
            recording = raw or {}
            for _, snapshot in records:
                # The lookup is preferred. Search and linked track payloads
                # can still supply evidence when an individual path fails.
                recording = merge_recordings(recording, snapshot)
            info = self.lookup(
                f"candidate.extract:{identifier}",
                self.track_info,
                self.mb._normalize_data(recording),
            )
            if info is None:
                continue
            canonical = info.track_id
            self.aliases[identifier] = canonical
            candidate = candidates.setdefault(canonical, Candidate(info, recording))
            merged = merge_recordings(candidate.recording, recording)
            if merged != candidate.recording:
                extracted = self.lookup(
                    f"candidate.extract:{canonical}",
                    self.track_info,
                    self.mb._normalize_data(merged),
                )
                if extracted:
                    candidate.info = extracted
                candidate.recording = merged
            candidate.paths.update(path for path, _ in records)
            if identifier in text_hits and canonical not in text_order:
                text_order.append(canonical)
        return [
            candidates[mbid]
            for mbid in [*text_order, *sorted(candidates.keys() - set(text_order))]
        ]

    def score(
        self, candidates: list[Candidate], item: Item, titles: list[str]
    ) -> tuple[str, float, Appearance | None] | None:
        requested_artists = set(
            filter(
                None, [self.evidence.get("artist_mbid"), *self.evidence["artist_mbids"]]
            )
        )
        if requested_artists:
            states = [
                requested_artists.issubset(ids)
                if (ids := artist_ids(candidate.recording))
                else None
                for candidate in candidates
            ]
            if True in states:
                candidates = [
                    candidate
                    for candidate, state in zip(candidates, states)
                    if state is not False
                ]

        if self.evidence.get("work_mbid") and any(
            "work_mbid" in candidate.paths for candidate in candidates
        ):
            candidates = [
                candidate
                for candidate in candidates
                if "work_mbid" in candidate.paths
                or "relations" not in candidate.recording
                or any(
                    (relation.get("work") or {}).get("id") == self.evidence["work_mbid"]
                    for relation in candidate.recording["relations"]
                )
            ]

        def recording_titles(candidate: Candidate) -> list[str | None]:
            return [
                candidate.info.title,
                *(
                    alias.get("name")
                    for alias in (candidate.recording.get("aliases") or [])
                ),
            ]

        def recording_names(candidate: Candidate) -> list[str | None]:
            credits = candidate.recording.get("artist-credit") or []
            return [
                candidate.info.artist,
                display_credit(credits, "joinphrase"),
                *names_from_credits(credits),
            ]

        linked = None
        for link_kind, identifiers in (
            ("youtube_recording_link", self.video_recordings),
            ("spotify_recording_link", self.spotify_recordings),
            ("isrc_recording_link", self.isrc_recordings),
        ):
            linked_ids = {self.aliases.get(mbid, mbid) for mbid in identifiers}
            if not titles or len(linked_ids) != 1:
                continue
            linked = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate.info.track_id in linked_ids
                    and (
                        link_kind == "youtube_recording_link"
                        or agrees(titles, "", recording_titles(candidate), [])
                    )
                ),
                None,
            )
            if linked is not None:
                candidates = [linked]
                break

        # Keep release-search hits long enough to check track titles and credits.
        spotify = self.evidence.get("source") == "spotify"
        source_artists = self.video.artists if self.video else (item.artist,)

        def song_agreement(known_titles, known_names):
            if not self.video:
                return agrees(titles, item.artist, known_titles, known_names)
            return bool(titles) and any(
                artist and agrees(titles, artist, known_titles, known_names)
                for artist in source_artists
            )

        same_song = set()
        if titles and any(source_artists):
            same_song = {
                candidate.info.track_id
                for candidate in candidates
                if song_agreement(
                    recording_titles(candidate), recording_names(candidate)
                )
            }
            if same_song:
                candidates = [
                    candidate
                    for candidate in candidates
                    if candidate.info.track_id in same_song
                    or ((spotify or self.video) and "edition.search" in candidate.paths)
                ]

        # Compare versions only after establishing which candidates share
        # the song. A live version of another song is still another song.
        for qualifier in () if self.video else ("version", "disambiguation"):
            if requested := self.evidence.get(qualifier):
                states = [
                    version_match(
                        requested,
                        candidate.info.title or "",
                        candidate.info.trackdisambig or "",
                    )
                    for candidate in candidates
                ]
                if True in states:
                    candidates = [
                        candidate
                        for candidate, state in zip(candidates, states)
                        if state is not False
                    ]

        options = {}
        values = self.edition_values()
        for candidate in candidates:
            mbid = candidate.info.track_id
            options[mbid] = (
                self.release_appearances(mbid)
                if values
                else [
                    occurrence
                    for release in self.browse_releases.values()
                    for occurrence in self.recording_appearances(release, mbid)
                ]
            )
        compatible = set(same_song)
        agreeing_tracks = set()
        if (spotify or self.video) and item.album:
            for candidate in candidates:
                mbid = candidate.info.track_id
                names = recording_names(candidate)
                known_titles = recording_titles(candidate)
                matches_album = [
                    a.track_id
                    for a in options[mbid]
                    if edition_match(
                        "album",
                        item.album,
                        a.release.get("title"),
                        normalize_album_ampersand=self.video is not None,
                    )
                    and song_agreement(
                        [*known_titles, a.track.get("title")],
                        [*names, *appearance_names(a.track, a.release)],
                    )
                ]
                if matches_album:
                    compatible.add(mbid)
                    agreeing_tracks.update(matches_album)
            if compatible.intersection(options) and not linked and not self.video:
                options = {
                    mbid: choices
                    for mbid, choices in options.items()
                    if mbid in compatible
                }
        if self.video and not linked:
            options = self.video_options(candidates, options, compatible, item, titles)
        options, identified = self.filter_editions(options, values)
        matches = []
        wanted_versions = recording_versions(titles[0]) if spotify and titles else set()
        credits = (
            self.evidence.get("artist_credits")
            if not self.evidence.get("artist")
            else None
        )
        for candidate in candidates:
            mbid = candidate.info.track_id
            if mbid not in options:
                continue
            observed_versions = (
                recording_versions(
                    candidate.info.title or "", candidate.info.trackdisambig or ""
                )
                if spotify
                else set()
            )
            video = bool(candidate.recording.get("video"))
            wrong_medium = (
                self.video.preferred_video not in (None, video)
                if self.video
                else spotify and video
            )
            choices = options[mbid]
            # Prefer positively identified editions when otherwise tied.
            choices = sorted(
                choices,
                key=lambda choice: (
                    -identified[choice.track_id],
                    edition_order(choice),
                ),
            )
            for appearance in choices or [None]:
                selected = (
                    appearance
                    if appearance and (appearance.track_id in identified or not values)
                    else None
                )
                song_agrees = mbid in same_song or (
                    selected is not None and selected.track_id in agreeing_tracks
                )
                difference = None
                missing_duration = False
                unmatched_album = False
                if self.video and candidate is not linked:
                    difference = length_difference(
                        item.length,
                        candidate.info.length,
                        (selected.track.get("length") or 0) / 1000
                        if selected
                        else None,
                    )
                    missing_duration = bool(item.length and difference is None)
                    unmatched_album = bool(
                        item.album
                        and (
                            not selected
                            or not edition_match(
                                "album",
                                item.album,
                                selected.release.get("title"),
                                normalize_album_ampersand=True,
                            )
                        )
                    )
                versions = observed_versions
                if spotify and selected:
                    versions = versions | recording_versions(
                        selected.track.get("title") or ""
                    )
                if not wanted_versions:
                    # A plain title can refer to the standard album or single
                    # cut. Keep those when considering a named mix or take.
                    versions = versions - {"album version", "single edit"}
                variant = int(wanted_versions != versions)
                if (
                    wanted_versions
                    and versions
                    and wanted_versions.isdisjoint(versions)
                ):
                    variant = 2
                distance = comparison(
                    item,
                    candidate.info,
                    titles,
                    appearance,
                    credits,
                    self.evidence.get("track_number"),
                )
                if distance is not None:
                    matches.append(
                        (
                            (
                                not song_agrees,
                                wrong_medium,
                                variant,
                                unmatched_album,
                                missing_duration,
                                distance,
                                difference or 0,
                            ),
                            candidate,
                            selected,
                        )
                    )
        if not matches:
            return None
        # With comparable identity evidence, preserve beets' ranking.
        (_, _, _, _, _, distance, _), winner, selected = min(
            matches, key=lambda match: match[0]
        )
        self.provenance["candidate_paths"] = sorted(winner.paths)
        if winner is linked:
            self.provenance[link_kind] = winner.info.track_id
        return winner.info.track_id, float(distance), selected

    def video_options(
        self,
        candidates: list[Candidate],
        options: dict[str, list[Appearance]],
        compatible: set[str],
        item: Item,
        titles: list[str],
    ) -> dict[str, list[Appearance]]:
        """Filter recording and tracklist evidence before choosing an edition."""
        if not titles:
            return {}
        eligible = {}
        grace = get_track_length_grace()
        version = self.evidence.get("version") or self.evidence.get("disambiguation")
        for candidate in candidates:
            mbid = candidate.info.track_id
            if mbid not in compatible:
                continue
            releases = [a.release for a in options[mbid]] or (
                candidate.recording.get("releases") or []
            )
            only_live = bool(releases) and all(
                "Live"
                in ((release.get("release-group") or {}).get("secondary-types") or [])
                for release in releases
            )
            disambiguation = candidate.info.trackdisambig or ""
            valid_appearances = []
            for appearance in [*options[mbid], None]:
                edition = (
                    appearance
                    if appearance
                    and edition_match(
                        "album",
                        item.album,
                        appearance.release.get("title"),
                        normalize_album_ampersand=True,
                    )
                    else None
                )
                difference = length_difference(
                    item.length,
                    candidate.info.length,
                    (edition.track.get("length") or 0) / 1000 if edition else None,
                )
                if not compatible_version(
                    titles[0],
                    version,
                    candidate.info.title or "",
                    f"{disambiguation} live"
                    if only_live and not edition
                    else disambiguation,
                    (edition.track.get("title") or "") if edition else "",
                ):
                    continue
                if difference is None:
                    if item.length and not edition:
                        continue
                elif difference > grace:
                    continue
                eligible[mbid] = valid_appearances
                if appearance:
                    valid_appearances.append(appearance)
        return eligible

    def finish(
        self, mbid: str, method, distance: float | None, selected: Appearance | None
    ) -> MatchResult:
        identity = {
            "recording_mbid": mbid,
            "method": method,
            "confidence": 1.0 if distance is None else 1.0 - distance,
            "distance": distance,
        }
        if not (recording := self.entity("recording", mbid)):
            return self.result("lookup_error", **identity)
        identity["recording_mbid"] = recording["id"]
        if self.video and method == "metadata":
            self.provenance["corroborated_recording"] = recording["id"]
        self.aliases[mbid] = recording["id"]
        collected = self.lookup("metadata", collect_metadata, self, recording, selected)
        if collected is None:
            return self.result("lookup_error", **identity)
        metadata, occurrence, inferred = collected
        edition = (
            {
                "release_mbid": occurrence.release_id,
                "release_track_mbid": occurrence.track_id,
                "inferred": inferred,
            }
            if occurrence
            else None
        )
        tags = self.lookup("metadata.tags", proposed_tags, self, metadata, occurrence)
        return self.result(
            "matched" if tags is not None else "lookup_error",
            **identity,
            edition=edition,
            metadata=metadata,
            proposed_tags=tags,
        )

    def run(self) -> MatchResult:
        self.normalize_mbids()
        recording = None
        exact_track = None
        if mbid := self.evidence.get("recording_mbid"):
            recording = self.resolve_recording("recording_mbid", mbid)
        release = self.supplied_release()
        if mbid := self.evidence.get("release_track_mbid"):
            exact_track = self.release_track(mbid)
        if recording and exact_track and recording["id"] != exact_track[0]:
            self.issue(
                "recording_mbid",
                "conflicting_ids",
                "The recording and release-track MBIDs resolve to different recordings",
            )
            return self.result("conflicting_ids")
        if recording or exact_track:
            mbid = recording["id"] if recording else exact_track[0]
            method = "recording_mbid" if recording else "release_track_mbid"
            selected = exact_track[1] if exact_track else None
            release_options = (
                list(self.recording_appearances(release, mbid)) if release else []
            )
            if release and (
                not release_options
                or (selected and selected.release_id != release["id"])
            ):
                self.issue(
                    "release_mbid",
                    "conflicting_ids",
                    "The supplied release conflicts with the verified identity or track occurrence",
                )
            values = self.edition_values()
            self.constraint_ids()
            if not selected and values:
                values = self.edition_values()
                # Once a supplied release is verified, only its track
                # occurrences can supply edition tags for that input.
                options = release_options or self.release_appearances(mbid)
                possible, identified = self.filter_editions({mbid: options}, values)
                verified = [
                    a for a in possible.get(mbid, []) if a.track_id in identified
                ]
                if verified or release_options:
                    selected = min(
                        verified or release_options,
                        key=lambda appearance: (
                            -identified[appearance.track_id],
                            edition_order(appearance),
                        ),
                    )
            return self.finish(mbid, method, None, selected)
        self.constraint_ids()
        self.enrich()
        titles = list(
            dict.fromkeys(
                title.strip()
                for title in [
                    self.evidence.get("title", ""),
                    *self.evidence.get("alternate_titles", []),
                ]
                if title.strip()
            )
        )
        if titles and not self.evidence.get("version"):
            self.evidence["version"] = version_evidence(titles[0])
        item = Item(
            title=titles[0] if titles else "",
            artist=(
                self.evidence.get("artist")
                or display_credit(self.evidence.get("artist_credits"))
            ).strip(),
            album=self.evidence.get("album") or "",
            length=(self.evidence.get("duration_ms") or 0) / 1000,
            disc=self.evidence.get("disc_number") or 0,
            track=self.evidence.get("track_number") or 0,
            data_source=self.plugin.data_source,
        )
        search_titles = list(
            dict.fromkeys(
                term for title in titles for term in (title, base_title(title)) if term
            )
        )
        candidates = self.find_candidates(Source.from_item(item), search_titles)
        selected = self.score(candidates, item, titles) if candidates else None
        if self.video and self.video.artist and titles and item.length:
            check_album = find_video = False
            if selected and not self.provenance.get("youtube_recording_link"):
                recording_id, _, occurrence = selected
                check_album = bool(
                    item.album
                    and not (
                        occurrence
                        and edition_match(
                            "album",
                            item.album,
                            occurrence.release.get("title"),
                            normalize_album_ampersand=True,
                        )
                    )
                )
                find_video = self.video.preferred_video is True and not any(
                    c.info.track_id == recording_id and c.recording.get("video")
                    for c in candidates
                )
            if selected is None or find_video or check_album:
                more = self.duration_candidates(
                    self.video.artist,
                    titles[0],
                    item.length,
                    video=True if find_video else None,
                )
                seen = {candidate.info.track_id for candidate in candidates}
                new = [
                    candidate
                    for candidate in more
                    if candidate.info.track_id not in seen
                ]
                if new:
                    candidates.extend(new)
                    selected = self.score(candidates, item, titles)
        if not candidates:
            if any(issue["code"] == "lookup_error" for issue in self.issues):
                return self.result("lookup_error")
            if (
                self.track.get("recording_mbid") is not None
                or self.track.get("release_track_mbid") is not None
            ):
                return self.result("unresolved_id")
            return self.result("no_candidates")
        if selected is None:
            return self.result("insufficient_evidence")
        mbid, distance, occurrence = selected
        return self.finish(mbid, "metadata", distance, occurrence)


class Engine:
    def __init__(self, settings, musicbrainz, providers, fingerprint):
        self.settings = settings
        self.mb = musicbrainz
        self.providers = providers
        self.fingerprint = fingerprint
        self.spotify_enabled = bool(
            settings.spotify_client_id and settings.spotify_client_secret
        )
        self.plugin = MusicBrainzPlugin()
        self.plugin.mb_api = self.mb
