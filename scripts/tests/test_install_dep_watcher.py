"""Tests for scripts/install_dep_watcher.sh — the profile/scripts-dir drift rule.

Background (t_3aadd2c0): Hermes resolves a bare ``--script NAME`` ONLY against
the bound home's ``scripts/`` dir ($HERMES_HOME/scripts; profiles never share
files). The installer used to hard-code the SHARED ~/.hermes/scripts while
``hermes cron create`` bound the INVOKING profile's home — the mismatch made
hscc-dep-watcher fail 17 consecutive daily runs ("Script not found"). These
tests pin the invariant: the copy destination and the cron binding must resolve
from the SAME home, namely $HERMES_HOME (default ~/.hermes).
"""

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALLER = os.path.join(REPO, "scripts", "install_dep_watcher.sh")
POLLER = os.path.join(REPO, "scripts", "dep_pr_watcher.py")


def _run(tmp_path, *, args=(), hermes_home=None, log_name="cron_stub.log"):
    """Run the installer with HOME=tmp_path/home and HERMES_HOME=hermes_home.

    ``hermes`` itself is stubbed: the installer prefers
    ``$HOME/.hermes/hermes-agent/venv/bin/hermes``, so we plant an executable
    there that appends its argv to $log_name. Nothing touches the real cron.
    """
    home = tmp_path / "home"
    stub_bin = home / ".hermes" / "hermes-agent" / "venv" / "bin"
    # exist_ok: a test may call _run twice on the same tmp_path (install then uninstall).
    stub_bin.mkdir(parents=True, exist_ok=True)
    log = tmp_path / log_name
    stub = stub_bin / "hermes"
    stub.write_text(
        '#!/usr/bin/env bash\n'
        f'printf \'%s\\n\' "$*" >> "{log}"\n'
        'exit 0\n'
    )
    stub.chmod(0o755)

    env = dict(os.environ)
    env["HOME"] = str(home)
    if hermes_home is None:
        env.pop("HERMES_HOME", None)
    else:
        env["HERMES_HOME"] = str(hermes_home)
    # Never let a PATH `hermes` shadow the stub (the installer falls back to it).
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    proc = subprocess.run(
        ["bash", INSTALLER, *args], capture_output=True, text=True, env=env,
        cwd=str(tmp_path), timeout=60,
    )
    return proc, (log.read_text() if log.exists() else "")


def test_installer_binds_scripts_dir_to_hermes_home(tmp_path):
    """Copy destination == $HERMES_HOME/scripts, not the hard-coded shared dir."""
    profile_home = tmp_path / "profile"
    proc, log = _run(tmp_path, hermes_home=profile_home)
    assert proc.returncode == 0, proc.stderr
    installed = profile_home / "scripts" / "dep_pr_watcher.py"
    assert installed.is_file(), "poller must land inside the bound home's scripts/"
    # The stub (== the same home `hermes cron create` binds) received the job.
    assert "cron create" in log and "--script dep_pr_watcher.py" in log
    assert "--name hscc-dep-watcher" in log
    # remove-before-create keeps idempotency.
    assert log.index("cron remove hscc-dep-watcher") < log.index("cron create")
    # And it must NOT have leaked into the (now distinct) shared location.
    shared = tmp_path / "home" / ".hermes" / "scripts" / "dep_pr_watcher.py"
    assert not shared.exists()


def test_default_home_unchanged_when_hermes_home_unset(tmp_path):
    """Without HERMES_HOME the installer keeps using ~/.hermes (shared home)."""
    proc, _log = _run(tmp_path, hermes_home=None)
    assert proc.returncode == 0, proc.stderr
    installed = tmp_path / "home" / ".hermes" / "scripts" / "dep_pr_watcher.py"
    assert installed.is_file()


def test_uninstall_removes_from_the_same_home(tmp_path):
    """--uninstall deletes exactly the copy a matching install created."""
    profile_home = tmp_path / "profile"
    proc, log = _run(tmp_path, hermes_home=profile_home)
    assert proc.returncode == 0, proc.stderr
    installed = profile_home / "scripts" / "dep_pr_watcher.py"
    assert installed.is_file()
    proc, log = _run(tmp_path, args=("--uninstall",), hermes_home=profile_home,
                     log_name="cron_stub_uninstall.log")
    assert proc.returncode == 0, proc.stderr
    assert not installed.exists()
    assert "cron remove hscc-dep-watcher" in log


def test_installed_copy_matches_repo_source(tmp_path):
    """The drift rule covers content too: installed == repo byte-for-byte."""
    profile_home = tmp_path / "profile"
    proc, _log = _run(tmp_path, hermes_home=profile_home)
    assert proc.returncode == 0, proc.stderr
    installed = (profile_home / "scripts" / "dep_pr_watcher.py").read_bytes()
    assert installed == open(POLLER, "rb").read()


def test_static_drift_pins():
    """Guard against re-hard-coding the shared path (the original bug shape)."""
    src = open(INSTALLER, encoding="utf-8").read()
    assert 'HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"' in src
    assert 'HERMES_SCRIPTS="$HERMES_HOME/scripts"' in src
    assert 'HERMES_SCRIPTS="$HOME/.hermes/scripts"' not in src
