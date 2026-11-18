Two community buttons up in the header where you can see them, and three bugs found by sitting down with my own code and hunting — the same way v2.0.2 got its fixes. None of them could brick anything, but two of them made the app *look* wrong or *act* lazy at exactly the moments you rely on. Here's everything, in plain English, with honest grades for how bad each bug actually was.

## See it running

![The library view with the new YouTube and Ko-fi buttons in the header](https://github.com/Johnzx07/ps3-rpcs3-game-updater/releases/download/v2.0.3/screenshot-main.png)

*Real library, real updates found: MAG waiting on 02.12, Resistance 3 on 01.20 (+3 earlier). Top-right, left of Settings, the two new buttons — red play badge and Ko-fi cup, both hand-drawn as SVG masters (in `assets/svg/`), rasterized to app-sized PNGs. If a machine can't load the images, the buttons fall back to plain text labels: the links physically cannot disappear.*

![A game card clicked open — details sidebar on the right](https://github.com/Johnzx07/ps3-rpcs3-game-updater/releases/download/v2.0.3/screenshot-details.png)

*Click any card (or its Details button) for the side panel: title, serial, installed version, latest patch found on Sony's servers, size, source folder, and the update list with Download / install-in-RPCS3 buttons right below.*

## How bad were these bugs?

The same scale as v2.0.2, so the grades mean the same thing:

- **A** — your-weekend-killer: crashes on open, wrong files installed, data at risk.
- **B** — feature-lie: something you use quietly stopped working or shows the wrong thing. Workaround exists, but you had to know.
- **C** — landmine: hadn't gone off yet, probably never would have, but it sat on a path I trust.

| # | Bug | Grade | How bad |
|---|-----|-------|---------|
| 1 | Fake duplicate game cards with red "Lookup failed" | **B** | 6.5/10 |
| 2 | "Install in RPCS3 now?" offer skipped for DRM-free updates | **B−** | 5.5/10 |
| 3 | Crash hiding inside the no-cover fallback | **C+** | 3/10 |

Nothing scored an A this round. The only A this app has ever earned was the v2.0.2 game-ID parser. These three are its smaller cousins.

## Bug 1 — the library showing the same game twice, one copy with a red error (B)

**What you saw:** an installed game got its own card reading `Lookup failed: Invalid serial format`, often as a second entry for a game already listed properly. Sorted alphabetically, that twin wasted a row and made you hunt.

**What was actually going on:** the scanner took the game folder's name as the Title ID as-is. Games whose folder is named like `BCUS98174GDAI32` (Sony's 9-char title + a 6-char suffix glued on) made the app ask the PSN servers about a 15-character thing they have never heard of — the card went red. And games whose ID hides in the nested `PS3_GAME\PARAM.SFO` weren't opened at all, so those cards had no title either.

**Fix:** read the nested PARAM.SFO when the root one is missing; salvage the first 9 chars of a folder name that starts with a valid ID; skip folders with nothing salvageable instead of minting a doomed card. One game = one card, and the twin merges with the good entry automatically.

## Bug 2 — the "install in RPCS3 now?" offer just never showed up (B−)

**What you saw:** you downloaded a DRM-free update, the progress bar finished… and that was it. No install offer. The log named a file that wasn't there. You'd drag the .pkg into RPCS3 yourself like in 2019 and never know the feature existed for you.

**What was actually going on:** pure paperwork. The batch planner promised the file would be saved as `DRM-Free something.pkg` but the downloader always saves under the URL's own name. Right before offering the install, the app checks which finished files actually exist — and for DRM-free updates the name it checked never existed, so the check filtered everything out and the prompt quietly never fired. Regular updates kept the offer because their planned name happened to match.

**Fix:** the batch now reports the path actually written, so the existence check passes and the prompt returns. Your download was always byte-for-byte correct — only the helpful ending was missing. But this feature exists precisely for the one-tap crowd, and DRM-free updates are what people go out of their way to grab.

## Bug 3 — the crash that lives in the code meant to catch crashes (C+)

**What you saw:** nothing. Yet. This is the honest entry: a latent defect, not a user report.

**What was actually going on:** when a game has no cover art, the app paints a dark tile with the game's initials using Windows fonts. If the font-metrics call itself fails, the drawing code jumps to a rescue branch — which used the size values the failed call was supposed to provide. The fallback referenced a variable that never got assigned: a crash while trying to avoid a crash.

**Fix:** give the size values a sane default before anything can fail. You'll never thank me for this one, because the worst case just stops happening.

## Numbers

- 7 files changed, +111 / −6 — every fix smaller than the paragraph explaining it.
- Test suite: 13 checks → **20**, all green on the machine that ships this build. The 7 new checks are offline (synthetic PARAM.SFO files, fake CDN, forced font failure) so they pass on any PC without internet — and they fail loudly if these bugs ever come back.
- The updater went 246 → 463 downloads between Nov 2 and Nov 17. When I said "nobody uses these little tools", the numbers said otherwise. This update is partly your fault. ❤

## Small print

| | |
|---|---|
| App version everywhere | 2.0.3 (header, app data, exe properties tab) |
| App icon | unchanged — the PS3 tower lives |
| Update | just run the new exe over the old one; settings, art cache and download folders stay put |
| Your downloaded .pkg files | untouched and still valid — Bug 2 was reporting, never files |
| Requirements | Windows x64, nothing else. The new buttons open in your normal browser; the app itself still only talks to Sony's update servers |
| File integrity | v2.0.3 exe SHA-256 `4cea8712bc5a60c685e5b75ca0bc16abae8c83794db6bb652a16aa9072187da0` (also in `SHA256SUMS.txt` in this repo). Unsigned community build — if Windows SmartScreen warns, that's expected; verify the checksum and run it |

## Found something weird?

Reply on this release or open an issue — I read all of it, and small tools like this get fixed faster than big ones.

New PS3 deep-dives and gaming clips drop on [youtube.com/@TheNewGamePluss](https://www.youtube.com/@TheNewGamePluss). And if this app saved you an afternoon: ♥ [ko-fi.com/thenewgameplus](https://ko-fi.com/thenewgameplus) — every coffee funds the next bug hunt. — John
