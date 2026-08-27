"""Tests for the SoundCloud metadata source."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import pytest

from beets import config
from beets.library import Item
from beetsplug.soundcloud import SoundCloudPlugin
from beetsplug.soundcloud.api import SoundCloudAPI, SoundCloudAPIError

if TYPE_CHECKING:
    from pathlib import Path

API_URL = "https://api.soundcloud.com"
AUTH_URL = "https://secure.soundcloud.com/oauth/token"
DEFAULT_BEARER = "access-token"
DEFAULT_RENEWAL = "refresh-token"
NEW_BEARER = "new-access"
REFRESHED_BEARER = "refreshed-access"
REPLACEMENT_RENEWAL = "replacement-refresh"
TRACK_DURATION_SECONDS = 123.0
TRACK_BPM = 128.0


def _write_token(
    path: Path,
    *,
    access_token: str = DEFAULT_BEARER,
    refresh_token: str = DEFAULT_RENEWAL,
    expires_at: float = 4_102_444_800,
) -> None:
    path.write_text(
        json.dumps(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": expires_at,
            }
        )
    )


def _user(
    username: str = "Uploader", urn: str = "soundcloud:users:10"
) -> dict[str, Any]:
    return {"username": username, "urn": urn}


def _track(
    urn: str = "soundcloud:tracks:1",
    *,
    title: str = "Track",
    artist: str | None = "Artist",
    user: dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "kind": "track",
        "urn": urn,
        "title": title,
        "duration": 123_000,
        "metadata_artist": artist,
        "permalink_url": "https://soundcloud.com/u/track",
        "user": user or _user(),
        **fields,
    }


def _playlist(
    urn: str = "soundcloud:playlists:2",
    *,
    playlist_type: str = "album",
    tracks: list[dict[str, Any]] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    tracks = tracks if tracks is not None else [_track()]
    return {
        "kind": "playlist",
        "urn": urn,
        "title": "Release",
        "playlist_type": playlist_type,
        "permalink_url": "https://soundcloud.com/u/sets/release",
        "track_count": len(tracks),
        "tracks": tracks,
        "user": _user(),
        **fields,
    }


@pytest.fixture
def token_path(tmp_path: Path) -> Path:
    return tmp_path / "soundcloud_token.json"


@pytest.fixture
def api(token_path: Path) -> SoundCloudAPI:
    _write_token(token_path)
    return SoundCloudAPI("client-id", "client-secret", token_path)


@pytest.fixture
def plugin(token_path: Path) -> SoundCloudPlugin:
    _write_token(token_path)
    plugin = SoundCloudPlugin()
    plugin.config["client_id"].set("client-id")
    plugin.config["client_secret"].set("client-secret")
    plugin.config["tokenfile"].set(str(token_path))
    return plugin


class TestAuthentication:
    def test_client_credentials_are_cached(
        self, token_path, requests_mock
    ) -> None:
        requests_mock.post(
            AUTH_URL,
            json={
                "access_token": NEW_BEARER,
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            },
        )
        requests_mock.get(f"{API_URL}/tracks", json={"collection": []})
        api = SoundCloudAPI("client-id", "client-secret", token_path)

        assert api.search_tracks("Title", 5) == []

        token_request = requests_mock.request_history[0]
        assert token_request.headers["Authorization"].startswith("Basic ")
        assert parse_qs(token_request.text) == {
            "grant_type": ["client_credentials"]
        }
        assert json.loads(token_path.read_text())["access_token"] == NEW_BEARER

    def test_expired_token_is_refreshed(
        self, token_path, requests_mock
    ) -> None:
        _write_token(token_path, expires_at=0)
        requests_mock.post(
            AUTH_URL,
            json={
                "access_token": REFRESHED_BEARER,
                "refresh_token": REPLACEMENT_RENEWAL,
                "expires_in": 3600,
            },
        )
        requests_mock.get(f"{API_URL}/tracks", json={"collection": []})
        api = SoundCloudAPI("client-id", "client-secret", token_path)

        api.search_tracks("Title", 5)

        token_request = requests_mock.request_history[0]
        assert parse_qs(token_request.text) == {
            "client_id": ["client-id"],
            "client_secret": ["client-secret"],
            "grant_type": ["refresh_token"],
            "refresh_token": ["refresh-token"],
        }
        saved = json.loads(token_path.read_text())
        assert saved["access_token"] == REFRESHED_BEARER
        assert saved["refresh_token"] == REPLACEMENT_RENEWAL

    def test_unauthorized_request_refreshes_once(
        self, api, requests_mock
    ) -> None:
        requests_mock.get(
            f"{API_URL}/tracks",
            [
                {"status_code": 401},
                {"json": {"collection": [_track()]}, "status_code": 200},
            ],
        )
        requests_mock.post(
            AUTH_URL,
            json={
                "access_token": REFRESHED_BEARER,
                "refresh_token": REPLACEMENT_RENEWAL,
                "expires_in": 3600,
            },
        )

        assert len(api.search_tracks("Title", 5)) == 1

        track_requests = [
            request
            for request in requests_mock.request_history
            if request.path == "/tracks"
        ]
        assert track_requests[0].headers["Authorization"] == (
            f"OAuth {DEFAULT_BEARER}"
        )
        assert track_requests[1].headers["Authorization"] == (
            f"OAuth {REFRESHED_BEARER}"
        )

    def test_second_client_reuses_token_refreshed_by_first(
        self, token_path, requests_mock
    ) -> None:
        _write_token(token_path)
        first_api = SoundCloudAPI("client-id", "client-secret", token_path)
        second_api = SoundCloudAPI("client-id", "client-secret", token_path)
        requests_mock.get(
            f"{API_URL}/tracks",
            [
                {"status_code": 401},
                {"json": {"collection": []}, "status_code": 200},
                {"status_code": 401},
                {"json": {"collection": []}, "status_code": 200},
            ],
        )
        requests_mock.post(
            AUTH_URL,
            json={
                "access_token": REFRESHED_BEARER,
                "refresh_token": REPLACEMENT_RENEWAL,
                "expires_in": 3600,
            },
        )

        first_api.search_tracks("Title", 5)
        second_api.search_tracks("Title", 5)

        token_requests = [
            request
            for request in requests_mock.request_history
            if request.method == "POST"
        ]
        assert len(token_requests) == 1

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            {"access_token": "access", "expires_in": 3600},
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": "one hour",
            },
        ],
    )
    def test_malformed_token_response_fails_clearly(
        self, token_path, requests_mock, payload
    ) -> None:
        requests_mock.post(AUTH_URL, json=payload)
        api = SoundCloudAPI("client-id", "client-secret", token_path)

        with pytest.raises(SoundCloudAPIError, match="authentication response"):
            api.search_tracks("Title", 5)

    def test_missing_credentials_fail_before_request(self, token_path) -> None:
        api = SoundCloudAPI("", "", token_path)

        with pytest.raises(
            SoundCloudAPIError, match="client_id and client_secret"
        ):
            api.search_tracks("Title", 5)


class TestAPIRequests:
    def test_search_follows_linked_pagination(self, api, requests_mock) -> None:
        next_url = f"{API_URL}/tracks?cursor=next"
        requests_mock.get(
            f"{API_URL}/tracks",
            json={
                "collection": [_track("soundcloud:tracks:1")],
                "next_href": next_url,
            },
        )
        requests_mock.get(
            next_url, json={"collection": [_track("soundcloud:tracks:2")]}
        )

        tracks = api.search_tracks("Artist Title", 2)

        assert [track["urn"] for track in tracks] == [
            "soundcloud:tracks:1",
            "soundcloud:tracks:2",
        ]
        first_request = requests_mock.request_history[0]
        assert first_request.qs == {
            "linked_partitioning": ["true"],
            "limit": ["2"],
            "q": ["artist title"],
        }

    def test_rate_limit_error_includes_retry_delay(
        self, api, requests_mock
    ) -> None:
        requests_mock.get(
            f"{API_URL}/tracks", status_code=429, headers={"Retry-After": "12"}
        )

        with pytest.raises(SoundCloudAPIError, match=r"rate limit.*12"):
            api.search_tracks("Title", 5)

    def test_playlist_fetches_all_track_pages(self, api, requests_mock) -> None:
        urn = "soundcloud:playlists:2"
        tracks_url = f"{API_URL}/playlists/{urn}/tracks"
        next_url = f"{tracks_url}?cursor=next"
        requests_mock.get(
            f"{API_URL}/playlists/{urn}",
            json=_playlist(tracks=[], track_count=2),
        )
        requests_mock.get(
            tracks_url,
            json={
                "collection": [_track("soundcloud:tracks:1")],
                "next_href": next_url,
            },
        )
        requests_mock.get(
            next_url, json={"collection": [_track("soundcloud:tracks:2")]}
        )

        playlist = api.get_playlist(urn)

        assert playlist is not None
        assert [track["urn"] for track in playlist["tracks"]] == [
            "soundcloud:tracks:1",
            "soundcloud:tracks:2",
        ]
        tracks_request = next(
            request
            for request in requests_mock.request_history
            if request.path.endswith("/tracks")
        )
        assert tracks_request.qs == {"linked_partitioning": ["true"]}

    def test_not_found_returns_none(self, api, requests_mock) -> None:
        requests_mock.get(
            f"{API_URL}/tracks/soundcloud:tracks:404", status_code=404
        )

        assert api.get_track("soundcloud:tracks:404") is None

    def test_malformed_resource_fails_clearly(self, api, requests_mock) -> None:
        requests_mock.get(
            f"{API_URL}/tracks/soundcloud:tracks:1",
            json={"kind": "track", "title": "Missing URN"},
        )

        with pytest.raises(SoundCloudAPIError, match=r"missing.*urn"):
            api.get_track("soundcloud:tracks:1")


class TestTrackLookup:
    def test_maps_stable_track_metadata(self, plugin, requests_mock) -> None:
        requests_mock.get(
            f"{API_URL}/tracks/soundcloud:tracks:1",
            json=_track(
                artist="Credited Artist",
                bpm=TRACK_BPM,
                genre="House",
                isrc="GB-SC0-24-00001",
                key_signature="C#m",
                label_name="Label",
                release_day=3,
                release_month=2,
                release_year=2024,
            ),
        )

        info = plugin.track_for_id("soundcloud:tracks:1")

        assert info is not None
        assert info.title == "Track"
        assert info.artist == "Credited Artist"
        assert info.artist_id is None
        assert info.length == TRACK_DURATION_SECONDS
        assert info.bpm == str(TRACK_BPM)
        assert info.initial_key == "C#m"
        assert info.genres == ["House"]
        assert info.isrc == "GB-SC0-24-00001"
        assert info.label == "Label"
        assert (info.year, info.month, info.day) == (2024, 2, 3)
        assert info.soundcloud_track_urn == "soundcloud:tracks:1"
        assert info.soundcloud_artist_urn == "soundcloud:users:10"

    def test_falls_back_to_uploader_for_missing_credit(
        self, plugin, requests_mock
    ) -> None:
        requests_mock.get(
            f"{API_URL}/tracks/soundcloud:tracks:1",
            json=_track(artist=None, user=_user("Fallback Artist")),
        )

        info = plugin.track_for_id("soundcloud:tracks:1")

        assert info is not None
        assert info.artist == "Fallback Artist"
        assert info.artist_id == "soundcloud:users:10"

    def test_url_resolves_only_track_resources(
        self, plugin, requests_mock
    ) -> None:
        url = "https://soundcloud.com/u/track"
        requests_mock.get(
            f"{API_URL}/resolve", json=_playlist(playlist_type="playlist")
        )

        assert plugin.track_for_id(url) is None


class TestAlbumLookup:
    def test_search_excludes_ordinary_playlists(
        self, plugin, requests_mock
    ) -> None:
        album = _playlist("soundcloud:playlists:2")
        ordinary = _playlist("soundcloud:playlists:3", playlist_type="playlist")
        next_url = f"{API_URL}/playlists?cursor=next"
        plugin.config["search_limit"].set(1)
        requests_mock.get(
            f"{API_URL}/playlists",
            json={"collection": [ordinary], "next_href": next_url},
        )
        requests_mock.get(next_url, json={"collection": [album]})
        requests_mock.get(
            f"{API_URL}/playlists/soundcloud:playlists:2", json=album
        )

        candidates = list(
            plugin.candidates([Item()], "Artist", "Release", False)
        )

        assert [candidate.album_id for candidate in candidates] == [
            "soundcloud:playlists:2"
        ]

    def test_direct_url_accepts_ordinary_playlist(
        self, plugin, requests_mock
    ) -> None:
        url = "https://soundcloud.com/u/sets/mix"
        playlist = _playlist(playlist_type="playlist")
        requests_mock.get(
            f"{API_URL}/resolve",
            json={"kind": "playlist", "urn": playlist["urn"]},
        )
        requests_mock.get(
            f"{API_URL}/playlists/{playlist['urn']}", json=playlist
        )

        info = plugin.album_for_id(url)

        assert info is not None
        assert info.album == "Release"
        assert info.albumtype == "playlist"

    def test_mixed_track_credits_create_compilation(
        self, plugin, requests_mock
    ) -> None:
        playlist = _playlist(
            tracks=[
                _track(
                    "soundcloud:tracks:1", title="First", artist="Artist One"
                ),
                _track(
                    "soundcloud:tracks:2", title="Second", artist="Artist Two"
                ),
            ],
            artwork_url="https://i1.sndcdn.com/artwork.jpg",
            ean="1234567890123",
            genre="Electronic",
            label_name="Label",
            release_day=6,
            release_month=5,
            release_year=2024,
        )
        requests_mock.get(
            f"{API_URL}/playlists/{playlist['urn']}", json=playlist
        )

        info = plugin.album_for_id(str(playlist["urn"]))

        assert info is not None
        assert info.artist == config["va_name"].as_str()
        assert info.va is True
        assert info.barcode == "1234567890123"
        assert info.cover_art_url == "https://i1.sndcdn.com/artwork.jpg"
        assert info.genres == ["Electronic"]
        assert info.label == "Label"
        assert (info.year, info.month, info.day) == (2024, 5, 6)
        assert info.soundcloud_playlist_urn == "soundcloud:playlists:2"
        assert info.soundcloud_artist_urn == "soundcloud:users:10"
        assert info.albumstatus is None
        assert [track.index for track in info.tracks] == [1, 2]
        assert [track.medium_total for track in info.tracks] == [2, 2]

    def test_shared_track_credit_becomes_album_artist(
        self, plugin, requests_mock
    ) -> None:
        playlist = _playlist(
            tracks=[
                _track("soundcloud:tracks:1", artist="Shared Artist"),
                _track("soundcloud:tracks:2", artist="Shared Artist"),
            ]
        )
        requests_mock.get(
            f"{API_URL}/playlists/{playlist['urn']}", json=playlist
        )

        info = plugin.album_for_id(str(playlist["urn"]))

        assert info is not None
        assert info.artist == "Shared Artist"
        assert info.va is False
