"""A red leg must name its failing tests: the pytest invocation pins `-rf` (t_d41c1668).

THE DEFECT. The runner invoked pytest as `-q -rs`. `-rs` was added deliberately —
it prints every skip REASON instead of a bare count (GAP-D-1, t_b82ad0fe; see the
comment above the serial invocation) — but no matching `-rf` ever went in, so the
`FAILED <node id>` lines pytest would otherwise print in its short summary were
suppressed. A red leg therefore handed the reader a dot band and a `FAILURES`
section and left them to RECONSTRUCT which node ids failed. That cost is measured,
not assumed: t_00d9581d reconstructs failing node ids from the dot stream three
separate times in one card, and its mutation battery had to add `-rf` to its OWN
pytest command while recording that the runner's `-rs` "suppresses `FAILED` lines
(known trap)".

WHY THE PIN IS ON ARGV AND NOT ON SCRIPT TEXT. A substring assertion on
`run_tests.sh` would stay green while the flag was unreachable (moved into a
branch that never runs, or shadowed by a later flag), and would go red on a
comment reword. So every test here runs the SHIPPED runner against the stub
interpreter seam the runner already offers (`HSCC_TEST_PY`) and asserts on the
argv the stub was ACTUALLY handed, one argument per line. That is the same seam
`test_run_tests_leg_parallelism.py` uses, and the same reason it exists: the
thing under test is what pytest receives, not what the file says.

Both call sites are pinned, because they are one promise. The parallel pack has
its own copy of the command and its comment says the copy "MUST be the serial
branch's command, flag for flag" — a `-rf` that lands on one branch and not the
other means a packed gate hides failing test names that a serial gate shows.
"""

import os
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
RUNNER = SCRIPTS / "run_tests.sh"

#: A real leg name. The runner rejects an unknown leg with exit 64 by design, so
#: a made-up name would exercise the rejection path instead of the invocation.
LEG = "hscc-commands"

#: The reporting flags, in the order the runner builds them. `-rs` (skip reasons)
#: and `-rf` (FAILED node ids) are a pair: one makes a skipped leg explain
#: itself, the other makes a red leg name its tests.
REPORTING_FLAGS = ("-rs", "-rf")

#: Sentinel between pytest invocations in the argv dump, so one run that starts
#: several legs still yields one argv LIST per invocation.
END = "\x1eEND"

#: Written to a tmp dir and pointed at by HSCC_TEST_PY. Records the argv it is
#: handed ONE ARGUMENT PER LINE — not a joined string, where `-m` + `not
#: loadstress` would be indistinguishable from a single `-m not loadstress`
#: token — then prints the shape a real failing leg prints and exits non-zero.
#: The leg name is derived from the `<dir>/tests` argument, exactly as
#: test_run_tests_leg_parallelism.py's stub derives it.
STUB = """#!/usr/bin/env bash
set -u
tests_arg=""
for a in "$@"; do case "$a" in */tests) tests_arg="$a" ;; esac; done
leg="(none)"
[ -n "$tests_arg" ] && leg="$(basename "$(dirname "$tests_arg")")"
for a in "$@"; do printf '%s\\n' "$a" >> "$STUB_ARGV"; done
printf '%s\\n' "__END__" >> "$STUB_ARGV"
echo "F"
echo "FAILED $leg/tests/test_stub.py::test_${leg//-/_}_breaks - assert 1 == 2"
echo "1 failed in 0.1s"
exit 1
"""


def _run(tmp_path, *, parallel=None, extra_argv=()):
    """Execute the shipped runner against the stub interpreter.

    Returns ``(invocations, log, rc)`` where ``invocations`` is one argv LIST per
    pytest process the runner actually started, in the order it started them.
    Output goes to a file rather than a pipe, for the reason recorded in
    test_run_tests_leg_parallelism.py: a watcher that outlives a leg holds a
    captured pipe's write end open and the harness hangs.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    stub = tmp_path / "stub_py.sh"
    stub.write_text(STUB)
    stub.chmod(0o755)
    argv_file = tmp_path / "argv.txt"
    log = tmp_path / "run.log"
    argv_file.write_text("")

    env = dict(os.environ)
    env.update({
        "HSCC_TEST_PY": str(stub),
        "STUB_ARGV": str(argv_file),
        "HSCC_LEG_HEARTBEAT_SECONDS": "0",
        "HSCC_TEST_LEGS": LEG,
    })
    if parallel is None:
        env.pop("HSCC_TEST_PARALLEL", None)
    else:
        env["HSCC_TEST_PARALLEL"] = str(parallel)

    with open(log, "wb") as fh:
        proc = subprocess.run(["bash", str(RUNNER), *extra_argv],
                              stdout=fh, stderr=subprocess.STDOUT,
                              env=env, timeout=180, cwd=str(REPO))

    invocations, cur = [], []
    for line in argv_file.read_text().splitlines():
        if line == "__END__":
            invocations.append(cur)
            cur = []
        else:
            cur.append(line)
    assert invocations, (
        f"the stub pytest never ran (runner rc={proc.returncode})\n--- log ---\n{log.read_text()}"
    )
    return invocations, log.read_text(), proc.returncode


# ── the flag set, pinned on the argv pytest actually receives ───────────────

def test_the_serial_leg_hands_pytest_every_reporting_flag(tmp_path):
    """`-rs` AND `-rf` must reach pytest, or a red leg names nothing.

    A future edit that drops `-rf` fails here, and the failure message prints the
    argv the runner actually built.
    """
    invocations, _, _ = _run(tmp_path)
    argv = invocations[0]
    for flag in REPORTING_FLAGS:
        why = ("a red leg prints no FAILED node ids" if flag == "-rf"
               else "a skipped leg prints no skip reasons")
        assert flag in argv, f"{flag} missing from the leg's pytest argv {argv!r} — without it, {why}"


def test_the_pinned_argv_is_exactly_what_the_runner_builds(tmp_path):
    """The whole argument set, so no flag can quietly disappear or move.

    Exact equality is the point: `-q -rs` alone — the shipped shape before
    t_d41c1668 — fails this, and so does any flag dropped from it later.
    """
    invocations, _, _ = _run(tmp_path)
    assert invocations[0] == [
        "-m", "pytest",
        "-q",
        "-rs", "-rf",
        str(REPO / LEG / "tests"),
        "-p", "no:cacheprovider",
        "-m", "not loadstress",
    ], "the leg's pytest argv drifted from the pinned set"


def test_the_reporting_flags_survive_operator_argv(tmp_path):
    """Forwarded operator args must not displace the pinned flags.

    The operator's own args come last and therefore win — the documented posture
    — but the pinned reporting flags have to still be in front of them.
    """
    invocations, _, _ = _run(tmp_path, extra_argv=("-k", "not slow"))
    argv = invocations[0]
    for flag in REPORTING_FLAGS:
        assert flag in argv, f"{flag} lost when operator argv is forwarded: {argv!r}"
    assert argv[-2:] == ["-k", "not slow"], f"operator argv must stay last: {argv!r}"


# ── the two call sites are one promise ──────────────────────────────────────

def test_both_call_sites_build_the_same_argv(tmp_path):
    """Serial and packed legs hand pytest the identical flag set.

    The parallel branch carries its own copy of the command. If `-rf` lands on
    one and not the other, a packed gate hides failing test names that a serial
    gate shows — precisely the drift the comment at that call site forbids.
    """
    serial, _, _ = _run(tmp_path / "serial")
    packed, _, _ = _run(tmp_path / "pack", parallel=2)
    assert packed[0] == serial[0], (
        f"argv drift between the two pytest call sites\n"
        f"  serial: {serial[0]}\n  pack:   {packed[0]}"
    )


def test_the_pack_leg_also_carries_every_reporting_flag(tmp_path):
    """Pin the packed branch directly, not only by equality with serial."""
    invocations, _, _ = _run(tmp_path, parallel=2)
    for flag in REPORTING_FLAGS:
        assert flag in invocations[0], f"{flag} missing from the PACKED leg's argv {invocations[0]!r}"


# ── the user-visible consequence, end to end ────────────────────────────────

def test_a_red_legs_output_names_the_failing_node_id(tmp_path):
    """The point of the flag: a reader of the leg log sees `FAILED <node id>`.

    The acceptance evidence in test form — a stub pytest that fails, run through
    the shipped runner, whose log must name the node id. Before `-rf`, pytest
    suppressed exactly these lines and the log carried only the dot band.
    """
    invocations, log, rc = _run(tmp_path)
    assert rc != 0, "the stub leg was supposed to fail"
    assert "-rf" in invocations[0], f"the leg ran without -rf: {invocations[0]!r}"
    assert "FAILED " in log, (
        "a red leg printed no FAILED node id — the reader has to reconstruct "
        f"which tests failed from the dot band\n--- leg log ---\n{log}"
    )
