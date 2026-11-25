"""Conservative title and credit comparisons for recording identity."""

import re
import unicodedata
from collections.abc import Iterable

_TITLE_SUFFIX = re.compile(
    r"\s*[([]([^()\[\]]+)[)\]]$|\s+[-–—]\s+((?:(?!\s+[-–—]\s+).)+)$"
)
_VERSION = re.compile(
    r"\b(?:live|remix\w*|remaster\w*|acoustic|instrumental|demo|radio edit|"
    r"soundtrack version|album version|single version|mono|stereo|unplugged|"
    r"japanese version|english version)\b",
    re.IGNORECASE,
)
_CREDIT = re.compile(r"\b(?:feat\.?|ft\.?|featuring|with|from)\s+", re.IGNORECASE)


def key(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.casefold().replace("&", " and "))
    return "".join(char for char in folded if char.isalnum())


def base_title(title: str) -> str:
    """Remove trailing credit, soundtrack and version text, keeping work numbers."""
    title = title.strip()
    while match := _TITLE_SUFFIX.search(title):
        qualifier = next(value for value in match.groups() if value is not None)
        if not (_CREDIT.match(qualifier) or _VERSION.search(qualifier)):
            break
        title = title[: match.start()].rstrip()
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


def agrees(
    source_titles: Iterable[str],
    source_artist: str,
    titles: Iterable[str],
    names: Iterable[str],
) -> bool:
    """Check each observed source field against the recording or a known alias."""
    names = [name for name in names if name]
    requested_titles = set().union(
        *(title_keys(title, [source_artist]) for title in source_titles if title)
    )
    requested_artist = credit_keys(source_artist)
    if not (requested_titles or requested_artist):
        return False
    return (
        not requested_titles
        or any(requested_titles & title_keys(title, names) for title in titles if title)
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
