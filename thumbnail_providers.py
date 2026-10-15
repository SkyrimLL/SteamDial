"""Thumbnail image sources (Strategy pattern), chained behind a single `ThumbnailProvider` protocol."""
from __future__ import annotations

import logging
from typing import Optional, Protocol, Sequence

import requests

from actual_playtime_service import ActualPlaytimeService

log = logging.getLogger(__name__)

_MIN_IMAGE_BYTES = 1024  # Steam's CDN returns tiny placeholder/empty bodies for missing artwork.
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_RIFF_MAGIC = b"RIFF"
_WEBP_MAGIC = b"WEBP"


class ThumbnailUnavailable(Exception):
    """Source could not be reached; the result must not be cached as 'confirmed missing'."""


class ThumbnailProvider(Protocol):
    name: str

    def fetch(self, appid: int) -> Optional[bytes]:
        """Returns image bytes, None if this appid has no artwork, or raises ThumbnailUnavailable."""
        ...


def _looks_like_image(content: bytes, min_bytes: int = _MIN_IMAGE_BYTES) -> bool:
    if len(content) < min_bytes:
        return False
    if content.startswith(_JPEG_MAGIC) or content.startswith(_PNG_MAGIC):
        return True
    return content.startswith(_RIFF_MAGIC) and content[8:12] == _WEBP_MAGIC


class SteamCdnProvider:
    """Downloads a single Steam CDN artwork variant (e.g. header, capsule, library image)."""

    def __init__(self, name: str, url_template: str, session: Optional[requests.Session] = None,
                 timeout: float = 10.0):
        self.name = name
        self._url_template = url_template
        self._session = session or requests.Session()
        self._timeout = timeout

    def fetch(self, appid: int) -> Optional[bytes]:
        url = self._url_template.format(appid=appid)
        try:
            resp = self._session.get(url, timeout=self._timeout)
        except requests.RequestException as exc:
            raise ThumbnailUnavailable(f"{self.name} error: {exc}") from exc
        if resp.status_code == 404:
            return None
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raise ThumbnailUnavailable(f"{self.name} error: {exc}") from exc
        return resp.content if _looks_like_image(resp.content) else None


class SteamGridDbProvider:
    """Community artwork from SteamGridDB, used when Steam's own CDN has nothing for an AppID.

    Requires a free API key from https://www.steamgriddb.com/profile/preferences/api.
    """

    name = "steamgriddb"
    _GRIDS_URL = "https://www.steamgriddb.com/api/v2/grids/steam/{appid}"

    def __init__(self, api_key: str, session: Optional[requests.Session] = None, timeout: float = 10.0):
        self._api_key = api_key
        self._session = session or requests.Session()
        self._timeout = timeout

    def fetch(self, appid: int) -> Optional[bytes]:
        image_url = self._find_grid_url(appid)
        if image_url is None:
            return None
        try:
            resp = self._session.get(image_url, timeout=self._timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ThumbnailUnavailable(f"{self.name} image download error: {exc}") from exc
        return resp.content if _looks_like_image(resp.content) else None

    def _find_grid_url(self, appid: int) -> Optional[str]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        params = {"dimensions": "460x215,920x430", "types": "static", "nsfw": "any"}
        try:
            resp = self._session.get(
                self._GRIDS_URL.format(appid=appid), headers=headers, params=params, timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise ThumbnailUnavailable(f"{self.name} error: {exc}") from exc
        if resp.status_code == 404:
            return None
        try:
            resp.raise_for_status()
            payload = resp.json()
        except (requests.HTTPError, ValueError) as exc:
            raise ThumbnailUnavailable(f"{self.name} error: {exc}") from exc
        results = payload.get("data") or []
        return results[0].get("url") if results else None


class SteamWebApiIconProvider:
    """Last-resort artwork: the small per-app icon from `IPlayerService/GetOwnedGames`.

    Only works for games the Steam Web API already returned data for (see `ActualPlaytimeService`);
    icons are tiny (32x32), so they're tried only after every other source has failed.
    """

    name = "steam_web_api_icon"
    _ICON_URL = "https://media.steampowered.com/steamcommunity/public/images/apps/{appid}/{icon_hash}.jpg"
    _MIN_ICON_BYTES = 256

    def __init__(self, actual_playtime: ActualPlaytimeService, session: Optional[requests.Session] = None,
                 timeout: float = 10.0):
        self._actual_playtime = actual_playtime
        self._session = session or requests.Session()
        self._timeout = timeout

    def fetch(self, appid: int) -> Optional[bytes]:
        data = self._actual_playtime.get(appid)
        if data is None or not data.icon_url:
            return None
        url = self._ICON_URL.format(appid=appid, icon_hash=data.icon_url)
        try:
            resp = self._session.get(url, timeout=self._timeout)
        except requests.RequestException as exc:
            raise ThumbnailUnavailable(f"{self.name} error: {exc}") from exc
        if resp.status_code == 404:
            return None
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raise ThumbnailUnavailable(f"{self.name} error: {exc}") from exc
        return resp.content if _looks_like_image(resp.content, min_bytes=self._MIN_ICON_BYTES) else None


class ChainedThumbnailProvider:
    """Tries providers in order (header art, then smaller capsule art, ...); first hit wins."""

    name = "chain"

    def __init__(self, providers: Sequence[ThumbnailProvider]):
        self._providers = list(providers)

    def fetch(self, appid: int) -> Optional[bytes]:
        unavailable: list[str] = []
        for provider in self._providers:
            try:
                content = provider.fetch(appid)
            except ThumbnailUnavailable as exc:
                log.debug("%s unavailable for app %s: %s", provider.name, appid, exc)
                unavailable.append(provider.name)
                continue
            if content is not None:
                return content
        if unavailable:
            raise ThumbnailUnavailable(f"Sources unreachable: {', '.join(unavailable)}")
        return None


def default_provider(steamgriddb_api_key: Optional[str] = None,
                      actual_playtime: Optional[ActualPlaytimeService] = None) -> ThumbnailProvider:
    session = requests.Session()
    providers: list[ThumbnailProvider] = [
        SteamCdnProvider("header", "https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg", session),
        SteamCdnProvider("capsule", "https://cdn.akamai.steamstatic.com/steam/apps/{appid}/capsule_231x87.jpg",
                         session),
        SteamCdnProvider("library_capsule",
                         "https://cdn.akamai.steamstatic.com/steam/apps/{appid}/library_600x900.jpg", session),
    ]
    if steamgriddb_api_key:
        providers.append(SteamGridDbProvider(steamgriddb_api_key, session))
    if actual_playtime is not None:
        providers.append(SteamWebApiIconProvider(actual_playtime, session))
    return ChainedThumbnailProvider(providers)
