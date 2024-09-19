from __future__ import annotations

from typing import Any

from typing_extensions import TypeAlias, TypedDict

JSONDict: TypeAlias = "dict[str, Any]"


class LRCLibItem(TypedDict):
    """Definition of a single lyrics JSON object returned by the LRCLib API."""

    id: int
    name: str
    trackName: str
    artistName: str
    albumName: str
    duration: float
    instrumental: bool
    plainLyrics: str
    syncedLyrics: str | None


class GeniusDateComponents(TypedDict):
    year: int
    month: int
    day: int


class GeniusArtist(TypedDict):
    api_path: str
    header_image_url: str
    id: int
    image_url: str
    is_meme_verified: bool
    is_verified: bool
    name: str
    url: str


class GeniusStats(TypedDict):
    unreviewed_annotations: int
    hot: bool


class GeniusSearchResult(TypedDict):
    annotation_count: int
    api_path: str
    artist_names: str
    full_title: str
    header_image_thumbnail_url: str
    header_image_url: str
    id: int
    lyrics_owner_id: int
    lyrics_state: str
    path: str
    primary_artist_names: str
    pyongs_count: int | None
    relationships_index_url: str
    release_date_components: GeniusDateComponents
    release_date_for_display: str
    release_date_with_abbreviated_month_for_display: str
    song_art_image_thumbnail_url: str
    song_art_image_url: str
    stats: GeniusStats
    title: str
    title_with_featured: str
    url: str
    featured_artists: list[GeniusArtist]
    primary_artist: GeniusArtist
    primary_artists: list[GeniusArtist]
