"""Combines scanned Steam games with cached playtime data for presentation."""
from __future__ import annotations

import threading
from typing import List, Optional

from actual_playtime_service import ActualPlaytimeService
from cache_manager import GameCache
from completionist_overrides import CompletionistOverrides
from models import SteamGame
from steam_parser import SteamLibraryScanner

CDN_HEADER_URL = "https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg"


class LibraryService:
    def __init__(self, scanner: SteamLibraryScanner, cache: GameCache,
                 actual_playtime: Optional[ActualPlaytimeService] = None,
                 completionist_overrides: Optional[CompletionistOverrides] = None):
        self._scanner = scanner
        self._cache = cache
        self._actual_playtime = actual_playtime
        self._overrides = completionist_overrides
        self._games: List[SteamGame] = []
        self._lock = threading.Lock()

    def rescan(self) -> List[SteamGame]:
        games = self._scanner.scan()
        with self._lock:
            self._games = games
        return games

    @property
    def games(self) -> List[SteamGame]:
        with self._lock:
            return list(self._games)

    def game_dicts(self) -> List[dict]:
        cached = self._cache.snapshot()
        result = []
        for game in self.games:
            entry = cached.get(game.appid)
            est = entry.estimate if entry else None
            actual = self._actual_playtime.get(game.appid) if self._actual_playtime else None
            completionist_hours = est.completionist_hours if est else None
            playtime_source = est.source if est else None
            found = bool(entry and entry.found)
            if completionist_hours is None and self._overrides is not None:
                override_hours = self._overrides.get(game.appid)
                if override_hours is not None:
                    completionist_hours = override_hours
                    playtime_source = "override"
                    found = True
            result.append({
                "appid": game.appid,
                "name": game.name,
                "installed": game.installed,
                "hidden": game.hidden,
                "size_on_disk": game.size_on_disk,
                "thumbnail": f"/thumb/{game.appid}",
                "main_hours": est.main_hours if est else None,
                "extra_hours": est.extra_hours if est else None,
                "completionist_hours": completionist_hours,
                "playtime_source": playtime_source,
                "playtime_status": "found" if found else ("not_found" if entry else "pending"),
                "actual_hours": round(actual.playtime_forever_minutes / 60, 1) if actual else None,
            })
        return result
