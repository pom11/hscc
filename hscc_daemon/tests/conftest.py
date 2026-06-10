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
import subprocess
import sys

import pytest


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
