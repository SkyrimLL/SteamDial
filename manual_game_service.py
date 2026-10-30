"""Use cases for the games the user added by hand: add, rename and delete.

Each one spans the manual-games store and the loaded library, and deleting also has to clear
the per-AppID leftovers other stores keep (artwork, overrides, cached estimates). Keeping that
coordination here leaves the Flask routes thin and the individual stores unaware of each other.
"""
from __future__ import annotations

import logging
from typing import Protocol, Sequence

from library_service import LibraryService
from manual_games import ManualGamesStore
from models import SteamGame

log = logging.getLogger(__name__)


class PerGameDataStore(Protocol):
    """Anything holding data keyed by AppID that a deleted game should not leave behind."""

    def discard(self, appid: int) -> None:
        ...


class ManualGameService:
    """Adds, renames and deletes manually added games, keeping the loaded library in step."""

    def __init__(self, store: ManualGamesStore, library: LibraryService,
                 dependents: Sequence[PerGameDataStore] = ()):
        self._store = store
        self._library = library
        self._dependents = tuple(dependents)

    def add(self, name: str) -> SteamGame:
        """Registers a new game. Raises `ValueError` for an unusable or duplicate title."""
        game = self._store.add(name)
        self._library.add_new_games([game])
        return game

    def rename(self, appid: int, name: str) -> SteamGame:
        """Retitles an added game, keeping its AppID so its artwork and overrides follow it.

        Raises `KeyError` when the game wasn't added by hand, `ValueError` for a bad title.
        """
        game = self._store.rename(appid, name)
        self._library.replace_game(game)
        return game

    def remove(self, appid: int) -> SteamGame:
        """Deletes an added game along with everything stored under its AppID.

        Raises `KeyError` when the game wasn't added by hand.
        """
        game = self._store.remove(appid)
        self._library.remove_game(appid)
        for store in self._dependents:
            try:
                store.discard(appid)
            except OSError:
                # The game itself is already gone; stale leftovers must not fail the deletion.
                log.exception("Could not clear data for removed game %s in %s", appid, type(store).__name__)
        return game
