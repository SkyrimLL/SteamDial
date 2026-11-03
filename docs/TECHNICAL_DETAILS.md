
## Features

- Finds your Steam installation on its own (Windows registry, the per-platform default
  install paths, then a sweep of the usual "Steam lives on my games drive" locations), with a
  **Steam library folder** setting as the manual override when detection comes up empty. The
  Settings panel opens on startup whenever no usable library is found.
- Scans your local Steam install for installed games (via `appinfo.vdf` and library cache
  files) — strictly read-only, nothing under the Steam root is ever modified.
- Builds its catalog of *owned* (not just installed) games from the official Steam Web API
  (`IPlayerService/GetOwnedGames`) when a `STEAM_API_KEY` is configured — the authoritative
  source for what the account owns, replacing the old guesswork of inferring ownership from
  leftover local cache files. The result is cached in
  [owned_games_cache.json](data/owned_games_cache.json) so the catalog is available offline too.
- Shows each game's real Steam playtime (not just the "time to beat" estimate) once the Web
  API is configured, and logs games missing a completionist estimate to
  [missing_completionist.json](data/missing_completionist.json) so you can spot gaps in the cache.
- Lets you patch those gaps yourself in
  [missing_completionist_override.json](data/missing_completionist_override.json): fill in
  `completionist_hours` for a game and it's used wherever the estimate is missing (including
  recommendations), and it drops out of `missing_completionist.json`.
- Enriches games with playtime estimates (main/extra/completionist hours) from online
  providers, cached locally in [game_cache.json](data/game_cache.json) so lookups aren't repeated.
- Recommends games to play based on a time budget, via quick presets (Quick Session,
  Weekend Game, Epic Long Haul) or natural language queries like "under 4 hours" or
  "a weekend game".
- Shows game header art, resolved in priority order: a manual override image you supply,
  Steam's own local cache, a previously downloaded copy, then a fresh download (Steam CDN,
  then optionally SteamGridDB). Downloaded art is cached locally in `thumbnails/` so it's
  fetched at most once per game. A background warm-up resolves artwork for the whole library
  on startup (and after a rescan) rather than waiting for each card to ask for it, which keeps
  the **No thumbnail** filter from listing games that do in fact have artwork.
- Lets you fix broken or missing thumbnails yourself by dropping a replacement image in
  `thumbnails/overrides/`.
- Optional SteamGridDB fallback for games with no artwork on Steam's own CDN, enabled by
  setting an API key in `.env`.
- Toggle to include/exclude games you've hidden in Steam's library, dedicated **Completed games
  only** / **Hidden games only** views that list those games regardless of the visibility
  toggles, and a dropdown to pick
  which playtime figure (Main Story / Main + Extra / Completionist) drives sorting and
  recommendations.
- Can run fully offline using only cached data (`--no-fetch`).
- Lets you add local games Steam doesn't track (other stores, offline installs) by title
  alone; they are stored in [manual_games.json](data/manual_games.json), merged into the
  library on every scan, and edited through the same per-game panels as scanned games.
  Added games can also be renamed (keeping their artwork and overrides) or deleted, which
  clears everything stored under their AppID.

## Requirements

- Python 3.11+
- Dependencies in [requirements.txt](requirements.txt): `flask`, `requests`, `vdf`,
  `howlongtobeatpy`, `pywebview`, `python-dotenv`

Install with:

```powershell
pip install -r requirements.txt
```

### Optional: SteamGridDB fallback

For games Steam's own CDN has no header/capsule art for, Steam Dial can fall back to
[SteamGridDB](https://www.steamgriddb.com/). Get a free API key from
[your SteamGridDB preferences](https://www.steamgriddb.com/profile/preferences/api) and put
it in a `.env` file next to `steamdial.py` (loaded automatically at startup):

```
STEAMGRIDDB_API_KEY=your_key_here
```

`.env` is gitignored. Without a key, this source is simply skipped — everything else still
works.

### Optional: Steam Web API (owned-games catalog + real playtime)

Without a Steam Web API key, Steam Dial falls back to inferring ownership from leftover local
cache files, and only ever shows "time to beat" estimates. With a key, it instead fetches the
account's real owned-games list and actual playtime via `IPlayerService/GetOwnedGames`, keyed
to whichever local Steam profile under `userdata` was used most recently.

Get a free key from [steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey) and
add it to `.env`:

```
STEAM_API_KEY=your_key_here
```

The fetched catalog is cached in `data/owned_games_cache.json` (safe to delete; it's just a local
mirror of the API response) and refreshed in the background on every rescan.

## Styling

The UI's CSS is a pre-built Tailwind bundle ([assets/app.css](assets/app.css)) checked into the
repo — the app never loads anything from a CDN, so it works fully offline. If you change the
classes used in [templates/index.html](templates/index.html), rebuild it with the official
[Tailwind standalone CLI](https://tailwindcss.com/blog/standalone-cli) (no Node.js required):

```powershell
# One-time download (kept out of git; see tools/ in .gitignore)
Invoke-WebRequest -Uri "https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-windows-x64.exe" -OutFile tools\tailwindcss.exe

# Rebuild assets/app.css from assets/tailwind.src.css
.\tools\tailwindcss.exe -i assets\tailwind.src.css -o assets\app.css --minify
```

## Building a standalone executable

Package Steam Dial as a single `SteamDial.exe` (no Python install required to run it) with
[PyInstaller](https://pyinstaller.org/):

```powershell
pip install pyinstaller pyinstaller-hooks-contrib
pyinstaller steamdial.spec --clean
```

The result is `dist\SteamDial.exe`. On first run it creates `data\` and `thumbnails\` folders
(and reads an optional `.env`) next to the exe itself — nothing user-specific is baked into the
binary, and the templates/CSS bundled inside it are read-only. Copy `.env` next to the exe if you
want the SteamGridDB/Steam Web API keys, same as running from source.

### Startup splash screen

The exe shows a splash screen (`assets/android-chrome-512x512.png`) while it starts, because a
onefile build has to unpack itself and scan the Steam library before the window can appear. It is
declared as `Splash(...)` in [`steamdial.spec`](../steamdial.spec) and drawn by PyInstaller's
bootloader in embedded Tcl/Tk — so it is already on screen *before* Python starts. Building it
therefore needs a working `tkinter` (it is Windows/Linux only) and adds roughly 1–2 MB to the exe.

The splash never times out: [`splash_screen.py`](../splash_screen.py) wraps the bootloader's
injected `pyi_splash` module and is responsible for closing it. `create_splash_screen()` returns a
no-op implementation when `pyi_splash` is absent (running from source, or a build without a
splash), so startup code can post progress messages unconditionally. It is dismissed when the
window fires `loaded`, before `app.run()` in `--browser` mode, and from a `finally` guard so a
startup crash cannot strand an always-on-top splash.

To change the image, point `Splash(...)` at another **PNG or GIF** (Tk `PhotoImage` reads nothing
else) and move `text_pos` to suit its height.

## Usage

```powershell
python steamdial.py
```

By default this opens Steam Dial in its own window, scans `F:\Steam` for games, and
fetches missing playtime estimates online, caching results in `data/game_cache.json`.

### Options

| Flag | Description |
| --- | --- |
| `--steam-root PATH` | Steam install folder to scan. When omitted, the folder is resolved from `STEAMDIAL_STEAM_ROOT` (set in `.env` via **Settings**, or in the environment), falling back to auto-detection |
| `--cache PATH` | Path to the playtime cache JSON (default `game_cache.json` in the `data/` folder) |
| `--thumbnail-cache PATH` | Folder for downloaded thumbnails (default `thumbnails` next to the script) |
| `--owned-games-cache PATH` | Path to the owned-games catalog cache (default `owned_games_cache.json` in the `data/` folder) |
| `--missing-playtime-log PATH` | Path to the missing-completionist-estimate log (default `missing_completionist.json` in the `data/` folder) |
| `--completionist-overrides PATH` | Path to the hand-filled completionist-hours overrides (default `missing_completionist_override.json` in the `data/` folder) |
| `--manual-games PATH` | Path to the list of hand-added local games (default `manual_games.json` in the `data/` folder) |
| `--steamgriddb-api-key KEY` | SteamGridDB API key (default from `.env` / `STEAMGRIDDB_API_KEY` env var) |
| `--steam-api-key KEY` | Steam Web API key, enables the owned-games catalog and real playtime (default from `.env` / `STEAM_API_KEY` env var) |
| `--host HOST` | Host to bind the local server to (default `127.0.0.1`) |
| `--port PORT` | Port to bind the local server to (default `5000`) |
| `--no-fetch` | Never go online; use only cached playtime data and thumbnails |
| `--browser` | Open in a regular browser tab instead of the native app window |

### Examples

```powershell
# Point at a different Steam library and browse offline
python steamdial.py --steam-root "D:\Games\Steam" --no-fetch

# Use a classic browser tab instead of the native window
python steamdial.py --browser
```

## How it works

| Module | Responsibility |
| --- | --- |
| [steamdial.py](steamdial.py) | CLI entry point, Flask routes, and window/browser bootstrap |
| [steam_parser.py](steam_parser.py) | Read-only scanner that discovers games from Steam's library/appcache files, using the owned-games catalog (when available) for not-installed games |
| [steam_root.py](steam_root.py) | Locates and validates the Steam install folder (registry, per-platform defaults, drive sweep) and backs the **Steam library folder** setting |
| [appinfo_reader.py](appinfo_reader.py) | Binary parser for Steam's `appcache/appinfo.vdf` (formats v27-v29) |
| [steam_web_api.py](steam_web_api.py) | Client for the official Steam Web API (`GetOwnedGames`) — the authoritative owned-games/playtime source |
| [owned_games_cache.py](owned_games_cache.py) | Persists the Steam Web API's owned-games catalog to [owned_games_cache.json](data/owned_games_cache.json), loaded offline at startup and refreshed on rescan |
| [actual_playtime_service.py](actual_playtime_service.py) | Read-only per-game view over the owned-games catalog (real playtime, icon URL) |
| [library_service.py](library_service.py) | Combines scanned games with cached playtime data and real playtime for the API/UI |
| [new_games_watcher.py](new_games_watcher.py) | Background watcher that re-checks the Steam Web API/local files right after startup, after each rescan, and every 10 minutes, flagging newly found games to power the "Scan new games" button |
| [cache_manager.py](cache_manager.py) | Thread-safe JSON cache for playtime lookups, stored in the project folder |
| [enrichment.py](enrichment.py) | Background worker that fills the cache with playtime estimates |
| [missing_playtime_log.py](missing_playtime_log.py) | Writes [missing_completionist.json](data/missing_completionist.json), listing games with no completionist playtime estimate |
| [completionist_overrides.py](completionist_overrides.py) | Loads [missing_completionist_override.json](data/missing_completionist_override.json), a hand-filled-in copy of the missing-completionist report, to patch cache gaps |
| [user_overrides.py](user_overrides.py) | Persists the edit panel's per-game hidden/completed/completionist-hours/play-time overrides to [user_overrides.json](data/user_overrides.json) |
| [manual_games.py](manual_games.py) | Persists games added by hand to [manual_games.json](data/manual_games.json), assigning each an AppID above Steam's 32-bit range and never recycling one |
| [manual_game_service.py](manual_game_service.py) | Add/rename/delete use cases for those games, keeping the loaded library in step and clearing artwork, overrides and cached estimates on deletion |
| [playtime_providers.py](playtime_providers.py) | Playtime estimate sources (e.g. HowLongToBeat) behind a common interface |
| [recommender.py](recommender.py) | Maps time budgets (presets or natural language) to game recommendations |
| [thumbnail_cache.py](thumbnail_cache.py) | On-disk thumbnail cache (`thumbnails/`) plus the `overrides/` manual-fix folder |
| [thumbnail_providers.py](thumbnail_providers.py) | Thumbnail image sources (Steam CDN, SteamGridDB) behind a common interface |
| [thumbnail_service.py](thumbnail_service.py) | Resolves a thumbnail: override → Steam's cache → our cache → download |
| [thumbnail_warmup.py](thumbnail_warmup.py) | Background worker that pre-downloads artwork for every game, so "has thumbnail" is accurate before any card is rendered |
| [models.py](models.py) | Shared plain data structures (`SteamGame`, `PlaytimeEstimate`, `CacheEntry`) |
| [templates/index.html](templates/index.html) | Front-end UI |

## Notes

- `data/game_cache.json` is local data and safe to delete to force re-fetching playtime
  estimates; it is never written to the Steam installation.
- `data/owned_games_cache.json` is a local mirror of the Steam Web API's owned-games catalog,
  safe to delete (it's rebuilt on the next rescan if a `STEAM_API_KEY` is configured).
- `data/missing_completionist.json` is a generated report of games lacking a completionist
  playtime estimate, refreshed automatically after every rescan/enrichment run; safe to
  delete.
- `data/missing_completionist_override.json` is for you: copy an entry from
  `missing_completionist.json`, add a `completionist_hours` value you looked up manually,
  and it's used in place of the missing estimate everywhere (UI, recommendations) and the
  game is removed from `missing_completionist.json` on the next refresh.
- `thumbnails/` holds downloaded artwork plus a small `_missing.json` ledger of appids
  confirmed to have no artwork anywhere (retried automatically after 7 days). Both are
  local data, safe to delete, and never written to the Steam installation.
- `thumbnails/overrides/` is for you: drop an image named `<AppID>.jpg` (or `.jpeg`/`.png`/
  `.webp`) there to replace a broken or missing thumbnail; it always wins over every other
  source.
- Set `STEAMDIAL_STEAM_ROOT` as an environment variable to avoid passing `--steam-root`
  every time. The **Steam library folder** setting writes the same variable to `.env`;
  leaving it empty restores auto-detection.
