"""PS3 RPCS3 Game Updater — core package.

Modules:
    models     – dataclasses (GameInfo, UpdateInfo) + serial validation
    scanner    – RPCS3 folder detection, games.yml + on-disk library scan
    psn        – PSN update lookup (verified v2 Sony update logic)
    downloader – .pkg download with SHA-1 verification + cancellation
    artwork    – local ICON0 / cache / optional remote art resolution
    engine     – threaded orchestration emitting UI events on a queue
"""

__version__ = "2.0.2"
