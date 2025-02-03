from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cached_property, lru_cache
from operator import eq
from typing import (
    Any,
    ClassVar,
    Dict,
    Generic,
    Sequence,
    Set,
    Tuple,
    Type,
    Union,
)

from confuse import ConfigView
from rich import box
from rich.align import Align
from rich.console import RenderableType
from rich.text import Text
from rich_tables.generic import flexitable
from rich_tables.utils import border_panel, new_table, pretty_diff, wrap

from beets import config, importer, library
from beets.autotag.hooks import (
    AlbumInfo,
    AlbumMatch,
    Info,
    TrackInfo,
    TrackMatch,
)
from beets.autotag.match import AnyMatch
from beets.ui import print_
from beets.util import cached_classproperty, console, displayable_path


@dataclass
class Diff:
    FORMAT_BY_FIELD = {
        "length": lambda x: datetime.fromtimestamp(x, timezone.utc).strftime(
            "%T"
        ),
        "va": lambda x: str(bool(x)),
    }
    item: library.Item
    info: Info

    @cached_classproperty
    def always_include_fields(cls) -> Sequence[str]:
        return []

    @cached_classproperty
    def exclude_fields(cls) -> Set[str]:
        return {"data_url"}

    @cached_property
    def info_data(self) -> Dict[str, Any]:
        return {
            k: v
            for k, v in self.info.data.items()
            if k not in self.exclude_fields
        }

    def get_value_pair(self, f: str) -> Tuple[Any, ...]:
        pair = (
            self.item.get(self.info.ITEM_FIELD_MAP.get(f, f)),
            self.info_data.get(f),
        )
        if fmt_func := self.FORMAT_BY_FIELD.get(f):
            return tuple((fmt_func(i) if i is not None else i) for i in pair)

        return pair

    @cached_property
    def diff_data(self) -> Dict[str, Tuple[Any, Any]]:
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

    @cached_property
    def changes(self) -> Dict[str, Union[Text, str]]:
        return {
            f: (f"[dim]{a}[/]" if a == b else pretty_diff(a, b))
            for f, (a, b) in self.diff_data.items()
        }


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
    def exclude_fields(cls) -> Set[str]:
        return {*super().exclude_fields, *cls.config["never"].as_str_seq()}

    @cached_property
    def info_data(self) -> Dict[str, Any]:
        data = super().info_data
        if "index" in data:
            data["index"] = data["medium_index"] or data["index"]
        return data


@dataclass
class SingletonDiff(TrackDiff):
    @cached_classproperty
    def always_include_fields(cls) -> Sequence[str]:
        return []

    @cached_classproperty
    def exclude_fields(cls) -> Set[str]:
        return Diff.exclude_fields


@dataclass
class AlbumDiff(Diff):
    info: AlbumInfo

    @cached_classproperty
    def exclude_fields(cls) -> Set[str]:
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

        console.print(new_table(rows=[row]))
        console.print(wrap(self.match.info.data_url, "b grey35"))

    def show(self) -> None:
        raise NotImplementedError

    def as_str(self):
        with console.capture() as capture:
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
        pairs = self.match.mapping
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
                        extra_track.length = datetime.fromtimestamp(
                            extra_track.length
                        ).strftime("%T")

                    tracks_table.add_dict_row(
                        extra_track, style="b yellow", ignore_extra_fields=True
                    )

        console.print(
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
    CHANGE_CLASS: ClassVar[Type[Change]]
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
        print_(
            self.PRINT_CANDIDATES_TEMPLATE.format(
                _type, self.task.item.artist, name
            )
        )

    def print_paths(self) -> None:
        print_(
            self.PRINT_PATHS_TEMPLATE.format(
                paths=displayable_path(self.task.paths, "\n"),
                count=len(self.task.items),
            )
        )

    def show(self) -> None:
        raise NotImplementedError

    @lru_cache
    def as_str(self) -> str:
        with console.capture() as capture:
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
        candidata = [
            {"id": str(i), **m.disambig_data}
            for i, m in enumerate(self.task.candidates, 1)
        ]
        console.print(
            border_panel(flexitable(candidata), title="Singleton candidates")
        )
        console.print("")

    def print_candidates(self) -> None:
        super().print_candidates("track", self.task.item.title)

    def print_not_found(self) -> None:
        print_("No matching recordings found.")


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
            for old, new in album_candidate.mapping:
                data = {"id": i, **TrackDiff(old, new).changes}
                tracks_table.add_dict_row(data)
            tracks_table.add_section()

        console.print(border_panel(tracks_table, title="Album tracks"))
        console.print(
            border_panel(flexitable(candidata), title="Album candidates")
        )
        console.print("")

    def print_candidates(self) -> None:
        super().print_candidates("album", self.task.item.album)

    def print_not_found(self) -> None:
        print_(f"No matching release found for {len(self.task.paths)} tracks.")
        print_(
            "For help, see: "
            "https://beets.readthedocs.org/en/latest/faq.html#nomatch"
        )
