"""Steam Dial - a local, read-only Steam library browser with playtime-based recommendations.

Run:  python steamdial.py [--steam-root F:\\Steam] [--port 5000] [--browser]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import threading
from pathlib import Path

from dotenv import dotenv_values, load_dotenv, set_key
from flask import Flask, abort, jsonify, render_template, request, send_file

from cache_manager import GameCache
from actual_playtime_service import ActualPlaytimeService
from completionist_overrides import CompletionistOverrides
from enrichment import PlaytimeEnrichmentService
from library_service import LibraryService
from missing_playtime_log import MissingPlaytimeLogger
from new_games_watcher import NewGamesWatcher
from owned_games_cache import OwnedGamesCache
from playtime_providers import default_provider
from recommender import PRESETS, PRESETS_BY_KEY, GameRecommender, TimeBudgetParser, TimeRange
from steam_parser import SteamLibraryScanner
from steam_web_api import SteamWebApiClient, resolve_steam_id64
from thumbnail_cache import ThumbnailCache
from thumbnail_providers import default_provider as default_thumbnail_provider
from thumbnail_service import ThumbnailService
from user_overrides import UserOverrides

# Bundled, read-only assets (templates/static/icon): inside the PyInstaller bundle when frozen.
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
# Writable app data (cache/thumbnails/.env): next to the executable when frozen, so it persists
# across runs instead of living in PyInstaller's onefile temp extraction folder.
BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH)
DEFAULT_STEAM_ROOT = os.environ.get("STEAMDIAL_STEAM_ROOT", r"F:\Steam")
DEFAULT_CACHE = DATA_DIR / "game_cache.json"
DEFAULT_THUMBNAIL_DIR = BASE_DIR / "thumbnails"
DEFAULT_MISSING_PLAYTIME_LOG = DATA_DIR / "missing_completionist.json"
DEFAULT_COMPLETIONIST_OVERRIDES = DATA_DIR / "missing_completionist_override.json"
DEFAULT_OWNED_GAMES_CACHE = DATA_DIR / "owned_games_cache.json"
DEFAULT_USER_OVERRIDES = DATA_DIR / "user_overrides.json"

log = logging.getLogger("steamdial")


def create_app(
    library: LibraryService,
    scanner: SteamLibraryScanner,
    enrichment: PlaytimeEnrichmentService,
    parser: TimeBudgetParser,
    recommender: GameRecommender,
    thumbnails: ThumbnailService,
    missing_playtime_log: MissingPlaytimeLogger,
    actual_playtime: ActualPlaytimeService,
    user_overrides: UserOverrides,
    new_games_watcher: NewGamesWatcher,
    allow_fetch: bool = True,
) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(RESOURCE_DIR / "templates"),
        static_folder=str(RESOURCE_DIR / "assets"),
        static_url_path="",
    )

    @app.get("/")
    def index():
        return render_template("index.html", presets=PRESETS)

    def thumbnail_url(appid: int, path: Path | None) -> str:
        url = f"/thumb/{appid}"
        if path is not None:
            stat = path.stat()
            url += f"?v={stat.st_mtime_ns}-{stat.st_size}"
        return url

    def add_thumbnail_info(games: list[dict]) -> list[dict]:
        for game in games:
            path = thumbnails.local_only(game["appid"])
            game["has_thumbnail"] = path is not None
            game["thumbnail"] = thumbnail_url(game["appid"], path)
        return games

    @app.get("/api/games")
    def api_games():
        games = add_thumbnail_info(library.game_dicts())
        status = enrichment.status()
        status["new_games_count"] = new_games_watcher.status()["count"]
        return jsonify(games=games, status=status)

    @app.get("/api/status")
    def api_status():
        status = enrichment.status()
        status["new_games_count"] = new_games_watcher.status()["count"]
        return jsonify(status)

    @app.get("/api/settings")
    def api_settings_get():
        values = dotenv_values(ENV_PATH) if ENV_PATH.is_file() else {}
        return jsonify(
            steam_api_key=values.get("STEAM_API_KEY") or "",
            steamgriddb_api_key=values.get("STEAMGRIDDB_API_KEY") or "",
            steam_api_host="localhost",
        )

    @app.post("/api/settings")
    def api_settings_post():
        data = request.get_json(silent=True) or {}
        steam_api_key = str(data.get("steam_api_key", "")).strip()
        steamgriddb_api_key = str(data.get("steamgriddb_api_key", "")).strip()
        ENV_PATH.touch(exist_ok=True)
        set_key(str(ENV_PATH), "STEAM_API_KEY", steam_api_key)
        set_key(str(ENV_PATH), "STEAMGRIDDB_API_KEY", steamgriddb_api_key)
        os.environ["STEAM_API_KEY"] = steam_api_key
        os.environ["STEAMGRIDDB_API_KEY"] = steamgriddb_api_key
        return jsonify(ok=True, restart_required=True)

    @app.post("/api/rescan")
    def api_rescan():
        """Full rescan: re-discovers every game and force-refreshes cached times/thumbnails from
        scratch, bypassing normal cache-staleness checks. Manual overrides are left untouched.
        """
        games = library.rescan()
        thumbnails.invalidate_cache()
        if allow_fetch:
            enrichment.start(games, force=True)
        missing_playtime_log.refresh_async(games)
        new_games_watcher.clear()
        if allow_fetch:
            # The rescan above used the previously-cached owned-games list; refresh it and flag
            # anything newly owned via the "Scan new games" button.
            new_games_watcher.check_async()
        return jsonify(count=len(games), status=enrichment.status())

    @app.post("/api/scan-new-games")
    def api_scan_new_games():
        """Lighter-weight scan: only adds games the background watcher already flagged as new."""
        added = library.add_new_games(new_games_watcher.new_games())
        new_games_watcher.clear()
        if allow_fetch:
            enrichment.start(library.games)
        missing_playtime_log.refresh_async(library.games)
        return jsonify(count=len(added), status=enrichment.status())

    @app.get("/api/recommend")
    def api_recommend():
        preset_key = request.args.get("preset", "").strip()
        max_hours_param = request.args.get("max_hours", "").strip()
        query = request.args.get("q", "").strip()[:200]
        metric = request.args.get("metric", "completionist_hours").strip()
        include_hidden = request.args.get("include_hidden", "0").strip() == "1"
        if metric not in GameRecommender.METRICS:
            return jsonify(error=f"Unknown playtime metric '{metric}'"), 400
        if preset_key:
            preset = PRESETS_BY_KEY.get(preset_key)
            if preset is None:
                return jsonify(error=f"Unknown preset '{preset_key}'"), 400
            time_range = preset.range
        elif max_hours_param:
            # Driven by the "time budget" slider: an explicit upper bound rather than parsed text.
            # The slider supplies its own display label since it knows the configured scale top.
            try:
                max_hours = math.inf if max_hours_param.lower() == "inf" else float(max_hours_param)
            except ValueError:
                return jsonify(error=f"Invalid max_hours value '{max_hours_param}'"), 400
            if not math.isinf(max_hours) and max_hours <= 0:
                return jsonify(error="max_hours must be greater than 0"), 400
            label = request.args.get("label", "").strip()[:60] or f"Up to {max_hours:g} hours"
            time_range = TimeRange(0, max_hours, label)
        else:
            time_range = parser.parse(query)
            if time_range is None:
                return jsonify(error="Couldn't understand that time budget. Try e.g. 'under 4 hours' "
                                     "or 'a weekend game'."), 400
        games = recommender.recommend(library.game_dicts(), time_range, metric=metric, include_hidden=include_hidden)
        return jsonify(range=time_range.to_dict(), games=add_thumbnail_info(games))

    @app.get("/thumb/<int:appid>")
    def thumbnail(appid: int):
        if appid <= 0:
            abort(404)
        path = thumbnails.resolve(appid) if allow_fetch else thumbnails.local_only(appid)
        if path is None:
            abort(404)
        return send_file(path, mimetype="image/jpeg", max_age=0)

    @app.post("/api/thumbnail/<int:appid>/override")
    def upload_thumbnail_override(appid: int):
        if appid <= 0:
            abort(404)
        file = request.files.get("image")
        if file is None or not file.filename:
            return jsonify(error="No image file provided"), 400
        content = file.read()
        try:
            path = thumbnails.set_override(appid, content)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except OSError:
            log.exception("Failed to save thumbnail override for app %s", appid)
            return jsonify(error="Could not save the thumbnail. Check available disk space and folder permissions."), 500
        return jsonify(ok=True, thumbnail=thumbnail_url(appid, path))

    @app.post("/api/game/<int:appid>/override")
    def api_game_override(appid: int):
        if appid <= 0:
            abort(404)
        data = request.get_json(silent=True) or {}
        hidden = bool(data.get("hidden", False))
        completed = bool(data.get("completed", False))
        raw_hours = data.get("completionist_hours")
        completionist_hours = None
        if raw_hours not in (None, ""):
            try:
                completionist_hours = float(raw_hours)
            except (TypeError, ValueError):
                return jsonify(error="completionist_hours must be a number"), 400
            if completionist_hours < 0:
                return jsonify(error="completionist_hours must not be negative"), 400
        user_overrides.set(appid, hidden=hidden, completed=completed, completionist_hours=completionist_hours)
        return jsonify(ok=True)

    return app


def main() -> None:
    cli = argparse.ArgumentParser(description="Steam Dial local library browser")
    cli.add_argument("--steam-root", default=DEFAULT_STEAM_ROOT, help="Steam install folder (read-only)")
    cli.add_argument("--cache", default=str(DEFAULT_CACHE), help="Path of the playtime cache JSON")
    cli.add_argument("--thumbnail-cache", default=str(DEFAULT_THUMBNAIL_DIR), help="Folder for downloaded thumbnails")
    cli.add_argument("--missing-playtime-log", default=str(DEFAULT_MISSING_PLAYTIME_LOG),
                      help="JSON file listing games with no completionist playtime estimate")
    cli.add_argument("--completionist-overrides", default=str(DEFAULT_COMPLETIONIST_OVERRIDES),
                      help="JSON file with manually supplied completionist_hours, used to patch cache gaps")
    cli.add_argument("--owned-games-cache", default=str(DEFAULT_OWNED_GAMES_CACHE),
                      help="JSON file caching the account's owned-games list from the Steam Web API")
    cli.add_argument("--user-overrides", default=str(DEFAULT_USER_OVERRIDES),
                      help="JSON file with per-game hidden/completed/completionist_hours overrides set via the edit panel")
    cli.add_argument("--steamgriddb-api-key", default=os.environ.get("STEAMGRIDDB_API_KEY"),
                      help="Optional SteamGridDB API key, used as a fallback thumbnail source")
    cli.add_argument("--steam-api-key", default=os.environ.get("STEAM_API_KEY"),
                      help="Optional Steam Web API key, used to fetch actual playtime and a fallback icon image")
    cli.add_argument("--host", default="127.0.0.1")
    cli.add_argument("--port", type=int, default=5000)
    cli.add_argument("--no-fetch", action="store_true", help="Never go online; use cached playtimes only")
    cli.add_argument("--browser", action="store_true",
                      help="Open in a regular browser tab instead of a native app window")
    args = cli.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    steam_root = Path(args.steam_root)
    cache = GameCache(Path(args.cache), forbidden_root=steam_root)

    steam_id64 = resolve_steam_id64(steam_root) if args.steam_api_key else None
    web_api_client = SteamWebApiClient(args.steam_api_key, steam_id64) if args.steam_api_key and steam_id64 else None
    if args.steam_api_key and steam_id64 is None:
        log.warning("STEAM_API_KEY is set but no local Steam profile was found under %s; "
                    "owned games and actual playtime won't be fetched", steam_root / "userdata")
    owned_games_cache = OwnedGamesCache(Path(args.owned_games_cache), web_api_client)
    actual_playtime = ActualPlaytimeService(owned_games_cache)

    completionist_overrides = CompletionistOverrides(Path(args.completionist_overrides))
    user_overrides = UserOverrides(Path(args.user_overrides))

    scanner = SteamLibraryScanner(steam_root, owned_games=owned_games_cache.snapshot)
    library = LibraryService(scanner, cache, actual_playtime, completionist_overrides, user_overrides)
    missing_playtime_log = MissingPlaytimeLogger(cache, Path(args.missing_playtime_log), completionist_overrides)
    enrichment = PlaytimeEnrichmentService(
        cache, default_provider(), on_run_complete=lambda: missing_playtime_log.refresh_async(library.games),
    )
    thumbnail_cache = ThumbnailCache(Path(args.thumbnail_cache), forbidden_root=steam_root)
    thumbnails = ThumbnailService(
        scanner, thumbnail_cache,
        default_thumbnail_provider(steamgriddb_api_key=args.steamgriddb_api_key, actual_playtime=actual_playtime),
    )

    games = library.rescan()
    if not args.no_fetch:
        enrichment.start(games)
    missing_playtime_log.refresh_async(games)

    # Its first check (run immediately on start) also refreshes the owned-games list from the Steam Web API.
    new_games_watcher = NewGamesWatcher(library, scanner, owned_games_cache if not args.no_fetch else None)
    new_games_watcher.start()

    app = create_app(library, scanner, enrichment, TimeBudgetParser(), GameRecommender(), thumbnails,
                     missing_playtime_log, actual_playtime, user_overrides,
                     new_games_watcher, allow_fetch=not args.no_fetch)

    if args.browser:
        log.info("Steam Dial running at http://%s:%d", args.host, args.port)
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    else:
        _run_in_window(app, args.host, args.port)


DEFAULT_ICON = RESOURCE_DIR / "assets" / "app-icon.ico"
WINDOW_STATE_PATH = DATA_DIR / "window_state.json"
WINDOW_MIN_SIZE = (900, 600)


def _load_window_size() -> tuple[int, int]:
    try:
        state = json.loads(WINDOW_STATE_PATH.read_text(encoding="utf-8"))
        width, height = int(state["width"]), int(state["height"])
        return max(width, WINDOW_MIN_SIZE[0]), max(height, WINDOW_MIN_SIZE[1])
    except Exception:
        return 1280, 860


def _save_window_size(window) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        WINDOW_STATE_PATH.write_text(
            json.dumps({"width": window.width, "height": window.height}), encoding="utf-8",
        )
    except Exception:
        log.exception("Failed to save window size")


def _run_in_window(app: Flask, host: str, port: int) -> None:
    """Serves Flask on a background thread and shows it in a native OS window (no browser tab/chrome)."""
    import webview

    if os.name == "nt":
        import ctypes
        # Gives the taskbar a distinct app identity, so it stops grouping/iconing this window under python.exe.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SteamDial.App")

    server = threading.Thread(
        target=app.run, kwargs={"host": host, "port": port, "debug": False, "use_reloader": False}, daemon=True,
    )
    server.start()
    log.info("Steam Dial running in its own window (http://%s:%d)", host, port)
    title = "Steam Dial"
    width, height = _load_window_size()
    window = webview.create_window(
        title, f"http://{host}:{port}", width=width, height=height, min_size=WINDOW_MIN_SIZE,
        background_color="#0e141b",
    )
    if window is not None:
        window.events.closing += lambda: _save_window_size(window)
        if os.name == "nt":
            window.events.shown += lambda: _enable_dark_title_bar(title)
            window.events.shown += lambda: _set_window_icon(title, DEFAULT_ICON)
    # private_mode defaults to True, which gives WebView2 a fresh, auto-deleted profile every
    # launch (PyInstaller onefile's changing _MEIPASS makes this worse) — localStorage prefs
    # like sort/filter selections would never survive a restart without this.
    webview.start(private_mode=False, storage_path=str(DATA_DIR / "webview_cache"))


def _enable_dark_title_bar(window_title: str) -> None:
    """Dark-themes the native window's title bar/border via the Windows DWM API (Windows 10 20H1+)."""
    import ctypes

    dwmapi = ctypes.windll.dwmapi
    hwnd = ctypes.windll.user32.FindWindowW(None, window_title)
    if not hwnd:
        return
    use_dark_mode = ctypes.c_int(1)
    for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE: 20 on Win10 20H1+, 19 on earlier builds
        if dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(use_dark_mode), ctypes.sizeof(use_dark_mode)) == 0:
            break


def _set_window_icon(window_title: str, icon_path: Path) -> None:
    """Sets the window/taskbar icon via WM_SETICON (pywebview's own `icon` start() arg is GTK/QT-only)."""
    import ctypes

    if not icon_path.is_file():
        return
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    hwnd = user32.FindWindowW(None, window_title)
    if not hwnd:
        return
    IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
    user32.LoadImageW.restype = ctypes.c_void_p
    hicon = user32.LoadImageW(None, str(icon_path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
    if not hicon:
        return
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
    user32.SendMessageW.restype = ctypes.c_void_p
    WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
    user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon)
    user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon)
    # The taskbar button reads the *class* icon, not just WM_SETICON, so set both.
    GCLP_HICON, GCLP_HICONSM = -14, -34
    user32.SetClassLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    user32.SetClassLongPtrW.restype = ctypes.c_void_p
    user32.SetClassLongPtrW(hwnd, GCLP_HICON, hicon)
    user32.SetClassLongPtrW(hwnd, GCLP_HICONSM, hicon)



if __name__ == "__main__":
    main()
