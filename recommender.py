"""Maps time budgets (presets or natural language) to playtime ranges and filters games."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Protocol, Sequence


@dataclass(frozen=True)
class TimeRange:
    min_hours: float = 0.0
    max_hours: float = math.inf
    label: str = ""

    def contains(self, hours: float) -> bool:
        return self.min_hours <= hours < self.max_hours

    def to_dict(self) -> dict:
        return {
            "min_hours": self.min_hours,
            "max_hours": None if math.isinf(self.max_hours) else self.max_hours,
            "label": self.label,
        }


@dataclass(frozen=True)
class Preset:
    key: str
    title: str
    description: str
    range: TimeRange


PRESETS: tuple[Preset, ...] = (
    Preset("short", "Quick Session", "Very short game for a few hours only", TimeRange(0, 5, "Under 5 hours")),
    Preset("weekend", "Weekend Game", "Something to finish over a weekend", TimeRange(5, 20, "5 to 20 hours")),
    Preset("epic", "Epic Long Haul", "A big adventure to sink into", TimeRange(20, math.inf, "20+ hours")),
)
PRESETS_BY_KEY = {p.key: p for p in PRESETS}


# ---- natural language parsing (each rule is an independent strategy) ----------------

_NUMBER_WORDS = {
    "one": 1, "a couple of": 2, "a couple": 2, "couple of": 2, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "an": 1, "a": 1,
}
_NUM = r"(\d+(?:\.\d+)?)"
_UNIT = r"\s*(?:h|hr|hrs|hour|hours)\b"


def _normalise(text: str) -> str:
    text = text.lower()
    for word, value in sorted(_NUMBER_WORDS.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(rf"\b{word}\b(?=\s*(?:h|hr|hrs|hour|hours)\b)", str(value), text)
    return text


class ParseRule(Protocol):
    def parse(self, text: str) -> Optional[TimeRange]: ...


class BetweenRule:
    _pattern = re.compile(rf"(?:between\s+)?{_NUM}\s*(?:-|to|and|–)\s*{_NUM}{_UNIT}")

    def parse(self, text: str) -> Optional[TimeRange]:
        m = self._pattern.search(text)
        if not m:
            return None
        lo, hi = sorted((float(m.group(1)), float(m.group(2))))
        return TimeRange(lo, hi, f"{lo:g} to {hi:g} hours")


class UpperBoundRule:
    _pattern = re.compile(rf"(?:under|less than|below|at most|max(?:imum)?|up to|within|no more than|<)\s*{_NUM}{_UNIT}")

    def parse(self, text: str) -> Optional[TimeRange]:
        m = self._pattern.search(text)
        return TimeRange(0, float(m.group(1)), f"Under {float(m.group(1)):g} hours") if m else None


class LowerBoundRule:
    _pattern = re.compile(rf"(?:over|more than|above|at least|min(?:imum)?|>)\s*{_NUM}{_UNIT}")

    def parse(self, text: str) -> Optional[TimeRange]:
        m = self._pattern.search(text)
        return TimeRange(float(m.group(1)), math.inf, f"{float(m.group(1)):g}+ hours") if m else None


class ApproximateRule:
    """'about 6 hours' -> a window around the number."""

    _pattern = re.compile(rf"(?:about|around|roughly|approximately|approx\.?|~)\s*{_NUM}{_UNIT}")

    def __init__(self, tolerance: float = 0.5):
        self._tol = tolerance

    def parse(self, text: str) -> Optional[TimeRange]:
        m = self._pattern.search(text)
        if not m:
            return None
        h = float(m.group(1))
        return TimeRange(round(h * (1 - self._tol), 1), round(h * (1 + self._tol), 1), f"Around {h:g} hours")


class BudgetRule:
    """A bare 'I have 3 hours' is a budget: anything that fits within it."""

    _pattern = re.compile(rf"{_NUM}{_UNIT}")

    def parse(self, text: str) -> Optional[TimeRange]:
        m = self._pattern.search(text)
        return TimeRange(0, float(m.group(1)), f"Up to {float(m.group(1)):g} hours") if m else None


class KeywordRule:
    def __init__(self, keywords: Iterable[str], preset_key: str):
        self._pattern = re.compile(r"\b(?:" + "|".join(keywords) + r")\b")
        self._preset = PRESETS_BY_KEY[preset_key]

    def parse(self, text: str) -> Optional[TimeRange]:
        return self._preset.range if self._pattern.search(text) else None


DEFAULT_RULES: tuple[ParseRule, ...] = (
    BetweenRule(),
    UpperBoundRule(),
    LowerBoundRule(),
    ApproximateRule(),
    BudgetRule(),
    KeywordRule(["epic", "long", "haul", "lengthy", "huge", "massive", "month", "vacation", "holiday"], "epic"),
    KeywordRule(["weekend", "medium", "couple of days", "few days"], "weekend"),
    KeywordRule(["short", "quick", "few hours", "evening", "tonight", "afternoon", "brief", "small"], "short"),
)


class TimeBudgetParser:
    def __init__(self, rules: Sequence[ParseRule] = DEFAULT_RULES):
        self._rules = list(rules)

    def parse(self, text: str) -> Optional[TimeRange]:
        normalised = _normalise(text or "")
        for rule in self._rules:
            result = rule.parse(normalised)
            if result is not None:
                return result
        return None


class GameRecommender:
    """Filters game dicts to a time range using the chosen playtime metric."""

    METRICS = ("main_hours", "extra_hours", "completionist_hours")

    def recommend(self, games: Iterable[dict], time_range: TimeRange, metric: str = "main_hours",
                  include_hidden: bool = False) -> list[dict]:
        if metric not in self.METRICS:
            raise ValueError(f"Unknown playtime metric '{metric}'")
        matches = [g for g in games if g.get(metric) is not None and time_range.contains(g[metric])
                   and (include_hidden or not g.get("hidden"))]
        return sorted(matches, key=lambda g: g[metric])
