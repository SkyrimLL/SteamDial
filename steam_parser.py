"""Discovers games in a local Steam installation.

Install state (installed/not, size on disk, library path) always comes from local manifests.
Ownership of not-installed games prefers the Steam Web API's owned-games list (see
`owned_games_cache.py`) when available, falling back to a heuristic over leftover local
cache files (appcache/librarycache, userdata/*/config/librarycache) otherwise.

Strictly read-only: every file under the Steam root is opened in read mode and
nothing is ever created, modified or deleted there.
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set

import vdf

from appinfo_reader import AppInfo, AppInfoReader
from models import SteamGame
from steam_web_api import OwnedGameRecord

log = logging.getLogger(__name__)

_STATE_FULLY_INSTALLED = 4
_GAME_TYPES = {"game"}


def _read_text_vdf(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return vdf.load(fh)
    except FileNotFoundError:
        log.debug("Missing VDF file: %s", path)
    except (OSError, SyntaxError, ValueError) as exc:
        log.warning("Could not parse %s: %s", path, exc)
    return None


def _lower_keys(d: dict) -> dict:
    """ACF/VDF key casing is inconsistent across Steam versions."""
    return {str(k).lower(): v for k, v in d.items()}


class SteamLibraryScanner:
    """Builds the game list from library folders, app manifests and the local app cache."""

    def __init__(self, steam_root: Path, owned_games: Optional[Callable[[], Dict[int, OwnedGameRecord]]] = None):
        self.steam_root = Path(steam_root)
        self._owned_games = owned_games

    # ---- library folders / manifests -------------------------------------------------

    def library_paths(self) -> List[Path]:
        paths: List[Path] = [self.steam_root]
        data = _read_text_vdf(self.steam_root / "steamapps" / "libraryfolders.vdf")
        if data:
            root = _lower_keys(data).get("libraryfolders", {})
            for entry in root.values():
                raw = entry.get("path") if isinstance(entry, dict) else entry
                if isinstance(raw, str) and raw:
                    p = Path(raw)
                    if p.resolve() not in {x.resolve() for x in paths}:
                        paths.append(p)
        return [p for p in paths if (p / "steamapps").is_dir()]

    def _installed_games(self) -> Dict[int, SteamGame]:
        games: Dict[int, SteamGame] = {}
        for library in self.library_paths():
            for manifest in (library / "steamapps").glob("appmanifest_*.acf"):
                data = _read_text_vdf(manifest)
                if not data:
                    continue
                state = _lower_keys(_lower_keys(data).get("appstate", {}))
                try:
                    appid = int(state.get("appid", 0))
                    flags = int(state.get("stateflags", 0))
                except ValueError:
                    continue
                if not appid:
                    continue
                games[appid] = SteamGame(
                    appid=appid,
                    name=state.get("name") or f"App {appid}",
                    installed=bool(flags & _STATE_FULLY_INSTALLED),
                    size_on_disk=int(state.get("sizeondisk", 0) or 0),
                    library_path=str(library),
                )
        return games

    # ---- owned-but-not-installed discovery -------------------------------------------

    def _owned_appid_candidates(self) -> Set[int]:
        """Steam keeps per-app artwork / library data for apps in the user's library."""
        candidates: Set[int] = set()
        cache_dir = self.steam_root / "appcache" / "librarycache"
        candidates |= self._numeric_names(cache_dir.iterdir() if cache_dir.is_dir() else [], dirs=True)

        userdata = self.steam_root / "userdata"
        if userdata.is_dir():
            for user in userdata.iterdir():
                user_cache = user / "config" / "librarycache"
                if user_cache.is_dir():
                    candidates |= self._numeric_names(user_cache.glob("*.json"), dirs=False)
        return candidates

    @staticmethod
    def _numeric_names(entries: Iterable[Path], dirs: bool) -> Set[int]:
        ids: Set[int] = set()
        for entry in entries:
            name = entry.name if dirs else entry.stem
            if name.isdigit() and (entry.is_dir() if dirs else entry.is_file()):
                ids.add(int(name))
        return ids

    # ---- hidden games ------------------------------------------------------------------

    def _hidden_appids(self) -> Set[int]:
        """Modern Steam tracks "Hidden" as a built-in collection, synced via its cloud-storage
        cache at userdata/<id>/config/cloudstorage/cloud-storage-namespace-*.json. Each entry's
        `value` is itself JSON; the one with id "hidden" lists appids in `added`/`removed`.
        (Older Steam versions stored a per-app `Hidden` flag in localconfig.vdf; that key is no
        longer written by current clients, so it isn't checked here.)
        """
        hidden: Set[int] = set()
        userdata = self.steam_root / "userdata"
        if not userdata.is_dir():
            return hidden
        for user in userdata.iterdir():
            cloud_dir = user / "config" / "cloudstorage"
            if not cloud_dir.is_dir():
                continue
            for namespace_file in cloud_dir.glob("cloud-storage-namespace-*.json"):
                hidden |= self._hidden_from_namespace_file(namespace_file)
        return hidden

    @staticmethod
    def _hidden_from_namespace_file(path: Path) -> Set[int]:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                entries = json.load(fh)
        except FileNotFoundError:
            return set()
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not parse %s: %s", path, exc)
            return set()

        for key, entry in entries:
            if key != "user-collections.hidden" or not isinstance(entry, dict):
                continue
            try:
                collection = json.loads(entry["value"])
                added = {int(a) for a in collection.get("added", [])}
                removed = {int(a) for a in collection.get("removed", [])}
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("Malformed hidden collection in %s: %s", path, exc)
                return set()
            return added - removed
        return set()

    # ---- public API ------------------------------------------------------------------

    def scan(self) -> List[SteamGame]:
        if not self.steam_root.is_dir():
            log.error("Steam root not found: %s", self.steam_root)
            return []

        appinfo: Dict[int, AppInfo] = AppInfoReader(self.steam_root / "appcache" / "appinfo.vdf").read_all()
        games = self._installed_games()

        # Drop installed non-games (redistributables, tools) when type info is available.
        games = {
            appid: g for appid, g in games.items()
            if appid not in appinfo or appinfo[appid].app_type in _GAME_TYPES
        }

        owned = self._owned_games() if self._owned_games is not None else {}
        if owned:
            # Steam Web API's owned-games list is the authoritative ownership source.
            for appid, record in owned.items():
                if appid in games:
                    continue
                games[appid] = SteamGame(appid=appid, name=record.name or f"App {appid}", installed=False)
        else:
            # No API key/profile configured (or nothing cached yet): fall back to inferring
            # ownership from leftover local cache files.
            for appid in self._owned_appid_candidates():
                info = appinfo.get(appid)
                if appid in games or info is None or info.app_type not in _GAME_TYPES:
                    continue
                games[appid] = SteamGame(appid=appid, name=info.name, installed=False)
        hidden = self._hidden_appids()
        games = {appid: (replace(g, hidden=True) if appid in hidden else g) for appid, g in games.items()}

        log.info("Discovered %d games (%d installed, %d hidden)",
                  len(games), sum(g.installed for g in games.values()), sum(g.hidden for g in games.values()))
        return sorted(games.values(), key=lambda g: g.name.lower())

    def local_header_image(self, appid: int) -> Optional[Path]:
        """Returns a cached header image from Steam's library cache, if present."""
        app_dir = self.steam_root / "appcache" / "librarycache" / str(int(appid))
        if not app_dir.is_dir():
            return None
        direct = app_dir / "header.jpg"
        if direct.is_file():
            return direct
        return next((p for p in app_dir.rglob("header.jpg") if p.is_file()), None)
