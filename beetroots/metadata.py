"""Canonical MusicBrainz metadata and proposed tags."""

from __future__ import annotations

from copy import deepcopy

from .evidence import Appearance, display_credit, edition_order


def present(values: dict) -> dict:
    return {
        key: value for key, value in values.items() if value not in (None, "", [], {})
    }


def artist_ids(entity: dict | None) -> list[str]:
    return [
        credit["artist"]["id"]
        for credit in (entity or {}).get("artist-credit") or []
        if (credit.get("artist") or {}).get("id")
    ]


def collect_metadata(
    run, recording: dict, selected: Appearance | None
) -> tuple[dict, Appearance | None, bool]:
    mbid = recording["id"]
    errors_before = len(run.issues)
    linked = {release["id"]: release for release in recording.get("releases") or []}
    for release in run.pages(
        "metadata.releases", run.mb.pages("release", recording=mbid)
    ):
        linked[release["id"]] = release
    if selected:
        linked.setdefault(selected.release_id, selected.release)

    releases = {}
    found = {}
    for release_id in sorted(linked):
        release = run.entity("release", release_id)
        if release is None:
            # A verified tracklist remains usable if the wider lookup failed.
            release = run.lookup(
                "metadata.release.tracklist", run.mb.release_core, release_id
            )
            if not release and selected and selected.release_id == release_id:
                release = selected.release
        if release:
            releases[release["id"]] = release
            occurrences = list(run.recording_appearances(release, mbid))
            if not occurrences:
                run.issue(
                    f"metadata.release:{release_id}",
                    "appearance_missing",
                    "Release tracklist lacks the chosen recording",
                )
            for appearance in occurrences:
                found[(appearance.release_id, appearance.track_id)] = appearance

    chosen = (
        next(
            (
                appearance
                for appearance in found.values()
                if appearance.track_id == selected.track_id
            ),
            None,
        )
        if selected
        else None
    )
    if selected and not chosen:
        run.issue(
            "metadata.edition",
            "appearance_missing",
            "The verified track occurrence is missing from the release payload",
        )
    if not chosen and found:
        chosen = min(found.values(), key=edition_order)
    inferred = chosen is not None and (
        selected is None or chosen.track_id != selected.track_id
    )

    entities: dict[str, dict[str, dict]] = {"release": releases}
    group_ids = {
        release["release-group"]["id"]
        for release in releases.values()
        if (release.get("release-group") or {}).get("id")
    }
    group_ids.update(group["id"] for group in recording.get("release-groups") or [])
    entities["release-group"] = run.entities("release-group", group_ids)

    # Fetch works and contributors connected to this recording or its
    # appearances. Keep references on those entities without chasing cycles.
    subjects = [recording, *releases.values(), *entities["release-group"].values()]
    for appearance in found.values():
        subjects.extend([appearance.track, appearance.track["recording"]])
    relation_ids: dict[str, set[str]] = {
        kind: set()
        for kind in ("work", "artist", "instrument", "place", "label", "area")
    }

    def add_relationships(subject: dict) -> None:
        for relation in subject.get("relations") or []:
            kind = relation.get("target-type")
            identifier = (relation.get(kind) or {}).get("id")
            if identifier and kind in relation_ids:
                relation_ids[kind].add(identifier)
        relation_ids["artist"].update(artist_ids(subject))

    for subject in subjects:
        add_relationships(subject)
    entities["work"] = run.entities("work", relation_ids.pop("work"))
    for work in entities["work"].values():
        add_relationships(work)

    for release in releases.values():
        for label in release.get("label-info") or []:
            if label_id := (label.get("label") or {}).get("id"):
                relation_ids["label"].add(label_id)
        for event in release.get("release-events") or []:
            if area_id := (event.get("area") or {}).get("id"):
                relation_ids["area"].add(area_id)
    for kind, ids in relation_ids.items():
        if ids:
            entities[kind] = run.entities(kind, ids)

    return (
        {
            "recording": recording,
            "appearances": [
                {
                    "release_mbid": appearance.release_id,
                    "release_track_mbid": appearance.track_id,
                    "recording_mbid": mbid,
                    "disc_number": appearance.medium.get("position"),
                    "track_number": appearance.track.get("position"),
                }
                for appearance in sorted(
                    found.values(),
                    key=lambda a: (
                        a.release_id,
                        a.medium.get("position") or 0,
                        a.track.get("position") or 0,
                        a.track_id,
                    ),
                )
            ],
            "entities": entities,
            "complete": len(run.issues) == errors_before,
        },
        chosen,
        inferred,
    )


def proposed_tags(run, metadata: dict, occurrence: Appearance | None) -> dict:
    recording = metadata["recording"]
    for_beets = deepcopy(recording)
    works = metadata["entities"]["work"]
    for relation in for_beets.get("relations") or []:
        work = relation.get("work")
        if work and work.get("id") in works:
            relation["work"] = works[work["id"]]
    info = run.track_info(run.mb._normalize_data(for_beets))
    values = {
        "title": recording.get("title"),
        "artist": display_credit(recording.get("artist-credit"), "joinphrase"),
        "artist_credits": recording.get("artist-credit"),
        "artist_mbids": artist_ids(recording),
        "artist_sort": info.get("artist_sort"),
        "recording_mbid": recording["id"],
        "duration_ms": recording.get("length"),
        "disambiguation": recording.get("disambiguation"),
        "first_release_date": recording.get("first-release-date"),
        "isrcs": recording.get("isrcs"),
        "genres": [genre["name"] for genre in recording.get("genres") or []],
        "work": info.get("work"),
        "work_mbid": info.get("mb_workid"),
        "composers": info.get("composers"),
        "lyricists": info.get("lyricists"),
        "remixers": info.get("remixers"),
        "arrangers": info.get("arrangers"),
    }
    if occurrence:
        release, medium, track = occurrence.release, occurrence.medium, occurrence.track
        group_id = (release.get("release-group") or {}).get("id")
        group = metadata["entities"]["release-group"].get(
            group_id, release.get("release-group") or {}
        )
        labels = release.get("label-info") or []
        values.update(
            {
                "release_mbid": release["id"],
                "release_group_mbid": group.get("id"),
                "release_track_mbid": track["id"],
                "release_track_title": track.get("title"),
                "release_track_artist_credits": track.get("artist-credit"),
                "album": release.get("title"),
                "album_artist": display_credit(
                    release.get("artist-credit"), "joinphrase"
                ),
                "album_artists": release.get("artist-credit"),
                "album_artist_mbids": artist_ids(release),
                "release_date": release.get("date"),
                "country": release.get("country"),
                "release_status": release.get("status"),
                "release_type": group.get("primary-type"),
                "release_secondary_types": group.get("secondary-types"),
                "release_disambiguation": release.get("disambiguation"),
                "barcode": release.get("barcode"),
                "labels": [
                    (entry.get("label") or {}).get("name")
                    for entry in labels
                    if (entry.get("label") or {}).get("name")
                ],
                "catalog_numbers": [
                    entry["catalog-number"]
                    for entry in labels
                    if entry.get("catalog-number")
                ],
                "disc_number": medium.get("position"),
                "disc_title": medium.get("title"),
                "track_number": track.get("position"),
                "track_number_text": track.get("number"),
                "track_total": medium.get("track-count"),
                "disc_total": len(release.get("media") or []) or None,
                "format": medium.get("format"),
            }
        )
    return present(values)
