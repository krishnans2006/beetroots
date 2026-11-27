"""Interpret YouTube display metadata as evidence for a recording."""

import re
from dataclasses import dataclass
from typing import Self

from .evidence import (
    display_credit,
    recording_versions,
    title_qualifiers,
    version_match,
)
from .identity import credit_keys, key

CATALOG_AUDIO = "MUSIC_VIDEO_TYPE_ATV"
OFFICIAL_VIDEO = "MUSIC_VIDEO_TYPE_OMV"
PRESENTATION = re.compile(
    r"\s*[(\[]\s*(?:official\s+)?"
    r"(?:music video|video|audio|lyric(?:s| video| visuali[sz]er)|visuali[sz]er)"
    r"\s*[)\]]$",
    re.IGNORECASE,
)
ARTIST_SEPARATOR = re.compile(r"\s+[-–—|]\s+")
MUSIC_VIDEO_LABEL = re.compile(
    r"[(\[]\s*(?:official\s+(?:music\s+)?video|music video)\s*[)\]]",
    re.IGNORECASE,
)
STANDARD_EDITS = frozenset({"album version", "single edit", "radio edit"})


def song_title(value: str, artists: tuple[str, ...]) -> str:
    value = value.strip()
    while match := PRESENTATION.search(value):
        value = value[: match.start()].rstrip()
    parts = ARTIST_SEPARATOR.split(value, maxsplit=1)
    if len(parts) == 2 and any(
        credit_keys(parts[0]) & credit_keys(artist) for artist in artists if artist
    ):
        return parts[1]
    return value


@dataclass(frozen=True)
class VideoEvidence:
    artists: tuple[str, ...]
    preferred_video: bool | None

    @property
    def artist(self) -> str:
        return self.artists[0] if self.artists else ""

    @classmethod
    def apply(cls, evidence: dict, provider: dict, source_tags: dict) -> Self:
        raw = provider.get("response", {})
        fields = provider.get("fields", {})
        video_type = raw.get("videoType")
        official = video_type in {CATALOG_AUDIO, OFFICIAL_VIDEO} or (
            video_type is None and bool(fields.get("album"))
        )
        if official:
            for field, value in fields.items():
                if field not in {"title", "isrcs"} and not evidence.get(field):
                    evidence[field] = value

        uploader = (evidence.get("uploader") or "").strip()
        topic = (
            uploader.removesuffix(" - Topic") if uploader.endswith(" - Topic") else ""
        )
        credits = evidence.get("artist_credits") or []
        artist = evidence.get("artist") or (credits[0]["name"] if credits else "")
        if not artist and topic:
            evidence["artist"] = artist = topic

        original_title = (evidence.get("video_title") or "").strip()
        if original_title.startswith(("http://", "https://")):
            original_title = ""
        provider_title = fields.get("title", "") if official else ""
        artists = (artist, display_credit(credits))
        if not artist and original_title:
            # A publisher can corroborate an explicit artist prefix. A
            # channel name alone is insufficient credit for an arbitrary upload.
            parts = ARTIST_SEPARATOR.split(original_title, maxsplit=1)
            if len(parts) == 2 and key(parts[0]) == key(uploader).removesuffix("vevo"):
                evidence["artist"] = artist = parts[0]
                artists = (artist,)
        cleaned = song_title(original_title, artists)
        title = evidence.get("title") or cleaned or song_title(provider_title, artists)
        if title:
            evidence["title"] = title
            if provider_title:
                evidence["alternate_titles"] = [
                    *evidence.get("alternate_titles", []),
                    song_title(provider_title, artists),
                ]
            source_tags["title"] = title if artist else original_title or title
        display_artist = evidence.get("artist") or display_credit(credits) or uploader
        if display_artist:
            source_tags["artist"] = display_artist
        if album := evidence.get("album"):
            source_tags["album"] = album
        artists = (
            (evidence["artist"],)
            if evidence.get("artist")
            else tuple(credit["name"] for credit in credits)
        )
        preferred_video = None
        if video_type == CATALOG_AUDIO or topic:
            preferred_video = False
        elif MUSIC_VIDEO_LABEL.search(original_title):
            preferred_video = True
        return cls(artists, preferred_video)


def compatible_version(
    title: str,
    version: str | None,
    observed_title: str,
    observed_disambiguation: str,
    edition_title: str = "",
) -> bool:
    wanted = recording_versions(title, version or "", distinguish_studio_mix=True)
    observed = recording_versions(
        observed_title, observed_disambiguation, distinguish_studio_mix=True
    )
    observed.update(recording_versions(edition_title, distinguish_studio_mix=True))
    if wanted - STANDARD_EDITS != observed - STANDARD_EDITS:
        return False
    if "mix" in wanted and (mix := version or title_qualifiers(title)):
        return (
            version_match(mix, observed_title, observed_disambiguation) is True
            or version_match(mix, edition_title, "") is True
        )
    return True


def length_difference(length: float, *observed: float | None) -> float | None:
    return min(
        (abs(length - value) for value in observed if length and value), default=None
    )
