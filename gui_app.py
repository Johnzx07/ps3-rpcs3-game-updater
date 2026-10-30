"""PS3 RPCS3 Game Updater — polished dark game-library GUI (tkinter + ttk).

Design: charcoal/navy surfaces, restrained electric-blue accents, games shown
as visual tiles with cover art (local ICON0 -> cached art -> refined
placeholder), search, status filters, per-game details, download progress and
cancellation. All Sony-facing behavior lives in updater.engine /
ps3_rpcs3_game_updater;
this module only renders state and forwards user intent.
"""
from __future__ import annotations

import os
import queue as _queue
import subprocess
import sys
import threading
import ctypes
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover - bundled build includes psutil
    psutil = None

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:  # Pillow is optional; art degrades to text placeholders without it
    from PIL import Image as _PILImage
    from PIL import ImageTk as _ImageTk
except ImportError:  # pragma: no cover
    _PILImage = None
    _ImageTk = None

from updater.artwork import make_placeholder
from updater.config import (
    config_dir,
    default_downloads_dir,
    load as load_config,
    save as save_config,
)
from updater.engine import UpdateEngine
from updater.models import is_valid_serial, normalize_serial
from updater.psn import fmt_size


def _log_uncaught(exc: BaseException, where: str):
    """Persist an unexpected error to errors.log in the app data folder.

    Windowed PyInstaller builds have no console, so anything that escapes the
    guarded paths would otherwise vanish silently. This keeps those failures
    diagnosable (the GUI log panel mirrors the most common ones too).
    """
    try:
        path = Path(config_dir()) / "errors.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {where}: {exc!r}\n")
    except Exception:  # noqa: BLE001 - logging must never raise
        pass


APP_TITLE = "PS3 RPCS3 Game Updater"
APP_VERSION = "2.0.2"
# These assets are bundled alongside the executable by ps3-rpcs3-game-updater.spec.
# Keep the source and frozen-app lookup identical by resolving from this module.
BRAND_PNG = os.path.join(_ROOT, "assets", "logo_header.png")
ICON_PATH = os.path.join(_ROOT, "assets", "app.ico")

# ---------------------------------------------------------------- palette ---
C_BG = "#0b0e14"        # app background (near-black navy)
C_PANEL = "#12161f"     # header / side panels
C_CARD = "#151a26"      # tile surface
C_CARD_HI = "#1d2436"   # hover surface
C_BORDER = "#232b3d"    # resting borders
C_TEXT = "#e8ecf4"
C_SUB = "#9aa7bd"
C_FAINT = "#5c6a84"
C_ACCENT = "#2f7bff"    # electric blue
C_ACCENT_HI = "#5b96ff"
C_OK = "#3ddc84"
C_WARN = "#ffb020"
C_ERR = "#ff5d5d"

FONT = "Segoe UI"
# Compact, square app-icon treatment for mixed ICON0/box-art sources.  Keeping
# a fixed icon box and preserving the source aspect ratio avoids the stretched
# poster/banner look of the original card design.
COVER_W = COVER_H = 104
TILE_W = 220
# GameTile disables pack propagation so its cover/action layout remains stable.
# Give it an explicit height as well; without one Tk allocates a one-pixel grid
# row, leaving only the card borders visible in the library view.
TILE_H = 276


def _font(size: int, weight: str = "normal"):
    return (FONT, size, weight)


# ------------------------------------------------------------------ tiles ---
class GameTile(tk.Frame):
    """One game card: cover art, title, serial, status, progress, actions."""

    def __init__(self, master, app, game):
        super().__init__(
            master, bg=C_CARD, width=TILE_W, height=TILE_H,
            highlightbackground=C_BORDER, highlightcolor=C_ACCENT, highlightthickness=1,
        )
        self.pack_propagate(False)
        self.app = app
        self.game = game
        self._photo = None

        cover_bg = tk.Frame(self, bg="#0e1219", width=TILE_W - 8, height=COVER_H + 12)
        cover_bg.pack(padx=4, pady=(6, 0))
        cover_bg.pack_propagate(False)
        self.cover_label = tk.Label(cover_bg, bg="#0e1219")
        self.cover_label.place(relx=0.5, rely=0.5, anchor="center")

        pad = {"px": 12}
        self.title_label = tk.Label(
            self, text=game.display_title, bg=C_CARD, fg=C_TEXT,
            font=_font(10, "bold"), wraplength=TILE_W - 28, justify="left", anchor="w",
        )
        self.title_label.pack(fill="x", padx=(pad["px"], pad["px"]), pady=(8, 0))

        self.serial_label = tk.Label(
            self, text=game.serial, bg=C_CARD, fg=C_FAINT, font=_font(8), anchor="w",
        )
        self.serial_label.pack(fill="x", padx=(pad["px"], pad["px"]))

        self.status_label = tk.Label(
            self, text="Checking…", bg=C_CARD, fg=C_SUB, font=_font(9), anchor="w",
        )
        self.status_label.pack(fill="x", padx=(pad["px"], pad["px"]), pady=(6, 0))

        self.size_label = tk.Label(self, text="", bg=C_CARD, fg=C_FAINT, font=_font(8), anchor="w")
        self.size_label.pack(fill="x", padx=(pad["px"], pad["px"]))

        self.progress = ttk.Progressbar(self, length=TILE_W - 24, mode="determinate", style="Accent.Horizontal.TProgressbar")
        self.progress.pack(fill="x", padx=12, pady=(6, 0))
        self.progress.pack_forget()

        btns = tk.Frame(self, bg=C_CARD)
        btns.pack(fill="x", padx=8, pady=(8, 10))
        self.update_btn = _flat_button(btns, "Update", lambda: app.request_download(game.serial), accent=True)
        self.update_btn.pack(side="left")
        self.cancel_btn = _flat_button(btns, "Cancel", lambda: app.cancel_download(game.serial))
        self.details_btn = _flat_button(btns, "Details", lambda: app.show_details(game.serial))
        self.details_btn.pack(side="right")

        self.bind("<Button-1>", lambda e: app.select_game(game.serial))
        self.title_label.bind("<Button-1>", lambda e: app.select_game(game.serial))
        self.cover_label.bind("<Button-1>", lambda e: app.select_game(game.serial))

    # ------------------------------------------------------------- rendering
    def set_image(self, pil):
        if _ImageTk is None or pil is None:
            return
        try:
            # Do not distort portrait covers, landscape title screens, or the
            # square ICON0.PNG files that RPCS3 exposes.  Every source becomes
            # a clean, centered app icon on the same dark backing.
            im = pil.convert("RGBA").copy()
            im.thumbnail((COVER_W, COVER_H), _PILImage.LANCZOS)
            icon = _PILImage.new("RGBA", (COVER_W, COVER_H), (14, 18, 25, 255))
            icon.alpha_composite(im, ((COVER_W - im.width) // 2, (COVER_H - im.height) // 2))
            self._photo = _ImageTk.PhotoImage(icon)
            self.cover_label.configure(image=self._photo)
        except Exception:  # noqa: BLE001 - art must never break the UI
            pass

    def refresh(self):
        app, game = self.app, self.game
        serial = game.serial
        updates = app.engine.updates.get(serial, [])
        state = app.lookup_state.get(serial)
        dl = app.downloading.get(serial)

        if dl is not None:
            total_pkgs = max(1, dl["total"])
            total_bytes = max(1, dl["total_bytes"])
            # dl["done"] counts bytes for the CURRENT package only; the handler
            # banks finished packages into done_before, so per-package size
            # differences no longer distort the fraction (e.g. 62.5% for 37.5%).
            done_bytes = min(dl.get("done_before", 0) + dl.get("done", 0), total_bytes)
            frac = done_bytes / total_bytes
            self.progress.configure(maximum=1000, value=int(frac * 1000))
            if not self.progress.winfo_ismapped():
                self.progress.pack(fill="x", padx=12, pady=(6, 0), before=self.size_label)
            self.status_label.configure(text=f"Downloading pkg {min(dl['index'] + 1, total_pkgs)} of {total_pkgs}", fg=C_ACCENT_HI)
            self.size_label.configure(
                text=fmt_size(done_bytes) + (" / " + fmt_size(dl["total_bytes"]) if dl["total_bytes"] else "")
            )
            self.update_btn.configure(state="disabled")
            self.cancel_btn.configure(state="normal")
        else:
            if self.progress.winfo_ismapped():
                self.progress.pack_forget()
            self.cancel_btn.configure(state="disabled")

            if state == "error":
                err = app.lookup_errors.get(serial, "lookup failed")
                self.status_label.configure(text="Lookup failed", fg=C_ERR)
                self.size_label.configure(text=err[:60])
                self.update_btn.configure(state="disabled")
            elif updates:
                latest = updates[-1]
                extra = f"  (+{len(updates) - 1} earlier)" if len(updates) > 1 else ""
                self.status_label.configure(text=f"Update available · {latest.version or '?'}{extra}", fg=C_ACCENT_HI)
                self.size_label.configure(text=fmt_size(latest.size_bytes))
                self.update_btn.configure(state="normal")
            elif state == "ok":
                self.status_label.configure(text="Up to date", fg=C_OK)
                self.size_label.configure(text="")
                self.update_btn.configure(state="disabled")
            else:
                self.status_label.configure(text="Checking…", fg=C_SUB)
                self.size_label.configure(text="")
                self.update_btn.configure(state="disabled")

        selected = app.selected_serial == serial
        self.configure(highlightbackground=C_ACCENT if selected else C_BORDER)


def _flat_button(parent, text, command, accent=False):
    return tk.Button(
        parent, text=text, command=command, bd=0, cursor="hand2",
        font=_font(9, "bold" if accent else "normal"),
        bg=C_ACCENT if accent else C_CARD_HI, fg="#ffffff" if accent else C_TEXT,
        activebackground=C_ACCENT_HI if accent else "#28324a", activeforeground="#ffffff",
        padx=10, pady=5,
    )


# -------------------------------------------------------------------- app ---
class PS3RPCS3GameUpdaterApp:
    FILTERS = (
        ("all", "All"),
        ("installed", "Installed"),
        ("update", "Need update"),
        ("updated", "Up to date"),
        ("error", "Errors"),
    )

    def __init__(self, root: tk.Tk):
        self.root = root
        cfg = load_config()
        self.engine = UpdateEngine()
        self.engine.configure(
            rpcs3_dir=cfg.get("rpcs3") or "",
            downloads_dir=cfg.get("downloads") or default_downloads_dir(),
            max_downloads=int(cfg.get("max_downloads", 4) or 4),
            allow_remote_art=True,
        )

        self.selected_serial: str = ""
        self.filter = "all"
        self.query = ""
        self.lookup_state: dict = {}      # serial -> checking|ok|error
        self.lookup_errors: dict = {}     # serial -> message
        self._pinned_serial = ""          # last Title ID checked: its card always shows
        self.downloading: dict = {}       # serial -> progress info
        self._dl_queue: list = []
        self.tiles: dict = {}             # serial -> GameTile
        self._photos: dict = {}           # serial -> PhotoImage (GC guard)
        self._empty_box = None
        self._cols_cache = 0
        self._pending_rpcs3_install = None

        self._build_ui()
        root.after(80, self._poll_events)
        self.engine.rescan()

    # ------------------------------------------------------------- UI build
    def _build_ui(self):
        r = self.root
        r.title(APP_TITLE)
        r.configure(bg=C_BG)
        r.minsize(980, 640)
        try:
            if os.path.isfile(ICON_PATH):
                r.iconbitmap(ICON_PATH)
        except Exception:  # noqa: BLE001 - non-Windows / missing icon
            pass

        style = ttk.Style(r)
        try:
            style.theme_use("clam")
        except tk.TclError:  # pragma: no cover
            pass
        style.configure(
            "Accent.Horizontal.TProgressbar",
            troughcolor=C_PANEL, background=C_ACCENT, bordercolor=C_PANEL,
            lightcolor=C_ACCENT, darkcolor=C_ACCENT, thickness=6,
        )

        self._build_header()
        self._build_toolbar()

        content = tk.Frame(r, bg=C_BG)
        content.pack(fill="both", expand=True, padx=10, pady=(4, 0))
        self._content = content

        body = tk.Frame(content, bg=C_BG)
        body.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(body, bg=C_BG, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.grid_frame = tk.Frame(self.canvas, bg=C_BG)
        self._grid_win = self.canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", self._on_canvas_resize)

        # The sidebar is intentionally hidden until a game card is selected.
        # Keeping it inside the content container lets it claim real horizontal
        # space instead of being squeezed below the scrollable library.
        self.side = tk.Frame(content, bg=C_PANEL, width=320)
        self.side.pack_propagate(False)
        self._sidebar_visible = False
        self._build_side_panel()

        self.status_var = tk.StringVar(value="Scanning library…")
        status_bar = tk.Frame(r, bg=C_PANEL)
        status_bar.pack(fill="x", side="bottom")
        tk.Label(status_bar, textvariable=self.status_var, bg=C_PANEL, fg=C_SUB, font=_font(9), anchor="w").pack(side="left", padx=12, pady=6)
        self.scan_progress = ttk.Progressbar(status_bar, length=180, mode="indeterminate", style="Accent.Horizontal.TProgressbar")

        r.bind_all("<MouseWheel>", self._on_mousewheel)
        for ev in ("<Button-4>", "<Button-5>"):  # Linux scroll
            try:
                r.bind_all(ev, self._on_mousewheel)
            except tk.TclError:  # pragma: no cover
                pass

    def _build_header(self):
        header = tk.Frame(self.root, bg=C_PANEL, height=64)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        brand = None
        if _PILImage is not None and os.path.isfile(BRAND_PNG):
            try:
                im = _PILImage.open(BRAND_PNG).convert("RGBA").resize((40, 40), _PILImage.LANCZOS)
                self._brand_photo = _ImageTk.PhotoImage(im)
                brand = tk.Label(header, image=self._brand_photo, bg=C_PANEL)
            except Exception:  # noqa: BLE001
                brand = None
        if brand is None:
            brand = tk.Label(header, text="PS3", bg=C_PANEL, fg=C_ACCENT_HI, font=_font(16, "bold"))
        brand.pack(side="left", padx=(14, 10))

        titles = tk.Frame(header, bg=C_PANEL)
        titles.pack(side="left")
        tk.Label(titles, text=f"{APP_TITLE}  •  v{APP_VERSION}", bg=C_PANEL, fg=C_TEXT, font=_font(13, "bold"), anchor="w").pack(anchor="nw")
        self.rpcs3_label = tk.Label(titles, text="", bg=C_PANEL, fg=C_FAINT, font=_font(8), anchor="w")
        self.rpcs3_label.pack(anchor="sw")

        rescan_btn = _flat_button(header, "⟳  Rescan", self.rescan)
        rescan_btn.pack(side="right", padx=(0, 12))
        settings_btn = _flat_button(header, "⚙  Settings", lambda: self.show_settings())
        settings_btn.pack(side="right", padx=(0, 8))

    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg=C_BG)
        bar.pack(fill="x", side="top", padx=14, pady=(10, 2))

        tk.Label(bar, text="Search library", bg=C_BG, fg=C_FAINT, font=_font(8)).pack(side="left", padx=(0, 5))
        self.search_var = tk.StringVar()
        search = tk.Entry(
            bar, textvariable=self.search_var, bd=0, relief="solid",
            bg=C_PANEL, fg=C_TEXT, insertbackground=C_ACCENT_HI,
            font=_font(10), highlightthickness=1, highlightbackground=C_BORDER,
            highlightcolor=C_ACCENT,
        )
        search.pack(side="left", ipady=6, fill="x", expand=True)
        search.bind("<KeyRelease>", lambda _event: self.search_library())
        search.bind("<Return>", lambda _event: self.search_library())
        _flat_button(bar, "Find", self.search_library).pack(side="left", padx=(4, 2))
        _flat_button(bar, "Clear", self.clear_library_search).pack(side="left", padx=(0, 6))

        tk.Label(bar, text="  ", bg=C_BG).pack(side="left")
        for key, label in self.FILTERS:
            b = _flat_button(bar, label, lambda k=key: self._set_filter(k))
            b.pack(side="left", padx=3)
            setattr(self, f"_filter_btn_{key}", b)

        tk.Label(bar, text="  Title ID", bg=C_BG, fg=C_FAINT, font=_font(8)).pack(side="left")
        self.check_var = tk.StringVar()
        check_entry = tk.Entry(
            bar, textvariable=self.check_var, width=12, bd=0, relief="solid",
            bg=C_PANEL, fg=C_TEXT, insertbackground=C_ACCENT_HI,
            font=_font(9), highlightthickness=1, highlightbackground=C_BORDER,
            highlightcolor=C_ACCENT,
        )
        check_entry.pack(side="left", padx=(4, 4), ipady=5)
        check_entry.bind("<Return>", lambda _event: self.check_title_id())
        _flat_button(bar, "Check updates", self.check_title_id).pack(side="left", padx=(0, 6))

        self.update_all_btn = _flat_button(bar, "Update all available", self.update_all_available, accent=True)
        self.update_all_btn.pack(side="right")
        self.count_label = tk.Label(bar, text="", bg=C_BG, fg=C_FAINT, font=_font(9))
        self.count_label.pack(side="right", padx=(0, 12))

    def _build_side_panel(self):
        p = self.side
        heading = tk.Frame(p, bg=C_PANEL)
        heading.pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(heading, text="GAME DETAILS", bg=C_PANEL, fg=C_FAINT, font=_font(8, "bold")).pack(side="left")
        _flat_button(heading, "×", self._hide_sidebar).pack(side="right")

        self.detail_frame = tk.Frame(p, bg=C_PANEL)
        self.detail_frame.pack(fill="both", expand=True)
        self._detail_labels: dict = {}
        for key in ("Title", "Serial", "Installed", "Latest update", "Size", "Source"):
            row = tk.Frame(self.detail_frame, bg=C_PANEL)
            row.pack(fill="x", padx=16, pady=2)
            tk.Label(row, text=key + ":", bg=C_PANEL, fg=C_FAINT, font=_font(9), width=10, anchor="w").pack(side="left")
            lbl = tk.Label(row, text="—", bg=C_PANEL, fg=C_TEXT, font=_font(9), anchor="w", wraplength=170, justify="left")
            lbl.pack(side="left", fill="x", expand=True)
            self._detail_labels[key] = lbl

        self.updates_listbox = tk.Listbox(
            p, bg=C_CARD, fg=C_SUB, font=_font(9), relief="flat", bd=0,
            highlightthickness=1, highlightbackground=C_BORDER, selectbackground=C_ACCENT,
            activestyle="none", exportselection=False,
        )
        self.updates_listbox.pack(fill="x", padx=16, pady=(8, 4))

        btns = tk.Frame(p, bg=C_PANEL)
        btns.pack(fill="x", padx=12, pady=6)
        self.detail_download_btn = _flat_button(btns, "Download updates", lambda: self.request_download(self.selected_serial), accent=True)
        self.detail_download_btn.pack(side="left")
        _flat_button(btns, "Open download folder", self.open_download_folder).pack(side="right")

        tk.Label(p, text="ACTIVITY", bg=C_PANEL, fg=C_FAINT, font=_font(8, "bold")).pack(anchor="w", padx=16, pady=(8, 2))
        self.log_text = tk.Text(
            p, height=9, bg="#0d1119", fg=C_SUB, font=("Consolas", 8), relief="flat", bd=0,
            state="disabled", wrap="word", highlightthickness=1, highlightbackground=C_BORDER,
        )
        self.log_text.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    # ------------------------------------------------------------- helpers
    def _set_status(self, text: str):
        self.status_var.set(text)

    def _log(self, line: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_mousewheel(self, event):
        delta = getattr(event, "delta", 0) or (120 if event.num == 4 else -120)
        self.canvas.yview_scroll(-int(delta // 120), "units")

    def _on_canvas_resize(self, _event=None):
        w = max(300, self.canvas.winfo_width())
        cols = max(1, (w - 24) // (TILE_W + 20))
        if cols != self._cols_cache:
            self._cols_cache = cols
            self._repack_tiles()

    # ------------------------------------------------------------- filtering
    def _set_filter(self, key):
        self.filter = key
        for k, _ in self.FILTERS:
            btn = getattr(self, f"_filter_btn_{k}")
            active = (k == key)
            btn.configure(bg=C_ACCENT if active else C_CARD_HI, fg="#ffffff" if active else C_TEXT)
        self._apply_filter()

    def _game_status(self, game):
        serial = game.serial
        updates = self.engine.updates.get(serial, [])
        if self.lookup_state.get(serial) == "error":
            return "error"
        if updates:
            return "update"
        if self.lookup_state.get(serial) == "ok":
            return "updated"
        return "checking"

    def _matches(self, game):
        q = self.query.strip().lower()
        if q:
            # Match what the card actually shows, including names fetched from
            # TMDB for titles that were not in games.yml.
            name = self.engine.names.get(game.serial, game.display_title)
            if q not in name.lower() and q not in game.serial.lower():
                return False
        if self.filter == "all":
            return True
        if self.filter == "installed":
            # Installed means the game exists in the scanned RPCS3 library, so a
            # title resolved only by Title ID does not belong in this view.
            return bool(game.path)
        # A title checked by Title ID must stay visible while its state is
        # unknown: every status filter hides cards whose state is still
        # "checking", which is why a checked card could never appear while
        # another status filter was active.
        if game.serial == self._pinned_serial:
            return True
        return self._game_status(game) == self.filter

    def _apply_filter(self):
        self.query = self.search_var.get()
        self._rebuild_tiles()

    def search_library(self):
        """Find a game by title, or look it up when the box holds a Title ID.

        Text is matched against the library — the names shown on the cards,
        including names resolved from Sony's TMDB — so typing a title once is
        enough. A nine-character Title ID is treated as a lookup instead of a
        filter, because library text can only match titles the app already knows
        about: a game that is not installed has no entry to match against, so the
        Title ID is the only universal handle for it.
        """
        query = self.search_var.get().strip()
        if is_valid_serial(query):
            self.check_var.set(normalize_serial(query))
            self.check_title_id()
            return
        self._apply_filter()
        if query:
            shown = len(self._visible_games())
            self._set_status(f"{shown} game(s) match “{query}”.")

    def clear_library_search(self):
        self.search_var.set("")
        self._apply_filter()
        self._set_status(f"Showing {len(self.engine.library)} game(s) in your library.")

    # -------------------------------------------------------------- tiles
    def _visible_games(self):
        return [g for g in self.engine.library if self._matches(g)]

    def _rebuild_tiles(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self._empty_box = None  # the empty-state box, if any, was destroyed above
        self.tiles = {}
        games = self._visible_games()
        cols = max(1, (max(300, self.canvas.winfo_width()) - 24) // (TILE_W + 20))
        for i, game in enumerate(games):
            tile = GameTile(self.grid_frame, self, game)
            self.tiles[game.serial] = tile
            img = None
            if _PILImage is not None:
                try:
                    img = self.engine.resolver.resolve_local(game) or self.engine.art.get(game.serial)
                except Exception:  # noqa: BLE001
                    img = None
            if img is None and _PILImage is not None:
                try:
                    img = make_placeholder(game.display_title, (COVER_W * 2, COVER_H * 2))
                except Exception:  # noqa: BLE001
                    img = None
            tile.set_image(img)
            if img is not None and _ImageTk is not None:
                self._photos[game.serial] = tile._photo
            tile.refresh()
            tile.grid(row=i // cols, column=i % cols, padx=10, pady=10, sticky="n")

        need = sum(1 for g in games if self._game_status(g) == "update")
        total = len(self.engine.library)
        self.count_label.configure(text=f"{total} games · {need} need updates" if total else "")
        self._toggle_empty_state(not games)

    def _repack_tiles(self):
        cols = max(1, self._cols_cache)
        for i, (serial, tile) in enumerate(self.tiles.items()):
            tile.grid_forget()
            tile.grid(row=i // cols, column=i % cols, padx=10, pady=10, sticky="n")

    def _toggle_empty_state(self, show: bool):
        if show and self._empty_box is None:
            box = tk.Frame(self.grid_frame, bg=C_BG, name="empty_state")
            self._empty_box = box
            box.grid(row=0, column=0, columnspan=8, pady=60)
            inner = tk.Frame(box, bg=C_CARD, highlightbackground=C_BORDER, highlightthickness=1)
            inner.pack(padx=40, pady=32)
            if _PILImage is not None and os.path.isfile(BRAND_PNG):
                try:
                    im = _PILImage.open(BRAND_PNG).convert("RGBA").resize((72, 72), _PILImage.LANCZOS)
                    self._empty_photo = _ImageTk.PhotoImage(im)
                    tk.Label(inner, image=self._empty_photo, bg=C_CARD).pack(pady=(24, 8))
                except Exception:  # noqa: BLE001
                    pass
            if self.engine.library:
                title = "No games match this view"
                detail = "Clear the search or choose a different filter."
            else:
                title = "No PS3 games found"
                detail = "Choose your RPCS3 folder to scan games.yml and local game data,\nor enter a 9-character Title ID above to check one game directly."
            tk.Label(inner, text=title, bg=C_CARD, fg=C_TEXT, font=_font(13, "bold")).pack()
            tk.Label(
                inner, text=detail,
                bg=C_CARD, fg=C_SUB, font=_font(9), justify="center",
            ).pack(pady=(6, 14))
            _flat_button(inner, "Open Settings", self.show_settings).pack()

    # ------------------------------------------------------------- selection
    def select_game(self, serial: str):
        self.selected_serial = serial
        self._show_sidebar()
        for s, t in self.tiles.items():
            if s == serial:
                t.configure(highlightbackground=C_ACCENT)
            elif not self.downloading.get(s):
                t.configure(highlightbackground=C_BORDER)
        self._refresh_details()

    def _scroll_to_serial(self, serial: str):
        """Bring a game card into view. Cosmetic, so it must never raise."""
        tile = self.tiles.get(serial)
        if tile is None:
            return
        try:
            tile.update_idletasks()
            bbox = self.canvas.bbox("all")
            height = max(1, int(bbox[3])) if bbox else 1
            y = max(0, int(tile.winfo_y()))
            self.canvas.yview_moveto(min(1.0, y / height))
        except Exception:  # noqa: BLE001 - scrolling must not break the UI
            pass

    def show_details(self, serial: str):
        """Open the in-window sidebar for a game card's details."""
        self.select_game(serial)

    def _show_sidebar(self):
        if not self._sidebar_visible:
            self.side.pack(side="right", fill="y", padx=(10, 0))
            self._sidebar_visible = True

    def _hide_sidebar(self):
        if self._sidebar_visible:
            self.side.pack_forget()
            self._sidebar_visible = False

    def _refresh_details(self):
        game = next((g for g in self.engine.library if g.serial == self.selected_serial), None)
        L = self._detail_labels
        if game is None:
            for k, lbl in L.items():
                lbl.configure(text="—")
            self.updates_listbox.delete(0, "end")
            self.detail_download_btn.configure(state="disabled")
            return
        updates = self.engine.updates.get(game.serial, [])
        latest = updates[-1] if updates else None
        L["Title"].configure(text=self.engine.names.get(game.serial) or game.display_title)
        L["Serial"].configure(text=game.serial)
        L["Installed"].configure(text=game.version or "unknown")
        L["Latest update"].configure(
            text=(f"{latest.version}  ({fmt_size(latest.size_bytes)})" if latest else ("up to date" if self.lookup_state.get(game.serial) == "ok" else "checking…"))
        )
        L["Size"].configure(text=fmt_size(sum(u.size_bytes for u in updates)) if updates else "")
        L["Source"].configure(text=f"{game.source} · {os.path.basename(game.path)}" if game.path else game.source)
        self.updates_listbox.delete(0, "end")
        for u in reversed(updates):  # newest first
            self.updates_listbox.insert("end", f"{u.version or '?':<12} {fmt_size(u.size_bytes):>9}")
        has = bool(updates) and game.serial not in self.downloading
        self.detail_download_btn.configure(state="normal" if has else "disabled")

    def _download_folder_for(self, serial: str, completed_paths=None) -> Path:
        """Return the game-specific package folder, falling back to downloads."""
        paths = [Path(path) for path in (completed_paths or []) if path]
        if paths:
            return paths[0].parent

        base = Path(self.engine.downloads_dir) / "PlayStation 3"
        if base.is_dir() and serial:
            matches = sorted(path for path in base.iterdir() if path.is_dir() and path.name.startswith(f"[{serial}]"))
            if matches:
                return matches[0]
        return base if base.is_dir() else Path(self.engine.downloads_dir)

    def open_download_folder(self, serial: str = "", completed_paths=None):
        """Open the folder that contains this game's downloaded PKG files."""
        serial = serial or self.selected_serial
        folder = self._download_folder_for(serial, completed_paths)
        if not folder.is_dir():
            messagebox.showinfo(
                APP_TITLE,
                "No download folder exists for this game yet. Download an update first.",
                parent=self.root,
            )
            return
        try:
            os.startfile(str(folder))  # type: ignore[attr-defined]
        except AttributeError:  # non-Windows fallback
            try:
                subprocess.Popen(["xdg-open", str(folder)])
            except OSError:  # no file opener available on this system
                self._log("Could not open the download folder (no file opener available).")
        except Exception:  # noqa: BLE001
            self._log("Could not open the download folder.")

    def _offer_rpcs3_install(self, serial: str, completed_paths):
        """Ask once, after a successful download, before opening RPCS3's installer."""
        paths = [str(Path(path)) for path in completed_paths if Path(path).is_file()]
        if not paths:
            return
        folder = self._download_folder_for(serial, paths)
        if messagebox.askyesno(
            APP_TITLE,
            "Download complete.\n\n"
            f"Open RPCS3 and install the package(s) for {serial} now?\n\n"
            f"RPCS3 will receive this folder:\n{folder}\n\n"
            "You will be able to review the package list in RPCS3 before installation.",
            parent=self.root,
        ):
            self._install_in_rpcs3(serial, paths)

    @staticmethod
    def _rpcs3_is_running(rpcs3_exe: Path) -> bool:
        """Return whether this (or another) RPCS3 process holds the single-instance lock."""
        if psutil is None:
            return False
        target = os.path.normcase(os.path.abspath(str(rpcs3_exe)))
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                name = (proc.info.get("name") or "").lower()
                executable = proc.info.get("exe")
                if executable and os.path.normcase(os.path.abspath(executable)) == target:
                    return True
                # RPCS3 itself rejects any second rpcs3.exe instance, including
                # one launched from a different configured install directory.
                if name == "rpcs3.exe":
                    return True
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
        return False

    def _queue_rpcs3_install(self, serial: str, completed_paths, rpcs3_exe: Path):
        """Hold one approved install until the user closes an existing RPCS3."""
        if self._pending_rpcs3_install is not None:
            messagebox.showinfo(
                APP_TITLE,
                "An RPCS3 package installation is already waiting for RPCS3 to close.\n\n"
                "The downloaded package remains safely in its download folder.",
                parent=self.root,
            )
            return
        self._pending_rpcs3_install = (serial, completed_paths, rpcs3_exe)
        self._set_status("RPCS3 is already open — installation is queued until it closes.")
        self._log(f"Queued {serial} for RPCS3; waiting for the existing instance to close.")
        self.root.after(1500, self._poll_pending_rpcs3_install)

    def _poll_pending_rpcs3_install(self):
        pending = self._pending_rpcs3_install
        if pending is None:
            return
        serial, completed_paths, rpcs3_exe = pending
        if self._rpcs3_is_running(rpcs3_exe):
            self.root.after(1500, self._poll_pending_rpcs3_install)
            return
        self._pending_rpcs3_install = None
        self._set_status("RPCS3 closed — opening its package installer now…")
        self._log(f"RPCS3 closed; opening the queued package installer for {serial}.")
        self._launch_rpcs3_package_installer(serial, completed_paths, rpcs3_exe)

    def _install_in_rpcs3(self, serial: str, completed_paths):
        """Queue or launch RPCS3's documented single-instance package installer."""
        rpcs3_exe = Path(self.engine.rpcs3_dir) / "rpcs3.exe"
        if not rpcs3_exe.is_file():
            messagebox.showwarning(
                APP_TITLE,
                "RPCS3 was not found in the configured folder.\n\n"
                "Choose the folder that contains rpcs3.exe in Settings, then try again.",
                parent=self.root,
            )
            return
        if self._rpcs3_is_running(rpcs3_exe):
            self._queue_rpcs3_install(serial, completed_paths, rpcs3_exe)
            return
        self._launch_rpcs3_package_installer(serial, completed_paths, rpcs3_exe)

    def _launch_rpcs3_package_installer(self, serial: str, completed_paths, rpcs3_exe: Path):
        """Start one RPCS3 instance after its single-instance lock is available."""
        folder = self._download_folder_for(serial, completed_paths)
        try:
            # RPCS3's --installpkg argument accepts a package directory and
            # opens its native installer, where the user can review the ordered
            # package set before committing the install.
            # A packaged updater is extracted into a temporary _MEI... folder.
            # Do not let RPCS3 inherit that directory: Windows can otherwise
            # resolve its Visual C++ runtime DLL from the updater's temporary
            # extraction folder instead of from the system/RPCS3 context.
            child_env = os.environ.copy()
            extraction_dir = getattr(sys, "_MEIPASS", None)
            if extraction_dir:
                # PyInstaller may add its one-file extraction folder to PATH.
                # It contains this app's VC runtime; leaking it to RPCS3 can
                # make RPCS3 load that DLL rather than its own/system runtime.
                extraction_dir = os.path.normcase(os.path.abspath(extraction_dir))
                safe_path_entries = []
                for entry in child_env.get("PATH", "").split(os.pathsep):
                    resolved_entry = os.path.normcase(os.path.abspath(entry))
                    if resolved_entry == extraction_dir or resolved_entry.startswith(extraction_dir + os.sep):
                        continue
                    safe_path_entries.append(entry)
                child_env["PATH"] = os.pathsep.join(safe_path_entries)
            # PyInstaller's Windows bootloader also calls SetDllDirectoryW for
            # its own process. That loader state is inherited by a direct child
            # even after PATH has been cleaned, so clear it only while creating
            # RPCS3, then restore the updater's extraction directory.
            restore_dll_directory = None
            if os.name == "nt" and extraction_dir:
                set_dll_directory = ctypes.windll.kernel32.SetDllDirectoryW
                set_dll_directory.argtypes = [ctypes.c_wchar_p]
                set_dll_directory.restype = ctypes.c_bool
                if not set_dll_directory(None):
                    raise OSError(ctypes.get_last_error(), "Could not clear the inherited DLL directory")
                restore_dll_directory = set_dll_directory
            try:
                subprocess.Popen(
                    [str(rpcs3_exe), "--installpkg", str(folder)],
                    cwd=str(rpcs3_exe.parent),
                    env=child_env,
                )
            finally:
                if restore_dll_directory:
                    restore_dll_directory(extraction_dir)
            self._set_status(
                f"Opened RPCS3's package installer for {serial}. "
                "RPCS3 may finish its game-library scan before showing the install flow."
            )
            self._log(f"Opened RPCS3 package installer for {serial}: {folder}")
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not start RPCS3:\n\n{exc}", parent=self.root)

    # ------------------------------------------------------------- downloads
    def check_title_id(self):
        serial = normalize_serial(self.check_var.get())
        if not is_valid_serial(serial):
            self._set_status("Enter a 9-character Title ID, for example BLUS30675.")
            return
        self.check_var.set(serial)
        self.lookup_errors.pop(serial, None)
        self.lookup_state.pop(serial, None)
        # Checking a Title ID must jump straight to that game. Clear the library
        # search and the status filter — a status filter hides every card whose
        # state is not known yet, so a checked title could never appear — pin the
        # serial, rebuild, and bring its card into view.
        self.search_var.set("")
        self.query = ""
        self._pinned_serial = serial
        self._set_filter("all")
        self._set_status(f"Checking updates for {serial}…")
        self._log(f"Checking updates for {serial}…")
        self.engine.check_title_id(serial)
        self.select_game(serial)
        self._scroll_to_serial(serial)

    def request_download(self, serial: str):
        if not serial or serial in self.downloading:
            return
        updates = self.engine.updates.get(serial)
        if not updates:
            messagebox.showinfo(APP_TITLE, "No published updates found for this game.")
            return
        self._dl_queue.append(serial)
        self._pump_downloads()

    def update_all_available(self):
        queued = 0
        for g in self.engine.library:
            if self.engine.updates.get(g.serial) and g.serial not in self.downloading and g.serial not in self._dl_queue:
                self._dl_queue.append(g.serial)
                queued += 1
        if queued:
            self._log(f"Queued {queued} game(s) for update.")
        self._pump_downloads()

    def _pump_downloads(self):
        active = len(self.downloading)
        while self._dl_queue and active < max(1, self.engine.max_downloads):
            serial = self._dl_queue.pop(0)
            if serial in self.downloading or not self.engine.updates.get(serial):
                continue
            pkgs = self.engine.updates[serial]
            self.downloading[serial] = {
                "index": 0, "total": len(pkgs),
                "done": 0, "done_before": 0,
                "total_bytes": sum(u.size_bytes for u in pkgs),
            }
            name = self.engine.names.get(serial) or next(
                (g.display_title for g in self.engine.library if g.serial == serial), serial
            )
            self._log(f"Downloading updates for {name} [{serial}] — {len(pkgs)} pkg(s).")
            self.engine.start_download(serial)
            active += 1
        for s in list(self.downloading):
            t = self.tiles.get(s)
            if t is not None:
                t.refresh()

    def cancel_download(self, serial: str):
        if serial in self.downloading:
            self.engine.cancel_download(serial)
            self._log(f"Stopping download for {serial}…")

    # ------------------------------------------------------------- settings
    def show_settings(self):
        dlg = tk.Toplevel(self.root)
        dlg.title(f"Settings — {APP_TITLE}")
        dlg.configure(bg=C_PANEL)
        dlg.resizable(False, False)
        try:
            if os.path.isfile(ICON_PATH):
                dlg.iconbitmap(ICON_PATH)
        except Exception:  # noqa: BLE001
            pass

        def _row(label_text):
            row = tk.Frame(dlg, bg=C_PANEL)
            row.pack(fill="x", padx=18, pady=6)
            tk.Label(row, text=label_text, bg=C_PANEL, fg=C_SUB, font=_font(9), width=20, anchor="w").pack(side="left")
            return row

        rpcs3_row = _row("RPCS3 folder:")
        v_rpc = tk.StringVar(value=self.engine.rpcs3_dir)
        e_rpc = tk.Entry(rpcs3_row, textvariable=v_rpc, bg=C_CARD, fg=C_TEXT, insertbackground=C_ACCENT_HI, bd=0, font=_font(9), highlightthickness=1, highlightbackground=C_BORDER)
        e_rpc.pack(side="left", fill="x", expand=True, ipady=5)

        dl_row = _row("Downloads folder:")
        v_dl = tk.StringVar(value=self.engine.downloads_dir)
        e_dl = tk.Entry(dl_row, textvariable=v_dl, bg=C_CARD, fg=C_TEXT, insertbackground=C_ACCENT_HI, bd=0, font=_font(9), highlightthickness=1, highlightbackground=C_BORDER)
        e_dl.pack(side="left", fill="x", expand=True, ipady=5)

        def _browse(var):
            d = filedialog.askdirectory(title="Choose folder", parent=dlg)
            if d:
                var.set(d)

        b1 = _flat_button(rpcs3_row, "Browse…", lambda: _browse(v_rpc))
        b1.pack(side="left", padx=(8, 0))
        b2 = _flat_button(dl_row, "Browse…", lambda: _browse(v_dl))
        b2.pack(side="left", padx=(8, 0))

        md_row = _row("Max concurrent downloads:")
        v_md = tk.StringVar(value=str(self.engine.max_downloads))
        tk.Spinbox(md_row, from_=1, to=16, textvariable=v_md, width=5, bg=C_CARD, fg=C_TEXT, bd=0, font=_font(9), buttonbackground=C_CARD_HI).pack(side="left")

        art_var = tk.BooleanVar(value=self.engine.allow_remote_art)
        tk.Checkbutton(
            dlg, text="Fetch remote artwork when local cover is missing", variable=art_var,
            bg=C_PANEL, fg=C_SUB, activebackground=C_PANEL, activeforeground=C_TEXT,
            selectcolor=C_CARD, font=_font(9), anchor="w",
        ).pack(fill="x", padx=18, pady=(4, 10))

        def _save():
            changed = v_rpc.get() != self.engine.rpcs3_dir
            try:
                max_downloads = max(1, int(v_md.get() or 1))
            except ValueError:  # e.g. a non-numeric spinbox value must not crash save
                max_downloads = self.engine.max_downloads
            self.engine.configure(
                rpcs3_dir=v_rpc.get().strip(),
                downloads_dir=v_dl.get().strip() or default_downloads_dir(),
                max_downloads=max_downloads,
                allow_remote_art=bool(art_var.get()),
            )
            save_config({
                "downloads": self.engine.downloads_dir,
                "rpcs3": self.engine.rpcs3_dir,
                "max_downloads": str(self.engine.max_downloads),
            })
            dlg.destroy()
            if changed:
                self.rescan()

        btns = tk.Frame(dlg)
        btns.pack(fill="x", padx=18, pady=(4, 16))
        _flat_button(btns, "Save & rescan", _save, accent=True).pack(side="right")
        _flat_button(btns, "Cancel", dlg.destroy).pack(side="right", padx=(0, 8))

    def rescan(self):
        self.lookup_state = {}
        self.lookup_errors = {}
        for s in list(self.downloading):
            self.engine.cancel_download(s)
        self._set_status("Scanning library…")
        self.scan_progress.pack(side="right", padx=12)
        self.scan_progress.start(12)
        self.rpcs3_label.configure(text=f"RPCS3: {self.engine.rpcs3_dir or 'not detected'}")
        self._rebuild_tiles()
        self.engine.rescan()

    # ---------------------------------------------------------- event pump
    def _poll_events(self):
        try:
            while True:
                kind, data = self.engine.events.get_nowait()
                handler = getattr(self, f"_on_{kind}", None)
                if handler is not None:
                    try:
                        handler(data)
                    except Exception as exc:  # noqa: BLE001 - one bad event must not kill the pump
                        _log_uncaught(exc, f"event '{kind}'")
                        try:
                            self._log(f"UI error handling '{kind}': {exc}")
                        except Exception:  # noqa: BLE001 - logging must never kill the pump
                            pass
        except _queue.Empty:
            pass
        self.root.after(80, self._poll_events)

    def _refresh_tile(self, serial):
        t = self.tiles.get(serial)
        if t is None:
            # The lookup resolved for a card that was never built: tiles only
            # exist for games passing the current filter, so a title that was
            # still "checking" when it was added had no card. Rebuild so the card
            # appears as soon as the lookup finishes, then bring it into view.
            self._rebuild_tiles()
            t = self.tiles.get(serial)
        else:
            t.refresh()
        if t is not None and serial == self._pinned_serial:
            self._scroll_to_serial(serial)
        if serial == self.selected_serial:
            self._refresh_details()

    def _on_scanned(self, data):
        self.rpcs3_label.configure(text=f"RPCS3: {data['rpcs3_dir'] or 'not detected'}")
        n = len(data["games"])
        self._set_status(f"{n} game(s) found — checking for updates…")
        self._rebuild_tiles()

    def _on_game_added(self, data):
        game = data["game"]
        self._set_status(f"Checking updates for {game.serial}…")
        self._rebuild_tiles()
        self.select_game(game.serial)
        if game.serial == self._pinned_serial:
            self._scroll_to_serial(game.serial)

    def _on_scan_error(self, data):
        self.scan_progress.stop()
        self.scan_progress.pack_forget()
        self._set_status("Scan failed: " + data["error"])
        messagebox.showerror(APP_TITLE, f"Could not scan the RPCS3 folder:\n\n{data['error']}")

    def _on_updates_found(self, data):
        serial = data["serial"]
        self.lookup_state[serial] = "ok"
        img = self.engine.art.get(serial)
        t = self.tiles.get(serial)
        if t is not None and img is not None and t._photo is None:
            t.set_image(img)
            self._photos[serial] = t._photo
        self._refresh_tile(serial)

    def _on_lookup_error(self, data):
        serial = data["serial"]
        self.lookup_state[serial] = "error"
        self.lookup_errors[serial] = data["error"]
        self._log(f"[{serial}] lookup failed: {data['error']}")
        self._refresh_tile(serial)

    def _on_scan_done(self, _data):
        self.scan_progress.stop()
        self.scan_progress.pack_forget()
        need = sum(1 for s in self.engine.updates if self.engine.updates[s])
        total = len(self.engine.library)
        self._set_status(f"Done — {need} of {total} game(s) have updates available.")

    def _on_download_started(self, data):
        info = self.downloading.get(data["serial"])
        if info is not None:
            info["total"] = data["total_pkgs"]
            info["total_bytes"] = max(info["total_bytes"], data["total_bytes"])
        self._refresh_tile(data["serial"])

    def _on_pkg_progress(self, data):
        info = self.downloading.get(data["serial"])
        if info is not None:
            if data["index"] > info["index"]:
                # The previous package finished — bank its bytes before moving on.
                info["done_before"] = info.get("done_before", 0) + info.get("done", 0)
                info["index"] = data["index"]
                info["done"] = 0
            info["done"] = data["done"]
            if data["total"]:
                info["total_bytes"] = max(info["total_bytes"], data["total"])
        self._refresh_tile(data["serial"])

    def _on_art_ready(self, data):
        """Apply remotely resolved cover art to the tile for that serial."""
        serial = data["serial"]
        img = data.get("image")
        t = self.tiles.get(serial)
        if t is None or img is None or _ImageTk is None:
            return
        t.set_image(img)
        if t._photo is not None:
            self._photos[serial] = t._photo

    def _on_download_finished(self, data):
        serial = data["serial"]
        self.downloading.pop(serial, None)
        ok_count = sum(1 for _, ok, _ in data["results"] if ok)
        completed_paths = [path for path, ok, _ in data["results"] if ok]
        total = len(data["results"])
        verb = "cancelled" if data.get("cancelled") else ("finished" if ok_count == total and total else "finished with errors")
        self._log(f"{data['title']} [{serial}]: {ok_count}/{total} pkg(s) — {verb}.")
        for path, ok, msg in data["results"]:
            mark = "✓" if ok else "✗"
            self._log(f"   {mark} {os.path.basename(path)}: {msg}")
        self._set_status(
            f"{data['title']}: {ok_count}/{total} package(s) downloaded." + (" (cancelled)" if data.get("cancelled") else "")
        )
        self._refresh_tile(serial)
        self._pump_downloads()
        if completed_paths and not data.get("cancelled"):
            self.root.after(0, lambda: self._offer_rpcs3_install(serial, completed_paths))


# --------------------------------------------------------------------- main ---
def main():
    # Windowed frozen builds have no console: persist anything uncaught so a
    # silent failure is still diagnosable from the app data folder.
    def _hook(args):
        _log_uncaught(args.exc_value, "uncaught")
    sys.excepthook = _hook
    try:
        threading.excepthook = _hook
    except AttributeError:  # pragma: no cover - pre-3.8 Pythons
        pass
    root = tk.Tk()
    PS3RPCS3GameUpdaterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
