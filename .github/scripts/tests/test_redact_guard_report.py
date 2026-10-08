"""Tests for the CI backstop: the address-guard workflow + its log redactor.

Three things have to hold, and each is checked by execution rather than by
reading the YAML:

  1. A committed real-shaped address makes the guard exit non-zero, and the
     offending ``file:line`` survives into what the job would print.
  2. The documented placeholders and the sanctioned fixture block do NOT.
  3. The real address never appears in the job output — this repo is PUBLIC and
     a job log is public, so the backstop must not itself publish the leak.

Fixtures are assembled at runtime (``_addr``) because the repo's own guard scans
every tracked file, including this one.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
CI_SCRIPTS = HERE.parent
REPO = CI_SCRIPTS.parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "address-guard.yml"
GUARD = REPO / "scripts" / "address_guard.py"
REDACTOR = CI_SCRIPTS / "redact_guard_report.py"

_spec = importlib.util.spec_from_file_location("redact_guard_report_under_test", REDACTOR)
assert _spec is not None and _spec.loader is not None
redactor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(redactor)


def _addr(*parts):
    """Assemble a forbidden-shaped address at runtime (see module docstring)."""
    return "".join(parts)


REAL_LAN = _addr("192", ".168.88.244")
REAL_TAILNET = _addr("100.64", ".55.7")
LAN_PLACEHOLDER = "10.0.0.244"
TAILNET_PLACEHOLDER = "100.64.0.1"


# ── the redactor (unit) ──────────────────────────────────────────────────────

@pytest.mark.parametrize("line,expect_visible", [
    # offender lines: position kept, value gone
    (f"  docs/hosts.md:7: {REAL_LAN}", "docs/hosts.md:7"),
    (f"  docs/hosts.md:7: {REAL_TAILNET}", "docs/hosts.md:7"),
    # the report footer must pass through untouched (it names placeholders)
    (f"  LAN node     -> {LAN_PLACEHOLDER}", LAN_PLACEHOLDER),
    (f"  tailnet host -> {TAILNET_PLACEHOLDER}", TAILNET_PLACEHOLDER),
    ("", ""),
])
def test_redact_keeps_position_drops_value(line, expect_visible):
    out = redactor.redact(line, redactor.load_pattern())
    assert expect_visible in out
    assert REAL_LAN not in out
    assert REAL_TAILNET not in out


def test_redact_is_idempotent_on_scrubbed_text():
    """Scrubbing twice must not eat the placeholders (no double-scrub damage)."""
    pattern = redactor.load_pattern()
    line = f"  hosts.md:3: {REAL_LAN}"
    once = redactor.redact(line, pattern)
    assert redactor.redact(once, pattern) == once


def test_redact_handles_colon_bearing_path():
    """A path with colons is the case shape-matching gets wrong (t_ec2c2f95 probes)."""
    pattern = redactor.load_pattern()
    line = f"  docs/we:ird.md:3: {REAL_LAN}"
    out = redactor.redact(line, pattern)
    assert REAL_LAN not in out
    assert "docs/we:ird.md:3" in out


def test_pattern_comes_from_the_shipped_guard():
    """One regex in the repo: the redactor must not carry its own copy."""
    src = REDACTOR.read_text(encoding="utf-8")
    assert "192" not in src and "100.64" not in src, "redactor hardcodes an address pattern"
    assert "address_guard.py" in src, "redactor must import the shared module"
    assert redactor.load_pattern() is not None


# ── the workflow shape (structural) ─────────────────────────────────────────

def test_workflow_exists_and_is_valid_yaml():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # YAML 1.1 parses the bare `on:` key as boolean True.
    triggers = doc.get("on", doc.get(True))
    assert "push" in triggers and "pull_request" in triggers
    assert doc["permissions"] == {"contents": "read"}, "backstop needs no write access"
    job = doc["jobs"]["guard"]
    assert job["runs-on"] == "ubuntu-latest"


def test_workflow_runs_the_shared_guard_not_a_second_detector():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = doc["jobs"]["guard"]["steps"]
    run = " ".join(s.get("run", "") for s in steps)
    assert "scripts/address_guard.py --tracked" in run
    assert ".github/scripts/redact_guard_report.py" in run
    # the guard reports on stderr; both streams must be captured
    assert "2>" in run
    # no address pattern may be invented here either
    src = WORKFLOW.read_text(encoding="utf-8")
    assert _addr("192.", ".168") not in src


def test_workflow_is_advisory_by_default_and_opt_in_blocking():
    """The operator's advisory-vs-blocking choice is a variable, not a code edit.

    Advisory must still fail when the guard cannot run (rc=2) — a silent pass is
    the failure mode this whole card exists to close.
    """
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = doc["jobs"]["guard"]["steps"][-1]
    assert "continue-on-error" not in step, \
        "continue-on-error would swallow rc=2; the step decides its own exit code"
    run = step["run"]
    assert "ADDRESS_GUARD_ENFORCE" in run or "ENFORCE" in step.get("env", {})
    assert "exit 0" in run, "advisory mode must be able to exit green"
    assert "::warning::" in run, "advisory leak should be a warning annotation"
    assert "::error::" in run, "blocking leak should be an error annotation"
    # rc=2 must fail before the advisory branch is consulted
    assert run.index("exit 2") < run.index("::warning::"), \
        "a guard that cannot run must not be demoted to advisory"


def test_guard_cannot_run_is_reported_before_the_redactor():
    """A checkout missing scripts/address_guard.py also breaks the redactor.

    Measured on real CI (probe/failclosed-9a4b7687): with the redactor branch
    first, that checkout reported "redactor could not load the pattern" — true
    but misleading, since the missing file is the guard's. rc=2 must be checked
    first so the annotation names the component that is actually absent.
    """
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    run = doc["jobs"]["guard"]["steps"][-1]["run"]
    assert run.index('"$rc" -eq 2') < run.index('"$redact_rc" -ne 0'), \
        "rc=2 must be diagnosed before the redactor's own failure"


def test_workflow_needs_no_network_beyond_checkout():
    """Acceptance: nothing in the job may reach the network."""
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = doc["jobs"]["guard"]["steps"]
    used = [s["uses"] for s in steps if "uses" in s]
    assert used == ["actions/checkout@v4"], "only checkout may be an external action"
    run = " ".join(s.get("run", "") for s in steps)
    for net in ("curl", "wget", "pip install", "npm ", "apt-get", "gh api"):
        assert net not in run, f"job would need network: {net}"


# ── end to end: the real command the job runs ───────────────────────────────

def _run_job_leg(repo_dir):
    """Run exactly what the workflow step runs: guard --tracked, then redact.

    Returns (exit_code, printed_output). stderr is routed through the redactor
    as the workflow does, so `printed_output` is what a job log would show.
    """
    env = dict(os.environ)
    guard = subprocess.run(
        [sys.executable, str(GUARD), "--tracked", "--repo", str(repo_dir)],
        capture_output=True, cwd=repo_dir, env=env,
    )
    red = subprocess.run(
        [sys.executable, str(REDACTOR)],
        input=guard.stderr, capture_output=True, env=env,
    )
    assert red.returncode == 0, f"redactor failed closed: {red.stderr.decode()}"
    return guard.returncode, guard.stdout.decode() + red.stdout.decode()


@pytest.fixture()
def throwaway(tmp_path):
    """A git repo carrying the shipped guard, like the hook tests do."""
    repo = tmp_path / "r"
    (repo / "scripts").mkdir(parents=True)
    (repo / "docs").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    import shutil
    shutil.copy2(GUARD, repo / "scripts" / "address_guard.py")
    return repo


def test_committed_real_address_fails_and_shows_file_line(throwaway):
    (throwaway / "docs" / "ledger.md").write_text(
        f"# tick\n\nremount: `sudo mount_nfs -o resvport {REAL_LAN}:/models /Volumes/NAS`\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=throwaway, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "tick"], cwd=throwaway, check=True)

    rc, printed = _run_job_leg(throwaway)
    assert rc == 1, printed
    assert "docs/ledger.md:3" in printed, f"file:line must be visible in the log:\n{printed}"
    assert REAL_LAN not in printed, "a PUBLIC job log must not carry the address"


def test_committed_real_tailnet_address_fails(throwaway):
    (throwaway / "docs" / "api.md").write_text(
        f"api at {REAL_TAILNET}:8787\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=throwaway, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "tick"], cwd=throwaway, check=True)

    rc, printed = _run_job_leg(throwaway)
    assert rc == 1
    assert "docs/api.md:1" in printed
    assert REAL_TAILNET not in printed


def test_placeholders_and_fixture_block_do_not_fail(throwaway):
    (throwaway / "docs" / "clean.md").write_text(
        f"LAN node `{LAN_PLACEHOLDER}`; tailnet `{TAILNET_PLACEHOLDER}`; "
        "fixtures 100.64.0.1 100.64.0.3 100.64.0.254 in the sanctioned /24\n"
        f"subnet {LAN_PLACEHOLDER}/24 export\n172.16.4.5 and 8.8.8.8 are out of scope\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=throwaway, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "clean"], cwd=throwaway, check=True)

    rc, printed = _run_job_leg(throwaway)
    assert rc == 0, f"placeholders and the fixture block must not fail the job:\n{printed}"


def test_the_real_repo_passes_the_job_leg():
    """The acceptance case that matters: today's main must be green."""
    rc, printed = _run_job_leg(REPO)
    assert rc == 0, printed


# ── the job's decision logic, executed verbatim ──────────────────────────────
#
# The leak branches (rc=1) cannot be exercised on real CI by pushing a leak,
# because pushing a real-shaped address is the very leak this repo forbids. So
# we take the step's shell script OUT of the YAML and run it verbatim against a
# stub guard, in every decision state. The redactor is the real one, and the
# script is the one shipping — this is the workflow's logic under test, not a
# copy of it.

def _step_script():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return doc["jobs"]["guard"]["steps"][-1]["run"]


def _run_step(tmp_path, guard_rc, report, enforce):
    """Run the workflow's real step script with a stubbed guard verdict.

    The stub prints REPORT (read from a file — the step calls the guard with
    ``--tracked`` and no report argument) on stderr and exits GUARD_RC, standing
    in for the real guard's verdict without needing a real address on disk.
    """
    work = tmp_path / "job"
    (work / "scripts").mkdir(parents=True)
    (work / ".github" / "scripts").mkdir(parents=True)
    (tmp_path / "report.txt").write_text(report, encoding="utf-8")
    # The real redactor resolves the guard relative to itself, so this stub is
    # the one it loads — one checkout, one pattern, as in CI.
    shutil.copy2(REDACTOR, work / ".github" / "scripts" / "redact_guard_report.py")
    (work / "scripts" / "address_guard.py").write_text(
        # Dual-mode on purpose: the redactor IMPORTS this module for its pattern,
        # while the step runs it as a CLI. Import the real guard for FORBIDDEN so
        # the redactor still sees the repo's one and only regex; emit the canned
        # verdict only when invoked as a program.
        "import importlib.util, pathlib, sys\n"
        f"_s = importlib.util.spec_from_file_location('_real_guard', r'{GUARD}')\n"
        "_m = importlib.util.module_from_spec(_s); _s.loader.exec_module(_m)\n"
        "FORBIDDEN = _m.FORBIDDEN\n"
        "if __name__ == '__main__':\n"
        "    sys.stderr.write(pathlib.Path(sys.argv[2]).read_text())\n"
        f"    sys.exit({guard_rc})\n",
        encoding="utf-8",
    )
    script = work / "step.sh"
    step = _step_script().replace(
        "scripts/address_guard.py --tracked",
        f"scripts/address_guard.py --tracked {tmp_path / 'report.txt'}",
    )
    script.write_text(step, encoding="utf-8")
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), ENFORCE=enforce)
    proc = subprocess.run(["bash", str(script)], capture_output=True, cwd=work, env=env)
    return proc.returncode, proc.stdout.decode() + proc.stderr.decode()


def test_step_advisory_leak_is_green_but_warns(tmp_path):
    """Option (a): a leak is surfaced, and nothing blocks."""
    rc, out = _run_step(tmp_path, 1, f"  docs/x.md:3: {REAL_LAN}\n", "")
    assert rc == 0, out
    assert "::warning::" in out, out
    assert "docs/x.md:3" in out
    assert REAL_LAN not in out


def test_step_enforced_leak_is_red(tmp_path):
    """Option (b)/(c): with ADDRESS_GUARD_ENFORCE=true the same leak fails."""
    rc, out = _run_step(tmp_path, 1, f"  docs/x.md:3: {REAL_LAN}\n", "true")
    assert rc == 1, out
    assert "::error::" in out
    assert "docs/x.md:3" in out
    assert REAL_LAN not in out


def test_step_fails_closed_when_guard_cannot_run(tmp_path):
    """rc=2 must be red even in advisory mode — never a silent pass."""
    rc, out = _run_step(tmp_path, 2, "address_guard: CANNOT RUN — no git\n", "")
    assert rc == 2, out
    assert "::error::" in out
    assert "advisory" not in out.lower() or "could not run" in out


def test_step_names_the_guard_not_the_redactor_when_the_guard_is_absent(tmp_path):
    """The real CI probe (probe/failclosed-9a4b7687) reported exit 3 here.

    A checkout without scripts/address_guard.py breaks the redactor too, so the
    redactor's message came first and blamed the wrong file. The rc=2 diagnosis
    must win.
    """
    work = tmp_path / "job"
    (work / "scripts").mkdir(parents=True)
    (work / ".github" / "scripts").mkdir(parents=True)
    shutil.copy2(REDACTOR, work / ".github" / "scripts" / "redact_guard_report.py")
    # No scripts/address_guard.py at all: the guard cannot run AND the redactor
    # cannot load its pattern, exactly the probe's state.
    script = work / "step.sh"
    script.write_text(_step_script(), encoding="utf-8")
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), ENFORCE="")
    proc = subprocess.run(["bash", str(script)], capture_output=True, cwd=work, env=env)
    out = proc.stdout.decode() + proc.stderr.decode()
    assert proc.returncode == 2, out
    assert "address_guard could not run" in out, out
    assert "redactor" not in out, out


def test_step_clean_tree_is_green(tmp_path):
    rc, out = _run_step(tmp_path, 0, "", "")
    assert rc == 0, out
    assert "::warning::" not in out and "::error::" not in out


def test_redactor_fails_closed_if_pattern_cannot_load(tmp_path):
    """No unredacted text may reach the log on our account.

    A copy placed where its own ``scripts/address_guard.py`` does not exist
    stands in for a broken checkout, driven through the real CLI path.
    """
    stranded = tmp_path / "a" / "b"
    stranded.mkdir(parents=True)
    copy = stranded / "redact_guard_report.py"
    shutil.copy2(REDACTOR, copy)

    proc = subprocess.run(
        [sys.executable, str(copy)],
        input=f"  docs/x.md:1: {REAL_LAN}\n".encode(),
        capture_output=True,
    )
    assert proc.returncode == 3, proc.stderr.decode()
    assert REAL_LAN not in proc.stdout.decode()
    assert REAL_LAN not in proc.stderr.decode()
    assert b"CANNOT LOAD PATTERN" in proc.stderr
