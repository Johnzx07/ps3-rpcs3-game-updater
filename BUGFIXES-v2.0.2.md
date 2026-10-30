# Bug-hunt findings — 2026-10-30 (v2.0.2)

Code audit of the full source (3,455 Python lines) against synthetic reproduction
tests and live Sony/TMDB endpoint probes. No test suite shipped, so a regression
suite was written: `tests/test_bugs.py` (13 checks; run `python tests/test_bugs.py`,
needs `requests` + `PyYAML`; hits live endpoints for 3 probes). Each bug below was
reproduced RED with the shipped code, then fixed and re-run GREEN.

Scope checked: `ps3_rpcs3_game_updater.py` (PSN XML, TMDB HMAC, download/SHA-1,
CLI), `gui_app.py` (full), `updater/` (scanner, engine, artwork, psn, downloader,
models, config), launcher `.bat`, `.spec`, CHANGELOG.

---

## Bug 1 — every real PS3 `PARAM.SFO` still failed to decode (scanner.py `read_sfo_strings`)

**What it did.** The v2.0.1 hotfix entry claims real game `PARAM.SFO` files decode;
they did not (except in the one layout the probe assumed). Library scanning from
`dev_hdd0/game` folders therefore lost the game title and the SFO serial: cards
fell back to folder names / serial-only display, and the folder-scan half of the
library merge was blind.

**Root cause (three compounding).**
1. The probe hard-coded the entry table at offset **32**; canonical PS3 games store
   it at `header_size` (**0x80**), after 160 bytes of header padding.
2. It read **3 ints** after the 16-byte key (`<III` = format, a, b); the canonical
   entry is format(u16)+pad+flag(u32)+offset(u32)+**size**(u32) — four fields, so
   `size` was never read and offset/size mapping was impossible.
3. The validation gate `25 <= entry_size <= 512` used header field 3 = `data_start`
   as a stride bound; any game with ≥9 entries has `data_start` = 0x80+n*46 > 512
   and was rejected outright.

**Reproduction.** `tests/test_bugs.py::t_sfo_original_shipped_fails` reconstructs
the shipped v2.0.1 parser and asserts it returns `{}` for a built-faithful
canonical fixture (entry table @0x80, stride 46, fmt/flag/off/size, integer
entries stored inline); `t_sfo_canonical` (4 entries), `t_sfo_canonical20`
(22 entries, data_start 1140 > 512) and `t_sfo_legacy32` (32-byte-header
producer variant) cover the fixed parser. Fixture magic must be the byte string
`SFB\0` (`int.from_bytes(b"SFB\x00", "little")`) — packing the magic as a u32
from the wiki hex value byte-swaps it and the parser correctly rejects it.

**Fix.** Probe (base, stride) candidate pairs derived from the header fields
(`header_size`/`entry_stride` canonical first, legacy 32/`data_start` kept) and a
new 4-int canonical entry read, canonical order first. String entries now require
a non-zero, in-bounds payload (a zero `size` means the fields were read from the
wrong offsets, i.e. a wrong layout — this kills the old probe's silent
empty-string acceptance); integer entries (value stored inline) are skipped
without bounds checks. Return contract unchanged: {} on failure, never raises.

## Bug 2 — truncated `.pkg` accepted as complete (core `download_pkg`)

**What it did.** When Sony's XML carries no `sha1sum` for a package (the SHA-1
check is skipped in that case) and the CDN closes the connection cleanly after a
short body, the streaming loop ended without an exception: the partial file was
renamed from `.part` to the final `.pkg` and reported **ok**. The user sees
"Downloaded 1/1" and installs a corrupt package in RPCS3. The "already present"
size re-check on the next run would eventually force a re-download, but the
first install attempt is silently broken.

**Reproduction.** `t_download_truncated`: `requests.get` stub streams 500 KB of a
declared 1,000,000-byte body with a clean close → shipped code returned
`ok=True ("ok")`. `t_download_complete` guards the full-file path.

**Fix.** After streaming, when no SHA-1 is on record, completed bytes are compared
against `Content-Length`; a short file is deleted and reported
`incomplete download (done/total bytes)`. Files with a SHA-1 keep the documented
warn-but-keep policy for Sony's stale hashes.

## Bug 3 — corrupted `config.ini` bricked GUI startup (config `load_config`)

**What it did.** The v2.0.1 changelog claims the non-numeric "max downloads"
crash was fixed — only the *save* path was. A hand-edited
`%APPDATA%\ps3-rpcs3-game-updater\config.ini` with `max_downloads = lots` raised
`ValueError` inside `configparser.getint`, and a duplicated `[settings]` section
raised `DuplicateSectionError`, both at `load_config()` during app construction:
the windowed exe dies instantly, no window, no recovery, and the bad file is
never rewritten.

**Reproduction.** `t_config_garbage`: writes each corrupt file directly under a
temporary config dir and asserts `load_config()` returns defaults without raising.

**Fix.** `cfg.read` is wrapped (any parse error → fresh defaults) and the
`getint` fallback is guarded; defaults apply and the next `save_config` rewrites
a clean file.

---

## Checked, no defect found (live-verified 2026-10-30)

- **PSN XML / dedupe**: real `*-ver.xml` for BCUS98148 parses to 13 pkgs, no
  duplicate URLs; `<package url>` attributes are absolute `http://` URLs
  (requests handles them; no PySN-style relative-URL join is needed).
  BLUS30089 returns exactly 1 update. TMDB keyless HMAC `fetch_meta` resolves
  names + icons. games.yml parses in both the PyYAML path and the fallback
  parser (flat + nested entries).
- **Engine/GUI**: stop/cancel paths, done_before byte banking, pinned-title
  filter behavior, RPCS3 single-instance install queuing, `_MEIPASS` DLL-dir
  hygiene, and the event pump guards all read coherently. No CI exists in the
  repo; `tests/test_bugs.py` is the first automated suite.

## Files changed (this fix set)

- `updater/scanner.py` — `read_sfo_strings` header-aware probing (Bug 1)
- `ps3_rpcs3_game_updater.py` — `download_pkg` truncation guard (Bug 2),
  `load_config` robustness (Bug 3)
- `tests/test_bugs.py` — new regression suite (13 checks, 3 live probes)
- `CHANGELOG.md` — v2.0.2 section

---

## Build + app test — 2026-10-30

**Build:** PyInstaller 6.22.2 on Python 3.14.5 (`py -3`), shipped `.spec` unmodified.
Artifact: `dist\ps3-rpcs3-game-updater.exe`, 41,605,139 bytes,
SHA-256 `c989a7c9f723525eff1bf3ca9598fb238d8d7c3e5bcadb3f6a95a5e10e9cc4fc`.
This build embeds a Windows version resource (FileVersion/ProductVersion **2.0.2.0**,
FileDescription "PS3 RPCS3 Game Updater") via `version_info.txt` + spec `version=`;
GUI header shows **v2.0.2** (`APP_VERSION`, gui_app.py:68; `__version__`, updater/__init__.py).

**v2.0.2 versioned rebuild (17:29, supersedes the 17:25 unversioned `c989a7c9…`):**
`dist\ps3-rpcs3-game-updater.exe` 41,605,344 bytes,
SHA-256 `2a4e34fd3be8dea7c2f6fe435d68bfd9a15c9e536999727bab519901f67b128a` — built in the
project tree with the shipped spec, deployed to `dist\` (the v2.0.1 exe kept beside it
as a backup). Verified: VersionInfo on disk,
13/13 suite green on the patched tree, app launched and header read on screen as v2.0.2.

**Packaged-app test on live desktop (PASS):** exe launched (window "PS3 RPCS3 Game
Updater"), scanned a real RPCS3 install (30+ installed titles), remote title
lookup filled card names, update states computed against live Sony endpoints,
status bar reached the Done state, artwork rendered (PIL/Tkinter), default
`~/PS3Updates` + artwork cache used correctly (paths from the app's own config).

**Download engine test, same shared module, CLI live (PASS):** `--cli BLUS30089`
downloaded `UP0001-BLUS30089_00-ASSASSINPATCH001-PE.pkg` (25,530,528 bytes =
24.3 MB, 100%), SHA-1 verified on disk `c667f9f5c7166c0714880e493dfe8963a67531cf`
— Sony's listing hash is stale (documented warn-but-keep policy applied), tidy
`PlayStation 3\[BLUS30089] Assassins Creed 110\` folder layout.

**UNVERIFIED (automation limits, not app failures):** clicking a tile's Update
button in the GUI via desktop automation — tkinter canvas widgets expose no UIA
Invoke pattern and the driver's foreground escalation failed on the multi-monitor test desktop
(target window never became foreground), so the GUI-triggered download +
install-queue path was not exercised by machine. **RESOLVED 2026-10-30 — verified end-to-end by the maintainer on a real RPCS3 install:**
clicked Update on a title with an available patch; the app downloaded the package to
the configured downloads folder (`PlayStation 3\<title folder>\`) and the in-app install
helper installed it into the RPCS3 `dev_hdd0\game\<serial>\` folder (PARAM.SFO /
PS3LOGO.DAT / TROPDIR / USRDIR rewritten). Full GUI loop confirmed on real data: scan -> live lookup -> download ->
install. Nothing left unverified. It is the same
`request_download -> download_pkg` path the CLI live test proved.
Automation clicks at pixel coordinates on this multi-monitor/DPI desktop are
land-risky — one opened an unrelated desktop app during testing
(nothing ran; system logs confirmed zero uninstall activity). Future
GUI automation of this app should use keyboard input after focus_app, or
coordinate clicks with verified foreground.

**New observation (pre-existing, not fixed here):** the folder-scan serial
derivation concatenated two serials for one dev_hdd0 entry
(`BCUS98174GDAI32`) -> card shows red "Lookup failed: Invalid serial format".
Cosmetic on the card; lookup gracefully fails per contract. Candidate v2.0.3 fix.

