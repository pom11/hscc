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
# bind/port passthrough into ProgramArguments (t_37948685)
#
# Why this exists: the operator's live API runs `hscc api start --tailscale`
# and had NO launchd equivalent to migrate to, because the argv was hardcoded.
# A migration that silently dropped the tailnet bind would move the iPhone
# client offline at the next reboot — so the flags must reach the unit.
# --------------------------------------------------------------------------- #

def test_serve_args_no_flags_is_empty_argv_tail():
    """The default unit is untouched: no flags -> nothing appended."""
    assert api_launchd.serve_args() == []
    assert api_launchd.serve_args(bind=None, port=None) == []


def test_serve_args_tailscale_is_the_keyword_not_a_resolved_ip():
    """The unit records the operator's INTENT, not a resolved address.

    `api_server.resolve_bind()` re-resolves the tailnet IP at every boot, so a
    unit written while Tailscale was down fails with the server's own error
    instead of carrying a stale IP that silently binds the wrong interface.
    """
    assert api_launchd.serve_args(bind="tailscale") == ["--tailscale"]


def test_serve_args_bind_loopback_and_explicit_ip():
    assert api_launchd.serve_args(bind="loopback") == ["--bind", "loopback"]
    # 100.64.0.1 is the documented tailnet placeholder (scripts/address_guard.py)
    assert api_launchd.serve_args(bind="100.64.0.1") == ["--bind", "100.64.0.1"]


def test_serve_args_port_and_combos():
    assert api_launchd.serve_args(port=9999) == ["--port", "9999"]
    assert api_launchd.serve_args(port="8788") == ["--port", "8788"]
    assert api_launchd.serve_args(bind="tailscale", port=8788) == \
        ["--tailscale", "--port", "8788"]
    assert api_launchd.serve_args(bind="100.64.0.1", port=8788) == \
        ["--bind", "100.64.0.1", "--port", "8788"]


def test_serve_args_refuses_wide_and_malformed_binds():
    """A unit that could not start is worse than no unit: launchd would
    respawn the failure forever under KeepAlive.

    0.0.0.0 and :: are the two literals api_server._REFUSED_BINDS always
    refuses, and they are caught here by ipaddress' `is_unspecified` rather
    than by a duplicated literal set (this module stays stdlib-only and cannot
    import api_server at install time). Anything else that is not an address —
    including the empty string, which api_server also refuses — is refused as
    malformed, which is the more honest message for a typo.
    """
    for bad in ("0.0.0.0", "::"):
        with pytest.raises(ValueError, match="untrusted network"):
            api_launchd.serve_args(bind=bad)
    for bad in ("not-an-ip", "100.64.0.999", "tailscale ", " localhost", "",
               "0.0.0.0/0"):
        with pytest.raises(ValueError, match="invalid --bind"):
            api_launchd.serve_args(bind=bad)


def test_serve_args_refuses_malformed_port():
    for bad in ("not-a-port", 0, -1, 70000):
        with pytest.raises(ValueError, match="port|invalid --port"):
            api_launchd.serve_args(port=bad)


def test_plist_program_arguments_carry_the_flags():
    d = api_launchd.build_api_plist(
        "/opt/venv/bin/python3", "/repo/hscc-api",
        serve_extra_args=api_launchd.serve_args(bind="tailscale", port=8788))
    assert d["ProgramArguments"] == [
        "/opt/venv/bin/python3", "-m", "api_server", "serve",
        "--tailscale", "--port", "8788"]
    # Still a valid plist (the flags must not break the plist round-trip).
    assert plistlib.loads(plistlib.dumps(d, sort_keys=False)) == d


def test_plist_no_flags_is_byte_identical_to_the_pre_flag_unit(home):
    """Byte-identical default: a plain loopback install must not shift one
    byte, or every existing unit would report 'changed' on the next install."""
    legacy = plistlib.dumps(
        {"Label": api_launchd.API_LABEL,
         "ProgramArguments": ["/venv/bin/python3", "-m", "api_server", "serve"],
         "WorkingDirectory": "/repo",
         "RunAtLoad": True, "KeepAlive": True,
         "StandardOutPath": os.path.expanduser("~/.hscc/logs/api.log"),
         "StandardErrorPath": os.path.expanduser("~/.hscc/logs/api.err.log"),
         "EnvironmentVariables": {
             "HOME": str(home),
             "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:"
                     + os.path.pathsep.join(
                         p for p in os.environ.get("PATH", "").split(
                             os.path.pathsep) if p)}},
        sort_keys=False)
    res = api_launchd.install_api_plist("/repo", python_path="/venv/bin/python3")
    with open(res["path"], "rb") as fh:
        assert fh.read() == legacy


def test_install_api_plist_writes_flags_and_reinstall_is_still_idempotent(home):
    extra = api_launchd.serve_args(bind="tailscale", port=8788)
    res = api_launchd.install_api_plist("/repo", python_path="/venv/bin/python3",
                                        serve_extra_args=extra)
    with open(res["path"], "rb") as fh:
        d = plistlib.load(fh)
    assert d["ProgramArguments"][-3:] == ["--tailscale", "--port", "8788"]
    # Same flags again -> no rewrite (the idempotence contract is unchanged).
    before = os.stat(api_launchd.API_PLIST_PATH).st_mtime_ns
    again = api_launchd.install_api_plist("/repo", python_path="/venv/bin/python3",
                                          serve_extra_args=extra)
    assert again["changed"] is False
    assert os.stat(api_launchd.API_PLIST_PATH).st_mtime_ns == before
    # Different flags -> a real change (the unit must follow the flags).
    flipped = api_launchd.install_api_plist("/repo", python_path="/venv/bin/python3",
                                            serve_extra_args=[])
    assert flipped["changed"] is True
    with open(api_launchd.API_PLIST_PATH, "rb") as fh:
        assert plistlib.load(fh)["ProgramArguments"] == [
            "/venv/bin/python3", "-m", "api_server", "serve"]


# --------------------------------------------------------------------------- #
# the CLI seam: `hscc api install-service` / `uninstall-service`
# --------------------------------------------------------------------------- #

def _cli(tmp_path, monkeypatch, argv):
    """Run cmd_api with EVERY path pinned into tmp_path. The `home` fixture
    must be requested by the caller — without it API_PLIST_PATH still points
    at the operator's real ~/Library/LaunchAgents.

    BOTH module objects get the pin, on purpose (t_37948685). This file imports
    the library bare (`import api_launchd`) while `_handle_install_service`
    does `from hscc_daemon import api_launchd` — same file, two DISTINCT module
    objects (probe-verified: `bare is pkg` is False), so patching only the bare
    one left the handler on a path baked from whatever HOME happened to be
    active when the package submodule was first imported. The existing CLI-seam
    tests passed only on that import-order luck; reading the plist back then
    hits a stale tmp dir from an earlier test. Patching the object the handler
    actually reads makes the seam deterministic.
    """
    plist_path = tmp_path / "Library" / "LaunchAgents" / PLIST
    monkeypatch.setattr(api_launchd, "API_PLIST_PATH", str(plist_path))
    import hscc_daemon.api_launchd as pkg_launchd
    monkeypatch.setattr(pkg_launchd, "API_PLIST_PATH", str(plist_path))
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


# --------------------------------------------------------------------------- #
# the CLI seam, flag passthrough (t_37948685)
# --------------------------------------------------------------------------- #

def test_cli_install_service_forwards_tailscale_into_the_unit(
        home, tmp_path, capsys, monkeypatch):
    """The migration case this card exists for: an operator whose live API is
    `hscc api start --tailscale` must be able to move onto launchd without the
    unit silently dropping the tailnet bind."""
    rc = _cli(tmp_path, monkeypatch, ["install-service", "--tailscale"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "--tailscale" in out                       # reported on screen
    with open(api_launchd.API_PLIST_PATH, "rb") as fh:
        d = plistlib.load(fh)
    assert d["ProgramArguments"] == [
        str(tmp_path / "hermes-agent" / "venv" / "bin" / "python3"),
        "-m", "api_server", "serve", "--tailscale"]


def test_cli_install_service_forwards_bind_and_port(
        home, tmp_path, capsys, monkeypatch):
    rc = _cli(tmp_path, monkeypatch,
              ["install-service", "--bind", "100.64.0.1", "--port", "8788"])
    assert rc == 0
    with open(api_launchd.API_PLIST_PATH, "rb") as fh:
        d = plistlib.load(fh)
    assert d["ProgramArguments"][-4:] == [
        "--bind", "100.64.0.1", "--port", "8788"]


def test_cli_install_service_drops_no_qr(
        home, tmp_path, capsys, monkeypatch):
    """--no-qr is display-only (the operator types it with `start`): accepted
    so the same command line works, and NOT written into a unit that has no
    terminal to print a QR to."""
    rc = _cli(tmp_path, monkeypatch, ["install-service", "--tailscale", "--no-qr"])
    assert rc == 0
    with open(api_launchd.API_PLIST_PATH, "rb") as fh:
        d = plistlib.load(fh)
    assert "--no-qr" not in d["ProgramArguments"]
    assert d["ProgramArguments"][-1] == "--tailscale"


def test_cli_install_service_refuses_wide_bind_before_writing(
        home, tmp_path, capsys, monkeypatch):
    """0.0.0.0 must be refused the same way `start` refuses it — and refused
    WITHOUT leaving a unit file behind."""
    rc = _cli(tmp_path, monkeypatch, ["install-service", "--bind", "0.0.0.0"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err and "untrusted network" in err
    assert not os.path.exists(api_launchd.API_PLIST_PATH)


def test_cli_install_service_refuses_bad_port_before_writing(
        home, tmp_path, capsys, monkeypatch):
    rc = _cli(tmp_path, monkeypatch, ["install-service", "--port", "bogus"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err
    assert not os.path.exists(api_launchd.API_PLIST_PATH)
