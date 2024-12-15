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

"""Adds Discogs album search support to the autotagger. Requires the
python3-discogs-client library.
"""

from __future__ import annotations

import http
import json
import os
import re
import socket
import time
import traceback
from collections import Counter
from contextlib import suppress
from functools import lru_cache, partial
from itertools import groupby, islice
from unicodedata import normalize

import confuse
from discogs_client import Client, Master, Release, Track
from discogs_client import __version__ as dc_string
from discogs_client.exceptions import DiscogsAPIError, HTTPError
from pycountry import countries, subdivisions
from requests.exceptions import ConnectionError
from typing_extensions import TypedDict

import beets
import beets.ui
from beets import config
from beets.autotag.hooks import AlbumInfo, TrackInfo, string_dist
from beets.plugins import BeetsPlugin, MetadataSourcePlugin, get_distance
from beets.util.id_extractors import extract_discogs_id_regex

USER_AGENT = f"beets/{beets.__version__} +https://beets.io/"
API_KEY = "rAzVUQYRaoFjeBjyWuWZ"
API_SECRET = "plxtUTqoCzwxZpqdPysCwGuBSmZNdZVy"

# Exceptions that discogs_client should really handle but does not.
CONNECTION_ERRORS = (
    ConnectionError,
    socket.error,
    http.client.HTTPException,
    ValueError,  # JSON decoding raises a ValueError.
    DiscogsAPIError,
)
COUNTRY_OVERRIDES = {
    "Russia": "RU",  # pycountry: Russian Federation
    "The Netherlands": "NL",  # pycountry: Netherlands
    "UK": "GB",  # pycountry: Great Britain
    "D.C.": "US",
    "South Korea": "KR",  # pycountry: Korea, Republic of
}

remove_idx = partial(re.compile(r" +\(\d+\)").sub, "")
remove_year = partial(re.compile(r" +\((19|20)\d\d\)").sub, "")
remove_remix = partial(re.compile(r" *\(.*?mix\)", re.I).sub, "")
remove_va_ft = partial(
    re.compile(r"va\b|various artists|\bf(ea)?t.*", re.I).sub, ""
)
remove_disc = partial(
    re.compile(r"(?i)\b(CD|disc|vinyl)\s*\d+| *\(.*?version\)", re.I).sub, ""
)


def clean_query(query: str) -> str:
    return remove_disc(query).replace("'", "")


TRACK_INDEX_PAT = re.compile(
    r"""
    (?:LP-|DVD[0-9]?|Video-)?
    (.{,3}?)           # medium: everything before medium_index.
    [-]?
    ((?<!\d)\d+?)?     # medium_index: a number at the end of `position`, except
                       # when followed by a subtrack index.
    \.?
    (                  # subtrack_index: can only be matched if medium or
                       # medium_index have been matched, and can be
        (?<=\w\.)\w+   # a dot followed by a string (A.1, 2.A)
      | (?<=\d)[A-Z]+  # a string that follows a number (1A, B2A)
    )?
    """,
    re.VERBOSE,
)


def get_medium(medium_str: str) -> int:
    if medium_str.isdigit():
        return int(medium_str)

    return ((ord(medium_str[0]) - ord("A")) // 2) + 1


class ReleaseFormat(TypedDict):
    name: str
    qty: int
    descriptions: list[str] | None


def get_title_without_remix(name: str) -> str:
    """Split the track name, deduce the title and return it."""
    return re.sub(r"[([]+.*", "", name)


class DiscogsPlugin(BeetsPlugin):
    def __init__(self):
        super().__init__()
        self.check_discogs_client()
        self.config.add(
            {
                "apikey": API_KEY,
                "apisecret": API_SECRET,
                "tokenfile": "discogs_token.json",
                "source_weight": 0.5,
                "user_token": "",
                "separator": ", ",
                "index_tracks": False,
                "append_style_genre": False,
                "results_count": 5,
            }
        )
        self.config["apikey"].redact = True
        self.config["apisecret"].redact = True
        self.config["user_token"].redact = True
        self.discogs_client = None
        self.register_listener("import_begin", self.setup)

    def check_discogs_client(self):
        """Ensure python3-discogs-client version >= 2.3.15"""
        dc_min_version = [2, 3, 15]
        dc_version = [int(elem) for elem in dc_string.split(".")]
        min_len = min(len(dc_version), len(dc_min_version))
        gt_min = [
            (elem > elem_min)
            for elem, elem_min in zip(
                dc_version[:min_len], dc_min_version[:min_len]
            )
        ]
        if True not in gt_min:
            self._log.warning(
                "python3-discogs-client version should be >= 2.3.15"
            )

    def setup(self, session=None):
        """Create the `discogs_client` field. Authenticate if necessary."""
        c_key = self.config["apikey"].as_str()
        c_secret = self.config["apisecret"].as_str()

        # Try using a configured user token (bypassing OAuth login).
        user_token = self.config["user_token"].as_str()
        if user_token:
            # The rate limit for authenticated users goes up to 60
            # requests per minute.
            self.discogs_client = Client(USER_AGENT, user_token=user_token)
            # self.discogs_client.verbose = True
            return

        # Get the OAuth token from a file or log in.
        try:
            with open(self._tokenfile()) as f:
                tokendata = json.load(f)
        except OSError:
            # No token yet. Generate one.
            token, secret = self.authenticate(c_key, c_secret)
        else:
            token = tokendata["token"]
            secret = tokendata["secret"]

        self.discogs_client = Client(USER_AGENT, c_key, c_secret, token, secret)
        # self.discogs_client.verbose = True

    def reset_auth(self):
        """Delete token file & redo the auth steps."""
        os.remove(self._tokenfile())
        self.setup()

    def _tokenfile(self):
        """Get the path to the JSON file for storing the OAuth token."""
        return self.config["tokenfile"].get(confuse.Filename(in_app_dir=True))

    def authenticate(self, c_key, c_secret):
        # Get the link for the OAuth page.
        auth_client = Client(USER_AGENT, c_key, c_secret)
        try:
            _, _, url = auth_client.get_authorize_url()
        except CONNECTION_ERRORS as e:
            self._log.debug("connection error: {0}", e)
            raise beets.ui.UserError("communication with Discogs failed")

        beets.ui.print_("To authenticate with Discogs, visit:")
        beets.ui.print_(url)

        # Ask for the code and validate it.
        code = beets.ui.input_("Enter the code:")
        try:
            token, secret = auth_client.get_access_token(code)
        except DiscogsAPIError:
            raise beets.ui.UserError("Discogs authorization failed")
        except CONNECTION_ERRORS as e:
            self._log.debug("connection error: {0}", e)
            raise beets.ui.UserError("Discogs token request failed")

        # Save the token for later use.
        self._log.debug("Discogs token {0}, secret {1}", token, secret)
        with open(self._tokenfile(), "w") as f:
            json.dump({"token": token, "secret": secret}, f)

        return token, secret

    def album_distance(self, items, album_info, mapping):
        """Returns the album distance."""
        return get_distance(
            data_source="Discogs", info=album_info, config=self.config
        )

    def track_distance(self, item, track_info):
        """Returns the track distance."""
        return get_distance(
            data_source="Discogs", info=track_info, config=self.config
        )

    def candidates(self, items, artist, album, va_likely, extra_tags=None):
        """Returns a list of AlbumInfo objects for discogs search results
        matching an album and artist (if not various).
        """
        if not self.discogs_client:
            return ()

        item = items[0]
        name = album or item.album or item.title
        query = f"{remove_va_ft(artist).strip()} - {name}"

        results = []
        if barcode := item.barcode:
            with suppress(DiscogsAPIError):
                results.extend(self.get_albums(query, barcode=barcode))

        if getattr(item, "data_source", "").lower() == "discogs":
            album_id = str(item.discogs_albumid or item.mb_albumid)

            try:
                results.append(
                    self.get_album_info(self.discogs_client.release(album_id))
                )
            except HTTPError as exc:
                if exc.status_code == 404:
                    album_id = album_id.replace("-1", "")
                    results.append(
                        self.get_album_info(
                            self.discogs_client.release(album_id)
                        )
                    )
            except Exception:
                pass

        try:
            results.extend(self.get_albums(query))
            if not results and items and item.label:
                query = f"{item.label} {item.album}"
                results.extend(self.get_albums(query))
        except DiscogsAPIError as e:
            self._log.debug("API Error: {0} (query: {1})", e, query)
            if e.status_code == 401:
                self.reset_auth()
                return self.candidates(items, artist, album, va_likely)
        except CONNECTION_ERRORS:
            self._log.debug("Connection error in album search", exc_info=True)
        else:
            return results

        return ()

    def get_track_from_album_by_title(
        self, album_info, title, dist_threshold=0.3
    ):
        def compare_func(track_info):
            track_title = getattr(track_info, "title", None)
            dist = string_dist(track_title, title)
            return track_title and dist < dist_threshold

        return self.get_track_from_album(album_info, compare_func)

    def get_track_from_album(self, album_info, compare_func):
        """Return the first track of the release where `compare_func` returns
        true.

        :return: TrackInfo object.
        :rtype: beets.autotag.hooks.TrackInfo
        """
        if not album_info:
            return None

        for track_info in album_info.tracks:
            # check for matching position
            if not compare_func(track_info):
                continue

            # attach artist info if not provided
            if not track_info["artist"]:
                track_info["artist"] = album_info.artist
                track_info["artist_id"] = str(album_info.artist_id)
            # attach album info
            track_info["album"] = album_info.album

            return track_info

        return None

    def item_candidates(self, item, artist, title):
        """Returns a list of TrackInfo objects for Search API results
        matching ``title`` and ``artist``.
        :param item: Singleton item to be matched.
        :type item: beets.library.Item
        :param artist: The artist of the track to be matched.
        :type artist: str
        :param title: The title of the track to be matched.
        :type title: str
        :return: Candidate TrackInfo objects.
        :rtype: list[beets.autotag.hooks.TrackInfo]
        """
        if not self.discogs_client:
            return []

        if not artist and not title:
            self._log.debug(
                "Skipping Discogs query. File missing artist and " "title tags."
            )
            return []

        query = f"{artist} {title}"
        try:
            albums = self.get_albums(query)
        except DiscogsAPIError as e:
            self._log.debug("API Error: {0} (query: {1})", e, query)
            if e.status_code == 401:
                self.reset_auth()
                return self.item_candidates(item, artist, title)
            else:
                return []
        except CONNECTION_ERRORS:
            self._log.debug("Connection error in track search", exc_info=True)
            return ()

        candidates = []
        for album_cur in albums:
            self._log.debug("searching within album {0}", album_cur.album)
            track_result = self.get_track_from_album_by_title(
                album_cur, item["title"]
            )
            if track_result:
                candidates.append(track_result)
        # first 10 results, don't overwhelm with options
        return candidates[:10]

    def album_for_id(self, album_id):
        """Fetches an album by its Discogs ID and returns an AlbumInfo object
        or None if the album is not found.
        """
        if not self.discogs_client:
            return

        self._log.debug("Searching for release {0}", album_id)

        discogs_id = extract_discogs_id_regex(album_id)

        if not discogs_id:
            return None

        result = Release(self.discogs_client, {"id": discogs_id})
        # Try to obtain title to verify that we indeed have a valid Release
        try:
            getattr(result, "title")
        except DiscogsAPIError as e:
            if e.status_code != 404:
                self._log.debug(
                    "API Error: {0} (query: {1})",
                    e,
                    result.data["resource_url"],
                )
                if e.status_code == 401:
                    self.reset_auth()
                    return self.album_for_id(album_id)
            return None
        except CONNECTION_ERRORS:
            self._log.debug("Connection error in album lookup", exc_info=True)
            return None
        return self.get_album_info(result)

    def get_albums(self, query: str, **kwargs):
        """Returns a list of AlbumInfo objects for a discogs search query."""
        query = clean_query(query)
        self._log.debug("Searching for '{}', {}", query, kwargs)

        results = self.discogs_client.search(query, type="release", **kwargs)
        max_count = self.config["results_count"].as_number()
        try:
            releases = islice(iter(results), max_count)
        except CONNECTION_ERRORS:
            self._log.debug(
                "Communication error while searching for {0!r}",
                query,
                exc_info=True,
            )
            return []
        return filter(
            None,
            map(
                self.get_album_info,
                sorted(
                    releases, key=lambda r: "file" not in str(r.data["formats"])
                ),
            ),
        )

    @lru_cache
    def get_master_year(self, master_id):
        """Fetches a master release given its Discogs ID and returns its year
        or None if the master release is not found.
        """
        self._log.debug("Searching for master release {0}", master_id)
        result = Master(self.discogs_client, {"id": master_id})

        try:
            year = result.fetch("year")
            return year
        except DiscogsAPIError as e:
            if e.status_code != 404:
                self._log.debug(
                    "API Error: {0} (query: {1})",
                    e,
                    result.data["resource_url"],
                )
                if e.status_code == 401:
                    self.reset_auth()
                    return self.get_master_year(master_id)
            return None
        except CONNECTION_ERRORS:
            self._log.debug(
                "Connection error in master release lookup", exc_info=True
            )
            return None

    @staticmethod
    def get_media_and_albumtype(
        formats: list[ReleaseFormat] | None,
    ) -> tuple[str | None, str | None]:
        media = albumtype = None
        if formats and (first_format := formats[0]):
            if descriptions := first_format["descriptions"]:
                albumtype = ", ".join(descriptions)
            media = first_format["name"]

        return media, albumtype

    def get_album_info(self, result):
        """Returns an AlbumInfo object for a discogs Release object."""
        artist, artist_id = MetadataSourcePlugin.get_artist(
            [a.data for a in result.artists], join_key="join"
        )
        artist_id = str(artist_id)
        album = re.sub(r" +", " ", result.title)
        album_id = str(result.data["id"])
        # Use `.data` to access the tracklist directly instead of the
        # convenient `.tracklist` property, which will strip out useful artist
        # information and leave us with skeleton `Artist` objects that will
        # each make an API call just to get the same data back.
        tracks = self.get_tracks(
            result.tracklist, remove_idx(artist), artist_id
        )

        # Extract information for the optional AlbumInfo fields, if possible.
        va = result.data["artists"][0].get("name", "").lower() == "various"
        year = result.data.get("year")
        country = result.data.get("country")
        if country and not re.match(r"[A-Z][A-Z]", country):
            try:
                name = (
                    normalize("NFKD", country)
                    .encode("ascii", "ignore")
                    .decode()
                )
                country = (
                    COUNTRY_OVERRIDES.get(name)
                    or getattr(
                        countries.get(name=name, default=object),
                        "alpha_2",
                        None,
                    )
                    or subdivisions.lookup(name).country_code
                )
            except (ValueError, LookupError):
                country = "XW"

        if country:
            country = country.replace("UK", "GB")

        data_url = result.data.get("uri")
        style = self.format(result.data.get("styles"))
        base_genre = self.format(result.data.get("genres"))
        if self.config["append_style_genre"] and style:
            genre = self.config["separator"].as_str().join([base_genre, style])
        else:
            genre = base_genre

        discogs_albumid = extract_discogs_id_regex(data_url)

        # Extract information for the optional AlbumInfo fields that are
        # contained on nested discogs fields.
        albumtypes = set()
        albumtype = media = label = catalognum = labelid = None
        formats = result.data.get("formats") or []
        albumstatus = "Official"
        albumtype = "album"
        if formats:
            _format = formats[0]
            descs = set(_format.get("descriptions") or [])
            media = (_format.get("name") or "").replace("File", "Digital Media")
            for desc in descs:
                if desc == "Promo":
                    albumstatus = "Promotional"
                elif desc in {"Album", "EP"}:
                    albumtype = desc.lower()
                    albumtypes.add(albumtype)
                elif desc == "Compilation":
                    albumtype = "compilation"
                    albumtypes.add("album")
                    albumtypes.add("compilation")
                elif albumtype == "Single":
                    albumtypes.add("single")
                    albumtype = "album"
        main_titles = {get_title_without_remix(t["title"]) for t in tracks}
        if len(main_titles) == 1:
            albumtype = "single"
            albumtypes = {"single"}
        albumtypes.add(albumtype)
        if result.data.get("labels"):
            label = result.data["labels"][0].get("name")
            labelid = result.data["labels"][0].get("id")
            catalognum = result.data["labels"][0].get("catno")
            if catalognum == "none":
                catalognum = None
            elif catalognum:
                catalognum = catalognum.upper()
        if va:
            artist = config["va_name"].as_str()

        cover_art_url = self.select_cover_art(result)
        # Explicitly set the `media` for the tracks, since it is expected by
        # `autotag.apply_metadata`, and set `medium_total`.
        medium_count = Counter(t.medium for t in tracks)
        for track in tracks:
            track.media = media
            track.medium_total = medium_count[track.medium]
            if not track.artist:  # get_track_info often fails to find artist
                track.artist = artist
            if not track.artist_id:
                track.artist_id = str(artist_id)
            # Discogs does not have track IDs. Invent our own IDs as proposed in #2336.
            track.track_id = f"{album_id}-{track.track_alt or track.index}"

        # Retrieve master release id (returns None if there isn't one).
        master_id = result.data.get("master_id")
        # Assume `original_year` is equal to `year` for releases without
        # a master release, otherwise fetch the master release.
        # original_year = result.master.year if master_id else year
        released = (result.data.get("released") or "").split("-")
        year = int(released[0]) if len(released[0]) else None
        month = int(released[1]) if len(released) > 1 else None
        day = int(released[2]) if len(released) > 2 else None
        comments = result.data.get("notes") or None

        if artist_sort := result.data.get("artists_sort"):
            artist_sort = remove_idx(artist_sort)
        data = dict(
            album=album,
            album_id=album_id,
            albumtype=albumtype,
            year=year,
            label=remove_idx(label) if label else label,
            artist_sort=artist_sort,
            comments=comments,
            albumtypes=sorted(albumtypes),
            month=month,
            day=day,
            catalognum=catalognum,
            country=country,
            style=genre,
            genre=style,
            original_year=year,
            data_source="discogs",
            data_url=data_url,
            discogs_labelid=labelid,
            discogs_artistid=artist_id,
            discogs_albumid=discogs_albumid,
            cover_art_url=cover_art_url,
        )
        if len(tracks) == 1:
            data.update(albumtype="single", albumtypes=["single"])
            for track in tracks:
                track.index = None
                track.medium_index = None
                track.medium = None
                track.medium_total = None
                track.track_alt = None
                track.update(data)

        return AlbumInfo(
            artist=artist,
            artists=[artist],
            artist_id=artist_id,
            artists_ids=[artist_id],
            tracks=tracks,
            va=va,
            albumstatus=albumstatus,
            mediums=len(medium_count),
            releasegroup_id=str(master_id) if master_id else None,
            media=media,
            **data,
        )

    def select_cover_art(self, result):
        """Returns the best candidate image, if any, from a Discogs `Release` object."""
        if result.data.get("images") and len(result.data.get("images")) > 0:
            # The first image in this list appears to be the one displayed first
            # on the release page - even if it is not flagged as `type: "primary"` - and
            # so it is the best candidate for the cover art.
            return result.data.get("images")[0].get("uri")

        return None

    def format(self, classification):
        if classification:
            return (
                self.config["separator"].as_str().join(sorted(classification))
            )
        else:
            return None

    def get_tracks(
        self, tracklist: list[Track], *args, **kwargs
    ) -> list[TrackInfo]:
        """Returns a list of TrackInfo objects for a discogs tracklist."""
        try:
            clean_tracklist = self.coalesce_tracks(tracklist or [])
        except Exception as exc:
            # FIXME: this is an extra precaution for making sure there are no
            # side effects after #2222. It should be removed after further
            # testing.
            self._log.debug("{}", traceback.format_exc())
            self._log.error("uncaught exception in coalesce_tracks: {}", exc)
            clean_tracklist = tracklist
        if not self.config["index_tracks"]:
            return [
                self.get_track_info(t, i, *args, **kwargs)
                for i, t in enumerate(clean_tracklist, 1)
            ]

        tracks: list[TrackInfo] = []
        index_tracks = {}
        index = 0
        # Distinct works and intra-work divisions, as defined by index tracks.
        divisions: list[str] = []
        next_divisions: list[str] = []
        for raw_track in clean_tracklist:
            # Only real tracks have `position`. Otherwise, it's an index track.
            if raw_track.position:
                index += 1
                if next_divisions:
                    # End of a block of index tracks: update the current
                    # divisions.
                    divisions += next_divisions
                    next_divisions = []
                    if self.config["index_tracks"]:
                        kwargs["prefix"] = ", ".join(divisions)
                tracks.append(
                    self.get_track_info(raw_track, index, *args, **kwargs)
                )
            else:
                next_divisions.append(raw_track.title)
                # We expect new levels of division at the beginning of the
                # tracklist (and possibly elsewhere).
                try:
                    divisions.pop()
                except IndexError:
                    pass
                index_tracks[index + 1] = raw_track.title

        return tracks

    def coalesce_tracks(self, raw_tracklist: list[Track]) -> list[Track]:
        """Pre-process a tracklist, merging subtracks into a single track. The
        title for the merged track is the one from the previous index track,
        if present; otherwise it is a combination of the subtracks titles.
        """

        def add_merged_subtracks(
            tracklist: list[Track], subtracks: list[Track]
        ) -> None:
            """Modify `tracklist` in place, merging a list of `subtracks` into
            a single track into `tracklist`."""
            # Calculate position based on first subtrack, without subindex.
            if tracklist and not tracklist[-1].position:
                # Assume the previous index track contains the track title.
                idx, medium_idx, sub_idx = self.get_track_index(
                    subtracks[0].position
                )
                if sub_idx:
                    # "Convert" the track title to a real track, discarding the
                    # subtracks assuming they are logical divisions of a
                    # physical track (12.2.9 Subtracks).
                    tracklist[-1].data["position"] = (
                        f'{idx or ""}{medium_idx or ""}'
                    )
                else:
                    # Promote the subtracks to real tracks, discarding the
                    # index track, assuming the subtracks are physical tracks.
                    index_track = tracklist.pop()
                    # Fix artists when they are specified on the index track.
                    if track_artists := index_track.artists:
                        for subtrack in subtracks:
                            if not subtrack.artists:
                                subtrack.data["artists"] = track_artists
                    # Concatenate index with track title when index_tracks
                    # option is set
                    if self.config["index_tracks"]:
                        for subtrack in subtracks:
                            subtrack.title = (
                                f"{index_track.title}: {subtrack.title}"
                            )
                    tracklist.extend(subtracks)
            else:
                # Merge the subtracks, pick a title, and append the new track.
                track = subtracks[0].copy()
                track["title"] = " / ".join([t["title"] for t in subtracks])
                tracklist.append(track)

        # Pre-process the tracklist, trying to identify subtracks.
        subtracks: list[Track] = []
        tracklist: list[Track] = []
        prev_subindex = ""
        for track in raw_tracklist:
            # Regular subtrack (track with subindex).
            if track.position:
                if not track.position.isdigit():
                    _, _, subindex = self.get_track_index(track.position)
                    if subindex:
                        if subindex.rjust(len(raw_tracklist)) > prev_subindex:
                            # Subtrack still part of the current main track.
                            subtracks.append(track)
                        else:
                            # Subtrack part of a new group (..., 1.3, *2.1*, ...).
                            add_merged_subtracks(tracklist, subtracks)
                            subtracks = [track]
                        prev_subindex = subindex.rjust(len(raw_tracklist))
                        continue

            # Index track with nested sub_tracks.
            elif sub_tracks := getattr(track, "sub_tracks", []):
                # Append the index track, assuming it contains the track title.
                tracklist.append(track)
                add_merged_subtracks(tracklist, sub_tracks)
                continue

            # Regular track or index track without nested sub_tracks.
            elif subtracks:
                add_merged_subtracks(tracklist, subtracks)
                subtracks = []
                prev_subindex = ""
            tracklist.append(track)

        # Merge and add the remaining subtracks, if any.
        if subtracks:
            add_merged_subtracks(tracklist, subtracks)

        return tracklist

    def get_track_info(
        self,
        track: Track,
        index: int,
        albumartist: str,
        albumartist_id: str,
        prefix: str | None = None,
    ) -> TrackInfo:
        """Returns a TrackInfo object for a discogs track."""
        medium, medium_index, _ = self.get_track_index(track.position)
        credits_by_role = {
            a: list(cs)
            for a, cs in groupby(
                sorted(track.credits, key=lambda a: a.role), lambda a: a.role
            )
        }
        if composers := credits_by_role.get("Written-By"):
            composer = " / ".join(
                remove_idx(c.data["anv"] or c.name) for c in composers
            )
        else:
            composer = None

        artist, artist_id = MetadataSourcePlugin.get_artist(
            (a.data for a in track.artists), join_key="join"
        )
        artist = remove_idx(artist) if artist else albumartist
        artist_id = str(artist_id) if artist_id else albumartist_id
        artists = [artist]
        artist_ids = [artist_id]
        featuring = []
        for credit in [*track.artists, *track.credits]:
            artist_ids.append(str(credit.id))
            name = remove_idx(credit.data["anv"] or credit.name)
            artists.append(name)

            if name.lower() not in artist.lower() and (
                (role := credit.role.lower())
                and (
                    "featuring" in role
                    or "vocals" in role
                    or "music by" in role
                    or "lyrics by" in role
                )
            ):
                featuring.append(name)

        if feat := " & ".join(featuring):
            artist += f" feat. {feat}"

        return TrackInfo(
            title=(f"{prefix}: " if prefix else "") + remove_year(track.title),
            track_id=None,
            artist=artist,
            artists=list(dict.fromkeys(artists)),
            artist_id=artist_id,
            artists_ids=list(dict.fromkeys(artist_ids)),
            composer=composer,
            length=self.get_track_length(track.duration),
            index=index,
            medium=get_medium(medium) if medium else None,
            medium_index=int(medium_index) if medium_index else None,
            track_alt=(
                f"{medium}{medium_index or ''}"
                if medium and medium.isalpha()
                else None
            ),
        )

    def get_track_index(
        self, position: str
    ) -> tuple[str | None, str | None, str | None]:
        """Returns the medium, medium index and subtrack index for a discogs
        track position."""
        # Match the standard Discogs positions (12.2.9), which can have several
        # forms (1, 1-1, A1, A1.1, A1a, ...).
        match = TRACK_INDEX_PAT.fullmatch(position.upper())

        if match:
            medium, index, subindex = (g or None for g in match.groups())
        else:
            self._log.debug("Invalid position: {0}", position)
            medium = index = subindex = None

        return medium, index, subindex

    def get_track_length(self, duration):
        """Returns the track length in seconds for a discogs duration."""
        try:
            length = time.strptime(duration, "%M:%S")
        except ValueError:
            return None
        return length.tm_min * 60 + length.tm_sec
