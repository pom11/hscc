"""The ``loadstress`` leg must not be able to rot (t_b0bcc49c).

The class this leg exists for is "green locally, red in the gate": cases that
pass on an idle box and fail on a loaded one. A stress leg fixes that only while
it still selects the cases it was written for, still deselects them from the
ordinary legs, and still cannot leave its burners behind. Every one of those is
a property a future edit can quietly undo, and every arm here is the guard that
makes undoing it a red test in an ordinary leg rather than a surprise in a queue
gate:

  1. the marker is REGISTERED (an unregistered marker is a warning today and an
     error tomorrow, and a marker that only warns deselects nothing silently);
  2. both affected files carry it, and the leg names exactly those files;
  3. the selection is NON-EMPTY and consists only of cases in those files;
  4. the ordinary legs' `-m` is the exact complement of the stress leg's;
  5. `loadstress` is selectable and is NOT in the default roster;
  6. an empty selection is an ERROR (exit 2), never a green run;
  7. the leg's own survivor detector bites on a live burner;
  8. a signal to the leg takes the burners with it (the t_71ed3b1c property,
     asserted at the leg's boundary rather than the harness's);
  9. no burner loop lives in the non-test scripts this file's sibling audit
     (`test_load_burners.py`, which sweeps `tests/` dirs only) cannot see.

Nothing here runs the stress pass itself — this file guards the seam, and the
measured load numbers live in docs/testing/loadstress-leg.md.
"""

import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

import loadstress_leg as leg

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "scripts" / "run_tests.sh"
INI = REPO / "pytest.ini"

#: The two files the flaky-teardown class lives in. Hard-coded HERE as well as
#: in the leg so a rewrite of either list has to be a deliberate edit in two
#: places that disagree, rather than one silent one.
AFFECTED = ("hscc-api/tests/test_api_server_stop_signal.py",
            "hscc_daemon/tests/test_api_cli_stop_sidecar.py")


def _sh(*args, env_extra=None, cwd=REPO):
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(args, capture_output=True, text=True, cwd=cwd,
                          env=env)


# ── 1: registered, not implied ──────────────────────────────────────────────


def _registered_markers() -> set[str]:
    """Names from pytest.ini's ``markers =`` list section.

    Parsed rather than searched for: `-m "not loadstress"` works whether or not
    the marker is registered, so "the string is in the file" proves nothing about
    the thing this arm is for. A list item is an indented line after `markers`.
    """
    names: set[str] = set()
    inside = False
    for line in INI.read_text(encoding="utf-8").splitlines():
        if not line[:1].isspace():
            inside = line.strip().startswith("markers")
            continue
        if inside and line.strip():
            names.add(line.strip().split(":", 1)[0].strip())
    return names


def test_the_marker_is_registered_in_pytest_ini():
    """An unregistered marker is a warning today and an error tomorrow.

    It is also softer than that: a marker that only warns still marks, and
    `-m "not loadstress"` still deselects — until the day pytest makes unknown
    marks fatal and the ordinary legs stop deselecting anything, at which point
    the stress cases silently rejoin every gate. Registration is the pin.
    """
    markers = _registered_markers()
    assert leg.MARKER in markers, (
        f"{leg.MARKER!r} is applied to cases but not registered in pytest.ini "
        f"(registered: {sorted(markers)})")
    # Non-vacuity: the parser must not be matching the whole file.
    assert "definitely-not-a-registered-marker" not in markers


# ── 2 & 3: the selection is the marked cases of exactly those two files ─────


def test_the_leg_names_exactly_the_two_affected_files():
    assert tuple(leg.FILES) == AFFECTED, (
        f"the stress leg runs {leg.FILES}; the class this card is about is "
        f"{AFFECTED}. If a third file joined the class, change both lists.")


def test_both_files_actually_carry_the_marker():
    for rel in AFFECTED:
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "@pytest.mark.loadstress" in text, (
            f"{rel} no longer marks anything: the stress leg would run zero of "
            f"its cases and the ordinary legs would run them unloaded")


def test_the_selection_is_non_empty_and_scoped_to_those_files():
    """The measured N, from the leg's own probe — not from a remembered count."""
    n, ids, _ = leg.collect([])
    assert n > 0, "the leg selects nothing; it must exit 2, never pass"
    assert len(ids) == n, f"reported {n} items, listed {len(ids)}"
    for node in ids:
        assert node.startswith(tuple(AFFECTED)), (
            f"{node} is selected by the stress leg but is not in the two "
            f"affected files — the marker leaked outside the class")


@pytest.mark.parametrize("rel", AFFECTED)
def test_each_file_partitions_cleanly_between_the_two_legs(rel):
    """marked + unmarked == the file, measured per file and per direction.

    This is the arm that catches the quiet failure of the whole design: a case
    that ends up in NEITHER leg (marked, then the stress leg stops selecting it)
    runs nowhere at all. Asserting only the stress side would let it go green.
    """
    def count(*extra: str) -> int:
        proc = _sh(sys.executable, "-m", "pytest", "-q", "--collect-only",
                   "-p", "no:cacheprovider", *extra)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        hit = re.search(r"^(\d+)(?:/\d+)? tests? collected", proc.stdout,
                        re.MULTILINE)
        assert hit, f"no item count for {rel}: {proc.stdout[-400:]}"
        return int(hit.group(1))

    whole = count(rel)
    marked = count("-m", leg.MARKER, rel)
    unmarked = count("-m", f"not {leg.MARKER}", rel)
    assert marked >= 1, f"{rel} contributes nothing to the stress leg"
    assert marked < whole, (
        f"{rel} is entirely marked: the ordinary leg would run none of it, "
        f"including the static source pins that need no load at all")
    assert marked + unmarked == whole, (
        f"{rel}: {marked} marked + {unmarked} unmarked != {whole} collected — "
        f"a case fell between the two legs and now runs nowhere")


#: Measured at the head that introduced the marker: 11 of 20 items in the api
#: file, 9 of 16 in the daemon one. Pinned because "the file still has a mark on
#: it" is not a guard — a case un-marked one at a time walks back into the
#: ordinary legs, where it passes on an idle box and the class this leg exists for
#: comes back. Un-marking a case is a legitimate edit; doing it silently is not.
EXPECTED_MARKED = {
    "hscc-api/tests/test_api_server_stop_signal.py": 11,
    "hscc_daemon/tests/test_api_cli_stop_sidecar.py": 9,
}


def test_the_marked_counts_are_pinned_to_the_measured_numbers():
    """The stress leg's N is a measured number here too, not a vibe."""
    n, ids, _ = leg.collect([])
    assert n == sum(EXPECTED_MARKED.values()), (
        f"the stress leg selects {n} items; it was measured at "
        f"{sum(EXPECTED_MARKED.values())}. If a case genuinely left the class, "
        f"say so by updating this dict and the doc table together")
    for rel, expected in EXPECTED_MARKED.items():
        got = sum(1 for node in ids if node.startswith(rel))
        assert got == expected, (
            f"{rel}: {got} marked, measured {expected} — a load-sensitive case "
            f"was un-marked and now runs only unloaded")
    del ids


# ── 4: the ordinary legs' deselect is the complement ────────────────────────


def test_the_ordinary_legs_deselect_exactly_what_the_stress_leg_selects():
    runner = RUNNER.read_text(encoding="utf-8")
    expected_deselect = f'-m "not {leg.MARKER}"'
    assert expected_deselect in runner, (
        f"scripts/run_tests.sh no longer carries {expected_deselect!r} on its "
        f"pytest line — the ordinary legs would pay for the stress cases, or "
        f"the stress marker would be decoration")
    assert leg.MARKER == "loadstress", (
        f"the marker name changed to {leg.MARKER!r}; update the runner's "
        f"deselect and this arm together")


# ── 5: selectable, never default ────────────────────────────────────────────


def _dirs_array() -> list[str]:
    text = RUNNER.read_text(encoding="utf-8")
    start = text.index("DIRS=(") + len("DIRS=(")
    return text[start:text.index(")", start)].split()


def test_loadstress_is_not_part_of_the_default_roster():
    """`HSCC_TEST_LEGS` unset must never pay for a stress run."""
    assert leg.LEG_NAME not in _dirs_array(), (
        "loadstress is in DIRS, which is the default roster — every full-suite "
        "gate and every queue gate would now pay for the repeats")


def test_the_runner_accepts_the_leg_and_reports_a_real_selection():
    """End-to-end through the canonical runner, without running the passes.

    `HSCC_TEST_PY` is pinned to this test's own interpreter on purpose. The
    runner resolves its default as `$HOME/.hermes/…/python`, and when the
    `scripts` leg runs under the roster-wide HOME seal that path is inside an
    empty tmp home — so a nested runner left to its own default falls back to a
    bare `python3` and dies on "No module named pytest". That is a property of
    the sealed posture, not of the leg, and this arm is exactly where it would
    otherwise be discovered: red in the gate, green on a bare shell.
    """
    proc = _sh("bash", str(RUNNER), "--collect-only",
               env_extra={"HSCC_TEST_LEGS": leg.LEG_NAME,
                          "HSCC_LEG_HEARTBEAT_SECONDS": "0",
                          "HSCC_TEST_PY": sys.executable})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "✓ loadstress" in out and "ALL GREEN" in out, out
    selected = re.search(r"(\d+) items selected", out)
    assert selected and int(selected.group(1)) > 0, (
        f"the leg ran through the runner and selected nothing: {out}")
    assert "not the full suite" in out.lower() or "SELECTED" in out, out


def test_an_unknown_leg_is_still_rejected_and_names_the_stress_leg():
    proc = _sh("bash", str(RUNNER), env_extra={"HSCC_TEST_LEGS": "loadstres"})
    assert proc.returncode == 64, proc.stdout + proc.stderr
    assert "loadstress" in proc.stderr, (
        "the roster a typo is checked against must include the selectable legs")


# ── 6: an empty selection is an error ───────────────────────────────────────


def test_a_selection_of_nothing_exits_2_and_says_why():
    """The green lie this leg exists to refuse.

    Mutating the `if n == 0` guard to `return 0` reddens this arm: that is the
    whole difference between a stress leg and a leg that stresses nothing.
    """
    proc = _sh(sys.executable, str(REPO / "scripts" / "loadstress_leg.py"),
               "--collect-only",
               env_extra={"HSCC_LOADSTRESS_MARKER": "no-such-marker-anywhere"})
    assert proc.returncode == 2, (
        f"a leg that selects nothing must not report success: rc="
        f"{proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    assert "ZERO items" in proc.stderr, proc.stderr


# ── 7 & 8: the burners cannot outlive the leg ───────────────────────────────


def _wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_the_survivor_detector_finds_and_takes_a_live_burner():
    """Non-vacuity first: the tag must be findable before it is reaped."""
    tag = f"hscc-loadstress-guard-{uuid.uuid4().hex}"
    harness = REPO / "scripts" / "load_burners.sh"
    env = {**os.environ, "HSCC_LOAD_BURNER_TAG": tag}
    proc = subprocess.Popen(
        ["/bin/sh", str(harness), "1", "60", "--", "sleep", "60"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        start_new_session=True)
    try:
        assert _wait_for(lambda: leg.survivors(tag)), (
            "the harness started but no tagged burner was findable — the "
            "detector would pass on nothing")
        assert leg.sweep(tag) >= 1, "sweep() reported killing nothing it could see"
        assert leg.wait_until_gone(tag, timeout=15) == [], (
            f"tagged burner survived the leg's own reaper: {leg.survivors(tag)}")
    finally:
        for pid in leg.survivors(tag):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=10)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_to_the_leg_takes_the_burners_with_it(sig):
    """The t_71ed3b1c property, asserted at the LEG's boundary.

    `test_load_burners.py` proves the harness cannot be SIGTERMed into an orphan.
    This is the step above it: signal the leg, and no burner may survive it.

    Stated honestly, because a guard that over-claims is worse than no guard:
    this arm passes WITH and WITHOUT the leg's own `_forwarded_stop`. When the leg
    dies, the pipe the harness writes into closes, and the harness takes SIGPIPE
    on its next write, dies through its own trap, and kills the burners. So the
    arm pins the property the card asked for — nothing outlives the leg — and NOT
    the forwarding, which earns its keep in the shape this arm cannot reach: an
    operator running the leg from a terminal, where the harness writes to a tty
    and no pipe ever closes. A sharper arm would have to keep the harness's
    output open (a file, or a status marker the leg writes when it forwards).
    """
    tag = f"hscc-loadstress-sig-{uuid.uuid4().hex}"
    env = {**os.environ, "HSCC_LOAD_BURNER_TAG": tag}
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "scripts" / "loadstress_leg.py"),
         "--burners", "2", "--repeats", "1", "--timeout", "120"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        cwd=REPO, start_new_session=True)
    try:
        assert _wait_for(lambda: leg.survivors(tag), timeout=30), (
            "the leg never started a tagged burner — nothing was proved about "
            "what happens to it")
        proc.send_signal(sig)
        rc = proc.wait(timeout=30)
        assert rc in (-sig, 128 + sig), (
            f"the leg exited {rc} after {sig.name}; a leg that swallows the "
            f"signal is a leg whose caller cannot trust its status")
        assert _wait_for(lambda: not leg.survivors(tag), timeout=20), (
            f"{sig.name} to the leg left burners {leg.survivors(tag)} alive — "
            f"the exact orphan shape t_71ed3b1c exists to prevent")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        for pid in leg.survivors(tag):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


# ── 9: the audit's blind spot ───────────────────────────────────────────────

_BURNER_LOOP = re.compile(r"while\s+(?::|true)\s*;(\s*)do\s*:\s*;?\s*done")


def test_no_non_test_script_inlines_a_burner_loop():
    """`test_load_burners.py` sweeps `tests/` dirs; the leg scripts are not in one.

    The stress leg and the runner are exactly where someone under time pressure
    would paste a spinner, and they are the two files that audit cannot see.
    Positive control first, or this is a pass that proves nothing.

    One exemption, and only this one: `load_burners.sh` itself. It is the
    sanctioned home for the loop — the file every other script is told to call —
    and its own suite (test_load_burners.py) is what owns the loop's group, trap
    and timeout. Any second file matching is not exempt, including the ones that
    would rather quote the incident than describe it.
    """
    assert _BURNER_LOOP.search("while " + ":; do :; done")
    assert _BURNER_LOOP.search("while true; do :; done")
    sanctioned = {"load_burners.sh"}
    offenders = []
    for path in sorted((REPO / "scripts").glob("*.py")) + sorted(
            (REPO / "scripts").glob("*.sh")):
        if path.name in sanctioned:
            continue
        if _BURNER_LOOP.search(path.read_text(errors="replace")):
            offenders.append(path.name)
    assert not offenders, (
        f"{offenders} inline a CPU burner loop instead of going through "
        f"scripts/load_burners.sh (own process group + trap + hard timeout)")
