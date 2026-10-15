"""Resolves a servable thumbnail path for an AppID: manual override, Steam's own cache, our
cache, or a fresh download.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from steam_parser import SteamLibraryScanner
from thumbnail_cache import ThumbnailCache
from thumbnail_providers import ThumbnailProvider, ThumbnailUnavailable

log = logging.getLogger(__name__)

_DEFAULT_RETRY_MISSING_AFTER_S = 7 * 24 * 3600


class ThumbnailService:
    def __init__(
        self,
        scanner: SteamLibraryScanner,
        cache: ThumbnailCache,
        provider: ThumbnailProvider,
        retry_missing_after_s: float = _DEFAULT_RETRY_MISSING_AFTER_S,
    ):
        self._scanner = scanner
        self._cache = cache
        self._provider = provider
        self._retry_after = retry_missing_after_s

    def local_only(self, appid: int) -> Optional[Path]:
        """Resolves without making any network request: manual override, then Steam's cache, then ours."""
        return (self._cache.get_override(appid)
                or self._scanner.local_header_image(appid)
                or self._cache.get_image(appid))

    def set_override(self, appid: int, content: bytes) -> Path:
        """Saves a user-uploaded image as the manual override thumbnail for `appid`."""
        return self._cache.store_override(appid, content)

    def resolve(self, appid: int) -> Optional[Path]:
        found = self.local_only(appid)
        if found is not None:
            return found

        if not self._cache.needs_fetch(appid, self._retry_after):
            return None

        try:
            content = self._provider.fetch(appid)
        except ThumbnailUnavailable as exc:
            log.debug("Thumbnail fetch unavailable for app %s: %s", appid, exc)
            return None  # transient network issue: don't mark as confirmed-missing, just retry next time

        if content is None:
            self._cache.mark_missing(appid)
            return None
        return self._cache.store_image(appid, content)
