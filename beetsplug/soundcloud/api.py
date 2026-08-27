"""Access public SoundCloud metadata with renewable application credentials."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager, suppress
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeGuard, overload

import requests

from beets import __version__
from beets.exceptions import UserError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from .api_types import (
        SoundCloudCollection,
        SoundCloudPlaylist,
        SoundCloudResource,
        SoundCloudToken,
        SoundCloudTrack,
    )

API_URL = "https://api.soundcloud.com"
AUTH_URL = "https://secure.soundcloud.com/oauth/token"
TOKEN_EXPIRY_MARGIN = 30
TOKEN_LOCK_POLL_INTERVAL = 0.05
TOKEN_LOCK_TIMEOUT = 15
TOKEN_LOCK_STALE_AFTER = 30


class SoundCloudAPIError(UserError):
    """Report an API failure that a user can act on."""


class SoundCloudNotFoundError(SoundCloudAPIError):
    """Indicate that a requested public resource does not exist."""


class SoundCloudAPI:
    """Retrieve public SoundCloud resources and keep OAuth tokens current."""

    def __init__(
        self, client_id: str, client_secret: str, token_path: str | Path
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_path = Path(token_path)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            f"beets/{__version__} https://beets.io/"
        )
        self._token = self._load_token()
        self._token_lock = threading.Lock()

    def _load_token(self) -> SoundCloudToken:
        try:
            with self.token_path.open() as token_file:
                token = json.load(token_file)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(token, dict):
            return {}
        access_token = token.get("access_token")
        refresh_token = token.get("refresh_token")
        expires_at = token.get("expires_at")
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(refresh_token, str)
            or not refresh_token
            or not isinstance(expires_at, (int, float))
            or isinstance(expires_at, bool)
        ):
            return {}
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": float(expires_at),
        }

    @staticmethod
    def _lock_is_stale(lock_path: Path) -> bool:
        try:
            return (
                time.time() - lock_path.stat().st_mtime > TOKEN_LOCK_STALE_AFTER
            )
        except FileNotFoundError:
            return True

    @staticmethod
    def _create_token_lock(lock_path: Path) -> int | None:
        try:
            return os.open(
                lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError:
            return None

    def _acquire_token_lock(self, lock_path: Path) -> int:
        deadline = time.monotonic() + TOKEN_LOCK_TIMEOUT
        while True:
            descriptor = self._create_token_lock(lock_path)
            if descriptor is not None:
                return descriptor
            if self._lock_is_stale(lock_path):
                with suppress(FileNotFoundError):
                    lock_path.unlink()
                continue
            if time.monotonic() >= deadline:
                raise SoundCloudAPIError(
                    "Timed out waiting for the SoundCloud token cache lock"
                )
            time.sleep(TOKEN_LOCK_POLL_INTERVAL)

    @contextmanager
    def _token_file_lock(self) -> Iterator[None]:
        lock_path = self.token_path.with_suffix(
            f"{self.token_path.suffix}.lock"
        )
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = self._acquire_token_lock(lock_path)

        try:
            os.write(descriptor, str(os.getpid()).encode())
            yield
        finally:
            os.close(descriptor)
            with suppress(FileNotFoundError):
                lock_path.unlink()

    def _save_token(self, token: SoundCloudToken) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.token_path.parent, prefix=f".{self.token_path.name}."
        )
        temporary_path = Path(temporary_name)
        try:
            temporary_path.chmod(0o600)
            with os.fdopen(descriptor, "w") as token_file:
                json.dump(token, token_file, indent=2)
                token_file.flush()
                os.fsync(token_file.fileno())
            temporary_path.replace(self.token_path)
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            with suppress(FileNotFoundError):
                temporary_path.unlink()
            raise

    def _require_credentials(self) -> None:
        if not self.client_id or not self.client_secret:
            raise SoundCloudAPIError(
                "SoundCloud client_id and client_secret must be configured"
            )

    def _token_is_current(self) -> bool:
        return bool(self._token.get("access_token")) and (
            self._token.get("expires_at", 0) > time.time() + TOKEN_EXPIRY_MARGIN
        )

    def _request_token(self, *, refresh: bool) -> SoundCloudToken:
        self._require_credentials()
        if refresh:
            data = {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self._token["refresh_token"],
            }
            auth = None
        else:
            data = {"grant_type": "client_credentials"}
            auth = (self.client_id, self.client_secret)

        try:
            response = self.session.post(
                AUTH_URL, data=data, auth=auth, timeout=10
            )
            response.raise_for_status()
            response_payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise SoundCloudAPIError(
                f"SoundCloud authentication failed: {exc}"
            ) from exc

        if not isinstance(response_payload, dict):
            raise SoundCloudAPIError(
                "SoundCloud authentication response has an unexpected shape"
            )
        access_token = response_payload.get("access_token")
        refresh_token = response_payload.get("refresh_token")
        expires_in = response_payload.get("expires_in")
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(refresh_token, str)
            or not refresh_token
            or not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or expires_in <= 0
        ):
            raise SoundCloudAPIError(
                "SoundCloud authentication response has invalid token data"
            )
        payload: SoundCloudToken = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": expires_in,
            "expires_at": time.time() + expires_in,
        }
        scope = response_payload.get("scope")
        if isinstance(scope, str):
            payload["scope"] = scope
        token_type = response_payload.get("token_type")
        if isinstance(token_type, str):
            payload["token_type"] = token_type
        self._token = payload
        self._save_token(payload)
        return payload

    def _access_token(self, *, rejected_token: str | None = None) -> str:
        with self._token_lock:
            if rejected_token is None and self._token_is_current():
                return self._token["access_token"]
            with self._token_file_lock():
                cached_token = self._load_token()
                cached_access_token = cached_token.get("access_token")
                if (
                    isinstance(cached_access_token, str)
                    and cached_access_token
                    and cached_access_token != rejected_token
                    and cached_token.get("expires_at", 0)
                    > time.time() + TOKEN_EXPIRY_MARGIN
                ):
                    self._token = cached_token
                    return cached_access_token
                if cached_token:
                    self._token = cached_token
                refresh = bool(self._token.get("refresh_token"))
                return self._request_token(refresh=refresh)["access_token"]

    def _request(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        retry_unauthorized: bool = True,
    ) -> requests.Response:
        access_token = self._access_token()
        response = self.session.get(
            url,
            headers={"Authorization": f"OAuth {access_token}"},
            params=params,
            timeout=10,
        )
        if (
            response.status_code == HTTPStatus.UNAUTHORIZED
            and retry_unauthorized
        ):
            response = self.session.get(
                url,
                headers={
                    "Authorization": (
                        "OAuth "
                        + self._access_token(rejected_token=access_token)
                    )
                },
                params=params,
                timeout=10,
            )
        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            delay = response.headers.get("Retry-After", "an unknown interval")
            raise SoundCloudAPIError(
                f"SoundCloud rate limit reached; retry after {delay} seconds"
            )
        if response.status_code == HTTPStatus.NOT_FOUND:
            raise SoundCloudNotFoundError(
                f"SoundCloud resource was not found: {url}"
            )
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SoundCloudAPIError(
                f"SoundCloud API request failed: {exc}"
            ) from exc
        return response

    def _get_json(
        self, url: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            payload = self._request(url, params=params).json()
        except ValueError as exc:
            raise SoundCloudAPIError(
                "SoundCloud returned an invalid JSON response"
            ) from exc
        if not isinstance(payload, dict):
            raise SoundCloudAPIError(
                "SoundCloud returned an unexpected response shape"
            )
        return payload

    def _get_paginated(
        self,
        url: str,
        *,
        params: dict[str, Any],
        limit: int,
        accept: Callable[[SoundCloudResource], bool],
    ) -> list[SoundCloudResource]:
        resources: list[SoundCloudResource] = []
        request_params: dict[str, Any] | None = params
        while url and len(resources) < limit:
            page: SoundCloudCollection = self._get_json(  # type: ignore[assignment]
                url, params=request_params
            )
            collection = page.get("collection")
            if not isinstance(collection, list):
                raise SoundCloudAPIError(
                    "SoundCloud collection response is missing collection"
                )
            for resource in collection:
                if accept(resource):
                    resources.append(resource)
                    if len(resources) == limit:
                        break
            next_href = page.get("next_href")
            url = next_href if isinstance(next_href, str) else ""
            request_params = None
        return resources

    def _get_collection(
        self,
        path: str,
        query: str,
        limit: int,
        accept: Callable[[SoundCloudResource], bool],
    ) -> list[SoundCloudResource]:
        return self._get_paginated(
            f"{API_URL}/{path}",
            params={"q": query, "limit": limit, "linked_partitioning": True},
            limit=limit,
            accept=accept,
        )

    def search_tracks(self, query: str, limit: int) -> list[SoundCloudTrack]:
        return [
            resource
            for resource in self._get_collection(
                "tracks", query, limit, self._is_track
            )
            if self._is_track(resource)
        ]

    def search_playlists(
        self, query: str, limit: int, *, playlist_type: str | None = None
    ) -> list[SoundCloudPlaylist]:
        def accept(resource: SoundCloudResource) -> bool:
            return self._is_playlist(resource) and (
                playlist_type is None
                or resource.get("playlist_type") == playlist_type
            )

        return [
            resource
            for resource in self._get_collection(
                "playlists", query, limit, accept
            )
            if self._is_playlist(resource)
        ]

    @staticmethod
    def _is_track(resource: SoundCloudResource) -> TypeGuard[SoundCloudTrack]:
        return resource.get("kind") == "track"

    @staticmethod
    def _is_playlist(
        resource: SoundCloudResource,
    ) -> TypeGuard[SoundCloudPlaylist]:
        return resource.get("kind") == "playlist"

    def resolve(self, url: str) -> SoundCloudResource | None:
        try:
            resource: SoundCloudResource = self._get_json(  # type: ignore[assignment]
                f"{API_URL}/resolve", params={"url": url}
            )
        except SoundCloudNotFoundError:
            return None
        self._validate_resource(resource)
        return resource

    @staticmethod
    def _validate_resource(resource: SoundCloudResource) -> None:
        if not resource.get("urn"):
            raise SoundCloudAPIError(
                "SoundCloud resource response is missing its urn"
            )

    @overload
    def _get_resource(
        self, identifier: str, kind: Literal["track"]
    ) -> SoundCloudTrack | None: ...

    @overload
    def _get_resource(
        self, identifier: str, kind: Literal["playlist"]
    ) -> SoundCloudPlaylist | None: ...

    def _get_resource(
        self, identifier: str, kind: Literal["track", "playlist"]
    ) -> SoundCloudResource | None:
        if identifier.startswith(("http://", "https://")):
            resolved = self.resolve(identifier)
            if not resolved or resolved.get("kind") != kind:
                return None
            identifier = resolved["urn"]
        elif not identifier.startswith(f"soundcloud:{kind}s:"):
            return None

        try:
            resource: SoundCloudResource = self._get_json(  # type: ignore[assignment]
                f"{API_URL}/{kind}s/{identifier}",
                params={"show_tracks": True} if kind == "playlist" else None,
            )
        except SoundCloudNotFoundError:
            return None
        self._validate_resource(resource)
        if resource.get("kind") != kind:
            return None
        if kind == "playlist" and self._is_playlist(resource):
            self._complete_playlist_tracks(resource, identifier)
        return resource

    def _complete_playlist_tracks(
        self, playlist: SoundCloudPlaylist, identifier: str
    ) -> None:
        tracks = playlist.get("tracks") or []
        track_count = playlist.get("track_count", len(tracks))
        if len(tracks) >= track_count:
            return
        resources = self._get_paginated(
            f"{API_URL}/playlists/{identifier}/tracks",
            params={"linked_partitioning": True},
            limit=track_count,
            accept=self._is_track,
        )
        playlist["tracks"] = [
            track for track in resources if self._is_track(track)
        ]

    def get_track(self, identifier: str) -> SoundCloudTrack | None:
        return self._get_resource(identifier, "track")

    def get_playlist(self, identifier: str) -> SoundCloudPlaylist | None:
        return self._get_resource(identifier, "playlist")
