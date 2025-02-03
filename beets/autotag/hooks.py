# This file is part of beets.
# Copyright 2016, Adrian Sampson.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

"""Glue between metadata sources and the matching logic."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from typing_extensions import Self

from beets import config
from beets.util import cached_classproperty, colorize, unique_list

if TYPE_CHECKING:
    from collections.abc import Sequence

    from beets.library import Album, Item

    from .distance import Distance

    JSONDict = dict[str, Any]


V = TypeVar("V")


def correct_list_fields(data: JSONDict) -> JSONDict:
    """Synchronise single and list values for the list fields that we use.

    That is, ensure the same value in the single field and the first element
    in the list.

    For context, the value we set as, say, ``mb_artistid`` is simply ignored:
    Under the current :class:`MediaFile` implementation, fields ``albumtype``,
    ``mb_artistid`` and ``mb_albumartistid`` are mapped to the first element of
    ``albumtypes``, ``mb_artistids`` and ``mb_albumartistids`` respectively.

    This means setting ``mb_artistid`` has no effect. However, beets
    functionality still assumes that ``mb_artistid`` is independent and stores
    its value in the database. If ``mb_artistid`` != ``mb_artistids[0]``,
    ``beet write`` command thinks that ``mb_artistid`` is modified and tries to
    update the field in the file. Of course nothing happens, so the same diff
    is shown every time the command is run.

    We can avoid this issue by ensuring that ``artist_id`` has the same value
    as ``artists_ids[0]``, and that's what this function does.
    """

    def ensure_first_value(single_field: str, list_field: str) -> None:
        """Ensure the first ``list_field`` item is equal to ``single_field``."""
        single_val, list_val = data.get(single_field), data.get(list_field, [])
        if single_val:
            data[list_field] = unique_list([single_val, *list_val])
        elif list_val:
            data[single_field] = list_val[0]

    ensure_first_value("albumtype", "albumtypes")
    ensure_first_value("artist_id", "artists_ids")

    return data


# Classes used to represent candidate options.
class AttrDict(dict[str, V]):
    """Mapping enabling attribute-style access to stored metadata values."""

    def copy(self) -> Self:
        return deepcopy(self)

    def __getattr__(self, attr: str) -> V:
        if attr in self:
            return self[attr]

        raise AttributeError(
            f"'{self.__class__.__name__}' object has no attribute '{attr}'"
        )

    def __setattr__(self, key: str, value: V) -> None:
        self.__setitem__(key, value)

    def __hash__(self) -> int:  # type: ignore[override]
        return id(self)


class Info(AttrDict[Any]):
    """Container for metadata about a musical entity."""

    IGNORED_ITEM_FIELDS: ClassVar[set[str]] = {"data_url"}
    ITEM_FIELD_MAP: ClassVar[dict[str, str]] = {}

    @cached_classproperty
    def nullable_fields(cls) -> set[str]:
        name = cls.__name__.lower().removesuffix("info")
        return set(config["overwrite_null"][name].as_str_seq())

    @property
    def name(self) -> str | None:
        raise NotImplementedError

    @property
    def data(self) -> JSONDict:
        data = {
            k: v
            for k, v in self.items()
            if (v is not None or k in self.nullable_fields)
        }
        if config["artist_credit"]:
            data.update(
                artist=self.artist_credit or self.artist,
                artists=self.artists_credit or self.artists,
            )

        return correct_list_fields(data)

    @property
    def item_data(self) -> JSONDict:
        """Return data where fields are mapped for the item."""
        return {
            self.ITEM_FIELD_MAP.get(k, k): v
            for k, v in self.data.items()
            if k not in self.IGNORED_ITEM_FIELDS
        }

    def __init__(
        self,
        album: str | None = None,
        artist_credit: str | None = None,
        artist_id: str | None = None,
        artist: str | None = None,
        artists_credit: list[str] | None = None,
        artists_ids: list[str] | None = None,
        artists: list[str] | None = None,
        artist_sort: str | None = None,
        artists_sort: list[str] | None = None,
        data_source: str | None = None,
        data_url: str | None = None,
        genre: str | None = None,
        media: str | None = None,
        **kwargs,
    ) -> None:
        self.album = album
        self.artist = artist
        self.artist_credit = artist_credit
        self.artist_id = artist_id
        self.artists = artists or []
        self.artists_credit = artists_credit or []
        self.artists_ids = artists_ids or []
        self.artist_sort = artist_sort
        self.artists_sort = artists_sort or []
        self.data_source = data_source
        self.data_url = data_url
        self.genre = genre
        self.media = media
        self.update(kwargs)


class AlbumInfo(Info):
    """Metadata snapshot representing a single album candidate.

    Aggregates track entries and album-wide context gathered from an external
    provider. Used during matching to evaluate similarity against a group of
    user items, and later to drive tagging decisions once selected.
    """

    IGNORED_ITEM_FIELDS = {*Info.IGNORED_ITEM_FIELDS, "tracks"}
    ITEM_FIELD_MAP = {
        **Info.ITEM_FIELD_MAP,
        "album_id": "mb_albumid",
        "artist": "albumartist",
        "artists": "albumartists",
        "artist_id": "mb_albumartistid",
        "artists_ids": "mb_albumartistids",
        "artist_credit": "albumartist_credit",
        "artists_credit": "albumartists_credit",
        "artist_sort": "albumartist_sort",
        "artists_sort": "albumartists_sort",
        "mediums": "disctotal",
        "releasegroup_id": "mb_releasegroupid",
        "va": "comp",
    }

    # TYPING: are all of these correct? I've assumed optional strings
    def __init__(
        self,
        tracks: list[TrackInfo],
        *,
        album_id: str | None = None,
        albumdisambig: str | None = None,
        albumstatus: str | None = None,
        albumtype: str | None = None,
        albumtypes: list[str] | None = None,
        asin: str | None = None,
        barcode: str | None = None,
        catalognum: str | None = None,
        country: str | None = None,
        day: int | None = None,
        discogs_albumid: str | None = None,
        discogs_artistid: str | None = None,
        discogs_labelid: str | None = None,
        label: str | None = None,
        language: str | None = None,
        mediums: int | None = None,
        month: int | None = None,
        original_day: int | None = None,
        original_month: int | None = None,
        original_year: int | None = None,
        release_group_title: str | None = None,
        releasegroup_id: str | None = None,
        releasegroupdisambig: str | None = None,
        script: str | None = None,
        style: str | None = None,
        va: bool = False,
        year: int | None = None,
        **kwargs,
    ) -> None:
        self.tracks = tracks
        self.album_id = album_id
        self.albumdisambig = albumdisambig
        self.albumstatus = albumstatus
        self.albumtype = albumtype
        self.albumtypes = albumtypes or []
        self.asin = asin
        self.barcode = barcode
        self.catalognum = catalognum
        self.country = country
        self.day = day
        self.discogs_albumid = discogs_albumid
        self.discogs_artistid = discogs_artistid
        self.discogs_labelid = discogs_labelid
        self.label = label
        self.language = language
        self.mediums = mediums
        self.month = month
        self.original_day = original_day
        self.original_month = original_month
        self.original_year = original_year
        self.release_group_title = release_group_title
        self.releasegroup_id = releasegroup_id
        self.releasegroupdisambig = releasegroupdisambig
        self.script = script
        self.style = style
        self.va = va
        self.year = year
        super().__init__(**kwargs)


class TrackInfo(Info):
    """Metadata snapshot for a single track candidate.

    Captures identifying details and creative credits used to compare against
    a user's item. Instances often originate within an AlbumInfo but may also
    stand alone for singleton matching.
    """

    IGNORED_ITEM_FIELDS = {*Info.IGNORED_ITEM_FIELDS, "length"}
    ITEM_FIELD_MAP = {
        **Info.ITEM_FIELD_MAP,
        "artist_id": "mb_artistid",
        "artists_ids": "mb_artistids",
        "index": "track",
        "medium_index": "track",
        "medium": "disc",
        "medium_total": "tracktotal",
        "release_track_id": "mb_releasetrackid",
        "track_id": "mb_trackid",
    }

    @property
    def data(self) -> JSONDict:
        data = super().data
        if not config["per_disc_numbering"]:
            data.update(track=self.index, tracktotal=None)

        return data

    @property
    def item_data(self) -> JSONDict:
        return super().item_data | {
            "mb_releasetrackid": self.release_track_id or self.track_id,
        }

    # TYPING: are all of these correct? I've assumed optional strings
    def __init__(
        self,
        *,
        arranger: str | None = None,
        bpm: str | None = None,
        composer: str | None = None,
        composer_sort: str | None = None,
        disctitle: str | None = None,
        index: int | None = None,
        initial_key: str | None = None,
        length: float | None = None,
        lyricist: str | None = None,
        mb_workid: str | None = None,
        medium: int | None = None,
        medium_index: int | None = None,
        medium_total: int | None = None,
        release_track_id: str | None = None,
        title: str | None = None,
        track_alt: str | None = None,
        track_id: str | None = None,
        work: str | None = None,
        work_disambig: str | None = None,
        **kwargs,
    ) -> None:
        self.arranger = arranger
        self.bpm = bpm
        self.composer = composer
        self.composer_sort = composer_sort
        self.disctitle = disctitle
        self.index = index
        self.initial_key = initial_key
        self.length = length
        self.lyricist = lyricist
        self.mb_workid = mb_workid
        self.medium = medium
        self.medium_index = medium_index
        self.medium_total = medium_total
        self.release_track_id = release_track_id
        self.title = title
        self.track_alt = track_alt
        self.track_id = track_id
        self.work = work
        self.work_disambig = work_disambig
        super().__init__(**kwargs)


# Structures that compose all the information for a candidate match.
@dataclass
class Match:
    disambig_fields_key: ClassVar[str]
    distance: Distance
    info: Info

    def apply_metadata(self) -> None:
        raise NotImplementedError

    @cached_classproperty
    def disambig_fields(cls) -> Sequence[str]:
        return config["match"][cls.disambig_fields_key].as_str_seq()

    @cached_classproperty
    def type(cls) -> str:
        return cls.__name__.lower().removesuffix("match")  # type: ignore[attr-defined]

    @property
    def dist(self) -> str:
        if self.distance <= config["match"]["strong_rec_thresh"].as_number():
            color = "text_success"
        elif self.distance <= config["match"]["medium_rec_thresh"].as_number():
            color = "text_warning"
        else:
            color = "text_error"
        return colorize(color, "%.1f%%" % ((1 - self.distance) * 100))

    @property
    def name(self) -> str:
        raise NotImplementedError

    @cached_property
    def penalty(self) -> str | None:
        """Returns a colorized string that indicates all the penalties
        applied to a distance object.
        """
        if penalties := self.distance.penalties:
            return colorize("text_warning", f"({', '.join(penalties)})")
        return None

    @property
    def dist_data(self) -> JSONDict:
        return {
            "name": self.name,
            "distance": self.dist,
            "penalty": self.penalty,
            "dist_count": round(float(1 - self.distance), 2),
            "dist": round(float(1 - self.distance), 2),
        }

    @property
    def disambig_data(self) -> JSONDict:
        """Return data for an AlbumInfo or TrackInfo object that
        provides context that helps disambiguate similar-looking albums and
        tracks.
        """
        dist_fields = self.dist_data.keys()
        match_fields = [k for k in self.disambig_fields if k not in dist_fields]
        data = {
            **self.dist_data,
            **{k: self.info.get(k, None) for k in match_fields},
        }

        return {k: v for k, v in data.items() if k in self.disambig_fields}


@dataclass
class AlbumMatch(Match):
    disambig_fields_key = "album_disambig_fields"
    info: AlbumInfo
    mapping: list[tuple[Item, TrackInfo]]
    extra_items: list[Item] = field(default_factory=list)
    extra_tracks: list[TrackInfo] = field(default_factory=list)

    def __hash__(self) -> int:
        return hash((id(self.items[0]), self.info.album_id))

    @property
    def name(self) -> str:
        return self.info.album or ""

    @cached_property
    def items(self) -> list[Item]:
        return [i for i, _ in self.mapping]

    @property
    def data_pairs(self) -> list[tuple[Item, JSONDict]]:
        album_data = self.info.item_data | {"tracktotal": len(self.info.tracks)}
        return [
            (i, album_data | {k: v for k, v in ti.item_data.items() if v})
            for i, ti in self.mapping
        ]

    def apply_metadata(self) -> None:
        """Apply metadata to each of the items."""
        for item, data in self.data_pairs:
            item.update(data)

    def apply_album_metadata(self, album: Album) -> None:
        """Apply metadata to each of the items."""
        album.update(self.info.item_data)


@dataclass
class TrackMatch(Match):
    disambig_fields_key = "singleton_disambig_fields"
    info: TrackInfo
    item: Item

    def __hash__(self) -> int:
        return hash((id(self.item), *map(str, self.info.item_data.items())))

    @property
    def name(self) -> str:
        return self.info.title or ""

    def apply_metadata(self) -> None:
        """Apply metadata to the item."""
        self.item.update(self.info.item_data)


AnyMatch = TypeVar("AnyMatch", bound=Match)
