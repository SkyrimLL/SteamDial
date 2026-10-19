"""Thread-safe on-disk cache of downloaded thumbnail images, stored in the project folder.

Mirrors cache_manager.GameCache's conventions (atomic writes, forbidden-root guard) but for
image bytes on disk plus a small JSON sidecar tracking appids confirmed to have no artwork.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger(__name__)

_MISSING_FILE = "_missing.json"
_OVERRIDE_SUBDIR = "overrides"
_OVERRIDE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
_OVERRIDE_README = """Drop an image here named <AppID>.jpg (or .jpeg/.png/.webp), e.g. 440.jpg,
to replace the thumbnail for that game. Files here always take priority over Steam's own
cached artwork and anything Steam Dial downloaded automatically.
"""
_OVERRIDE_MAX_BYTES = 8 * 1024 * 1024
_JPEG_MAGIC = b"\xff\xd8\xff"


class ThumbnailCache:
    """Persists one image file per AppID under `directory`, plus a missing-lookup ledger.

    Also exposes a user-facing `overrides/` subfolder for manually supplied replacement images.
    """

    def __init__(self, directory: Path, forbidden_root: Optional[Path] = None):
        self.directory = Path(directory).resolve()
        if forbidden_root is not None and self.directory.is_relative_to(Path(forbidden_root).resolve()):
            raise ValueError(f"Refusing to store thumbnails inside the read-only Steam folder: {self.directory}")
        self._lock = threading.Lock()
        self._missing_path = self.directory / _MISSING_FILE
        self._missing: Dict[int, float] = self._load_missing()
        self.overrides_dir = self.directory / _OVERRIDE_SUBDIR
        self._ensure_overrides_dir()

    def _ensure_overrides_dir(self) -> None:
        try:
            self.overrides_dir.mkdir(parents=True, exist_ok=True)
            readme = self.overrides_dir / "README.txt"
            if not readme.exists():
                readme.write_text(_OVERRIDE_README, encoding="utf-8")
        except OSError as exc:
            log.warning("Could not prepare thumbnail overrides folder %s: %s", self.overrides_dir, exc)

    def get_override(self, appid: int) -> Optional[Path]:
        for ext in _OVERRIDE_EXTENSIONS:
            candidate = self.overrides_dir / f"{appid}{ext}"
            if candidate.is_file():
                return candidate
        return None

    def store_override(self, appid: int, content: bytes) -> Path:
        """Saves a user-uploaded override image as `<appid>.jpg`, replacing any existing override."""
        if not content or len(content) > _OVERRIDE_MAX_BYTES or not content.startswith(_JPEG_MAGIC):
            raise ValueError("Uploaded thumbnail must be a non-empty JPEG image under 8 MB")
        self.overrides_dir.mkdir(parents=True, exist_ok=True)
        target = self.overrides_dir / f"{appid}.jpg"
        fd, tmp = tempfile.mkstemp(dir=self.overrides_dir, prefix=".override-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            os.replace(tmp, target)
        except OSError:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        for ext in _OVERRIDE_EXTENSIONS:
            if ext == ".jpg":
                continue
            stale = self.overrides_dir / f"{appid}{ext}"
            if stale.is_file():
                try:
                    stale.unlink()
                except OSError as exc:
                    log.warning("Could not remove stale override %s: %s", stale, exc)
        return target

    def _load_missing(self) -> Dict[int, float]:
        try:
            with open(self._missing_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            return {int(k): float(v) for k, v in raw.items()}
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            log.warning("Thumbnail missing-ledger %s unreadable (%s); starting fresh", self._missing_path, exc)
            return {}

    def _flush_missing(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".missing-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({str(k): v for k, v in self._missing.items()}, fh)
            os.replace(tmp, self._missing_path)
        except OSError:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def _image_path(self, appid: int) -> Path:
        return self.directory / f"{appid}.jpg"

    def get_image(self, appid: int) -> Optional[Path]:
        path = self._image_path(appid)
        return path if path.is_file() else None

    def store_image(self, appid: int, content: bytes) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self._image_path(appid)
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".thumb-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            os.replace(tmp, target)
        except OSError:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        with self._lock:
            self._missing.pop(appid, None)
            try:
                self._flush_missing()
            except OSError as exc:
                log.warning("Failed to update thumbnail missing-ledger: %s", exc)
        return target

    def mark_missing(self, appid: int) -> None:
        with self._lock:
            self._missing[appid] = time.time()
            try:
                self._flush_missing()
            except OSError as exc:
                log.warning("Failed to update thumbnail missing-ledger: %s", exc)

    def needs_fetch(self, appid: int, retry_missing_after_s: float) -> bool:
        """False only if we already have the image, or recently confirmed none exists."""
        if self.get_image(appid) is not None:
            return False
        with self._lock:
            checked_at = self._missing.get(appid)
        return checked_at is None or (time.time() - checked_at) > retry_missing_after_s

    def invalidate_all(self) -> None:
        """Clears our own downloaded images and the missing-ledger (not manual overrides), so
        every appid is re-fetched from the provider next time it's requested. Used by Rescan.
        """
        with self._lock:
            for path in self.directory.glob("*.jpg"):
                try:
                    path.unlink()
                except OSError as exc:
                    log.warning("Could not remove cached thumbnail %s: %s", path, exc)
            self._missing.clear()
            try:
                self._flush_missing()
            except OSError as exc:
                log.warning("Failed to reset thumbnail missing-ledger: %s", exc)
