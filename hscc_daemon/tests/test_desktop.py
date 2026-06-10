"""Unit tests for desktop.py - desktop notifications and event emitter.

All subprocess calls (osascript, notify-send) are mocked. File I/O uses tmp_path.
"""
import json
import os
import pytest
from pathlib import Path


class TestNotifyMacos:
    """_notify_macos() sends native macOS notifications."""

    def test_non_darwin_returns_false(self, monkeypatch):
        from hscc_daemon import desktop
        import sys
        monkeypatch.setattr(sys, "platform", "linux")
        assert desktop._notify_macos("title", "body") is False

    def test_no_osascript_returns_false(self, monkeypatch):
        from hscc_daemon import desktop
        import sys
        import shutil
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(shutil, "which", lambda x: None)
        assert desktop._notify_macos("title", "body") is False

    def test_success(self, fake_subprocess, monkeypatch):
        from hscc_daemon import desktop
        import sys
        import shutil
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/osascript" if x == "osascript" else None)
        monkeypatch.setattr(desktop, "log", lambda *a, **kw: None)
        fake_subprocess.set_result(stdout="", returncode=0)
        assert desktop._notify_macos("title", "body") is True

    def test_failure(self, fake_subprocess, monkeypatch):
        from hscc_daemon import desktop
        import sys
        import shutil
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/osascript" if x == "osascript" else None)
        monkeypatch.setattr(desktop, "log", lambda *a, **kw: None)
        fake_subprocess.set_result(stdout="", stderr="error", returncode=1)
        assert desktop._notify_macos("title", "body") is False

    def test_timeout(self, fake_subprocess, monkeypatch):
        from hscc_daemon import desktop
        import sys
        import shutil
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/osascript" if x == "osascript" else None)
        monkeypatch.setattr(desktop, "log", lambda *a, **kw: None)
        fake_subprocess.set_result(timeout_exc=True)
        assert desktop._notify_macos("title", "body") is False

    def test_escapes_quotes(self, fake_subprocess, monkeypatch):
        from hscc_daemon import desktop
        import sys
        import shutil
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/osascript" if x == "osascript" else None)
        monkeypatch.setattr(desktop, "log", lambda *a, **kw: None)
        fake_subprocess.set_result(stdout="", returncode=0)
        # Should not crash with quotes in title/body
        desktop._notify_macos('title "with" quotes', 'body "text"')


class TestNotifyLinux:
    """_notify_linux() sends native Linux notifications."""

    def test_no_notify_send(self, monkeypatch):
        from hscc_daemon import desktop
        import shutil
        monkeypatch.setattr(shutil, "which", lambda x: None)
        assert desktop._notify_linux("title", "body") is False

    def test_success(self, fake_subprocess, monkeypatch):
        from hscc_daemon import desktop
        import shutil
        monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/notify-send" if x == "notify-send" else None)
        monkeypatch.setattr(desktop, "log", lambda *a, **kw: None)
        fake_subprocess.set_result(stdout="", returncode=0)
        assert desktop._notify_linux("title", "body") is True

    def test_priority_mapping(self, fake_subprocess, monkeypatch):
        from hscc_daemon import desktop
        import shutil
        monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/notify-send" if x == "notify-send" else None)
        monkeypatch.setattr(desktop, "log", lambda *a, **kw: None)
        fake_subprocess.set_result(stdout="", returncode=0)
        # high -> critical urgency
        desktop._notify_linux("title", "body", priority="high")

    def test_failure(self, fake_subprocess, monkeypatch):
        from hscc_daemon import desktop
        import shutil
        monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/notify-send" if x == "notify-send" else None)
        monkeypatch.setattr(desktop, "log", lambda *a, **kw: None)
        fake_subprocess.set_result(stdout="", returncode=1)
        assert desktop._notify_linux("title", "body") is False


class TestSendDesktopNotification:
    """send_desktop_notification() falls back to file when no native notifier."""

    def test_fallback_to_file(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import desktop
        import sys
        import shutil
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(shutil, "which", lambda x: None)  # no notify-send
        monkeypatch.setattr(desktop, "HSCC_DIR", str(tmp_hfcc_dir))
        monkeypatch.setattr(desktop, "log", lambda *a, **kw: None)

        result = desktop.send_desktop_notification("Test", "Body")
        assert result is False  # fallback doesn't return True

        notif_path = tmp_hfcc_dir / "notifications.json"
        assert notif_path.exists()
        data = json.loads(notif_path.read_text())
        assert len(data["notifications"]) == 1
        assert data["notifications"][0]["title"] == "Test"

    def test_appends_to_existing(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import desktop
        import sys
        import shutil
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(shutil, "which", lambda x: None)
        monkeypatch.setattr(desktop, "HSCC_DIR", str(tmp_hfcc_dir))
        monkeypatch.setattr(desktop, "log", lambda *a, **kw: None)

        # Pre-existing notification
        notif_path = tmp_hfcc_dir / "notifications.json"
        notif_path.write_text(json.dumps({"notifications": [{"title": "old"}]}))

        desktop.send_desktop_notification("New", "Body")
        data = json.loads(notif_path.read_text())
        assert len(data["notifications"]) == 2
        assert data["notifications"][1]["title"] == "New"

    def test_macos_notification_alias(self, tmp_hfcc_dir, monkeypatch):
        """send_macos_notification is an alias for send_desktop_notification."""
        from hscc_daemon import desktop
        import sys
        import shutil
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(shutil, "which", lambda x: None)
        monkeypatch.setattr(desktop, "HSCC_DIR", str(tmp_hfcc_dir))
        monkeypatch.setattr(desktop, "log", lambda *a, **kw: None)

        desktop.send_macos_notification("Test", "Body")
        notif_path = tmp_hfcc_dir / "notifications.json"
        assert notif_path.exists()


class TestEmitEvent:
    """emit_event() appends events to events.jsonl."""

    def test_emits_event(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import desktop
        monkeypatch.setattr(desktop, "HSCC_DIR", str(tmp_hfcc_dir))
        monkeypatch.setattr(desktop, "log", lambda *a, **kw: None)

        event = desktop.emit_event("test.type", {"key": "val"}, severity="warning")
        assert event is not None
        assert event["event_type"] == "test.type"
        assert event["severity"] == "warning"

        events_file = tmp_hfcc_dir / "events.jsonl"
        assert events_file.exists()
        lines = events_file.read_text().strip().split("\n")
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["payload"] == {"key": "val"}

    def test_appends_multiple(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import desktop
        monkeypatch.setattr(desktop, "HSCC_DIR", str(tmp_hfcc_dir))
        monkeypatch.setattr(desktop, "log", lambda *a, **kw: None)

        desktop.emit_event("e1", {"n": 1})
        desktop.emit_event("e2", {"n": 2})
        lines = (tmp_hfcc_dir / "events.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2

    def test_default_severity(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import desktop
        monkeypatch.setattr(desktop, "HSCC_DIR", str(tmp_hfcc_dir))
        monkeypatch.setattr(desktop, "log", lambda *a, **kw: None)

        event = desktop.emit_event("test", {})
        assert event["severity"] == "info"
        assert event["source"] == "hscc_daemon"

    def test_io_error_returns_none(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import desktop
        monkeypatch.setattr(desktop, "HSCC_DIR", str(tmp_hfcc_dir / "nope"))
        monkeypatch.setattr(desktop, "log", lambda *a, **kw: None)
        # tmp_hfcc_dir/nope doesn't exist -> IOError
        result = desktop.emit_event("test", {})
        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
