"""Playtime estimate sources (Strategy pattern) behind a single `PlaytimeProvider` protocol."""
from __future__ import annotations

import logging
import re
from typing import Optional, Protocol, Sequence

import requests

from models import PlaytimeEstimate

log = logging.getLogger(__name__)


class LookupUnavailable(Exception):
    """Source could not be reached; the result must not be cached as 'not found'."""


class PlaytimeProvider(Protocol):
    name: str

    def lookup(self, appid: int, title: str) -> Optional[PlaytimeEstimate]:
        """Returns an estimate, None if the game is unknown, or raises LookupUnavailable."""
        ...


_TRADEMARKS = re.compile(r"[™®©]")
_EDITION_SUFFIX = re.compile(
    r"\s*[-:–]?\s*\(?(game of the year|goty|definitive|enhanced|complete|remastered|special|anniversary|"
    r"deluxe|ultimate|gold|director'?s cut)( edition)?\)?\s*$",
    re.IGNORECASE,
)


def title_variants(title: str) -> list[str]:
    """Raw title first, then progressively simplified forms that match HLTB naming better."""
    cleaned = _TRADEMARKS.sub("", title).strip()
    variants = [cleaned]
    stripped = _EDITION_SUFFIX.sub("", cleaned).strip()
    if stripped and stripped not in variants:
        variants.append(stripped)
    return variants


class HowLongToBeatProvider:
    """Adapter over the `howlongtobeatpy` package."""

    name = "howlongtobeat"

    def __init__(self, min_similarity: float = 0.55):
        from howlongtobeatpy import HowLongToBeat  # deferred so the app still starts without it

        self._client = HowLongToBeat()
        self._min_similarity = min_similarity

    def lookup(self, appid: int, title: str) -> Optional[PlaytimeEstimate]:
        for variant in title_variants(title):
            try:
                results = self._client.search(variant)
            except Exception as exc:  # third-party lib raises assorted network/parse errors
                raise LookupUnavailable(f"HLTB error: {exc}") from exc
            if results is None:
                raise LookupUnavailable("HLTB request failed")
            best = max(results, key=lambda r: r.similarity, default=None)
            if best is None or best.similarity < self._min_similarity:
                continue
            main = best.main_story or best.main_extra or best.completionist
            if not main:
                continue
            return PlaytimeEstimate(
                main_hours=round(float(main), 1),
                extra_hours=round(float(best.main_extra), 1) if best.main_extra else None,
                completionist_hours=round(float(best.completionist), 1) if best.completionist else None,
                source=self.name,
                matched_name=best.game_name,
            )
        return None


class SteamSpyProvider:
    """Fallback using SteamSpy's median playtime of owners (minutes)."""

    name = "steamspy"
    _URL = "https://steamspy.com/api.php"

    def __init__(self, session: Optional[requests.Session] = None, timeout: float = 10.0):
        self._session = session or requests.Session()
        self._timeout = timeout

    def lookup(self, appid: int, title: str) -> Optional[PlaytimeEstimate]:
        try:
            resp = self._session.get(
                self._URL, params={"request": "appdetails", "appid": appid}, timeout=self._timeout
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise LookupUnavailable(f"SteamSpy error: {exc}") from exc

        minutes = data.get("median_forever") or data.get("average_forever") or 0
        if not minutes:
            return None
        return PlaytimeEstimate(
            main_hours=round(minutes / 60.0, 1),
            source=self.name,
            matched_name=data.get("name") or title,
        )


class ChainedPlaytimeProvider:
    """Tries providers in order; first hit wins (Chain of Responsibility)."""

    name = "chain"

    def __init__(self, providers: Sequence[PlaytimeProvider]):
        self._providers = list(providers)

    def lookup(self, appid: int, title: str) -> Optional[PlaytimeEstimate]:
        unavailable: list[str] = []
        for provider in self._providers:
            try:
                result = provider.lookup(appid, title)
            except LookupUnavailable as exc:
                log.debug("%s unavailable for %s: %s", provider.name, title, exc)
                unavailable.append(provider.name)
                continue
            if result is not None:
                return result
        if unavailable:
            raise LookupUnavailable(f"Sources unreachable: {', '.join(unavailable)}")
        return None


def default_provider() -> PlaytimeProvider:
    providers: list[PlaytimeProvider] = []
    try:
        providers.append(HowLongToBeatProvider())
    except ImportError:
        log.warning("howlongtobeatpy not installed; HowLongToBeat lookups disabled")
    providers.append(SteamSpyProvider())
    return ChainedPlaytimeProvider(providers)
