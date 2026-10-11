"""Cover-art resolution: local ICON0 first, cache next, optional remote last."""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from typing import Optional

try:  # Pillow is optional: art degrades to text placeholders without it
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    Image = None
    ImageDraw = None

try:  # Pillow >= 9.2 ships the textsize helper; older versions use textbbox
    from PIL.ImageFont import load_default as _load_default_font
except Exception:  # pragma: no cover
    def _load_default_font():
        return None

_CACHE_DIRNAME = "art_cache"
_REMOTE_RE = re.compile(r"https?://[^\s\"']+\.(?:png|jpg|jpeg|webp)", re.I)
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# Remote box art comes from Sony's public keyless TMDB endpoint (the same one
# RPCS3's Discord bot uses) through the verified updater.psn adapter. The
# PlayStation Store web API paths were probed live (2026-10) and are dead
# (404/403 from plain clients), so they are not used. TMDB entries are not
# contractual either — cache-first, throttled, and never raising.
_MIN_REQUEST_GAP = 1.0  # courtesy throttle for remote metadata


def cache_dir(base_dir: str) -> str:
    d = os.path.join(base_dir, _CACHE_DIRNAME)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _cache_path(serial: str, base_dir: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", serial or "unknown")
    return os.path.join(cache_dir(base_dir), f"{safe}.png")


def find_local_icon(game_path: str) -> Optional[str]:
    """Return the first usable ICON0 image inside a game folder, if any."""
    if Image is None or not game_path or not os.path.isdir(game_path):
        return None
    for root, _dirs, files in os.walk(game_path):
        depth = root[len(os.path.normpath(game_path)):].count(os.sep)
        if depth > 3:
            continue
        for name in files:
            low = name.lower()
            if low == "icon0.png" or (low.startswith("icon0") and low.endswith((".png", ".jpg", ".jpeg"))):
                candidate = os.path.join(root, name)
                try:
                    with Image.open(candidate) as im:
                        im.verify()
                    return candidate
                except Exception:
                    continue
        if len(files) > 400:  # don't keep walking huge trees
            break
    return None


def load_image(path: str, size=(360, 500)) -> Optional["Image.Image"]:
    if Image is None:
        return None
    try:
        with Image.open(path) as im:
            im = im.convert("RGBA")
            im.thumbnail((size[0] * 2, size[1] * 2), Image.LANCZOS)
            return im
    except Exception:
        return None


def save_cached(serial: str, image: "Image.Image", base_dir: str) -> Optional[str]:
    if Image is None:
        return None
    try:
        path = _cache_path(serial, base_dir)
        image.save(path, "PNG")
        return path
    except OSError:
        return None


def load_cached(serial: str, base_dir: str) -> Optional["Image.Image"]:
    path = _cache_path(serial, base_dir)
    if os.path.isfile(path):
        return load_image(path)
    return None


def fetch_remote_artwork(url: str, timeout: float = 6.0) -> Optional["Image.Image"]:
    """Best-effort remote artwork download. Never raises."""
    if Image is None:
        return None
    try:
        import urllib.request

        req = urllib.request.Request(
            url, headers={"User-Agent": _UA}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(8 * 1024 * 1024)
        import io

        im = Image.open(io.BytesIO(data))
        return im.convert("RGBA")
    except Exception:
        return None


def extract_remote_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = _REMOTE_RE.search(text)
    return m.group(0) if m else None


class ArtworkResolver:
    """Resolves cover art for a game without ever blocking or raising.

    Priority: local ICON0 -> on-disk cache -> (optional) remote URL -> placeholder.
    Remote lookups run in a worker thread and are skipped entirely when disabled.
    """

    def __init__(self, base_dir: str, allow_remote: bool = False):
        self.base_dir = base_dir
        self.allow_remote = allow_remote
        self._lock = threading.Lock()
        self._inflight = set()
        self._missed = set()  # title IDs confirmed to have no store art
        self._last_request = 0.0

    def resolve_local(self, game) -> Optional["Image.Image"]:
        icon = find_local_icon(game.path) if getattr(game, "path", "") else None
        if icon:
            im = load_image(icon)
            if im is not None and getattr(game, "serial", ""):
                save_cached(game.serial, im, self.base_dir)
            return im
        if getattr(game, "serial", ""):
            return load_cached(game.serial, self.base_dir)
        return None

    def resolve_remote_by_serial(self, serial: str, name: str = "") -> Optional["Image.Image"]:
        """Best-effort cover art for titles not installed in RPCS3 (no local ICON0).

        Source is Sony's public keyless TMDB endpoint (same as RPCS3's Discord
        bot), reached through the verified updater.psn adapter: title ID ->
        display name + box-art URL. TMDB entries are not contractual — some
        titles have no entry at all — so this is cache-first, throttled,
        memoizes misses, and never raises; the placeholder remains the caller's
        final fallback.
        """
        if Image is None or not self.allow_remote:
            return None
        serial = (serial or "").strip().upper()
        if not serial:
            return None
        cached = load_cached(serial, self.base_dir)
        if cached:
            return cached

        with self._lock:
            if serial in self._missed or serial in self._inflight:
                return None
            self._inflight.add(serial)
        try:
            now = time.time()
            with self._lock:
                gap = _MIN_REQUEST_GAP - (now - self._last_request)
            if gap > 0:
                time.sleep(gap)
            with self._lock:
                self._last_request = time.time()

            from . import psn  # verified lookup logic; no import cycle (psn never imports artwork)
            import io

            meta_name, icon_url = psn.fetch_meta(serial, timeout=12)
            data = psn.download_icon(icon_url, timeout=15)
            if not data:
                with self._lock:
                    self._missed.add(serial)
                return None
            im = Image.open(io.BytesIO(data))
            im.load()
            save_cached(serial, im, self.base_dir)
            return im
        except Exception:  # noqa: BLE001 - remote art is best-effort
            with self._lock:
                self._missed.add(serial)
            return None
        finally:
            with self._lock:
                self._inflight.discard(serial)

    def resolve_remote(self, game, url: str, on_done=None):
        """Kick off an optional remote fetch; calls on_done(serial, image|None)."""
        serial = getattr(game, "serial", "") or ""
        if not (self.allow_remote and url and serial):
            return

        def _work():
            with self._lock:
                if serial in self._inflight:
                    return
                self._inflight.add(serial)
            try:
                im = fetch_remote_artwork(url)
                if im is not None:
                    save_cached(serial, im, self.base_dir)
            finally:
                with self._lock:
                    self._inflight.discard(serial)
            if on_done is not None:
                try:
                    on_done(serial, im)
                except Exception:
                    pass

        threading.Thread(target=_work, daemon=True).start()


def make_placeholder(title: str = "", size=(360, 500)) -> Optional["Image.Image"]:
    """Generate a refined dark placeholder tile with the game's initials."""
    if Image is None or ImageDraw is None:
        return None
    w, h = size
    img = Image.new("RGBA", (w, h), (24, 28, 38, 255))
    d = ImageDraw.Draw(img)

    # subtle vertical gradient
    top = (30, 36, 50)
    bottom = (17, 20, 29)
    for y in range(h):
        t = y / max(1, h - 1)
        col = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        d.line([(0, y), (w, y)], fill=col + (255,))

    # faint diagonal accent lines
    for i in range(-h, w, 46):
        d.line([(i, h), (i + h, 0)], fill=(70, 130, 255, 18), width=2)

    initials = _initials(title) or "?"
    font = None
    for path in (
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
    ):
        if os.path.isfile(path):
            try:
                from PIL import ImageFont

                font = ImageFont.truetype(path, int(min(w, h) * 0.34))
            except Exception:
                font = None
            break
    if font is None:
        font = _load_default_font()

    try:
        bbox = d.textbbox((0, 0), initials, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        tw, th = len(initials) * 40, 40
    d.text(((w - tw) / 2 - (bbox[0] if font else 0), (h - th) / 2 - (bbox[1] if font else 0)),
           initials, fill=(96, 156, 255, 235), font=font)

    # bottom bar hint
    d.rectangle([0, h - 6, w, h], fill=(47, 213, 255, 90))
    return img


def _initials(title: str) -> str:
    words = [w for w in re.split(r"[\s\-_:]+", (title or "").strip()) if w]
    if not words:
        return ""
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[-1][0]).upper()


def fingerprint(serial: str, version: Optional[str]) -> str:
    """Stable id used to key cached update lookups."""
    raw = f"{serial}|{version or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
