import subprocess

import execlib


def test_empty_command():
    r = execlib.sparkrun_exec({})
    assert r["ok"] is False and "required" in r["error"]


def test_non_sparkrun_rejected():
    r = execlib.sparkrun_exec({"command": "rm -rf /"})
    assert r["ok"] is False and "must start with 'sparkrun'" in r["error"]


def test_success(monkeypatch):
    def fake_run(argv, capture_output, text, timeout):
        assert argv == ["sparkrun", "status"]
        return subprocess.CompletedProcess(argv, 0, stdout="all good\n", stderr="")
    monkeypatch.setattr(execlib.subprocess, "run", fake_run)
    r = execlib.sparkrun_exec({"command": "sparkrun status"})
    assert r["ok"] is True
    assert r["exit_code"] == 0
    assert r["stdout"] == "all good"
    assert r["stderr"] is None


def test_nonzero_exit_captures_stderr(monkeypatch):
    def fake_run(argv, capture_output, text, timeout):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
    monkeypatch.setattr(execlib.subprocess, "run", fake_run)
    r = execlib.sparkrun_exec({"command": "sparkrun run bad-recipe"})
    assert r["ok"] is False
    assert r["exit_code"] == 1
    assert r["stderr"] == "boom"


def test_timeout_clamped(monkeypatch):
    seen = {}

    def fake_run(argv, capture_output, text, timeout):
        seen["timeout"] = timeout
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(execlib.subprocess, "run", fake_run)
    execlib.sparkrun_exec({"command": "sparkrun list", "timeout": 99999})
    assert seen["timeout"] == execlib.MAX_TIMEOUT


def test_timeout_expired(monkeypatch):
    def fake_run(argv, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)
    monkeypatch.setattr(execlib.subprocess, "run", fake_run)
    r = execlib.sparkrun_exec({"command": "sparkrun run x", "timeout": 5})
    assert r["ok"] is False and "timed out" in r["error"]


def test_not_installed(monkeypatch):
    def fake_run(argv, capture_output, text, timeout):
        raise FileNotFoundError()
    monkeypatch.setattr(execlib.subprocess, "run", fake_run)
    r = execlib.sparkrun_exec({"command": "sparkrun status"})
    assert r["ok"] is False and "not found" in r["error"]


def test_stringify_wrapper_returns_json():
    import __init__ as plugin
    wrapped = plugin._stringify(lambda a, **k: {"ok": True, "x": 1})
    out = wrapped({"command": "sparkrun status"})
    assert isinstance(out, str) and '"ok": true' in out
