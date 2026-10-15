"""Thread-safe JSON cache for playtime lookups, stored in the project folder (never in Steam)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from models import CacheEntry, PlaytimeEstimate

log = logging.getLogger(__name__)

_SCHEMA_VERSION = 1


class GameCache:
    """Persists one `CacheEntry` per AppID. Every `put` is flushed to disk atomically."""

    def __init__(self, path: Path, forbidden_root: Optional[Path] = None):
        self.path = Path(path).resolve()
        if forbidden_root is not None and self.path.is_relative_to(Path(forbidden_root).resolve()):
            raise ValueError(f"Refusing to store cache inside the read-only Steam folder: {self.path}")
        self._lock = threading.Lock()
        self._entries: Dict[int, CacheEntry] = self._load()

    def _load(self) -> Dict[int, CacheEntry]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            backup = self.path.with_suffix(".corrupt.json")
            log.error("Cache %s unreadable (%s); starting fresh, keeping copy at %s", self.path, exc, backup)
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            return {}

        entries: Dict[int, CacheEntry] = {}
        for key, value in raw.get("games", {}).items():
            try:
                entries[int(key)] = CacheEntry.from_dict(value)
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("Skipping malformed cache entry %s: %s", key, exc)
        return entries

    def _flush(self) -> None:
        payload = {
            "version": _SCHEMA_VERSION,
            "games": {str(k): v.to_dict() for k, v in sorted(self._entries.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".cache-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def get(self, appid: int) -> Optional[CacheEntry]:
        with self._lock:
            return self._entries.get(appid)

    def snapshot(self) -> Dict[int, CacheEntry]:
        with self._lock:
            return dict(self._entries)

    def put(self, appid: int, name: str, estimate: Optional[PlaytimeEstimate]) -> CacheEntry:
        entry = CacheEntry(appid=appid, name=name, fetched_at=time.time(), estimate=estimate)
        with self._lock:
            self._entries[appid] = entry
            try:
                self._flush()
            except OSError as exc:
                log.error("Failed to write cache %s: %s", self.path, exc)
        return entry

    def needs_lookup(self, appid: int, retry_missing_after_s: float) -> bool:
        """True for unknown games, or games previously not found whose retry window elapsed."""
        entry = self.get(appid)
        if entry is None:
            return True
        return not entry.found and (time.time() - entry.fetched_at) > retry_missing_after_s
