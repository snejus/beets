"""Add public SoundCloud track and release matches to the autotagger."""

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING, ClassVar

import confuse

from beets import config
from beets.autotag import AlbumInfo, TrackInfo
from beets.dbcore import types
from beets.metadata_plugins import IDResponse, SearchApiMetadataSourcePlugin

from .api import SoundCloudAPI

if TYPE_CHECKING:
    from collections.abc import Sequence

    from beets.library import Item
    from beets.metadata_plugins import QueryType, SearchParams

    from .api_types import (
        SoundCloudPlaylist,
        SoundCloudResource,
        SoundCloudTrack,
    )


class SoundCloudSearchResult(IDResponse):
    pass


class SoundCloudPlugin(SearchApiMetadataSourcePlugin[SoundCloudSearchResult]):
    item_types: ClassVar[dict[str, types.Type]] = {
        "soundcloud_track_urn": types.STRING,
        "soundcloud_artist_urn": types.STRING,
    }
    album_types: ClassVar[dict[str, types.Type]] = {
        "soundcloud_playlist_urn": types.STRING,
        "soundcloud_artist_urn": types.STRING,
    }

    def __init__(self) -> None:
        super().__init__()
        self.config.add(
            {
                "client_id": "",
                "client_secret": "",
                "tokenfile": "soundcloud_token.json",
            }
        )
        self.config["client_id"].redact = True
        self.config["client_secret"].redact = True

    def _tokenfile(self) -> str:
        return self.config["tokenfile"].get(confuse.Filename(in_app_dir=True))

    @cached_property
    def api(self) -> SoundCloudAPI:
        return SoundCloudAPI(
            self.config["client_id"].as_str(),
            self.config["client_secret"].as_str(),
            self._tokenfile(),
        )

    def get_search_query_with_filters(
        self,
        query_type: QueryType,
        _items: Sequence[Item],
        artist: str,
        name: str,
        va_likely: bool,
    ) -> tuple[str, dict[str, str]]:
        if query_type == "album" and va_likely:
            return name, {}
        return " ".join(filter(None, (artist, name))), {}

    def get_search_response(
        self, params: SearchParams
    ) -> list[SoundCloudSearchResult]:
        resources: Sequence[SoundCloudResource]
        if params.query_type == "track":
            resources = self.api.search_tracks(params.query, params.limit)
        else:
            resources = self.api.search_playlists(
                params.query, params.limit, playlist_type="album"
            )
        return [
            SoundCloudSearchResult(id=resource["urn"])
            for resource in resources
            if resource.get("urn")
        ]

    @staticmethod
    def _genres(
        resource: SoundCloudTrack | SoundCloudPlaylist,
    ) -> list[str] | None:
        genre = resource.get("genre")
        return [genre] if isinstance(genre, str) and genre else None

    @staticmethod
    def _artist(
        track: SoundCloudTrack,
    ) -> tuple[str | None, str | None, str | None]:
        user = track.get("user") or {}
        credit = track.get("metadata_artist")
        uploader_urn = user.get("urn")
        if credit:
            return credit, None, uploader_urn
        return user.get("username"), uploader_urn, uploader_urn

    def _track_info(
        self,
        track: SoundCloudTrack,
        *,
        index: int | None = None,
        total: int | None = None,
        album: str | None = None,
    ) -> TrackInfo:
        artist, artist_id, artist_urn = self._artist(track)
        urn = track["urn"]
        duration = track.get("duration")
        bpm = track.get("bpm")
        return TrackInfo(
            title=track.get("title"),
            track_id=urn,
            soundcloud_track_urn=urn,
            artist=artist,
            artist_id=artist_id,
            soundcloud_artist_urn=artist_urn,
            album=album or track.get("release"),
            length=duration / 1000 if duration is not None else None,
            index=index,
            medium=1 if index is not None else None,
            medium_index=index,
            medium_total=total,
            year=track.get("release_year"),
            month=track.get("release_month"),
            day=track.get("release_day"),
            genres=self._genres(track),
            label=track.get("label_name"),
            isrc=track.get("isrc"),
            bpm=str(bpm) if bpm is not None else None,
            initial_key=track.get("key_signature"),
            cover_art_url=track.get("artwork_url"),
            data_source=self.data_source,
            data_url=track.get("permalink_url"),
            media="Digital Media",
        )

    def track_for_id(self, track_id: str) -> TrackInfo | None:
        if track := self.api.get_track(track_id):
            return self._track_info(track)
        return None

    def _album_info(self, playlist: SoundCloudPlaylist) -> AlbumInfo:
        track_data = playlist.get("tracks") or []
        total = len(track_data)
        tracks: list[TrackInfo] = []
        artist_credits: dict[str, tuple[str, set[str]]] = {}
        for index, raw_track in enumerate(track_data, start=1):
            track = self._track_info(
                raw_track, index=index, total=total, album=playlist.get("title")
            )
            tracks.append(track)
            if track.artist:
                _, artist_ids = artist_credits.setdefault(
                    track.artist.casefold(), (track.artist, set())
                )
                if track.artist_id:
                    artist_ids.add(track.artist_id)

        user = playlist.get("user") or {}
        uploader_urn = user.get("urn")
        va = len(artist_credits) > 1
        artist: str | None
        artist_id: str | None
        if va:
            artist = config["va_name"].as_str()
            artist_id = None
        elif artist_credits:
            artist, artist_ids = next(iter(artist_credits.values()))
            artist_id = next(iter(artist_ids)) if len(artist_ids) == 1 else None
        else:
            artist = user.get("username")
            artist_id = uploader_urn

        urn = playlist["urn"]
        playlist_type = playlist.get("playlist_type") or "playlist"
        return AlbumInfo(
            album=playlist.get("title"),
            album_id=urn,
            soundcloud_playlist_urn=urn,
            artist=artist,
            artist_id=artist_id,
            soundcloud_artist_urn=uploader_urn,
            tracks=tracks,
            va=va,
            albumtype=playlist_type,
            albumtypes=[playlist_type],
            barcode=playlist.get("ean"),
            year=playlist.get("release_year"),
            month=playlist.get("release_month"),
            day=playlist.get("release_day"),
            genres=self._genres(playlist),
            label=playlist.get("label_name"),
            mediums=1,
            cover_art_url=playlist.get("artwork_url"),
            data_source=self.data_source,
            data_url=playlist.get("permalink_url"),
            media="Digital Media",
        )

    def album_for_id(self, album_id: str) -> AlbumInfo | None:
        if playlist := self.api.get_playlist(album_id):
            return self._album_info(playlist)
        return None
