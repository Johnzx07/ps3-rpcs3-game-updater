"""Threaded orchestration: scan -> lookup updates -> download, with UI events.

The engine never touches tkinter. It publishes (kind, payload) tuples on a
queue.Queue that the GUI drains on its main thread via `after()` polling:

    scanned          {games: [GameInfo], rpcs3_dir: str}
    scan_error       {error: str}
    updates_found    {serial, name|None, latest: UpdateInfo|None, count: int}
    lookup_error     {serial, error: str}
    art_ready        {serial, image: PIL.Image|None}
    scan_done        {}
    download_started {serial, title, total_pkgs, total_bytes}
    pkg_progress     {serial, index, done, total}
    download_finished{serial, title, results: [(path, ok, msg)], cancelled: bool}

All Sony-facing behavior (PSN lookup, TMDB meta, local-first art, PKG download
with SHA-1 verification and cancellation) is delegated to the verified logic in
ps3_rpcs3_game_updater.py via updater.psn / updater.downloader.
"""
from __future__ import annotations

import io
import queue as _queue
import threading
from pathlib import Path
from typing import Dict, List, Optional

try:  # Pillow is optional; art degrades to placeholders without it
    from PIL import Image as _PILImage
except ImportError:  # pragma: no cover
    _PILImage = None

from .artwork import ArtworkResolver, save_cached
from .config import art_cache_dir, default_downloads_dir
from .downloader import download_batch, plan_downloads
from .models import GameInfo, UpdateInfo, is_valid_serial, normalize_serial
from .psn import lookup_with_meta, pkg_to_update
from .scanner import build_library, detect_rpcs3_dir

LOOKUP_TIMEOUT = 20


class UpdateEngine:
    """Owns the library state and all background work for one app session."""

    def __init__(self) -> None:
        self.events: "_queue.Queue" = _queue.Queue()
        self.library: List[GameInfo] = []
        self.updates: Dict[str, List[UpdateInfo]] = {}
        self.names: Dict[str, str] = {}          # serial -> PSN/TMDB display name
        self.art: Dict[str, object] = {}         # serial -> PIL.Image (remote/cached)
        self.rpcs3_dir: str = ""
        self.downloads_dir: str = default_downloads_dir()
        self.max_downloads: int = 4
        self.allow_remote_art: bool = True
        self.resolver = ArtworkResolver(art_cache_dir(self.downloads_dir), False)
        self._pkgs: Dict[str, List[dict]] = {}   # serial -> raw Sony pkg dicts
        self._stop_events: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ utils
    def _emit(self, kind: str, **data):
        self.events.put((kind, data))

    def _attach_art(self, serial: str, image, game=None):
        """Store resolved cover art and publish the documented `art_ready` event.

        Priority: caller-supplied Sony icon bytes -> local ICON0/on-disk cache ->
        remote TMDB box art via the resolver (cache-first and throttled; only
        when the resolver allows remote). The GUI's placeholder remains the
        final fallback.
        """
        if image is None and game is not None:
            try:
                image = self.resolver.resolve_local(game)
            except Exception:  # noqa: BLE001 - local art is best-effort
                image = None
        if image is None:
            try:
                image = self.resolver.resolve_remote_by_serial(serial, self.names.get(serial, ""))
            except Exception:  # noqa: BLE001 - remote art is best-effort
                image = None
        if image is None:
            return
        with self._lock:
            self.art[serial] = image
        try:
            save_cached(serial, image, art_cache_dir(self.downloads_dir))
        except Exception:  # noqa: BLE001 - cache write is best-effort
            pass
        self._emit("art_ready", serial=serial, image=image)

    def configure(
        self,
        rpcs3_dir: str = "",
        downloads_dir: str = "",
        max_downloads: int = 4,
        allow_remote_art: bool = True,
    ):
        with self._lock:
            if rpcs3_dir is not None:
                self.rpcs3_dir = rpcs3_dir or ""
            if downloads_dir:
                self.downloads_dir = downloads_dir
            self.max_downloads = max(1, int(max_downloads or 1))
            self.allow_remote_art = bool(allow_remote_art)
        self.resolver = ArtworkResolver(
            art_cache_dir(self.downloads_dir), allow_remote=self.allow_remote_art
        )

    # ----------------------------------------------------------------- rescan
    def rescan(self):
        """Detect the RPCS3 folder, build the local library (emitted immediately),
        then look up PSN updates for every game. Runs in a worker thread."""
        threading.Thread(target=self._rescan_worker, daemon=True).start()

    def _rescan_worker(self):
        try:
            rpcs3 = self.rpcs3_dir or detect_rpcs3_dir() or ""
            library = build_library(rpcs3) if rpcs3 else []
            with self._lock:
                self.rpcs3_dir = rpcs3
                # A title checked by Title ID is usually not in games.yml, so a
                # rescan must keep it: otherwise the card, name, and artwork the
                # user just looked up disappear as soon as the scan finishes.
                manual = [g for g in self.library if g.source == "manual"]
                seen = {g.serial for g in library}
                library.extend(g for g in manual if g.serial not in seen)
                library.sort(key=lambda g: (g.display_title.lower(), g.serial))
                self.library = library
                gone = {s for s in list(self.updates) if s not in {g.serial for g in library}}
                for s in gone:
                    self.updates.pop(s, None)
                    self._pkgs.pop(s, None)
            self._emit("scanned", games=list(library), rpcs3_dir=rpcs3)
        except Exception as exc:  # noqa: BLE001 - surface a readable error in the UI
            self._emit("scan_error", error=f"{type(exc).__name__}: {exc}")
            return

        stop = threading.Event()
        with self._lock:
            self._stop_events["lookup"] = stop
            serials = [g.serial for g in self.library]

        for serial in serials:
            if stop.is_set():
                break
            try:
                name, pkgs, meta_name, icon_bytes = lookup_with_meta(
                    serial, timeout=LOOKUP_TIMEOUT, rpcs3_dir=self.rpcs3_dir
                )
            except Exception as exc:  # LookupError/ValueError from fetch_updates
                self._emit("lookup_error", serial=serial, error=str(exc))
                continue

            updates = [pkg_to_update(serial, p) for p in pkgs]
            display_name = meta_name or name or ""
            with self._lock:
                self.updates[serial] = updates
                self._pkgs[serial] = list(pkgs)
                if display_name:
                    self.names[serial] = display_name

            image = None
            if icon_bytes and _PILImage is not None:
                try:
                    image = _PILImage.open(io.BytesIO(icon_bytes))
                    image.load()
                except Exception:  # noqa: BLE001 - corrupt art must never break the scan
                    image = None
            game = next((g for g in self.library if g.serial == serial), None)
            self._attach_art(serial, image, game)

            latest = updates[-1] if updates else None
            self._emit(
                "updates_found",
                serial=serial,
                name=display_name or None,
                latest=latest,
                count=len(updates),
            )
        self._emit("scan_done")

    def cancel_lookup(self):
        with self._lock:
            ev = self._stop_events.get("lookup")
        if ev is not None:
            ev.set()

    def check_title_id(self, serial: str):
        """Look up one title ID and add it to the visible library immediately.

        This keeps the compact GUI useful even when a game is not listed in
        RPCS3's games.yml yet (or the user simply wants to check one known
        title).  The UI receives ``game_added`` before the network lookup so it
        can render a card, placeholder artwork, and an honest "Checking…"
        state instead of appearing empty while the request is in flight.
        """
        serial = normalize_serial(serial)
        if not is_valid_serial(serial):
            self._emit(
                "lookup_error",
                serial=serial,
                error="Enter a 9-character title ID, for example BLUS30675.",
            )
            return

        with self._lock:
            if not any(game.serial == serial for game in self.library):
                self.library.append(GameInfo(serial=serial, title=serial, source="manual"))
                self.library.sort(key=lambda game: (game.display_title.lower(), game.serial))
            game = next(game for game in self.library if game.serial == serial)
        self._emit("game_added", game=game)
        threading.Thread(target=self._lookup_one_worker, args=(serial,), daemon=True).start()

    def _lookup_one_worker(self, serial: str):
        """Fetch update metadata and artwork for a manually checked title.

        The whole body is guarded: in a windowed build an uncaught worker
        exception is silent, so anything unexpected must surface as
        ``lookup_error`` instead of leaving the card stuck on "Checking…".
        """
        try:
            name, pkgs, meta_name, icon_bytes = lookup_with_meta(
                serial, timeout=LOOKUP_TIMEOUT, rpcs3_dir=self.rpcs3_dir
            )

            updates = [pkg_to_update(serial, pkg) for pkg in pkgs]
            display_name = meta_name or name or ""
            with self._lock:
                self.updates[serial] = updates
                self._pkgs[serial] = list(pkgs)
                if display_name:
                    self.names[serial] = display_name

            image = None
            if icon_bytes and _PILImage is not None:
                try:
                    image = _PILImage.open(io.BytesIO(icon_bytes))
                    image.load()
                except Exception:  # noqa: BLE001 - corrupt art must never break lookup
                    image = None
            game = next((g for g in self.library if g.serial == serial), None)
            self._attach_art(serial, image, game)

            self._emit(
                "updates_found",
                serial=serial,
                name=display_name or None,
                latest=updates[-1] if updates else None,
                count=len(updates),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced by the UI
            self._emit("lookup_error", serial=serial, error=str(exc))

    # --------------------------------------------------------------- downloads
    def start_download(self, serial: str):
        """Download every published update for one game (ascending install order)."""
        with self._lock:
            pkgs = list(self._pkgs.get(serial) or [])
            name = self.names.get(serial) or ""
            if not name:
                game = next((g for g in self.library if g.serial == serial), None)
                name = (game.title if game is not None else "") or serial
        if not pkgs:
            self._emit("download_finished", serial=serial, title=name, results=[], cancelled=False)
            return

        stop = threading.Event()
        with self._lock:
            self._stop_events[serial] = stop
        total_bytes = sum(int(p.get("size") or 0) for p in pkgs)

        def _work():
            items = plan_downloads(name, serial, pkgs, Path(self.downloads_dir))
            self._emit(
                "download_started",
                serial=serial,
                title=name,
                total_pkgs=len(items),
                total_bytes=total_bytes,
            )

            def _progress(index: int, done: int, total: int):
                self._emit("pkg_progress", serial=serial, index=index, done=done, total=total)

            results = download_batch(items, progress_cb=_progress, stop_event=stop)
            cancelled = stop.is_set()
            self._emit(
                "download_finished",
                serial=serial,
                title=name,
                results=[(p, ok, m) for p, ok, m in results],
                cancelled=cancelled,
            )

        threading.Thread(target=_work, daemon=True).start()

    def cancel_download(self, serial: str):
        with self._lock:
            ev = self._stop_events.get(serial)
        if ev is not None:
            ev.set()
