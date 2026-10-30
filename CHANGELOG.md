# Changelog

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
