"""RPCS3 discovery: locate the install, parse config/games.yml, scan game data."""
from __future__ import annotations

import os
import re
import struct
from typing import Dict, List, Optional

from .models import GameInfo, is_valid_serial

GAMES_YML_RELPATH = os.path.join("config", "games.yml")
DEV_HDD_GAME_RELPATH = os.path.join("dev_hdd0", "game")


# --------------------------------------------------------------------------- #
# RPCS3 folder detection
# --------------------------------------------------------------------------- #

def _looks_like_rpcs3(path: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    return (
        os.path.isfile(os.path.join(path, "rpcs3.exe"))
        or os.path.isfile(os.path.join(path, GAMES_YML_RELPATH))
        or os.path.isdir(os.path.join(path, "dev_hdd0"))
    )


def detect_rpcs3_dir() -> Optional[str]:
    """Best-effort detection of the RPCS3 installation folder."""
    candidates: List[str] = []

    env = os.environ.get("RPCS3_DIR") or os.environ.get("RPCS3_PATH")
    if env:
        candidates.append(env)

    try:
        import winreg  # type: ignore

        for hive, sub in (
            (winreg.HKEY_CURRENT_USER, r"Software\RPCS3"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\RPCS3"),
        ):
            try:
                with winreg.OpenKey(hive, sub) as key:
                    for i in range(64):
                        try:
                            name, value, _ = winreg.EnumValue(key, i)
                            if isinstance(value, str) and os.path.isdir(value):
                                candidates.append(value)
                        except OSError:
                            break
            except OSError:
                pass
    except Exception:  # pragma: no cover - non-Windows
        pass

    home = os.path.expanduser("~")
    candidates.extend(
        [
            os.path.join(home, "RPCS3"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "rpcs3"),
            os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "rpcs3"),
        ]
    )

    for cand in candidates:
        if not cand:
            continue
        try:
            real = os.path.realpath(cand)
        except OSError:
            continue
        if _looks_like_rpcs3(real):
            return real
    return None


# --------------------------------------------------------------------------- #
# games.yml parsing (minimal YAML, no external dependency)
# --------------------------------------------------------------------------- #

_TITLE_ID_RE = re.compile(r"^[A-Z0-9]{9}$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _title_from_path(path: str) -> str:
    """Derive a display name from a game path, e.g. '.../Game [BLUS12345]/' -> 'Game'."""
    parts = [p for p in path.replace("\\", "/").split("/") if p and not p.endswith(":")]
    cand = next((p for p in reversed(parts) if p.lower() != "game"), "")
    return re.sub(r"\s*\[[A-Z0-9]{9}\]\s*$", "", cand).strip()


def parse_games_yml(text: str) -> List[Dict[str, str]]:
    """Parse RPCS3's games.yml without an external dependency.

    Supported shapes:
      game:                                   # current RPCS3 root key
        BLUS12345: D:/ROMS/... [BLUS12345]/   # flat — value is the game path
        BLUS12345:
          name: Game Name
          path: D:/...
      BLUS12345: path                         # legacy top-level mapping
      - serial: BLUS12345                     # legacy list of mappings
    """
    entries: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None
    in_game_root = False

    def new_entry(serial: str = "") -> Dict[str, str]:
        nonlocal current
        current = {"serial": serial, "title": "", "path": "", "version": ""}
        return current

    def finish():
        nonlocal current
        if current is not None and current.get("serial"):
            entries.append(current)
        current = None

    def assign(e: Dict[str, str], key: str, value: str):
        if key == "name" or key == "title":
            e["title"] = value or e["title"]
        elif key == "path":
            e["path"] = value
        elif key == "serial":
            e["serial"] = value.upper()
        elif key == "version":
            e["version"] = value

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        if stripped.startswith("- "):
            finish()
            current = new_entry()
            in_game_root = False
            item = stripped[2:].strip()
            if ":" in item:
                key, _, value = item.partition(":")
                assign(current, key.strip().strip("\"'").lower(), _unquote(value))
            continue

        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip().strip("\"'").lower()
        value = _unquote(value)

        if indent == 0:
            finish()
            if key == "game" and not value:
                in_game_root = True  # entries come from the indented children
                continue
            in_game_root = False
            e = new_entry(key.upper() if _TITLE_ID_RE.match(key.upper()) else "")
            if value:
                # Flat mapping form: the value itself is the path (or a name).
                if "/" in value or "\\" in value:
                    e["path"] = value
                else:
                    e["title"] = value
            continue

        # Indented child: under 'game:' a title-ID key starts a new entry;
        # anything else is a field of the entry above it.
        if _TITLE_ID_RE.match(key.upper()):
            finish()
            e = new_entry(key.upper())
            if value:
                # Flat form: the value itself is the path (or a name).
                if "/" in value or "\\" in value:
                    e["path"] = value
                else:
                    e["title"] = value
        elif current is not None:
            assign(current, key, value)

    finish()
    for e in entries:
        if not e.get("title") and e.get("path"):
            e["title"] = _title_from_path(e["path"])
    return [e for e in entries if any(e.values())]


def load_games_yml(rpcs3_dir: str) -> List[Dict[str, str]]:
    """Load config/games.yml via the verified v2 parser module.

    The original returns {serial: name} and handles both RPCS3 formats — flat
    (value is the game path) and nested (name:/path:) — deriving display names
    from folder paths like '.../Game Name [BLUS12345]/'. Falls back to the
    built-in minimal parser when the v2 core or PyYAML is unavailable.
    """
    mapping = None
    try:
        import sys as _sys

        try:
            import ps3_rpcs3_game_updater as _orig
        except ImportError:
            _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            import ps3_rpcs3_game_updater as _orig

        mapping = _orig.read_games_yml(rpcs3_dir)
    except Exception:  # noqa: BLE001 - missing file / PyYAML -> built-in fallback
        mapping = None
    if isinstance(mapping, dict):
        return [{"serial": str(k).upper(), "title": v or ""} for k, v in mapping.items()]

    path = os.path.join(rpcs3_dir, GAMES_YML_RELPATH)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return parse_games_yml(fh.read())
    except OSError:
        return []


# --------------------------------------------------------------------------- #
# PARAM.SFO reader (minimal, strings only)
# --------------------------------------------------------------------------- #

def read_sfo_strings(path: str) -> Dict[str, str]:
    """Read string values from a PS3 PARAM.SFO file. Returns {} on failure.

    Classic (PS3 game) SFO layout: 32-byte little-endian header
    (magic, version, maxent, entry_size, nument, key_len, data_len, padding),
    entry table starting immediately after at offset 32. VITA 'PSF\\0' files use
    a different layout and are out of scope — they simply return {}.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read(1024 * 1024)
    except OSError:
        return {}
    if len(data) < 32 or data[:4] != b"SFB\x00":
        return {}
    try:
        _magic, _version, header_size, data_start, num_entries, entry_stride, data_size = struct.unpack_from("<7I", data, 0)
    except struct.error:
        return {}
    # Canonical ps3devwiki PARAM.SFO fields: header_size is where the entry
    # table starts (0x80 on real PS3 games), entry_stride is the per-entry
    # size (46), data_start is where the string values live.
    if not (1 <= num_entries <= 64):
        return {}

    # Producer variants differ in where the entry table sits and what stride
    # it uses; probe the plausible (base, stride) pairs from the header.
    candidates = []
    for base, stride in ((header_size, entry_stride), (32, entry_stride),
                         (32, data_start), (header_size, data_start)):
        if 32 <= base <= 4096 and 25 <= stride <= 1024 and (base, stride) not in candidates:
            candidates.append((base, stride))

    def probe(fmt_width: int, mode: str, base: int, stride: int) -> Optional[Dict[str, str]]:
        """Try one (base, stride, entry-field-order) layout. Returns strings
        only if every entry validates; a validating layout is never guessed."""
        entry_min = 16 + fmt_width + (12 if mode == "4int" else 8)
        if stride < entry_min:
            return None
        out: Dict[str, str] = {}
        for i in range(num_entries):
            off = base + i * stride
            if off + entry_min > len(data):
                return None
            key_end = data.find(b"\x00", off, off + 16)
            key = data[off:key_end if key_end >= 0 else off + 16].decode("ascii", "ignore").strip()
            # SFO keys are ASCII identifiers (NAME, TITLE_ID, PARENTAL_LEVEL...).
            if not key or not all(c.isalnum() or c == "_" for c in key):
                return None
            if fmt_width == 1:
                fmt, a, b = struct.unpack_from("<BII", data, off + 16)
            elif mode == "4int":
                fmt, _flag, a, b = struct.unpack_from("<4I", data, off + 16)
            else:
                fmt, a, b = struct.unpack_from("<III", data, off + 16)
            if mode == "4int":
                value_off, size = a, b          # key, format, flag, offset, size
            elif mode == "off_first":
                value_off, size = a, b          # key, format, offset, size
            else:
                size, value_off = a, b          # key, format, size, offset
            if fmt not in (0x01, 0x02, 0x04):
                return None
            if fmt == 0x02:
                continue  # integer value stored inline; we only read strings
            # Strings must point inside the file, past the entry table, with a
            # real (NUL-terminated) payload: size 0 means the fields were read
            # from the wrong offsets, i.e. a wrong layout.
            if fmt == 0x04 and (size == 0 or value_off < base or value_off + size > len(data)):
                return None
            if fmt == 0x01 and (value_off < base or value_off + size > len(data)):
                return None
            raw = data[value_off:value_off + size]
            nul = raw.find(b"\x00")
            text = (raw[:nul] if nul >= 0 else raw).decode("utf-8", "ignore").strip()
            if any(ord(c) < 0x20 for c in text):
                return None  # binary where a string was expected: wrong layout
            out[key] = text
        return out or None

    # Canonical PSDevWiki entry is key[16], format(u16), pad, flag(u32),
    # offset(u32), size(u32) — probe that first, then producer variants
    # (u8 format, no flag field, swapped offset/size). The first layout that
    # validates for every entry wins.
    for fmt_width, mode in ((4, "4int"), (4, "off_first"), (1, "off_first"),
                            (4, "size_first"), (1, "size_first")):
        for base, stride in candidates:
            result = probe(fmt_width, mode, base, stride)
            if result:
                return result
    return {}


# --------------------------------------------------------------------------- #
# Game folder scanning
# --------------------------------------------------------------------------- #

_VERSION_DIR_RE = re.compile(r"^\d{2}\.\d{2}(?:\.\d{2})?$")


def _installed_version_from_folder(game_dir: str) -> Optional[str]:
    """Detect an installed update version from a version-named subfolder."""
    for root, dirs, files in os.walk(game_dir):
        depth = root[len(os.path.normpath(game_dir)):].count(os.sep)
        if depth > 2:
            dirs[:] = []
            continue
        for d in list(dirs):
            if _VERSION_DIR_RE.match(d):
                sfo = os.path.join(root, d, "PARAM.SFO")
                if os.path.isfile(sfo):
                    return d
        # don't descend into PS3_GAME content trees deeper than needed
        dirs[:] = [d for d in dirs if d.upper() != "PS3_GAME"][:16]
    return None


def _folder_game_info(folder: str) -> Optional[GameInfo]:
    name = os.path.basename(os.path.normpath(folder))
    has_ps3_game = os.path.isdir(os.path.join(folder, "PS3_GAME"))
    sfo_path = os.path.join(folder, "PARAM.SFO")
    if not (has_ps3_game or os.path.isfile(sfo_path)):
        return None

    title = ""
    serial = name.upper()
    if os.path.isfile(sfo_path):
        strings = read_sfo_strings(sfo_path)
        title = strings.get("TITLE", "")
        sfo_serial = (strings.get("SERIAL") or "").upper()
        if is_valid_serial(sfo_serial):
            serial = sfo_serial

    return GameInfo(
        serial=serial,
        title=title,
        version=_installed_version_from_folder(folder),
        path=os.path.normpath(folder),
        source="folder",
    )


def scan_games_root(games_root: str) -> List[GameInfo]:
    """Scan a dev_hdd0/game style folder for installed games."""
    found: List[GameInfo] = []
    if not os.path.isdir(games_root):
        return found
    try:
        names = sorted(os.listdir(games_root))
    except OSError:
        return found
    for name in names:
        folder = os.path.join(games_root, name)
        if not os.path.isdir(folder):
            continue
        info = _folder_game_info(folder)
        if info is not None:
            found.append(info)
    return found


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #

def build_library(rpcs3_dir: str, games_root: Optional[str] = None) -> List[GameInfo]:
    """Combine games.yml entries with on-disk game folders.

    Folder data wins for paths; yml provides titles/versions when the folder
    has no readable metadata. Result is sorted by title (case-insensitive).
    """
    merged: Dict[str, GameInfo] = {}

    for entry in load_games_yml(rpcs3_dir):
        serial = (entry.get("serial") or "").strip().upper()
        if not is_valid_serial(serial):
            continue
        info = GameInfo(
            serial=serial,
            title=(entry.get("title") or entry.get("name") or "").strip(),
            version=(entry.get("version") or None),
            yml_version=(entry.get("version") or None),
            source="yml",
        )
        merged[serial] = info

    root = games_root or os.path.join(rpcs3_dir, DEV_HDD_GAME_RELPATH)
    for folder_info in scan_games_root(root):
        serial = folder_info.serial
        existing = merged.get(serial)
        if existing is None:
            merged[serial] = folder_info
        else:
            existing.path = folder_info.path
            existing.source = "both"
            if not existing.title and folder_info.title:
                existing.title = folder_info.title
            if folder_info.version:
                existing.version = folder_info.version

    library = sorted(merged.values(), key=lambda g: (g.display_title.lower(), g.serial))
    return library
