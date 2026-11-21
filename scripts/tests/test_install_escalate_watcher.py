"""Tests for scripts/install_escalate_watcher.sh — the profile/scripts-dir drift rule.

Same defect shape as t_3aadd2c0 (fixed there for install_dep_watcher.sh): Hermes
resolves a bare ``--script NAME`` ONLY against the bound home's ``scripts/`` dir
($HERMES_HOME/scripts; profiles never share files). The installer used to
hard-code the SHARED ~/.hermes/scripts while ``hermes cron create`` bound the
INVOKING profile's home — running it from a profile shell would produce an
hscc-escalate-watcher job whose script can never be found. These tests pin the
invariant: the copy destination and the cron binding must resolve from the SAME
home, namely $HERMES_HOME (default ~/.hermes). The hermes CLI is stubbed; the
real cron store is never touched.
"""

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALLER = os.path.join(REPO, "scripts", "install_escalate_watcher.sh")
RUNNER = os.path.join(REPO, "scripts", "escalate_watcher_run.py")


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
    installed = profile_home / "scripts" / "escalate_watcher_run.py"
    assert installed.is_file(), "runner must land inside the bound home's scripts/"
    # The stub (== the same home `hermes cron create` binds) received the job.
    assert "cron create" in log and "--script escalate_watcher_run.py" in log
    assert "--name hscc-escalate-watcher" in log
    # remove-before-create keeps idempotency.
    assert log.index("cron remove hscc-escalate-watcher") < log.index("cron create")
    # And it must NOT have leaked into the (now distinct) shared location.
    shared = tmp_path / "home" / ".hermes" / "scripts" / "escalate_watcher_run.py"
    assert not shared.exists()


def test_default_home_unchanged_when_hermes_home_unset(tmp_path):
    """Without HERMES_HOME the installer keeps using ~/.hermes (shared home)."""
    proc, _log = _run(tmp_path, hermes_home=None)
    assert proc.returncode == 0, proc.stderr
    installed = tmp_path / "home" / ".hermes" / "scripts" / "escalate_watcher_run.py"
    assert installed.is_file()


def test_uninstall_removes_from_the_same_home(tmp_path):
    """--uninstall deletes exactly the copy a matching install created."""
    profile_home = tmp_path / "profile"
    proc, log = _run(tmp_path, hermes_home=profile_home)
    assert proc.returncode == 0, proc.stderr
    installed = profile_home / "scripts" / "escalate_watcher_run.py"
    assert installed.is_file()
    proc, log = _run(tmp_path, args=("--uninstall",), hermes_home=profile_home,
                     log_name="cron_stub_uninstall.log")
    assert proc.returncode == 0, proc.stderr
    assert not installed.exists()
    assert "cron remove hscc-escalate-watcher" in log


def test_installed_copy_matches_repo_source(tmp_path):
    """The drift rule covers content too: installed == repo byte-for-byte."""
    profile_home = tmp_path / "profile"
    proc, _log = _run(tmp_path, hermes_home=profile_home)
    assert proc.returncode == 0, proc.stderr
    installed = (profile_home / "scripts" / "escalate_watcher_run.py").read_bytes()
    assert installed == open(RUNNER, "rb").read()


def test_static_drift_pins():
    """Guard against re-hard-coding the shared path (the original bug shape)."""
    src = open(INSTALLER, encoding="utf-8").read()
    assert 'HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"' in src
    assert 'HERMES_SCRIPTS="$HERMES_HOME/scripts"' in src
    assert 'HERMES_SCRIPTS="$HOME/.hermes/scripts"' not in src


# ── --status: the installed-copy drift check (t_95eb9f15) ───────────────────
#
# Same shape as install_dep_watcher.sh: the runner is `cp`-ied into the bound
# home, so a deployed copy can predate the repo while the cron job keeps running
# it — and its liveness verdict then says nothing useful. --status compares the
# two copies and says so.

def test_status_compares_the_deployed_copy_with_the_repo_copy(tmp_path):
    profile_home = tmp_path / "profile"
    proc, _log = _run(tmp_path, hermes_home=profile_home)
    assert proc.returncode == 0, proc.stderr
    installed = profile_home / "scripts" / "escalate_watcher_run.py"

    proc, _log = _run(tmp_path, args=("--status",), hermes_home=profile_home,
                      log_name="cron_stub_status.log")
    assert proc.returncode == 0, proc.stderr
    assert "copy:     in-sync" in proc.stdout, proc.stdout
    assert "liveness:" in proc.stdout
    assert str(installed) in proc.stdout

    installed.write_text(installed.read_text() + "\n# the repo moved on\n")
    proc, _log = _run(tmp_path, args=("--status",), hermes_home=profile_home,
                      log_name="cron_stub_status2.log")
    assert proc.returncode == 0, proc.stderr
    assert "copy:     drifted" in proc.stdout, proc.stdout
    assert "NOT the repo copy" in proc.stdout
    assert "re-run scripts/install_escalate_watcher.sh" in proc.stdout


def test_status_reports_without_installing_anything(tmp_path):
    """--status is a read: rc 0 on an uninstalled host, and no cron mutation."""
    profile_home = tmp_path / "profile"
    proc, log = _run(tmp_path, args=("--status",), hermes_home=profile_home)
    assert proc.returncode == 0, proc.stderr
    assert "NOT REGISTERED" in proc.stdout
    assert "copy:" in proc.stdout and "liveness:" in proc.stdout
    assert "cron create" not in log and "cron remove" not in log
    assert not (profile_home / "scripts" / "escalate_watcher_run.py").exists()


def test_unknown_argument_is_refused_rather_than_installing(tmp_path):
    profile_home = tmp_path / "profile"
    proc, log = _run(tmp_path, args=("--statys",), hermes_home=profile_home)
    assert proc.returncode == 2
    assert "unknown argument" in proc.stderr and "--status" in proc.stderr
    assert "cron create" not in log
