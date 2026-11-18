"""PKG downloading — adapter over the verified v2 update logic.

download_pkg() semantics (preserved exactly):
  * skips re-download when a file of the right size already exists
  * SHA-1 checked against Sony's metadata; on mismatch the file is KEPT with a
    warning message (Sony has stale hashes for some re-issued packages and
    RPCS3 verifies PKG signatures itself at install time)
  * partial files are deleted on network failure or user stop

plan_downloads() builds '<base>/PlayStation 3/[ID] Name' folders like PySN.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import ps3_rpcs3_game_updater as _orig
except ImportError:  # pragma: no cover - path fallback for unusual launch dirs
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import ps3_rpcs3_game_updater as _orig


sha1_of_file = _orig.sha1_of_file
download_pkg = _orig.download_pkg
plan_downloads = _orig.plan_downloads

PlanItem = Tuple[dict, Path]  # (pkg dict, planned destination path)


def download_batch(
    items: List[PlanItem],
    progress_cb=None,
    stop_event=None,
):
    """Download [(pkg, dest_path)] sequentially in install order.

    Returns a list of (actual_path, ok, message) — one entry per item.
    progress_cb(pkg_index, done_bytes, total_bytes) is called periodically.
    When stop_event is set the remaining items are reported as skipped and no
    further network traffic happens.
    """
    results: List[Tuple[str, bool, str]] = []
    for i, (pkg, dest_path) in enumerate(items):
        if stop_event is not None and stop_event.is_set():
            results.append((str(dest_path), False, "skipped (cancelled)"))
            continue

        def _cb(done: int, total: int, _i=i):
            if progress_cb is not None:
                try:
                    progress_cb(_i, done, total)
                except Exception:  # noqa: BLE001 - UI callback must never kill the worker
                    pass

        path, ok, msg = download_pkg(
            pkg, dest_path.parent, progress_cb=_cb, stop_event=stop_event
        )
        # download_pkg names the saved file from the URL basename, which
        # differs from the planned destination for DRM-free entries
        # ("DRM-Free <file>"). Report the ACTUAL path: the GUI filters the
        # completed list with is_file() before offering the RPCS3 install,
        # so the nonexistent planned name silently killed that offer.
        results.append((path or str(dest_path), ok, msg))
    return results
