"""Desktop notifications and event emitter."""

import json
import os
import shutil
import subprocess
import sys
import uuid

from . import log
from .state import now_iso


HSCC_DIR = os.path.expanduser("~/.hscc")


def _notify_macos(title, body, priority="normal"):
    """Native macOS notification via osascript. Returns True on success."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return False
    title_esc = title.replace('"', '\\"')
    body_esc = body.replace('"', '\\"')
    simple_script = (
        f'display notification \\"{body_esc}\\" with title \\"{title_esc}\\"'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", simple_script],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            log(f"macOS notification sent: {title}")
            return True
    except Exception:
        pass
    return False


def _notify_linux(title, body, priority="normal"):
    """Native Linux notification via notify-send (libnotify). Returns True on success."""
    notify_send = shutil.which("notify-send")
    if not notify_send:
        return False
    urgency = {"low": "low", "critical": "critical", "high": "critical"}.get(
        priority, "normal"
    )
    try:
        result = subprocess.run(
            [notify_send, "-a", "HSCC", "-u", urgency, title, body],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            log(f"Linux notification sent: {title}")
            return True
    except Exception:
        pass
    return False


def send_desktop_notification(title, body, priority="normal", app_id="com.hermes.hscc-daemon"):
    """Send a native desktop notification, falling back to notifications.json."""
    if _notify_macos(title, body, priority) or _notify_linux(title, body, priority):
        return True

    # Fallback: write to notifications file (works on any platform / headless)
    try:
        notif_data = {"notifications": []}
        notif_path = os.path.join(HSCC_DIR, "notifications.json")
        if os.path.exists(notif_path):
            with open(notif_path) as f:
                notif_data = json.load(f)
        notif_data["notifications"].append({
            "id": str(uuid.uuid4())[:8],
            "timestamp": now_iso(),
            "read": False,
            "priority": priority,
            "title": title,
            "body": body,
            "channel": "daemon",
        })
        with open(notif_path, "w") as f:
            json.dump(notif_data, f, indent=2)
        log(f"Notification saved to file (no native notifier available): {title}")
    except Exception:
        pass

    return False


# Backward-compatible alias: older callers used the macOS-specific name.
send_macos_notification = send_desktop_notification


def emit_event(event_type, payload, severity="info", source="hscc-daemon"):
    """Append an event to events.jsonl."""
    EVENTS_FILE = os.path.join(HSCC_DIR, "events.jsonl")
    event = {
        "event_type": event_type,
        "severity": severity,
        "source": source,
        "timestamp": now_iso(),
        "payload": payload,
    }
    try:
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(event, default=str) + "\n")
        return event
    except IOError:
        log(f"Failed to write event: {event_type}", "ERROR")
        return None
