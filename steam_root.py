"""Locates and validates the local Steam installation folder.

Steam Dial needs a Steam root before it can scan anything, and hard-coding one only works on
the machine it was written for. Candidate folders come from pluggable sources (the Windows
registry, the per-platform default install paths, and a sweep of the usual "I moved Steam to
another drive" locations); the detector validates them and keeps the first real installation.

Nothing here writes to the Steam folder - candidates are only ever probed for existence.
"""
from __future__ import annotations

import logging
import os
import string
import sys
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Protocol, Sequence, Union

log = logging.getLogger(__name__)

# Last resort, so the app still starts (and can report the problem) when nothing is detected.
FALLBACK_STEAM_ROOT = Path(r"F:\Steam")

# Every usable Steam root holds the library folder Steam installs games into.
_LIBRARY_DIR = "steamapps"
# Present in a real Steam *installation* but not in a bare secondary library folder, which
# also has a steamapps/ directory and would otherwise look like a valid root.
_INSTALL_MARKERS = ("steam.exe", "steam.sh", "steamclient.dll", "appcache", "userdata")

PathLike = Union[str, "os.PathLike[str]"]


def is_steam_root(path: Optional[PathLike]) -> bool:
    """True when `path` can be scanned for games, i.e. it holds a `steamapps` library folder."""
    if path is None or str(path).strip() == "":
        return False
    try:
        return (Path(path) / _LIBRARY_DIR).is_dir()
    except OSError:  # unreadable drive, malformed path, ...
        return False


def _is_steam_installation(path: Path) -> bool:
    """True for a full Steam install, as opposed to a secondary library folder."""
    try:
        return is_steam_root(path) and any((path / marker).exists() for marker in _INSTALL_MARKERS)
    except OSError:
        return False


class SteamRootCandidateSource(Protocol):
    """Supplies folders that *might* be a Steam root. Validation is the detector's job."""

    def candidates(self) -> Iterable[Path]:
        ...


class WindowsRegistryCandidateSource:
    """Steam records its own install folder in the registry - the most reliable source."""

    _KEYS = (
        ("HKEY_CURRENT_USER", r"Software\Valve\Steam", "SteamPath"),
        ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Valve\Steam", "InstallPath"),
    )

    def candidates(self) -> Iterator[Path]:
        if os.name != "nt":
            return
        import winreg

        for hive, subkey, value_name in self._KEYS:
            try:
                with winreg.OpenKey(getattr(winreg, hive), subkey) as key:
                    raw, _ = winreg.QueryValueEx(key, value_name)
            except OSError as exc:
                log.debug("No Steam path in %s\\%s: %s", hive, subkey, exc)
                continue
            if isinstance(raw, str) and raw.strip():
                yield Path(raw.strip())


class WellKnownPathCandidateSource:
    """The default install location for the current platform."""

    def candidates(self) -> Iterator[Path]:
        if os.name == "nt":
            for variable in ("ProgramFiles(x86)", "ProgramW6432", "ProgramFiles"):
                base = os.environ.get(variable)
                if base:
                    yield Path(base) / "Steam"
        elif sys.platform == "darwin":
            yield Path.home() / "Library" / "Application Support" / "Steam"
        else:
            yield Path.home() / ".steam" / "steam"
            yield Path.home() / ".local" / "share" / "Steam"


class WindowsDriveScanCandidateSource:
    """Catches the common "Steam lives on my games drive" setups, e.g. ``F:\\Steam``."""

    _SUBPATHS = (
        Path("Steam"),
        Path("Program Files (x86)") / "Steam",
        Path("Program Files") / "Steam",
        Path("Games") / "Steam",
        Path("SteamLibrary"),
    )

    def candidates(self) -> Iterator[Path]:
        if os.name != "nt":
            return
        for letter in string.ascii_uppercase[2:]:  # skip legacy floppy drives A: and B:
            drive = Path(f"{letter}:\\")
            try:
                if not drive.is_dir():
                    continue
            except OSError:
                continue
            for subpath in self._SUBPATHS:
                yield drive / subpath


def default_candidate_sources() -> Sequence[SteamRootCandidateSource]:
    """Ordered cheapest/most-authoritative first; the drive sweep is the slow last resort."""
    return (
        WindowsRegistryCandidateSource(),
        WellKnownPathCandidateSource(),
        WindowsDriveScanCandidateSource(),
    )


class SteamRootDetector:
    """Returns the first candidate that looks like a real Steam installation."""

    def __init__(self, sources: Optional[Sequence[SteamRootCandidateSource]] = None):
        self._sources = tuple(sources) if sources is not None else tuple(default_candidate_sources())

    def detect(self) -> Optional[Path]:
        library_only: Optional[Path] = None
        for candidate in self._unique_candidates():
            if _is_steam_installation(candidate):
                log.info("Detected Steam installation at %s", candidate)
                return candidate
            # A plain library folder is still scannable, but keep looking for the real install.
            if library_only is None and is_steam_root(candidate):
                library_only = candidate
        if library_only is not None:
            log.info("Detected a Steam library folder at %s", library_only)
        return library_only

    def _unique_candidates(self) -> Iterator[Path]:
        seen: set[Path] = set()
        for source in self._sources:
            try:
                candidates: List[Path] = list(source.candidates())
            except Exception:  # a broken source must not sink the whole detection
                log.exception("Steam root candidate source %s failed", type(source).__name__)
                continue
            for candidate in candidates:
                try:
                    key = candidate.resolve()
                except OSError:
                    key = candidate
                if key in seen:
                    continue
                seen.add(key)
                yield candidate


def resolve_steam_root(
    configured: Optional[PathLike] = None,
    detector: Optional[SteamRootDetector] = None,
    fallback: Path = FALLBACK_STEAM_ROOT,
) -> Path:
    """Resolves the Steam root to run with.

    Order: an explicitly configured value (CLI flag, ``STEAMDIAL_STEAM_ROOT`` from the
    environment or ``.env``) wins whenever it is usable; a stale configured value falls back
    to auto-detection, and is kept as-is only when detection finds nothing either - so the
    Settings panel can show the user exactly which path is failing.
    """
    configured_path = Path(str(configured).strip()) if configured is not None and str(configured).strip() else None
    if configured_path is not None and is_steam_root(configured_path):
        return configured_path

    detected = (detector if detector is not None else SteamRootDetector()).detect()
    if configured_path is not None:
        if detected is not None:
            log.warning("Configured Steam root %s has no %s folder; using detected %s instead",
                        configured_path, _LIBRARY_DIR, detected)
            return detected
        log.error("Configured Steam root %s has no %s folder and no Steam installation was detected",
                  configured_path, _LIBRARY_DIR)
        return configured_path
    if detected is not None:
        return detected
    log.error("No Steam installation detected; falling back to %s. Set the Steam library folder in Settings.",
              fallback)
    return Path(fallback)


class SteamRootSettings:
    """What the Settings panel needs to know about the Steam root: the folder the app is
    actually running with, whether that folder works, and what detection would pick now.
    """

    def __init__(self, active: PathLike, detector: Optional[SteamRootDetector] = None):
        self._active = Path(active)
        self._detector = detector if detector is not None else SteamRootDetector()

    @property
    def active(self) -> Path:
        return self._active

    def is_valid(self) -> bool:
        return is_steam_root(self._active)

    def detect(self) -> Optional[Path]:
        return self._detector.detect()

    def validate(self, value: Optional[PathLike]) -> Optional[Path]:
        """Checks a user-supplied folder. Returns the normalised path, or None for "auto-detect".

        Raises ValueError with a user-facing message when the folder isn't a Steam root.
        """
        if value is None or str(value).strip() == "":
            return None
        path = Path(str(value).strip().strip('"'))
        if not path.is_dir():
            raise ValueError(f"Folder not found: {path}")
        if not is_steam_root(path):
            raise ValueError(f"No {_LIBRARY_DIR} folder inside {path} - pick your Steam install folder.")
        return path
