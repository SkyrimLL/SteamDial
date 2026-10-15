"""Loads user-supplied completionist-hour overrides for games the estimate couldn't find.

Reads `missing_completionist_override.json` -- a hand-edited copy of a missing-completionist
report with `completionist_hours` filled in per game -- and exposes it as a read-only lookup
used to patch gaps left by the automated playtime lookup.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger(__name__)


class CompletionistOverrides:
    """Read-only lookup of manually supplied completionist hours, keyed by AppID."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._hours: Dict[int, float] = self._load()

    def _load(self) -> Dict[int, float]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Completionist overrides %s unreadable (%s); ignoring", self.path, exc)
            return {}

        hours: Dict[int, float] = {}
        for game in raw.get("games", []):
            try:
                appid = int(game["appid"])
            except (KeyError, TypeError, ValueError):
                continue
            value = game.get("completionist_hours")
            if value is None:
                continue
            try:
                hours[appid] = float(value)
            except (TypeError, ValueError):
                continue
        return hours

    def get(self, appid: int) -> Optional[float]:
        return self._hours.get(appid)

    def __contains__(self, appid: int) -> bool:
        return appid in self._hours

    def __len__(self) -> int:
        return len(self._hours)
