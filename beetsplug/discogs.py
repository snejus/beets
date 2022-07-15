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
from __future__ import absolute_import, division, print_function

import json
import os
import re
import socket
import time
import traceback
import typing as t
from operator import truth, attrgetter
from itertools import islice
from string import ascii_lowercase
from unicodedata import normalize
import beets
import beets.ui
import confuse
from beets import config
from beets.autotag.hooks import AlbumInfo, TrackInfo
from beets.plugins import BeetsPlugin, MetadataSourcePlugin, get_distance
from beetsplug.bandcamp._metaguru import Helpers
from pycountry import countries, subdivisions
from discogs_client import Client, Master, Release
from discogs_client.exceptions import DiscogsAPIError
from requests.exceptions import ConnectionError
from six.moves import http_client

console = None
try:
    from rich.console import Console

    console = Console(force_terminal=True, force_interactive=True)
except ModuleNotFoundError:
    pass

USER_AGENT = "beets/{0} +https://beets.io/".format(beets.__version__)
API_KEY = "rAzVUQYRaoFjeBjyWuWZ"
API_SECRET = "plxtUTqoCzwxZpqdPysCwGuBSmZNdZVy"

# Exceptions that discogs_client should really handle but does not.
CONNECTION_ERRORS = (
    ConnectionError,
    socket.error,
    http_client.HTTPException,
    ValueError,  # JSON decoding raises a ValueError.
    DiscogsAPIError,
)


class DiscogsPlugin(BeetsPlugin):
    def __init__(self):
        super().__init__()
        self.config.add({
            'apikey': API_KEY,
            'apisecret': API_SECRET,
            'tokenfile': 'discogs_token.json',
            'source_weight': 0.5,
            'user_token': '',
            'separator': ', ',
            'index_tracks': False,
            'append_style_genre': False,
        })
        self.config['apikey'].redact = True
        self.config['apisecret'].redact = True
        self.config['user_token'].redact = True
        self.discogs_client = None
        self.register_listener('import_begin', self.setup)

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

    def reset_auth(self):
        """Delete token file & redo the auth steps.
        """
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
            self._log.info("connection error: {0}", e)
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
            self._log.info("connection error: {0}", e)
            raise beets.ui.UserError("Discogs token request failed")

        # Save the token for later use.
        self._log.info("Discogs token {0}, secret {1}", token, secret)
        with open(self._tokenfile(), "w") as f:
            json.dump({"token": token, "secret": secret}, f)

        return token, secret

    def album_distance(self, items, album_info, mapping):
        """Returns the album distance."""
        return get_distance(data_source="discogs", info=album_info, config=self.config)

    def track_distance(self, item, track_info):
        """Returns the track distance."""
        return get_distance(data_source="discogs", info=track_info, config=self.config)

    def candidates(self, items, artist, album, va_likely, extra_tags=None):
        """Returns a list of AlbumInfo objects for discogs search results
        matching an album and artist (if not various).
        """
        if "bandcamp" in items[0].mb_albumid:
            return ()

        if not self.discogs_client:
            return ()

        if not album and not artist:
            self._log.debug('Skipping Discogs query. Files missing album and '
                            'artist tags.')
            return ()

        query = "%s %s" % (artist, album)
        albums = ()
        try:
            self._log.debug("Searching for '{}'", query)
            albums = self.get_albums(query)
        except DiscogsAPIError as e:
            self._log.info("API Error: {0} (query: {1})", e, query)
            if e.status_code == 401:
                self.reset_auth()
                albums = self.candidates(items, artist, album, va_likely)
        except CONNECTION_ERRORS:
            self._log.info("Connection error in album search", exc_info=True)

        albums = list(albums)
        self._log.debug("Found {} albums", len(albums))
        return albums

    @staticmethod
    def extract_release_id_regex(album_id):
        """Returns the Discogs_id or None."""
        # Discogs-IDs are simple integers. In order to avoid confusion with
        # other metadata plugins, we only look for very specific formats of the
        # input string:
        # - plain integer, optionally wrapped in brackets and prefixed by an
        #   'r', as this is how discogs displays the release ID on its webpage.
        # - legacy url format: discogs.com/<name of release>/release/<id>
        # - current url format: discogs.com/release/<id>-<name of release>
        # See #291, #4080 and #4085 for the discussions leading up to these
        # patterns.
        # Regex has been tested here https://regex101.com/r/wyLdB4/2

        for pattern in [
                r'^\[?r?(?P<id>\d+)\]?$',
                r'discogs\.com/release/(?P<id>\d+)-',
                r'discogs\.com/[^/]+/release/(?P<id>\d+)',
        ]:
            match = re.search(pattern, album_id)
            if match:
                return int(match.group('id'))

        return None

    def album_for_id(self, album_id):
        """Fetches an album by its Discogs ID and returns an AlbumInfo object
        or None if the album is not found.
        """
        if not self.discogs_client:
            return

        self._log.debug('Searching for release {0}', album_id)

        discogs_id = self.extract_release_id_regex(album_id)

        if not discogs_id:
            return None

        result = Release(self.discogs_client, {'id': discogs_id})
        # Try to obtain title to verify that we indeed have a valid Release
        try:
            getattr(result, "title")
        except DiscogsAPIError as e:
            if e.status_code != 404:
                self._log.info(
                    "API Error: {0} (query: {1})", e, result.data["resource_url"]
                )
                if e.status_code == 401:
                    self.reset_auth()
                    return self.album_for_id(album_id)
            return None
        except CONNECTION_ERRORS:
            self._log.debug('Connection error in album lookup',
                            exc_info=True)
            return None
        return self.get_album_info(result)

    def get_albums(self, query):
        """Returns a list of AlbumInfo objects for a discogs search query."""
        # Strip non-word characters from query. Things like "!" and "-" can cause a query
        # to return no results, even if they match the artist or album title.
        # Use `re.UNICODE` flag to avoid stripping non-english word characters.
        query = re.sub(r"(?u)\W+", " ", query, re.UNICODE)
        # Strip medium information from query, Things like "CD1" and "disk 1"
        # can also negate an otherwise positive result.
        query = re.sub(r"(?i)\b(CD|disc)\s*\d+", "", query)

        try:
            releases = self.discogs_client.search(query,
                                                  type='release').page(1)

        except CONNECTION_ERRORS:
            self._log.info(
                "Communication error while searching for {0!r}", query, exc_info=True
            )
            return ()
        return islice(filter(truth, map(self.get_album_info, releases)), 5)

    def get_master_year(self, master_id):
        """Fetches a master release given its Discogs ID and returns its year
        or None if the master release is not found.
        """
        self._log.info("Searching for master release {0}", master_id)
        result = Master(self.discogs_client, {"id": master_id})

        try:
            year = result.fetch('year')
            return year
        except DiscogsAPIError as e:
            self._log.info("API Error: {0} (query: {1})", e, result.data["resource_url"])
            # if e.status_code != 404:
            #     self._log.info(u'API Error: {0} (query: {1})', e,
            #                     result.data['resource_url'])
            #     if e.status_code == 401:
            #         self.reset_auth()
            return self.get_master_year(master_id)
            # return None
        except CONNECTION_ERRORS:
            self._log.info("Connection error in master release lookup", exc_info=True)
            return None

    def get_album_info(self, result):
        """Returns an AlbumInfo object for a discogs Release object."""
        # Explicitly reload the `Release` fields, as they might not be yet
        # present if the result is from a `discogs_client.search()`.
        if not result.data.get("artists"):
            result.refresh()
        # print(vars(result))

        # Sanity check for required fields. The list of required fields is
        # defined at Guideline 1.3.1.a, but in practice some releases might be
        # lacking some of these fields. This function expects at least:
        # `artists` (>0), `title`, `id`, `tracklist` (>0)
        # https://www.discogs.com/help/doc/submission-guidelines-general-rules
        if not all([result.data.get(k) for k in ["artists", "title", "id", "tracklist"]]):
            self._log.warning("Release does not contain the required fields")
            return None

        artist, artist_id = MetadataSourcePlugin.get_artist(
            [a.data for a in result.artists]
        )
        album = re.sub(r" +", " ", result.title)
        album_id = result.data["id"]
        # Use `.data` to access the tracklist directly instead of the
        # convenient `.tracklist` property, which will strip out useful artist
        # information and leave us with skeleton `Artist` objects that will
        # each make an API call just to get the same data back.
        tracks = self.get_tracks(result.data["tracklist"])

        # Extract information for the optional AlbumInfo fields, if possible.
        va = result.data["artists"][0].get("name", "").lower() == "various"
        year = result.data.get("year")
        mediums = [t.medium for t in tracks]
        country = (result.data.get("country") or "").replace("UK", "GB")
        if not re.match(r"[A-Z][A-Z]", country):
            COUNTRY_OVERRIDES = {
                "Russia": "RU",  # pycountry: Russian Federation
                "The Netherlands": "NL",  # pycountry: Netherlands
                "UK": "GB",  # pycountry: Great Britain
                "D.C.": "US",
                "South Korea": "KR",  # pycountry: Korea, Republic of
            }
            try:
                name = normalize("NFKD", country).encode("ascii", "ignore").decode()
                country = (
                    COUNTRY_OVERRIDES.get(name)
                    or getattr(countries.get(name=name, default=object), "alpha_2", None)
                    or subdivisions.lookup(name).country_code
                )
            except (ValueError, LookupError):
                country = "XW"

        data_url = result.data.get("uri")
        genre = self.format(result.data.get("styles"))
        style = self.format(result.data.get("genres"))
        if self.config['append_style_genre'] and style:
            genre = self.config['separator'].as_str().join([genre, style])
        discogs_albumid = self.extract_release_id_regex(result.data.get("uri"))

        # Extract information for the optional AlbumInfo fields that are
        # contained on nested discogs fields.
        albumtypes = set()
        albumtype = media = label = catalogno = labelid = None
        formats = result.data.get("formats") or []
        albumstatus = "Official"
        albumtype = "album"
        # print(f"{artist} - {album}")
        if formats:
            _format = formats[0]
            media = (_format.get("name") or "").replace("File", "Digital Media")
            for desc in set(_format.get("descriptions") or []):
                if desc == "Promo":
                    albumstatus = "Promotional"
                elif desc in {"Album", "EP"}:
                    albumtype = desc.lower()
                    albumtypes.add(albumtype)
                elif desc == "Compilation":
                    albumtype = "album"
                    albumtypes.add("compilation")
                    va = True
                elif albumtype == "Single":
                    albumtypes.add("single")
                    albumtype = "album"
                    titles = set(map(lambda x: x.get("main_title"), map(Helpers.parse_track_name, map(attrgetter("title"), tracks))))
                    if len(titles) < 2:
                        albumtype = "single"
        albumtypes.add(albumtype)
        if result.data.get("labels"):
            label = result.data["labels"][0].get("name")
            labelid = result.data["labels"][0].get("id")
            catalogno = result.data["labels"][0].get("catno")
            if catalogno == "none":
                catalogno = None
            elif catalogno:
                catalogno = catalogno.upper()

        # Explicitly set the `media` for the tracks, since it is expected by
        # `autotag.apply_metadata`, and set `medium_total`.
        # albumtype = tracks[0].disctitle
        for track in tracks:
            if not track.artist:
                track.artist = artist
            track.media = media
            track.medium_total = mediums.count(track.medium)
            # Discogs does not have track IDs. Invent our own IDs as proposed in #2336.
            track.track_id = str(album_id) + "-" + (track.track_alt or str(track.index))

        if va:
            artist = config["va_name"].as_str()

        # a master release, otherwise fetch the master release.
        original_year = year
        # original_year = self.get_master_year(master_id) if master_id else year
        released = (result.data.get("released") or "").split("-")
        artwork_url = result.data.get("cover_image")
        if len(tracks) == 1:
            t = tracks[0]
            albumtype = "single"
            albumtypes = [albumtype]
            album = artist + " - " + t.title
            album_id = t.track_id
        album_info = AlbumInfo(
            album=album,
            album_id=album_id,
            artist=artist,
            artist_id=artist_id,
            tracks=tracks,
            albumstatus=albumstatus,
            albumtype=albumtype,
            albumtypes="; ".join(sorted(albumtypes)),
            va=va,
            mediums=len(set(mediums)),
            releasegroup_id=result.data.get("master_id"),

            comments=result.data.get("notes") or None,
            year=int(released[0] if len(released[0]) else 0),
            month=int(released[1] if len(released) > 1 else 0),
            day=int(released[2] if len(released) > 2 else 0),
            label=re.sub(r" \([0-9]+\)", "", str(label)),
            catalognum=catalogno,
            country=country,
            style=style,
            genre=genre,
            media=media,
            original_year=original_year,
            data_source="discogs",
            data_url=data_url,
            discogs_albumid=discogs_albumid,
            discogs_labelid=labelid,
            discogs_artistid=artist_id,
        )
        if artwork_url:
            album_info.artpath = artwork_url
        return album_info

    def format(self, classification: str) -> t.Optional[str]:
        if classification:
            return self.config["separator"].as_str().join(sorted(classification))
        else:
            return None

    def get_tracks(self, tracklist):
        """Returns a list of TrackInfo objects for a discogs tracklist."""
        try:
            clean_tracklist = self.coalesce_tracks(tracklist)
        except Exception as exc:
            # FIXME: this is an extra precaution for making sure there are no
            # side effects after #2222. It should be removed after further
            # testing.
            self._log.info("{}", traceback.format_exc())
            self._log.error("uncaught exception in coalesce_tracks: {}", exc)
            clean_tracklist = tracklist
        tracks = []
        index_tracks = {}
        index = 0
        # Distinct works and intra-work divisions, as defined by index tracks.
        divisions, next_divisions = [], []
        for track in clean_tracklist:
            # Only real tracks have `position`. Otherwise, it's an index track.
            if track["position"]:
                index += 1
                if next_divisions:
                    # End of a block of index tracks: update the current
                    # divisions.
                    divisions += next_divisions
                    del next_divisions[:]
                track_info = self.get_track_info(track, index, divisions)
                pos = track.get("position")
                if pos and not pos.isnumeric():
                    track_info.track_alt = pos
                tracks.append(track_info)
            else:
                next_divisions.append(track["title"])
                # We expect new levels of division at the beginning of the
                # tracklist (and possibly elsewhere).
                try:
                    divisions.pop()
                except IndexError:
                    pass
                index_tracks[index + 1] = track["title"]

        # Fix up medium and medium_index for each track. Discogs position is
        # unreliable, but tracks are in order.
        medium = None
        medium_count, index_count, side_count = 0, 0, 0
        sides_per_medium = 1

        # If a medium has two sides (ie. vinyl or cassette), each pair of
        # consecutive sides should belong to the same medium.
        if all([track.medium is not None for track in tracks]):
            m = sorted({track.medium.lower() for track in tracks})
            # If all track.medium are single consecutive letters, assume it is
            # a 2-sided medium.
            if "".join(m) in ascii_lowercase:
                sides_per_medium = 2

        for track in tracks:
            # Handle special case where a different medium does not indicate a
            # new disc, when there is no medium_index and the ordinal of medium
            # is not sequential. For example, I, II, III, IV, V. Assume these
            # are the track index, not the medium.
            # side_count is the number of mediums or medium sides (in the case
            # of two-sided mediums) that were seen before.
            medium_is_index = (
                track.medium
                and not track.medium_index
                and (
                    len(track.medium) != 1
                    or
                    # Not within standard incremental medium values (A, B, C, ...).
                    ord(track.medium) - 64 != side_count + 1
                )
            )

            if not medium_is_index and medium != track.medium:
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
                medium = track.medium

            index_count += 1
            medium_count = 1 if medium_count == 0 else medium_count
            track.medium, track.medium_index = medium_count, index_count
            track.data_source = "discogs"

        return tracks

    def coalesce_tracks(self, raw_tracklist):
        """Pre-process a tracklist, merging subtracks into a single track. The
        title for the merged track is the one from the previous index track,
        if present; otherwise it is a combination of the subtracks titles.
        """

        def add_merged_subtracks(tracklist, subtracks):
            """Modify `tracklist` in place, merging a list of `subtracks` into
            a single track into `tracklist`."""
            # Calculate position based on first subtrack, without subindex.
            idx, medium_idx, sub_idx = self.get_track_index(subtracks[0]["position"])
            position = "%s%s" % (idx or "", medium_idx or "")

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
                            subtrack["title"] = "{}: {}".format(
                                index_track["title"], subtrack["title"]
                            )
                    tracklist.extend(subtracks)
            else:
                # Merge the subtracks, pick a title, and append the new track.
                track = subtracks[0].copy()
                track["title"] = " / ".join([t["title"] for t in subtracks])
                tracklist.append(track)

        # Pre-process the tracklist, trying to identify subtracks.
        subtracks = []
        tracklist = []
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
                        add_merged_subtracks(tracklist, subtracks)
                        subtracks = [track]
                    prev_subindex = subindex.rjust(len(raw_tracklist))
                    continue

            # Index track with nested sub_tracks.
            if not track["position"] and "sub_tracks" in track:
                # Append the index track, assuming it contains the track title.
                tracklist.append(track)
                add_merged_subtracks(tracklist, track["sub_tracks"])
                continue

            # Regular track or index track without nested sub_tracks.
            if subtracks:
                add_merged_subtracks(tracklist, subtracks)
                subtracks = []
                prev_subindex = ""
            tracklist.append(track)

        # Merge and add the remaining subtracks, if any.
        if subtracks:
            add_merged_subtracks(tracklist, subtracks)

        return tracklist

    def get_track_info(self, track, index, divisions):
        """Returns a TrackInfo object for a discogs track."""
        title = track["title"]
        if self.config["index_tracks"]:
            prefix = ", ".join(divisions)
            if prefix:
                title = "{}: {}".format(prefix, title)
        track_id = None
        medium, medium_index, _ = self.get_track_index(track["position"])
        artist, artist_id = MetadataSourcePlugin.get_artist(track.get("artists", []))
        length = self.get_track_length(track["duration"])
        return TrackInfo(
            title=title,
            track_id=track_id,
            artist=artist,
            artist_id=artist_id,
            length=length,
            index=index,
            medium=medium,
            medium_index=medium_index,
        )

    def get_track_index(self, position: str) -> t.Iterable[t.Optional[str]]:
        """Returns the medium, medium index and subtrack index for a discogs
        track position."""
        # Match the standard Discogs positions (12.2.9), which can have several
        # forms (1, 1-1, A1, A1.1, A1a, ...).
        match = re.match(
            r"^(?P<medium>.*?)"  # medium: everything before medium_index.
            r"(?P<medium_index>\d*?)"  # medium_index: a number at the end of
            # `position`, except if followed by a subtrack index.
            # subtrack_index: can only be matched if medium
            # or medium_index have been matched, and can be
            r"(?P<subindex>(?:(?<=\w)\.)[\w]+"  # - a dot followed by a string (A.1, 2.A)
            r"|(?<=\d)[A-Z]+"  # - a string that follows a number (1A, B2a)
            r")?$",
            position.upper(),
        )

        # medium = index = subindex = None
        if match:
            return match.groups()
        self._log.info("Invalid position: {0}", position)
        return None, None, None

    def get_track_length(self, duration: str) -> t.Optional[int]:
        """Returns the track length in seconds for a discogs duration."""
        try:
            length = time.strptime(duration, "%M:%S")
        except ValueError:
            return None
        return length.tm_min * 60 + length.tm_sec
