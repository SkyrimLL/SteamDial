"""Combines scanned Steam games with cached playtime data for presentation."""
from __future__ import annotations

import threading
from typing import List, Optional

from actual_playtime_service import ActualPlaytimeService
from cache_manager import GameCache
from completionist_overrides import CompletionistOverrides
from models import SteamGame
from steam_parser import SteamLibraryScanner
from user_overrides import GameOverride, UserOverrides

CDN_HEADER_URL = "https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg"


class LibraryService:
    def __init__(self, scanner: SteamLibraryScanner, cache: GameCache,
                 actual_playtime: Optional[ActualPlaytimeService] = None,
                 completionist_overrides: Optional[CompletionistOverrides] = None,
                 user_overrides: Optional[UserOverrides] = None):
        self._scanner = scanner
        self._cache = cache
        self._actual_playtime = actual_playtime
        self._overrides = completionist_overrides
        self._user_overrides = user_overrides
        self._games: List[SteamGame] = []
        self._lock = threading.Lock()

    def rescan(self) -> List[SteamGame]:
        games = self._scanner.scan()
        with self._lock:
            self._games = games
        return games

    def add_new_games(self, new_games: List[SteamGame]) -> List[SteamGame]:
        """Merges previously-unknown games into the loaded library without a full rescan."""
        with self._lock:
            known = {g.appid for g in self._games}
            added = [g for g in new_games if g.appid not in known]
            self._games = sorted(self._games + added, key=lambda g: g.name.lower())
            return added

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
            user_override = self._user_overrides.get(game.appid) if self._user_overrides else GameOverride()
            if user_override.completionist_hours is not None:
                completionist_hours = user_override.completionist_hours
                playtime_source = "manual"
                found = True
            actual_hours = round(actual.playtime_forever_minutes / 60, 1) if actual else None
            # Playtime meeting/exceeding the completionist estimate counts as completed unless the user says otherwise.
            auto_completed = actual_hours is not None and completionist_hours is not None and actual_hours >= completionist_hours
            result.append({
                "appid": game.appid,
                "name": game.name,
                "installed": game.installed,
                "hidden": game.hidden or user_override.hidden,
                "user_hidden": user_override.hidden,
                "completed": user_override.completed or auto_completed,
                "size_on_disk": game.size_on_disk,
                "thumbnail": f"/thumb/{game.appid}",
                "main_hours": est.main_hours if est else None,
                "extra_hours": est.extra_hours if est else None,
                "completionist_hours": completionist_hours,
                "completionist_override_hours": user_override.completionist_hours,
                "playtime_source": playtime_source,
                "playtime_status": "found" if found else ("not_found" if entry else "pending"),
                "actual_hours": actual_hours,
            })
        return result
