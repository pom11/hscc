"""The leg-selection block of scripts/run_tests.sh (t_23bf61f3).

Before this block the runner had NO way to run a chosen subset: extra argv goes
to pytest, so `run_tests.sh hscc-api` ran all twelve legs and passed "hscc-api"
to pytest as a path. That is the reason the roster never made it into CI — a job
that must run everything is either slow or (as measured on #443/#446) not run at
all, and a leg nobody runs cannot block a merge.

Claims, each pinned separately:

  1. the leg loop iterates the SELECTION, not the roster, and still sets
     CURRENT_SUITE (the SIGTERM forensics depend on it — a loop rewritten to
     ``for d in "${LEGS[@]}"`` without that line loses the "mid-which-suite"
     report, which is the whole point of the trap);
  2. unset HSCC_TEST_LEGS selects every leg, so the default posture is
     byte-identical to the pre-CI behaviour and no existing habit changes;
  3. a selection runs only the named legs, in roster order, deduplicated;
  4. an unknown leg name is a HARD ERROR (exit 64) before any leg starts, and
     the valid roster is printed. Fail-closed is the load-bearing choice: a typo
     that silently ran nothing would report green, and "green" from a job that
     executed zero tests is the exact failure mode this gate exists to close;
  5. the selection is ANNOUNCED, so a reader of a redirected log can tell
     "3 legs, selected" from "12 legs, full suite" without re-running anything.

Style note: claims 2-5 execute the SHIPPED block (extracted verbatim, then run
under bash against a stub roster) rather than a copy, so the guard cannot drift
from the runner — same approach as test_run_tests_harness_env.py. Claim 4 is
also run against the real script end-to-end.
"""

import re
import subprocess
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
RUNNER = SCRIPTS / "run_tests.sh"
REPO = SCRIPTS.parent

#: From the first line of the block to the banner that follows it. Anchored on
#: two literals that are each unique in the file, so a rewrite of the block's
#: internals cannot make this match nothing.
_BLOCK = re.compile(
    r'(?ms)^SELECTED_LEGS="\$\{HSCC_TEST_LEGS:-\}"$\n.*?^(?=# ━━━ SIGTERM forensics)'
)

STUB = ("alpha", "beta", "gamma", "delta")


def _shipped_block() -> str:
    m = _BLOCK.search(RUNNER.read_text(encoding="utf-8"))
    if m is None:
        raise AssertionError(
            "scripts/run_tests.sh no longer defines the HSCC_TEST_LEGS selection "
            "block in the shape this guard parses — re-read the runner before "
            "believing any CI leg selection is what the workflow thinks it is"
        )
    return m.group(0)


def _run(selection: str | None, roster: tuple[str, ...] = STUB) -> subprocess.CompletedProcess:
    """Execute the shipped block under bash with a stub roster."""
    script = "set -u\nDIRS=(%s)\n%s\nprintf '%%s\\n' \"${LEGS[@]}\"\n" % (
        " ".join(roster),
        _shipped_block(),
    )
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(script)
        path = Path(fh.name)
    # HOME is not optional for the real runner (line 23 expands $HOME under
    # `set -u`), so the harness gives it a tmp dir rather than pretending the
    # variable is unset in CI. GitHub runners always set HOME.
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": tempfile.gettempdir()}
    if selection is not None:
        env["HSCC_TEST_LEGS"] = selection
    try:
        return subprocess.run(
            ["bash", str(path)], capture_output=True, text=True, check=False, env=env
        )
    finally:
        path.unlink(missing_ok=True)


def _legs(proc: subprocess.CompletedProcess) -> list[str]:
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = proc.stdout.splitlines()
    return [ln for ln in lines if ln in STUB]


# ── claim 1: the loop consumes the selection ─────────────────────────────────


def test_the_leg_loop_iterates_the_selection_not_the_roster():
    text = RUNNER.read_text(encoding="utf-8")
    assert 'for d in "${LEGS[@]}"; do' in text, "the leg loop is not iterating the selection"
    assert 'for d in "${DIRS[@]}"; do' not in text, (
        "a second loop over the raw roster means a selected run still executes "
        "every leg — HSCC_TEST_LEGS would be decorative"
    )


def test_the_loop_still_publishes_current_suite_for_the_sigterm_trap():
    text = RUNNER.read_text(encoding="utf-8")
    loop = text.split('for d in "${LEGS[@]}"; do', 1)[1].split("\ndone", 1)[0]
    assert 'CURRENT_SUITE="$d"' in loop, (
        "the selection rewrite dropped the per-leg CURRENT_SUITE assignment; "
        "scripts/run_tests.sh's SIGTERM trap reports 'mid-which-suite' from it"
    )


# ── claim 2: the default posture is unchanged ────────────────────────────────


def test_unset_selection_is_the_whole_roster():
    assert _legs(_run(None)) == list(STUB)
    assert _legs(_run("")) == list(STUB), "an empty HSCC_TEST_LEGS must mean the full suite"


def test_the_announce_distinguishes_the_full_suite_from_a_selection():
    full = _run(None).stdout
    assert "FULL SUITE" in full, full
    part = _run("beta").stdout
    assert "SELECTED 1/4" in part, part
    assert "NOT the full suite" in part, part


# ── claim 3: a selection is a selection ──────────────────────────────────────


def test_only_the_named_legs_run_in_roster_order():
    assert _legs(_run("gamma alpha")) == ["alpha", "gamma"], "order must follow DIRS, not the request"


def test_spaces_and_commas_are_the_same_selection_and_duplicates_collapse():
    assert _legs(_run("delta beta beta")) == ["beta", "delta"]
    assert _legs(_run("delta,beta")) == _legs(_run("delta beta"))


def test_selection_survives_a_roster_that_is_reordered():
    # The map's determinism claim: the legs run in roster order even when the
    # caller lists them backwards.
    assert _legs(_run("delta gamma beta alpha")) == list(STUB)


# ── claim 4: fail closed on a name that is not a leg ─────────────────────────


def test_an_unknown_leg_is_a_hard_error_naming_the_valid_roster():
    proc = _run("beta nope")
    assert proc.returncode == 64, proc.stdout + proc.stderr
    assert "nope" in proc.stderr
    for leg in STUB:
        assert leg in proc.stderr, f"the error must print the roster so a typo is fixable: {proc.stderr}"
    assert "delta" in _legs(_run("beta delta")), "a rejected selection must not corrupt the block"


def test_the_shipped_script_rejects_an_unknown_leg_before_running_anything():
    """End-to-end on the real file: a bad selection must not start pytest."""
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": tempfile.gettempdir(),
           "HSCC_TEST_LEGS": "hscc-apo"}
    proc = subprocess.run(
        ["bash", str(RUNNER)], capture_output=True, text=True, check=False, env=env, cwd=REPO
    )
    assert proc.returncode == 64, proc.stdout + proc.stderr
    assert "━━━" not in proc.stdout, "a leg started despite the bad selection"
    assert "hscc-apo" in proc.stderr


# ── claim 5: the roster this all rests on is real ────────────────────────────


def test_every_leg_in_the_roster_has_a_tests_dir():
    """A roster entry with no tests/ dir runs `pytest <missing>` and reports a
    usage error as a leg failure — or worse, silently nothing."""
    text = RUNNER.read_text(encoding="utf-8")
    start = text.index("DIRS=(") + len("DIRS=(")
    legs = text[start : text.index(")", start)].split()
    assert len(legs) == 12, legs
    for leg in legs:
        assert (REPO / leg / "tests").is_dir(), f"{leg} is in DIRS but has no tests/ dir"


@pytest.mark.parametrize("bad", ["hscc-api,", ",", " ", "hscc api"])
def test_malformed_selections_never_run_a_subset_silently(bad):
    """Whitespace/typo shapes that a workflow could plausibly pass through."""
    proc = _run(bad)
    # "hscc-api," -> token "hscc-api" is unknown under the stub roster, so 64;
    # empty-ish strings mean the full suite (claim 2), never an empty run.
    if bad.strip(" ,") == "":
        assert proc.returncode == 0
        assert "FULL SUITE" in proc.stdout
    else:
        assert proc.returncode == 64, f"{bad!r} must be rejected, not half-run"
