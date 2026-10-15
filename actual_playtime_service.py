"""Thin per-game lookup view over the shared `OwnedGamesCache` snapshot.

Used by consumers (library_service, thumbnail_providers) that only care about one game's real
playtime/icon at a time. This intentionally does not fetch anything itself: `OwnedGamesCache` is
the single source of truth for the Steam Web API's owned-games data, refreshed on rescan. Two
independent fetchers hitting the same endpoint used to cause the two to disagree (one fresh, one
stale/failed), which is why this is now just a read-only view.
"""
from __future__ import annotations

from typing import Optional

from owned_games_cache import OwnedGamesCache
from steam_web_api import OwnedGameRecord


class ActualPlaytimeService:
    def __init__(self, owned_games: OwnedGamesCache):
        self._owned_games = owned_games

    def get(self, appid: int) -> Optional[OwnedGameRecord]:
        return self._owned_games.snapshot().get(appid)

