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
# Modifier-letter apostrophes are alphanumeric in Unicode. They serve the
# same purpose as straight and curly apostrophes in many catalog credits.
_APOSTROPHES = str.maketrans("", "", "ʻʼʹꞌ")
_OPUS = re.compile(r"\bop\.?\s*\d+", re.IGNORECASE)
_CATALOG_NUMBER = re.compile(
    r",?\s+\b(?:B|D|H|K|KV|RV|BWV|WoO)\.?\s*\d+[a-z]?\b",
    re.IGNORECASE,
)


def key(value: str) -> str:
    value = value.translate(_APOSTROPHES).casefold().replace("&", " and ")
    folded = unicodedata.normalize("NFKD", value)
    return "".join(char for char in folded if char.isalnum())


def base_title(title: str) -> str:
    """Remove trailing credit, soundtrack and version text, keeping work numbers."""
    title = title.strip()
    while match := _BRACKET_SUFFIX.search(title) or _DASH_SUFFIX.search(title):
        qualifier = match[1].strip()
        if not (_CREDIT.match(qualifier) or _VERSION.search(qualifier)):
            break
        title = _TRAILING_DASH.sub("", title[: match.start()].rstrip())
    return title


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
