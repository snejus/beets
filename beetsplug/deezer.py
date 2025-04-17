# This file is part of beets.
# Copyright 2019, Rahul Ahuja.
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

"""Adds Deezer release and track search support to the autotagger"""

from __future__ import annotations

import collections
import time
from typing import TYPE_CHECKING, Literal, TypedDict

import requests
from typing_extensions import NotRequired

from beets import ui
from beets.autotag.hooks import AlbumInfo, TrackInfo
from beets.dbcore import types
from beets.metadata_plugins import (
    IDResponse,
    SearchApiMetadataSourcePlugin,
    SearchFilter,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from beets.library import Item, Library

    from ._typing import JSONDict


class Artist(TypedDict):
    """Artist object returned by the Deezer API."""

    id: int
    name: str
    link: str


class Track(IDResponse):
    title: str
    artist: Artist
    track_position: int
    disk_number: int
    duration: int
    link: str
    contributors: NotRequired[list[Artist]]


class DeezerPlugin(SearchApiMetadataSourcePlugin[Track]):
    item_types = {
        "deezer_track_rank": types.INTEGER,
        "deezer_track_id": types.INTEGER,
        "deezer_updated": types.DATE,
    }
    # Base URLs for the Deezer API
    # Documentation: https://developers.deezer.com/api/
    search_url = "https://api.deezer.com/search/"
    album_url = "https://api.deezer.com/album/"
    track_url = "https://api.deezer.com/track/"

    def __init__(self) -> None:
        super().__init__()

    def commands(self):
        """Add beet UI commands to interact with Deezer."""
        deezer_update_cmd = ui.Subcommand(
            "deezerupdate", help=f"Update {self.data_source} rank"
        )

        def func(lib: Library, opts, args):
            items = lib.items(args)
            self.deezerupdate(list(items), ui.should_write())

        deezer_update_cmd.func = func

        return [deezer_update_cmd]

    def album_for_id(self, album_id: str) -> AlbumInfo | None:
        """Fetch an album by its Deezer ID or URL."""
        if not (deezer_id := self._extract_id(album_id)):
            return None

        album_url = f"{self.album_url}{deezer_id}"
        if not (album_data := self.fetch_data(album_url)):
            return None

        contributors = album_data.get("contributors")
        if contributors is not None:
            artist, artist_id = self.get_artist(contributors)
        else:
            artist, artist_id = None, None

        album_url = f"{self.album_url}{deezer_id}"
        album_data = requests.get(album_url, timeout=10).json()

        tracks_data = requests.get(f"{album_url}/tracks", timeout=10).json()
        tracks_total = tracks_data.get("total")
        tracks_data = tracks_data.get("data")
        released = {}
        if release_date := album_data.get("release_date"):
            released = dict(
                zip(("year", "month", "day"), map(int, release_date.split("-")))
            )
        if not tracks_data or not released:
            return None

        album = album_data["title"]
        albumtype = album_data["record_type"]

        va = False
        if " VA" in album:
            artist = "Various Artists"
            albumtype = "compilation"
            va = True
        else:
            artist = self.get_artist([album_data["artist"]])[0]
        genres = album_data["genres"]["data"]
        style = ", ".join((g.get("name") or "" for g in genres))
        style = style.replace("Electro", "electronic")
        tracks = []
        medium_totals: dict[int | None, int] = collections.defaultdict(int)
        for i, track_data in enumerate(tracks_data, start=1):
            track = self._get_track(track_data)
            track.medium_total = tracks_total
            track.index = i
            medium_totals[track.medium] += 1
            tracks.append(track)
        for track in tracks:
            track.medium_total = medium_totals[track.medium]

        return AlbumInfo(
            tracks,
            album=album,
            albumtype=albumtype,
            artist=artist,
            artists=[artist],
            artist_id=str(artist_id),
            albumstatus="Official",
            album_id=deezer_id,
            artist_credit=self.get_artist([album_data["artist"]])[0],
            mediums=max(filter(None, medium_totals.keys())),
            data_source=self.data_source,
            data_url=album_data["link"],
            label=album_data["label"],
            media="Digital Media",
            style=style,
            upc=album_data.get("upc"),
            va=va,
            year=released.get("year"),
            month=released.get("month"),
            day=released.get("day"),
        )

    def track_for_id(self, track_id: str) -> None | TrackInfo:
        """Fetch a track by its Deezer ID or URL and return a
        TrackInfo object or None if the track is not found.

        :param track_id: (Optional) Deezer ID or URL for the track. Either
            ``track_id`` or ``track_data`` must be provided.

        """
        if not (deezer_id := self._extract_id(track_id)):
            self._log.debug("Invalid Deezer track_id: {}", track_id)
            return None

        if not (track_data := self.fetch_data(f"{self.track_url}{deezer_id}")):
            self._log.debug("Track not found: {}", track_id)
            return None

        track = self._get_track(track_data)

        # Get album's tracks to set `track.index` (position on the entire
        # release) and `track.medium_total` (total number of tracks on
        # the track's disc).
        if not (
            album_tracks_obj := self.fetch_data(
                f"{self.album_url}{track_data['album']['id']}/tracks"
            )
        ):
            return None

        try:
            album_tracks_data = album_tracks_obj["data"]
        except KeyError:
            self._log.debug(
                "Error fetching album tracks for {}", track_data["album"]["id"]
            )
            return None
        medium_total = 0
        for i, track_data in enumerate(album_tracks_data, start=1):
            if track_data["disk_number"] == track.medium:
                medium_total += 1
                if track_data["id"] == track.track_id:
                    track.index = i
        track.medium_total = medium_total
        return track

    def _get_track(self, track_data: JSONDict, total: int = 0) -> TrackInfo:
        """Convert a Deezer track object dict to a TrackInfo object.

        :param track_data: Deezer Track object dict
        """
        artist, artist_id = self.get_artist(
            track_data.get("contributors") or [track_data["artist"] or ""]
        )
        position = track_data.get("track_position")
        return TrackInfo(
            title=track_data["title"],
            track_id=track_data["id"],
            deezer_track_id=track_data["id"],
            isrc=track_data.get("isrc"),
            artist=artist,
            artist_id=str(artist_id),
            length=track_data["duration"],
            index=position,
            medium=track_data["disk_number"],
            deezer_track_rank=track_data.get("rank"),
            medium_index=position,
            data_source=self.data_source,
            data_url=track_data["link"],
            deezer_updated=time.time(),
        )

    def _search_api(
        self,
        query_type: Literal[
            "album",
            "track",
            "artist",
            "history",
            "playlist",
            "podcast",
            "radio",
            "user",
        ],
        filters: SearchFilter,
        query_string: str = "",
    ) -> Sequence[Track]:
        """Query the Deezer Search API for the specified ``query_string``, applying
        the provided ``filters``.

        :param filters: Field filters to apply.
        :param query_string: Additional query to include in the search.
        :return: JSON data for the class:`Response <Response>` object or None
            if no search results are returned.
        """
        query = self._construct_search_query(
            query_string=query_string, filters=filters
        )
        self._log.debug("Searching {.data_source} for '{}'", self, query)
        try:
            response = requests.get(
                f"{self.search_url}{query_type}",
                params={
                    "q": query,
                    "limit": self.config["search_limit"].get(),
                },
                timeout=10,
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            self._log.error(
                "Error fetching data from {.data_source} API\n Error: {}",
                self,
                e,
            )
            return ()
        response_data: Sequence[IDResponse] = response.json().get("data", [])
        self._log.debug(
            "Found {} result(s) from {.data_source} for '{}', returning first 5",
            len(response_data),
            self,
            query,
        )
        return response_data

    def deezerupdate(self, items: Sequence[Item], write: bool):
        """Obtain rank information from Deezer."""
        for index, item in enumerate(items, start=1):
            self._log.info(
                "Processing {}/{} tracks - {} ", index, len(items), item
            )
            try:
                deezer_track_id = item.deezer_track_id
            except AttributeError:
                self._log.debug("No deezer_track_id present for: {}", item)
                continue
            try:
                rank = self.fetch_data(
                    f"{self.track_url}{deezer_track_id}"
                ).get("rank")
                self._log.debug(
                    "Deezer track: {} has {} rank", deezer_track_id, rank
                )
            except Exception as e:
                self._log.debug("Invalid Deezer track_id: {}", e)
                continue
            item.deezer_track_rank = int(rank)
            item.store()
            item.deezer_updated = time.time()
            if write:
                item.try_write()

    def fetch_data(self, url: str):
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            self._log.error("Error fetching data from {}\n Error: {}", url, e)
            return None
        if "error" in data:
            self._log.debug("Deezer API error: {}", data["error"]["message"])
            return None
        return data
