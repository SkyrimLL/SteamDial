"""Thread-safe JSON store for per-game overrides entered by the user via the edit panel.

Unlike `CompletionistOverrides` (read-only, hand-edited from the missing-playtime report),
this store is written by the app itself: marking a game hidden/completed outside Steam, or
manually correcting a wrong completionist-hours estimate, all go through `UserOverrides.set()`.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger(__name__)

_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class GameOverride:
    hidden: bool = False
    completed: bool = False
    completionist_hours: Optional[float] = None

    def is_empty(self) -> bool:
        return not self.hidden and not self.completed and self.completionist_hours is None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "GameOverride":
        return cls(
            hidden=bool(data.get("hidden", False)),
            completed=bool(data.get("completed", False)),
            completionist_hours=data.get("completionist_hours"),
        )


class UserOverrides:
    """Persists one `GameOverride` per AppID, set through the UI's edit panel."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._entries: Dict[int, GameOverride] = self._load()

    def _load(self) -> Dict[int, GameOverride]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("User overrides %s unreadable (%s); starting empty", self.path, exc)
            return {}

        entries: Dict[int, GameOverride] = {}
        for key, value in raw.get("games", {}).items():
            try:
                entries[int(key)] = GameOverride.from_dict(value)
            except (TypeError, ValueError) as exc:
                log.warning("Skipping malformed user override %s: %s", key, exc)
        return entries

    def _flush(self) -> None:
        payload = {
            "version": _SCHEMA_VERSION,
            "games": {str(k): v.to_dict() for k, v in sorted(self._entries.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".user-overrides-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def get(self, appid: int) -> GameOverride:
        with self._lock:
            return self._entries.get(appid, GameOverride())

    def snapshot(self) -> Dict[int, GameOverride]:
        with self._lock:
            return dict(self._entries)

    def set(self, appid: int, hidden: bool, completed: bool, completionist_hours: Optional[float]) -> GameOverride:
        """Replaces the override for `appid`, removing the entry entirely once it's back to defaults."""
        override = GameOverride(hidden=hidden, completed=completed, completionist_hours=completionist_hours)
        with self._lock:
            if override.is_empty():
                self._entries.pop(appid, None)
            else:
                self._entries[appid] = override
            try:
                self._flush()
            except OSError as exc:
                log.error("Failed to write user overrides %s: %s", self.path, exc)
        return override
