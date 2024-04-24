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

import time
from functools import partial
from typing import Any, Dict

import requests
import unidecode

from beets import ui
from beets.autotag import AlbumInfo, TrackInfo
from beets.dbcore import types
from beets.plugins import BeetsPlugin, MetadataSourcePlugin
from beets.util.id_extractors import deezer_id_regex


class DeezerPlugin(MetadataSourcePlugin, BeetsPlugin):
    data_source = "Deezer"

    item_types = {
        "deezer_track_rank": types.INTEGER,
        "deezer_track_id": types.INTEGER,
        "deezer_updated": types.DateType(),
    }

    # Base URLs for the Deezer API
    # Documentation: https://developers.deezer.com/api/
    search_url = "https://api.deezer.com/search/"
    album_url = "https://api.deezer.com/album/"
    track_url = "https://api.deezer.com/track/"

    id_regex = deezer_id_regex

    def __init__(self):
        super().__init__()

    def commands(self):
        """Add beet UI commands to interact with Deezer."""
        deezer_update_cmd = ui.Subcommand(
            "deezerupdate", help=f"Update {self.data_source} rank"
        )

        def func(lib, opts, args):
            items = lib.items(ui.decargs(args))
            self.deezerupdate(items, ui.should_write())

        deezer_update_cmd.func = func

        return [deezer_update_cmd]

    def fetch_data(self, url):
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            self._log.error("Error fetching data from {}\n Error: {}", url, e)
            return None
        if "error" in data:
            self._log.error("Deezer API error: {}", data["error"]["message"])
            return None
        return data

    def album_for_id(self, album_id):
        """Fetch an album by its Deezer ID or URL and return an
        AlbumInfo object or None if the album is not found.

        :param album_id: Deezer ID or URL for the album.
        :type album_id: str
        :return: AlbumInfo object for album.
        :rtype: beets.autotag.hooks.AlbumInfo or None
        """
        deezer_id = self._get_id("album", album_id, self.id_regex)
        if deezer_id is None:
            return None
        album_data = self.fetch_data(self.album_url + deezer_id)
        if album_data is None:
            return None
        contributors = album_data.get("contributors")
        if contributors is not None:
            artist, artist_id = self.get_artist(contributors)
        else:
            artist, artist_id = None, None

        album_url = self.album_url + deezer_id
        album_data = requests.get(album_url, timeout=10).json()

        tracks_data = requests.get(f"{album_url}/tracks", timeout=10).json()
        tracks_total = tracks_data["total"]
        tracks_data = tracks_data["data"]
        release_date = album_data["release_date"]
        released = dict(
            zip(("year", "month", "day"), map(int, release_date.split("-")))
        )
        if not tracks_data or not released:
            return None

        get_track = partial(self._get_track, total=tracks_total)
        album = AlbumInfo(list(map(get_track, tracks_data)))
        album.update(released)
        album.artist, album.artist_id = self.get_artist(
            album_data["contributors"]
        )
        album.album, album.albumtype = (
            album_data["title"],
            album_data["record_type"],
        )
        album.va = False
        if " VA" in album.album:
            album.artist = "Various Artists"
            album.albumtype = "compilation"
            album.va = True
        else:
            album.artist_credit = self.get_artist([album_data["artist"]])[0]
        genres = album_data["genres"]["data"]
        album.style = ", ".join((g.get("name") or "" for g in genres))
        album.style = album.style.replace("Electro", "electronic")
        album.update(
            albumstatus="Official",
            album_id=deezer_id,
            data_source=self.data_source,
            data_url=album_data["link"],
            label=album_data["label"],
            media="Digital Media",
            mediums=1,
            upc=album_data.get("upc"),
        )
        return album

    def _get_track(
        self, track_data: Dict[str, Any], total: int = 0
    ) -> TrackInfo:
        """Convert a Deezer track object dict to a TrackInfo object.

        :param track_data: Deezer Track object dict
        :type track_data: dict
        :return: TrackInfo object for track
        :rtype: beets.autotag.hooks.TrackInfo
        """
        artist, artist_id = self.get_artist(
            track_data.get("contributors", [track_data.get("artist") or ""])
        )
        position = track_data.get("track_position")
        return TrackInfo(
            title=track_data["title"],
            track_id=track_data["id"],
            deezer_track_id=track_data["id"],
            isrc=track_data.get("isrc"),
            artist=artist,
            artist_id=artist_id,
            length=track_data["duration"],
            index=position,
            medium=track_data.get("disk_number"),
            medium_index=position,
            medium_total=total,
            data_source=self.data_source,
            data_url=track_data["link"],
            deezer_updated=time.time(),
        )

    def track_for_id(self, track_id=None, track_data=None):
        """Fetch a track by its Deezer ID or URL and return a
        TrackInfo object or None if the track is not found.

        :param track_id: (Optional) Deezer ID or URL for the track. Either
            ``track_id`` or ``track_data`` must be provided.
        :type track_id: str
        :param track_data: (Optional) Simplified track object dict. May be
            provided instead of ``track_id`` to avoid unnecessary API calls.
        :type track_data: dict
        :return: TrackInfo object for track
        :rtype: beets.autotag.hooks.TrackInfo or None
        """
        if track_data is None:
            deezer_id = self._get_id("track", track_id, self.id_regex)
            if deezer_id is None:
                return None
            track_data = self.fetch_data(self.track_url + deezer_id)
            if track_data is None:
                return None
        track = self._get_track(track_data)

        # Get album's tracks to set `track.index` (position on the entire
        # release) and `track.medium_total` (total number of tracks on
        # the track's disc).
        album_tracks_obj = self.fetch_data(
            self.album_url + str(track_data["album"]["id"]) + "/tracks"
        )
        if album_tracks_obj is None:
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

    @staticmethod
    def _construct_search_query(filters=None, keywords=""):
        """Construct a query string with the specified filters and keywords to
        be provided to the Deezer Search API
        (https://developers.deezer.com/api/search).

        :param filters: (Optional) Field filters to apply.
        :type filters: dict
        :param keywords: (Optional) Query keywords to use.
        :type keywords: str
        :return: Query string to be provided to the Search API.
        :rtype: str
        """
        query = (
            keywords + " " + " ".join(f'{k}:"{v}"' for k, v in filters.items())
        )
        return unidecode.unidecode(query)

    def _search_api(self, query_type, filters=None, keywords=""):
        """Query the Deezer Search API for the specified ``keywords``, applying
        the provided ``filters``.

        :param query_type: The Deezer Search API method to use. Valid types
            are: 'album', 'artist', 'history', 'playlist', 'podcast',
            'radio', 'track', 'user', and 'track'.
        :type query_type: str
        :param filters: (Optional) Field filters to apply.
        :type filters: dict
        :param keywords: (Optional) Query keywords to use.
        :type keywords: str
        :return: JSON data for the class:`Response <Response>` object or None
            if no search results are returned.
        :rtype: dict or None
        """
        query = self._construct_search_query(keywords=keywords, filters=filters)
        if not query:
            return None
        self._log.debug(f"Searching {self.data_source} for '{query}'")
        response = requests.get(
            self.search_url + query_type,
            params={"q": query},
            timeout=10,
        )
        response.raise_for_status()
        response_data = response.json().get("data", [])
        self._log.debug(
            "Found {} result(s) from {} for '{}', returning first 5",
            len(response_data),
            self.data_source,
            query,
        )
        return response_data

    def deezerupdate(self, items, write):
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
