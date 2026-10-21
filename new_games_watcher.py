"""Background watcher that periodically checks the Steam Web API / local files for new games.

Runs independently of the explicit "Rescan" action so the UI can surface a lightweight "new
games found" affordance without re-scanning/re-enriching the whole library every time.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable, List, Optional

from library_service import LibraryService
from models import SteamGame
from owned_games_cache import OwnedGamesCache
from steam_parser import SteamLibraryScanner

log = logging.getLogger(__name__)


@dataclass
class NewGamesStatus:
    count: int = 0
    checked_at: float = 0.0


class NewGamesWatcher:
    """Periodically re-scans local files / the Steam Web API and tracks newly found appids."""

    def __init__(
        self,
        library: LibraryService,
        scanner: SteamLibraryScanner,
        owned_games_cache: Optional[OwnedGamesCache],
        interval_s: float = 10 * 60,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._library = library
        self._scanner = scanner
        self._owned_games_cache = owned_games_cache
        self._interval = interval_s
        self._sleep = sleep
        self._lock = threading.Lock()
        self._check_lock = threading.Lock()
        self._status = NewGamesStatus()
        self._new_games: List[SteamGame] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="new-games-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        with self._lock:
            return asdict(self._status)

    def new_games(self) -> List[SteamGame]:
        with self._lock:
            return list(self._new_games)

    def clear(self) -> None:
        with self._lock:
            self._new_games = []
            self._status = NewGamesStatus(count=0, checked_at=self._status.checked_at)

    def check_now(self) -> int:
        """Refreshes owned-games from the Steam Web API, re-scans local files, and records any
        appids not currently loaded in the library. Returns the new-games count."""
        with self._check_lock:
            if self._owned_games_cache is not None:
                self._owned_games_cache.refresh_now()
            candidates = self._scanner.scan()
            known = {g.appid for g in self._library.games}
            found = [g for g in candidates if g.appid not in known]
            with self._lock:
                self._new_games = found
                self._status = NewGamesStatus(count=len(found), checked_at=time.time())
            return len(found)

    def check_async(self) -> None:
        """Runs `check_now()` on a background thread (e.g. right after a full rescan, whose
        scan used the owned-games list as it was *before* this refresh)."""
        threading.Thread(target=self._safe_check, name="new-games-check", daemon=True).start()

    def _safe_check(self) -> None:
        try:
            self.check_now()
        except Exception:
            log.exception("New-games check failed")

    def _run(self) -> None:
        # Check immediately: the startup scan used the owned-games list persisted on disk, so
        # games bought since the last run only show up once that list is refreshed here.
        self._safe_check()
        while not self._stop.wait(self._interval):
            self._safe_check()
