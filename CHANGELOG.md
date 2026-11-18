# Changelog

## [v2.0.3] - 2026-11-17 — bug fixes + community buttons

New suite: `tests/test_v203.py` (7 checks, offline). Each bug reproduced RED
with shipped code, fixed, re-run GREEN. Old suite `tests/test_bugs.py` still
13/13.

### Added

- Header **YouTube** button (red play-badge glyph) opens the main channel
  [@TheNewGamePluss](https://www.youtube.com/@TheNewGamePluss) in the default browser.
- Header **Ko-fi** button (cup glyph) opens
  [ko-fi.com/thenewgameplus](https://ko-fi.com/thenewgameplus) for donations.
- Both glyphs are SVG masters under `assets/svg/` (recolor-friendly), rasterised to
  `assets/youtube_24.png` / `kofi_24.png` by `tools/write_glyph_svgs.py` +
  `tools/compose_glyphs.py`; buttons fall back to plain text labels if Pillow or a
  glyph is missing, so the links can never vanish.
- App now self-identifies as v2.0.3 (header, `__version__`, Windows version resource).

### Fixed

- `read_sfo_strings` callers missed real card data (scanner `_folder_game_info`): RPCS3 names installed game-data folders by content id (`BCUS98174GDAI32`), whose first 9 chars ARE the title id — the old code uppercased the whole folder name, producing an invalid serial that could only fail lookup and duplicated the same game as a second card; disc-layout SFOs live inside `PS3_GAME/PARAM.SFO`, which was never read, leaving those cards nameless. Serial now falls back to the embedded title id, the nested SFO is read, and unsalvageable folder names are dropped. This was the "New observation (candidate v2.0.3 fix)" in `BUGFIXES-v2.0.2.md`.
- `download_batch` (downloader) reported the PLANNED destination path instead of the file `download_pkg` actually saved (URL basename; the two differ for DRM-free entries, "DRM-Free <file>"). The GUI filters completed paths with `is_file()` before offering the RPCS3 installer, so DRM-free-only downloads never offered install and the activity log named a file that did not exist. Results now carry the real path.
- `make_placeholder` (artwork) crashed with `UnboundLocalError` when `Draw.textbbox` raised: the `except` fallback positioned text with `bbox[0]/bbox[1]`, but `bbox` was only bound inside the `try` that had just failed. `bbox` now defaults before the try, so the initials placeholder still renders on any font-metrics failure.

### Checked, no defect found (round 3)

- PSN XML dedupe, fetch_updates/fetch_meta contracts, TMDB HMAC key, config load/save guards, download truncation guard, sha1 warn-but-keep policy, engine stop/cancel + byte banking, _MEIPASS DLL-dir hygiene, GUI event-pump guards, version sorting (leading-zero strings vs int keys), is_valid_serial vs fetch_updates regex parity.

### Notes

- `_installed_version_from_folder` returns the highest version directory present and deliberately ignores update-only folders lacking PARAM.SFO — correct, covered by design.

Shipped as GitHub release **v2.0.3** (2026-11-17) with asset `ps3-rpcs3-game-updater.exe` (SHA-256 `4cea8712bc5a60c685e5b75ca0bc16abae8c83794db6bb652a16aa9072187da0`); release notes embed the new header and game-details screenshots.

## [v2.0.2] - 2026-10-30 — bug-hunt fixes, 2026-10-30

- App now self-identifies as v2.0.2: GUI header, `__version__`, and embedded
  Windows version resource (FileVersion/ProductVersion 2.0.2.0)

Three real defects found by code audit + synthetic reproduction tests (suite in `tests/test_bugs.py`, 13 checks incl. live Sony/TMDB probes). Full analysis: `BUGFIXES-v2.0.2.md`.

### Fixed

- `read_sfo_strings` (scanner): the v2.0.1 probe only understood entry tables at offset 32 with a 3-int entry read. Canonical PS3 `PARAM.SFO` files keep their entry table at `header_size` (0x80) with 46-byte entries laid out format/flag/offset/size, and `data_start` of any game with ≥9 entries exceeds the old 512-byte cap — all of those still returned `{}`. Layout is now probed from the header fields (base + stride candidates) with a 4-int canonical entry read first; a validating layout is still never guessed. Regression tests: 4-entry and 20-entry canonical fixtures + the 32-byte-header variant.
- `download_pkg` (core): a CDN that closes the stream cleanly after a short body left a truncated `.pkg` renamed to the final name and reported **ok** when Sony's listing carried no SHA-1 — the user then installs a broken package in RPCS3. Completed-byte count is now checked against `Content-Length` in the no-SHA-1 case; short files are deleted and reported `incomplete download (done/total bytes)`.
- `load_config` (config): the v2.0.1 fix covered the save path only. A hand-edited `config.ini` with non-numeric `max_downloads` (`ValueError`) or duplicate sections (`DuplicateSectionError`) crashed the GUI at startup with no recovery. Reads are now guarded and fall back to defaults.

### Added

- `tests/test_bugs.py`: regression suite reproducing each bug (including a reconstruction of the shipped v2.0.1 SFO parser asserting it fails the valid fixture) plus live PSN/TMDB endpoint probes. Run: `python tests/test_bugs.py` (needs `requests`, `yaml`).

## v2.0.1 — 2026-10-11

Hotfix release: library scanning, title lookup, and the GUI search/filter flow.

### Fixed

- `read_sfo_strings` now uses the canonical classic SFO layout (32-byte header, entry table at offset 32) with validating entry probes, so real game `PARAM.SFO` files decode instead of always returning `{}`.
- `games.yml` parsing handles RPCS3's actual `game:`-rooted format (flat and nested entries) in both the PyYAML path and the built-in fallback parser.
- CLI progress callback no longer raises `ZeroDivisionError` for zero-length/unknown-size packages.
- GUI tile download progress now reflects actual bytes across packages (per-package size differences no longer skew the bar).
- The empty-state box is tracked by reference instead of the fragile `nametowidget("empty_state")` lookup.
- Settings save no longer crashes on non-numeric "max downloads" values; removed a dead `if True` expression.
- The `xdg-open` fallback is guarded so opening a download folder cannot crash the GUI on systems without an opener.
- Missing `requests` now raises `ImportError` instead of exiting at import time (the module is imported by the GUI and `updater` package).
- Malformed `size` attributes in Sony XML are handled without crashing; skipped "already present" downloads now verify the existing file's SHA-1 against Sony's listing (warn-but-keep, matching the documented policy).
- urllib3 warning silencing uses the modern `import urllib3` path, with the deprecated shim as fallback.
- Unexpected exceptions no longer kill the GUI silently: handler failures in the event pump are caught, shown in the in-app log, and written to `errors.log` in the app data folder; worker lookups surface as `lookup_error`; windowed builds now record uncaught main/worker-thread errors (a windowed exe previously had no console, so any such failure vanished).
- `requirements.txt` includes optional `psutil`; the PyInstaller spec adds it to hidden imports.
- Library title search matches the resolved display name (`engine.names`), so a game reachable only through remote lookup is found by typing its title once — previously the search matched only local/folder names, so a not-installed title had to be typed in both the search box and the Title ID box. Checking a Title ID also clears the search filter, so the card you just looked up is always visible.
- Checking a Title ID now jumps to that game: it clears both the text filter and the status filter, pins the checked title so its card stays visible, and scrolls the card into view. Previously any status filter hid a checked title, because a card whose lookup state is still unknown (`checking`) matches no filter, so the card never appeared until the grid was rebuilt by searching.
- A lookup that finishes now builds the missing card instead of only refreshing an existing one (`_refresh_tile` no-ops for a serial with no tile).
- The search box accepts a nine-character Title ID and performs the lookup, so a title that is not installed can be found from the search box alone; text queries still match the library.
- A rescan no longer drops titles checked by Title ID. The scan replaced the whole library, so a card for a game that is not in `games.yml` — with its resolved name and artwork — disappeared as soon as the scan finished. Manual entries are now preserved, and the update state for them is no longer pruned.

### Added

- New **Installed** filter: shows only games present in the scanned RPCS3 library (`GameEntry.path` non-empty), separating local titles from titles resolved by Title ID.

- Remote cover art for titles not installed in RPCS3 (`ArtworkResolver.resolve_remote_by_serial`) resolves through Sony's public keyless TMDB endpoint via `updater.psn` — cache-first, throttled, misses memoized, never raises. The originally planned PlayStation Store web API paths were probed live (2026-10) and are dead (404/403), so they were removed.
- `lookup_with_meta` no longer gates metadata/box-art enrichment on published updates existing: zero-update titles (e.g. NPUB31689, Resident Evil 0) now show their display name and box art.
- The `art_ready` event promised in `updater/engine.py`'s contract is now actually emitted, and the GUI applies remotely resolved art to tiles.

[v2.0.1](https://github.com/Johnzx07/ps3-rpcs3-game-updater/releases/tag/v2.0.1) — executable SHA-256 `5a1cabbd351f6c3128623f52b4e213a9f034584263a04c87b1c7c9a4de7e74cb`.

## v2.0.0 — 2026-09-12

This is the full v2 replacement release of **PS3 RPCS3 Game Updater**.

### New experience

- Rebuilt desktop UI with compact artwork cards, filters, title search, title-ID lookups, update state, per-game progress, and an activity log.
- Added an in-window game-details sidebar. Clicking a card or **Details** shows versions, package sizes, available updates, download controls, and the package folder shortcut.
- Added a title-based local library search while keeping the exact title-ID updater lookup.

### Downloads and RPCS3 installation

- Added direct per-game package-folder access.
- Added a post-download confirmation before launching RPCS3’s native package installer.
- Added single-instance-aware install queuing: a confirmed install waits for an already-open RPCS3 to close, then launches automatically without causing RPCS3’s second-instance failure.
- Protected the RPCS3 child process from the frozen updater’s temporary DLL directory, ensuring it uses the correct system/RPCS3 Visual C++ runtime.
- Documented the native RPCS3 game-scan and package-review stages with screenshots.

### Project cleanup

- Renamed the main implementation module, launcher, spec file, executable, configuration identity, and documentation for `ps3-rpcs3-game-updater`.
- Removed the original v1-only GUI test, parser test, setup guide, contribution guide, and test matrix in favor of the v2 release documentation.

[v2.0.0](https://github.com/Johnzx07/ps3-rpcs3-game-updater/releases/tag/v2.0.0)
