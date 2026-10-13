"""Unit tests for api_launchd — the opt-in launchd unit for `hscc api`.

Everything is pure-text: build the plist dict, install it into a temp HOME,
read it back with plistlib. launchctl itself is never invoked (install
never loads; loading stays a manual operator step).
"""
import os
import plistlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import api_launchd  # noqa: E402


PLIST = "com.hscc.api.plist"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A private HOME so nothing near the real ~/Library is touched."""
    monkeypatch.setenv("HOME", str(tmp_path))
    plist_path = tmp_path / "Library" / "LaunchAgents" / PLIST
    monkeypatch.setattr(api_launchd, "API_PLIST_PATH", str(plist_path))
    return tmp_path


# --------------------------------------------------------------------------- #
# plist shape (the parts that matter for boot-resilience and safety)
# --------------------------------------------------------------------------- #

def test_plist_runs_api_server_serve_with_hermes_python():
    d = api_launchd.build_api_plist("/opt/venv/bin/python3", "/repo/hscc-api")
    assert d["ProgramArguments"] == ["/opt/venv/bin/python3", "-m", "api_server", "serve"]
    assert d["WorkingDirectory"] == "/repo/hscc-api"


def test_plist_survives_reboot_and_crash():
    d = api_launchd.build_api_plist("/p", "/d")
    assert d["RunAtLoad"] is True and d["KeepAlive"] is True
    assert d["Label"] == api_launchd.API_LABEL


def test_plist_is_plistlib_roundtrippable():
    import plistlib
    d = api_launchd.build_api_plist("/p", "/d")
    assert plistlib.loads(plistlib.dumps(d, sort_keys=False)) == d


def test_recommend_api_python_prefers_hermes_venv(tmp_path, monkeypatch):
    py = tmp_path / "hermes-agent" / "venv" / "bin" / "python3"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert api_launchd.recommend_api_python() == str(py)


def test_recommend_api_python_returns_none_when_no_venv(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "missing"))
    assert api_launchd.recommend_api_python() is None
    # install must REFUSE to write a plist that could never start
    with pytest.raises(RuntimeError):
        api_launchd.install_api_plist("/repo")


# --------------------------------------------------------------------------- #
# install / uninstall (file I/O only — never launchctl)
# --------------------------------------------------------------------------- #

def test_install_writes_readable_plist_and_reports_path(home):
    res = api_launchd.install_api_plist("/repo/hscc-api", python_path="/venv/bin/python3")
    assert res["changed"] is True
    with open(res["path"], "rb") as fh:
        d = plistlib.load(fh)
    assert d["Label"] == "com.hscc.api"
    assert d["ProgramArguments"] == ["/venv/bin/python3", "-m", "api_server", "serve"]
    assert d["EnvironmentVariables"]["HOME"] == str(home)


def test_install_is_idempotent_byte_identical_no_rewrite(home):
    api_launchd.install_api_plist("/repo", python_path="/venv/bin/python3")
    p = api_launchd.API_PLIST_PATH
    before = os.stat(p).st_mtime_ns
    res = api_launchd.install_api_plist("/repo", python_path="/venv/bin/python3")
    assert res["changed"] is False
    assert os.stat(p).st_mtime_ns == before


def test_install_rewrites_when_content_changes(home):
    api_launchd.install_api_plist("/repo-a", python_path="/venv/bin/python3")
    res = api_launchd.install_api_plist("/repo-b", python_path="/venv/bin/python3")
    assert res["changed"] is True
    with open(api_launchd.API_PLIST_PATH, "rb") as fh:
        assert plistlib.load(fh)["WorkingDirectory"] == "/repo-b"


def test_install_never_loads_the_unit(home):
    """The whole opt-in contract: writing the file must not touch launchctl.

    Expressed structurally: this module has no subprocess import AT ALL, so
    install cannot exec launchctl by construction. If a future edit wants to
    auto-load the unit, it must add the import and this test fails — the
    opt-in guarantee gets renegotiated in the diff, not in a docstring.
    """
    import inspect
    src = inspect.getsource(api_launchd)
    assert "import subprocess" not in src
    assert not hasattr(api_launchd, "subprocess")


def test_uninstall_removes_then_is_clean_noop(home):
    api_launchd.install_api_plist("/repo", python_path="/venv/bin/python3")
    assert api_launchd.uninstall_api_plist()["removed"] is True
    assert api_launchd.uninstall_api_plist()["removed"] is False


def test_plist_snippet_shows_manual_launchctl_route():
    s = api_launchd.plist_snippet("/repo/hscc-api")
    for needle in ("launchctl bootstrap", "plutil -lint", "api_server",
                   "WorkingDirectory", "LaunchAgents"):
        assert needle in s


# --------------------------------------------------------------------------- #
# the CLI seam: `hscc api install-service` / `uninstall-service`
# --------------------------------------------------------------------------- #

def _cli(tmp_path, monkeypatch, argv):
    """Run cmd_api with EVERY path pinned into tmp_path. The `home` fixture
    must be requested by the caller — without it API_PLIST_PATH still points
    at the operator's real ~/Library/LaunchAgents."""
    plist_path = tmp_path / "Library" / "LaunchAgents" / PLIST
    monkeypatch.setattr(api_launchd, "API_PLIST_PATH", str(plist_path))
    from hscc_daemon import api_cli
    py = tmp_path / "hermes-agent" / "venv" / "bin" / "python3"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(api_cli, "_resolve_api_dir", lambda: tmp_path / "hscc-api")
    (tmp_path / "hscc-api").mkdir(exist_ok=True)
    return api_cli.cmd_api(argv)


def test_cli_install_service_writes_unit_in_private_home(home, tmp_path, capsys, monkeypatch):
    rc = _cli(tmp_path, monkeypatch, ["install-service"])
    out = capsys.readouterr().out
    assert rc == 0
    assert str(tmp_path / "Library" / "LaunchAgents" / PLIST) in out
    assert "NOT loaded yet" in out                      # the opt-in promise, on screen
    assert "launchctl bootstrap" in out                 # the manual step is spelled out
    assert os.path.exists(api_launchd.API_PLIST_PATH)


def test_cli_install_service_is_idempotent(home, tmp_path, capsys, monkeypatch):
    _cli(tmp_path, monkeypatch, ["install-service"])
    capsys.readouterr()
    rc = _cli(tmp_path, monkeypatch, ["install-service"])
    assert rc == 0
    assert "already up to date" in capsys.readouterr().out


def test_cli_install_service_refuses_without_venv_python(home, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "missing"))
    from hscc_daemon import api_cli
    monkeypatch.setattr(api_cli, "_resolve_api_dir", lambda: tmp_path / "hscc-api")
    (tmp_path / "hscc-api").mkdir(exist_ok=True)
    rc = api_cli.cmd_api(["install-service"])
    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_cli_uninstall_service_roundtrip(tmp_path, capsys, monkeypatch):
    _cli(tmp_path, monkeypatch, ["install-service"])
    capsys.readouterr()
    rc = _cli(tmp_path, monkeypatch, ["uninstall-service"])
    assert rc == 0 and "removed" in capsys.readouterr().out
    assert not os.path.exists(api_launchd.API_PLIST_PATH)
    rc = _cli(tmp_path, monkeypatch, ["uninstall-service"])   # second time: no-op
    assert rc == 0 and "nothing to remove" in capsys.readouterr().out
