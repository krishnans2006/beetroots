"""Identify a recording before retrieving its canonical metadata."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from beets.autotag import Source
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
    version_evidence,
    version_match,
)
from .lookups import LookupFailure
from .metadata import artist_ids, collect_metadata, proposed_tags
from .schema import MatchIssue, MatchResult, TrackInput


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
                for key, value in fields.items():
                    if key == "isrcs":
                        isrcs.extend(value)
                    elif not self.evidence.get(key) and not (
                        key == "artist_credits" and self.evidence.get("artist")
                    ):
                        self.evidence[key] = value
            if metadata:
                self.provenance["providers"][provider] = metadata
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
        values = {
            key: self.evidence[key]
            for key in EDITION_FIELDS
            if self.evidence.get(key)
            or (key == "track_number" and self.evidence.get(key) == 0)
        }
        if "album_artists" in values:
            values["album_artists"] = display_credit(values["album_artists"])
        return values

    def filter_editions(
        self, options: dict[str, list[Appearance]], values: dict
    ) -> tuple[dict[str, list[Appearance]], Counter[str]]:
        identified: Counter[str] = Counter()
        for key, value in values.items():
            compared = {
                mbid: [
                    (appearance, edition_match(key, value, appearance.fields()[key]))
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
                add(path, response.get("recordings") or [])
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
        for title in titles or [""]:
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
            for hit in results or []:
                identifier = hit.get("id")
                if identifier and MBID.fullmatch(identifier):
                    text_hits.setdefault(identifier, []).append(("text", hit))
        supplemental_hits = self.supplemental(titles)
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
        for key in ("version", "disambiguation"):
            if requested := self.evidence.get(key):
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
        options, identified = self.filter_editions(options, values)
        matches = []
        credits = (
            self.evidence.get("artist_credits")
            if not self.evidence.get("artist")
            else None
        )
        for candidate in candidates:
            mbid = candidate.info.track_id
            if mbid not in options:
                continue
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
                distance = comparison(
                    item,
                    candidate.info,
                    titles,
                    appearance,
                    credits,
                    self.evidence.get("track_number"),
                )
                if distance is not None:
                    selected = (
                        appearance
                        if appearance
                        and (appearance.track_id in identified or not values)
                        else None
                    )
                    matches.append((distance, candidate, selected))
        if not matches:
            return None
        # min keeps the first candidate at a tied beets distance.
        distance, winner, selected = min(matches, key=lambda match: match[0])
        self.provenance["candidate_paths"] = sorted(winner.paths)
        return winner.info.track_id, float(distance), selected

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
        candidates = self.find_candidates(Source.from_item(item), titles)
        if not candidates:
            if any(issue["code"] == "lookup_error" for issue in self.issues):
                return self.result("lookup_error")
            if (
                self.track.get("recording_mbid") is not None
                or self.track.get("release_track_mbid") is not None
            ):
                return self.result("unresolved_id")
            return self.result("no_candidates")
        selected = self.score(candidates, item, titles)
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
