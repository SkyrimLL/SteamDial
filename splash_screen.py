"""Adapter over PyInstaller's `pyi_splash` bootloader splash screen.

The splash is drawn by the bootloader's embedded Tcl/Tk *before* Python starts, which covers
the onefile unpack delay. It never closes on its own, so the app must call `close()`.

`pyi_splash` only exists inside a frozen build whose spec declares a `Splash(...)`; everywhere
else (dev runs, onefile builds without a splash) the factory returns a no-op implementation so
callers never need to care whether they are frozen.
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

log = logging.getLogger("steamdial.splash")


@runtime_checkable
class SplashScreen(Protocol):
    """Startup progress indicator shown until the main window is ready."""

    def update_text(self, text: str) -> None:
        """Replaces the status line on the splash image."""

    def close(self) -> None:
        """Dismisses the splash. Safe to call repeatedly."""


class NullSplashScreen:
    """Used when no splash is available, so callers can stay unconditional."""

    def update_text(self, text: str) -> None:
        return None

    def close(self) -> None:
        return None


class PyInstallerSplashScreen:
    """Delegates to the `pyi_splash` module injected by the PyInstaller bootloader."""

    def __init__(self, pyi_splash_module) -> None:
        self._splash = pyi_splash_module
        self._closed = False

    def update_text(self, text: str) -> None:
        if self._closed:
            return
        try:
            self._splash.update_text(text)
        except Exception:
            # A user-closed or already-torn-down splash breaks the IPC pipe; startup must not die with it.
            log.debug("Could not update splash text", exc_info=True)
            self._closed = True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._splash.close()
        except Exception:
            log.debug("Could not close splash", exc_info=True)


def create_splash_screen() -> SplashScreen:
    """Returns the bootloader-backed splash when running inside a frozen build that has one."""
    try:
        import pyi_splash  # type: ignore[import-not-found]  # Injected by the PyInstaller bootloader only.
    except ImportError:
        return NullSplashScreen()
    return PyInstallerSplashScreen(pyi_splash)
