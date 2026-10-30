"""Background worker that pre-resolves artwork for every known AppID.

Thumbnails are otherwise only downloaded when a card asks for one (`GET /thumb/<appid>`), so
`/api/games` would report "no thumbnail" for any game whose artwork simply hasn't been fetched
yet — even though the card then renders it fine. Warming the cache up front keeps that flag
honest, which is what the "No thumbnail" filter relies on.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, Optional, Protocol

log = logging.getLogger(__name__)


class ThumbnailResolver(Protocol):
    """The single capability this worker needs from a thumbnail source."""

    def resolve(self, appid: int) -> Optional[Path]:
        """Returns a local path to the AppID's artwork, downloading it if necessary."""
        ...


class ThumbnailWarmupService:
    """Resolves artwork for a batch of AppIDs on a background thread pool.

    Resolution is cheap for anything already cached or recently confirmed to have no artwork,
    so repeat runs cost little more than a stat() per game.
    """

    def __init__(self, resolver: ThumbnailResolver, max_workers: int = 4):
        self._resolver = resolver
        self._max_workers = max(1, max_workers)
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, appids: Iterable[int]) -> bool:
        """Starts a warm-up run in the background. Returns False if one is already in flight."""
        todo = list(dict.fromkeys(int(appid) for appid in appids))
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            if not todo:
                return True
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, args=(todo,),
                                            name="thumbnail-warmup", daemon=True)
            self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _run(self, appids: list[int]) -> None:
        resolved = 0
        with ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="thumb-warm") as pool:
            for found in pool.map(self._resolve_one, appids):
                if found:
                    resolved += 1
        log.info("Thumbnail warm-up finished: artwork available for %d of %d games", resolved, len(appids))

    def _resolve_one(self, appid: int) -> bool:
        if self._stop.is_set():
            return False
        try:
            return self._resolver.resolve(appid) is not None
        except Exception:  # a single bad AppID must not abort the whole run
            log.exception("Thumbnail warm-up failed for app %s", appid)
            return False
