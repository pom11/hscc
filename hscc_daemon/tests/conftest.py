"""Shared test fixtures for hscc_daemon unit tests.

Usage::

    # For state module:
    def test_something(tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir / "state"))
        state.ensure_state_dir()
        ...

    # For lifecycle module:
    def test_watchdog(tmp_hfcc_dir, monkeypatch):
        from hscc_daemon.lifecycle import save_watchdog_block
        monkeypatch.setattr("hscc_daemon.lifecycle.WATCHDOG_BLOCK_FILE",
                            str(tmp_hfcc_dir / "watchdog-block.json"))
        ...
"""

import json
import os
import subprocess
import sys

import pytest


# ---------------------------------------------------------------------------
# Autouse ~/.hscc isolation (RELEASE BLOCKER regression)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_hscc(tmp_path, monkeypatch):
    """Redirect EVERY ~/.hscc write path to a per-test tmp dir.

    Runs for EVERY test in the package (autouse), so no test can reach the
    operator's real ~/.hscc even when it forgets to patch individually.

    Why this exists (RELEASE BLOCKER): test_daemon_ops.py patched
    ``autodown.load_config`` to return {"enabled": True} but did NOT patch
    ``save_config`` / ``autodown.AUTODOWN_FILE``. Phase 6 added activity probes
    to ``cycle()``, so the timing had ``record_activity()`` running during
    those tests, calling the PATCHED loader and ``save_config``-ing the partial
    3-key dict straight to the REAL ``~/.hscc/autodown.json`` — silently arming
    idle-teardown on the live cluster. A later variant leaked into the LIVE
    ``~/.hscc/watchdog-block.json`` (test_trigger.py / test_health.py did not
    patch ``lifecycle.WATCHDOG_BLOCK_FILE``): the suite wrote
    {"blocked": true, "intentional": "autodown"} over the real block, silently
    disabling cluster supervision for ~1 hour.

    Belt-and-braces, covering the DIRECTORY AS A WHOLE:
      (a) Patch ``os.path.expanduser`` so any ``~/.hscc/...`` path computed at
          RUNTIME (function-scoped ``expanduser`` calls, e.g.
          lifecycle.py:30/:87, verify.py:132, daemon_ops usage) resolves under
          the per-test tmp dir. Everything outside ``~/.hscc`` is delegated to
          the real expanduser unchanged.
      (b) Patch every module-level ``~/.hscc``-rooted constant (each module
          computes its own copy at IMPORT time, so they are already baked in
          before this fixture runs and must be overwritten explicitly). See
          ``_module_attrs()`` — the enumerated audit in the card.
      (c) Redirect ``state.STATE_DIR`` to the tmp state dir too, so the common
          ``state.write_state`` funnel can never reach ``~/.hscc/state``.

    Function-scoped monkeypatch is torn down after each test, and any test
    that patches its own tmp path simply overrides (LIFO teardown → the test's
    patch wins during the test).
    """
    base = str(tmp_path / "hscc")

    from hscc_daemon import (
        autodown, daemon_ops, desktop, hscc, lifecycle, replay, state, trigger,
    )

    # (a) Runtime expanduser redirect for the whole ~/.hscc directory tree.
    real_hscc = os.path.expanduser("~/.hscc")
    _real_expanduser = os.path.expanduser

    def _redirect_expanduser(path):
        expanded = _real_expanduser(path)
        if expanded == real_hscc:
            return base
        if expanded.startswith(real_hscc + os.sep):
            return os.path.join(base, expanded[len(real_hscc) + 1:])
        return expanded

    monkeypatch.setattr(os.path, "expanduser", _redirect_expanduser)

    # (b) Module-level constants already computed at import — overwrite each.
    def _module_attrs():
        from hscc_daemon import (
            autodown, daemon_ops, desktop, hscc, lifecycle, replay, state,
            trigger,
        )
        def p(sub):
            return os.path.join(base, sub)
        return [
            # autodown.py
            (autodown, "AUTODOWN_FILE", p("autodown.json")),
            (autodown, "AUTODOWN_LOCK", p("autodown.lock")),
            (autodown, "AGENTS_FILE", p("agents.json")),
            (autodown, "HTTP_ACTIVITY_STATE", p("activity.json")),
            (autodown, "TELEGRAM_OFFSET_FILE", p("state/telegram_probe.offset")),
            # replay.py (inbound-message queue — never touch the real one)
            (replay, "QUEUE_FILE", p("queued_messages.json")),
            (replay, "WEBHOOK_DELIVER_URL",
             "http://127.0.0.1:9/webhooks/autodown-replay"),
            (replay, "WEBHOOK_DELIVER_SECRET", ""),
            # lifecycle.py
            (lifecycle, "BRIDGE_FILE", p("bridge.json")),
            (lifecycle, "WATCHDOG_BLOCK_FILE", p("watchdog-block.json")),
            # state.py (the write_state/read_state funnel)
            (state, "STATE_DIR", p("state")),
            # trigger.py
            (trigger, "EVENTS_FILE", p("events.jsonl")),
            (trigger, "TRIGGERS_FILE", p("triggers.json")),
            (trigger, "COOLDOWN_FILE", p("cooldowns.json")),
            # daemon_ops.py
            (daemon_ops, "PID_FILE", p("daemon.pid")),
            (daemon_ops, "LOG_FILE", p("daemon.log")),
            (daemon_ops, "STATE_DIR", p("state")),
            (daemon_ops, "HSCC_DIR", base),
            # desktop.py (emit_event / notifications live under HSCC_DIR)
            (desktop, "HSCC_DIR", base),
            # hscc.py CLI module
            (hscc, "BRIDGE_FILE", p("bridge.json")),
            (hscc, "ORCH_ENDPOINT_STATE", p("orch-endpoint")),
        ]

    for mod, attr, val in _module_attrs():
        monkeypatch.setattr(mod, attr, val, raising=False)

    # (c) Hermetic delivery stub so NO test can ever reach a live gateway.
    # Replay's default production delivery POSTs to the gateway webhook; the
    # WEBHOOK_DELIVER_URL module-attr redirect above already points it at a dead
    # port (9), and this stub is belt-and-suspenders so even an accidental call
    # to default_deliver_message returns False (fail-closed → message retained)
    # instead of attempting any network egress. We deliberately do NOT stub
    # autodown._notify / _waking_notice here: existing notification tests patch
    # notify_operations/send_macos_notification themselves and assert on them
    # firing, and the new replay tests pass their own injectable notify_fn /
    # notify=False — so a blanket _notify stub would silently swallow those and
    # break real assertions. Individual tests that want a no-op notifier still
    # patch it (LIFO teardown -> the test's patch wins during the test).
    monkeypatch.setattr(replay, "default_deliver_message",
                        lambda msg: False, raising=False)


class _SubprocessResult:
    """Fake subprocess.CompletedProcess for controlling command output."""

    def __init__(self, stdout="", stderr="", returncode=0,
                 timeout_exc=False, file_not_found=False):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self._timeout_exc = timeout_exc
        self._file_not_found = file_not_found


class _FakeSubprocess:
    """Patch subprocess.run and provide set_result() for the next call."""

    def __init__(self, monkeypatch):
        self._queue = []

    def _fake_run(self, *args, **kwargs):
        if self._queue:
            result = self._queue.pop(0)
        else:
            result = _SubprocessResult()
        if result._timeout_exc:
            raise subprocess.TimeoutExpired(
                cmd=args[0] if args else "cmd",
                timeout=kwargs.get("timeout", 30)
            )
        if result._file_not_found:
            raise FileNotFoundError(
                args[0][0] if args and isinstance(args[0], list) else "cmd"
            )
        return result

    def set_result(self, stdout="", stderr="", returncode=0,
                   timeout_exc=False, file_not_found=False):
        self._queue.append(_SubprocessResult(
            stdout=stdout, stderr=stderr, returncode=returncode,
            timeout_exc=timeout_exc, file_not_found=file_not_found
        ))


@pytest.fixture
def tmp_hfcc_dir(tmp_path):
    """Return a path to use as HSCC_DIR / state dir for a single test."""
    d = tmp_path / "hscc"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def state_dir(tmp_hfcc_dir):
    """Create an empty state directory."""
    d = tmp_hfcc_dir / "state"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def sample_state(state_dir):
    """Write a sample state file and return (path, data_dict)."""
    data = {
        "timestamp": "2026-06-09T12:00:00+00:00",
        "stream": "dgx",
        "ok": True,
        "details": {"gpu_count": 8, "ssh_reachable": True},
    }
    path = state_dir / "dgx.json"
    path.write_text(json.dumps(data, indent=2))
    return path, data


@pytest.fixture
def agents_json(tmp_hfcc_dir):
    """Create an agents.json with known content. Returns (path, data_dict)."""
    data = {
        "agents": [
            {"id": "agent-001", "model": "auto", "endpoint": "", "status": "idle"},
            {"id": "agent-002", "model": "auto", "endpoint": "", "status": "working"},
        ]
    }
    path = tmp_hfcc_dir / "agents.json"
    path.write_text(json.dumps(data, indent=4))
    return path, data


@pytest.fixture
def workers_json(tmp_hfcc_dir):
    """Create a workers.json with known content. Returns (path, data_dict)."""
    data = {
        "workers": [
            {"node": "10.0.0.246", "status": "online"},
            {"node": "10.0.0.247", "status": "offline"},
        ]
    }
    path = tmp_hfcc_dir / "workers.json"
    path.write_text(json.dumps(data, indent=4))
    return path, data


@pytest.fixture
def fake_subprocess(monkeypatch):
    """Patch subprocess.run to control command output.

    Patches the top-level subprocess module AND all hscc_daemon submodules
    that import subprocess, since they each hold their own reference.

    Returns an object with ``.set_result(**kwargs)`` that overrides the default
    subprocess.run behaviour for the *next* call.

    Example::

        fake_subprocess.set_result(stdout="hello", returncode=0)
        fake_subprocess.set_result(timeout_exc=True)
    """
    import hscc_daemon.util as util_mod
    import hscc_daemon.desktop as desktop_mod
    import hscc_daemon.serving as serving_mod
    import hscc_daemon.health as health_mod
    import hscc_daemon.daemon_ops as daemon_ops_mod
    import hscc_daemon.lifecycle as lifecycle_mod

    fs = _FakeSubprocess(monkeypatch)
    monkeypatch.setattr(subprocess, "run", fs._fake_run)
    for mod in (util_mod, desktop_mod, serving_mod, health_mod, daemon_ops_mod, lifecycle_mod):
        if hasattr(mod, "subprocess"):
            monkeypatch.setattr(mod.subprocess, "run", fs._fake_run)
    return fs
