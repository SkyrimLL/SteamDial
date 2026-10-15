"""Background worker that fills the cache with playtime estimates for uncached games."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable, Optional, Sequence

from cache_manager import GameCache
from models import SteamGame
from playtime_providers import LookupUnavailable, PlaytimeProvider

log = logging.getLogger(__name__)


@dataclass
class EnrichmentStatus:
    running: bool = False
    total: int = 0
    done: int = 0
    current: Optional[str] = None
    offline: bool = False
    message: str = "idle"


class PlaytimeEnrichmentService:
    """Looks up only games missing from the cache, saving each result immediately."""

    def __init__(
        self,
        cache: GameCache,
        provider: PlaytimeProvider,
        delay_s: float = 1.0,
        retry_missing_after_s: float = 30 * 24 * 3600,
        max_consecutive_failures: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        on_run_complete: Optional[Callable[[], None]] = None,
    ):
        self._cache = cache
        self._provider = provider
        self._delay = delay_s
        self._retry_after = retry_missing_after_s
        self._max_failures = max_consecutive_failures
        self._sleep = sleep
        self._on_run_complete = on_run_complete
        self._status = EnrichmentStatus()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def status(self) -> dict:
        with self._lock:
            return asdict(self._status)

    def pending(self, games: Sequence[SteamGame]) -> list[SteamGame]:
        return [g for g in games if self._cache.needs_lookup(g.appid, self._retry_after)]

    def start(self, games: Sequence[SteamGame]) -> bool:
        """Starts a background run if one isn't active. Returns False if already running."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            todo = self.pending(games)
            self._status = EnrichmentStatus(running=bool(todo), total=len(todo),
                                            message="fetching" if todo else "cache up to date")
            if not todo:
                return True
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, args=(todo,), name="playtime-enrich", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        self._stop.set()

    def _update(self, **changes) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(self._status, key, value)

    def _run(self, todo: list[SteamGame]) -> None:
        failures = 0
        for index, game in enumerate(todo, start=1):
            if self._stop.is_set():
                self._update(message="stopped")
                break
            self._update(current=game.name)
            try:
                estimate = self._provider.lookup(game.appid, game.name)
            except LookupUnavailable as exc:
                failures += 1
                log.warning("Lookup unavailable for %s: %s", game.name, exc)
                if failures >= self._max_failures:
                    self._update(offline=True, message="network unavailable; will retry on next scan")
                    break
            else:
                failures = 0
                self._cache.put(game.appid, game.name, estimate)
            self._update(done=index)
            self._sleep(self._delay)
        else:
            self._update(message="complete")
        self._update(running=False, current=None)
        if self._on_run_complete is not None:
            try:
                self._on_run_complete()
            except Exception:
                log.exception("on_run_complete callback failed")
