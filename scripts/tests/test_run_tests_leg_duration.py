"""The per-leg duration on scripts/run_tests.sh's summary rows (t_fdc2bff0).

WHY THIS IS PINNED AT ALL. The runner has always started a per-leg clock
(`LEG_T0`) for the heartbeat and then printed only `✓ <leg>`. That single missing
number is why the merge queue's local gate could only ever be discussed as one
~75 min block: nothing it printed said which leg cost what, and the answer had
to be rebuilt by hand from twelve `HSCC_TEST_LEGS=<leg>` invocations
(docs/audits/merge-queue-gate-leg-timings_t_fdc2bff0.md). A number that no
machine reads is a number that disappears the first time someone tidies the
summary, so it gets a guard.

Two things the rows have to keep doing, each pinned below:

  1. carry an integer wall time on EVERY leg — green ones included, because the
     green legs are what a cost breakdown is made of, and a failure digest never
     mentions them;
  2. keep the red row's shape `merge_queue._LEG_FAILURE_RES` matches (`^✗\\s` at
     the start of the line), so widening the parenthetical cannot silently drop
     the leg from a gate handback's digest.

Style note: like test_run_tests_leg_selection.py and test_run_tests_harness_env.py,
this runs the SHIPPED text (extracted verbatim) rather than a copy of it, so the
guard cannot drift from the runner. The pytest the legs "run" is a stub: the
claim is about the runner's reporting, not about any suite.
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
RUNNER = SCRIPTS / "run_tests.sh"

#: The serial leg loop and the summary block that reports it. The loop is
#: anchored on its unique header and closed at its own top-level `done`; the
#: summary is taken from its banner to EOF, so the rows and the exit line are
#: part of what gets executed rather than re-typed here.
#:
#: WHY TWO PIECES. `run_tests.sh` grew a second, opt-in leg loop
#: (`HSCC_TEST_PARALLEL`, t_6c2123f9) that lives in the `else` of
#: `if [ "$PARALLEL_MODE" -eq 0 ]`; taking the file to EOF from the serial
#: `for` would swallow that `else` and produce an unbalanced script. The serial
#: loop still ends at its own `done`, so the guard executes exactly the shipped
#: serial branch plus the shipped summary it feeds.
_LOOP = re.compile(r'(?ms)^for d in "\$\{LEGS\[@\]\}"; do$.*?^done$')
_SUMMARY = re.compile(r'(?ms)^echo "━━━ Summary ━━━"$.*\Z')

GREEN_ROW = re.compile(r"^  ✓ (?P<leg>\S+) \((?P<secs>\d+)s\)$")
RED_ROW = re.compile(
    r"^  ✗ (?P<leg>\S+) \(pytest exit (?P<code>\d+), (?P<secs>\d+)s\)$")


def _shipped_loop() -> str:
    text = RUNNER.read_text(encoding="utf-8")
    loop = _LOOP.search(text)
    summary = _SUMMARY.search(text)
    if loop is None or summary is None:
        raise AssertionError(
            "scripts/run_tests.sh no longer has the serial leg loop and summary "
            "block this guard parses — re-read the runner before believing "
            "anything about its summary rows"
        )
    return loop.group(0) + "\n" + summary.group(0)


def _run_legs(tmp_path: Path, legs, fail=()) -> subprocess.CompletedProcess:
    """Execute the shipped loop against a stub roster and a stub pytest.

    ``fail`` names the legs whose stub pytest exits 3; the rest exit 0. Mixing
    them in one run is the point — a breakdown is only useful if a red leg does
    not take the green legs' numbers down with it.
    """
    arms = "".join(
        f'  *" {tmp_path}/{leg}/tests "*) exit 3;;\n' for leg in fail)
    stub = tmp_path / "stub_py.sh"
    stub.write_text(
        "#!/bin/bash\n"
        'echo "invoked: $*" >> "$STUB_LOG"\n'
        "case \" $* \" in\n"
        f"{arms}"
        "esac\n"
        "exit 0\n",
        encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "invocations.log"
    log.write_text("", encoding="utf-8")

    script = tmp_path / "loop.sh"
    script.write_text(
        "set -u\n"
        f'ROOT="{tmp_path}"\n'
        f'PY="{stub}"\n'
        # `env -u NAME` is legal for a name that is not set, and the loop expands
        # this array unconditionally — bash 3.2 refuses an empty one under set -u.
        "ENV_UNSET_ARGS=(-u HSCC_TEST_STUB_PIN)\n"
        "HEARTBEAT_SECONDS=0\n"
        'CURRENT_SUITE=""\n'
        "rc=0\n"
        "declare -a summary\n"
        f'LEGS=({" ".join(legs)})\n'
        + _shipped_loop(),
        encoding="utf-8")

    return subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, timeout=120,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "STUB_LOG": str(log),
             "HOME": str(tmp_path)},
        cwd=str(tmp_path))


def _rows(proc) -> list[str]:
    body = proc.stdout.split("━━━ Summary ━━━", 1)
    assert len(body) == 2, f"no summary block in:\n{proc.stdout}\n{proc.stderr}"
    return [ln for ln in body[1].splitlines() if ln.startswith("  ")]


def test_every_green_leg_row_carries_its_wall_time(tmp_path):
    legs = ("alpha", "beta", "gamma")
    proc = _run_legs(tmp_path, legs, fail=())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rows = _rows(proc)
    assert len(rows) == len(legs), rows
    for leg, row in zip(legs, rows):
        m = GREEN_ROW.match(row)
        assert m, f"green row lost its duration or its shape: {row!r}"
        assert m.group("leg") == leg, row
        # Not merely non-empty: a real integer, so a dropped `$(( ))` that leaves
        # `()` or `(s)` cannot pass as "has a duration".
        assert m.group("secs").isdigit() and int(m.group("secs")) >= 0, row
    assert "ALL GREEN" in proc.stdout


def test_a_red_leg_row_keeps_its_cost_and_the_digest_anchor(tmp_path):
    proc = _run_legs(tmp_path, ("alpha", "beta"), fail=("alpha",))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    rows = _rows(proc)
    m = RED_ROW.match(rows[0])
    assert m, f"red row no longer names the exit code and the wall time: {rows[0]!r}"
    assert m.group("leg") == "alpha" and m.group("code") == "3", rows[0]
    assert m.group("secs").isdigit(), rows[0]
    # The digest anchor merge_queue actually matches: a leading `✗ ` at line start.
    assert re.match(r"^✗\s", rows[0].strip()), rows[0]
    # The green neighbour is still reported with its cost, so a breakdown survives
    # a red run rather than only a green one.
    assert GREEN_ROW.match(rows[1]), rows[1]
    assert "FAILURES ABOVE" in proc.stdout


def test_the_clock_is_the_legs_own_and_not_a_constant(tmp_path):
    """Two legs of ~1 s each must not report the same second they started at.

    Guards the one thing a rewrite gets wrong: printing the runner's total
    `${SECONDS}` (or reusing one captured start) instead of the leg's own clock.
    """
    stub = tmp_path / "slow_py.sh"
    stub.write_text("#!/bin/bash\n" 'echo "invoked: $*" >> "$STUB_LOG"\n'
                    "sleep 1\n", encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "invocations.log"
    log.write_text("", encoding="utf-8")
    script = tmp_path / "loop.sh"
    script.write_text(
        "set -u\n"
        f'ROOT="{tmp_path}"\n'
        f'PY="{stub}"\n'
        "ENV_UNSET_ARGS=(-u HSCC_TEST_STUB_PIN)\n"
        "HEARTBEAT_SECONDS=0\n"
        'CURRENT_SUITE=""\n'
        "rc=0\n"
        "declare -a summary\n"
        "LEGS=(alpha beta)\n"
        + _shipped_loop(),
        encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, timeout=120,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "STUB_LOG": str(log),
             "HOME": str(tmp_path)},
        cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    secs = [int(GREEN_ROW.match(r).group("secs")) for r in _rows(proc)]
    assert len(secs) == 2, secs
    # Two legs that each slept a second must report at least one second between
    # them. NOT `each >= 1`: the clock is a whole-second difference, so one leg
    # straddling a second boundary legitimately measures 0 — asserting otherwise
    # is a boundary flake, which is how this case first failed.
    assert sum(secs) >= 1, secs
    # And each leg was really run: the stub was invoked once per leg, by name.
    invoked = (tmp_path / "invocations.log").read_text(encoding="utf-8")
    assert invoked.count("invoked:") == 2, invoked
    assert all(leg in invoked for leg in ("alpha", "beta")), invoked
