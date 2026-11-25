from dataclasses import dataclass, field
from typing import Literal, NotRequired, TypedDict

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
