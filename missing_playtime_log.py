"""Background logger that lists games missing a completionist playtime estimate.

Writes a JSON file (appid + name entries) for games whose cache entry has no
`completionist_hours` value, so they can be looked up manually via other methods later.
Games covered by a `CompletionistOverrides` file are considered resolved and excluded.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional, Sequence

from cache_manager import GameCache
from completionist_overrides import CompletionistOverrides
from models import SteamGame

log = logging.getLogger(__name__)


class MissingPlaytimeLogger:
    """Writes games with no completionist estimate to a JSON file, off the calling thread."""

    def __init__(self, cache: GameCache, path: Path, overrides: Optional[CompletionistOverrides] = None):
        self._cache = cache
        self.path = Path(path)
        self._overrides = overrides
        self._write_lock = threading.Lock()

    def refresh_async(self, games: Sequence[SteamGame]) -> None:
        """Regenerates the log on a background thread so callers aren't blocked by disk I/O."""
        threading.Thread(
            target=self._refresh, args=(list(games),), name="missing-playtime-log", daemon=True,
        ).start()

    def _refresh(self, games: list[SteamGame]) -> None:
        cached = self._cache.snapshot()
        missing = [
            game for game in games
            if ((entry := cached.get(game.appid)) is None or entry.estimate is None
                or entry.estimate.completionist_hours is None)
            and (self._overrides is None or game.appid not in self._overrides)
        ]
        missing.sort(key=lambda g: g.name.lower())

        payload = {
            "metric": "completionist_hours",
            "count": len(missing),
            "games": [{"appid": game.appid, "name": game.name} for game in missing],
        }

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._write_lock:
                fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".missing-playtime-", suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump(payload, fh, indent=2, ensure_ascii=False)
                    os.replace(tmp, self.path)
                except OSError:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                    raise
        except OSError as exc:
            log.error("Failed to write missing playtime log %s: %s", self.path, exc)
