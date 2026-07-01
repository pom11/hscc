"""Telegram notification shim for HSCC plugins.

Wraps ``hscc_daemon.telegram.notify_operations`` so hscc-cluster can post
alerts to the ops Telegram topic without a hard dependency on the daemon
package. Best-effort: if the daemon is not importable, this module provides
a no-op notify_operations.
"""

def notify_operations(text):
    """Post a message to the HSCC ops Telegram topic.

    Delegates to ``hscc_daemon.telegram.notify_operations`` when the daemon
    is installed, otherwise silently does nothing (never raises).
    """
    try:
        from hscc_daemon import telegram as _tg
        return _tg.notify_operations(text)
    except ImportError:
        # Daemon not in path (e.g. running as a plugin in a different
        # profile) — best-effort, no-op.
        return False