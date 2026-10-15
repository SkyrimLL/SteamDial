"""Client for the official Steam Web API (read-only): account-wide owned-games data.

This is the authoritative source for what the account actually owns (replacing the old
heuristic of inferring ownership from leftover local cache files), plus per-game playtime
and a small icon image for games with no cached artwork.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import requests

log = logging.getLogger(__name__)

_GET_OWNED_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
_ACCOUNT_ID_OFFSET = 76561197960265728  # SteamID64 = this + the 32-bit account id.


@dataclass(frozen=True)
class OwnedGameRecord:
    """One entry from `IPlayerService/GetOwnedGames`, as returned for the configured account."""

    appid: int
    name: str
    playtime_forever_minutes: int
    playtime_windows_minutes: int = 0
    playtime_mac_minutes: int = 0
    playtime_linux_minutes: int = 0
    playtime_2weeks_minutes: int = 0
    rtime_last_played: int = 0
    has_community_visible_stats: bool = False
    icon_url: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "appid": self.appid,
            "name": self.name,
            "playtime_forever_minutes": self.playtime_forever_minutes,
            "playtime_windows_minutes": self.playtime_windows_minutes,
            "playtime_mac_minutes": self.playtime_mac_minutes,
            "playtime_linux_minutes": self.playtime_linux_minutes,
            "playtime_2weeks_minutes": self.playtime_2weeks_minutes,
            "rtime_last_played": self.rtime_last_played,
            "has_community_visible_stats": self.has_community_visible_stats,
            "icon_url": self.icon_url,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OwnedGameRecord":
        return cls(
            appid=int(data["appid"]),
            name=data.get("name", ""),
            playtime_forever_minutes=int(data.get("playtime_forever_minutes", 0) or 0),
            playtime_windows_minutes=int(data.get("playtime_windows_minutes", 0) or 0),
            playtime_mac_minutes=int(data.get("playtime_mac_minutes", 0) or 0),
            playtime_linux_minutes=int(data.get("playtime_linux_minutes", 0) or 0),
            playtime_2weeks_minutes=int(data.get("playtime_2weeks_minutes", 0) or 0),
            rtime_last_played=int(data.get("rtime_last_played", 0) or 0),
            has_community_visible_stats=bool(data.get("has_community_visible_stats", False)),
            icon_url=data.get("icon_url") or None,
        )


def resolve_steam_id64(steam_root: Path) -> Optional[int]:
    """Picks the most recently used local profile's SteamID64 from `userdata` folder names."""
    userdata = Path(steam_root) / "userdata"
    if not userdata.is_dir():
        return None
    candidates = [p for p in userdata.iterdir() if p.is_dir() and p.name.isdigit() and p.name != "0"]
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    return _ACCOUNT_ID_OFFSET + int(newest.name)


class SteamWebApiClient:
    """Wraps `IPlayerService/GetOwnedGames`, the only official source for real playtime."""

    def __init__(self, api_key: str, steam_id64: int, session: Optional[requests.Session] = None,
                 timeout: float = 10.0):
        self._api_key = api_key
        self._steam_id64 = steam_id64
        self._session = session or requests.Session()
        self._timeout = timeout

    def fetch_owned_games(self) -> Dict[int, OwnedGameRecord]:
        """Returns every owned app's metadata and playtime, keyed by appid.

        Raises `requests.RequestException` or `ValueError` on failure; callers must not treat
        that as "no games" and should keep whatever snapshot they already had.
        """
        params = {
            "key": self._api_key,
            "steamid": self._steam_id64,
            "include_appinfo": 1,
            "include_played_free_games": 1,
            "format": "json",
        }
        resp = self._session.get(_GET_OWNED_GAMES_URL, params=params, timeout=self._timeout)
        resp.raise_for_status()
        payload = resp.json().get("response") or {}
        games = payload.get("games") or []

        result: Dict[int, OwnedGameRecord] = {}
        for g in games:
            appid = g.get("appid")
            if appid is None:
                continue
            appid = int(appid)
            result[appid] = OwnedGameRecord(
                appid=appid,
                name=g.get("name") or f"App {appid}",
                playtime_forever_minutes=int(g.get("playtime_forever", 0) or 0),
                playtime_windows_minutes=int(g.get("playtime_windows_forever", 0) or 0),
                playtime_mac_minutes=int(g.get("playtime_mac_forever", 0) or 0),
                playtime_linux_minutes=int(g.get("playtime_linux_forever", 0) or 0),
                playtime_2weeks_minutes=int(g.get("playtime_2weeks", 0) or 0),
                rtime_last_played=int(g.get("rtime_last_played", 0) or 0),
                has_community_visible_stats=bool(g.get("has_community_visible_stats", False)),
                icon_url=g.get("img_icon_url") or None,
            )
        return result

