"""Gate guard for the alone-vs-in-leg sweep (t_6cd17e1c).

The property: **no ``hscc_daemon`` test file may answer alone differently than it
does inside the leg.** ``scripts/run_tests.sh`` runs a leg as ONE pytest process,
so a leg can print ``ALL GREEN`` while a file inside it FAILS — or skips for a
different reason — when run by itself. When that happens the leg is answering a
question about test *order*, and the dual-interpreter stamp's promise ("a FAILED
in a sibling leg is a regression") is false. It has happened twice in
``hscc_daemon``; the leg was green both times
(``docs/audits/daemon-alone-run-order-dependence_t_572b4856.md`` §2, §7).

The sweep that measures it costs ~12 min per interpreter, so it runs on a
schedule (``scripts/alone_sweep_gate.sh`` + ``scripts/install_alone_sweep_gate.sh``,
decision recorded in the gate's header and in the audit's §9). This file is the
half that stays in the per-PR lane: it spends milliseconds and asserts that the
scheduled gate is still *capable* of going red —

  * the measurement stays single-sourced (the gate routes, it does not
    re-measure — a second argv/scrub implementation is how a gate quietly starts
    measuring something else),
  * the alone side still mirrors the runner's argv and its ``SUITE_ENV_UNSET``
    scrub (drift here makes the comparison dishonest, and dishonest-by-a-little
    is the whole failure class),
  * the enumeration is still the whole leg dir, never a curated subset,
  * the diff tool has TEETH, measured on fixtures rather than asserted: a status
    change, a skip-reason change whose tally matches, a collection error, and a
    file absent from the leg — plus one green fixture, because a tool that fails
    for everything also "catches" everything,
  * and the gate fails CLOSED: no interpreter, broken wiring, or a concurrent
    pass all exit 2, never 0.

Nothing here runs pytest over the checkout — by design. Two pytest processes over
one checkout trip the port/lock fixtures, and that flake reads exactly like an
order effect.
"""

import glob
import os
import re
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO, "scripts")
GATE = os.path.join(SCRIPTS, "alone_sweep_gate.sh")
INSTALLER = os.path.join(SCRIPTS, "install_alone_sweep_gate.sh")
RUNNER = os.path.join(SCRIPTS, "run_tests.sh")
SUPPORT = os.path.join(REPO, "docs", "audits", "support", "t_572b4856")
SWEEP = os.path.join(SUPPORT, "alone_sweep.sh")
DIFF = os.path.join(SUPPORT, "diff_alone_vs_leg.py")
LEG_DIR = os.path.join(REPO, "hscc_daemon")


def _read(path):
    assert os.path.isabs(path)
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _run(cmd, env=None, cwd=REPO, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          cwd=cwd, timeout=timeout)


# ── the measurement stays in ONE place ──────────────────────────────────────

def test_gate_routes_the_shipped_sweep_and_does_not_re_measure_it():
    """The gate must call the audit's sweep and contain no pytest call itself.

    A second implementation of "run one file alone" is how a gate starts
    measuring something adjacent to the property instead of the property: the
    argv, the scrub and the enumeration would live in two files and only one of
    them gets read.
    """
    gate = _read(GATE)
    assert os.path.isfile(SWEEP) and os.path.isfile(DIFF), (
        "the sweep tooling moved — the gate points at "
        "docs/audits/support/t_572b4856/alone_sweep.sh")
    assert "alone_sweep.sh" in gate, "the gate no longer routes the shipped sweep"
    assert "-m pytest" not in gate, (
        "the gate now invokes pytest itself: the measurement must stay in "
        "alone_sweep.sh (single-sourced argv + scrub), not be re-implemented here")


def test_alone_scrub_matches_the_runner_scrub():
    """Same scrub on both sides, or the comparison is not measuring one variable.

    The leg runs under ``env "${ENV_UNSET_ARGS[@]}"`` built from
    ``SUITE_ENV_UNSET``; the alone side mirrors it with explicit ``-u`` flags. If
    a sixth pin is added to the runner and not to the sweep, every alone-vs-leg
    diff from then on is taken against a *different environment* on each side —
    and a harness bug starts looking like an order effect, or the reverse.
    """
    runner = _read(RUNNER)
    block = re.search(r"SUITE_ENV_UNSET=\((.*?)\n\)", runner, re.S)
    assert block, "run_tests.sh no longer declares SUITE_ENV_UNSET as a list"
    pins = set()
    for line in block.group(1).splitlines():
        pins.update(t for t in line.split("#")[0].split() if t)

    sweep = _read(SWEEP)
    cmd = re.search(r"env\b(.*?)\"?PY\"?\s+-m pytest", sweep, re.S)
    assert cmd, "the sweep no longer scrubs with an explicit `env -u ...` list"
    mirrored = set(re.findall(r"(?:^|\s)-u\s+([A-Z][A-Z0-9_]+)", cmd.group(1)))
    assert mirrored == pins, (
        f"scrub drift: runner unpins {sorted(pins)}, the alone side unpins "
        f"{sorted(mirrored)} — the two sides are no longer the same environment")


def test_alone_argv_matches_the_runner_argv():
    """Same pytest flags on both sides (only the file set may differ)."""
    def flags(text, pattern):
        m = re.search(pattern, text, re.S)
        assert m, "pytest invocation shape changed in a way this guard cannot read"
        toks = m.group(1).split()
        out, i = [], 0
        while i < len(toks):
            t = toks[i]
            if t.startswith("-"):
                out.append(t)
                if t == "-p" and i + 1 < len(toks):
                    out.append(toks[i + 1])
                    i += 1
            i += 1
        return out

    runner = flags(_read(RUNNER), r'-m pytest\\?\s+(.*?)"\$@"')
    # Up to the line end on purpose: the sweep adds --junitxml on a second line
    # and the runner is handed it too (`run_tests.sh` forwards extra argv), so
    # that flag is on BOTH sides and comparing it would prove nothing.
    sweep = flags(_read(SWEEP), r'-m pytest\s+(-[^\n]*?)(?:\\\s*$|\n)')
    assert runner == sweep, (
        f"argv drift: in-leg {runner} vs alone {sweep} — the sweep must mirror "
        "the runner's flags, because the file set is the only intended variable")


def test_sweep_enumerates_the_whole_leg_dir():
    """No curated subset: the enumeration has to be the dir-wide glob.

    A subset is how a nightly quietly stops covering the files that regress. The
    card's option (b) — sweep only the files that touch ``sys.path`` — was
    rejected partly for this reason: the *at-risk* set is exactly what a bad fix
    changes, and §7's regression surfaced in files nothing would have flagged.
    """
    sweep = _read(SWEEP)
    assert re.search(r'FILES=\("\$LEG"/tests/test_\*\.py\)', sweep), (
        "the sweep's enumeration is no longer the whole leg dir")
    for path in (SWEEP, GATE, INSTALLER):
        text = _read(path)
        assert not re.search(r"\btest_[a-z0-9_]+\.py\b",
                             re.sub(r"^[ \t]*#.*$", "", text, flags=re.M)), (
            f"{os.path.basename(path)} names a specific test file outside a "
            "comment: the gate must not special-case files")


# ── the diff tool has teeth (fixtures, so this costs milliseconds) ──────────

def _write_junit(path, cases):
    """cases: [(classname, name, status|None)] -> a junit xml the diff reads."""
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", name="pytest",
                          tests=str(len(cases)))
    for classname, name, status in cases:
        tc = ET.SubElement(suite, "testcase", classname=classname, name=name,
                           time="0.01")
        if status:
            ET.SubElement(tc, status, message=f"status={status}", type=status)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def _fixture(tmp_path, leg_cases, files, leg_skips=()):
    """Build an out-dir shaped like alone_sweep.sh's.

    leg_cases: the in-leg junit. files: {stem: (cases, rc, skips)} per alone run.
    skips: [(path, line, reason)] as pytest's `-rs` would print them.
    """
    out = tmp_path / "sweep"
    (out / "xml").mkdir(parents=True)
    (out / "logs").mkdir()
    (out / "rc").mkdir()
    _write_junit(str(out / "inleg.xml"), leg_cases)
    with open(out / "inleg.log", "w", encoding="utf-8") as fh:
        for path, line, reason in leg_skips:
            fh.write(f"SKIPPED [1] {path}:{line}: {reason}\n")
    for stem, (cases, rc, skips) in files.items():
        _write_junit(str(out / "xml" / f"{stem}.xml"), cases)
        (out / "rc" / f"{stem}.rc").write_text(f"{rc}\n")
        with open(out / "logs" / f"{stem}.log", "w", encoding="utf-8") as fh:
            for path, line, reason in skips:
                fh.write(f"SKIPPED [1] {path}:{line}: {reason}\n")
    return out


def _diff(out):
    return _run([sys.executable, DIFF, str(out)])


PASSING_LEG = [
    ("tests.test_alpha", "test_one", None),
    ("tests.test_alpha", "test_two", "skipped"),
    ("tests.test_beta", "test_three", None),
]
ALPHA_OK = [("tests.test_alpha", "test_one", None),
            ("tests.test_alpha", "test_two", "skipped")]
BETA_OK = [("tests.test_beta", "test_three", None)]
SKIP_AT = [("tests/test_alpha.py", "12", "No module named 'hermes_cli'")]


def test_diff_is_green_when_every_file_agrees(tmp_path):
    """A green fixture, so the three red fixtures below cannot be rc=2 noise."""
    out = _fixture(tmp_path, PASSING_LEG, {
        "test_alpha": (ALPHA_OK, 0, SKIP_AT),
        "test_beta": (BETA_OK, 0, []),
    }, leg_skips=SKIP_AT)
    proc = _diff(out)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "files whose ALONE run differs from the leg: 0" in proc.stdout


def test_diff_catches_a_status_that_changed(tmp_path):
    out = _fixture(tmp_path, PASSING_LEG, {
        "test_alpha": ([("tests.test_alpha", "test_one", "failure"),
                        ("tests.test_alpha", "test_two", "skipped")], 1, SKIP_AT),
        "test_beta": (BETA_OK, 0, []),
    }, leg_skips=SKIP_AT)
    proc = _diff(out)
    assert proc.returncode == 1, proc.stdout
    assert "test_alpha" in proc.stdout
    assert "passed" in proc.stdout and "failure" in proc.stdout


def test_diff_catches_a_skip_reason_change_with_a_matching_tally(tmp_path):
    """The subtle half: same counts, different *reason* — still a different answer.

    This is the shape the resident-``hermes_cli`` leak made: one ``importorskip``
    line reported ``No module named 'hermes_cli'`` alone and ``No module named
    'utils'`` in-leg. junit alone cannot see it; the stamp rule is read off `-rs`.
    """
    out = _fixture(tmp_path, PASSING_LEG, {
        "test_alpha": (ALPHA_OK, 0,
                       [("tests/test_alpha.py", "12", "No module named 'utils'")]),
        "test_beta": (BETA_OK, 0, []),
    }, leg_skips=SKIP_AT)
    proc = _diff(out)
    assert proc.returncode == 1, proc.stdout
    assert "test_alpha" in proc.stdout
    assert "reason" in proc.stdout


def test_diff_catches_a_file_that_errors_alone_at_collection(tmp_path):
    """The M3 shape: a bare `import hscc` that only resolves because another file
    left the dir on sys.path. Alone -> collection error; in-leg -> green."""
    out = _fixture(tmp_path, PASSING_LEG, {
        "test_alpha": ([("tests.test_alpha", "<collection>", "error")], 2, []),
        "test_beta": (BETA_OK, 0, []),
    }, leg_skips=SKIP_AT)
    proc = _diff(out)
    assert proc.returncode == 1, proc.stdout
    assert "test_alpha" in proc.stdout and "error" in proc.stdout


def test_diff_catches_a_file_the_leg_never_ran(tmp_path):
    """A file present alone but absent in-leg is a difference, not a free pass.

    `load_junit`/`tally` would compare {} to {} and match; the tool requires a
    non-empty in-leg map, and this pins that it still does.
    """
    out = _fixture(tmp_path, PASSING_LEG, {
        "test_gamma": (BETA_OK, 0, []),
    }, leg_skips=SKIP_AT)
    proc = _diff(out)
    assert proc.returncode == 1, proc.stdout
    assert "test_gamma" in proc.stdout


# ── the gate fails closed ───────────────────────────────────────────────────

def _gate(*args, out=None, env=None, python=None):
    """Run the gate with an interpreter that exists.

    The scripts-leg conftest redirects ``HOME`` into a sandbox, and the gate's
    DEFAULT interpreters are `$HOME/...` paths, so without an explicit
    ``--python`` every test here would (correctly) hit the gate's no-interpreter
    refusal and test the wrong path. ``python=False`` means "inject nothing", for
    the cases that are testing how the interpreter list is resolved.
    """
    cmd = ["bash", GATE]
    if out is not None:
        cmd += ["--out", str(out)]
    if python is not False and not any(a == "--python" for a in args):
        cmd += ["--python", python or sys.executable]
    cmd += list(args)
    e = dict(os.environ)
    e["HSCC_ALONE_SWEEP_REPO"] = REPO
    e.update(env or {})
    return _run(cmd, env=e, cwd=None)


def test_gate_preflights_the_real_checkout_and_names_the_whole_inventory(tmp_path):
    proc = _gate("--preflight", out=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    on_disk = len(glob.glob(os.path.join(LEG_DIR, "tests", "test_*.py")))
    m = re.search(r"would sweep (\d+) files per interpreter", proc.stdout)
    assert m, proc.stdout
    assert int(m.group(1)) == on_disk, (
        f"the gate would sweep {m.group(1)} files but the leg dir holds "
        f"{on_disk} — the enumeration has narrowed")


def test_gate_is_not_measured_when_no_interpreter_exists(tmp_path):
    """No interpreter == nothing swept == NOT a pass. The green-because-nothing-
    ran failure is the one this gate exists to make impossible."""
    proc = _gate("--python", os.path.join(str(tmp_path), "nope-python"),
                 out=tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "NOT MEASURED" in (proc.stdout + proc.stderr)


def test_gate_is_not_measured_when_the_wiring_is_broken(tmp_path):
    proc = _gate("--preflight", out=tmp_path,
                 env={"HSCC_ALONE_SWEEP_REPO": str(tmp_path / "not-a-repo")})
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "NOT MEASURED" in (proc.stdout + proc.stderr)


def test_gate_refuses_a_concurrent_pass(tmp_path):
    """Two passes at once break the measurement's own precondition (one pytest
    process at a time over this checkout), so the second one must refuse."""
    lock = tmp_path / ".gate.lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{os.getpid()}\n")   # this process is alive
    proc = _gate("--preflight", out=tmp_path)       # lock is taken after preflight
    # preflight exits first, so drive the real path with an interpreter that is
    # present: the lock check is what must stop it, long before any sweep runs.
    assert proc.returncode == 0, proc.stdout        # preflight never touches the lock
    proc = _gate(out=tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "another pass" in proc.stdout + proc.stderr


def test_gate_leaves_no_lock_behind(tmp_path):
    """A pass that refused must not strand the lock and block every later pass."""
    dead = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
    dead.wait()                                   # reaped: this pid is free
    lock = tmp_path / ".gate.lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{dead.pid}\n")
    proc = _gate("--python", os.path.join(str(tmp_path), "nope-python"),
                 out=tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "stale lock" in proc.stdout, "a dead holder must not wedge the gate"
    assert not lock.exists(), (
        "the lock outlived its holder's process — later passes could never run")


# ── the scheduler wiring ────────────────────────────────────────────────────

def _stub_hermes(tmp_path):
    """A fake `hermes` that logs its argv; the installer prefers this path.

    Also plants an executable `python` at the canonical stamp-interpreter path
    (`<home>/.hermes/hermes-agent/venv/bin/python`) — the installer resolves and
    bakes the interpreters it can actually see, and refuses when it sees none.
    """
    home = tmp_path / "home"
    bindir = home / ".hermes" / "hermes-agent" / "venv" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "cron_stub.log"
    stub = bindir / "hermes"
    stub.write_text('#!/usr/bin/env bash\n'
                    f'printf \'%s\\n\' "$*" >> "{log}"\n'
                    'exit 0\n')
    stub.chmod(0o755)
    fake_py = bindir / "python"
    fake_py.write_text("#!/bin/sh\nexit 0\n")
    fake_py.chmod(0o755)
    return home, log


def _install(tmp_path, *args, hermes_home=None):
    home, log = _stub_hermes(tmp_path)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("HERMES_HOME", None)
    if hermes_home is not None:
        env["HERMES_HOME"] = str(hermes_home)
    # A PATH `hermes` must not shadow the stub (the installer falls back to it).
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    env.pop("HSCC_ALONE_SWEEP_OUT", None)
    proc = _run(["bash", INSTALLER, *args], env=env, cwd=str(tmp_path), timeout=60)
    return proc, (log.read_text() if log.exists() else ""), home


def test_installer_measures_committed_code_from_the_checkout_it_ran_in(tmp_path):
    proc, log, home = _install(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "cron create" in log and "--name hscc-alone-sweep-gate" in log
    assert "--script hscc-alone-sweep-gate.sh" in log
    assert log.index("cron remove hscc-alone-sweep-gate") < log.index("cron create"), \
        "remove-before-create is what keeps re-install idempotent"
    wrapper = tmp_path / "home" / ".hermes" / "scripts" / "hscc-alone-sweep-gate.sh"
    assert wrapper.is_file(), "the wrapper must land in the bound home's scripts/"
    text = wrapper.read_text()
    assert f'HSCC_ALONE_SWEEP_REPO="{REPO}"' in text, (
        "the nightly must run the gate from the checkout, not from a stale copy")
    # Hermes may run a --no-agent job under a bound home that is NOT the
    # operator's shell HOME, so everything the gate resolves from $HOME has to be
    # resolved at install time instead (test_hermes_python.py is the same trap on
    # the resolver side).
    assert f'HSCC_ALONE_SWEEP_PYTHONS="{home}/.hermes/hermes-agent/venv/bin/python"' \
        in text, f"the interpreter list is not baked from the installing shell:\n{text}"
    assert 'export HSCC_ALONE_SWEEP_OUT="' in text, \
        "the report root is not baked — the job and --status could disagree on where " \
        "the verdict lives"
    exec_line = [l for l in text.splitlines() if l.startswith("exec ")]
    assert exec_line, "the wrapper must exec the gate, not copy it"
    parts = shlex.split(exec_line[0])
    assert parts[0] == "exec" and os.path.isfile(parts[1]), \
        f"the wrapper execs a path that does not exist: {exec_line}"


def test_gate_honours_the_baked_interpreter_list(tmp_path):
    """The bake is only real if the gate reads it: a sandboxed HOME has neither
    default interpreter, so the ONLY way this preflight can pass is the env list.
    """
    ok = _gate("--preflight", out=tmp_path, python=False,
               env={"HSCC_ALONE_SWEEP_PYTHONS": sys.executable})
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "interpreter present" in ok.stdout
    bogus = _gate("--preflight", out=tmp_path / "b", python=False,
                  env={"HSCC_ALONE_SWEEP_PYTHONS": "/nope/python"})
    assert bogus.returncode == 2, bogus.stdout + bogus.stderr


def test_installer_refuses_when_it_can_see_no_interpreter(tmp_path):
    """Installing a job whose interpreters do not exist would produce a nightly
    that exits 2 forever — the same class as the 17 failed runs this installer's
    header documents. Refuse at install, in the operator's own shell, where the
    reason is legible.
    """
    home, log = _stub_hermes(tmp_path)
    (home / ".hermes" / "hermes-agent" / "venv" / "bin" / "python").unlink()
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    env.pop("HERMES_HOME", None)
    env["HSCC_ALONE_SWEEP_PYTHONS"] = str(tmp_path / "absent-python")
    proc = _run(["bash", INSTALLER], env=env, cwd=str(tmp_path), timeout=60)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "no stamp interpreter" in (proc.stdout + proc.stderr)
    assert "cron create" not in (log.read_text() if log.exists() else "")
    assert not (home / ".hermes" / "scripts" / "hscc-alone-sweep-gate.sh").exists()


def test_installer_binds_the_wrapper_to_the_same_home_cron_binds(tmp_path):
    """The t_3aadd2c0 drift rule: `$HERMES_HOME/scripts` is the ONLY place Hermes
    finds a bare `--script NAME`, and profiles never share files."""
    profile = tmp_path / "profile"
    proc, log, home = _install(tmp_path, hermes_home=profile)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (profile / "scripts" / "hscc-alone-sweep-gate.sh").is_file()
    assert not (home / ".hermes" / "scripts" / "hscc-alone-sweep-gate.sh").exists(), \
        "a profile install must not leave the wrapper in the shared home"


def test_installer_refuses_a_tree_it_cannot_measure(tmp_path):
    """A job that can only fail at 04:30 is the failure this installer documents.

    The installer resolves the repo from its own path, so copying it alone into a
    tree with no gate next to it reproduces "checkout that cannot measure" — and
    it must say so BEFORE registering anything (a registered job nobody notices
    failing is the 17-consecutive-failures incident, t_3aadd2c0).
    """
    lonely = tmp_path / "lonely" / "scripts"
    lonely.mkdir(parents=True)
    shutil.copy2(INSTALLER, str(lonely / "install_alone_sweep_gate.sh"))
    home, log = _stub_hermes(tmp_path)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    env.pop("HERMES_HOME", None)
    env.pop("HSCC_ALONE_SWEEP_OUT", None)
    proc = _run(["bash", str(lonely / "install_alone_sweep_gate.sh")],
                env=env, cwd=str(tmp_path), timeout=60)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "no gate at" in (proc.stdout + proc.stderr)
    assert "cron create" not in (log.read_text() if log.exists() else ""), \
        "a tree it cannot measure must not end up with a registered job"


def test_status_says_so_when_no_pass_has_ever_run(tmp_path):
    """'Never ran' must never render as a pass."""
    proc, _log, _home = _install(tmp_path)
    assert proc.returncode == 0
    status, _l, _h = _install(tmp_path, "--status")
    assert status.returncode == 0, status.stdout + status.stderr
    assert "NONE on record" in status.stdout


def test_status_works_on_a_host_with_nothing_installed(tmp_path):
    """The first thing an operator runs is also the one with nothing to read.

    Measured, not hypothetical: the first version of this path read the baked
    paths out of the wrapper with a command substitution under `set -euo pipefail`,
    so on a host where the wrapper did not exist `sed` failed inside the
    substitution, the assignment inherited rc 1, and --status died printing three
    lines and none of the answer. A missing wrapper is a STATUS LINE.
    """
    home, _log = _stub_hermes(tmp_path)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    env.pop("HERMES_HOME", None)
    env.pop("HSCC_ALONE_SWEEP_OUT", None)
    status = _run(["bash", INSTALLER, "--status"], env=env,
                  cwd=str(tmp_path), timeout=60)
    assert status.returncode == 0, status.stdout + status.stderr
    for line in ("NOT REGISTERED", "MISSING", "NONE on record"):
        assert line in status.stdout, f"--status lost '{line}':\n{status.stdout}"


def test_uninstall_removes_both_the_job_and_the_wrapper(tmp_path):
    proc, _log, home = _install(tmp_path)
    assert proc.returncode == 0
    proc, log, home = _install(tmp_path, "--uninstall")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "cron remove hscc-alone-sweep-gate" in log
    assert not (home / ".hermes" / "scripts" / "hscc-alone-sweep-gate.sh").exists()


def test_schedule_is_nightly_and_not_a_second_of_the_suite():
    """The routing decision, pinned: this job is scheduled, so a wrong cadence is
    a wrong gate. Daily, and overridable — never per-PR (see the gate header)."""
    text = _read(INSTALLER)
    m = re.search(r'SCHEDULE="\$\{HSCC_ALONE_SWEEP_SCHEDULE:-(.*?)}"', text)
    assert m, "the installer no longer has a single overridable default schedule"
    assert m.group(1).count(" ") == 4, f"'{m.group(1)}' is not a cron spec"
    assert "*/" not in m.group(1), (
        "a sub-daily cadence would put ~25 min of pytest back on a hot path and "
        "risk overlapping passes")
