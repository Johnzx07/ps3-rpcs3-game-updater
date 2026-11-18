# Bug hunt round 3 (v2.0.3 candidates): reproductions + regressions.
# Exit 0 = all green. Failures print RED with the observed behavior.
# Companion to tests/test_bugs.py (the v2.0.2 suite) — same harness style.
import os, struct, sys, tempfile, traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root (tests/ sibling)
sys.path.insert(0, REPO)

import ps3_rpcs3_game_updater as core
from updater import scanner, downloader, artwork

RESULTS = []

def check(name, fn):
    try:
        fn()
        RESULTS.append(("PASS", name, ""))
    except AssertionError as e:
        RESULTS.append(("RED ", name, str(e)))
    except Exception as e:
        RESULTS.append(("ERR ", name, f"{type(e).__name__}: {e}"))
        traceback.print_exc()

# ---------------- canonical PARAM.SFO builder (as tests/test_bugs.py) --------
def build_sfo(path, entries):
    """Canonical PS3 PARAM.SFO: header_size 0x80, entry table at 0x80,
    46-byte entries (key[16], fmt u16, pad2, flag u32, offset u32, size u32)."""
    base, stride, header_size = 0x80, 46, 0x80
    data_start = base + stride * len(entries)
    table, strs = bytearray(), bytearray()
    for key, fmt, text in entries:
        table += key.encode("ascii") + b"\x00" * (16 - len(key))
        table += struct.pack("<H", fmt) + b"\x00\x00"
        if fmt == 0x04:  # string
            raw = text.encode("utf-8") + b"\x00"
            table += struct.pack("<III", 0, data_start + len(strs), len(raw))
            strs += raw
        else:            # integer stored inline
            table += struct.pack("<III", 0, len(strs), 4)
            strs += struct.pack("<I", int(text))
        table += b"\x00" * (stride - len(table) % stride) if len(table) % stride else b""
    header = struct.pack("<7I", int.from_bytes(b"SFB\x00", "little"), 0x00020001,
                         header_size, data_start, len(entries), stride, len(strs)) \
             + struct.pack("<I", base)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(bytes(header) + bytes(bytearray(base - 32)) + bytes(table) + bytes(strs))

GOW3 = [("TITLE", 0x04, "God of War III"), ("SERIAL", 0x04, "BCUS98148"),
        ("PARENTAL_LEVEL", 0x02, "5")]

def _fake_resp(body, total):
    class R:
        headers = {"Content-Length": str(total)}
        def raise_for_status(self): pass
        def iter_content(self, chunk_size=1):
            for i in range(0, len(body), chunk_size):
                yield body[i:i + chunk_size]
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return R()

# ---------------- Bug A: content-id folder names produced invalid serials ----
# Documented leftover in BUGFIXES-v2.0.2.md ("BCUS98174GDAI32 -> red Lookup
# failed: Invalid serial format"). The folder name is RPCS3's content-id style
# (<9-char title id><suffix>); the old code uppercased the WHOLE name, which
# failed the serial contract, so the card could only ever fail its lookup and
# duplicated the yml entry for the same game.
def t_folder_contentid_sfo_in_ps3_game():
    tmp = tempfile.mkdtemp(prefix="scan_cid_")
    root = os.path.join(tmp, "dev_hdd0", "game")
    game = os.path.join(root, "BCUS98174GDAI32")
    build_sfo(os.path.join(game, "PS3_GAME", "PARAM.SFO"), GOW3)  # disc layout: SFO only inside PS3_GAME
    found = scanner.scan_games_root(root)
    assert len(found) == 1, f"expected 1 game, got {found!r}"
    g = found[0]
    assert g.serial == "BCUS98148", f"folder-derived serial invalid: {g.serial!r} (title id is BCUS98148)"
    assert g.title == "God of War III", f"PS3_GAME/PARAM.SFO never read: title={g.title!r}"

def t_folder_contentid_no_sfo_falls_back_to_titleid():
    tmp = tempfile.mkdtemp(prefix="scan_cid2_")
    root = os.path.join(tmp, "dev_hdd0", "game")
    os.makedirs(os.path.join(root, "BCUS98174GDAI32", "PS3_GAME"))  # no SFO anywhere
    found = scanner.scan_games_root(root)
    assert len(found) == 1, f"expected 1 game, got {found!r}"
    assert found[0].serial == "BCUS98174", \
        f"content-id folder name must yield its embedded 9-char title id, got {found[0].serial!r}"

def t_junk_folder_name_dropped():
    tmp = tempfile.mkdtemp(prefix="scan_junk_")
    root = os.path.join(tmp, "dev_hdd0", "game")
    os.makedirs(os.path.join(root, "HDD_GAME_DATA", "PS3_GAME"))  # no salvageable title id anywhere
    found = scanner.scan_games_root(root)
    assert found == [], f"unsalvageable folder must be dropped, got {found!r}"

def t_build_library_merges_contentid_with_yml():
    tmp = tempfile.mkdtemp(prefix="scan_merge_")
    cfg = os.path.join(tmp, "config"); os.makedirs(cfg)
    with open(os.path.join(cfg, "games.yml"), "w", encoding="utf-8") as f:
        f.write("game:\n  BCUS98148: D:/ROMS/PS3/Games/God of War III [BCUS98148]/\n")
    game = os.path.join(tmp, "dev_hdd0", "game", "BCUS98174GDAI32")
    build_sfo(os.path.join(game, "PS3_GAME", "PARAM.SFO"), GOW3)
    lib = scanner.build_library(tmp)
    serials = [g.serial for g in lib]
    assert serials == ["BCUS98148"], f"same game listed twice (yml + content-id folder): {serials!r}"
    g = lib[0]
    assert g.source == "both" and os.path.normpath(game) in g.path, f"merge lost the folder path: {g!r}"

# ---------------- Bug B: batch reported planned paths, not the saved file ----
# download_pkg names the file from the URL basename; the plan's destination
# name differs for DRM-free entries ("DRM-Free <file>"), and the batch threw
# the actual path away. The GUI filters completed paths with is_file(), so a
# nonexistent planned name silently killed the "install in RPCS3" offer.
def t_batch_reports_actual_path():
    import requests
    tmp = tempfile.mkdtemp(prefix="batch_")
    body = b"Y" * 1000
    pkg = {"url": "https://cdn.example/UP9000-BCUS98148_0-GOW3UPDATE0000.pkg",
           "version": "01.00", "size": 1000, "sha1": "", "drm_free": True}
    _, dest = core.plan_downloads("God of War III", "BCUS98148", [pkg], __import__("pathlib").Path(tmp))[0]
    assert dest.name.startswith("DRM-Free "), f"plan no longer uses the DRM-Free prefix name: {dest.name}"
    real = requests.get
    requests.get = lambda url, **k: _fake_resp(body, 1000)
    try:
        results = downloader.download_batch([(pkg, dest)])
    finally:
        requests.get = real
    path, ok, msg = results[0]
    assert ok, f"download failed: {msg}"
    assert os.path.isfile(path), f"reported path does not exist: {path!r} (actual file was saved under the URL name)"

def t_batch_skip_path_still_reported():
    import requests
    tmp = tempfile.mkdtemp(prefix="batch2_")
    pkg = {"url": "https://cdn.example/UP-SMALL.pkg", "version": "01.00",
           "size": 3, "sha1": "", "drm_free": False}
    dest = __import__("pathlib").Path(tmp) / pkg["url"].split("/")[-1]
    dest.write_bytes(b"abc")  # right size already on disk -> skip path
    results = downloader.download_batch([(pkg, dest)])
    assert results[0][1] and os.path.isfile(results[0][0]), f"skip case broke: {results[0]!r}"

# ---------------- Bug C: make_placeholder NameError when textbbox fails ------
def t_placeholder_survives_textbbox_failure():
    from PIL import ImageDraw
    Draw = ImageDraw.ImageDraw
    real_bbox, real_text = Draw.textbbox, Draw.text
    Draw.textbbox = lambda *a, **k: (_ for _ in ()).throw(OSError("simulated font failure"))
    try:
        im = artwork.make_placeholder("Test Game")  # must fall back, not NameError on bbox
    finally:
        Draw.textbbox, Draw.text = real_bbox, real_text
    assert im is not None and im.width > 0, "placeholder returned nothing after font metrics failure"

check("content-id folder + PS3_GAME SFO -> valid serial + title", t_folder_contentid_sfo_in_ps3_game)
check("content-id folder without any SFO -> embedded title id", t_folder_contentid_no_sfo_falls_back_to_titleid)
check("folder name with no salvageable serial is dropped", t_junk_folder_name_dropped)
check("yml + content-id folder merge into ONE card", t_build_library_merges_contentid_with_yml)
check("download_batch reports the saved file path (DRM-free)", t_batch_reports_actual_path)
check("download_batch skip-existing path stays correct (regression)", t_batch_skip_path_still_reported)
check("make_placeholder survives textbbox failure", t_placeholder_survives_textbbox_failure)

w = max(len(n) for _, n, _ in RESULTS)
for st, n, m in RESULTS:
    print(f"[{st}] {n.ljust(w)}  {m}")
fails = sum(1 for s, _, _ in RESULTS if s.strip() in ("RED", "ERR"))
print(f"\n{len(RESULTS)-fails}/{len(RESULTS)} green")
sys.exit(1 if fails else 0)
