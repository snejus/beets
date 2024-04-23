from __future__ import annotations

from math import floor
from time import localtime, strftime
from typing import TYPE_CHECKING, Any

from rich import box
from rich.align import Align
from rich_tables.diff import pretty_diff as diff
from rich_tables.fields import FIELDS_MAP
from rich_tables.generic import flexitable
from rich_tables.utils import border_panel, new_table, wrap

from beets import config
from beets.autotag.hooks import AlbumInfo
from beets.util import get_console

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rich.console import Console, RenderableType

    from beets.autotag.hooks import AlbumMatch, Info, TrackMatch
    from beets.library.models import Item

    JSONDict = dict[str, Any]


def get_diff(field_name: str, before: Any, after: Any) -> str:
    if field_name == "length":
        before = FIELDS_MAP["length"](int(float(before or 0)))
        after = FIELDS_MAP["length"](int(float(after or 0)))

    return diff(str(before or ""), str(after or ""))


track_info_to_item_field: dict[str, str] = {
    "artist_id": "mb_artistid",
    "track_id": "mb_trackid",
    "va": "comp",
    "index": "track",
    "mediums": "disctotal",
    "medium_total": "tracktotal",
    "medium_index": "track",
    "medium": "disc",
    "releasegroup_id": "mb_releasegroupid",
    "release_track_id": "mb_releasetrackid",
}
album_info_to_item_field = {
    **track_info_to_item_field,
    "artist": "albumartist",
    "album_id": "mb_albumid",
    "artist_sort": "albumartist_sort",
    "artist_credit": "albumartist_credit",
}
track_overwrite_fields = set(config["overwrite_null"]["track"].as_str_seq())
album_overwrite_fields = set(config["overwrite_null"]["album"].as_str_seq())

track_fields = config["match"]["singleton_disambig_fields"].as_str_seq()


def get_track_diff(old: JSONDict, new: JSONDict) -> list[str]:
    return [get_diff(f, old.get(f), new.get(f)) for f in track_fields]


def show_album_change(
    cur_artist: str, cur_album: str, match: AlbumMatch
) -> None:
    """Print out a representation of the changes that will be made if an
    album's tags are changed according to `match`, which must be an AlbumMatch
    object.
    """
    pairs = match.merged_pairs
    new = match.info.copy()
    old = pairs[0][0].copy()
    old["album"] = cur_album

    show_item_change(old, new, {"tracks", "data_url"})

    fields = track_fields
    tracks_table = new_table(
        *fields,
        highlight=False,
        box=box.HORIZONTALS,
        border_style="white",
    )
    for item, track_info in pairs:
        tracks_table.add_row(*get_track_diff(dict(item), track_info))
        tracks_table.rows[-1].style = "dim"

    # Missing and unmatched tracks.
    for n, tracks in [
        ("Missing", match.extra_tracks),
        ("Unmatched", match.extra_items),
    ]:
        if tracks:
            tracks_table.add_row(end_section=True)
            tracks_table.add_row(f"[b]{n}[/]")
            for track in tracks:
                track = track.copy()
                if track.length:
                    track.length = strftime(
                        "%M:%S", localtime(floor(float(track.length)))
                    )

                values = map(str, (track.get(f) or "" for f in fields))
                tracks_table.add_row(*values, style="b yellow")

    title = wrap("Tracks", "b i cyan")
    get_console().print(border_panel(tracks_table, title=title))


def show_item_change(old: Item, new: Info, skip: set[str] = set()) -> None:
    """Print out the change that would occur by tagging `item` with the
    metadata from `match` - either an album or a track.
    """
    new_meta = new_table()  # for all new metadata
    upd_meta = new_table()  # for changes only

    if isinstance(new, AlbumInfo):
        info_to_item_field = album_info_to_item_field
        overwrite_fields = album_overwrite_fields
    else:
        info_to_item_field = track_info_to_item_field
        overwrite_fields = track_overwrite_fields

    fields = sorted(new.keys() - skip)
    saved_fields = new.keys()
    for field in fields:
        old_value = old.get(info_to_item_field.get(field, field), "")
        new_value = new.get(field)
        if field == "va":
            old_value = bool(old_value)
        elif field == "releasegroup_id" and old_value == "0":
            old_value = ""
        if field in saved_fields or (
            new_value is not None or field in overwrite_fields
        ):
            new_meta.add_row(wrap(field, "b"), str(new_value))
            if str(old_value or "") != str(new_value or ""):
                diff = get_diff(field, old_value, new_value)
                upd_meta.add_row(wrap(field, "b"), diff)

    if "tracklist" in old:
        old["comments"] += "\n\nTracklist\n\n" + old["tracklist"]

    console = get_console()
    if upd_meta.row_count:
        _type = "Album" if isinstance(new, AlbumInfo) else "Singleton"
        color = "magenta" if _type == "Album" else "cyan"
        updates_panel = border_panel(
            upd_meta, title="Updates", border_style="yellow"
        )
        info_panel = border_panel(new_meta, title=_type, border_style=color)
        row: list[RenderableType] = [
            Align.center(updates_panel, vertical="bottom"),
            info_panel,
        ]
        console.print(new_table(rows=[row]))

    console.print(wrap(new.data_url or "", "b grey35"))


def print_singleton_candidates(
    console: Console, candidates: Sequence[TrackMatch]
) -> None:
    candidata = [
        {"id": str(i), **m.disambig_data} for i, m in enumerate(candidates, 1)
    ]
    console.print(
        border_panel(flexitable(candidata), title="Singleton candidates")
    )
    console.print("")


def print_album_candidates(
    console: Console, candidates: Sequence[AlbumMatch]
) -> None:
    candidata = []
    track_diffs_table = new_table("id", *track_fields)
    for idx, candidate in enumerate(candidates, 1):
        i = str(idx)
        candidata.append({"id": i, **candidate.disambig_data})
        for old, new in candidate.merged_pairs:
            track_diffs_table.add_row(i, *get_track_diff(dict(old), new))
        track_diffs_table.add_row("")

    console.print(border_panel(track_diffs_table, title="Album tracks"))
    console.print(border_panel(flexitable(candidata), title="Album candidates"))
    console.print("")
