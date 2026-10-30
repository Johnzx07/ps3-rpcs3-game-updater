#!/usr/bin/env python3
"""
PS3 RPCS3 Game Updater
======================
Scans your RPCS3 library (games.yml) or a single serial, finds available
title updates on Sony's PSN servers, downloads the .pkg files in correct
install order, and verifies each one with SHA-1.

Usage:  python ps3_rpcs3_game_updater.py          (GUI)
        python ps3_rpcs3_game_updater.py --cli BLUS30089   (headless test / scripting)

Only stdlib + requests + PyYAML are required.
"""

import argparse
import configparser
import hashlib
import hmac
import io
import os
import queue
import re
import sys
import threading
import time
import traceback
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import requests
except ImportError:
    # Raise instead of sys.exit(1): this module is imported by the GUI and the
    # updater package, and exiting at import time kills the caller process.
    raise ImportError(
        "Missing dependency 'requests'. Install with:  pip install requests"
    )

# Pillow is optional: box-art thumbnails are an enhancement. A top-level (guarded)
# import lets PyInstaller detect PIL + ImageTk statically, and the app still runs
# without art if Pillow isn't installed.
try:
    from PIL import Image as _PILImage
    # PhotoImage lives in Pillow (PIL.ImageTk), NOT tkinter — a top-level guarded
    # import also lets PyInstaller bundle the _imagingtk extension statically.
    from PIL import ImageTk as _PILImageTk
except ImportError:
    _PILImage = None
    _PILImageTk = None

APP_NAME = "ps3-rpcs3-game-updater"
LEGACY_APP_NAME = "PS3Updater"
UPDATE_XML_URL = "https://a0.ww.np.dl.playstation.net/tpl/np/{tid}/{tid}-ver.xml"
# Sony's Title Metadata Database (TMDB) — same public endpoint the RPCS3 Discord
# bot uses for game titles + box art. The path is keyed by an HMAC-SHA1 of the
# product id under a fixed public key, so no account/credentials are needed.
TMDB_URL = "https://tmdb.np.dl.playstation.net/tmdb/{pid}_{h}/{pid}.xml"
TMDB_HMAC_KEY = bytes.fromhex(
    "F5DE66D2680E255B2DF79E74F890EBF349262F618BCAE2A9ACCDEE5156CE8DF2"
    "CDF2D48C71173CDC2594465B87405D197CF1AED3B7E9671EEB56CA6753C2E6B0")
DEFAULT_MAX_DOWNLOADS = 4
# Box-art sanity cap: anything bigger is not an icon (spec: validate image size).
MAX_ICON_BYTES = 8 * 1024 * 1024

# Support links — shown in the GUI header and About dialog.
REPO_URL = "https://github.com/Johnzx07/ps3-rpcs3-game-updater"
YOUTUBE_URL = "https://www.youtube.com/@TheNewGamePluss"
KOFI_URL = "https://ko-fi.com/thenewgameplus"


def _valid_icon_bytes(data) -> bool:
    """True if the bytes decode as a real image of sane size. Never raises."""
    if not data or len(data) > MAX_ICON_BYTES:
        return False
    if _PILImage is None:
        # Pillow missing — accept non-empty bytes; set_art() will no-op safely.
        return True
    try:
        with _PILImage.open(io.BytesIO(data)) as im:
            im.verify()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def config_dir() -> Path:
    if sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / APP_NAME
    elif sys.platform.startswith("linux"):
        d = Path.home() / ".config" / APP_NAME
    else:
        appdata = os.environ.get("APPDATA", str(Path.home()))
        d = Path(appdata) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def legacy_config_file() -> Path:
    """Return the previous config path so existing settings migrate cleanly."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / LEGACY_APP_NAME / "config.ini"
    if sys.platform.startswith("linux"):
        return Path.home() / ".config" / LEGACY_APP_NAME / "config.ini"
    appdata = os.environ.get("APPDATA", str(Path.home()))
    return Path(appdata) / LEGACY_APP_NAME / "config.ini"


def load_config() -> dict:
    cfg = configparser.ConfigParser()
    path = config_dir() / "config.ini"
    if not path.exists():
        legacy = legacy_config_file()
        if legacy.exists():
            path = legacy
    if path.exists():
        try:
            cfg.read(path)
        except Exception:
            # A hand-edited/corrupted config (duplicate sections, bad syntax)
            # must never brick startup — fall back to defaults.
            cfg = configparser.ConfigParser()
    downloads = cfg.get("paths", "downloads", fallback=str(Path.home() / "PS3Updates"))
    rpcs3 = cfg.get("paths", "rpcs3", fallback="")
    try:
        max_dl = cfg.getint("settings", "max_downloads", fallback=DEFAULT_MAX_DOWNLOADS)
    except ValueError:
        max_dl = DEFAULT_MAX_DOWNLOADS
    return {"downloads": downloads, "rpcs3": rpcs3, "max_downloads": max_dl}


def save_config(cfg: dict):
    p = configparser.ConfigParser()
    p.add_section("paths")
    p.set("paths", "downloads", cfg["downloads"])
    p.set("paths", "rpcs3", cfg.get("rpcs3", ""))
    p.add_section("settings")
    p.set("settings", "max_downloads", str(cfg.get("max_downloads", DEFAULT_MAX_DOWNLOADS)))
    with open(config_dir() / "config.ini", "w") as f:
        p.write(f)


# --------------------------------------------------------------------------
# PSN update lookup (pure functions — testable without the GUI)
# --------------------------------------------------------------------------
def sanitize_name(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_\- \u3000-\u303F\u3040-\u30FF\u4E00-\u9FFF\uFF00-\uFFEF]", "", name or "")
    return re.sub(r"[\s.]+$", "", name).strip()


def version_key(v: str):
    """Sort key for PS3 versions like '01.02' -> (1, 2)."""
    parts = []
    for chunk in v.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def fmt_size(n):
    """Human-readable byte size."""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def _pkg_int(value) -> int:
    """Sony size attrs are numeric strings; never crash on malformed ones."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def fetch_updates(title_id: str, timeout: int = 20):
    """Query Sony's PSN update endpoint.

    Returns (game_name, [pkg dicts]) where each pkg dict has keys:
      version, size (int bytes), sha1, url, drm_free (bool)
    Raises LookupError for invalid/unknown serials, ValueError on network errors.
    """
    title_id = title_id.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{9}", title_id):
        raise LookupError(f"Invalid serial format: {title_id!r} (expected 9 chars, e.g. BLUS30675)")

    url = UPDATE_XML_URL.format(tid=title_id)
    # TLS note (verified against the live endpoint — see README "Security"):
    # Sony's XML host presents a certificate chain rooted in its own private CA
    # ("SCEI DNAS Root 05"), which is absent from every public trust store, so
    # standard verification (verify=True) fails for EVERY client here — the same
    # situation PySN/Rusty-PSN work around. verify=False is therefore scoped to
    # THIS Sony request only and must never be reused for unrelated domains.
    try:
        r = requests.get(url, timeout=timeout, verify=False)
    except requests.RequestException as e:
        raise ValueError(f"Network error contacting PSN: {e}") from e

    if r.status_code != 200 or not r.text.strip():
        # 'Not found' body or empty => no updates (or unknown id; both look the same)
        return None, []

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        raise ValueError(f"Unexpected response from PSN for {title_id}: {e}") from e

    if root.tag != "titlepatch":
        return None, []

    # Game name lives in the first package's paramsfo (may be TITLE child or text)
    game_name = ""
    pkgs = []
    for pkg_el in root.iter("package"):
        ver = pkg_el.get("version", "")
        size_s = pkg_el.get("size", "0")
        sha1 = pkg_el.get("sha1sum", "")
        url_ = pkg_el.get("url", "")
        if not game_name:
            pfo = pkg_el.find("paramsfo")
            if pfo is not None:
                t = pfo.find("TITLE")
                game_name = (t.text if t is not None else pfo.text) or ""
        if url_:
            pkgs.append({
                "version": ver,
                "size": _pkg_int(size_s),
                "sha1": sha1.lower(),
                "url": url_,
                "drm_free": False,
            })

    # DRM-free updates: <tag ...><package .../><url>...</url></tag> style entries
    for tag_el in root.iter("tag"):
        for u_el in tag_el.findall("url"):
            ver = (u_el.get("version") or "").strip()
            size_s = u_el.get("size", "0")
            sha1 = u_el.get("sha1sum", "")
            url_ = u_el.text.strip() if u_el.text else ""
            if not url_:
                continue
            pkgs.append({
                "version": ver,
                "size": _pkg_int(size_s),
                "sha1": sha1.lower(),
                "url": url_,
                "drm_free": True,
            })

    # De-duplicate (some titles list the same pkg twice) and sort by version
    seen = set()
    unique = []
    for p in pkgs:
        key = (p["version"], p["sha1"])
        if key in seen or not p["url"]:
            continue
        seen.add(key)
        unique.append(p)
    unique.sort(key=lambda p: version_key(p["version"]))

    return sanitize_name(game_name), unique


# --------------------------------------------------------------------------
# Title metadata (TMDB) — game titles + box art, like the RPCS3 Discord bot
# --------------------------------------------------------------------------
def _tmdb_hash(pid: str) -> str:
    """HMAC-SHA1 of a product id under Sony's public TMDB key."""
    return hmac.new(TMDB_HMAC_KEY, pid.encode(), hashlib.sha1).hexdigest().upper()


def fetch_meta(serial: str, timeout: int = 20):
    """Look up a title's display name + box-art icon URL in Sony's TMDB.

    Returns (name, icon_url); either may be '' on any failure. Never raises —
    metadata is an enhancement and must not break the core PSN lookup. Uses
    standard TLS verification (TMDB presents a valid public cert), unlike the
    PSN update endpoint which needs verify=False for its private-CA chain.
    """
    serial = serial.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{9}", serial):
        return "", ""
    pid = f"{serial}_00"
    url = TMDB_URL.format(pid=pid, h=_tmdb_hash(pid))
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200 or not r.text.strip():
            return "", ""
        root = ET.fromstring(r.content)
    except Exception:
        return "", ""
    name_el = root.find("name")
    icon_el = root.find("icon")
    name = (name_el.text if name_el is not None and name_el.text else "") or ""
    icon_url = (icon_el.text if icon_el is not None and icon_el.text else "") or ""
    return sanitize_name(name), icon_url.strip()


def download_icon(url: str, timeout: int = 15):
    """Fetch box-art bytes for a TMDB icon URL. Returns bytes or None; never raises."""
    if not url:
        return None
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200 or not r.content:
            return None
        data = r.content
    except Exception:
        return None
    # Validate type/size before handing bytes to the UI (spec requirement).
    return data if _valid_icon_bytes(data) else None


def read_local_icon(rpcs3_dir: str, title_id: str):
    """Read box-art bytes from a locally installed game (priority-1 art source).

    Looks for ICON0.PNG / PIC1.png under <rpcs3>/dev_hdd0/game/<id>/USRDIR/
    (case-insensitive on the file name). Returns bytes or None; never raises —
    local art is an enhancement and must not break the core PSN lookup.
    """
    if not rpcs3_dir:
        return None
    base = Path(rpcs3_dir) / "dev_hdd0" / "game" / title_id.upper()
    if not base.is_dir():
        return None
    usrd = base / "USRDIR"
    if not usrd.is_dir():
        return None
    wanted = {"icon0.png", "pic1.png"}
    try:
        for f in usrd.iterdir():
            if f.is_file() and f.name.lower() in wanted:
                data = f.read_bytes()
                # validate type/size like the remote path does (see download_icon)
                if _valid_icon_bytes(data):
                    return data
    except OSError:
        pass
    return None


def lookup_with_meta(tid: str, timeout: int = 20, rpcs3_dir: str = ""):
    """fetch_updates + metadata enrichment.

    Returns (game_name, pkgs, meta_name, icon_bytes). Propagates fetch_updates'
    LookupError/ValueError so callers keep their existing error paths; any
    metadata/icon failure degrades to empty values instead of raising.

    Art priority (mirrors the RPCS3 Discord bot's local-first design):
      1. locally installed game art (<rpcs3>/dev_hdd0/game/<id>/USRDIR/ICON0.PNG)
         — offline, instant; when present we skip the TMDB round-trip entirely
      2. Sony TMDB box art (network) + display name — used when no usable local
         icon exists, and also to name titles that have no published updates.
    """
    game_name, pkgs = fetch_updates(tid, timeout=timeout)
    meta_name, icon_bytes = "", None
    # Enrichment must NOT be gated on published updates: a title that is not
    # installed in RPCS3, or has zero updates (e.g. a clean first-release-only
    # title like NPUB31689), still deserves its display name and box art.
    try:
        icon_bytes = read_local_icon(rpcs3_dir, tid)
        if _valid_icon_bytes(icon_bytes):
            # Local art present; fetch only the TMDB name when there is nothing
            # else to show — normal installed, updatable games still skip the
            # TMDB round-trip entirely (documented behavior).
            if not pkgs:
                meta_name, _ = fetch_meta(tid, timeout=timeout)
        else:
            meta_name, icon_url = fetch_meta(tid, timeout=timeout)
            icon_bytes = download_icon(icon_url, timeout=15)
    except Exception:
        pass
    return game_name, pkgs, meta_name, icon_bytes


def read_games_yml(rpcs3_dir: str):
    """Return {title_id: name} from an RPCS3 install's config/games.yml.

    Handles both formats:
      "BLUS12345":            (flat — value is the game path)
        D:/ROMS/PS3/Games/Game Name [BLUS12345]/
      "BLUS12345":            (nested — newer RPCS3 builds)
        name: Game Name
        path: D:/...

    Real RPCS3 installs nest everything under a top-level 'game:' key; that is
    unwrapped first, and the legacy bare-mapping form still works.
    """
    import yaml  # local import so --cli works even if yaml is missing for GUI-only use
    yml = Path(rpcs3_dir) / "config" / "games.yml"
    if not yml.is_file():
        raise FileNotFoundError(f"No games.yml at {yml}")
    with open(yml, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if isinstance(data, dict) and isinstance(data.get("game"), dict):
        data = data["game"]
    out = {}
    for tid, info in data.items():
        name = ""
        path_s = ""
        if isinstance(info, dict):
            name = str(info.get("name", "") or "")
            path_s = str(info.get("path", "") or "")
        else:
            path_s = str(info or "")  # flat format: the value IS the path
        if not name and path_s:
            # Derive a display name from the folder, e.g.
            # "D:/ROMS/PS3/Games/God of War III [BCUS98111]/" -> "God of War III"
            parts = [p for p in Path(path_s.replace("\\", "/")).parts
                     if p and not p.endswith(":") and p.lower() != "game"]
            cand = next((p for p in reversed(parts)), "")
            name = re.sub(r"\s*\[[A-Z0-9]{9}\]\s*$", "", cand).strip()
        out[str(tid).upper()] = sanitize_name(name)
    return out


# --------------------------------------------------------------------------
# Downloading (pure functions — testable without the GUI)
# --------------------------------------------------------------------------
def sha1_of_file(path: Path, chunk: int = 1 << 20):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def download_pkg(pkg: dict, dest_dir: Path, progress_cb=None, stop_event=None):
    """Download one pkg to dest_dir/<basename>. Returns (path, ok, message).

    progress_cb(done_bytes, total_bytes) is called periodically.
    Skips re-download if a file of the right size already exists.
    SHA-1 is checked against Sony's metadata; on mismatch the file is KEPT
    with a warning — Sony has stale hashes for some re-issued packages, and
    RPCS3 verifies PKG signatures itself at install time (final gate).
    Partial files are deleted on network failure.
    """
    fname = os.path.basename(pkg["url"].split("?")[0]) or f"{pkg['version']}.pkg"
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / fname
    tmp = dest_dir / (fname + ".part")

    if final.exists() and final.stat().st_size == pkg["size"]:
        # Verify the SHA-1 of the existing file too — skipping on size alone
        # contradicts the hash-check policy documented above.
        if pkg["sha1"]:
            digest = sha1_of_file(final)
            if digest != pkg["sha1"]:
                return str(final), True, (
                    f"already present, but SHA-1 differs from Sony's listing: "
                    f"{digest[:8]}… vs {pkg['sha1'][:8]}…"
                )
        return str(final), True, "already present"

    try:
        # TLS note (verified against the live endpoint — see README "Security"):
        # The CDN host b0.ww.np.dl.playstation.net serves a valid public
        # Akamai/DigiCert certificate, but for its internal name (a248.e.akamai.net),
        # not the requested hostname — so standard verification fails with a
        # hostname mismatch for EVERY client. verify=False is scoped to THIS Sony
        # CDN request only and must never be reused for unrelated domains.
        with requests.get(pkg["url"], stream=True, timeout=60, verify=False) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or pkg["size"] or 0)
            done = 0
            last_report = 0.0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if stop_event is not None and stop_event.is_set():
                        raise InterruptedError("stopped by user")
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if progress_cb and (now - last_report > 0.15 or done == total):
                        progress_cb(done, total)
                        last_report = now

        # A CDN can close the connection cleanly after a short body; with no
        # SHA-1 on record nothing else catches the truncation, and the partial
        # .pkg would be kept and reported "ok" (RPCS3's install then fails).
        if total and done != total and not (pkg.get("sha1") or "").lower():
            tmp.unlink(missing_ok=True)
            return str(final), False, f"incomplete download ({done}/{total} bytes)"

        digest = sha1_of_file(tmp)
        if pkg["sha1"] and digest != pkg["sha1"]:
            os.replace(tmp, final)  # keep it — see docstring; RPCS3 re-verifies at install
            return str(final), True, f"downloaded (SHA-1 differs from Sony's listing: {digest[:8]}… vs {pkg['sha1'][:8]}…)"
        os.replace(tmp, final)
        if progress_cb:
            progress_cb(total or done, total or done)
        return str(final), True, "ok"
    except InterruptedError as e:
        tmp.unlink(missing_ok=True)
        return str(final), False, str(e)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return str(final), False, f"{type(e).__name__}: {e}"


def plan_downloads(game_name: str, title_id: str, pkgs, base_dir: Path):
    """Build the ordered download list for one game.

    Returns [(pkg, dest_path)] in install order (ascending version).
    Folders are named  '<base>/PlayStation 3/[ID] Name' like PySN does.
    """
    name = sanitize_name(game_name) or title_id
    folder = base_dir / "PlayStation 3" / f"[{title_id}] {name}"
    out = []
    for p in pkgs:  # already sorted ascending by fetch_updates()
        fname = os.path.basename(p["url"].split("?")[0]) or f"{p['version']}.pkg"
        if p["drm_free"]:
            fname = "DRM-Free " + fname
        out.append((p, folder / fname))
    return out


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
def _apply_style(root):
    """Apply a native theme plus app typography. Never raises — styling must
    never be able to take the whole GUI down."""
    from tkinter import ttk
    try:
        style = ttk.Style(root)
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("Header.TLabel", font=("Segoe UI", 13, "bold"))
        style.configure("Sub.TLabel", font=("Segoe UI", 9), foreground="#5a6270")
        style.configure("Summary.TLabel", font=("Segoe UI", 10))
        style.configure("Hint.TLabel", font=("Segoe UI", 8), foreground="#7a8290")
        style.configure("Status.TLabel", font=("Segoe UI", 9))
        style.configure("Treeview", rowheight=48, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
    except Exception:
        pass


def _row_tag_for(status):
    """Map a status string to its color tag ('' = default foreground)."""
    s = str(status)
    if s.startswith("\u2713"):                       # ✓ done
        return "ok"
    if s.startswith("\u2717") or s.startswith("error:"):   # ✗ failed / error row
        return "fail"
    if "downloading" in s:
        return "busy"
    return ""


def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    cfg = load_config()
    # Two queues on purpose: cmd_q carries commands TO the worker threads,
    # ev_q carries events FROM them to the UI. One shared queue would let an
    # idle worker steal (and silently drop) UI events — that was exactly why
    # download progress never showed up in the table.
    # Keep the command and event queues separate so UI work stays responsive.
    cmd_q: "queue.Queue" = queue.Queue()
    ev_q: "queue.Queue" = queue.Queue()
    stop_event = threading.Event()
    state = {
        "rows": {},          # iid -> dict(game, id, ver, size, status, path, pkg)
        "images": {},        # iid -> PhotoImage (kept alive so tkinter doesn't GC it)
        "downloading": False,
        "total_bytes": 0,
        "done_bytes": 0,     # bytes of finished packages (drives the bar)
        "inflight_bytes": 0, # bytes received on the current package
    }

    root = tk.Tk()
    root.title("PS3 RPCS3 Game Updater")
    root.geometry("1040x680")
    root.minsize(920, 560)
    _apply_style(root)

    # ---- header -----------------------------------------------------------
    head = ttk.Frame(root, padding=(12, 10, 12, 0))
    head.pack(fill="x")
    ttk.Label(head, text="PS3 RPCS3 Game Updater", style="Header.TLabel").pack(side="left")
    ttk.Label(head, text="   PSN title updates for RPCS3 — no account needed",
              style="Sub.TLabel").pack(side="left", pady=(5, 0))

    def open_url(url):
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def show_about():
        messagebox.showinfo(
            "About PS3 RPCS3 Game Updater",
            "PS3 RPCS3 Game Updater\n"
            "Finds and downloads official PSN title updates (.pkg)\n"
            "for your RPCS3 games — no account needed.\n\n"
            f"Source code:\n{REPO_URL}\n\n"
            "Enjoying the tool?\n"
            f"YouTube (subscribe): {YOUTUBE_URL}\n"
            f"Ko-fi (donations): {KOFI_URL}")

    # Right side of the header: About + support links. Pack order matters —
    # each side="right" widget lands to the LEFT of the previous one, so pack
    # right-to-left to get [About] | separator | [YouTube] [Ko-fi].
    ttk.Button(head, text="Ko-fi — support", command=lambda: open_url(KOFI_URL)).pack(side="right")
    ttk.Button(head, text="YouTube — subscribe", command=lambda: open_url(YOUTUBE_URL)).pack(side="right", padx=(8, 0))
    ttk.Separator(head, orient="vertical").pack(side="right", fill="y", padx=12)
    ttk.Button(head, text="About…", command=show_about).pack(side="right")

    # ---- search row -------------------------------------------------------
    top = ttk.Frame(root, padding=(12, 8, 12, 2))
    top.pack(fill="x")
    ttk.Label(top, text="Serial:").pack(side="left")
    entry = ttk.Entry(top, font=("Consolas", 11))
    entry.pack(side="left", fill="x", expand=True, padx=(6, 8), ipady=2)
    btn_search = ttk.Button(top, text="Search updates")

    def do_search():
        tid = entry.get().strip()
        if not tid:
            return
        entry.delete(0, "end")
        # Pass the RPCS3 dir so local ICON0.PNG / PARAM.SFO can supply metadata (priority 1).
        rpcs3_dir = rpcs3_var.get().strip() or cfg.get("rpcs3", "")
        cmd_q.put(("search_one", tid.upper(), rpcs3_dir))

    btn_search.configure(command=do_search)
    btn_search.pack(side="left")
    entry.bind("<Return>", lambda e: do_search())

    top2 = ttk.Frame(root, padding=(12, 0, 12, 4))
    top2.pack(fill="x")
    btn_scan = ttk.Button(top2, text="Scan RPCS3 library (games.yml)")

    def do_scan():
        rpcs3_dir = rpcs3_var.get().strip() or cfg.get("rpcs3", "")
        if not rpcs3_dir:
            chosen = filedialog.askdirectory(title="Select your RPCS3 install folder")
            if not chosen:
                return
            rpcs3_dir = chosen
            rpcs3_var.set(chosen)
            cfg["rpcs3"] = chosen
            save_config(cfg)
        try:
            games = read_games_yml(rpcs3_dir)
        except Exception as e:
            messagebox.showerror("PS3 RPCS3 Game Updater", f"Could not read library:\n{e}")
            return
        if not games:
            messagebox.showinfo("PS3 RPCS3 Game Updater", "No games found in games.yml.")
            return
        cmd_q.put(("search_many", list(games.keys()), {k: v for k, v in games.items()}, rpcs3_dir))

    btn_scan.configure(command=do_scan)
    btn_scan.pack(side="left")

    # ---- folders row ------------------------------------------------------
    setrow = ttk.LabelFrame(root, text="Folders", padding=(8, 6))
    setrow.pack(fill="x", padx=12, pady=(0, 4))
    ttk.Label(setrow, text="Downloads:").pack(side="left")
    dl_var = tk.StringVar(value=cfg.get("downloads", ""))
    ttk.Entry(setrow, textvariable=dl_var).pack(side="left", fill="x", expand=True, padx=(4, 4), ipady=1)

    def pick_downloads():
        d = filedialog.askdirectory(title="Choose download folder")
        if d:
            dl_var.set(d)
            cfg["downloads"] = d
            save_config(cfg)

    ttk.Button(setrow, text="Browse…", command=pick_downloads).pack(side="left")
    ttk.Label(setrow, text="   RPCS3:").pack(side="left")
    rpcs3_var = tk.StringVar(value=cfg.get("rpcs3", ""))
    ttk.Entry(setrow, textvariable=rpcs3_var, width=28).pack(side="left", fill="x", expand=True, padx=(4, 4), ipady=1)

    def pick_rpcs3():
        d = filedialog.askdirectory(title="Choose RPCS3 install folder")
        if d:
            rpcs3_var.set(d)
            cfg["rpcs3"] = d
            save_config(cfg)

    ttk.Button(setrow, text="Browse…", command=pick_rpcs3).pack(side="left")

    # ---- summary + install-order guidance ---------------------------------
    sumframe = ttk.Frame(root, padding=(12, 0))
    sumframe.pack(fill="x")
    summary_lbl = ttk.Label(sumframe, text="", style="Summary.TLabel", anchor="w")
    summary_lbl.pack(side="left", fill="x", expand=True)
    ttk.Label(sumframe, text="Install top to bottom — the # column is install order.",
              style="Hint.TLabel", anchor="e").pack(side="right")

    def set_summary(text):
        summary_lbl.configure(text=text)

    # ---- results table ----------------------------------------------------
    mid = ttk.Frame(root, padding=(12, 0))
    mid.pack(fill="both", expand=True)
    cols = ("art", "order", "game", "id", "version", "size", "status")
    # "tree headings" (not just "headings"): with plain "headings" tkinter hides
    # the item-image column entirely, so set_art() had nowhere to draw and no
    # game icon was ever visible. The art column is the tree's item-image slot.
    tree = ttk.Treeview(mid, columns=cols, show="tree headings", selectmode="extended")
    for c, w in (("art", 30), ("order", 46), ("game", 300), ("id", 105), ("version", 92),
                 ("size", 92), ("status", 280)):
        tree.heading(c, text={"art": "", "order": "# (install order)", "game": "Game", "id": "Serial",
                              "version": "Version", "size": "Size", "status": "Status"}[c])
        tree.column(c, width=w, anchor="w" if c in ("game", "status") else "center")
    # color-code rows by state (done / failed / downloading)
    tree.tag_configure("ok", foreground="#1a7f37")
    tree.tag_configure("fail", foreground="#b3261e")
    tree.tag_configure("busy", foreground="#0b5cad")
    vsb = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.pack(side="left", fill="both", expand=True)
    vsb.pack(side="right", fill="y")

    def add_row(game, tid, ver, size, status, pkg=None):
        iid = tree.insert("", "end", values=("", "", game, tid, ver, fmt_size(size), status))
        state["rows"][iid] = {"game": game, "id": tid, "ver": ver, "raw_ver": (pkg or {}).get("version", ""),
                              "size": size, "status": status, "pkg": pkg}
        tree.item(iid, tags=(_row_tag_for(status),))
        return iid

    def set_row(iid, **kw):
        r = state["rows"].get(iid)
        if not r:
            return
        r.update(kw)
        # Rebuild the display row from the (now updated) record. Reading back
        # with tree.set() would just re-write the OLD values — a no-op.
        vals = ["", tree.set(iid, "order"), r.get("game", ""), r.get("id", ""),
                r.get("ver", "\u2014"), fmt_size(r.get("size")), r.get("status", "\u2014")]
        tree.item(iid, values=vals)
        tree.item(iid, tags=(_row_tag_for(r.get("status", "")),))

    def set_art(iid, icon_bytes):
        """Attach a box-art thumbnail to a row (main thread only). No-op if Pillow
        is missing or the bytes are empty/undecodable — art never breaks the UI."""
        if not icon_bytes or _PILImage is None or _PILImageTk is None:
            return
        try:
            img = _PILImage.open(io.BytesIO(icon_bytes))
            # Fit a compact square-ish thumbnail to default row height.
            img.thumbnail((40, 40), _PILImage.LANCZOS)
            photo = _PILImageTk.PhotoImage(img)
        except Exception:
            return
        state["images"][iid] = photo          # keep alive (tkinter GCs unreferenced images)
        tree.item(iid, image=photo)

    # ---- bottom bar -------------------------------------------------------
    bot = ttk.Frame(root, padding=(12, 6))
    bot.pack(fill="x", side="bottom")
    btn_dl = ttk.Button(bot, text="Download selected (in order)")
    btn_stop = ttk.Button(bot, text="Stop", state="disabled")
    btn_open = ttk.Button(bot, text="Open downloads folder")
    btn_dl.pack(side="left")
    btn_stop.pack(side="left", padx=6)
    btn_open.pack(side="right")

    def open_folder():
        d = dl_var.get() or cfg.get("downloads", "")
        if not d:
            return
        try:
            os.startfile(d)  # Windows
        except Exception:
            pass

    btn_open.configure(command=open_folder)

    overall = ttk.Progressbar(bot, mode="determinate")
    overall.pack(fill="x", pady=(8, 2))
    status_lbl = ttk.Label(root, text="Ready. Paste a serial (e.g. BLUS30675) and press Search, or scan your library.", style="Status.TLabel", padding=(14, 4), anchor="w")
    status_lbl.pack(fill="x", side="bottom")

    # ---- worker: searches -------------------------------------------------
    def search_worker():
        while True:
            msg = cmd_q.get()
            kind = msg[0]
            if kind == "search_one":
                tid = msg[1]
                rpcs3_dir = msg[2] if len(msg) > 2 else ""   # older 2-tuples (self-test) still work
                ev_q.put(("status", f"Searching PSN for {tid}\u2026"))
                try:
                    name, pkgs, meta_name, icon_bytes = lookup_with_meta(tid, rpcs3_dir=rpcs3_dir)
                except LookupError as e:
                    ev_q.put(("result_error", tid, str(e)))
                    continue
                except ValueError as e:
                    ev_q.put(("result_error", tid, str(e)))
                    continue
                if not pkgs:
                    ev_q.put(("result_none", tid, name))
                else:
                    ev_q.put(("results", tid, name, pkgs, meta_name, icon_bytes))
            elif kind == "search_many":
                tids = msg[1]
                names_hint = msg[2]
                rpcs3_dir = msg[3] if len(msg) > 3 else ""   # older 3-tuples (self-test) still work
                ev_q.put(("status", f"Searching PSN for {len(tids)} games\u2026"))
                with ThreadPoolExecutor(max_workers=6) as ex:
                    futs = {ex.submit(lookup_with_meta, t, rpcs3_dir=rpcs3_dir): t for t in tids}
                    done_n = 0
                    for fut in futs:
                        tid = futs[fut]
                        try:
                            name, pkgs, meta_name, icon_bytes = fut.result(timeout=120)
                        except LookupError as e:
                            ev_q.put(("result_error", tid, str(e)))
                        except Exception as e:
                            ev_q.put(("result_error", tid, f"{type(e).__name__}: {e}"))
                        else:
                            if not pkgs:
                                ev_q.put(("result_none", tid, name or names_hint.get(tid, "")))
                            else:
                                display = meta_name or name or names_hint.get(tid, "")
                                ev_q.put(("results", tid, display, pkgs, meta_name, icon_bytes))
                        done_n += 1
                        ev_q.put(("status", f"Searching\u2026 {done_n}/{len(tids)}"))

    threading.Thread(target=search_worker, daemon=True).start()

    # ---- worker: downloads ------------------------------------------------
    def download_worker(order):
        """order: list of (iid, pkg, dest_path) in install order."""
        for idx, (iid, pkg, dest) in enumerate(order, 1):
            if stop_event.is_set():
                ev_q.put(("dl_status", iid, "stopped"))
                continue
            ev_q.put(("dl_status", iid, f"downloading ({idx}/{len(order)})\u2026"))

            def cb(done, total, _iid=iid):
                ev_q.put(("dl_progress", _iid, done, total))

            path, ok, msg = download_pkg(pkg, dest.parent, progress_cb=cb, stop_event=stop_event)
            if ok:
                ev_q.put(("dl_done", iid, path, msg))
            else:
                ev_q.put(("dl_fail", iid, msg))
        ev_q.put(("dl_all_done", None))

    def start_downloads():
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("PS3 RPCS3 Game Updater", "Select one or more update rows first.")
            return
        base = Path(dl_var.get().strip())
        if not base:
            messagebox.showerror("PS3 RPCS3 Game Updater", "Choose a download folder first (top row).")
            return
        # group by game, keep ascending version order within each game
        per_game = {}
        for iid in sel:
            r = state["rows"][iid]
            if not r.get("pkg"):
                continue
            per_game.setdefault(r["id"], []).append((r.get("raw_ver") or r["ver"], iid, r["pkg"]))
        order = []
        for tid in sorted(per_game):
            for ver, iid, pkg in sorted(per_game[tid], key=lambda x: version_key(x[0])):
                p, dest = plan_downloads(state["rows"][iid]["game"], tid, [pkg], base)[0]
                order.append((iid, pkg, dest))
        if not order:
            return
        state["downloading"] = True
        stop_event.clear()
        btn_dl.configure(state="disabled")
        btn_stop.configure(state="normal")
        total = sum(p["size"] for _, p, _ in order)
        ev_q.put(("dl_total", total))
        threading.Thread(target=download_worker, args=(order,), daemon=True).start()

    def stop_downloads():
        stop_event.set()
        status_lbl.configure(text="Stopping after current chunk\u2026")

    btn_dl.configure(command=start_downloads)
    btn_stop.configure(command=stop_downloads)

    # ---- queue pump -------------------------------------------------------
    def poll_queue():
        try:
            while True:
                msg = ev_q.get_nowait()
                kind = msg[0]
                if kind == "status":
                    status_lbl.configure(text=msg[1])
                elif kind == "results":
                    if len(msg) >= 6:
                        _, tid, name, pkgs, meta_name, icon_bytes = msg[:6]
                    else:
                        _, tid, name, pkgs = msg
                        meta_name, icon_bytes = "", None
                    # number rows in install order for this game; prefer the
                    # TMDB display name over the raw PSN name when available.
                    display = meta_name or name or tid
                    row_iids = []
                    for i, p in enumerate(pkgs, 1):
                        tag = " DRM-free" if p["drm_free"] else ""
                        iid = add_row(display, tid, f"{p['version']}{tag}", p["size"], "available", pkg=p)
                        tree.set(iid, "order", str(i))
                        row_iids.append(iid)
                    if icon_bytes:
                        for iid in row_iids:
                            set_art(iid, icon_bytes)
                    total_sz = sum(p["size"] for p in pkgs)
                    set_summary(f"{display} ({tid}) \u2014 {len(pkgs)} update(s), "
                                f"{fmt_size(total_sz)} total, latest v{pkgs[-1]['version']}")
                elif kind == "result_none":
                    _, tid, name = msg
                    add_row(name or "No updates available", tid, "", 0, "no updates")
                    set_summary(f"No updates available for {tid}" + (f" ({name})" if name else ""))
                elif kind == "result_error":
                    _, tid, err = msg
                    add_row(err.split(":")[0], tid, "", 0, f"error: {err}")
                    set_summary(f"Search failed for {tid} \u2014 see the table row.")
                elif kind == "dl_total":
                    state["total_bytes"] = msg[1]
                    state["done_bytes"] = 0
                    state["inflight_bytes"] = 0
                    overall.configure(maximum=max(msg[1], 1), value=0)
                    status_lbl.configure(text=f"Downloading {fmt_size(msg[1])}\u2026")
                elif kind == "dl_status":
                    set_row(msg[1], status=msg[2])
                elif kind == "dl_progress":
                    _, iid, done, total = msg
                    r = state["rows"].get(iid)
                    if r:
                        pct = f"{done/total*100:.0f}%" if total else ""
                        set_row(iid, status=f"downloading {fmt_size(done)}/{fmt_size(total)} ({pct})")
                        state["inflight_bytes"] = done
                        overall.configure(value=state["done_bytes"] + done)
                elif kind == "dl_done":
                    _, iid, path, msg2 = msg
                    r = state["rows"].get(iid)
                    if r:
                        state["done_bytes"] += r.get("size", 0)
                        state["inflight_bytes"] = 0
                        overall.configure(value=state["done_bytes"])
                        set_row(iid, status=f"\u2713 {msg2} \u2014 {path}")
                elif kind == "dl_fail":
                    _, iid, err = msg
                    state["inflight_bytes"] = 0
                    set_row(iid, status=f"\u2717 failed: {err}")
                elif kind == "dl_all_done":
                    state["downloading"] = False
                    btn_dl.configure(state="normal")
                    btn_stop.configure(state="disabled")
                    overall.configure(value=state["done_bytes"])
                    n_ok = sum(1 for r in state["rows"].values() if str(r.get("status", "")).startswith("\u2713"))
                    status_lbl.configure(text=f"Done. {n_ok} update(s) ready to install \u2014 drag the .pkg files into RPCS3 or use File \u2192 Install Packages.")
                else:
                    print(f"[ps3-rpcs3-game-updater] unknown event: {msg!r}", file=sys.stderr, flush=True)
        except queue.Empty:
            pass
        except Exception:
            # A single bad event must never kill the pump — if it did, every
            # later progress/completion update would vanish silently.
            print("[ps3-rpcs3-game-updater] poll_queue error:", file=sys.stderr, flush=True)
            traceback.print_exc()
        root.after(100, poll_queue)

    poll_queue()
    root.mainloop()



# --------------------------------------------------------------------------
# CLI (headless) — also used for smoke-testing the core logic
# --------------------------------------------------------------------------
def run_cli(serials, out_dir):
    base = Path(out_dir).expanduser()
    total_ok = 0
    for tid in serials:
        print(f"== {tid} ==")
        try:
            name, pkgs = fetch_updates(tid)
        except (LookupError, ValueError) as e:
            print(f"   error: {e}")
            continue
        if not pkgs:
            print("   no updates available")
            continue
        print(f"   {name or tid}: {len(pkgs)} update(s)")
        plan = plan_downloads(name, tid, pkgs, base)
        for i, (p, dest) in enumerate(plan, 1):
            print(f"   [{i}] v{p['version']}  {fmt_size(p['size'])}  {'DRM-free ' if p['drm_free'] else ''}{dest.name}")

        def cb(done, total):
            if total <= 0:  # zero-length/unknown-size pkg: avoid ZeroDivisionError
                print(f"\r      downloading {done/1048576:.1f} MB", end="", flush=True)
                return
            print(f"\r      downloading {done/1048576:.1f}/{total/1048576:.1f} MB ({done/total*100:.0f}%)", end="", flush=True)

        for i, (p, dest) in enumerate(plan, 1):
            path, ok, msg = download_pkg(p, dest.parent, progress_cb=cb)
            print()
            if ok:
                total_ok += 1
                print(f"   [{i}] OK ({msg}): {path}")
            else:
                print(f"   [{i}] FAILED: {msg}")
    print(f"\nDone. {total_ok} package(s) downloaded to {base}")


def main():
    # Silence urllib3 InsecureRequestWarning — emitted only by the two Sony PSN
    # requests above that use verify=False (see their TLS notes and README "Security").
    try:
        import urllib3  # modern path; requests.packages.urllib3 is a deprecated shim
        urllib3.disable_warnings()
    except ImportError:
        requests.packages.urllib3.disable_warnings()
    ap = argparse.ArgumentParser(description="PS3 RPCS3 Game Updater")
    ap.add_argument("--cli", nargs="+", metavar="SERIAL", help="headless mode: search+download given serials")
    ap.add_argument("-o", "--out", default=str(Path.home() / "PS3Updates"), help="output folder (CLI mode)")
    args = ap.parse_args()
    if args.cli:
        run_cli(args.cli, args.out)
    else:
        run_gui()


if __name__ == "__main__":
    main()
