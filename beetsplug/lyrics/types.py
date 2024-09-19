from __future__ import annotations

from typing_extensions import TypedDict


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
