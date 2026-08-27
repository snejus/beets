"""Describe the SoundCloud response fields consumed by the plugin."""

from __future__ import annotations

from typing import TypedDict


class SoundCloudUser(TypedDict, total=False):
    urn: str
    username: str


class SoundCloudTrack(TypedDict, total=False):
    artwork_url: str | None
    bpm: float | None
    duration: int
    genre: str | None
    isrc: str | None
    key_signature: str | None
    kind: str
    label_name: str | None
    metadata_artist: str | None
    permalink_url: str
    release: str | None
    release_day: int | None
    release_month: int | None
    release_year: int | None
    title: str
    urn: str
    user: SoundCloudUser


class SoundCloudPlaylist(TypedDict, total=False):
    artwork_url: str | None
    ean: str | None
    genre: str | None
    kind: str
    label_name: str | None
    permalink_url: str
    playlist_type: str
    release: str | None
    release_day: int | None
    release_month: int | None
    release_year: int | None
    title: str
    track_count: int
    tracks: list[SoundCloudTrack]
    urn: str
    user: SoundCloudUser


SoundCloudResource = SoundCloudTrack | SoundCloudPlaylist


class SoundCloudCollection(TypedDict, total=False):
    collection: list[SoundCloudResource]
    next_href: str | None


class SoundCloudToken(TypedDict, total=False):
    access_token: str
    expires_at: float
    expires_in: int
    refresh_token: str
    scope: str
    token_type: str
