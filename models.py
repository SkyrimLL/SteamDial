"""Plain domain data structures shared across the application."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


@dataclass(frozen=True)
class SteamGame:
    """A game discovered in the local Steam installation."""

    appid: int
    name: str
    installed: bool
    size_on_disk: int = 0
    library_path: Optional[str] = None
    hidden: bool = False


@dataclass(frozen=True)
class PlaytimeEstimate:
    """Estimated hours to beat a game. `main_hours` is the value used for recommendations."""

    main_hours: Optional[float]
    extra_hours: Optional[float] = None
    completionist_hours: Optional[float] = None
    source: str = "unknown"
    matched_name: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PlaytimeEstimate":
        return cls(
            main_hours=data.get("main_hours"),
            extra_hours=data.get("extra_hours"),
            completionist_hours=data.get("completionist_hours"),
            source=data.get("source", "unknown"),
            matched_name=data.get("matched_name"),
        )


@dataclass
class CacheEntry:
    """A persisted lookup result. `estimate` is None when no source knew the game."""

    appid: int
    name: str
    fetched_at: float
    estimate: Optional[PlaytimeEstimate] = None

    @property
    def found(self) -> bool:
        return self.estimate is not None and self.estimate.main_hours is not None

    def to_dict(self) -> dict:
        return {
            "appid": self.appid,
            "name": self.name,
            "fetched_at": self.fetched_at,
            "estimate": self.estimate.to_dict() if self.estimate else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CacheEntry":
        est = data.get("estimate")
        return cls(
            appid=int(data["appid"]),
            name=data.get("name", ""),
            fetched_at=float(data.get("fetched_at", 0)),
            estimate=PlaytimeEstimate.from_dict(est) if est else None,
        )
