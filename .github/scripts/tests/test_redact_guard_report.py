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

import ast
import base64
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
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
    assert run.index('"$rc" -eq 2') < run.index('"$redact_out_rc" -ne 0'), \
        "rc=2 must be diagnosed before the redactor's own failure"
    # both captured streams are gated, so neither can slip past the diagnosis
    assert '"$redact_err_rc" -ne 0' in run


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


def _run_step(tmp_path, guard_rc, report, enforce, guard_source=None):
    """Run the workflow's real step script with a stubbed guard verdict.

    The stub prints REPORT (read from a file — the step calls the guard with
    ``--tracked`` and no report argument) on stderr and exits GUARD_RC, standing
    in for the real guard's verdict without needing a real address on disk.

    GUARD_SOURCE replaces the dual-mode stub entirely — for the broken-guard
    and warning-emitting inputs the fail-closed tests drive.
    """
    work = tmp_path / "job"
    (work / "scripts").mkdir(parents=True)
    (work / ".github" / "scripts").mkdir(parents=True)
    (tmp_path / "report.txt").write_text(report, encoding="utf-8")
    # The real redactor resolves the guard relative to itself, so this stub is
    # the one it loads — one checkout, one pattern, as in CI.
    shutil.copy2(REDACTOR, work / ".github" / "scripts" / "redact_guard_report.py")
    if guard_source is None:
        guard_source = (
            # Dual-mode on purpose: the redactor IMPORTS this module for its pattern,
            # while the step runs it as a CLI. Import the real guard for FORBIDDEN so
            # the redactor still sees the repo's one and only regex; emit the canned
            # verdict only when invoked as a program.
            _GUARD_BODY + "if __name__ == '__main__':\n"
            "    sys.stderr.write(pathlib.Path(sys.argv[2]).read_text())\n"
            f"    sys.exit({guard_rc})\n"
        )
    (work / "scripts" / "address_guard.py").write_text(guard_source, encoding="utf-8")
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


# ── the fail-closed path must not itself publish (review round 1) ───────────
#
# "Exit non-zero" is not the guarantee. A failure path also WRITES TEXT to the
# log, and both channels found in review wrote guard-derived text: the
# load-failure message interpolated ``str(exc)``, and importing the guard lets
# Python's warning renderer print the guard's SOURCE LINE. Both are pinned here
# with the address assembled at runtime, exactly as the repo's guard requires.

# Shared scaffold for stub guards used by the fail-closed tests.
#
# ROUND-4 CONTRACT CHANGE (t_38fd345f), stated here because it is why every
# stub below looks like this now: the parent no longer accepts a guard whose
# FORBIDDEN it cannot read off the source text. `FORBIDDEN = _m.FORBIDDEN` —
# the scaffold every round-1..3 stub used — is exactly the non-literal shape
# the round-4 fix refuses: D0 closed the form-only protocol by making the
# SOURCE the authority, and a re-bound import is not a source statement.
# Stubs now carry FORBIDDEN as a real re.compile(<literal>), assembled at
# runtime from the shipped guard's own pattern text: still ONE regex in the
# repo, and still nothing address-shaped committed to this file.
_gspec = importlib.util.spec_from_file_location("guard_pattern_source", GUARD)
assert _gspec is not None and _gspec.loader is not None
_gmod = importlib.util.module_from_spec(_gspec)
_gspec.loader.exec_module(_gmod)
GUARD_PATTERN_TEXT = _gmod.FORBIDDEN.pattern
GUARD_FLAGS = _gmod.FORBIDDEN.flags

# The import line stubs start from. Stubs needing more modules replace
# THIS string (they used to replace the importlib import line).
_GUARD_IMPORTS = "import pathlib, re, sys\n"
_GUARD_BODY = _GUARD_IMPORTS + ("FORBIDDEN = re.compile(%r)" + "\n") % (GUARD_PATTERN_TEXT,)
_GUARD_CLI = (
    "if __name__ == '__main__':\n"
    "    sys.stderr.write(pathlib.Path(sys.argv[2]).read_text())\n"
    "    sys.exit(1)\n"
)


def _stub_raising_with_address(addr):
    """A guard that blows up at import, quoting an address in the message."""
    return _GUARD_BODY + f"raise ValueError('boom at {addr}')\n"


def _stub_functional_with_address_in_source(addr):
    """A guard that WORKS — right pattern, right rc — but carries an address
    inside a non-raw literal containing an invalid escape.

    One missing ``r`` prefix. Python compiles this at import time on every CI
    run (checkouts have no __pycache__) and, on 3.12+, renders an
    invalid-escape SyntaxWarning by DEFAULT — and the renderer's job is to
    print the offending source line, which is where the address lives.
    """
    return "\n".join([
        _GUARD_IMPORTS.strip(),
        "FORBIDDEN = re.compile(%r)" % (GUARD_PATTERN_TEXT,),
        f'NOTE = "live tailnet peer seen as {addr} matched by \\d{{1,3}} octets"',
        "if __name__ == '__main__':",
        "    sys.stderr.write(pathlib.Path(sys.argv[2]).read_text())",
        "    sys.exit(1)",
    ]) + "\n"


def _stub_nonpattern_forbidden(addr):
    """FORBIDDEN bound to something that is NOT a compiled regex AT RUNTIME.

    The parent's source check only sees SHAPE (module-level re.compile(literal)),
    so the child's runtime type gate stays reachable: this source shadows the
    name `re` with a class whose compile() hands back a plain string. The input
    exists to fire the child's isinstance gate (ERR TypeError), the round-2
    diagnosis the assertion pins — reachable precisely because AST parsing
    cannot know that `re` is a class by the time FORBIDDEN is evaluated.
    """
    return (
        "import pathlib, sys\n"
        + "class re:\n"
        + "    @staticmethod\n"
        + "    def compile(pattern, flags=0):\n"
        + "        return pattern\n"
        + "FORBIDDEN = re.compile('abc')\n"
    )


def _stub_prints_at_import(addr):
    """A functional guard that also chatters on stdout/stderr at import time.

    Keeps the repo's real FORBIDDEN so the report is still redacted — the point
    is that the NOISE is discarded, not that detection stops working.
    """
    return "\n".join([
        _GUARD_IMPORTS.strip(),
        "FORBIDDEN = re.compile(%r)" % (GUARD_PATTERN_TEXT,),
        f"print('scanning {addr}', file=sys.stderr)",
        f"print('also on stdout {addr}')",
    ]) + "\n"


# ── review round 2: channels the round-1 fix left open ───────────────────────
#
# Round 1 sealed `except Exception`, the warning renderer, and the sys-level
# streams. Two channels in the SAME input class survived it and were reproduced
# on both interpreters against the gated tip:
#
#   * BaseException / SystemExit raised at guard import unwinds past main(), and
#     CPython prints the traceback — the frame text IS the guard's source line;
#   * a write straight to **fd 1/2** (`os.write(2, b'...')`, or C-level output)
#     ignores redirect_stdout/redirect_stderr, because those rebind the *objects*
#     while the descriptor still points at the step's inherited pipes.
#
# The second one is the shape round 1 escalated for: it publishes the value while
# the ADVISORY job reports GREEN.

def _stub_baseexception_at_import(addr):
    """Any BaseException at module level — `except Exception` does not catch it."""
    return _GUARD_BODY + f"raise BaseException('fatal at {addr}')\n"


def _stub_sysexit_at_import(addr):
    """A plausible defensive-guard edit: refuse to run, quoting a host.

    A local `python3 scripts/address_guard.py --tracked` shows only the message
    line, so the author never sees the traceback CI would print.
    """
    return _GUARD_BODY + f"sys.exit('cannot run with {addr}')\n"


def _stub_fdwrite_at_import(addr, fd=2):
    """A fully functional guard (right FORBIDDEN, right rc) that writes to a raw
    file descriptor at import time.
    """
    return _GUARD_BODY.replace(
        _GUARD_IMPORTS,
        "import os, pathlib, re, sys\n",
    ) + f"os.write({fd}, b'fd{fd} {addr}\\n')\n" + _GUARD_CLI


def _redactor_alone(stub_src, tmp_path, stdin_text=None, bare=False, plant=None):
    """Run the real redactor CLI against a checkout carrying STUB_SRC.

    bare=True runs it as plain ``python3`` with NO -W/-E flags, which pins the
    REDACTOR's own silencing rather than the interpreter flags the step happens
    to pass. The step uses the flagged form; both must be safe.
    """
    work = tmp_path / "alone"
    (work / "scripts").mkdir(parents=True)
    (work / ".github" / "scripts").mkdir(parents=True)
    shutil.copy2(REDACTOR, work / ".github" / "scripts" / "redact_guard_report.py")
    (work / "scripts" / "address_guard.py").write_text(stub_src, encoding="utf-8")
    if plant is not None:
        plant(work)
    argv = [sys.executable] + (["-W", "ignore", "-E"] if not bare else [])
    proc = subprocess.run(
        argv + [str(work / ".github" / "scripts" / "redact_guard_report.py")],
        input=(stdin_text or f"  docs/x.md:1: {REAL_LAN}\n").encode(),
        capture_output=True, cwd=work,
    )
    return proc.returncode, proc.stdout.decode() + proc.stderr.decode()


def test_load_failure_names_nothing_but_the_exception_type(tmp_path):
    """str(exc) is the first channel: an import error can quote the address."""
    addr = REAL_LAN
    rc, out = _redactor_alone(_stub_raising_with_address(addr), tmp_path)
    assert rc == 3, out
    assert addr not in out, f"the diagnostic published the address:\n{out}"
    assert "CANNOT LOAD PATTERN" in out, out
    assert "ValueError" in out, "the reader still needs to know what failed"
    assert "boom at" not in out, out


def test_guard_raising_at_import_does_not_publish_through_the_step(tmp_path):
    """Channel 1 driven verbatim through the shipping step script.

    A guard module that raises on import cannot run, so nothing usable can come
    of it: the step must fail closed (redactor refuses -> exit 3) and must not
    quote the exception message, which here carries the address.
    """
    rc, out = _run_step(
        tmp_path, 1, f"  docs/x.md:3: {REAL_LAN}\n", "",
        guard_source=_stub_raising_with_address(REAL_LAN),
    )
    assert rc == 3, f"a guard that cannot import must fail the job:\n{out}"
    assert REAL_LAN not in out, f"the step's output published the address:\n{out}"
    assert "boom at" not in out, out
    assert "CANNOT LOAD PATTERN [ValueError]" in out, out


def test_guard_with_address_in_source_never_reaches_the_log(tmp_path):
    """THE worst case (review probe7): a functional guard, an ADVISORY green
    job, and a real-shaped host inside a non-raw literal with an invalid escape.

    Before the fix this published the guard's own source line to the PUBLIC log
    on every run while the check reported success.
    """
    rc, out = _run_step(
        tmp_path, 1, f"  docs/x.md:3: {REAL_LAN}\n", "",
        guard_source=_stub_functional_with_address_in_source(REAL_TAILNET),
    )
    assert rc == 0, f"advisory posture with a functional guard must be green:\n{out}"
    assert REAL_TAILNET not in out, f"job log carries the guard's source line:\n{out}"
    assert REAL_LAN not in out, out
    # and the check must still be doing its job, not just quiet
    assert "docs/x.md:3" in out, out
    assert "***" in out, out


def test_nonpattern_forbidden_fails_closed_without_traceback(tmp_path):
    rc, out = _redactor_alone(_stub_nonpattern_forbidden(REAL_LAN), tmp_path)
    assert rc == 3, out
    assert "Traceback" not in out, f"raw traceback would reach the log:\n{out}"
    assert REAL_LAN not in out, out
    # Pinned to the gate itself (mutation-tested): without the isinstance check
    # the same input dies LATER inside the redaction loop and reports
    # "UNEXPECTED ERROR [AttributeError]" — same rc, wrong diagnosis.
    assert "CANNOT LOAD PATTERN [TypeError]" in out, out


def test_import_time_noise_is_discarded_not_forwarded(tmp_path):
    rc, out = _redactor_alone(_stub_prints_at_import(REAL_TAILNET), tmp_path)
    assert rc == 0, out
    assert REAL_TAILNET not in out, f"import-time print reached the log:\n{out}"
    assert "***" in out, "the report itself must still be redacted"


def test_warning_channel_closed_by_the_redactor_itself(tmp_path):
    """Same input, NO -W/-E on the interpreter.

    The step passes `-W ignore -E`, which alone would suppress the warning — so
    a test that only used the flagged form would pass even if load_pattern()'s
    own catch_warnings() were removed. This is the layer proof: plain python3,
    and the value must still never appear.
    """
    rc, out = _redactor_alone(
        _stub_functional_with_address_in_source(REAL_TAILNET), tmp_path, bare=True)
    assert rc == 0, out
    assert REAL_TAILNET not in out, f"guard source line reached the log:\n{out}"
    assert "WARNING" not in out.upper(), out
    assert "***" in out, out


def test_guard_stdout_also_goes_through_the_redactor(tmp_path):
    """The step used to `cat` guard.out raw; the guard writes values to stderr
    today, so pin the stdout channel shut rather than trusting that."""
    work = tmp_path / "job"
    (work / "scripts").mkdir(parents=True)
    (work / ".github" / "scripts").mkdir(parents=True)
    (tmp_path / "report.txt").write_text("", encoding="utf-8")
    shutil.copy2(REDACTOR, work / ".github" / "scripts" / "redact_guard_report.py")
    (work / "scripts" / "address_guard.py").write_text(
        _GUARD_BODY + "if __name__ == '__main__':\n"
        f"    print('value leaked on stdout: {REAL_LAN}')\n"
        "    sys.stderr.write(pathlib.Path(sys.argv[2]).read_text())\n"
        "    sys.exit(1)\n",
        encoding="utf-8",
    )
    script = work / "step.sh"
    script.write_text(
        _step_script().replace(
            "scripts/address_guard.py --tracked",
            f"scripts/address_guard.py --tracked {tmp_path / 'report.txt'}"),
        encoding="utf-8")
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), ENFORCE="")
    proc = subprocess.run(["bash", str(script)], capture_output=True, cwd=work, env=env)
    out = proc.stdout.decode() + proc.stderr.decode()
    assert proc.returncode == 0, out          # advisory leak
    assert REAL_LAN not in out, f"stdout channel leaked:\n{out}"
    assert "value leaked on stdout" in out, "position kept, value dropped"


@pytest.mark.parametrize("value,expect_red", [
    ("true", True), ("True", True), ("TRUE", True), ("1", True), ("yes", True),
    ("", False), ("false", False), ("False", False), ("0", False), ("no", False),
    ("maybe", False),
])
def test_enforce_variable_cannot_fail_open_on_capitalisation(tmp_path, value, expect_red):
    """A variable named ENFORCE may only loosen the job explicitly (review)."""
    rc, out = _run_step(tmp_path, 1, f"  docs/x.md:3: {REAL_LAN}\n", value)
    assert (rc == 1) is expect_red, f"ENFORCE={value!r} -> rc={rc}\n{out}"
    assert ("::error::" in out) is expect_red, out
    assert ("::warning::" in out) is (not expect_red), out
    assert REAL_LAN not in out


# ── round-2 regressions: BaseException out of main(), and fd-level writes ────

def test_guard_baseexception_at_import_does_not_publish_through_the_step(tmp_path):
    """`raise BaseException('fatal at <addr>')` at guard module level (review r2).

    `except Exception` did not catch it: it unwound past main() and CPython
    printed the traceback, whose frame text IS the guard source line. The step
    must still fail closed (rc=3) with the value nowhere in the output.
    """
    rc, out = _run_step(
        tmp_path, 1, f"  docs/x.md:3: {REAL_LAN}\n", "",
        guard_source=_stub_baseexception_at_import(REAL_LAN),
    )
    assert rc == 3, f"a guard that cannot import must fail the job:\n{out}"
    assert REAL_LAN not in out, f"the traceback published the address:\n{out}"
    assert "fatal at" not in out, out
    assert "Traceback" not in out, out
    assert "CANNOT LOAD PATTERN [BaseException]" in out, out


def test_guard_sysexit_with_address_does_not_publish_through_the_step(tmp_path):
    """`sys.exit('cannot run with <addr>')` at guard module level (review r2).

    SystemExit is a BaseException, and a bare `sys.exit(str)` prints the string
    to stderr on its way out — the plausible defensive-guard edit whose local
    repro shows the author only the message, never the CI traceback.
    """
    rc, out = _run_step(
        tmp_path, 1, f"  docs/x.md:3: {REAL_LAN}\n", "",
        guard_source=_stub_sysexit_at_import(REAL_LAN),
    )
    assert rc == 3, out
    assert REAL_LAN not in out, f"the exit message published the address:\n{out}"
    assert "cannot run with" not in out, out
    assert "CANNOT LOAD PATTERN [SystemExit]" in out, out


def test_fd_level_write_at_import_never_reaches_the_log(tmp_path):
    """fd 1/2 writes bypass every OBJECT-level silencer (review r2, the probe7
    shape): redirect_stdout/redirect_stderr rebind sys.stdout/std.stderr, but
    the descriptor still points at the step's inherited pipes.

    The stub is a FULLY FUNCTIONAL guard (right FORBIDDEN, right rc=1) and the
    posture is advisory, so the job must stay GREEN while the fd write is
    swallowed — and the report must still come out redacted.
    """
    rc, out = _run_step(
        tmp_path, 1, f"  docs/x.md:3: {REAL_LAN}\n", "",
        guard_source=_stub_fdwrite_at_import(REAL_TAILNET, fd=2),
    )
    assert rc == 0, f"advisory posture with a functional guard must be green:\n{out}"
    assert REAL_TAILNET not in out, f"fd-2 write reached the log:\n{out}"
    assert REAL_LAN not in out, out
    assert "docs/x.md:3" in out and "***" in out, out  # check still works


def test_fd_level_write_through_the_redactor_alone(tmp_path):
    """Same input with no step wrapper and NO -W/-E flags: the redactor's own
    load_pattern() must be the layer that holds, not the interpreter flags."""
    rc, out = _redactor_alone(
        _stub_fdwrite_at_import(REAL_TAILNET, fd=2), tmp_path, bare=True)
    assert rc == 0, out
    assert REAL_TAILNET not in out, f"fd-2 write reached the log:\n{out}"
    assert "***" in out, "the report itself must still be redacted"
    assert "docs/x.md:1" in out, "position kept"


def test_fd1_write_at_import_never_reaches_the_log(tmp_path):
    """fd 1 is as exposed as fd 2 — pin both descriptors, not just stderr."""
    rc, out = _redactor_alone(
        _stub_fdwrite_at_import(REAL_LAN, fd=1), tmp_path, bare=True)
    assert rc == 0, out
    assert REAL_LAN not in out, f"fd-1 write reached the log:\n{out}"
    assert "***" in out, out


def test_redactor_still_works_after_the_fd_juggling():
    """Normal operation under the fd-restore path (regression both ways).

    The dup2 swap must leave stdout/stderr USABLE for main(): the redacted
    report still arrives on stdout, position kept, value gone.
    """
    proc = subprocess.run(
        [sys.executable, str(REDACTOR)],
        input=f"  docs/ok.md:9: {REAL_LAN}\n".encode(), capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout.decode() == f"  docs/ok.md:9: ***\n"
    assert REAL_LAN not in proc.stdout.decode() + proc.stderr.decode()


# ── round-3 regressions: guard code that OUTLIVES an in-process window ────────
#
# Rounds 1 and 2 each added another in-process silencer (fixed-string diagnostics,
# then catch_warnings + stream redirects, then a dup/dup2 window over fd 1/2) and
# each was bypassed from inside the same process. Round 3 reproduced four such
# bypasses on both interpreters while the ADVISORY job stayed GREEN:
#
#   * ``atexit.register`` at import — the handler runs at interpreter shutdown,
#     i.e. AFTER ``finally`` handed fd 1/2 back to the step's pipes;
#   * a non-daemon ``threading.Thread`` started at import — joined at shutdown,
#     also after the window;
#   * an fd-table scan DURING the window — the window's own ``os.dup(1)`` /
#     ``os.dup(2)`` saved descriptors sit live in the fd table for the whole
#     import, so the writes land on the step's pipes;
#   * ``dup2`` back over fd 1 during the window, then ``print``.
#
# The fix is therefore structural, not another layer: the guard's module code runs
# in a CHILD whose stdin/stderr are /dev/null and whose stdout is a pipe only the
# redactor reads, and the redactor never echoes a byte from that pipe. These tests
# pin that boundary — each is the exact input that leaked at d4fa0db8.

def _stub_atexit_dump(addr, channel="stderr"):
    """A fully functional guard that dumps an address at interpreter shutdown.

    ``atexit`` handlers run after load_pattern()'s ``finally`` has restored the
    descriptors, so no window inside the redactor can cover them. The address is
    interpolated into the stub body, exactly as the round-2 fd-write stubs do.
    """
    action = {
        "stderr": "sys.stderr.write(" + repr("bye " + addr + "\n") + ")",
        "fd1": "os.write(1, " + repr(b"bye1 " + addr.encode() + b"\n") + ")",
        "fd2": "os.write(2, " + repr(b"bye2 " + addr.encode() + b"\n") + ")",
    }[channel]
    return _GUARD_BODY.replace(
        _GUARD_IMPORTS,
        "import atexit, os, pathlib, re, sys\n",
    ) + "atexit.register(lambda: " + action + ")\n" + _GUARD_CLI


def _stub_thread_dump(addr):
    """A functional guard that writes an address from a non-daemon thread."""
    return _GUARD_BODY.replace(
        _GUARD_IMPORTS,
        "import os, pathlib, re, sys, threading, time\n",
    ) + ("threading.Thread(target=lambda: (time.sleep(0.15), "
         "os.write(2, b'thr %s\\n'))).start()\n" % addr) + _GUARD_CLI


def _stub_fdscan(addr):
    """A functional guard that writes to every writable descriptor it can find.

    This is the one the fd WINDOW cannot survive: it does not need fd 1/2, it
    finds the redactor's own saved copies of them in the fd table.
    """
    return _GUARD_BODY.replace(
        _GUARD_IMPORTS,
        "import os, pathlib, re, sys\n",
    ) + ("for _fd in range(3, 64):\n"
        "    try:\n"
        "        os.write(_fd, b'scan %s\\n')\n"
        "    except OSError:\n"
        "        pass\n" % addr) + _GUARD_CLI


def _stub_dup2_back(addr):
    """A functional guard that re-points a saved descriptor over fd 1 and prints."""
    return _GUARD_BODY.replace(
        _GUARD_IMPORTS,
        "import os, pathlib, re, stat, sys\n",
    ) + ("for _fd in range(3, 64):\n"
        "    try:\n"
        "        _st = os.fstat(_fd)\n"
        "    except OSError:\n"
        "        continue\n"
        "    if stat.S_ISFIFO(_st.st_mode) or stat.S_ISSOCK(_st.st_mode):\n"
        "        os.dup2(_fd, 1)\n"
        "        sys.stdout = os.fdopen(1, 'w')\n"
        "        break\n"
        "print('dup2back %s')\n" % addr) + _GUARD_CLI


# Every input, both postures of the driver: the shipping step script (parsed
# verbatim from the YAML) and the redactor CLI alone with NO interpreter flags.
_ROUND3_STUBS = {
    "atexit_stderr": lambda: _stub_atexit_dump(REAL_TAILNET, "stderr"),
    "atexit_fd1": lambda: _stub_atexit_dump(REAL_LAN, "fd1"),
    "atexit_fd2": lambda: _stub_atexit_dump(REAL_TAILNET, "fd2"),
    "thread": lambda: _stub_thread_dump(REAL_TAILNET),
    "fdscan": lambda: _stub_fdscan(REAL_LAN),
    "dup2_back": lambda: _stub_dup2_back(REAL_TAILNET),
}
_ROUND3_IDS = list(_ROUND3_STUBS)


@pytest.mark.parametrize("channel", ["stderr", "fd1", "fd2"])
def test_atexit_dump_at_shutdown_never_reaches_the_log(tmp_path, channel):
    """Round-3 channel 1, through the STEP in advisory posture.

    The stub is a FULLY FUNCTIONAL guard (right FORBIDDEN, right rc=1), so the
    job must stay GREEN — and the report must still come out redacted with its
    position kept. Before the structural fix this published the address twice per
    step (once per redactor invocation) while reporting success.
    """
    addr = REAL_LAN if channel == "fd1" else REAL_TAILNET
    rc, out = _run_step(
        tmp_path, 1, f"  docs/x.md:3: {REAL_LAN}\n", "",
        guard_source=_stub_atexit_dump(addr, channel),
    )
    assert rc == 0, f"advisory posture with a functional guard must be green:\n{out}"
    assert addr not in out, f"{channel} atexit dump reached the log:\n{out}"
    assert REAL_LAN not in out and REAL_TAILNET not in out, out
    assert "docs/x.md:3" in out and "***" in out, out  # check still works


@pytest.mark.parametrize("case", _ROUND3_IDS)
def test_round3_guard_behaviours_publish_nothing_alone(case, tmp_path):
    """The same six inputs with no step wrapper and NO -W/-E flags.

    Layer proof, as in round 1: the interpreter flags the step passes must not be
    what holds. Here what holds is that the guard's code does not run in this
    process at all.
    """
    stub = _ROUND3_STUBS[case]()
    rc, out = _redactor_alone(stub, tmp_path, bare=True)
    assert rc == 0, f"a functional guard must still redact (case {case}):\n{out}"
    assert REAL_LAN not in out and REAL_TAILNET not in out, \
        f"{case} published an address through the CLI:\n{out}"
    assert "***" in out and "docs/x.md:1" in out, out


@pytest.mark.parametrize("case", _ROUND3_IDS)
def test_round3_guard_behaviours_publish_nothing_through_the_step(case, tmp_path):
    """All six through the shipping step script — the shape the reviewer measured."""
    rc, out = _run_step(
        tmp_path, 1, f"  docs/x.md:3: {REAL_LAN}\n", "",
        guard_source=_ROUND3_STUBS[case](),
    )
    assert rc == 0, f"advisory + functional guard must be green (case {case}):\n{out}"
    assert REAL_LAN not in out and REAL_TAILNET not in out, \
        f"{case} published an address through the step:\n{out}"
    assert "docs/x.md:3" in out and "***" in out, out


def test_guard_module_code_never_runs_in_the_redactors_own_process(tmp_path):
    """The trust boundary, pinned behaviourally rather than by reading the source.

    The stub records the pid it was imported into. If extraction ever went back to
    ``exec_module`` in this process, the recorded pid would be the redactor's own —
    and every deferred-write channel in this section would reopen. So this test
    fails on any regression to the in-process design, independent of the probes.
    """
    pidfile = tmp_path / "imported_by.pid"
    stub = (
        _GUARD_BODY.replace(_GUARD_IMPORTS,
                            "import os, pathlib, re, sys\n") +
        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "if __name__ == '__main__':\n"
        "    sys.stderr.write(pathlib.Path(sys.argv[2]).read_text())\n"
        "    sys.exit(1)\n"
    )
    work = tmp_path / "pidproof"
    (work / "scripts").mkdir(parents=True)
    (work / ".github" / "scripts").mkdir(parents=True)
    shutil.copy2(REDACTOR, work / ".github" / "scripts" / "redact_guard_report.py")
    (work / "scripts" / "address_guard.py").write_text(stub, encoding="utf-8")
    env = dict(os.environ, PYTHONHASHSEED="0")
    proc = subprocess.Popen(
        [sys.executable, str(work / ".github" / "scripts" / "redact_guard_report.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=work, env=env,
    )
    out, err = proc.communicate(f"  docs/x.md:1: {REAL_LAN}\n".encode())
    assert proc.returncode == 0, (out + err).decode()
    assert "***" in out.decode(), out.decode()
    assert pidfile.exists(), "the guard was never imported — the test is not testing"
    imported_by = int(pidfile.read_text())
    assert imported_by != proc.pid, (
        f"the guard's module code ran IN the redactor process ({proc.pid}); "
        "the child boundary that closes atexit/thread/fd-scan is gone"
    )


def test_the_parent_code_has_no_in_process_import_of_the_guard():
    """Structural counterpart to the pid proof: the parent must not exec the guard.

    ``importlib`` may appear only inside the child's source STRING — which is a
    string literal to the parser, not an import statement. A regression that
    reintroduces in-process ``exec_module`` fails here even if every probe happens
    to pass.
    """
    src = REDACTOR.read_text(encoding="utf-8")
    tree = ast.parse(src)
    parent_imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            parent_imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            parent_imports.add(node.module.split(".")[0])
    allowed = {"__future__", "ast", "base64", "hashlib", "os", "re", "selectors",
               "signal", "subprocess", "sys", "time", "warnings", "pathlib"}
    assert parent_imports <= allowed, f"unexpected in-process import: {parent_imports - allowed}"
    assert "importlib" not in parent_imports, \
        "the redactor imports importlib in-process again — guard code would run here"
    assert "subprocess" in parent_imports, "no child process = no boundary"
    # Nothing that executes guard-derived bytes may cross the boundary.
    assert "import pickle" not in src and "pickle.load" not in src, \
        "unpickling guard bytes would run guard code in this process"


def _stub_no_verdict(addr):
    """A guard the step can run fine, but that dies silently when IMPORTED.

    ``os._exit`` skips atexit and leaves the pipe empty: the redactor gets no
    verdict at all and must refuse rather than print the report unredacted.
    """
    return (
        _GUARD_BODY + "if '--tracked' not in sys.argv:\n"
        "    import os; os._exit(0)\n"
        "if __name__ == '__main__':\n"
        "    sys.stderr.write(pathlib.Path(sys.argv[2]).read_text())\n"
        "    sys.exit(1)\n"
    )


def test_child_that_emits_no_verdict_fails_closed(tmp_path):
    """No protocol line = no pattern = refuse. rc=3, and nothing unredacted out."""
    rc, out = _run_step(
        tmp_path, 1, f"  docs/x.md:3: {REAL_LAN}\n", "",
        guard_source=_stub_no_verdict(REAL_LAN),
    )
    assert rc == 3, f"a guard that yields no verdict must fail the job:\n{out}"
    assert REAL_LAN not in out, f"the report went out unredacted:\n{out}"
    assert "docs/x.md:3" not in out, "no pattern means no redaction, so no report"
    assert "CANNOT LOAD PATTERN" in out, out


def test_child_junk_terminated_with_newline_is_dropped_and_verdict_honoured(tmp_path):
    """Protocol contract (a), pinned explicitly by request of review round 3.

    Guard chatter that ends with a newline is its OWN line: it is not a verdict,
    it is dropped without being echoed, and the single well-formed OK verdict is
    still honoured — so a healthy guard that just chatters stays ADVISORY-GREEN
    with the report redacted.
    """
    stub = _GUARD_BODY.replace(
        _GUARD_IMPORTS,
        "import pathlib, re, sys\n",
    ) + "sys.stdout.write('chatter %s\\n')\n" % REAL_TAILNET + _GUARD_CLI
    rc, out = _redactor_alone(stub, tmp_path, bare=True)
    assert rc == 0, f"newline-terminated junk must not break a healthy guard:\n{out}"
    assert REAL_TAILNET not in out, f"child pipe bytes were echoed:\n{out}"
    assert "chatter" not in out, "the child's chatter must not be printed either"
    assert "***" in out and "docs/x.md:1" in out, out


def test_unterminated_junk_merged_into_the_verdict_line_fails_closed(tmp_path):
    """Protocol contract (b), also pinned by request: the merged-line edge.

    Junk with NO trailing newline merges into the child's verdict line
    (``fd1 <addr> OK\\t...``), so the line is no longer well-formed and there is no
    single OK verdict. The step fails CLOSED (rc=3) instead of going advisory-green.
    Safe in both directions for the same reason: not one byte read from the child is
    ever printed.
    """
    stub = _GUARD_BODY.replace(
        _GUARD_IMPORTS,
        "import os, pathlib, re, sys\n",
    ) + "os.write(1, b'fd1 %s')\n" % REAL_TAILNET + _GUARD_CLI
    rc, out = _redactor_alone(stub, tmp_path, bare=True)
    assert rc == 3, f"an unparseable verdict stream must fail closed, got rc={rc}:\n{out}"
    assert REAL_TAILNET not in out, f"the merged junk line was echoed:\n{out}"


def test_guard_that_burns_the_pipe_cannot_hang_the_step(monkeypatch, tmp_path):
    """The reviewer's non-blocking note: bound the child's cleanup too.

    A guard that spawns a GRANDCHILD inheriting stdout leaves the pipe's write end
    open, so ``read()`` never sees EOF. Without a bound that is a slow failure — the
    step hangs to the job timeout. Here the read is time-boxed, the pipe is closed,
    the whole process group is SIGKILLed and reaped with a second, shorter bound.
    """
    monkeypatch.setattr(redactor, "CHILD_TIMEOUT_S", 2)
    monkeypatch.setattr(redactor, "REAP_TIMEOUT_S", 1)
    # close_fds=False is the WHOLE test: it is what lets the grandchild inherit
    # the child's stdout (the verdict pipe) and keep its write end open after the
    # child exits. With the default close_fds=True the pipe sees EOF instantly and
    # the test would pass without exercising anything (my first cut did exactly
    # that — caught by mutation-checking my own tests, per 10c).
    stub = _GUARD_BODY.replace(
        _GUARD_IMPORTS,
        "import pathlib, re, subprocess, sys\n",
    ) + ("subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],"
         " close_fds=False)\n") + _GUARD_CLI
    work = tmp_path / "hang"
    (work / "scripts").mkdir(parents=True)
    (work / ".github" / "scripts").mkdir(parents=True)
    (work / ".github" / "scripts" / "redact_guard_report.py").write_text(
        REDACTOR.read_text(encoding="utf-8"), encoding="utf-8")
    stub_path = work / "scripts" / "address_guard.py"
    stub_path.write_text(stub, encoding="utf-8")
    # load_pattern resolves the guard via the module constant, not cwd — point it
    # at the stub or the test would run the REAL guard and test nothing.
    monkeypatch.setattr(redactor, "GUARD", stub_path)
    started = time.monotonic()
    with pytest.raises(redactor.PatternUnavailable) as excinfo:
        redactor.load_pattern()
    elapsed = time.monotonic() - started
    assert elapsed < 15, f"the child was not taken down within bounds ({elapsed:.1f}s)"
    assert excinfo.value.type_name == "TimeoutError", excinfo.value.type_name


def test_load_pattern_still_returns_the_real_compiled_pattern():
    """The boundary must not have quietly broken the thing the file is for.

    Equivalence, not existence: the pattern that comes back over the pipe must be
    the guard's own — same source text, same flags — so the redaction is still the
    repo's ONE regex rather than something that merely compiles.
    """
    spec = importlib.util.spec_from_file_location("guard_for_equivalence", GUARD)
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    pattern = redactor.load_pattern()
    assert isinstance(pattern, re.Pattern)
    assert pattern.pattern == guard.FORBIDDEN.pattern
    assert pattern.flags == guard.FORBIDDEN.flags
    # and it still catches what the guard catches, on both shapes
    assert pattern.search(REAL_LAN) and pattern.search(REAL_TAILNET)
    assert not pattern.search(LAN_PLACEHOLDER) and not pattern.search(TAILNET_PLACEHOLDER)


def test_child_forged_err_name_is_never_echoed(tmp_path):
    """The TYPE_NAME gate, pinned by attacking it from the guard's side.

    Guard module code runs in the CHILD, so it can write anything to the child's
    stdout — including a forged ``ERR`` line whose "exception name" field carries a
    real address. The parent must reject it: that field is validated against
    ``TYPE_NAME`` (no digits, no dots), so the diagnostic loses the name and the
    address never reaches the log. Delete the filter in _parse_verdict and this
    test fails by printing the address.
    """
    stub = _GUARD_BODY + (
        "import sys as _s\n"
        "_s.stdout.write('ERR\\t%s\\n'); _s.stdout.flush()\n" % REAL_LAN
    ) + _GUARD_CLI
    rc, out = _redactor_alone(stub, tmp_path, bare=True)
    assert rc == 3, f"a forged ERR verdict must poison the stream:\n{out}"
    assert REAL_LAN not in out, f"the name field was echoed unfiltered:\n{out}"
    assert "CANNOT LOAD PATTERN" in out, out


def test_child_forged_extra_ok_line_fails_closed(tmp_path):
    """Two verdicts is not a verdict.

    A guard that writes its own ``OK`` line — e.g. one whose pattern is a single
    address, which would "redact" every other value into silence — must not get to
    choose the pattern by out-voting the real one. Exactly-one is the contract, and
    the forged line's content is never printed either way.
    """
    forged = base64.b64encode(REAL_LAN.encode()).decode()
    stub = _GUARD_BODY + (
        "import sys as _s\n"
        f"_s.stdout.write('OK\\t{forged}\\t0\\n'); _s.stdout.flush()\n"
    ) + _GUARD_CLI
    rc, out = _redactor_alone(stub, tmp_path, bare=True)
    assert rc == 3, f"a second verdict line must fail closed, got rc={rc}:\n{out}"
    assert REAL_LAN not in out, out


def test_bounded_child_read_leaves_no_surviving_grandchild(tmp_path, monkeypatch):
    """The child's WHOLE process group must go down, not just the child.

    The bounded read is what keeps the STEP from hanging; this pins the other half
    of the reviewer's non-blocking note — that the spawned helper is actually
    taken with it, rather than sitting on the runner for its full sleep. Replace
    the process-group kill with a plain ``proc.kill()`` and the grandchild outlives
    load_pattern(), which fails here.
    """
    monkeypatch.setattr(redactor, "CHILD_TIMEOUT_S", 2)
    monkeypatch.setattr(redactor, "REAP_TIMEOUT_S", 1)
    pidfile = tmp_path / "grandchild.pid"
    stub = _GUARD_BODY.replace(
        _GUARD_IMPORTS,
        "import pathlib, re, subprocess, sys\n",
    ) + (
        "import os as _os\n"
        f"_p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],"
        " close_fds=False)\n"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(_p.pid))\n"
    ) + _GUARD_CLI
    stub_path = tmp_path / "gc" / "scripts" / "address_guard.py"
    stub_path.parent.mkdir(parents=True)
    stub_path.write_text(stub, encoding="utf-8")
    monkeypatch.setattr(redactor, "GUARD", stub_path)

    with pytest.raises(redactor.PatternUnavailable):
        redactor.load_pattern()

    pid = int(pidfile.read_text())
    # Give the reaper a moment; a SIGKILLed process that launchd has not reaped yet
    # still answers signal 0. If it is ALIVE (M5), it answers for the full 120 s.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return  # gone: the process group went down with the child
        except PermissionError:  # not ours (should not happen); treat as evidence
            break
        time.sleep(0.2)
    pytest.fail(f"grandchild pid {pid} survived load_pattern() — the process-group "
                "kill is missing, so a spawned helper holds runner resources")


def test_blob_that_is_not_strict_base64_is_rejected(tmp_path):
    """``validate=True`` is a real gate, not decoration.

    A URL-safe character spliced into the verdict's payload is discarded by a
    lenient decode, which would hand back the correct pattern and let the job
    proceed on a stream that is NOT the protocol the parent agreed to. Strict
    decoding rejects it: rc=3, and the payload's bytes never reach the log.
    """
    import base64 as _b64
    blob = _b64.b64encode(b"\\b(?:192\\.168\\.88\\.\\d{1,3})\\b").decode()
    spliced = blob[:10] + "-" + blob[10:]
    stub = _GUARD_BODY + (
        "import sys as _s\n"
        f"_s.stdout.write('OK\\t{spliced}\\t0\\n'); _s.stdout.flush()\n"
    ) + _GUARD_CLI
    rc, out = _redactor_alone(stub, tmp_path, bare=True)
    assert rc == 3, f"a non-strict-base64 payload must fail closed, got rc={rc}:\n{out}"
    assert REAL_LAN not in out, out
    assert "CANNOT LOAD PATTERN" in out, out


def test_parse_verdict_rejects_non_strict_base64_payload():
    """Direct unit pin for ``validate=True`` — why not end-to-end only?

    Through the CLI a stub that writes its own OK line produces TWO verdicts and
    dies at the exactly-one check before the base64 gate is ever reached (my first
    e2e version of this test passed against the mutated redactor for exactly that
    reason — the mutation battery caught it, audit 10c lesson applied again).
    Directly: drop validate and the spliced payload decodes to a WORKING pattern
    and the job proceeds on a stream that is not the agreed protocol.
    """
    clean = base64.b64encode(b"abc").decode()          # "YWJj"
    spliced = clean[:2] + "-" + clean[2:]               # discarded by lenient decode
    # control: the clean payload is accepted
    assert redactor._parse_verdict(f"OK\t{clean}\t0\n".encode()).pattern == "abc"
    with pytest.raises(redactor.PatternUnavailable):
        redactor._parse_verdict(f"OK\t{spliced}\t0\n".encode())


def test_pattern_unavailable_never_carries_an_unvalidated_name():
    """The constructor's own TYPE_NAME gate, pinned directly (mutation M2c).

    The parse side already filters forged names; this is the second lock. Remove
    THIS filter and only this filter and the forged-ERR end-to-end test would still
    pass — so both layers get their own pin rather than sharing one.
    """
    assert redactor.PatternUnavailable(REAL_LAN).type_name is None
    assert redactor.PatternUnavailable("ValueError").type_name == "ValueError"
    assert redactor.PatternUnavailable().type_name is None


def test_forged_err_name_alongside_a_real_one_keeps_only_the_real_one(tmp_path):
    """Pins the PARSE-side TYPE_NAME filter on its own (mutation M2 survivor).

    With one ERR line the constructor-side gate makes the two layers
    indistinguishable (defense in depth — the reviewer's words for the round-2
    M3 survivor). Two ERR lines separate them: the parse side must drop the
    forged name and honour the single valid one. Without that filter `names`
    holds two entries, no single name is trusted, and the reader loses the
    diagnosis this control exists to give.
    """
    stub = _GUARD_BODY + (
        "import sys as _s\n"
        "_s.stdout.write('ERR\\tValueError\\n')\n"
        "_s.stdout.write('ERR\\t%s\\n'); _s.stdout.flush()\n" % REAL_LAN
    ) + _GUARD_CLI
    rc, out = _redactor_alone(stub, tmp_path, bare=True)
    assert rc == 3, out
    assert REAL_LAN not in out, f"the forged name reached the log:\n{out}"
    assert "CANNOT LOAD PATTERN [ValueError]" in out, \
        f"the forged line must not cost the reader the real diagnosis:\n{out}"


# ── the parent's own payload handling, pinned directly ───────────────────────
#
# Everything above drives the whole tool. These three hit _parse_verdict with a
# single OK line whose PAYLOAD is hostile, which end-to-end cannot be arranged
# without adding a second verdict line (and so dying at the exactly-one check
# first) — the same lesson as the base64 pin above: test the guard you mean.

def test_uncompilable_pattern_text_fails_closed():
    """A payload that decodes fine but is not a valid regex must not reach redact().

    base64 of "[" decodes cleanly and then raises re.error at compile. The parent
    must turn that into the fixed fail-closed diagnostic, rc=3, not a traceback and
    not a partially-working redactor that prints report lines unredacted.
    """
    blob = base64.b64encode(b"[").decode()
    with pytest.raises(redactor.PatternUnavailable):
        redactor._parse_verdict(f"OK\t{blob}\t0\n".encode())


@pytest.mark.parametrize("flags_txt", ["0x200", "99999999999999999999", "-1",
                                       "__import__('os')", "[1,2]", "True", "None",
                                       # the discriminator for literal_eval vs eval:
                                       # eval() returns the int 0 and would ACCEPT
                                       # this, literal_eval refuses it. Without it
                                       # the eval() mutant survives (my first cut).
                                       "len(b'')"])
def test_flags_field_accepts_only_a_bounded_int(flags_txt):
    """The flags field is the one place a number crosses the boundary.

    ``ast.literal_eval`` (not ``eval``) plus an int/range check means expressions,
    lists, bools and out-of-range values are all refused. An unbounded int would
    reach ``re.compile`` and a non-int would be a type error at best.
    """
    blob = base64.b64encode(b"abc").decode()
    with pytest.raises(redactor.PatternUnavailable):
        redactor._parse_verdict(f"OK\t{blob}\t{flags_txt}\n".encode())


def test_oversized_pattern_text_fails_closed():
    """Length bound: the payload must not be able to size the parent's memory."""
    blob = base64.b64encode(b"a" * (redactor.MAX_PATTERN_CHARS + 1)).decode()
    with pytest.raises(redactor.PatternUnavailable):
        redactor._parse_verdict(f"OK\t{blob}\t0\n".encode())


def test_verdict_without_trailing_newline_is_still_accepted():
    """The child flushes and exits, so a final line may lack its newline.

    Accepted deliberately: refusing it would make the common case depend on the
    child's last write ending in \\n. Note this is NOT the merged-junk case — there
    the junk PRECEDES the verdict on the same line, so no line is well-formed.
    """
    blob = base64.b64encode(b"abc").decode()
    assert redactor._parse_verdict(f"OK\t{blob}\t0".encode()).pattern == "abc"


def test_child_has_no_descriptor_to_the_job_log():
    """Structural statement of the boundary, pinned so a future edit cannot drift.

    The containment claim is: the child gets NO descriptor that reaches the step.
    If anyone adds ``stderr=PIPE``-adjacent plumbing, ``pass_fds``, an inherited
    handle or ``close_fds=False``, the guard's import-time code gets a path to the
    job log again and this assertion fails with the reason in its message.
    """
    src = REDACTOR.read_text(encoding="utf-8")
    assert "stdin=subprocess.DEVNULL" in src
    assert "stderr=subprocess.DEVNULL" in src
    assert "stdout=subprocess.PIPE" in src
    assert "close_fds=True" in src, "close_fds=False would hand the child our fds"
    assert "pass_fds" not in src, "pass_fds deliberately forwards descriptors"
    assert "start_new_session=True" in src, "needed to take the group down whole"


def test_a_repo_root_module_cannot_shadow_the_childs_stdlib_imports(tmp_path):
    """The child must not import a committed module from the checkout root.

    With ``python -c``, ``sys.path[0]`` is the CURRENT DIRECTORY — which in CI is
    the checkout root — so a committed ``base64.py`` there would satisfy the
    child's own ``import base64`` and its module body would run in the child.
    Measured on py3.11.16: without ``-P`` the repo file wins; with it, cwd is kept
    out of sys.path (PEP 674). The vector is created by THIS design (the pre-fix
    redactor imported in-process, where sys.path[0] was the script's own dir), so
    it is this card's to close and to pin.

    Constructed as a real attack: the shadow emits a FORGED OK verdict carrying a
    single-address pattern, which would "redact" every other value into silence —
    the weakest-possible-FORBIDDEN trick from the other side of the boundary.
    """
    work = tmp_path / "shadow"
    (work / "scripts").mkdir(parents=True)
    (work / ".github" / "scripts").mkdir(parents=True)
    shutil.copy2(REDACTOR, work / ".github" / "scripts" / "redact_guard_report.py")
    # a working guard, so the legit verdict would be a real pattern
    (work / "scripts" / "address_guard.py").write_text(_GUARD_BODY + _GUARD_CLI,
                                                       encoding="utf-8")
    # the shadow: emits a one-address pattern as if it were the guard's FORBIDDEN
    forged = base64.b64encode(REAL_LAN.encode()).decode()
    (work / "base64.py").write_text(
        "import sys\n"
        f"sys.stdout.write('OK\\t{forged}\\t0\\n')\n"
        "sys.stdout.flush()\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, "-E", "-s", "-W", "ignore",
         str(work / ".github" / "scripts" / "redact_guard_report.py")],
        input=f"  docs/x.md:1: {REAL_LAN}\n  docs/y.md:2: {REAL_TAILNET}\n".encode(),
        capture_output=True, cwd=work,
    )
    out = proc.stdout.decode() + proc.stderr.decode()
    assert proc.returncode == 0, out
    # Both addresses must be scrubbed. If the shadow had won, only REAL_LAN would
    # be masked and REAL_TAILNET would print in the clear.
    assert REAL_LAN not in out and REAL_TAILNET not in out, \
        f"a repo-root module shadowed the child's stdlib:\n{out}"
    assert out.count("***") == 2, out


def test_child_invocation_drops_cwd_from_its_own_sys_path():
    """Cheap structural backstop to the shadow test above.

    The child sanitises ``sys.path`` itself (drop ``''``/``'.'``) BEFORE importing
    anything resolvable from it. Pinned structurally because the end-to-end attack
    above can only show the vector is closed *on the interpreter running the suite*;
    this pin says HOW it is closed, so it cannot silently rot into "-P does it".

    Deliberately NOT the ``-P`` flag: ``-P`` needs Python >= 3.11 and this repo's
    floor is 3.10 (hscc-cli/ and hscc-project/ pyproject.toml both
    ``requires-python = ">=3.10"``). An unsupported flag makes the child exit 2
    before it emits a verdict — that would take the whole guard down on a supported
    interpreter, i.e. a fail-closed that is really a breakage.
    """
    src = REDACTOR.read_text(encoding="utf-8")
    child = src.split('CHILD_SOURCE = r"""', 1)[1].split('"""', 1)[0]
    assert 'not in ("", ".")' in child, \
        "child no longer drops cwd from its own sys.path"
    # the sanitisation must come BEFORE the stdlib imports it protects
    assert child.index('not in ("", ".")') < child.index("import base64"), \
        "sys.path sanitised after the imports it is supposed to protect"
    assert 'import sys' == child.strip().splitlines()[0], \
        "child must import only `sys` before sanitising (sys is a builtin)"

# ── review round 4 (t_38fd345f): authorship of the verdict, and the import cache ─
#
# Two publish channels survived rounds 1-3 and were reproduced by the reviewer at
# the gated tip (6 leaks for D0 on both interpreters, 3 for D1 on 3.13):
#
#   D0 — the pipe validated FORM, never AUTHORSHIP. The guard's own module body
#        runs inside the child, so it can write a well-formed ``OK`` line itself
#        and ``os._exit(0)`` before the child speaks: the parent was left holding
#        one perfectly-formed verdict that the *guard* had authored, and masked
#        the report with whatever pattern that line named.
#   D1 — the child loaded the guard through ``spec_from_file_location`` =
#        ``SourceFileLoader`` = ``scripts/__pycache__``. A PEP 552 unchecked-hash
#        ``.pyc`` is trusted WITHOUT reading the ``.py``, so a committed artifact
#        (invisible to ``scan_blob``: NUL bytes ⇒ binary ⇒ skipped) handed the
#        child a different FORBIDDEN than the step's own guard run used.
#
# The closure is stated in the redactor's trust-boundary docstring; these tests
# pin the two halves that a future edit is most likely to undo: the source-shape
# gates (a weakened parse reopens D0) and the cache refusal (a reintroduced
# loader reopens D1). All fixtures keep the repo rule: no address-shaped text
# committed here — every address is the runtime-assembled REAL_LAN/REAL_TAILNET.


# ── D0 half 1: source authority — only ONE source shape is a statement of FORBIDDEN

def _extract(blob):
    """Parent-side extraction from raw bytes (zero execution — that is the point)."""
    return redactor._extract_source_pattern(blob)


@pytest.mark.parametrize("blob", [
    # re-bound import: the round-1..3 stub scaffold, and the exact shape D0 needs
    # to launder a pattern the source never states
    b"import re; _m = object(); FORBIDDEN = re.compile(_m.FORBIDDEN)",
    # f-string: JoinedStr — cannot be a data statement of a secret-scanning regex
    b"import re; FORBIDDEN = re.compile(f'abc')",
    # concatenation is a BinOp, not one literal datum: refused on shape.
    # (ast.literal_eval also refuses it here on 3.11, but that is the
    # evaluator's choice, not the contract — the flags case below is.)
    b"import re; FORBIDDEN = re.compile('a' + 'b')",
    # name / call / attribute indirection
    b"import re; PAT = 'abc'; FORBIDDEN = re.compile(PAT)",
    b"import re; FORBIDDEN = re.compile(str('abc'))",
    b"import re; re = None; FORBIDDEN = object.__getattribute__(0, 'x')",
    # signature deviations: keyword flags, three args
    b"import re; FORBIDDEN = re.compile('abc', flags=2)",
    b"import re; FORBIDDEN = re.compile('abc', 0, 0)",
    # flags must be a plain int literal in range: bool (isinstance(int) trap),
    # out-of-range, string
    b"import re; FORBIDDEN = re.compile('abc', True)",
    b"import re; FORBIDDEN = re.compile('abc', 999)",
    b"import re; FORBIDDEN = re.compile('abc', '2')",
    # numeric expression as flags: ast.literal_eval FOLDS this to 2 (measured),
    # so a literal_eval-based check ACCEPTS it; the shape gate refuses. The
    # mutant-killer for 'revert the shape gate to an evaluator'.
    b"import re; FORBIDDEN = re.compile('abc', 2-0)",
    # text must be a non-empty str constant
    b"import re; FORBIDDEN = re.compile('')",
    b"import re; FORBIDDEN = re.compile(5)",
    # FORBIDDEN not a compile call at all, or absent, or source unparsable
    b"import re; FORBIDDEN = 'abc'",
    b"import re",
    b"def (",
])
def test_source_authority_refuses_every_deviation_from_the_one_accepted_shape(blob):
    with pytest.raises(redactor.PatternUnavailable):
        _extract(blob)


@pytest.mark.parametrize("blob,expected", [
    (b"import re; FORBIDDEN = re.compile('xyz')", ("xyz", re.compile('xyz').flags)),
    (b"import re; FORBIDDEN = re.compile('xyz', 2)", ("xyz", 2 | re.UNICODE)),
    # annotated assignment binds under exec too, so the source check accepts it
    (b"import re; FORBIDDEN: object = re.compile('xyz')",
     ("xyz", re.compile('xyz').flags)),
    # last module-level binding wins, mirroring what exec ends up binding
    (b"import re; FORBIDDEN = re.compile('first'); FORBIDDEN = re.compile('last')",
     ("last", re.compile('last').flags)),
])
def test_source_authority_accepts_exactly_the_stated_data_shapes(blob, expected):
    pattern = _extract(blob)
    assert (pattern.pattern, pattern.flags) == expected


def test_source_authority_of_the_shipped_guard_matches_its_compiled_object():
    """The accepted shape must be the shipped guard's actual shape — and what
    comes back is COMPILED, not the (text, flags) tuple the AST saw.

    A bare ``re.compile(text)`` gains ``re.UNICODE`` at compile time: an
    implementation that kept the argv tuple for the agreement check fails on the
    repo's own current file (measured in round 4), so the agreement is defined on
    compiled objects and this pins it.
    """
    pattern = _extract(redactor._read_guard_source())
    assert pattern.pattern == GUARD_PATTERN_TEXT
    assert pattern.flags == re.compile(GUARD_PATTERN_TEXT).flags


# ── D0 half 2: agreement — the child's verdict cannot replace the pattern ──────

def _stub_forged_ok(pattern_text, exit_after):
    """A guard whose BODY writes its own protocol line — the D0 authorship move.

    Writes a well-formed ``OK`` (base64 pattern + tab + flags + newline, built
    with chr() so this file stays free of literal-tab/backslash soup) straight
    from guard module code, then optionally ``os._exit(0)`` so the child's own
    verdict never happens — exactly the reviewer's forged-exit repro.
    """
    b64 = base64.b64encode(pattern_text.encode("utf-8")).decode("ascii")
    return (
        _GUARD_BODY.replace(
            _GUARD_IMPORTS, "import base64, os, pathlib, re, sys\n")
        + 'sys.stdout.write("OK" + chr(9) + "%s" + chr(9) + "%d" + chr(10))\n'
        % (b64, int(GUARD_FLAGS))
        + "sys.stdout.flush()\n"
        + ("os._exit(0)\n" if exit_after else "")
        + _GUARD_CLI
    )


def test_guard_authored_ok_cannot_replace_the_pattern(tmp_path):
    """D0 verbatim: forged well-formed OK + early exit ⇒ refused, not honoured.

    The guard body emits a perfectly well-formed verdict naming a pattern the
    source does not carry ("ZZZNOPE" — matches nothing in the report), then exits
    before the child speaks. Old redactor: honoured it, and since the named
    pattern matches nothing the report went out UNREDACTED with rc=0. Now:
    agreement fails, rc=3, nothing echoed.
    """
    rc, out = _redactor_alone(_stub_forged_ok("ZZZNOPE", True), tmp_path)
    assert rc == 3, f"a guard-authored verdict must not steer redaction:\n{out}"
    assert "PATTERN DISAGREEMENT" in out, out
    assert "CANNOT LOAD PATTERN" in out, out
    assert REAL_LAN not in out, f"the report went out unredacted:\n{out}"
    assert "docs/x.md:1" not in out, "refusal must echo no report line"
    assert "ZZZNOPE" not in out, "a refused verdict must not be quoted back"


def test_guard_authored_ok_that_happens_to_agree_is_harmless(tmp_path):
    """The authorship attack is inert because masking uses the SOURCE pattern.

    Same forgery, but naming the real pattern: agreement passes and the run
    succeeds — and that is safe by construction, because the redactor masks with
    the source-derived pattern regardless of what the child said. Pin: this can
    never be the channel, so no future 'fix' needs to chase forgeries harder.
    """
    rc, out = _redactor_alone(_stub_forged_ok(GUARD_PATTERN_TEXT, True), tmp_path)
    assert rc == 0, out
    assert "***" in out, f"agreement must keep the happy path working:\n{out}"
    assert REAL_LAN not in out, out


def test_two_ok_lines_even_naming_the_real_pattern_fail_closed(tmp_path):
    """The count gate, isolated from the agreement rail.

    Both OK lines name the REAL pattern here, so agreement would pass on either
    one — only the protocol contract ("exactly one verdict line") refuses. Two
    verdicts means the last-emit guarantee is gone (D0's whole lesson), so the
    stream is not a verdict at all: rc=3, nothing echoed."""
    rc, out = _redactor_alone(_stub_forged_ok(GUARD_PATTERN_TEXT, False), tmp_path)
    assert rc == 3, f"two verdict lines must fail closed even when both agree:\n{out}"
    assert "CANNOT LOAD PATTERN" in out, out
    assert REAL_LAN not in out, out


def test_two_ok_lines_fail_closed(tmp_path):
    """The last-emit assumption is gone, so the count gate carries the contract:
    a forged OK followed by the child's own = two verdicts = not a verdict."""
    rc, out = _redactor_alone(_stub_forged_ok("ZZZNOPE", False), tmp_path)
    assert rc == 3, f"two OK lines must not be resolved by picking one:\n{out}"
    assert "CANNOT LOAD PATTERN" in out, out
    assert REAL_LAN not in out, out


# ── D1: the import cache cannot shadow the guard ───────────────────────────────

def _load_pattern_source():
    """The load_pattern() function's source text, for structural pins."""
    import inspect
    return inspect.getsource(redactor.load_pattern)


def _pyc_bytes(src_text, flags):
    """A PEP 552 .pyc body for SRC_TEXT with the given header FLAGS field."""
    import marshal
    code = compile(src_text, "address_guard.py", "exec")
    head = (importlib.util.MAGIC_NUMBER + int(flags).to_bytes(4, "little")
            + b"\0" * 8)
    return head + marshal.dumps(code)


def _plant_pyc(work, flags, src_text="import re; FORBIDDEN = re.compile('1[.]2[.]3[.]4')\n"):
    cache = work / "scripts" / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / ("address_guard.%s.pyc" % sys.implementation.cache_tag)).write_bytes(
        _pyc_bytes(src_text, flags))


@pytest.mark.parametrize("flags,label", [
    (0b01, "unchecked-hash — trusted WITHOUT reading the .py (the D1 class)"),
])
def test_unchecked_hash_pyc_on_the_cache_name_is_refused(tmp_path, flags, label):
    guard = tmp_path / "scripts" / "address_guard.py"
    guard.parent.mkdir(parents=True)
    guard.write_text("import re; FORBIDDEN = re.compile('x')\n", encoding="utf-8")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(redactor, "GUARD", guard)
    try:
        _plant_pyc(tmp_path, flags)
        with pytest.raises(redactor.PatternCacheShadowed):
            redactor._refuse_if_cache_shadows()
    finally:
        monkey.undo()


def test_ordinary_local_pyc_files_must_not_break_the_job(tmp_path):
    """Dev machines carry pytest-generated scripts/__pycache__ (measured: every
    suite run writes one). Timestamp (0b00) and checked-hash (0b11) artifacts
    are inert or self-validating and must NOT fail the job — refusing them would
    turn D1's defense-in-depth into a CI-wide outage."""
    guard = tmp_path / "scripts" / "address_guard.py"
    guard.parent.mkdir(parents=True)
    guard.write_text("import re; FORBIDDEN = re.compile('x')\n", encoding="utf-8")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(redactor, "GUARD", guard)
    try:
        for flags in (0b00, 0b11):
            _plant_pyc(tmp_path, flags)
            redactor._refuse_if_cache_shadows()  # must not raise
        # truncated header: unreadable, but not the dangerous class — ignore it
        _plant_pyc(tmp_path, 0b00)
        p = (tmp_path / "scripts" / "__pycache__"
             / ("address_guard.%s.pyc" % sys.implementation.cache_tag))
        p.write_bytes(b"\0" * 4)
        redactor._refuse_if_cache_shadows()  # must not raise
    finally:
        monkey.undo()


def test_unchecked_hash_pyc_makes_the_cli_fail_closed_end_to_end(tmp_path):
    """The reviewer planted exactly this artifact with
    ``compileall --invalidation-mode unchecked-hash``; the CLI must refuse to
    redact at all while it sits on the cache name."""
    rc, out = _redactor_alone(
        _GUARD_BODY + _GUARD_CLI, tmp_path,
        plant=lambda work: _plant_pyc(work, 0b01),
    )
    assert rc == 3, f"an unchecked-hash pyc must fail the job:\n{out}"
    assert "CACHE SHADOW" in out, out
    assert "CANNOT LOAD PATTERN" in out, out
    assert "PATTERN DISAGREEMENT" not in out, "distinct diagnoses, distinct sentences"
    assert REAL_LAN not in out, out
    # The refusal is not just implemented, it is INVOKED, and before the source
    # is trusted: removing the call site from load_pattern() (or moving it after
    # extraction) must fail here, not only in the unit test of the check itself.
    lp = _load_pattern_source()
    assert "_refuse_if_cache_shadows()" in lp, "the cache check is never invoked"
    assert lp.index("_refuse_if_cache_shadows()") < lp.index("_extract_source_pattern("), \
        "the cache refusal must come BEFORE the source is trusted"


def test_poisoned_timestamp_pyc_cannot_steer_redaction(tmp_path):
    """D1's mechanism is GONE, not merely distrusted: the child ``compile()``s
    source bytes and never consults the cache, so even an artifact a loader
    would honour cannot change the pattern used — and the benign timestamp kind
    (which the refusal lets through) still redacts correctly with rc=0."""
    rc, out = _redactor_alone(
        _GUARD_BODY + _GUARD_CLI, tmp_path,
        plant=lambda work: _plant_pyc(work, 0b00),
    )
    assert rc == 0, out
    assert "***" in out, out
    assert REAL_LAN not in out, out


def test_child_source_has_no_loader_and_digests_before_it_speaks():
    """Structural pins for the D1 + authorship closure — the end-to-end tests can
    only show the vector closed on THIS interpreter; these say HOW."""
    src = REDACTOR.read_text(encoding="utf-8")
    child = src.split('CHILD_SOURCE = r"""', 1)[1].split('"""', 1)[0]
    # the closure is EXPLAINED in comments that name the loaders; the pins are
    # about CODE, so judge the code with comment lines removed
    code = chr(10).join(ln for ln in child.splitlines()
                        if not ln.strip().startswith("#"))
    assert "spec_from_file_location" not in code, "SourceFileLoader = cache = D1"
    assert "SourceFileLoader" not in code
    assert "importlib" not in code, "no import machinery on the child path at all"
    assert "compile(source" in code, "child must exec the SOURCE BYTES, not a module"
    digest_check = code.index("hashlib.sha256(source).hexdigest()")
    first_ok = code.index('emit("OK"')
    assert digest_check < first_ok, \
        "the child may only speak AFTER pinning its bytes to the parent's digest"
    assert 'raise RuntimeError("source bytes do not match the pinned digest")' \
        in child, "a mismatch must raise BEFORE any OK can be emitted"


def test_child_refuses_bytes_that_do_not_match_the_pinned_digest():
    """The digest pin, exercised: hand _child_pipe a digest that cannot match the
    real file and the child must answer ERR (a NAME), never an OK — this is the
    half of authorship the file-swap race would otherwise bypass. The verdict
    then fails closed in the parser with exactly that type name."""
    raw = redactor._child_pipe("0" * 64)
    assert raw.startswith(b"ERR") and b"OK" not in raw, \
        f"a mismatched child must not emit a verdict line: {raw!r}"
    with pytest.raises(redactor.PatternUnavailable) as excinfo:
        redactor._parse_verdict(raw)
    assert excinfo.value.type_name == "RuntimeError"
