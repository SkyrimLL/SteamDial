# Steam Dial

How much time do you have?

SteamDial is a local, read-only Steam library browser that recommends what to play next based on how
much time you have, using "time to beat" estimates (e.g. HowLongToBeat).

It can also show you only much your play time compares to the estimated play time of a game.

It runs a small Flask app served either in its own native window (via `pywebview`) or in
a regular browser tab, and never writes to your Steam installation.

<img width="1964" height="1673" alt="screenshot" src="https://github.com/SkyrimLL/SteamDial/blob/5f7e387f350a914509b0afbde18420efaca88bb5/docs/screenshot.jpg" />



## Getting started

Launch Steam Dial (either `python steamdial.py` from source, or `SteamDial.exe` if you're
using the packaged build). On first run it scans your local Steam library and opens in its
own window, no setup required — everything below is optional, on top of that default
experience.

## Setting up API keys

Click the gear icon in the top-right corner to open **Settings**. Two keys are supported,
both optional and both free:

- **Steam Web API key** — lets Steam Dial fetch your real owned-games list and actual
  playtime from Steam directly, instead of guessing from local files. Get one from
  [steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey).
- **SteamGridDB API key** — fills in box art for games that Steam's own servers don't have
  artwork for. Get one from your
  [SteamGridDB account preferences](https://www.steamgriddb.com/profile/preferences/api).

Paste the key(s) in, hit **Save**, then restart Steam Dial for them to take effect.

## Running a scan

Steam Dial scans your library automatically on startup, then right away (and every few
minutes after that) checks the Steam Web API and local files in the background for newly
bought games. When new games
are found, an orange **Scan new games** button appears next to **Rescan**; click it to add
just those games without re-scanning your whole library. Use **Rescan** instead if you've
installed/uninstalled/hidden games and want to refresh the full list — this also re-checks
for new playtime estimates and artwork in the background (shown by the progress bar under
the search bar).

## Opening games

Hover over a game card to reveal **Steam** and **HowLongToBeat** buttons over its artwork.
**Steam** opens the game's library page in the Steam client; **HowLongToBeat** opens a
search for the game in your browser. The buttons also appear when tabbing into a card
with the keyboard, and stay visible on touch devices. Clicking elsewhere on a card no
longer opens Steam.

## Fixing missing thumbnails

Hover over any game card and click the small pencil icon on its artwork to open the
thumbnail editor. Pick an image from your computer, pan and zoom it to fit, then save — your
image always takes priority over anything Steam Dial would have found on its own. Use the
**No thumbnail** filter (see below) to quickly find every game still missing artwork.
Saved replacements persist across sorting, filtering, recommendations, rescans, and restarts.
They are stored in `thumbnails/overrides/` beside the script (or executable); replacing an
image updates its URL so the browser does not reuse previously cached artwork.

## Sorting and filtering

- **Search** — filter the library by game name.
- **Status dropdown** — show all games, or narrow to Installed, Not installed, No playtime
  data, or No thumbnail.
- **Sort dropdown** — order by Name, Shortest/Longest first, Time left (shortest first), or
  Installed first.
- **Playtime basis dropdown** — switch between Main Story, Main + Extra, and Completionist
  hours; this changes both the hours shown on each card and what "shortest/longest first"
  sorting is based on.
- **Include hidden games** — also show games you've hidden in your Steam library.
- **"How much time do you have?"** — pick a preset (Quick Session, Weekend Game, Epic Long
  Haul), type a time budget in plain language (e.g. "under 4 hours", "a weekend game"), or
  drag the slider to update your recommendations. The slider ranges from less than 1 hour
  through budgets of up to 60 hours, with a final open-ended **More than 60 hours** option.

For install instructions, command-line flags, and a breakdown of the codebase, see
[docs/TECHnICAL_DETAILS.md](docs/TECHNICAL_DETAILS.md).


## DISCLAIMER

Steam Dial was developed to fill a gap in the official Steam client (the absence of filters by average time to play or complete games).

It has been developed with Claude and GitHub Copilot to save time, and has been tested on Windows 11.

This has no affiliation to Steam or Valve, and is in no way an official product.

It is subject to break any time Valve makes a significant change to the Steam Desktop client.
