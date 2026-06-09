"""Test fixtures for hscc-provision plugin tests."""

import json
import os
import subprocess
import sys
import importlib

import pytest


class _SubprocessResult:
    """Fake subprocess.CompletedProcess."""
    def __init__(self, stdout="", stderr="", returncode=0,
                 timeout_exc=False, file_not_found=False):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self._timeout_exc = timeout_exc
        self._file_not_found = file_not_found


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_hfcc_dir(tmp_path):
    """Return a path to use as HSCC_DIR for a single test."""
    return tmp_path / "hscc"


@pytest.fixture
def provision_state_file(tmp_path):
    """Create a provision.json with known content.

    Returns (path, data_dict) so the caller can modify `data_dict` before
    calling save_provision_state, or read `data_dict` after loading.
    """
    state = {"mappings": {}, "history": []}
    path = tmp_path / "provision.json"
    path.write_text(json.dumps(state, indent=4))
    return path, state


def _ensure_hfcc_patched(monkeypatch, tmp_hfcc_dir, hscc_mod):
    """Apply HSCC_DIR/PROVISION_JSON/AGENTS_JSON patches + os.path.exists override."""
    monkeypatch.setattr(hscc_mod, "HSCC_DIR", str(tmp_hfcc_dir))
    monkeypatch.setattr(hscc_mod, "PROVISION_JSON", str(tmp_hfcc_dir / "provision.json"))
    monkeypatch.setattr(hscc_mod, "AGENTS_JSON", str(tmp_hfcc_dir / "agents.json"))

    _real_exists = os.path.exists

    def _fake_exists(path):
        p = str(path)
        # Allow access within temp dir
        if p.startswith(str(tmp_hfcc_dir.parent)):
            return _real_exists(p)
        if p == os.path.expanduser("~/.hermes/config.yaml"):
            return _real_exists(p)
        return False

    monkeypatch.setattr(os.path, "exists", _fake_exists)


@pytest.fixture
def hscc_module(tmp_hfcc_dir, monkeypatch):
    """Import the hscc module fresh for each test, with HSCC_DIR patched.

    Returns the module so tests can reference hscc module-level functions.
    """
    mod_name = "hscc"
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    hscc_mod = importlib.import_module(mod_name)
    _ensure_hfcc_patched(monkeypatch, tmp_hfcc_dir, hscc_mod)
    return hscc_mod


@pytest.fixture(autouse=True)
def patch_hfcc_dir(monkeypatch, tmp_hfcc_dir):
    """Override HSCC_DIR, PROVISION_JSON, and AGENTS_JSON to point into tmp_path.

    This is the autouse fixture — it patches the module on every test.
    """
    import importlib
    mod_name = "hscc"
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    hscc_mod = importlib.import_module(mod_name)
    _ensure_hfcc_patched(monkeypatch, tmp_hfcc_dir, hscc_mod)


@pytest.fixture(autouse=True)
def patch_hermes_config(monkeypatch):
    """Make get_hermes_inference_host return a fixed host or None."""
    fake_config = os.path.expanduser("~/.hermes/config.yaml")
    os.makedirs(os.path.dirname(fake_config), exist_ok=True)
    # Write empty config so get_hermes_inference_host can read it and return None
    with open(fake_config, "w") as f:
        f.write("")
    monkeypatch.setattr("hscc.HERMES_CONFIG", fake_config)


@pytest.fixture
def fake_ssh(monkeypatch):
    """Patch subprocess.run to control ssh/sparkrun command output.

    Returns a callable ``set_result(**kwargs)`` that overrides the default
    subprocess.run behaviour.  Each call to ``set_result`` changes the output
    for the *next* call.

    Example::

        fake_ssh.set_result(stdout="job info", returncode=0)
        fake_ssh.set_result(timeout_exc=True)  # next call raises
        fake_ssh.set_result(stdout="", stderr="err", returncode=1)

    Also returns a list ``_calls`` so tests can inspect what commands were run.
    """
    _queue = []  # list of _SubprocessResult
    _calls = []  # list of dicts with call info

    # Default result
    _queue.append(_SubprocessResult())

    def _fake_run(args, capture_output=False, text=None, timeout=None, env=None):
        result = _queue.pop(0) if _queue else _SubprocessResult()
        _calls.append({"args": args, "timeout": timeout, "env": env})
        if result._timeout_exc:
            raise subprocess.TimeoutExpired(cmd=args, timeout=timeout or 30)
        if result._file_not_found:
            raise FileNotFoundError(args[0] if isinstance(args, list) else str(args))
        return result

    def _set_result(stdout="", stderr="", returncode=0,
                    timeout_exc=False, file_not_found=False):
        r = _SubprocessResult(stdout=stdout, stderr=stderr, returncode=returncode,
                              timeout_exc=timeout_exc, file_not_found=file_not_found)
        _queue.append(r)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    return _set_result, _calls


@pytest.fixture
def sample_agents_json(tmp_path):
    """Create an agents.json with known content."""
    data = {
        "agents": [
            {"id": "agent-001", "model": "auto", "endpoint": "", "status": "idle"},
            {"id": "agent-002", "model": "auto", "endpoint": "", "status": "idle"},
        ]
    }
    path = tmp_path / "agents.json"
    path.write_text(json.dumps(data, indent=4))
    return path, data
