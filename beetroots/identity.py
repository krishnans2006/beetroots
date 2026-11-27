"""Conservative title and credit comparisons for recording identity."""

import re
import unicodedata
from collections.abc import Iterable

_BRACKET_SUFFIX = re.compile(r"\s*[([]([^()\[\]]+)[)\]]$")
_DASH_SUFFIX = re.compile(r"\s+[-–—]\s+((?:(?!\s+[-–—]\s+).)+)$")
_TRAILING_DASH = re.compile(r"\s+[-–—]$")
_VERSION = re.compile(
    r"^(?:live(?:\s+(?:(?:at|from|in|on|version)\b|\d{4}\b).*)?|"
    r"live\s+studio\s+ver(?:sion)?\b.*|live\s+.+\btour\s+\d{4}|"
    r"acoustic(?: version)?|instrumental(?: version)?|"
    r"demo(?: version)?|mono|stereo|unplugged|bonus track|theme song|"
    r"(?:theme from|recorded at)\s+.+)$"
    r"|\b(?:remix|remaster(?:ed)?|mix|edit|version)\s*(?:\d{4})?$",
    re.IGNORECASE,
)
_CREDIT = re.compile(r"\b(?:feat\.?|ft\.?|featuring|with|from)\s+", re.IGNORECASE)
_FEATURE_SUFFIX = re.compile(r"\s+(?:feat\.|ft\.|featuring)\s+.+$", re.IGNORECASE)
_FEATURE = re.compile(r"(?:feat\.?|ft\.?|featuring)\s+(.+)", re.IGNORECASE)
_FEATURE_JOIN = re.compile(r"\s*(?:,|&|\band\b)\s*", re.IGNORECASE)
_AFFILIATION = re.compile(r"\s+of\s+", re.IGNORECASE)
_MASTERING = r"(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?"
_MINOR_EDIT = re.compile(
    r"(?:(?:album|single|radio)\s+(?:edit|version|ver\.?)|original version|early fade)",
    re.IGNORECASE,
)
_ALBUM_EDITION = re.compile(
    rf"(?:{_MASTERING}|(?:deluxe|expanded|(?:\d+(?:st|nd|rd|th)\s+)?anniversary)"
    rf"(?:\s+re-?mastered)?)(?:\s+edition)?",
    re.IGNORECASE,
)
# Modifier-letter apostrophes are alphanumeric in Unicode. They serve the
# same purpose as straight and curly apostrophes in many catalog credits.
_APOSTROPHES = str.maketrans("", "", "ʻʼʹꞌ")
_OPUS = re.compile(r"\bop\.?\s*\d+", re.IGNORECASE)
_CATALOG_NUMBER = re.compile(
    r",?\s+\b(?:B|D|H|K|KV|RV|BWV|WoO)\.?\s*\d+[a-z]?\b",
    re.IGNORECASE,
)
_NUMBERED_WORK = re.compile(
    r"\b(?:symphony|concerto|sonata|quartet|suite)\s+(?:no\.?\s*)?\d+\b",
    re.IGNORECASE,
)


def key(value: str) -> str:
    value = value.translate(_APOSTROPHES).replace("&", " and ")
    folded = unicodedata.normalize("NFKD", value).casefold()
    return "".join(char for char in folded if char.isalnum())


def base_title(title: str) -> str:
    """Remove trailing credit, soundtrack and version text, keeping work numbers."""
    title = title.strip()
    while match := _BRACKET_SUFFIX.search(title) or _DASH_SUFFIX.search(title):
        qualifier = match[1].strip()
        if not (_CREDIT.match(qualifier) or _VERSION.search(qualifier)):
            break
        title = _TRAILING_DASH.sub("", title[: match.start()].rstrip())
    return _FEATURE_SUFFIX.sub("", title)


def album_key(title: str) -> str:
    title = title.strip()
    while match := _BRACKET_SUFFIX.search(title) or _DASH_SUFFIX.search(title):
        if not _ALBUM_EDITION.fullmatch(match[1].strip()):
            break
        title = title[: match.start()].rstrip()
    # Some album titles consist solely of symbols.
    return key(title) or title.casefold()


def featured_artist(title: str) -> str:
    if suffix := _BRACKET_SUFFIX.search(title) or _DASH_SUFFIX.search(title):
        return match[1].strip() if (match := _FEATURE.fullmatch(suffix[1])) else ""
    if (suffix := _FEATURE_SUFFIX.search(title)) and (
        match := _FEATURE.fullmatch(suffix[0].strip())
    ):
        return match[1].strip()
    return ""


def feature_is_credited(feature: str, names: set[str]) -> bool:
    def credited(value: str) -> bool:
        # The source may append a credited performer's group as an affiliation.
        return (
            key(value) in names
            or key(_AFFILIATION.split(value, maxsplit=1)[0]) in names
        )

    return key(feature) in names or all(
        credited(name) for name in _FEATURE_JOIN.split(feature)
    )


def mastering_label(text: str) -> bool:
    return bool(re.fullmatch(_MASTERING, text.strip(), re.IGNORECASE))


def minor_edit_label(text: str) -> bool:
    return bool(_MINOR_EDIT.fullmatch(text.strip()))


def recording_title(title: str, *, minor_edits: bool = False) -> str:
    """Match catalog titles while retaining audible version qualifiers."""
    title = title.strip()
    while match := _BRACKET_SUFFIX.search(title) or _DASH_SUFFIX.search(title):
        suffix = match[1].strip()
        if not (
            mastering_label(suffix)
            or _FEATURE.fullmatch(suffix)
            or (minor_edits and minor_edit_label(suffix))
        ):
            break
        title = title[: match.start()].rstrip()
    return _FEATURE_SUFFIX.sub("", title)


def title_keys(title: str, artists: Iterable[str] = ()) -> set[str]:
    base = base_title(title)
    keys = {key(title), key(base)}
    prefix, separator, work = base.partition(": ")
    if separator and any(
        key(prefix) in (key(artist), key(artist.rsplit(maxsplit=1)[-1]))
        for artist in artists
        if artist.strip()
    ):
        keys.add(key(work))
    return keys - {""}


def credit_keys(name: str) -> set[str]:
    main = re.split(
        r"\s+(?:feat\.?|ft\.?|featuring|with)\s+|\s+[-–—]\s+",
        name,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    bare = re.sub(r"^the\s+", "", main, flags=re.IGNORECASE)
    return {key(name), key(main), key(bare)} - {""}


def same_title(left: str, right: str, source_artist: str, names: list[str]) -> bool:
    if title_keys(left, [source_artist]) & title_keys(right, names):
        return True
    # Some catalogs give both an opus and a second catalog number, while
    # others give only the opus. Keep the work and movement text intact.
    left, right = base_title(left), base_title(right)
    if not (_OPUS.search(left) and _OPUS.search(right)):
        return False
    one_catalog = bool(_CATALOG_NUMBER.search(left)) != bool(
        _CATALOG_NUMBER.search(right)
    )
    return one_catalog and bool(
        title_keys(_CATALOG_NUMBER.sub("", left), [source_artist])
        & title_keys(_CATALOG_NUMBER.sub("", right), names)
    )


def numbered_work(title: str) -> bool:
    return bool(
        _OPUS.search(title)
        or _CATALOG_NUMBER.search(title)
        or _NUMBERED_WORK.search(title)
    )


def agrees(
    source_titles: Iterable[str],
    source_artist: str,
    titles: Iterable[str],
    names: Iterable[str],
) -> bool:
    """Check each observed source field against the recording or a known alias."""
    names = [name for name in names if name]
    requested_titles = [
        title for title in source_titles if title_keys(title, [source_artist])
    ]
    requested_artist = credit_keys(source_artist)
    if not (requested_titles or requested_artist):
        return False
    return (
        not requested_titles
        or any(
            same_title(requested, title, source_artist, names)
            for title in titles
            if title
            for requested in requested_titles
        )
    ) and (
        not requested_artist
        or any(requested_artist & credit_keys(name) for name in names)
    )


def names_from_credits(credits: list[dict]) -> list[str]:
    return [
        name
        for credit in credits
        for name in (credit.get("name"), (credit.get("artist") or {}).get("name"))
        if name
    ]


def appearance_names(track: dict, release: dict) -> list[str]:
    """Credits that explain a source album artist on this release."""
    return [
        name
        for entity in (track, release)
        for name in names_from_credits(entity.get("artist-credit") or [])
    ]
