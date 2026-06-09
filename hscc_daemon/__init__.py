"""HSCC Daemon — modular split from hscc.py.

Submodules:
  serving     — Cluster topology (cluster.json / serving.json)
  state       — State directory management
  util        — Utility functions
  health      — Check functions (dgx, gateway, nas, workers, etc.)
  lifecycle   — Agent reconciliation, pipeline_watchdog
  trigger     — Trigger engine
  desktop     — Desktop notifications, event emitter
  daemon_ops  — Daemon lifecycle (PID, log, stream watcher)
  install     — Service management (launchd/systemd)
  cli         — CLI commands and main entry point
"""

__version__ = "2026.06.09.2"
__all__ = ["serving", "state", "util", "health", "lifecycle", "trigger", "desktop", "daemon_ops", "install", "cli"]

# Re-export log so modules can use 'from . import log'
from .daemon_ops import log
