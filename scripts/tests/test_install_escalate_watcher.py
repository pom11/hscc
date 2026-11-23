"""Tests for scripts/install_escalate_watcher.sh — the generated-wrapper install.

Two eras of one defect class, same as install_dep_watcher.sh, one watcher over.

t_3aadd2c0 / t_e286c4b9 (the binding): Hermes resolves a bare ``--script NAME``
ONLY against the bound home's ``scripts/`` dir ($HERMES_HOME/scripts; profiles
never share files). Hard-coding the SHARED ~/.hermes/scripts while
``hermes cron create`` bound the INVOKING profile's home — running it from a
profile shell would produce an hscc-escalate-watcher job whose script can never
be found. The stub destination and the cron binding must resolve from the SAME
home, namely $HERMES_HOME (default ~/.hermes).

t_95eb9f15 → t_accbc5da (the content): this installer used to `cp` the runner into
that dir, and a deployed copy is a second copy of the code that nothing
re-checked — the same installed-copy drift the dep watcher was caught with. The
class is now removed rather than detected: the installer generates a stub that
runs the repo's file, so there is no second copy to go stale. The hermes CLI is
stubbed; the real cron store is never touched.
"""

import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALLER = os.path.join(REPO, "scripts", "install_escalate_watcher.sh")
RUNNER = os.path.join(REPO, "scripts", "escalate_watcher_run.py")
# The stub is named after the CRON JOB, not the repo script (t_accbc5da): an
# operator who cats the deployed file must see it is generated, and the
# copy-shape leaf (`escalate_watcher_run.py`) must end up EMPTY under scripts/.
WRAPPER = "hscc-escalate-watcher.py"
RETIRED = "escalate_watcher_run.py"


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


# ── the binding (t_3aadd2c0) ────────────────────────────────────────────────

def test_installer_binds_scripts_dir_to_hermes_home(tmp_path):
    """Stub destination == $HERMES_HOME/scripts, not the hard-coded shared dir."""
    profile_home = tmp_path / "profile"
    proc, log = _run(tmp_path, hermes_home=profile_home)
    assert proc.returncode == 0, proc.stderr
    stub = profile_home / "scripts" / WRAPPER
    assert stub.is_file(), "the generated stub must land inside the bound home's scripts/"
    # The stub (== the same home `hermes cron create` binds) received the job.
    assert "cron create" in log and f"--script {WRAPPER}" in log
    assert "--name hscc-escalate-watcher" in log
    # remove-before-create keeps idempotency.
    assert log.index("cron remove hscc-escalate-watcher") < log.index("cron create")
    # And it must NOT have leaked into the (now distinct) shared location.
    shared = tmp_path / "home" / ".hermes" / "scripts" / WRAPPER
    assert not shared.exists()


def test_default_home_unchanged_when_hermes_home_unset(tmp_path):
    """Without HERMES_HOME the installer keeps using ~/.hermes (shared home)."""
    proc, _log = _run(tmp_path, hermes_home=None)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "home" / ".hermes" / "scripts" / WRAPPER).is_file()


def test_uninstall_removes_from_the_same_home(tmp_path):
    """--uninstall deletes exactly what a matching install created, in that home,
    plus the file the retired copy shape left there."""
    profile_home = tmp_path / "profile"
    proc, log = _run(tmp_path, hermes_home=profile_home)
    assert proc.returncode == 0, proc.stderr
    stub = profile_home / "scripts" / WRAPPER
    assert stub.is_file()
    leftover = profile_home / "scripts" / RETIRED
    leftover.write_text("a copy an old installer left\n")
    proc, log = _run(tmp_path, args=("--uninstall",), hermes_home=profile_home,
                     log_name="cron_stub_uninstall.log")
    assert proc.returncode == 0, proc.stderr
    assert not stub.exists()
    assert not leftover.exists(), "the retired copy is a decoy; uninstall clears it too"
    assert "cron remove hscc-escalate-watcher" in log


def test_static_drift_pins():
    """Guard against re-hard-coding the shared path (the original bug shape) and
    against the `cp` coming back (the shape t_accbc5da removed)."""
    src = open(INSTALLER, encoding="utf-8").read()
    assert 'HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"' in src
    assert 'HERMES_SCRIPTS="$HERMES_HOME/scripts"' in src
    assert 'HERMES_SCRIPTS="$HOME/.hermes/scripts"' not in src
    assert 'cp "$SCRIPT_DIR' not in src, "this installer must not copy the runner again"


# ── the content (t_accbc5da): a generated stub, not a second copy ───────────

def test_the_deployed_file_is_a_generated_stub_running_the_repo_file(tmp_path):
    """No second copy of the runner may exist on the host. The deployed file is
    generated, names the repo's runner, and holds no runner code of its own."""
    profile_home = tmp_path / "profile"
    proc, _log = _run(tmp_path, hermes_home=profile_home)
    assert proc.returncode == 0, proc.stderr
    stub = (profile_home / "scripts" / WRAPPER).read_text()
    assert stub.startswith("#!/usr/bin/env python3")
    assert "# GENERATED by" in stub
    assert f"# HSCC-TARGET: {RUNNER}" in stub
    assert f'TARGET = "{RUNNER}"' in stub
    assert open(RUNNER, encoding="utf-8").read() not in stub
    assert "def " not in stub, "a stub with a def in it is a copy again"
    # .py, not .sh: Hermes picks the interpreter by extension and the runner
    # needs Hermes' own venv (hermes_cli, hscc_daemon). See the installer header.
    assert WRAPPER.endswith(".py")


def test_running_the_stub_runs_the_repo_file(tmp_path):
    """The handoff works: executing the generated stub executes the CHECKOUT's
    escalate_watcher_run.py — argparse's own usage line is the evidence, and it
    is the checkout's file whose ``_stamp_module()`` then resolves the checkout's
    hscc_daemon/cron_watchers.py first (t_77db1513, pinned in
    test_escalate_watcher_run.py). That chain is why the job stamps through git's
    module instead of the deployed plugin's older one.

    ``--help`` is the probe: argparse exits before main(), so nothing is
    escalated, reassigned or stamped by the probe itself."""
    profile_home = tmp_path / "profile"
    proc, _log = _run(tmp_path, hermes_home=profile_home)
    assert proc.returncode == 0, proc.stderr
    stub = profile_home / "scripts" / WRAPPER
    run = subprocess.run(
        [sys.executable, str(stub), "--help"], capture_output=True, text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr
    assert "usage: escalate_watcher_run.py" in run.stdout, run.stdout
    assert "strong tier" in run.stdout, run.stdout


def test_installer_removes_the_copy_the_old_shape_left(tmp_path):
    """The transition, closed on the host rather than just in git. A leftover
    ``escalate_watcher_run.py`` under scripts/ is the file a pre-wrapper cron
    binding runs; re-installing must delete it (and say so), or a stale second
    copy stays one re-registration away from running again."""
    profile_home = tmp_path / "profile"
    (profile_home / "scripts").mkdir(parents=True)
    leftover = profile_home / "scripts" / RETIRED
    leftover.write_text("# an older runner, stamping nothing\n")

    proc, _log = _run(tmp_path, hermes_home=profile_home)
    assert proc.returncode == 0, proc.stderr
    assert not leftover.exists(), "the retired copy must not survive a re-install"
    assert "removed the deployed copy" in proc.stdout, proc.stdout
    assert (profile_home / "scripts" / WRAPPER).is_file()


def test_static_wrapper_and_roster_agree():
    """cron_watchers.py prints the installer as the fix and the installer names
    the file it generates — pin that the roster's copy-shape path (the one
    deployed_copy_drift looks at for a leftover) is still this installer's
    retired leaf, and that the stub it now generates is named for the job."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cw_roster", os.path.join(REPO, "hscc_daemon", "cron_watchers.py"))
    assert spec is not None and spec.loader is not None
    cw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cw)
    row = cw.WATCHERS["escalate_watcher"]
    assert row["deploy"] == cw.DEPLOY_WRAPPER
    assert row["script"] == RETIRED
    assert WRAPPER == row["cron_job"] + ".py"


# ── --status: the wrapper's own failure mode + the retired copy (t_95eb9f15) ─
#
# Same rule as install_dep_watcher.sh: `copy:` stays because a host that has not
# re-run this installer yet is still running the `cp`-ed file, and that line is
# what tells the operator to re-run it. Once they have, it settles on
# `not-a-copy` — nothing left to compare, which is the point of the card.

def test_status_says_not_a_copy_for_a_wrapper_install(tmp_path):
    """A correctly-installed wrapper has no deployed copy: `not-a-copy`, plus the
    target line naming the repo file it runs."""
    profile_home = tmp_path / "profile"
    proc, _log = _run(tmp_path, hermes_home=profile_home)
    assert proc.returncode == 0, proc.stderr

    proc, _log = _run(tmp_path, args=("--status",), hermes_home=profile_home,
                      log_name="cron_stub_status.log")
    assert proc.returncode == 0, proc.stderr
    assert "copy:     not-a-copy" in proc.stdout, proc.stdout
    assert "liveness:" in proc.stdout
    assert f"runs {RUNNER}" in proc.stdout, proc.stdout


def test_status_warns_about_a_copy_the_old_installer_left(tmp_path):
    """The t_95eb9f15 section, carried over: the stale leftover is named, the
    disagreement is spelled out, and the one installer that fixes it is."""
    profile_home = tmp_path / "profile"
    proc, _log = _run(tmp_path, hermes_home=profile_home)
    assert proc.returncode == 0, proc.stderr
    leftover = profile_home / "scripts" / RETIRED
    leftover.write_text("# an older runner, stamping nothing\n")

    proc, _log = _run(tmp_path, args=("--status",), hermes_home=profile_home,
                      log_name="cron_stub_status2.log")
    assert proc.returncode == 0, proc.stderr
    assert "copy:     drifted" in proc.stdout, proc.stdout
    assert "NOT the repo copy" in proc.stdout
    assert "re-run scripts/install_escalate_watcher.sh" in proc.stdout
    assert "leftover:" in proc.stdout, proc.stdout

    proc, _log = _run(tmp_path, hermes_home=profile_home,
                      log_name="cron_stub_reinstall.log")
    assert proc.returncode == 0, proc.stderr
    proc, _log = _run(tmp_path, args=("--status",), hermes_home=profile_home,
                      log_name="cron_stub_status3.log")
    assert "copy:     not-a-copy" in proc.stdout, proc.stdout
    assert "leftover:" not in proc.stdout


def test_status_warns_when_the_baked_target_no_longer_exists(tmp_path):
    """The wrapper shape's own failure mode (t_accbc5da): the repo path is baked
    at install time, so moving or deleting the checkout leaves the stub dangling
    and the job fails on its next 15-minute tick. --status has to say THAT, and
    say the fix, rather than reporting a healthy-looking wrapper."""
    profile_home = tmp_path / "profile"
    proc, _log = _run(tmp_path, hermes_home=profile_home)
    assert proc.returncode == 0, proc.stderr
    stub = profile_home / "scripts" / WRAPPER
    text = stub.read_text()
    moved = str(tmp_path / "deleted-checkout" / "scripts" / RETIRED)
    stub.write_text(text.replace(f"# HSCC-TARGET: {RUNNER}",
                                 f"# HSCC-TARGET: {moved}")
                    .replace(f'TARGET = "{RUNNER}"', f'TARGET = "{moved}"'))

    proc, _log = _run(tmp_path, args=("--status",), hermes_home=profile_home,
                      log_name="cron_stub_dangling.log")
    assert proc.returncode == 0, proc.stderr
    assert "DANGLING" in proc.stdout, proc.stdout
    assert moved in proc.stdout
    assert "Re-run this installer" in proc.stdout, proc.stdout

    # And the stub itself is loud about it rather than exiting 0 on nothing:
    # run history has to carry the reason, not just the failure.
    run = subprocess.run([sys.executable, str(stub)], capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == 2, run.stdout
    assert "checkout this stub was installed from is gone" in run.stderr, run.stderr
    assert "install_escalate_watcher.sh" in run.stderr, run.stderr


def test_status_reports_without_installing_anything(tmp_path):
    """--status is a read: rc 0 on an uninstalled host, and no cron mutation."""
    profile_home = tmp_path / "profile"
    proc, log = _run(tmp_path, args=("--status",), hermes_home=profile_home)
    assert proc.returncode == 0, proc.stderr
    assert "NOT REGISTERED" in proc.stdout
    assert "wrapper:" in proc.stdout and "MISSING" in proc.stdout
    assert "copy:" in proc.stdout and "liveness:" in proc.stdout
    assert "cron create" not in log and "cron remove" not in log
    assert not (profile_home / "scripts" / WRAPPER).exists()


def test_status_names_a_stub_this_installer_did_not_write(tmp_path):
    """A file at the wrapper path that carries no GENERATED marker is not
    evidence of a working install — saying `NOT GENERATED` beats claiming the
    install is healthy, and beats the `MISSING` line that would be a lie."""
    profile_home = tmp_path / "profile"
    scripts = profile_home / "scripts"
    scripts.mkdir(parents=True)
    (scripts / WRAPPER).write_text("#!/usr/bin/env python3\nprint('hand written')\n")
    proc, _log = _run(tmp_path, args=("--status",), hermes_home=profile_home)
    assert proc.returncode == 0, proc.stderr
    assert "NOT GENERATED" in proc.stdout, proc.stdout


def test_unknown_argument_is_refused_rather_than_installing(tmp_path):
    profile_home = tmp_path / "profile"
    proc, log = _run(tmp_path, args=("--statys",), hermes_home=profile_home)
    assert proc.returncode == 2
    assert "unknown argument" in proc.stderr and "--status" in proc.stderr
    assert "cron create" not in log


def test_generated_stub_is_valid_python_and_bakes_no_relative_path(tmp_path):
    """A stub with a syntax error fails at the next tick with a traceback nobody
    reads, and a relative baked path resolves against cron's cwd, not the repo's."""
    profile_home = tmp_path / "profile"
    proc, _log = _run(tmp_path, hermes_home=profile_home)
    assert proc.returncode == 0, proc.stderr
    stub = (profile_home / "scripts" / WRAPPER).read_text()
    compile(stub, str(profile_home / "scripts" / WRAPPER), "exec")
    baked = re.findall(r'^TARGET = "(.*)"$', stub, re.M)
    assert len(baked) == 1 and os.path.isabs(baked[0]), baked
    # The HSCC-TARGET marker --status reads must agree with what runs.
    assert f"# HSCC-TARGET: {baked[0]}" in stub
