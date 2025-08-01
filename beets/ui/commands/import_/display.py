from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cached_property, lru_cache
from operator import eq
from typing import TYPE_CHECKING, Any, ClassVar, Generic

from rich import box
from rich.align import Align
from rich_tables.diff import pretty_diff
from rich_tables.generic import flexitable
from rich_tables.utils import border_panel, new_table, wrap

from beets import config, library, ui
from beets.autotag.hooks import AlbumInfo, AlbumMatch, AnyMatch, TrackMatch
from beets.util import cached_classproperty, displayable_path, get_console

if TYPE_CHECKING:
    from collections.abc import Sequence

    from confuse import ConfigView
    from rich.console import RenderableType
    from rich.text import Text

    from beets import importer, library
    from beets.autotag.hooks import AlbumInfo, Info, TrackInfo

    JSONDict = dict[str, Any]


@dataclass
class Diff:
    """Represent and format differences between an existing item and new info.

    This helper aggregates the relevant fields from an existing library item and
    parsed incoming metadata, formats field-specific values for human-readable
    comparison, and exposes computed views used by UI code to render what will
    change during import.
    """

    FORMAT_BY_FIELD = {
        "length": lambda x: (
            datetime.fromtimestamp(x).astimezone(timezone.utc).strftime("%T")
        ),
        "va": lambda x: str(bool(x)),
    }
    item: library.Item
    info: Info

    @cached_classproperty
    def always_include_fields(cls) -> Sequence[str]:
        return []

    @cached_classproperty
    def exclude_fields(cls) -> set[str]:
        return {"data_url"}

    @cached_property
    def info_data(self) -> dict[str, Any]:
        return {
            k: v
            for k, v in self.info.item_data.items()
            if k not in self.exclude_fields
        }

    def get_value_pair(self, f: str) -> tuple[Any, ...]:
        """Produce the (existing, incoming) value pair for a single field.

        Applies special-case handling for particular fields (for example,
        preserving lyric sources or formatting time-like and boolean values)
        so comparisons and pretty-printing behave consistently.
        """
        if f == "lyrics" and "Source: " in (
            old_lyrics := self.item.lyrics or ""
        ):
            return old_lyrics, old_lyrics

        pair = (
            self.item.get(self.info.MEDIA_FIELD_MAP.get(f, f)),
            self.info_data.get(f),
        )
        if fmt_func := self.FORMAT_BY_FIELD.get(f):
            return tuple((fmt_func(i) if i is not None else i) for i in pair)

        return pair

    @property
    def diff_data(self) -> dict[str, tuple[Any, Any]]:
        """Compute the map of fields that differ between item and incoming info.

        The result maps field names to their (existing, incoming) value pairs.
        Only pairs that have non-empty 'after' value are included, except for
        fields that must always be shown.
        """
        always_include = self.always_include_fields
        fields = dict.fromkeys((*always_include, *self.info_data))
        return {
            f: pair
            for f in fields
            if (
                any(pair := self.get_value_pair(f))
                and (not eq(*pair) or f in always_include)
            )
        }

    @property
    def changes(self) -> dict[str, Text | str]:
        return {f: pretty_diff(a, b) for f, (a, b) in self.diff_data.items()}


@dataclass
class TrackDiff(Diff):
    info: TrackInfo

    @cached_classproperty
    def config(cls) -> ConfigView:
        return config["ui"]["import"]["album_track_fields"]

    @cached_classproperty
    def always_include_fields(cls) -> Sequence[str]:
        return cls.config["always"].as_str_seq()

    @cached_classproperty
    def exclude_fields(cls) -> set[str]:
        return {*super().exclude_fields, *cls.config["never"].as_str_seq()}

    @cached_property
    def info_data(self) -> dict[str, Any]:
        return super().info_data


@dataclass
class SingletonDiff(TrackDiff):
    @cached_classproperty
    def config(cls) -> ConfigView:
        return config["ui"]["import"]["singleton_fields"]

    @cached_classproperty
    def exclude_fields(cls) -> set[str]:
        return Diff.exclude_fields


@dataclass
class AlbumDiff(Diff):
    info: AlbumInfo

    @cached_classproperty
    def exclude_fields(cls) -> set[str]:
        return {*super().exclude_fields, "tracks"}


@dataclass
class Change(Generic[AnyMatch]):
    name: ClassVar[str]
    info_border_color: ClassVar[str]
    match: AnyMatch

    def get_info_panel(self, diff: Diff) -> RenderableType:
        table = new_table()

        for field, value in sorted(
            (k, (v if v is not None else "")) for k, v in diff.info_data.items()
        ):
            table.add_row(wrap(field, "b"), str(value))
        return border_panel(
            table, title=self.name, border_style=self.info_border_color
        )

    def show_item_change(self, diff: Diff) -> None:
        """Print out the change that would occur by tagging `item` with the
        metadata from `match` - either an album or a track.
        """
        row = [self.get_info_panel(diff)]
        if changes := sorted(diff.changes.items()):
            table = new_table(highlight=False)
            for field, change in changes:
                table.add_row(wrap(field, "b"), change)
            panel = border_panel(table, title="Updates", border_style="yellow")
            row.insert(0, Align.center(panel, vertical="bottom"))

        get_console().print(new_table(rows=[row]))
        get_console().print(wrap(self.match.info.data_url or "", "b grey35"))

    def show(self) -> None:
        raise NotImplementedError

    def as_str(self):
        with get_console().capture() as capture:
            self.show()

        return capture.get()


@dataclass
class AlbumChange(Change[AlbumMatch]):
    name = "Album"
    info_border_color = "magenta"

    def show(self) -> None:
        """Print out a representation of the changes that will be made if an
        album's tags are changed according to `match`, which must be an AlbumMatch
        object.
        """
        pairs = self.match.item_info_pairs
        self.show_item_change(AlbumDiff(pairs[0][0], self.match.info))

        tracks_table = new_table(
            highlight=False,
            box=box.HORIZONTALS,
            border_style="white",
            row_styles=["dim"],
        )

        for item, info in pairs:
            tracks_table.add_dict_row(TrackDiff(item, info).changes)

        # Missing and unmatched tracks.
        for name, tracks in [
            ("Missing", self.match.extra_tracks),
            ("Unmatched", self.match.extra_items),
        ]:
            if tracks:
                tracks_table.add_row(end_section=True)
                tracks_table.add_row(f"[b]{name}[/]")
                for extra_track in tracks:
                    extra_track = extra_track.copy()
                    if extra_track.length:
                        extra_track.length = Diff.FORMAT_BY_FIELD["length"](
                            extra_track.length
                        )

                    tracks_table.add_dict_row(
                        extra_track, style="b yellow", ignore_extra_fields=True
                    )

        get_console().print(
            border_panel(tracks_table, title=wrap("Tracks", "b i cyan"))
        )


@dataclass
class TrackChange(Change[TrackMatch]):
    name = "Singleton"
    info_border_color = "cyan"

    def show(self) -> None:
        return self.show_item_change(
            SingletonDiff(self.match.item, self.match.info)
        )


@dataclass
class View(Generic[AnyMatch]):
    PRINT_CANDIDATES_TEMPLATE = 'Finding tags for {} "{} - {}".'
    PRINT_PATHS_TEMPLATE = (
        "[import_path]{paths}[/] [import_path_items]({count} items)[/]"
    )
    CHANGE_CLASS: ClassVar[type[Change]]
    task: importer.ImportTask[AnyMatch]

    @property
    def candidates(self) -> Sequence[AnyMatch]:
        return self.task.candidates

    def __hash__(self):
        return hash(tuple(map(hash, self.task.candidates)))

    @lru_cache
    def get_match_display(self, match: AnyMatch) -> str:
        return self.CHANGE_CLASS(match).as_str()

    def show_match(self, idx: int) -> AnyMatch:
        match = self.task.candidates[idx]
        print(self.get_match_display(match))
        return match

    def _announce(self, _type: str, name: str) -> None:
        ui.print_(
            self.PRINT_CANDIDATES_TEMPLATE.format(
                _type, self.task.item.artist, name
            )
        )

    def print_paths(self) -> None:
        ui.print_(
            self.PRINT_PATHS_TEMPLATE.format(
                paths=displayable_path(self.task.paths, "\n"),
                count=len(self.task.items),
            )
        )

    def show(self) -> None:
        raise NotImplementedError

    @lru_cache
    def as_str(self) -> str:
        with get_console().capture() as capture:
            self.show()

        return capture.get()

    def print_candidates(self, *args, **kwargs) -> None:
        self._announce(*args, **kwargs)
        print(self.as_str())

    def print_not_found(self) -> None:
        raise NotImplementedError


class SingletonView(View[TrackMatch]):
    CHANGE_CLASS = TrackChange

    def show(self) -> None:
        candidata = []
        tracks_table = new_table(
            highlight=False, overflow="ellipsis", max_width=20
        )
        for idx, candidate in enumerate(self.candidates, 1):
            i = str(idx)
            candidata.append({"id": i, **candidate.disambig_data})
            old, new = candidate.item, candidate.info
            data = {"id": i, **TrackDiff(old, new).changes}
            tracks_table.add_dict_row(data)

        get_console().print(border_panel(tracks_table, title=""))
        get_console().print(
            border_panel(flexitable(candidata), title="Singleton candidates")
        )
        get_console().print("")

    def print_candidates(self) -> None:
        super().print_candidates("track", self.task.item.title)

    def print_not_found(self) -> None:
        ui.print_("No matching recordings found.")


class AlbumView(View[AlbumMatch]):
    CHANGE_CLASS = AlbumChange

    def show(self) -> None:
        candidata = []
        tracks_table = new_table(
            highlight=False, overflow="ellipsis", max_width=20
        )
        for idx, album_candidate in enumerate(self.candidates, 1):
            i = str(idx)
            candidata.append({"id": i, **album_candidate.disambig_data})
            for old, new in album_candidate.item_info_pairs:
                data = {"id": i, **TrackDiff(old, new).changes}
                tracks_table.add_dict_row(data)
            tracks_table.add_section()

        get_console().print(border_panel(tracks_table, title="Album tracks"))
        get_console().print(
            border_panel(flexitable(candidata), title="Album candidates")
        )
        get_console().print("")

    def print_candidates(self) -> None:
        super().print_candidates("album", self.task.item.album)

    def print_not_found(self) -> None:
        ui.print_(
            f"No matching release found for {len(self.task.paths)} tracks."
        )
        ui.print_(
            "For help, see: "
            "https://beets.readthedocs.org/en/latest/faq.html#nomatch"
        )
