"""Adds Deezer release and track search support to the autotagger"""

from __future__ import annotations

import collections
import time
from typing import TYPE_CHECKING, ClassVar, Literal, TypedDict

import requests
from typing_extensions import NotRequired

from beets import config, ui
from beets.autotag import AlbumInfo, TrackInfo
from beets.dbcore import types
from beets.metadata_plugins import (
    IDResponse,
    SearchApiMetadataSourcePlugin,
    SearchParams,
)

VARIOUS_ARTISTS_ID = 5080

if TYPE_CHECKING:
    import optparse
    from collections.abc import Iterator, Sequence

    from beets.library import Item, Library
    from beets.metadata_plugins import QueryType


class Artist(TypedDict):
    """Artist object returned by the Deezer API."""

    id: int
    type: Literal["artist"]
    name: str
    link: str
    picture_small: str
    picture_medium: str
    picture_big: str
    picture_xl: str
    tracklist: str


class Contributor(Artist):
    share: str
    radio: bool
    role: Literal[
        "Main", "Guest", "Composer", "Lyricist", "Producer", "Remixer", "Other"
    ]


class SearchTrack(IDResponse):
    type: Literal["track"]
    readable: bool
    title: str
    title_short: str
    title_version: str
    link: str
    duration: int
    rank: int
    explicit_lyrics: bool
    explicit_content_lyrics: int
    explicit_content_cover: int
    preview: str
    md5_image: str
    artist: Artist
    album: TrackAlbum


class Track(SearchTrack):
    isrc: str
    share: str
    track_position: int
    disk_number: int
    release_date: str
    bpm: int
    gain: int
    available_countries: list[str]
    contributors: NotRequired[list[Contributor]]
    track_token: str


class TrackAlbum(IDResponse):
    """Artist object returned by the Deezer API."""

    type: Literal["album"]
    title: str
    cover: str
    cover_small: str
    cover_medium: str
    cover_big: str
    cover_xl: str
    md5_image: str
    tracklist: str


class SearchAlbum(TrackAlbum):
    """Album object returned by the Deezer Search API."""

    genre_id: int
    nb_tracks: int
    record_type: Literal["album"]
    explicit_lyrics: bool
    artist: Artist


class Genre(TypedDict):
    id: int
    type: Literal["genre"]
    name: str
    picture: str


class Album(SearchAlbum):
    upc: str
    share: str
    genres: dict[Literal["data"], list[Genre]]
    label: str
    link: str
    duration: int
    fans: int
    release_date: str
    available: bool
    explicit_content_lyrics: int
    explicit_content_cover: int
    contributors: list[Contributor]
    tracks: dict[Literal["data"], Sequence[SearchTrack]]


class DeezerPlugin(SearchApiMetadataSourcePlugin[SearchTrack | SearchAlbum]):
    item_types: ClassVar[dict[str, types.Type]] = {
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

    def commands(self) -> list[ui.Subcommand]:
        """Add beet UI commands to interact with Deezer."""
        deezer_update_cmd = ui.Subcommand(
            "deezerupdate", help=f"Update {self.data_source} rank"
        )

        def func(lib: Library, opts: optparse.Values, args: list[str]) -> None:
            items = lib.items(args)
            self.deezerupdate(list(items), ui.should_write())

        deezer_update_cmd.func = func

        return [deezer_update_cmd]

    def album_for_id(self, album_id: str) -> AlbumInfo | None:
        """Fetch an album by its Deezer ID or URL."""
        if not (deezer_id := self._extract_id(album_id)):
            return None

        album_url = f"{self.album_url}{deezer_id}"
        album_data: Album | None
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
        style = ", ".join(g.get("name") or "" for g in genres)
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

        is_va = str(album_data["artist"]["id"]) == str(VARIOUS_ARTISTS_ID)
        if is_va:
            va_name = config["va_name"].as_str()
            artist = va_name

        return AlbumInfo(
            tracks,
            album=album,
            albumtype=albumtype,
            artist=artist,
            artists=[artist],
            artist_id=str(artist_id),
            albumstatus="Official",
            album_id=deezer_id,
            artist_credit=(
                artist if is_va else self.get_artist([album_data["artist"]])[0]
            ),
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

    def track_for_id(self, track_id: str) -> TrackInfo | None:
        """Fetch a track by its Deezer ID or URL and return a
        TrackInfo object or None if the track is not found.

        :param track_id: (Optional) Deezer ID or URL for the track. Either
            ``track_id`` or ``track_data`` must be provided.

        """
        if not (deezer_id := self._extract_id(track_id)):
            self._log.debug("Invalid Deezer track_id: {}", track_id)
            return None

        track_data: Track
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
            if track_data.get("disk_number") == track.medium:
                medium_total += 1
                if track_data["id"] == track.track_id:
                    track.index = i
        track.medium_total = medium_total
        return track

    def _get_track(self, track_data: Track, total: int = 0) -> TrackInfo:
        """Convert a Deezer track object dict to a TrackInfo object.

        :param track_data: Deezer Track object dict
        """
        contributors = track_data.get("contributors")
        if contributors is None and (artist_data := track_data["artist"]):
            contributors = [artist_data]
        if contributors is not None:
            artist, artist_id = self.get_artist(contributors)
        else:
            artist, artist_id = None, None
        position = track_data.get("track_position")
        return TrackInfo(
            title=track_data["title"],
            track_id=str(track_data["id"]),
            deezer_track_id=track_data["id"],
            isrc=track_data.get("isrc"),
            artist=artist,
            artist_id=str(artist_id) if artist_id is not None else None,
            length=track_data["duration"],
            index=position,
            medium=track_data.get("disk_number"),
            deezer_track_rank=track_data.get("rank"),
            medium_index=position,
            data_source=self.data_source,
            data_url=track_data["link"],
            deezer_updated=time.time(),
        )

    def get_search_query_with_filters(
        self,
        query_type: QueryType,
        items: Sequence[Item],
        artist: str,
        name: str,
        va_likely: bool,
    ) -> tuple[str, dict[str, str]]:
        if query_type == "album":
            query = f'album:"{name}"'
            if not va_likely:
                query += f' artist:"{artist}"'
        else:
            # Deezer drops unquoted free text as soon as the query carries any
            # field:"value" filter, so `<title> artist:"<artist>"` degenerated
            # into "every track by this artist", truncated to `search_limit`.
            # The wanted track routinely fell outside that window. Filtering on
            # the title instead is no better, because `artist:` is fuzzy enough
            # to match unrelated artists ("Pan Da Punk" for "Daft Punk"), so the
            # two filters can intersect to nothing even for a well-tagged file.
            # Plain free text lets Deezer's own relevance ranking do the work.
            query = f"{name} {artist}".strip()

        return query, {query_type: name} if name else {}

    def get_search_response(
        self, params: SearchParams
    ) -> list[SearchTrack | SearchAlbum]:
        """Search Deezer and return the raw result payload entries."""

        response = requests.get(
            f"{self.search_url}{params.query_type}",
            params={
                **params.filters,
                "q": params.query,
                "limit": str(params.limit),
            },
            timeout=10,
        )
        response.raise_for_status()
        return response.json()["data"]

    def albums_from_tracks_query(self, query: str) -> Iterator[AlbumInfo]:
        search_params = SearchParams(
            query_type="track", query=query, filters={}, limit=self.search_limit
        )
        for track in self.get_search_response(search_params):
            if album := self.album_for_id(track["album"]["id"]):
                yield album

    def candidates(
        self, items: Sequence[Item], artist: str, album: str, va_likely: bool
    ) -> Iterator[AlbumInfo]:
        first_item = items[0]
        track_query = f"{first_item.artist} - {first_item.title}"
        yield from self.albums_from_tracks_query(track_query)

        results = self._get_candidates("album", items, artist, album, va_likely)
        yield from filter(
            None, self.albums_for_ids(str(r["id"]) for r in results)
        )

    def deezerupdate(self, items: Sequence[Item], write: bool) -> None:
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
                track = self.fetch_data(f"{self.track_url}{deezer_track_id}")
            except Exception as e:
                self._log.debug("Invalid Deezer track_id: {}", e)
                continue
            else:
                if track and (rank := track.get("rank") is not None):
                    self._log.debug(
                        "Deezer track: {} has {} rank", deezer_track_id, rank
                    )
                    item.deezer_track_rank = int(rank)
                    item.store()
                    item.deezer_updated = time.time()
                    if write:
                        item.try_write()

    def fetch_data(self, url: str) -> JSONDict | None:
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
