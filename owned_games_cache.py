"""Thread-safe on-disk cache of the account's full owned-games list from the Steam Web API.

This is the authoritative source of "what does this account own", used by the scanner in
place of the old heuristic (inferring ownership from leftover local librarycache files). The
cache is loaded from disk synchronously at startup so cataloguing/scanning can run offline
using the last known snapshot, then refreshed from the API in the background on each rescan.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from steam_web_api import OwnedGameRecord, SteamWebApiClient

log = logging.getLogger(__name__)

_SCHEMA_VERSION = 1


class OwnedGamesCache:
    """Persists `SteamWebApiClient.fetch_owned_games()` as a detailed local JSON file."""

    def __init__(self, path: Path, client: Optional[SteamWebApiClient]):
        self.path = Path(path)
        self._client = client
        self._lock = threading.Lock()
        self._fetched_at: float = 0.0
        self._games: Dict[int, OwnedGameRecord] = self._load()

    def _load(self) -> Dict[int, OwnedGameRecord]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Owned-games cache %s unreadable (%s); starting empty", self.path, exc)
            return {}

        self._fetched_at = float(raw.get("fetched_at", 0) or 0)
        games: Dict[int, OwnedGameRecord] = {}
        for key, value in raw.get("games", {}).items():
            try:
                games[int(key)] = OwnedGameRecord.from_dict(value)
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("Skipping malformed owned-games entry %s: %s", key, exc)
        return games

    def _flush(self) -> None:
        payload = {
            "version": _SCHEMA_VERSION,
            "fetched_at": self._fetched_at,
            "games": {str(k): v.to_dict() for k, v in sorted(self._games.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".owned-games-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def refresh_async(self) -> None:
        if self._client is None:
            return
        threading.Thread(target=self._refresh, name="owned-games-fetch", daemon=True).start()

    def refresh_now(self) -> None:
        """Synchronous version of `refresh_async()`, for callers already on a background thread."""
        if self._client is None:
            return
        self._refresh()

    def _refresh(self) -> None:
        try:
            games = self._client.fetch_owned_games()  # type: ignore[union-attr]
        except Exception as exc:  # network/API errors must not crash a rescan
            log.warning("Could not fetch owned games from the Steam Web API: %s", exc)
            return
        with self._lock:
            self._games = games
            self._fetched_at = time.time()
            try:
                self._flush()
            except OSError as exc:
                log.error("Failed to write owned-games cache %s: %s", self.path, exc)
        log.info("Fetched and cached %d owned games from the Steam Web API", len(games))

    def snapshot(self) -> Dict[int, OwnedGameRecord]:
        with self._lock:
            return dict(self._games)
