"""HSCC CLI wrapper — forwards to hscc_daemon package."""
import sys
from pathlib import Path

# Add plugins dir to path so hscc_daemon can be imported
_plugins = Path(__file__).resolve().parent.parent.parent / ".."
if str(_plugins) not in sys.path:
    sys.path.insert(0, str(_plugins))

from hscc_daemon.hscc import main

__all__ = ["main"]
