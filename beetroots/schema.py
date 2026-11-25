from dataclasses import dataclass, field
from typing import Literal, NotRequired, TypedDict

from .identity import agrees, names_from_credits
from .identity import key as identity_key

VERSION = "0.1.0"
BEETS_VERSION = "2.14.1"

type JSONValue = (
    None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
)


class ArtistCredit(TypedDict):
    name: str
    join_phrase: NotRequired[str]
    artist_mbid: NotRequired[str]


class TrackInput(TypedDict, total=False):
    """Evidence for a single listen, also accepted as JSON by the CLI."""

    record_id: str | int
    title: str
    alternate_titles: list[str]
    artist: str
    artist_credits: list[ArtistCredit]
    album: str
    album_artists: list[ArtistCredit] | list[str] | str
    version: str
    disambiguation: str
    recording_mbid: str
    release_track_mbid: str
    release_mbid: str
    release_group_mbid: str
    artist_mbid: str
    artist_mbids: list[str]
    work_mbid: str
    isrcs: list[str]
    spotify_track_id: str
    spotify_uri: str
    youtube_video_id: str
    source_urls: list[str]
    duration_ms: int
    disc_number: int
    track_number: int
    release_date: str
    country: str
    format: str
    label: str
    catalog_number: str
    barcode: str
    source: str
    listened_at: str | int | float
    elapsed_ms: int
    context: JSONValue


class MatchIssue(TypedDict):
    path: str
    code: str
    detail: str


type IdentificationMethod = Literal["recording_mbid", "release_track_mbid", "metadata"]
type MatchStatus = Literal[
    "matched",
    "invalid_input",
    "conflicting_ids",
    "unresolved_id",
    "lookup_error",
    "no_candidates",
    "insufficient_evidence",
]


@dataclass(frozen=True)
class MatchResult:
    record_id: str | int | None
    status: MatchStatus
    config_fingerprint: str
    matcher_version: str = VERSION
    method: IdentificationMethod | None = None
    recording_mbid: str | None = None
    confidence: float | None = None
    distance: float | None = None
    edition: dict | None = None
    metadata: dict | None = None
    proposed_tags: dict | None = None
    provenance: dict = field(default_factory=dict)
    issues: list[MatchIssue] = field(default_factory=list)

    def tags_for(
        self,
        track: TrackInput,
        *,
        min_confidence: float,
        require_song_agreement: bool = False,
    ) -> dict:
        """Apply a sufficiently confident proposal over the source's display tags."""
        tags = {
            key: track[key] for key in ("title", "artist", "album") if track.get(key)
        }
        proposal = self.proposed_tags
        if not proposal or self.confidence is None or self.confidence < min_confidence:
            return tags
        if require_song_agreement and self.method == "metadata":
            metadata = self.metadata or {}
            recording = metadata.get("recording") or {}
            titles = [proposal.get("title"), proposal.get("release_track_title")]
            titles.extend(
                alias.get("name") for alias in (recording.get("aliases") or [])
            )
            artists = [
                proposal.get("artist"),
                *names_from_credits(proposal.get("artist_credits", [])),
            ]
            if (
                track.get("source") == "spotify"
                and track.get("album")
                and identity_key(track["album"])
                == identity_key(proposal.get("album", ""))
                and self.edition
                and not self.edition["inferred"]
            ):
                for field in ("release_track_artist_credits", "album_artists"):
                    artists.extend(names_from_credits(proposal.get(field, [])))
            for artist in metadata.get("entities", {}).get("artist", {}).values():
                if artist.get("id") in proposal.get("artist_mbids", []):
                    artists.extend(
                        alias.get("name") for alias in (artist.get("aliases") or [])
                    )
            source_titles = [track.get("title", ""), *track.get("alternate_titles", [])]
            linked = self.recording_mbid and any(
                self.provenance.get(field) == self.recording_mbid
                for field in ("spotify_recording_link", "isrc_recording_link")
            )
            if not (
                agrees(source_titles, track.get("artist", ""), titles, artists)
                or (
                    linked
                    and any(source_titles)
                    and agrees(source_titles, "", titles, [])
                )
            ):
                return tags
        tags.update(proposal)
        return tags


def validate(track: object) -> str | None:
    if not isinstance(track, dict):
        return "Expected a JSON object"
    for key, value in track.items():
        if key not in TrackInput.__annotations__:
            return f"Unknown field {key!r}"
        if value is None:
            continue
        match key:
            case "context":
                valid = True
            case "record_id" | "listened_at":
                types = (str, int) if key == "record_id" else (str, int, float)
                valid = type(value) in types
            case "duration_ms" | "elapsed_ms" | "disc_number" | "track_number":
                valid = type(value) is int and value >= 0
            case "alternate_titles" | "artist_mbids" | "isrcs" | "source_urls":
                valid = isinstance(value, list) and all(
                    isinstance(v, str) for v in value
                )
            case "artist_credits" | "album_artists":
                valid = key == "album_artists" and isinstance(value, str)
                if isinstance(value, list):
                    valid = all(
                        (key == "album_artists" and isinstance(v, str))
                        or (
                            isinstance(v, dict)
                            and isinstance(v.get("name"), str)
                            and all(
                                isinstance(v.get(k, ""), str)
                                for k in ("join_phrase", "artist_mbid")
                            )
                        )
                        for v in value
                    )
            case _:
                valid = isinstance(value, str)
        if not valid:
            return f"Invalid field {key!r}"
    return None
