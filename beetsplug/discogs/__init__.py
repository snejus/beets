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
from contextlib import suppress
from functools import cache, partial
from string import ascii_lowercase
from typing import TYPE_CHECKING
from unicodedata import normalize

import confuse
from discogs_client import Client, Master, Release
from discogs_client.exceptions import DiscogsAPIError
from pycountry import countries, subdivisions
from requests.exceptions import ConnectionError

import beets
import beets.ui
from beets import config
from beets.autotag.distance import string_dist
from beets.autotag.hooks import AlbumInfo, TrackInfo
from beets.exceptions import UserError
from beets.metadata_plugins import MetadataSourcePlugin

from .states import DISAMBIGUATION_RE, ArtistState, TracklistState

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

    from beets.library import Item

    from .types import ReleaseFormat, Track

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
    "Europe": "EU",
    "Worldwide": "XW",
}
TRACK_INDEX_RE = re.compile(
    r"""
    (.*?)   # medium: everything before medium_index.
    (\d*?)  # medium_index: a number at the end of
            # `position`, except if followed by a subtrack index.
            # subtrack_index: can only be matched if medium
            # or medium_index have been matched, and can be
    (
        (?<=\w)\.[\w]+  # a dot followed by a string (A.1, 2.A)
      | (?<=\d)[A-Z]+   # a string that follows a number (1A, B2a)
    )?
    """,
    re.VERBOSE,
)
DISAMBIGUATION_RE = re.compile(r" \(\d+\)")

split_country = re.compile(r"\b(?:, |,? & )\b").split
remove_va_ft = partial(re.compile(r"va\b|\bf(ea)?t.*", re.I).sub, "")
remove_disc = partial(re.compile(r"(?i)\b(CD|disc|vinyl)\s*\d+", re.I).sub, "")


def clean_query(query: str) -> str:
    return remove_disc(query).replace("'", "")


def get_title_without_remix(name: str) -> str:
    """Split the track name, deduce the title and return it."""
    return re.sub(r"[([]+.*", "", name).strip()


class DiscogsPlugin(MetadataSourcePlugin):
    def __init__(self):
        super().__init__()
        self.config.add(
            {
                "apikey": API_KEY,
                "apisecret": API_SECRET,
                "tokenfile": "discogs_token.json",
                "user_token": "",
                "separator": ", ",
                "index_tracks": False,
                "append_style_genre": False,
                "strip_disambiguation": True,
                "featured_string": "Feat.",
                "anv": {
                    "artist_credit": True,
                    "artist": False,
                    "album_artist": False,
                },
            }
        )
        self.config["apikey"].redact = True
        self.config["apisecret"].redact = True
        self.config["user_token"].redact = True
        self.setup()

    def setup(self, session=None) -> None:
        """Create the `discogs_client` field. Authenticate if necessary."""
        c_key = self.config["apikey"].as_str()
        c_secret = self.config["apisecret"].as_str()

        # Try using a configured user token (bypassing OAuth login).
        user_token = self.config["user_token"].as_str()
        if user_token:
            # The rate limit for authenticated users goes up to 60
            # requests per minute.
            self.discogs_client = Client(USER_AGENT, user_token=user_token)
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

    def reset_auth(self) -> None:
        """Delete token file & redo the auth steps."""
        os.remove(self._tokenfile())
        self.setup()

    def _tokenfile(self) -> str:
        """Get the path to the JSON file for storing the OAuth token."""
        return self.config["tokenfile"].get(confuse.Filename(in_app_dir=True))

    def authenticate(self, c_key: str, c_secret: str) -> tuple[str, str]:
        # Get the link for the OAuth page.
        auth_client = Client(USER_AGENT, c_key, c_secret)
        try:
            _, _, url = auth_client.get_authorize_url()
        except CONNECTION_ERRORS as e:
            self._log.debug("connection error: {}", e)
            raise UserError("communication with Discogs failed")

        beets.ui.print_("To authenticate with Discogs, visit:")
        beets.ui.print_(url)

        # Ask for the code and validate it.
        code = beets.ui.input_("Enter the code:")
        try:
            token, secret = auth_client.get_access_token(code)
        except DiscogsAPIError:
            raise UserError("Discogs authorization failed")
        except CONNECTION_ERRORS as e:
            self._log.debug("connection error: {}", e)
            raise UserError("Discogs token request failed")

        # Save the token for later use.
        self._log.debug("Discogs token {}, secret {}", token, secret)
        with open(self._tokenfile(), "w") as f:
            json.dump({"token": token, "secret": secret}, f)

        return token, secret

    def candidates(
        self, items: Sequence[Item], artist: str, album: str, va_likely: bool
    ) -> Iterable[AlbumInfo]:
        item = items[0]

        results: list[AlbumInfo] = []
        if barcode := item.barcode:
            with suppress(DiscogsAPIError):
                results.extend(self.get_albums(barcode=barcode))

        if getattr(item, "data_source", "").lower() == "discogs" and (
            album_info := self.album_for_id(
                str(item.discogs_albumid or item.mb_albumid).replace("-1", "")
            )
        ):
            results.append(album_info)

        name = album or item.album or item.title
        if "various" in artist.lower():
            artist = item.artist
        query = f"{remove_va_ft(artist).strip()} - {name}"
        results.extend(self.get_albums(clean_query(query)))
        if not results and items and item.label and item.album:
            query = f"{item.label} {item.album}"
            results.extend(self.get_albums(clean_query(query)))

        return results

    def get_track_from_album(
        self, album_info: AlbumInfo, compare: Callable[[TrackInfo], float]
    ) -> TrackInfo | None:
        """Return the best matching track of the release."""
        scores_and_tracks = [(compare(t), t) for t in album_info.tracks]
        score, track_info = min(scores_and_tracks, key=lambda x: x[0])
        if score > 0.3:
            return None

        track_info["artist"] = album_info.artist
        track_info["artist_id"] = album_info.artist_id
        track_info["album"] = album_info.album
        return track_info

    def item_candidates(
        self, item: Item, artist: str, title: str
    ) -> Iterable[TrackInfo]:
        albums = self.candidates([item], artist, title, False)

        def compare_func(track_info: TrackInfo) -> float:
            return string_dist(track_info.title, title)

        tracks = (self.get_track_from_album(a, compare_func) for a in albums)
        return list(filter(None, tracks))

    def album_for_id(self, album_id: str) -> AlbumInfo | None:
        """Fetches an album by its Discogs ID and returns an AlbumInfo object
        or None if the album is not found.
        """
        self._log.debug("Searching for release {}", album_id)

        discogs_id = self._extract_id(album_id)

        if not discogs_id:
            return None

        result = Release(self.discogs_client, {"id": discogs_id})
        # Try to obtain title to verify that we indeed have a valid Release
        try:
            getattr(result, "title")
        except DiscogsAPIError as e:
            if e.status_code != 404:
                self._log.debug(
                    "API Error: {} (query: {})",
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

    def track_for_id(self, track_id: str) -> TrackInfo | None:
        if album := self.album_for_id(track_id):
            for track in album.tracks:
                if track.track_id == track_id:
                    return track
        return None

    def get_albums(self, *args, **kwargs) -> Iterator[AlbumInfo]:
        """Returns a list of AlbumInfo objects for a discogs search query."""
        kwargs["type"] = "release"
        query = " ".join(args)
        self._log.debug("Searching for '{}', {}", query, kwargs)

        try:
            results = self.discogs_client.search(*args, **kwargs)
            results.per_page = self.config["search_limit"].get()
            releases = results.page(1)
        except CONNECTION_ERRORS:
            self._log.debug(
                "Communication error while searching for {0!r}",
                query,
                exc_info=True,
            )
        else:
            yield from filter(None, map(self.get_album_info, releases))

    @cache
    def get_master_year(self, master_id: str) -> int | None:
        """Fetches a master release given its Discogs ID and returns its year
        or None if the master release is not found.
        """
        self._log.debug("Getting master release {}", master_id)
        result = Master(self.discogs_client, {"id": master_id})

        try:
            return result.fetch("year")
        except DiscogsAPIError as e:
            if e.status_code != 404:
                self._log.debug(
                    "API Error: {} (query: {})",
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
    def parse_formats(
        formats: list[ReleaseFormat],
        album: str,
        track_titles: list[str],
        va: bool,
    ) -> tuple[str, str, set[str], str]:
        albumtype, albumtypes = "album", set()
        albumstatus, media = "Official", "Digital Media"

        if len(set(map(get_title_without_remix, track_titles))) == 1:
            albumtype = "album"
            albumtypes.update(("album", "single"))

        if formats:
            _format = formats[0]
            media = _format.get("name", "").replace("File", "Digital Media")
            descs = set(map(str.lower, _format.get("descriptions") or []))
            for desc in descs:
                if desc == "promo":
                    albumstatus = "Promotional"
                elif desc in {"album", "ep"}:
                    albumtype = desc
                elif desc == "compilation":
                    albumtype = desc
                    albumtypes.add("album")
                elif desc == "single":
                    albumtype = "single"
                    albumtypes = {"single"}

        if any("remix" in t.lower() for t in track_titles):
            albumtypes.add("remix")

        if len(track_titles) == 1:
            albumtype = "single"

        if va:
            albumtype = "compilation"
            albumtypes.add("album")

        albumtypes.add(albumtype)
        return albumstatus, albumtype, albumtypes, media

    @staticmethod
    def get_country_abbr(country: str) -> str:
        if re.fullmatch(r"[A-Z][A-Z]", country):
            return country.replace("UK", "GB")

        with suppress(ValueError, LookupError):
            name = normalize("NFKD", country).encode("ascii", "ignore").decode()
            return (
                COUNTRY_OVERRIDES.get(name)
                or getattr(
                    countries.get(name=name, default=object),
                    "alpha_2",
                    None,
                )
                or subdivisions.lookup(name).country_code
            )

        return country

    def get_album_info(self, result: Release) -> AlbumInfo | None:
        """Returns an AlbumInfo object for a discogs Release object."""
        # Explicitly reload the `Release` fields, as they might not be yet
        # present if the result is from a `discogs_client.search()`.
        if not result.data.get("artists"):
            try:
                result.refresh()
            except CONNECTION_ERRORS:
                self._log.debug(
                    "Connection error in release lookup: {0}",
                    result,
                )
                return None

        artist_data = [a.data for a in result.artists]
        # Information for the album artist
        albumartist = ArtistState.from_config(
            self.config, artist_data, for_album_artist=True
        )

        album = re.sub(r" +", " ", result.title)
        album_id = result.data["id"]
        # Use `.data` to access the tracklist directly instead of the
        # convenient `.tracklist` property, which will strip out useful artist
        # information and leave us with skeleton `Artist` objects that will
        # each make an API call just to get the same data back.
        tracks = self.get_tracks(
            result.data["tracklist"],
            ArtistState.from_config(self.config, artist_data),
        )

        # Extract information for the optional AlbumInfo fields, if possible.
        va = albumartist.artist == config["va_name"].as_str()
        year = result.data.get("year")
        mediums = [t["medium"] for t in tracks]
        data_url = result.data.get("uri")
        styles: list[str] = result.data.get("styles") or []
        genres: list[str] = result.data.get("genres") or []

        if self.config["append_style_genre"]:
            genres.extend(styles)

        discogs_albumid = self._extract_id(result.data.get("uri"))

        # Extract information for the optional AlbumInfo fields that are
        # contained on nested discogs fields.
        albumstatus, albumtype, albumtypes, media = self.parse_formats(
            result.data.get("formats") or [],
            album,
            [t["title"] for t in tracks],
            va,
        )
        cover_art_url = self.select_cover_art(result)

        # Additional cleanups
        # Explicitly set the `media` for the tracks, since it is expected by
        # `autotag.apply_metadata`, and set `medium_total`.
        for track in tracks:
            track.media = media
            track.medium_total = mediums.count(track.medium)
            # Discogs does not have track IDs. Invent our own IDs as proposed
            # in #2336.
            track.track_id = f"{album_id}-{track.track_alt or track.index}"
            track.data_url = data_url
            track.data_source = "Discogs"

        # Retrieve master release id (returns None if there isn't one).
        master_id = result.data.get("master_id")
        # Assume `original_year` is equal to `year` for releases without
        # a master release, otherwise fetch the master release.
        original_year = self.get_master_year(master_id) if master_id else year

        released = (result.data.get("released") or "").split("-")
        label = labels[0] if (labels := result.data.get("labels")) else None
        return AlbumInfo(
            album=album,
            album_id=album_id,
            **albumartist.info,  # Unpacks values to satisfy the keyword arguments
            tracks=tracks,
            albumstatus=albumstatus,
            albumtype=albumtype,
            albumtypes=sorted(albumtypes),
            va=va,
            year=int(released[0]) if released[0] else None,
            month=int(released[1]) if len(released) > 1 else None,
            day=int(released[2]) if len(released) > 2 else None,
            comments=result.data.get("notes"),
            label=self.strip_disambiguation(label["name"]) if label else None,
            mediums=len(set(mediums)),
            releasegroup_id=str(master_id) if master_id else None,
            catalognum=(
                label
                and label["catno"].replace("none", "").replace(" ", "").upper()
                or None
            ),
            country=(
                ", ".join(map(self.get_country_abbr, split_country(country)))
                if (country := result.data.get("country"))
                else None
            ),
            artist_sort=(
                self.strip_disambiguation(asort)
                if (asort := result.data.get("artists_sort"))
                else None
            ),
            style=self.config["separator"].as_str().join(genres) or None,
            genres=styles,
            media=media,
            original_year=original_year,
            data_source=self.data_source,
            data_url=data_url,
            discogs_albumid=discogs_albumid,
            discogs_labelid=label["id"] if label else None,
            discogs_artistid=albumartist.artist_id,
            cover_art_url=cover_art_url,
        )

    def select_cover_art(self, result: Release) -> str | None:
        """Returns the best candidate image, if any, from a Discogs `Release` object."""
        if result.data.get("images") and len(result.data.get("images")) > 0:
            # The first image in this list appears to be the one displayed first
            # on the release page - even if it is not flagged as `type: "primary"` - and
            # so it is the best candidate for the cover art.
            return result.data.get("images")[0].get("uri")

        return None

    def get_tracks(
        self,
        tracklist: list[Track],
        albumartistinfo: ArtistState,
    ) -> list[TrackInfo]:
        """Returns a list of TrackInfo objects for a discogs tracklist."""
        try:
            clean_tracklist: list[Track] = self._coalesce_tracks(tracklist)
        except Exception as exc:
            # FIXME: this is an extra precaution for making sure there are no
            # side effects after #2222. It should be removed after further
            # testing.
            self._log.debug("{}", traceback.format_exc())
            self._log.error("uncaught exception in _coalesce_tracks: {}", exc)
            clean_tracklist = tracklist
        t = TracklistState.build(self, clean_tracklist, albumartistinfo)
        # Fix up medium and medium_index for each track. Discogs position is
        # unreliable, but tracks are in order.
        medium = None
        medium_count, index_count, side_count = 0, 0, 0
        sides_per_medium = 1

        # If a medium has two sides (ie. vinyl or cassette), each pair of
        # consecutive sides should belong to the same medium.
        if all([medium is not None for medium in t.mediums]):
            m = sorted(
                {medium.lower() if medium else "" for medium in t.mediums}
            )
            # If all track.medium are single consecutive letters, assume it is
            # a 2-sided medium.
            if "".join(m) in ascii_lowercase:
                sides_per_medium = 2

        for i, track in enumerate(t.tracks):
            # Handle special case where a different medium does not indicate a
            # new disc, when there is no medium_index and the ordinal of medium
            # is not sequential. For example, I, II, III, IV, V. Assume these
            # are the track index, not the medium.
            # side_count is the number of mediums or medium sides (in the case
            # of two-sided mediums) that were seen before.
            medium_str = t.mediums[i]
            medium_index = t.medium_indices[i]
            medium_is_index = (
                medium_str
                and not medium_index
                and (
                    len(medium_str) != 1
                    or
                    # Not within standard incremental medium values (A, B, C, ...).
                    ord(medium_str) - 64 != side_count + 1
                )
            )

            if not medium_is_index and medium != medium_str:
                side_count += 1
                if sides_per_medium == 2:
                    if side_count % sides_per_medium:
                        # Two-sided medium changed. Reset index_count.
                        index_count = 0
                        medium_count += 1
                else:
                    # Medium changed. Reset index_count.
                    medium_count += 1
                    index_count = 0
                medium = medium_str

            index_count += 1
            medium_count = 1 if medium_count == 0 else medium_count
            track.medium, track.medium_index = medium_count, index_count

        # Get `disctitle` from Discogs index tracks. Assume that an index track
        # before the first track of each medium is a disc title.
        for track in t.tracks:
            if track.medium_index == 1:
                if track.index in t.index_tracks:
                    disctitle = t.index_tracks[track.index]
                else:
                    disctitle = None
            track.disctitle = disctitle

        return t.tracks

    def _coalesce_tracks(self, raw_tracklist: list[Track]) -> list[Track]:
        """Pre-process a tracklist, merging subtracks into a single track. The
        title for the merged track is the one from the previous index track,
        if present; otherwise it is a combination of the subtracks titles.
        """
        # Pre-process the tracklist, trying to identify subtracks.

        subtracks: list[Track] = []
        tracklist: list[Track] = []
        prev_subindex = ""
        for track in raw_tracklist:
            # Regular subtrack (track with subindex).
            if track["position"]:
                _, _, subindex = self.get_track_index(track["position"])
                if subindex:
                    if subindex.rjust(len(raw_tracklist)) > prev_subindex:
                        # Subtrack still part of the current main track.
                        subtracks.append(track)
                    else:
                        # Subtrack part of a new group (..., 1.3, *2.1*, ...).
                        self._add_merged_subtracks(tracklist, subtracks)
                        subtracks = [track]
                    prev_subindex = subindex.rjust(len(raw_tracklist))
                    continue

            # Index track with nested sub_tracks.
            if not track["position"] and "sub_tracks" in track:
                # Append the index track, assuming it contains the track title.
                tracklist.append(track)
                self._add_merged_subtracks(tracklist, track["sub_tracks"])
                continue

            # Regular track or index track without nested sub_tracks.
            if subtracks:
                self._add_merged_subtracks(tracklist, subtracks)
                subtracks = []
                prev_subindex = ""
            tracklist.append(track)

        # Merge and add the remaining subtracks, if any.
        if subtracks:
            self._add_merged_subtracks(tracklist, subtracks)

        return tracklist

    def _add_merged_subtracks(
        self,
        tracklist: list[Track],
        subtracks: list[Track],
    ) -> None:
        """Modify `tracklist` in place, merging a list of `subtracks` into
        a single track into `tracklist`."""
        # Calculate position based on first subtrack, without subindex.
        idx, medium_idx, sub_idx = self.get_track_index(
            subtracks[0]["position"]
        )
        position = f"{idx or ''}{medium_idx or ''}"

        if tracklist and not tracklist[-1]["position"]:
            # Assume the previous index track contains the track title.
            if sub_idx:
                # "Convert" the track title to a real track, discarding the
                # subtracks assuming they are logical divisions of a
                # physical track (12.2.9 Subtracks).
                tracklist[-1]["position"] = position
            else:
                # Promote the subtracks to real tracks, discarding the
                # index track, assuming the subtracks are physical tracks.
                index_track = tracklist.pop()
                # Fix artists when they are specified on the index track.
                if index_track.get("artists"):
                    for subtrack in subtracks:
                        if not subtrack.get("artists"):
                            subtrack["artists"] = index_track["artists"]
                # Concatenate index with track title when index_tracks
                # option is set
                if self.config["index_tracks"]:
                    for subtrack in subtracks:
                        subtrack["title"] = (
                            f"{index_track['title']}: {subtrack['title']}"
                        )
                tracklist.extend(subtracks)
        else:
            # Merge the subtracks, pick a title, and append the new track.
            track = subtracks[0].copy()
            track["title"] = " / ".join([t["title"] for t in subtracks])
            tracklist.append(track)

    def strip_disambiguation(self, text: str) -> str:
        """Removes discogs specific disambiguations from a string.
        Turns 'Label Name (5)' to 'Label Name' or 'Artist (1) & Another Artist (2)'
        to 'Artist & Another Artist'. Does nothing if strip_disambiguation is False."""
        if not self.config["strip_disambiguation"]:
            return text
        return DISAMBIGUATION_RE.sub("", text)

    def get_track_info(
        self,
        track: Track,
        index: int,
        divisions: list[str],
        albumartistinfo: ArtistState,
    ) -> tuple[TrackInfo, str | None, str | None]:
        """Returns a TrackInfo object for a discogs track."""

        title = track["title"]
        if self.config["index_tracks"]:
            prefix = ", ".join(divisions)
            if prefix:
                title = f"{prefix}: {title}"
        track_id = None
        medium, medium_index, _ = self.get_track_index(track["position"])

        length = self.get_track_length(track["duration"])
        # If artists are found on the track, we will use those instead
        artistinfo = ArtistState.from_config(
            self.config,
            [
                *(track.get("artists") or albumartistinfo.raw_artists),
                *track.get("extraartists", []),
            ],
        )

        return (
            TrackInfo(
                title=title,
                track_id=track_id,
                **artistinfo.info,
                length=length,
                index=index,
            ),
            medium,
            medium_index,
        )

    @staticmethod
    def get_track_index(
        position: str,
    ) -> tuple[str | None, str | None, str | None]:
        """Returns the medium, medium index and subtrack index for a discogs
        track position."""
        # Match the standard Discogs positions (12.2.9), which can have several
        # forms (1, 1-1, A1, A1.1, A1a, ...).
        medium = index = subindex = None
        if match := TRACK_INDEX_RE.fullmatch(position.upper()):
            medium, index, subindex = match.groups()

            if subindex and subindex.startswith("."):
                subindex = subindex[1:]

        return medium or None, index or None, subindex or None

    def get_track_length(self, duration: str) -> int | None:
        """Returns the track length in seconds for a discogs duration."""
        try:
            length = time.strptime(duration, "%M:%S")
        except ValueError:
            return None
        return length.tm_min * 60 + length.tm_sec
