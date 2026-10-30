# Bug hunt for ps3-rpcs3-game-updater: reproductions + live probes.
# Exit 0 = all green. Failures print RED with the observed behavior.
import io, os, struct, sys, tempfile, traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root (tests/ sibling)
SCRATCH = tempfile.mkdtemp(prefix="bughunt_")
sys.path.insert(0, REPO)

import ps3_rpcs3_game_updater as core
from updater import scanner

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

def check_live(name, fn):
    try:
        msg = fn()
        RESULTS.append(("LIVE", name, msg))
    except Exception as e:
        RESULTS.append(("LIVE-FAIL", name, f"{type(e).__name__}: {e}"))

# ---------------- synthetic PARAM.SFO builders ----------------
def build_sfo(entries, header_size=0x80, base=0x80, stride=46):
    """Canonical PS3 PARAM.SFO: 32-byte header, padding, entry table at
    `base` with 46-byte entries (key[16], fmt u16, pad2, flag u32, offset u32,
    size u32, 14 pad bytes), string/int data at data_start."""
    data_start = base + stride * len(entries)
    blob = bytearray(data_start)
    table = bytearray()
    strs = bytearray()
    for key, fmt, text in entries:
        table += key.encode("ascii") + b"\x00" * (16 - len(key))
        table += struct.pack("<H", fmt) + b"\x00\x00"
        if fmt == 0x04:  # string
            raw = text.encode("utf-8") + b"\x00"
            off = data_start + len(strs)
            strs += raw
            table += struct.pack("<III", 0, off, len(raw))
        else:            # integer
            table += struct.pack("<III", 0, len(strs), 4)
            strs += struct.pack("<I", int(text))
        table += b"\x00" * (stride - len(table) % stride) if len(table) % stride else b""
    header = struct.pack("<7I", int.from_bytes(b"SFB\x00", "little"), 0x00020001, header_size, data_start,
                         len(entries), 46, len(strs)) + struct.pack("<I", base)
    path = os.path.join(SCRATCH, "sfo", name_ := f"sfo_{header_size}_{base}.bin")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(bytes(header) + bytes(bytearray(base - 32)) + bytes(table) + bytes(strs))
    return path

ENTRIES = [("TITLE", 0x04, "God of War III"), ("TITLE_ID", 0x04, "BCUS98148"),
           ("PARENTAL_LEVEL", 0x02, "5"), ("CATEGORY", 0x02, "1")]

def t_sfo_canonical():
    p = build_sfo(ENTRIES)  # canonical: header_size 0x80, entries at 0x80, stride 46
    got = scanner.read_sfo_strings(p)
    assert got.get("TITLE") == "God of War III", f"canonical SFO -> {got!r} (expected TITLE decoded)"
    assert got.get("TITLE_ID") == "BCUS98148", f"TITLE_ID missing in {got!r}"

def t_sfo_canonical20():
    # 20 entries pushes data_start (0x80 + 20*46 = 992) past the old code's
    # 512-byte entry_size cap -> every real multi-entry game SFO failed there.
    ents = [("TITLE", 0x04, "Red Dead Redemption"), ("TITLE_ID", 0x04, "BLUS30089")]
    for i in range(18):
        ents.append((f"KEY{i:02d}", 0x04, f"value {i}"))
    ents += [("PARENTAL_LEVEL", 0x02, "7"), ("CATEGORY", 0x02, "1")]
    p = build_sfo(ents)
    got = scanner.read_sfo_strings(p)
    assert got.get("TITLE") == "Red Dead Redemption", f"20-entry canonical SFO -> keys={sorted(got)}"
    assert got.get("TITLE_ID") == "BLUS30089", f"20-entry: TITLE_ID missing in {got!r}"

def t_sfo_legacy32():
    # producer variant current code claims to support: entries at 32, stride=data_start
    stride = 46
    p = build_sfo(ENTRIES, header_size=32, base=32, stride=stride)
    got = scanner.read_sfo_strings(p)
    assert got.get("TITLE_ID") == "BCUS98148", f"32-byte-header variant -> {got!r}"

# ---------------- config corruption ----------------
def t_config_garbage():
    tmp = tempfile.mkdtemp(prefix="cfg_")
    real_cd = core.config_dir
    core.config_dir = lambda: __import__("pathlib").Path(tmp)
    try:
        # 1) non-numeric max_downloads (hand-edited config)
        with open(os.path.join(tmp, "config.ini"), "w") as f:
            f.write("[paths]\ndownloads = X:\\\nrpcs3 =\n[settings]\nmax_downloads = lots\n")
        cfg = core.load_config()  # must not raise; must fall back to default
        assert cfg["max_downloads"] == core.DEFAULT_MAX_DOWNLOADS, f"got {cfg['max_downloads']!r}"
        # 2) duplicate [settings] sections (DuplicateSectionError on read)
        with open(os.path.join(tmp, "config.ini"), "w") as f:
            f.write("[settings]\nmax_downloads = 4\n[settings]\nmax_downloads = 5\n")
        cfg2 = core.load_config()
        assert isinstance(cfg2["max_downloads"], int), f"crashed on duplicate section: {cfg2!r}"
    finally:
        core.config_dir = real_cd

# ---------------- download_pkg truncation ----------------
class _FakeResp:
    def __init__(self, body, total):
        self._b, self._tot = body, total
        self.headers = {"Content-Length": str(total)}
    def raise_for_status(self): pass
    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._b), chunk_size):
            yield self._b[i:i + chunk_size]
    def __enter__(self): return self
    def __exit__(self, *a): return False

def t_download_truncated():
    import requests
    tmp = tempfile.mkdtemp(prefix="dl_")
    body = b"X" * 500_000
    real = requests.get
    requests.get = lambda url, **k: _FakeResp(body, 1_000_000)  # clean close at half
    try:
        pkg = {"url": "https://cdn.example/UP9000-BCUS98148_0-UPDATE0100.pkg",
               "version": "01.00", "size": 1_000_000, "sha1": "", "drm_free": False}
        path, ok, msg = core.download_pkg(pkg, __import__("pathlib").Path(tmp))
        assert not ok, f"TRUNCATED file accepted as ok=True ({msg}) — 500KB accepted for 1MB"
    finally:
        requests.get = real

def t_download_complete():
    import requests
    tmp = tempfile.mkdtemp(prefix="dl_")
    body = b"X" * 1_000_000
    real = requests.get
    requests.get = lambda url, **k: _FakeResp(body, 1_000_000)
    try:
        pkg = {"url": "https://cdn.example/UP-BLAH.pkg", "version": "01.00",
               "size": 1_000_000, "sha1": "", "drm_free": False}
        path, ok, msg = core.download_pkg(pkg, __import__("pathlib").Path(tmp))
        assert ok, f"complete download failed: {msg}"
    finally:
        requests.get = real

# ---------------- fetch_updates XML duplicate packages ----------------
FIXTURE = b"""<?xml version="1.0" encoding="utf-8"?>
<titlepatch titleid="BCUS98148">
<tag title="01.00-updatedata" version="01.00" index="0">
 <updateinfo sequencer="0">
  <package index="0" version="01.00" ps3_systemver="04000000" pkgversion="01.00"
           license="18" sha1sum="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
           size="100" url="np_0000-BCUS98148_00-GOW3UPDATE0000000000" kcode="00000001">
   <paramsfo><TITLE>God of War III</TITLE></paramsfo>
  </package>
 </updateinfo>
</tag>
<tag name="DRM-Free 01.00" title="God of War III" version="01.00" index="1"
     sha1="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">
 <package version="01.00" index="0" kcode="00000002"
          size="100" url="np_0000-BCUS98148_00-GOW3UPDATE00000000000000"
          sha1sum="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" />
 <url version="01.00" size="100">https://b00.ws1.ps3.update.playstation.net/update/ps3/image/us/np_0000-BCUS98148_00-GOW3UPDATE00000000000000.pkg</url>
</tag>
</titlepatch>"""

def t_fetch_updates_dupe():
    import requests
    real = requests.get
    requests.get = lambda url, **k: _FakeXml(FIXTURE)
    try:
        name, pkgs = core.fetch_updates("BCUS98148")
        urls = [p["url"] for p in pkgs]
        assert len(urls) == len(set(urls)), f"same URL listed twice: {urls}"
    finally:
        requests.get = real

class _FakeXml:
    def __init__(self, content):
        self.content, self.text = content, content.decode()
        self.status_code = 200

# ---------------- games.yml parsing (regression) ----------------
def t_games_yml():
    import yaml  # noqa
    tmp = tempfile.mkdtemp(prefix="yml_")
    cfg = os.path.join(tmp, "config"); os.makedirs(cfg)
    with open(os.path.join(cfg, "games.yml"), "w", encoding="utf-8") as f:
        f.write('game:\n'
                '  BCUS98148: D:/ROMS/PS3/Games/God of War III [BCUS98148]/\n'
                '  BLUS30089:\n'
                '    name: Red Dead Redemption\n'
                '    path: D:/ROMS/PS3/Games/RDR [BLUS30089]/\n')
    got = core.read_games_yml(tmp)
    assert got.get("BCUS98148") == "God of War III", f"flat: {got!r}"
    assert got.get("BLUS30089") == "Red Dead Redemption", f"nested: {got!r}"

def t_games_yml_fallback():
    text = ('game:\n'
            '  BCUS98148: D:/ROMS/PS3/Games/God of War III [BCUS98148]/\n'
            '  BLUS30089:\n'
            '    name: Red Dead Redemption\n'
            '    path: D:/ROMS/PS3/Games/RDR [BLUS30089]/\n')
    got = scanner.parse_games_yml(text)
    d = {e["serial"]: e["title"] for e in got}
    assert d.get("BCUS98148") == "God of War III", f"flat fallback: {d!r}"
    assert d.get("BLUS30089") == "Red Dead Redemption", f"nested fallback: {d!r}"

# ---------------- live probes against Sony ----------------
def live_updates(tid):
    name, pkgs = core.fetch_updates(tid, timeout=25)
    urls = [os.path.basename(p["url"].split("?")[0]) for p in pkgs]
    dupes = [u for u in set(urls) if urls.count(u) > 1]
    vers = [p["version"] for p in pkgs]
    return (f"name={name!r} n={len(pkgs)} vers={vers[:4]}{'...' if len(vers)>4 else ''} "
            f"dupe_urls={dupes or 'none'}")

def live_meta(tid):
    n, u = core.fetch_meta(tid, timeout=25)
    return f"name={n!r} icon={'yes' if u.startswith('http') else u!r}"

def t_sfo_original_shipped_fails():
    """Prove the bug: the code AS SHIPPED in v2.0.1 (probe from base 32,
    stride=data_start, 3-int entry reads) returns {} for a valid canonical
    PARAM.SFO. The patched read_sfo_strings above must decode the same file."""
    import struct as _st
    def orig(path):
        data = open(path, "rb").read(1 << 20)
        if len(data) < 32 or data[:4] != b"SFB\x00":
            return {}
        _,_,max_entries,entry_size,num_entries,key_len,data_len = _st.unpack_from("<7I", data, 0)
        if not (1 <= num_entries <= max_entries and 25 <= entry_size <= 512):
            return {}
        base = 32
        def probe(fmt_width, offset_first):
            entry_min = 16 + fmt_width + 8
            if entry_size < entry_min: return None
            out = {}
            for i in range(num_entries):
                off = base + i * entry_size
                if off + entry_min > len(data): return None
                ke = data.find(b"\x00", off, off + 16)
                key = data[off:ke if ke >= 0 else off + 16].decode("ascii","ignore").strip()
                if not key or not all(c.isalnum() or c == "_" for c in key): return None
                if fmt_width == 1: fmt,a,b = _st.unpack_from("<BII", data, off+16)
                else: fmt,a,b = _st.unpack_from("<III", data, off+16)
                if offset_first: value_off, size = a, b
                else: size, value_off = a, b
                if fmt not in (1,2,4) or value_off < base or value_off + size > len(data): return None
                if fmt == 2: continue
                raw = data[value_off:value_off+size]
                nul = raw.find(b"\x00")
                text = (raw[:nul] if nul >= 0 else raw).decode("utf-8","ignore").strip()
                if any(ord(c) < 0x20 for c in text): return None
                out[key] = text
            return out or None
        for fw, of in ((1,True),(4,True),(1,False),(4,False)):
            r = probe(fw, of)
            if r: return r
        return {}
    p = build_sfo(ENTRIES)
    assert orig(p) == {}, "shipped parser unexpectedly handled the file — bug not present"
    got = scanner.read_sfo_strings(p)
    assert got.get("TITLE") == "God of War III", f"patched parser failed: {got!r}"

check("SFO canonical PS3 layout (entries @0x80, stride 46)", t_sfo_canonical)
check("SFO shipped v2.0.1 parser fails it (bug proof)", t_sfo_original_shipped_fails)
check("SFO canonical 20-entry (data_start > 512)", t_sfo_canonical20)
check("SFO 32-byte-header variant (regression)", t_sfo_legacy32)
check("config.ini garbage max_downloads -> default", t_config_garbage)
check("truncated download (no sha1) must fail", t_download_truncated)
check("complete download stays ok (regression)", t_download_complete)
check("fetch_updates dedupes same URL", t_fetch_updates_dupe)
check("games.yml PyYAML path (regression)", t_games_yml)
check("games.yml fallback parser (regression)", t_games_yml_fallback)
check_live("live fetch_updates BCUS98148", lambda: live_updates("BCUS98148"))
check_live("live fetch_updates BLUS30089", lambda: live_updates("BLUS30089"))
check_live("live fetch_meta BCUS98148", lambda: live_meta("BCUS98148"))

w = max(len(n) for _, n, _ in RESULTS)
for st, n, m in RESULTS:
    print(f"[{st}] {n.ljust(w)}  {m}")
fails = sum(1 for s, _, _ in RESULTS if s.strip() in ("RED", "ERR", "LIVE-FAIL"))
print(f"\n{len(RESULTS)-fails}/{len(RESULTS)} green")
sys.exit(1 if fails else 0)
