"""Input normalization and evidence from verified MusicBrainz tracklists."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from beets.autotag.distance import (
    VA_ARTISTS,
    Distance,
    string_dist,
    track_distance,
    track_index_changed,
)
from beets.autotag.hooks import TrackInfo

MBID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
ISRC = re.compile(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}")
SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{22}")
YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}")


def display_credit(credits: str | list | None, join_key: str = "join_phrase") -> str:
    if isinstance(credits, str):
        return credits
    credits = credits or []
    parts = []
    for index, credit in enumerate(credits):
        default_join = ", " if index + 1 < len(credits) else ""
        if isinstance(credit, str):
            parts.extend([credit, default_join])
        else:
            parts.extend([credit.get("name", ""), credit.get(join_key, default_join)])
    return "".join(parts)


def provider_ids(url: str) -> tuple[str | None, str | None]:
    if url.startswith("spotify:track:"):
        return url.removeprefix("spotify:track:"), None
    parts = urlsplit(url)
    path = parts.path.strip("/").split("/")
    if parts.hostname == "open.spotify.com" and len(path) >= 2 and path[-2] == "track":
        return path[-1], None
    if parts.hostname == "youtu.be":
        return None, path[0]
    if parts.hostname in {
        "music.youtube.com",
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
    }:
        if parts.path == "/watch":
            return None, parse_qs(parts.query).get("v", [None])[0]
        if len(path) >= 2 and path[0] in {"shorts", "embed"}:
            return None, path[1]
    return None, None


def duration_ms(length: object) -> int | None:
    if not isinstance(length, str) or not re.fullmatch(
        r"\d+(?::[0-5]?\d){1,2}", length
    ):
        return None
    seconds = 0
    for part in length.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds * 1000


def provider_fields(raw: dict, provider: str) -> dict:
    spotify = provider == "spotify"
    artists = [
        {"name": artist["name"]}
        for artist in raw.get("artists") or []
        if artist.get("name")
    ]
    album = raw.get("album") or {}
    isrc = (raw.get("external_ids") or {}).get("isrc") or raw.get("isrc")
    fields = {
        "title": raw.get("name" if spotify else "title"),
        "artist_credits": artists,
        "album": album.get("name"),
        "album_artists": [
            {"name": artist["name"]}
            for artist in album.get("artists") or []
            if artist.get("name")
        ],
        "duration_ms": raw.get("duration_ms") or duration_ms(raw.get("length")),
        "track_number": raw.get("track_number" if spotify else "trackNumber"),
        "disc_number": raw.get("disc_number"),
        "release_date": album.get("release_date"),
        "isrcs": [isrc] if isrc else [],
    }
    return {key: value for key, value in fields.items() if value}


@dataclass(frozen=True)
class Appearance:
    release: dict
    medium: dict
    track: dict
    index: int | None

    @property
    def track_id(self) -> str:
        return self.track["id"]

    @property
    def release_id(self) -> str:
        return self.release["id"]

    def fields(self) -> dict:
        labels = self.release.get("label-info") or []
        events = self.release.get("release-events") or []
        return {
            "release_mbid": self.release_id,
            "release_group_mbid": (self.release.get("release-group") or {}).get("id"),
            "album": self.release.get("title"),
            "album_artists": self.release.get("artist-credit"),
            "release_date": [
                self.release.get("date"),
                *(event.get("date") for event in events),
            ],
            "country": [
                self.release.get("country"),
                *(
                    code
                    for event in events
                    for code in (event.get("area") or {}).get("iso-3166-1-codes", [])
                ),
            ],
            "format": self.medium.get("format"),
            "label": [(entry.get("label") or {}).get("name") for entry in labels],
            "catalog_number": [entry.get("catalog-number") for entry in labels],
            "barcode": self.release.get("barcode"),
            "disc_number": self.medium.get("position"),
            "track_number": [self.track.get("position"), self.index],
        }


def appearances(release: dict):
    offset = 0
    for medium in sorted(
        release.get("media") or [], key=lambda item: item.get("position") or 0
    ):
        pregap = medium.get("pregap")
        if pregap:
            yield Appearance(release, medium, pregap, None)
        tracks = sorted(
            medium.get("tracks") or [], key=lambda item: item.get("position") or 0
        )
        for i, track in enumerate(tracks, 1):
            yield Appearance(release, medium, track, offset + i)
        offset += medium.get("track-count", len(tracks))
        for track in sorted(
            medium.get("data-tracks") or [], key=lambda item: item.get("position") or 0
        ):
            yield Appearance(release, medium, track, None)


def edition_order(appearance: Appearance) -> tuple:
    release = appearance.release
    date = release.get("date") or ""
    parts = [int(part) for part in date.split("-") if part.isdigit()]
    known_date = (parts + [0, 0, 0])[:3]
    return (
        release.get("status") != "Official",
        not parts,
        *known_date,
        release["id"],
        appearance.medium.get("position") or 0,
        appearance.track.get("position") or 0,
        appearance.track_id,
    )


def text_key(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


VERSION_WORDS = re.compile(
    r"\b(?:live|studio|acoustic|unplugged|remix|instrumental|demo|radio edit|remaster(?:ed)?)\b"
)
VERSION_SUFFIX = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]|\s[-–—]\s(.+)$")
_RECORDING_VERSIONS = {
    "live": r"\blive\b|\bin concert\b",
    "acoustic": r"\b(?:acoustic|unplugged)\b",
    "instrumental": r"\b(?:instrumental|karaoke|backing track)\b",
    "demo": r"\bdemo\b",
    "mix": r"\b(?:remix|rework|dub mix|surround mix|dolby atmos|5 1 mix)\b",
    "radio edit": r"\bradio (?:edit|ver(?:sion)?|mix)\b",
    "single edit": r"\bsingle (?:edit|ver(?:sion)?|mix)\b",
    "album version": r"\balbum (?:edit|ver(?:sion)?|mix)\b",
    "long version": r"\b(?:long|extended) (?:edit|ver(?:sion)?|mix)\b",
    "short version": r"\bshort (?:edit|ver(?:sion)?|mix)\b",
    "alternate": r"\b(?:alternate|alternative|session|sessions) (?:take|mix|version)\b",
}


def title_qualifiers(title: str) -> str:
    return " ".join(
        value
        for match in VERSION_SUFFIX.finditer(title)
        for value in match.groups()
        if value
    )


def recording_versions(
    title: str, disambiguation: str = "", *, distinguish_studio_mix: bool = False
) -> set[str]:
    """Read explicit audio variations. Mastering labels describe the edition."""
    parts = [text_key(title_qualifiers(title)), text_key(disambiguation)]
    text = " ".join(parts)
    versions = {
        kind
        for kind, expression in _RECORDING_VERSIONS.items()
        if re.search(expression, text)
    }
    if distinguish_studio_mix and re.search(r"\bstudio mix\b", text):
        versions.add("mix")
    if not versions and any(
        re.search(r"\bmix\b", part)
        and not re.search(r"\b(?:original|mono|stereo)\b", part)
        for part in parts
    ):
        versions.add("mix")
    return versions


def version_evidence(title: str) -> str:
    parts = []
    for match in VERSION_SUFFIX.finditer(title):
        part = next(value for value in match.groups() if value is not None)
        if VERSION_WORDS.search(part.casefold()):
            parts.append(part)
    return " ".join(parts)


def version_match(requested: str, title: str, disambiguation: str) -> bool | None:
    requested = text_key(requested)
    if not requested:
        return None
    explicit = text_key(f"{title_qualifiers(title)} {disambiguation}")
    if re.search(rf"\b{re.escape(requested)}\b", explicit):
        return True
    requested_types = set(VERSION_WORDS.findall(requested))
    observed_types = set(VERSION_WORDS.findall(explicit))
    if (
        requested_types
        and observed_types
        and requested_types.isdisjoint(observed_types)
    ):
        return False
    if (explicit and not requested_types) or (requested_types & observed_types):
        return False
    return None


EDITION_FIELDS = (
    "release_mbid",
    "release_group_mbid",
    "album",
    "album_artists",
    "release_date",
    "country",
    "format",
    "label",
    "catalog_number",
    "barcode",
    "disc_number",
    "track_number",
)


def edition_match(
    field: str, wanted, observed, *, normalize_album_ampersand: bool = False
) -> bool | None:
    if field == "album_artists":
        if not observed:
            return None
        if isinstance(wanted, str):
            return text_key(wanted) == text_key(display_credit(observed, "joinphrase"))
        wanted_names = [
            text_key(credit if isinstance(credit, str) else credit["name"])
            for credit in wanted
        ]
        return wanted_names == [text_key(credit["name"]) for credit in observed]

    values = [
        value
        for value in (observed if isinstance(observed, list) else [observed])
        if value is not None and value != ""
    ]
    if not values:
        return None
    if isinstance(wanted, str):
        if field == "album" and normalize_album_ampersand:
            return any(
                text_key(wanted.replace("&", " and "))
                == text_key(value.replace("&", " and "))
                for value in values
            )
        if field == "release_date":
            wanted_parts = wanted.split("-")
            unknown = False
            for value in values:
                parts = value.split("-")
                overlap = min(len(wanted_parts), len(parts))
                if parts[:overlap] == wanted_parts[:overlap]:
                    if len(parts) >= len(wanted_parts):
                        return True
                    unknown = True
            return None if unknown else False
        return any(text_key(wanted) == text_key(value) for value in values)
    return wanted in values


def comparison(
    item,
    info: TrackInfo,
    titles: list[str],
    appearance: Appearance | None,
    credits: list[dict] | None,
    track_number: int | None,
) -> Distance | None:
    item = item.copy()
    candidate = info.copy()
    if credits and candidate.artists:
        item.artist = ", ".join(credit["name"] for credit in credits)
        artist_names = [
            candidate.artists,
            candidate.artists_credit or candidate.artists,
        ]
        candidate.artist = min(
            (", ".join(names) for names in artist_names),
            key=lambda name: string_dist(item.artist, name),
        )
    if appearance:
        candidate.medium = appearance.medium.get("position")
        candidate.medium_index = appearance.track.get("position")
        candidate.index = appearance.index or candidate.medium_index
    if not item.length:
        candidate.length = None
    if titles and candidate.title:
        item.title = min(titles, key=lambda title: string_dist(title, candidate.title))
    observed = {
        "track_title": titles and candidate.title,
        "track_artist": candidate.artist and item.artist.lower() not in VA_ARTISTS,
        "track_length": item.length and candidate.length,
        "track_index": track_number is not None and candidate.index is not None,
        "medium": item.disc and candidate.medium,
    }
    scored = track_distance(item, candidate, incl_artist=True)
    distance = Distance()
    # beets omits zero-valued penalties when iterating Distance. Those
    # comparisons still count toward the denominator when both values exist.
    for key, penalties in scored._penalties.items():
        if observed.get(key) and scored._weights[key] > 0:
            for penalty in penalties:
                distance.add(key, penalty)
    if (
        observed["track_index"]
        and "track_index" not in scored._penalties
        and scored._weights["track_index"] > 0
    ):
        # A real pregap has position zero. The stock matcher treats the
        # zero in Item as missing, so only compare it when explicitly supplied.
        distance.add_expr("track_index", track_index_changed(item, candidate))
    return distance if distance.max_distance else None
