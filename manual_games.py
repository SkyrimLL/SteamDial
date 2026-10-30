"""Thread-safe JSON store for games the user added by hand.

Steam Dial builds its library from the local Steam installation, so games owned elsewhere
(GOG, itch.io, physical/offline installs) never show up. This store keeps those titles,
identified by a synthetic AppID, so the rest of the app can treat them like any scanned game:
playtime lookups, thumbnails overrides and the per-game edit panel all key off the AppID.

Only the title is stored here; every other property is an edit handled by the existing stores
(`UserOverrides` for played/completionist hours, `ThumbnailCache` for artwork).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Protocol

from models import SteamGame

log = logging.getLogger(__name__)

_SCHEMA_VERSION = 1

# Steam draws its AppIDs from the 32-bit range, so starting above it keeps synthetic IDs from
# ever colliding with a real game the scanner discovers later.
MANUAL_APPID_BASE = 9_000_000_000
MAX_NAME_LENGTH = 200


class ManualGamesSource(Protocol):
    """Read side of the store, as consumed by `LibraryService`."""

    def games(self) -> List[SteamGame]:
        ...


def is_manual_appid(appid: int) -> bool:
    return appid >= MANUAL_APPID_BASE


class ManualGamesStore:
    """Persists user-added games, assigning each one an AppID outside Steam's range."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._names: Dict[int, str] = {}
        self._next_appid = MANUAL_APPID_BASE
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Manual games %s unreadable (%s); starting empty", self.path, exc)
            return

        names: Dict[int, str] = {}
        for key, value in raw.get("games", {}).items():
            try:
                appid = int(key)
            except (TypeError, ValueError):
                log.warning("Skipping malformed manual game entry %s", key)
                continue
            name = str(value.get("name", "")).strip() if isinstance(value, dict) else str(value).strip()
            if not name:
                log.warning("Skipping manual game %s with no title", key)
                continue
            names[appid] = name

        try:
            next_appid = int(raw.get("next_appid", MANUAL_APPID_BASE))
        except (TypeError, ValueError):
            next_appid = MANUAL_APPID_BASE
        self._names = names
        # Deleted games must not hand their AppID to the next addition, or it would inherit the
        # artwork and overrides stored under it, so the counter only ever moves forward.
        self._next_appid = max(next_appid, max(names, default=MANUAL_APPID_BASE - 1) + 1, MANUAL_APPID_BASE)

    def _flush(self) -> None:
        payload = {
            "version": _SCHEMA_VERSION,
            "next_appid": self._next_appid,
            "games": {str(k): {"name": v} for k, v in sorted(self._names.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".manual-games-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    @staticmethod
    def _clean_title(name: str) -> str:
        title = " ".join(str(name).split())
        if not title:
            raise ValueError("Game title must not be empty")
        if len(title) > MAX_NAME_LENGTH:
            raise ValueError(f"Game title must be at most {MAX_NAME_LENGTH} characters")
        return title

    def _reject_duplicate(self, title: str, excluding: int | None = None) -> None:
        if any(appid != excluding and existing.lower() == title.lower()
               for appid, existing in self._names.items()):
            raise ValueError(f"'{title}' has already been added")

    def games(self) -> List[SteamGame]:
        with self._lock:
            entries = list(self._names.items())
        games = [SteamGame(appid=appid, name=name, installed=False, manual=True)
                 for appid, name in entries]
        return sorted(games, key=lambda g: g.name.lower())

    def add(self, name: str) -> SteamGame:
        """Registers a new game under a freshly allocated AppID.

        Raises `ValueError` when the title is blank, too long, or already added.
        """
        title = self._clean_title(name)
        with self._lock:
            self._reject_duplicate(title)
            appid, self._next_appid = self._next_appid, self._next_appid + 1
            self._names[appid] = title
            try:
                self._flush()
            except OSError as exc:
                del self._names[appid]
                self._next_appid = appid
                log.error("Failed to write manual games %s: %s", self.path, exc)
                raise
        return SteamGame(appid=appid, name=title, installed=False, manual=True)

    def rename(self, appid: int, name: str) -> SteamGame:
        """Gives an added game a new title, keeping its AppID (and so its artwork and overrides).

        Raises `KeyError` when the game wasn't added by hand, `ValueError` for an unusable title.
        """
        title = self._clean_title(name)
        with self._lock:
            previous = self._names.get(appid)
            if previous is None:
                raise KeyError(appid)
            self._reject_duplicate(title, excluding=appid)
            self._names[appid] = title
            try:
                self._flush()
            except OSError as exc:
                self._names[appid] = previous
                log.error("Failed to write manual games %s: %s", self.path, exc)
                raise
        return SteamGame(appid=appid, name=title, installed=False, manual=True)

    def remove(self, appid: int) -> SteamGame:
        """Deletes an added game. Raises `KeyError` when it wasn't added by hand."""
        with self._lock:
            name = self._names.get(appid)
            if name is None:
                raise KeyError(appid)
            del self._names[appid]
            try:
                self._flush()
            except OSError as exc:
                self._names[appid] = name
                log.error("Failed to write manual games %s: %s", self.path, exc)
                raise
        return SteamGame(appid=appid, name=name, installed=False, manual=True)
