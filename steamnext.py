"""SteamNext - a local, read-only Steam library browser with playtime-based recommendations.

Run:  python steamnext.py [--steam-root F:\\Steam] [--port 5000] [--browser]
"""
from __future__ import annotations

import argparse
import logging
import os
import threading
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, render_template, request, send_file

from cache_manager import GameCache
from actual_playtime_service import ActualPlaytimeService
from enrichment import PlaytimeEnrichmentService
from library_service import LibraryService
from missing_playtime_log import MissingPlaytimeLogger
from owned_games_cache import OwnedGamesCache
from playtime_providers import default_provider
from recommender import PRESETS, PRESETS_BY_KEY, GameRecommender, TimeBudgetParser
from steam_parser import SteamLibraryScanner
from steam_web_api import SteamWebApiClient, resolve_steam_id64
from thumbnail_cache import ThumbnailCache
from thumbnail_providers import default_provider as default_thumbnail_provider
from thumbnail_service import ThumbnailService

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DEFAULT_STEAM_ROOT = os.environ.get("STEAMNEXT_STEAM_ROOT", r"F:\Steam")
DEFAULT_CACHE = BASE_DIR / "game_cache.json"
DEFAULT_THUMBNAIL_DIR = BASE_DIR / "thumbnails"
DEFAULT_MISSING_PLAYTIME_LOG = BASE_DIR / "missing_completionist.json"
DEFAULT_OWNED_GAMES_CACHE = BASE_DIR / "owned_games_cache.json"

log = logging.getLogger("steamnext")


def create_app(
    library: LibraryService,
    scanner: SteamLibraryScanner,
    enrichment: PlaytimeEnrichmentService,
    parser: TimeBudgetParser,
    recommender: GameRecommender,
    thumbnails: ThumbnailService,
    missing_playtime_log: MissingPlaytimeLogger,
    actual_playtime: ActualPlaytimeService,
    owned_games_cache: OwnedGamesCache,
    allow_fetch: bool = True,
) -> Flask:
    app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))

    @app.get("/")
    def index():
        return render_template("index.html", presets=PRESETS)

    @app.get("/api/games")
    def api_games():
        return jsonify(games=library.game_dicts(), status=enrichment.status())

    @app.get("/api/status")
    def api_status():
        return jsonify(enrichment.status())

    @app.post("/api/rescan")
    def api_rescan():
        games = library.rescan()
        if allow_fetch:
            enrichment.start(games)
            owned_games_cache.refresh_async()
        missing_playtime_log.refresh_async(games)
        return jsonify(count=len(games), status=enrichment.status())

    @app.get("/api/recommend")
    def api_recommend():
        preset_key = request.args.get("preset", "").strip()
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
        else:
            time_range = parser.parse(query)
            if time_range is None:
                return jsonify(error="Couldn't understand that time budget. Try e.g. 'under 4 hours' "
                                     "or 'a weekend game'."), 400
        games = recommender.recommend(library.game_dicts(), time_range, metric=metric, include_hidden=include_hidden)
        return jsonify(range=time_range.to_dict(), games=games)

    @app.get("/thumb/<int:appid>")
    def thumbnail(appid: int):
        if appid <= 0:
            abort(404)
        path = thumbnails.resolve(appid) if allow_fetch else thumbnails.local_only(appid)
        if path is None:
            abort(404)
        return send_file(path, mimetype="image/jpeg", max_age=7 * 24 * 3600)

    @app.post("/api/thumbnail/<int:appid>/override")
    def upload_thumbnail_override(appid: int):
        if appid <= 0:
            abort(404)
        file = request.files.get("image")
        if file is None or not file.filename:
            return jsonify(error="No image file provided"), 400
        content = file.read()
        try:
            thumbnails.set_override(appid, content)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(ok=True)

    return app


def main() -> None:
    cli = argparse.ArgumentParser(description="SteamNext local library browser")
    cli.add_argument("--steam-root", default=DEFAULT_STEAM_ROOT, help="Steam install folder (read-only)")
    cli.add_argument("--cache", default=str(DEFAULT_CACHE), help="Path of the playtime cache JSON")
    cli.add_argument("--thumbnail-cache", default=str(DEFAULT_THUMBNAIL_DIR), help="Folder for downloaded thumbnails")
    cli.add_argument("--missing-playtime-log", default=str(DEFAULT_MISSING_PLAYTIME_LOG),
                      help="JSON file listing games with no completionist playtime estimate")
    cli.add_argument("--owned-games-cache", default=str(DEFAULT_OWNED_GAMES_CACHE),
                      help="JSON file caching the account's owned-games list from the Steam Web API")
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

    scanner = SteamLibraryScanner(steam_root, owned_games=owned_games_cache.snapshot)
    library = LibraryService(scanner, cache, actual_playtime)
    missing_playtime_log = MissingPlaytimeLogger(cache, Path(args.missing_playtime_log))
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
        owned_games_cache.refresh_async()
    missing_playtime_log.refresh_async(games)

    app = create_app(library, scanner, enrichment, TimeBudgetParser(), GameRecommender(), thumbnails,
                     missing_playtime_log, actual_playtime, owned_games_cache, allow_fetch=not args.no_fetch)

    if args.browser:
        log.info("SteamNext running at http://%s:%d", args.host, args.port)
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    else:
        _run_in_window(app, args.host, args.port)


DEFAULT_ICON = BASE_DIR / "assets" / "app_icon.ico"


def _run_in_window(app: Flask, host: str, port: int) -> None:
    """Serves Flask on a background thread and shows it in a native OS window (no browser tab/chrome)."""
    import webview

    server = threading.Thread(
        target=app.run, kwargs={"host": host, "port": port, "debug": False, "use_reloader": False}, daemon=True,
    )
    server.start()
    log.info("SteamNext running in its own window (http://%s:%d)", host, port)
    title = "SteamNext"
    window = webview.create_window(
        title, f"http://{host}:{port}", width=1280, height=860, min_size=(900, 600), background_color="#0e141b",
    )
    if window is not None and os.name == "nt":
        window.events.shown += lambda: _enable_dark_title_bar(title)
        window.events.shown += lambda: _set_window_icon(title, DEFAULT_ICON)
    webview.start()


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



if __name__ == "__main__":
    main()
