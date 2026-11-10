"""CLI help-surface gate for the unified ``hscc`` CLI (card t_7b0f65c8).

Executes in the canonical suite: ``scripts`` is a DIRS entry of
``scripts/run_tests.sh``, so this module runs every command and subcommand's
``--help`` and ``hscc help`` path in a sandbox and asserts the operator's
invariants (see ``scripts/hscc_cli_help_audit.py`` for the engine, its safety
model, and what A/B/C/D mean).

Green policy — the audit DEFINES what the fix cards (F1..F5) must satisfy:

  * every violation measured on the base tree ships as an explicit EXPECTED
    rule in ``cli_help_expected.json`` (kind + command + needle + owning fix
    card). The suite stays GREEN today, and every unfixed violation is still
    named — nothing is silently allowed;
  * matching is bipartite and strict (findings <-> rules, one-to-one): a
    finding with no rule is a NEW violation (hard fail), a rule with no
    finding is STALE — its violation was fixed/renamed and the fix card must
    prune its entry. This keeps the gate honest from both directions and
    turns it green card-by-card as F1..F5 land;
  * counts are checked too: a rule that matches MORE findings than its
    ``count_at_baseline`` is a new instance of a known violation and fails.

Do not "fix" failures by re-running the generator without reviewing the
diff — the rules ARE the contract the fix cards implement.

Why the surface is enumerated code-side rather than scraped from ``--help``
text: the dispatch tables (hscc_daemon/hscc.py) and the flightdeck parser ARE
the surface; a removed command must not silently vanish from the audit.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "scripts"))

import hscc_cli_help_audit as audit_mod  # noqa: E402

# The gate asserts against the SAME registries the audit derives from, so these
# imports are the point of the assertions, not decoration (t_a95a67df).
import hscc_daemon.hscc as daemon_hscc            # noqa: E402
from hscc_daemon import kanban_blocked            # noqa: E402

EXPECTED_PATH = HERE / "cli_help_expected.json"
A_COUNTS_PATH = HERE / "cli_help_a_counts.json"

# Surface floors. If enumeration shrinks, the walker broke (or commands were
# removed without amending the audit) — that must fail, not silently pass.
# Probe floor amended 180 -> 178 by t_fc307a51 (F5): the ed-* placeholder trio
# was DELETEd from the CLI (operator fan-out ruling), and the audit's
# enumeration loses the three commands in the same commit — they are gone
# from COMMAND_HELP too, so both the `--help` and the `hscc help` probes go
# (6 paths, 9 probes).
# Probe floor amended 178 -> 299 by t_5132deb3 (the CLI-wide -h close F5
# recorded as a follow-up): every flag/flag2 surface now probes `-h`
# alongside `--help` (scripts/hscc_cli_help_audit.py invocations_for), except
# the six launch-class verbs whose -h half is pinned IN-PROCESS instead
# (LAUNCH_PROBE_EXEMPT — their --help probes still run). 299 is EXACTLY what
# a healthy surface measures with both flags — zero slack, so any further
# shrink trips this gate.
# Probe floor amended 299 -> 331 by t_a95a67df (audit A GAP-A-2/A-3): the walk
# now covers the kanban verbs it used to hardcode away (+5 paths/+10 probes),
# reaches the `orch` group router (+3 paths/+7 probes: router --help/-h plus
# inject and waker) and the `cards` group at all (+2 paths/+5 probes). The
# router paths' `hscc help <group>` invocation repeats the one the COMMAND_HELP
# blurb path already issues — the Prober caches by (argv, width) so it costs no
# second subprocess, but it is still counted here, as it was for every other
# group before this change. 331 is EXACTLY what a healthy surface measures —
# zero slack, same rule as above.
MIN_SURFACE_PATHS = 150
MIN_PROBES = 331
MIN_EXAMPLES = 60


@pytest.fixture(scope="module")
def report():
    """One audit run shared by every assertion (full CLI walk ~35 s)."""
    return audit_mod.audit(str(REPO))


@pytest.fixture(scope="module")
def rules():
    data = json.loads(EXPECTED_PATH.read_text())
    return data["rules"]


@pytest.fixture(scope="module")
def a_counts_baseline():
    data = json.loads(A_COUNTS_PATH.read_text())
    return data["counts"], data["lines"]


# Candidate list for the cross-interpreter pin. An entry is only a CANDIDATE;
# :func:`_sibling_is_runnable` decides whether it is a SIBLING (see there).
_SIBLING_CANDIDATES = (
    "~/.hermes/hermes-agent/venv/bin/python",
    "~/miniconda3/envs/p313/bin/python",
    "/Users/desac/miniconda3/envs/p313/bin/python",
    "python3.11", "python3.12", "python3.13",
)

# path -> drop reason, filled by :func:`_sibling_interpreters`. Module level so
# a test can assert a drop is EXPLAINED rather than invisible.
SIBLING_DROPPED: dict = {}

# Loads the audit module exactly the way the (A)-only pass does — same file,
# same module-level imports — so a candidate that passes this probe has proved
# the ONE precondition the pass needs (t_36d9d8fb). ``-B`` keeps it from
# dropping a sibling-version .pyc into the checkout.
_SIBLING_PROBE = (
    "import importlib.util, sys\n"
    "spec = importlib.util.spec_from_file_location('sibling_probe', sys.argv[1])\n"
    "mod = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(mod)\n"
)


def _sibling_is_runnable(py: str):
    """(ok, reason) — can THIS interpreter actually load the audit module?

    "Exists on disk" is not "can run the (A)-only pass". The pass imports
    ``hscc_daemon.hscc`` and ``flightdeck.cli`` at module level, which pull
    third-party packages (``rich``, ``yaml``, ``pygments``, ``markdown_it``)
    that live in the *running* interpreter's own site-packages. A bare
    interpreter therefore satisfies every existence check and then dies with
    ``ModuleNotFoundError`` inside the pass — which the gate used to report as
    a (A)-table failure.

    Measured on this host: ``/opt/homebrew/bin/python3.12`` was a red leg until
    its site-packages gained ``rich``/``pyyaml``, and green from that moment on
    with no code change on our side. The gate was not measuring argparse
    behaviour there at all — it was measuring whether someone had happened to
    ``pip install`` into a Homebrew interpreter, and it failed LOUDLY (a red
    leg, blamed on the card whose diff was checked out) instead of skipping.
    Probing costs ~0.2 s per candidate (measured), so the gate now drops a
    dep-less interpreter with a recorded reason and still runs every sibling
    that is genuinely able to answer.
    """
    try:
        proc = subprocess.run([py, "-B", "-c", _SIBLING_PROBE,
                               str(REPO / "scripts" / "hscc_cli_help_audit.py")],
                              capture_output=True, text=True, timeout=120)
    except Exception as e:                                    # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    if proc.returncode != 0:
        return False, _root_cause(proc) or f"exit {proc.returncode}"
    return True, ""


# What a failing sibling's stderr tail could look like when the REAL cause sat
# above it. Python echoes the offending SOURCE LINE into a traceback, and the
# audit's own post-sys.path imports carry `# noqa: E402` — so a character-cut
# tail of a `ModuleNotFoundError: No module named 'rich'` traceback ends on the
# literal text "E402". That is what sent three cards down a lint rabbit hole
# (t_36d9d8fb): the reported failure was "failed the (A)-only pass: E402" and
# the actual failure was a missing third-party package. The fix is `_root_cause`
# below, pinned by test_sibling_cause_extraction_never_blames_a_lint_code.


def _root_cause(proc) -> str:
    """The exception line of a failed sibling run — never a mid-line cut.

    Reads whole lines, so the excerpt can never start inside a ``# noqa:``
    comment and hand the next reader a phantom lint code.
    """
    lines = [ln for ln in (proc.stderr or proc.stdout or "").splitlines()
             if ln.strip()]
    for ln in reversed(lines):
        stripped = ln.lstrip()
        if stripped and not stripped.startswith(("File \"", "^", "#")) \
                and not stripped[0].isdigit() and "  File " not in ln:
            return stripped[:300]
    return ""


def _sibling_interpreters() -> list[str]:
    """Interpreters distinct from this one that can run the (A)-only pass.

    The gate must produce the SAME normalised (A) table on every CPython the
    suite runs on (t_cfdcefa9 / GATE-313: argparse 3.13 widened its help column
    by 2 chars and the gate went red on 3.13 only). Candidates: this leg's
    ``HSCC_TEST_PY`` siblings from run_tests.sh, plus PATH ``python3.x``.
    Deduped by realpath so aliases don't re-run the same interpreter.

    Drops every candidate that cannot load the audit module, recording why in
    :data:`SIBLING_DROPPED` so the skip reason can name it — a dropped
    candidate is visible, never silently absent."""
    cands = []
    for p in (os.environ.get("HSCC_SIBLING_PY"), *_SIBLING_CANDIDATES):
        if not p:
            continue
        cands.append(os.path.expanduser(p))
    here = os.path.realpath(sys.executable)
    seen, out = {here}, []
    SIBLING_DROPPED.clear()
    for c in cands:
        path = c if os.path.isabs(c) else _which(c)
        if not path:
            continue
        # "Candidate" is not "runnable": an entry may point into a HOME that
        # does not exist (sandboxed-HOME tests, another operator's absolute
        # path baked into the list). Only a real executable file is a sibling.
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        ok, why = _sibling_is_runnable(path)
        if not ok:
            SIBLING_DROPPED[path] = why
            continue
        out.append(path)
    return out


def _which(name: str):
    from shutil import which
    return which(name)


def _finding_matches_rule(f, rule):
    return (f["kind"] == rule["kind"]
            and f["command"] == rule["command"]
            and rule["needle"] in f["detail"])


def _greedy_bipartite(findings, rules):
    """Assign findings to rules one-to-one; exact (kind, command) first so a
    specific rule claims its findings before any looser rule can."""
    order = sorted(range(len(rules)),
                   key=lambda i: 0 if "%" not in rules[i]["command"] else 1)
    assigned = {}
    taken = set()
    for i in order:
        for j, f in enumerate(findings):
            if j in taken:
                continue
            if _finding_matches_rule(f, rules[i]):
                assigned[i] = assigned.get(i, []) + [j]
                taken.add(j)
    unmatched_findings = [findings[j] for j in range(len(findings))
                          if j not in taken]
    unmatched_rules = [rules[i] for i in range(len(rules)) if i not in assigned]
    over = [(rules[i], len(v)) for i, v in assigned.items()
            if len(v) > rules[i].get("count_at_baseline", len(v))]
    return unmatched_findings, unmatched_rules, over


def test_surface_is_enumerated(report):
    """The walk must cover the real surface — a shrinking surface is a finding."""
    assert report["surface_paths"] >= MIN_SURFACE_PATHS, (
        f"audit surface shrank: expected >= {MIN_SURFACE_PATHS} code-derived "
        f"paths, got {report['surface_paths']} — the walker is broken")
    assert report["probes"] >= MIN_PROBES, (
        "probes shrank — help paths are not being executed")


def test_examples_are_parse_checked(report):
    """Every example must be parse-checked (the operator was previously burned
    by a "green" test that actually RAN an example).

    If extraction found (almost) no examples while the CLI clearly advertises
    ``example:`` lines, the EXTRACTION broke — that must fail loudly rather
    than let examples silently go unvalidated.
    """
    assert report["examples_total"] >= MIN_EXAMPLES, (
        "example extraction collapsed — examples would silently go "
        f"unparse-checked (got {report['examples_total']})")


def test_help_paths_only_skip_api_start(report):
    """Only 'api start --help' may be skipped (it would fork a live server),
    plus the t_5132deb3 launch-class `-h` exemptions — and those are pinned
    to the audit constant EXACTLY, so an exemption can never be quietly
    widened: the skip set must equal LAUNCH_PROBE_EXEMPT one-for-one."""
    for s in report["skips"]:
        assert "api start" in s["surface"] or s.get("kind") == "h-exempt", (
            f"unexpected skip — every help path must actually run: {s}")
    h_exempt = {s["surface"] for s in report["skips"]
                if s.get("kind") == "h-exempt"}
    assert h_exempt == {"hscc " + " ".join(list(v) + ["-h"])
                        for v in audit_mod.LAUNCH_PROBE_EXEMPT}, (
        f"the -h exemption drifted from LAUNCH_PROBE_EXEMPT: {sorted(h_exempt)}")
    assert report["widths"] == [80, 120, 500]


def test_a_counts_match_committed_table(report, a_counts_baseline):
    """(A) is logical-line identity: the per-surface table must equal the
    committed one (t_cfdcefa9 / GATE-313).

    The table holds canonicalised (A) details, so this fails the moment an
    (A) count or line becomes rendering-dependent — e.g. a CPython argparse
    tweak re-introduces a width-80 wrap fragment counted as a second
    violation — on the interpreter that sees the drift, with no subprocess
    and no interpreter guessing. Re-cut it with
    ``python3 scripts/gen_cli_help_a_baseline.py`` and REVIEW the diff: the
    table is a contract, not a snapshot."""
    counts, lines = a_counts_baseline
    assert {k: len(v) for k, v in report["a_counts"].items()} == counts, (
        "the (A) per-surface count table drifted from "
        "scripts/tests/cli_help_a_counts.json — (A) identity is supposed to "
        "be rendering-independent; see the audit's a_findings_for_surface")
    assert report["a_counts"] == lines, (
        "the (A) per-surface LINE table drifted from the committed fixture — "
        "a canonicalised help line changed content, or (A) detail became "
        "width-dependent again")


def _sibling_a_table(py: str, surfaces: list, timeout: int | None = None) -> tuple:
    """Run the sibling's (A)-only pass; (table, failure_text_or_None, receipt).

    Shared driver so the gate AND its anti-vacuity receipt cannot drift into
    two different notions of "the sibling ran" (t_36d9d8fb).

    ``surfaces`` empty means "no surface has an (A) line locally", which is the
    GREEN state (``cli_help_a_counts.json`` ships ``{}`` — zero ``flightdeck``
    leaks is the contract, not an absence of data). It is emphatically NOT
    "nothing to check": the sibling's job then is to prove it independently
    finds nothing either, across the whole surface. That is sent as the
    explicit ``'*'`` rather than as ``""`` -> ``None``, because an empty string
    in the argv of a future ``TimeoutExpired`` reads like a bug in the caller
    and has already cost this card a wrong first theory (t_28b0e521).

    ``receipt`` is the audit's ``[a-table] surfaces=N probes=N secs=N`` stderr
    line, parsed into a dict. It is the only thing that distinguishes "the
    sibling walked every probe and agreed" from "the sibling answered instantly
    having done nothing", and :func:`_receipt_probes` makes the gate fail on
    the second reading instead of accepting it.

    ``timeout`` is left to the caller because the budget is calibrated against
    the interpreter that is about to run the pass — see :func:`_a_table_timeout`
    and the measurement block beside it.
    """
    proc = subprocess.run(
        [py, str(REPO / "scripts" / "hscc_cli_help_audit.py"),
         "--repo", str(REPO), "--a-table", ",".join(surfaces) or "*"],
        capture_output=True, text=True,
        timeout=timeout if timeout is not None else A_TABLE_TIMEOUT_SECS)
    if proc.returncode != 0:
        # A non-zero exit means the sibling could not complete the pass at all
        # — a broken interpreter or an import error inside it, NOT a table
        # mismatch. `_root_cause` reports the whole exception line; the old
        # `stderr[-400:]` character cut could (and did) land inside a
        # `# noqa: E402` source line echoed by the traceback and blame a lint
        # code that nothing ever reported.
        return None, (_root_cause(proc) or proc.stderr.strip()[-400:]), {}
    try:
        return json.loads(proc.stdout), None, _parse_a_receipt(proc.stderr)
    except Exception as e:                                     # noqa: BLE001
        return None, f"(A) table unparseable: {type(e).__name__}: {e}", {}


# ── the (A)-only pass time budget (t_28b0e521) ───────────────────────────
#
# This cap was a bare `timeout=300` and the pass needed ~312 s on a loaded
# box, so the leg died in `subprocess.TimeoutExpired` for weeks. The real
# defect was redundant work (the pass rendered every surface at 80, 120 AND
# 500 and read only the widest — 899 CLI launches where 331 suffice), and that
# is fixed in `hscc_cli_help_audit.a_table`. This budget is the SECOND half:
# a cap that is a constant is still a flake generator on a box whose
# contention varies 5x between an idle tree and five workers plus reviewer
# legs, which is exactly the spread that produced the three identical reports
# this card cites.
#
# MEASURED on this host (14 cores), sibling = `~/miniconda3/envs/p313`,
# load = 8 CPU spinners + 8 concurrent `python -c 'import rich, yaml'`
# churners (load average 9.1-9.4), `nice -n 10`, 2 runs each:
#
#   899 probes (before):  312.55 s   312.00 s     -> blew the 300 s cap
#   331 probes (after):   see the changelog entry for this card
#   single CLI --help probe, same load: see A_TABLE_PROBE_SECS_MEASURED
#
# So the budget is not "300 became 900". It is: measure what ONE probe costs
# this interpreter under the load that actually exists right now, multiply by
# the number of probes this pass will launch, and keep a generous safety
# factor. A busy box therefore gets a bigger budget instead of a red leg, and
# a wedged pass is still caught — the ceiling below is what bounds it, so the
# budget can never grow into an infinite hang.
A_TABLE_PROBE_SECS_MEASURED = 0.35   # measured, loaded host; see block above
A_TABLE_TIMEOUT_FLOOR_SECS = 180     # never tighter, even on an idle box
A_TABLE_TIMEOUT_CEIL_SECS = 900      # a wedge must still surface as a failure
A_TABLE_TIMEOUT_SAFETY = 4.0         # slack for contention rising MID-walk
A_TABLE_TIMEOUT_SECS = 600           # documented default; see _a_table_timeout

#: The probe count the whole-surface (A)-only walk must launch on a healthy
#: tree: 331 invocations (`MIN_PROBES`, the audit's own floor) minus the two
#: `api start` invocations the (A)-only pass never executes. MEASURED on this
#: host via the receipt: `[a-table] surfaces=329 probes=329`. The gate asserts
#: the sibling's receipt against it, so the pass cannot satisfy its budget by
#: doing less work — the property that makes a generous budget safe here rather
#: than a way to hide a hang.
A_TABLE_MIN_PROBES = 329


def _a_table_timeout(py: str, n_probes_hint: int = A_TABLE_MIN_PROBES) -> int:
    """Calibrate the (A)-only budget against THIS interpreter, right now.

    The unit of work the whole pass is made of is one CLI launch
    (``python -m hscc_daemon.hscc <argv> --help``), so this times three real
    ones of exactly that and takes the median — a direct measurement of the
    contention that exists at this moment, under the same interpreter the pass
    will run under. No load-average heuristic, no per-host constant. Costs
    ~1-2 s, against a pass that costs minutes.

    On a calibration failure it returns :data:`A_TABLE_TIMEOUT_SECS`, the
    documented constant: an unmeasurable box must not be able to authorise a
    one-second budget by accident.

    The probe honours the audit's own safety model rather than re-deriving it:
    HOME points at a throwaway dir seeded with ``.hscc/cluster.json``, because
    "we only ever run ``--help``" is exactly the assumption this repo refuses
    to make about the operator's real ``~/.hscc`` (see the audit's Safety model
    block). Seeding the cluster file also keeps the probe measuring the same
    code path the real pass measures, instead of an absent-config branch.
    """
    home = Path(tempfile.mkdtemp(prefix="a-table-calib-home-"))
    try:
        (home / ".hscc").mkdir(parents=True, exist_ok=True)
        # Address guard: 10.0.0.x is the documented LAN placeholder, never a
        # real host (same convention as the audit's own seed).
        (home / ".hscc" / "cluster.json").write_text(
            '{"gateway": {"host": "192.0.2.1"}, "workers": [],'
            ' "nasDevices": []}')
        env = {**os.environ,
               "HOME": str(home),
               "PYTHONPATH": os.pathsep.join(
                   [str(REPO), str(REPO / "hscc-project")]),
               "COLUMNS": "500", "NO_COLOR": "1", "TERM": "dumb"}
        costs = []
        for _ in range(3):
            t0 = time.monotonic()
            try:
                subprocess.run([py, "-m", "hscc_daemon.hscc", "--help"],
                               capture_output=True, text=True, timeout=60,
                               env=env, cwd=str(home))
            except Exception:                                    # noqa: BLE001
                return A_TABLE_TIMEOUT_SECS
            costs.append(time.monotonic() - t0)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    unit = max(sorted(costs)[len(costs) // 2], A_TABLE_PROBE_SECS_MEASURED)
    return int(min(max(n_probes_hint * unit * A_TABLE_TIMEOUT_SAFETY,
                       A_TABLE_TIMEOUT_FLOOR_SECS),
                   A_TABLE_TIMEOUT_CEIL_SECS))


#: The audit's per-run receipt line, parsed. Absent receipt -> {} -> the gate
#: treats it as ZERO probes: a pass that cannot prove it walked is a failure,
#: never a pass. That default is deliberate — it is the same "unrecognised
#: shape fails, never defaults to ok" rule t_82fa20f5 landed for the
#: cross-validation probe, applied here to the sibling walk.
_A_RECEIPT_RE = None


def _parse_a_receipt(stderr: str) -> dict:
    import re as _re
    global _A_RECEIPT_RE
    if _A_RECEIPT_RE is None:
        _A_RECEIPT_RE = _re.compile(
            r"\[a-table\]\s+surfaces=(\d+)\s+probes=(\d+)\s+secs=([\d.]+)")
    for ln in reversed((stderr or "").splitlines()):
        m = _A_RECEIPT_RE.search(ln)
        if m:
            return {"surfaces": int(m.group(1)), "probes": int(m.group(2)),
                    "secs": float(m.group(3))}
    return {}


def _receipt_probes(receipt: dict) -> int:
    return int(receipt.get("probes", 0) or 0)


def test_a_table_identical_under_sibling_interpreters(report):
    """Cross-interpreter pin (the stronger half of GATE-313).

    Runs the audit's (A)-only pass for every surface that currently has an
    (A) line under a SIBLING CPython (one distinct from pytest's own) and
    asserts the tables are equal. argparse 3.13 widened its help column by 2
    chars, which is exactly the class of drift this catches inside a single
    leg — a sibling that renders the same logical line differently can no
    longer hide until its own full-suite leg runs. Skips only when this host
    has no runnable sibling; the skip reason NAMES every candidate that was
    dropped and why, so a host that loses its siblings says so instead of
    looking green (t_36d9d8fb).

    Two things stop this becoming a test that only passes on an idle box
    (t_28b0e521): the budget is CALIBRATED against the sibling's own measured
    probe cost rather than being a constant, and the sibling's receipt must
    report the full probe walk — so the pass cannot buy its way inside the
    budget by doing less work. A timeout is re-raised as an assertion that
    states the measured per-probe cost it budgeted against, instead of
    surfacing as a bare ``subprocess.TimeoutExpired`` traceback with no
    number in it, which is what made three separate cards re-diagnose this
    from scratch."""
    surfaces = sorted(report["a_counts"])
    siblings = _sibling_interpreters()
    if not siblings:
        dropped = "; ".join(f"{p}: {w}" for p, w in sorted(SIBLING_DROPPED.items()))
        pytest.skip("no sibling interpreter distinct from this one is runnable"
                    " on PATH"
                    + (f" (dropped -> {dropped})" if dropped else ""))
    for py in siblings:
        budget = _a_table_timeout(py)
        try:
            table, failed, receipt = _sibling_a_table(py, surfaces,
                                                      timeout=budget)
        except subprocess.TimeoutExpired as e:
            pytest.fail(
                f"sibling {py} did not finish the (A)-only pass inside its"
                f" calibrated budget of {budget}s (median CLI --help probe"
                f" measured just before the walk; see the measurement block"
                f" beside A_TABLE_TIMEOUT_SECS). This pass launches"
                f" ~{A_TABLE_MIN_PROBES} CLI subprocesses; a timeout here"
                f" means the box is starved or the pass is wedged, NOT that"
                f" the (A) table drifted.\n  stdout tail:"
                f" {(e.stdout or '')[-300:]!r}\n  stderr tail:"
                f" {(e.stderr or '')[-300:]!r}")
        else:
            assert failed is None, (
                f"sibling {py} could not run the (A)-only pass: {failed}")
            # The walk must have actually happened. Without this the sibling
            # could answer `{}` in 0.1 s and the equality below would pass
            # while having compared nothing (t_28b0e521 requirement: never
            # green merely because the box happened to be free).
            assert _receipt_probes(receipt) >= A_TABLE_MIN_PROBES, (
                f"sibling {py} reported only {receipt!r} for the (A)-only"
                f" walk — a healthy whole-surface pass launches >="
                f" {A_TABLE_MIN_PROBES} CLI probes. The sibling answered"
                f" without walking the surface, so the table equality below"
                f" would be vacuous.")
            assert table == report["a_counts"], (
                f"(A) table differs under sibling interpreter {py} "
                f"({' '.join(_py_version(py))}) — finding identity became "
                f"interpreter-dependent again:\n"
                f"  sibling: {json.dumps(table, indent=2)}\n"
                f"  local:   {json.dumps(report['a_counts'], indent=2)}")


def _py_version(py: str) -> list:
    proc = subprocess.run([py, "-c", "import sys;print(sys.version)"],
                          capture_output=True, text=True, timeout=30)
    return proc.stdout.split() or ["?"]


# ── sibling-gate integrity pins (t_36d9d8fb) ─────────────────────────────
# The card that opened this work recorded the failure as
# "sibling /opt/homebrew/bin/python3.12 failed the (A)-only pass: E402" and
# asked why a linter reported E402 in-leg. Nothing ever reported E402: the
# sibling died on `ModuleNotFoundError: No module named 'rich'` and the
# `[-400:]` character cut landed inside the `# noqa: E402` source line Python
# echoes into a traceback. These pins hold both halves of the fix.

# The real captured stderr shape, verbatim in structure (t_49013b1b leg log):
# Python echoes the offending source line, the audit's own imports carry
# `# noqa: E402`, and the exception line sits BELOW the echoed source line.
_REAL_DEP_FAIL_STDERR = """\
Traceback (most recent call last):
  File "/repo/scripts/hscc_cli_help_audit.py", line 163, in <module>
    import hscc_daemon.hscc as daemon_hscc  # noqa: E402  (module import is side-effect free)
  File "/repo/hscc_daemon/hscc.py", line 29, in <module>
    from hscc_daemon import cli_theme as theme
  File "/repo/hscc_daemon/cli_theme.py", line 25, in <module>
    from rich.color import Color
ModuleNotFoundError: No module named 'rich'
"""


class _FakeProc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_sibling_cause_extraction_never_blames_a_lint_code():
    """The failure EXCERPT must be a whole line — the old cut invented E402.

    Proves the trap and the fix on the same bytes: the character cut the gate
    used to take does contain the literal text E402 (so the phantom code is
    reproducible), while `_root_cause` returns the ModuleNotFoundError that
    actually killed the sibling."""
    old_style = _REAL_DEP_FAIL_STDERR[-400:]
    assert "E402" in old_style and "ModuleNotFoundError" in old_style, (
        "fixture drifted: this must stay the real traceback shape")
    cause = _root_cause(_FakeProc(rc=1, err=_REAL_DEP_FAIL_STDERR))
    assert cause.startswith("ModuleNotFoundError"), cause
    assert "E402" not in cause, cause
    # A lint code only ever appears here as a `# noqa:` comment — never as a
    # reported cause.
    assert "# noqa" not in cause


def test_sibling_gate_uses_the_interpreter_that_can_import_the_audit(
        monkeypatch):
    """A dependency-less interpreter is DROPPED with a reason, not red.

    Builds a real venv from the leg's own interpreter (no third-party deps, so
    `import rich` inside the audit genuinely fails) and asserts the candidate
    is dropped AND the drop is explained — the two properties the old code
    lacked: it treated the interpreter as runnable and failed the leg."""
    import venv

    sandbox = Path(tempfile.mkdtemp(prefix="sibling-nodeps-"))
    target = sandbox / "venv"
    try:
        # A COPY venv, not a symlink one: under `symlinks=True` the venv's
        # bin/python resolves back to the leg's own interpreter and the
        # selector would dedupe it away as "this interpreter" before the probe
        # ever ran (measured: symlinked realpath == sys.executable, copied
        # realpath distinct). Build cost 0.02 s.
        venv.EnvBuilder(with_pip=False, symlinks=False).create(str(target))
        py = str(target / "bin" / "python")
        assert os.path.isfile(py), "venv build produced no interpreter"
        # Sanity: this interpreter really is alive and really lacks a dep the
        # audit needs, so a PASS here means the probe rejected it, not that the
        # venv was broken.
        alive = subprocess.run([py, "-c", "import sys;print(sys.version_info[:2])"],
                              capture_output=True, text=True, timeout=60)
        assert alive.returncode == 0, alive.stderr
        missing = subprocess.run([py, "-c", "import rich"],
                                 capture_output=True, text=True, timeout=60)
        assert missing.returncode != 0 and "rich" in missing.stderr, (
            "pin premise broken: the venv has rich, so it proves nothing")

        ok, reason = _sibling_is_runnable(py)
        assert ok is False, "a dep-less interpreter must not be a sibling"
        assert "ModuleNotFoundError" in reason and "rich" in reason, reason

        # And the real selector must drop it (with a recorded reason) when it
        # is the only candidate standing.
        monkeypatch.delenv("HSCC_SIBLING_PY", raising=False)
        monkeypatch.setattr(sys.modules[__name__], "_SIBLING_CANDIDATES", (py,))
        assert _sibling_interpreters() == []
        assert SIBLING_DROPPED.get(py), "drop must carry a recorded reason"
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def test_sibling_gate_still_runs_a_real_sibling():
    """Anti-vacuity: the gate may not be 'fixed' into a permanent skip.

    Two receipts, both cheap:

      1. the selected sibling must actually EXECUTE the audit's (A)-only
         contract and answer with parseable JSON. Restricting the walk to a
         surface name that cannot exist makes it return ``{}`` without
         launching a probe subprocess, so this proves the whole
         spawn -> import -> argparse -> JSON path for real in seconds;
      2. if this host has ANY python3.x that can import the audit's
         third-party deps (checked independently of the gate's own probe, so
         the assertion is not circular) the gate must have selected at least
         one sibling. A change that empties `_sibling_interpreters()` on a
         host with a usable sibling fails here instead of silently un-gating
         GATE-313."""
    siblings = _sibling_interpreters()
    for py in siblings:
        table, failed, _receipt = _sibling_a_table(py, ["no-such-surface-xyz"])
        assert failed is None, (
            f"selected sibling {py} cannot run the (A)-only contract: {failed}")
        assert table == {}, f"unexpected (A) table for a phantom surface: {table}"

    have_capable = False
    for name in ("python3.11", "python3.12", "python3.13", "python3.14"):
        path = _which(name)
        if not path:
            continue
        probe = subprocess.run([path, "-c", "import rich, yaml"],
                               capture_output=True, text=True, timeout=60)
        if probe.returncode == 0:
            have_capable = True
            break
    if have_capable:
        assert siblings, (
            "a capable interpreter exists on PATH but the gate selected no "
            f"sibling (dropped: {SIBLING_DROPPED}) — the cross-interpreter "
            "pin has been un-gated")
    # Whatever the outcome, a dropped candidate must always be explained.
    assert all(v for v in SIBLING_DROPPED.values()), SIBLING_DROPPED


# ── (A) identity: pure-function pins (no subprocess, deterministic) ──────
# These are the unit-level guard of the GATE-313 fix: the wide-only +
# canonicalise rules and the A-WIDTH content guard, pinned directly.

def _render(width, text):
    return {"argv": [], "width": width, "rc": 0, "hung": False,
            "out": text, "err": "", "secs": 0, "new_files": [], "trips": [],
            "plist_created": False, "port_flip": False}


def _renders(wide_text, w80_text=None, w120_text=None):
    return {500: _render(500, wide_text),
            120: _render(120, w120_text if w120_text is not None else wide_text),
            80: _render(80, w80_text if w80_text is not None else wide_text)}


def test_a_identity_absorbs_wrap_fragments():
    """A width-80 fragment of the wide line counts ONCE (GATE-313 itself):
    3.13's wider help column wrapped 'doctor …' at 80 where 3.11 did not, and
    the raw union counted the same logical line twice."""
    wide = ("doctor          self-check: is flightdeck's view of the fleet "
            "trustworthy right now across nodes")
    wrapped = "doctor self-check: is flightdeck's view of the fleet"
    findings = audit_mod.a_findings_for_surface(_renders(wide, w80_text=wrapped))
    a = [f for f in findings if f["kind"] == "A"]
    assert [f["detail"] for f in a] == [audit_mod.canonical_line(wide)]
    assert not [f for f in findings if f["kind"] == "A-WIDTH"]


def test_a_identity_absorbs_continuation_wrap():
    """A wrapped CONTINUATION carrying 'flightdeck' is a substring of the wide
    line — absorbed, not reported as width-dependent content."""
    wide = "x" * 60 + " flightdeck keeps its own roadmap under hscc-project"
    cont = "flightdeck keeps its own roadmap under hscc-project"
    findings = audit_mod.a_findings_for_surface(
        _renders(wide, w80_text="x" * 60 + "\n" + cont))
    assert [f["kind"] for f in findings] == ["A"]


def test_a_identity_canonicalises_padding_drift():
    """argparse column-gap drift (2 extra spaces) is the same identity."""
    a = audit_mod.canonical_line("doctor    self-check:  flightdeck posture")
    b = audit_mod.canonical_line("doctor      self-check: flightdeck posture")
    assert a == b == "doctor self-check: flightdeck posture"


def test_a_width_guard_fires_on_width_aware_content():
    """A narrow line that is no fragment of the wide render means the help
    CONTENT itself depends on width — reported as A-WIDTH, never dropped."""
    wide = "try 'hscc project doctor' for the fleet view"
    narrow = "try 'hscc project doctor' flightdeck legacy mode only at 80 cols"
    findings = audit_mod.a_findings_for_surface(_renders(wide, w80_text=narrow))
    guard = [f for f in findings if f["kind"] == "A-WIDTH"]
    assert len(guard) == 1 and "flightdeck" in guard[0]["detail"]
    assert guard[0]["width"] == 80


def test_a_count_table_shape():
    """a_count_table() keys on command, keeps only kind (A), sorted."""
    fs = [{"command": "hscc x --help", "kind": "A", "detail": "b flightdeck"},
          {"command": "hscc x --help", "kind": "A", "detail": "a flightdeck"},
          {"command": "hscc x --help", "kind": "B", "detail": "c flightdeck"},
          {"command": "hscc y --help", "kind": "A-WIDTH", "detail": "d"}]
    assert audit_mod.a_count_table(fs) == {
        "hscc x --help": ["a flightdeck", "b flightdeck"]}


# ── the (A)-only width reduction is table-preserving (t_28b0e521) ─────────
#
# `a_table` stopped rendering the 80/120 widths, which is what cut the sibling
# pass from 899 CLI subprocesses to 329 (measured 312.55 s -> 114.80 s on a
# loaded host — the difference between blowing the cap and clearing it). That
# is only legitimate if the table cannot change. These two pins are the proof,
# one by construction and one by example; without them the cut is an opinion.

def test_a_table_probes_only_the_authoritative_width():
    """PROOF BY CONSTRUCTION: the width dropped is never the width read.

    `a_findings_for_surface` derives every kind-"A" detail from `max(renders)`
    alone. So if the width the (A)-only pass keeps IS the widest width the full
    audit probes for that kind, the (A) table it produces is the same table —
    the only findings removed are the A-WIDTH entries `a_table` never reads.
    This assertion is that identity, checked against both sides of the seam
    rather than against literals.
    """
    for kind_flag, full_widths in ((True, audit_mod.WIDTHS_ALL),
                                   (False, audit_mod.WIDTHS_WRAP)):
        kept = audit_mod.A_TABLE_WIDTHS[kind_flag]
        assert kept == (max(full_widths),), (
            f"kind_flag={kind_flag}: the (A)-only pass keeps {kept} but the "
            f"full audit probes {full_widths} — it must keep exactly the "
            f"widest, which is the only render a_findings_for_surface reads "
            f"for class (A). If this fires, the (A)-only table is NOT the "
            f"same table the full audit computes.")


def test_a_table_wide_only_equals_all_widths_on_a_leaking_surface():
    """PROOF BY EXAMPLE: a surface that DOES leak gives one identical table.

    The committed (A) table is `{}` (zero leaks is the contract), so on a green
    tree a width change cannot be observed by comparing tables — `{}` compared
    with `{}` proves nothing. This synthesises the case the fixture cannot: a
    wide line carrying a violation plus narrow renders that are genuine wrap
    fragments of it. Dropping the narrow widths must not move the (A) set by a
    single entry, and the A-WIDTH-only divergence must be exactly what is lost.
    """
    wide = ("doctor          self-check: is flightdeck's view of the fleet "
            "trustworthy right now")
    wrapped = "doctor self-check: is flightdeck's view of the fleet"
    all_w = _renders(wide, w80_text=wrapped)
    wide_only = {500: all_w[500]}

    def a_lines(renders):
        return sorted(f["detail"] for f
                      in audit_mod.a_findings_for_surface(renders)
                      if f["kind"] == "A")

    assert a_lines(wide_only) == a_lines(all_w), (
        "the (A)-only pass's narrower probe set changed the (A) table — the "
        "width reduction in a_table is NOT table-preserving")
    # And name precisely what the reduced pass gives up, so nobody later
    # "restores" it believing the (A) table was losing information:
    assert [f["kind"] for f in audit_mod.a_findings_for_surface(all_w)
            if f["kind"] == "A-WIDTH"] == [], (
        "fixture drifted: this narrow render is a fragment, so A-WIDTH must "
        "be empty and nothing at all is lost by the reduction")


def test_a_table_receipt_parser_never_invents_probes():
    """The anti-vacuity receipt fails CLOSED.

    `_parse_a_receipt` feeding `A_TABLE_MIN_PROBES` is the only thing between
    "the sibling walked 329 probes" and "the sibling answered instantly". A
    missing or malformed line must read as ZERO probes (a failure), never as a
    default that satisfies the floor.
    """
    good = ("[a-table] surfaces=329 probes=329 secs=114.80\n")
    assert _parse_a_receipt(good) == {"surfaces": 329, "probes": 329,
                                      "secs": 114.80}
    assert _receipt_probes(_parse_a_receipt("")) == 0
    assert _receipt_probes(_parse_a_receipt("Traceback ...")) == 0
    assert _receipt_probes({}) == 0
    # A receipt that under-reports must not be coerced up to the floor.
    assert _receipt_probes(_parse_a_receipt(
        "[a-table] surfaces=1 probes=1 secs=0.5")) < A_TABLE_MIN_PROBES


def test_styles_recorded(report):
    """(E) every help path carries a style so the convergence card can act."""
    styles = set(report["styles"].values())
    assert {"rich", "argparse", "print"} <= styles, (
        f"expected all three help styles present, got {styles}")


# ── (COV) the walk covers the group verbs (GAP-A-2 / GAP-A-3) ────────────
#
# Audit A proved the kanban group had been enumerated from two literals in the
# audit ({"blocked","stale"}) while the CLI grew monitor/pause/resume/paused/
# enforce, and that `orch` / `cards` were never walked at all — so the (D)
# "a --help must not execute" guarantee was unenforced for exactly the newest,
# most mutating verbs. These pins fail if that coverage is ever lost again:
# the first asserts the audit's OWN coverage table is clean and names the
# verbs; the second pins the enumeration seam itself (no new hardcoded list).

def test_group_verbs_are_walked(report):
    """Every verb kanban/orch/cards advertise is actually probed."""
    cov = report["coverage"]
    # The registries are the source of truth here ON PURPOSE: this assertion is
    # independent of the audit's derivation, so a registry the audit reads and
    # a registry this test reads must agree or the walk silently shrank.
    from hscc_daemon import kanban_cli

    assert set(cov["kanban"]["walked"]) == (
        set(kanban_blocked.SUBCOMMAND_HELP) | set(kanban_cli.VALID_SUBCOMMANDS)), (
        "hscc kanban verbs walked != the modules' registries — the enumeration "
        f"lost verbs: {sorted(cov['kanban']['walked'])}")
    # The verbs audit A found unprobed, named explicitly so a regression that
    # drops exactly these is obvious in the failure message.
    for verb in ("pause", "resume", "paused", "enforce", "monitor"):
        assert verb in cov["kanban"]["walked"], f"kanban {verb} not walked"
    assert set(cov["orch"]["walked"]) == set(audit_mod.orch_group_verbs()), (
        f"hscc orch verbs walked != _ORCH_GROUP_ENTRIES: {cov['orch']}")
    assert {"inject", "waker"} <= set(cov["orch"]["walked"]), (
        f"orch group router verbs missing: {cov['orch']}")
    assert set(cov["cards"]["walked"]) == set(audit_mod.cards_group_verbs()), (
        f"hscc cards verbs walked != the dispatcher's inventory: {cov['cards']}")
    assert "monitor" in cov["cards"]["walked"]
    for group, row in cov.items():
        assert row["walked"] == row["advertised"], (
            f"hscc {group}: walked {row['walked']} != dispatcher advertises "
            f"{row['advertised']}")
    # And the probes must really have run, not just been enumerated.
    probed = set(report["styles"])
    for verb in ("pause", "resume", "paused", "enforce"):
        assert f"hscc kanban {verb} --help" in probed, f"{verb} --help never ran"
        assert f"hscc kanban {verb} -h" in probed, f"{verb} -h never ran"
    assert "hscc orch --help" in probed and "hscc orch -h" in probed
    assert "hscc cards --help" in probed and "hscc cards monitor --help" in probed


def test_no_coverage_findings(report):
    """COV findings are the audit telling us its own walk has a hole."""
    cov = [f for f in report["findings"] if f["kind"] == "COV"]
    assert not cov, "\\n" + "\\n".join(
        f"  [{f['kind']}] {f['command']} :: {f['detail']}" for f in cov)


def test_coverage_gate_fires_when_a_verb_is_lost(monkeypatch, tmp_path):
    """RED test for the RED gate: a verb the walk stops probing MUST be a COV
    finding (GAP-A-2's whole failure mode was a hole nobody could see).

    Simulates the exact rot — a registry that no longer names a verb the live
    dispatcher still accepts — by trimming ``kanban_cli.VALID_SUBCOMMANDS``,
    then rebuilding the surface and re-running the coverage check. Without the
    (COV) class this passes silently, which is precisely the bug being fixed.
    """
    from hscc_daemon import kanban_cli

    monkeypatch.setattr(kanban_cli, "VALID_SUBCOMMANDS",
                        tuple(v for v in kanban_cli.VALID_SUBCOMMANDS
                              if v != "enforce"), raising=True)
    # the orch engine probe is a subprocess and irrelevant to this leg
    monkeypatch.setattr(audit_mod, "_orch_router_verbs",
                        lambda *_a, **_k: ({"inject", "waker"}, None),
                        raising=True)
    canary = audit_mod.make_canary_dir(tmp_path)
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)

    found = []
    paths, _fd = audit_mod.build_surface()
    table = audit_mod._coverage(tmp_path, canary, paths,
                                lambda g, c, k, d, f: found.append((k, c, d)))
    assert "enforce" not in table["kanban"]["walked"], (
        "the trimmed registry did not actually remove the verb from the walk "
        "— this test would pass without the coverage check")
    hits = [d for k, _c, d in found if k == "COV" and "enforce" in d]
    assert hits, (
        "a verb the dispatcher advertises but the walk dropped produced NO "
        f"COV finding — the gate cannot see GAP-A-2-class rot: {found}")


def test_coverage_gate_fires_on_an_unaskable_dispatcher(monkeypatch, tmp_path):
    """A dispatcher whose refusal names no verb list is reported, not skipped."""
    monkeypatch.setattr(audit_mod, "_verbs_from_text", lambda _t: set(),
                        raising=True)
    monkeypatch.setattr(audit_mod, "_orch_router_verbs",
                        lambda *_a, **_k: (set(), "engine missing"),
                        raising=True)
    canary = audit_mod.make_canary_dir(tmp_path)
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    found = []
    audit_mod._coverage(tmp_path, canary, audit_mod.build_surface()[0],
                        lambda g, c, k, d, f: found.append((k, c, d)))
    assert [f for f in found if f[0] == "COV"], (
        "an unaskable dispatcher produced no finding")


def test_verb_text_parser():
    """The parser that reads a dispatcher's refusal inventory (both spellings).

    This is the seam that turns a dispatcher's error line into a verb set; a
    silent mis-parse here shrinks the walk, which is what the coverage check is
    built to catch — so it is pinned directly, on text, with no CLI involved.
    """
    parse = audit_mod._verbs_from_text
    assert parse("Error: unknown kanban subcommand: x\n"
                 "Valid subcommands: blocked, stale, monitor, pause") == {
                     "blocked", "stale", "monitor", "pause"}
    assert parse("unknown cards subcommand: x\nValid subcommands: monitor") == {
        "monitor"}
    assert parse("ValueError: unknown orch subcommand: x "
                 "(available: inject, waker)") == {"inject", "waker"}
    # a refusal that names no list is "no inventory", never a guessed set
    assert parse("Error: unknown kanban subcommand: x") == set()
    assert parse("") == set()


def test_enumeration_probes_cannot_execute_a_verb(monkeypatch):
    """Landmine pin: the bogus-verb enumeration probes must reach NO verb body.

    The audit derives verb sets by calling each hand-rolled dispatcher with
    :data:`audit_mod.BOGUS_VERB` (an unknown verb → refusal). That is only safe
    because the refusal path returns before any verb runs — the exact class of
    assumption that broke as BROKEN B10 in kanban_cli. So the verb bodies are
    patched to RAISE and the probe is re-run: a bogus-verb call that ever
    reaches a verb fails HERE, with zero real side effects, instead of shipping
    an audit that mutates state while "reading help".
    """
    def trip(*_a, **_k):
        raise AssertionError("enumeration probe reached a verb body")

    from hscc_daemon import cards_live, kanban_blocked, kanban_cli

    home_at_entry = os.environ.get("HOME")

    for name in ("cmd_blocked",):
        monkeypatch.setattr(kanban_blocked, name, trip, raising=True)
    for name in ("_cmd_stale", "_cmd_monitor", "_cmd_pause"):
        monkeypatch.setattr(kanban_cli, name, trip, raising=True)
    for name in ("snapshot", "run_live"):
        monkeypatch.setattr(cards_live, name, trip, raising=True)

    assert audit_mod._ask_dispatcher(kanban_blocked.cmd_kanban) == (
        {"blocked", "stale", "monitor", "pause", "resume", "paused", "enforce"},
        [])
    assert audit_mod._ask_dispatcher(cards_live.cmd_cards) == ({"monitor"}, [])
    # And the probe must leave HOME exactly where it found it: it swaps HOME to
    # a throwaway dir for the call, so a refusal path that grew a state write is
    # caught by the second tuple slot instead of being paid for in whatever HOME
    # the caller had (scripts/tests/conftest.py redirects HOME for the leg).
    assert os.environ.get("HOME") == home_at_entry, (
        "the enumeration probe did not restore HOME")


def test_group_verb_sets_are_derived_not_listed():
    """The enumeration seam must not regress into a literal verb list.

    GAP-A-2's root cause was a list in THIS file that the CLI outgrew. These
    assertions read the same registries the audit reads, so a new verb lands in
    the walk automatically — and if someone reintroduces a hardcoded set, the
    comparison against the registry stops being tautological and fails.
    """
    from hscc_daemon import kanban_cli

    assert set(audit_mod.kanban_group_verbs()) == (
        set(kanban_blocked.SUBCOMMAND_HELP) | set(kanban_cli.VALID_SUBCOMMANDS))
    assert set(audit_mod.orch_group_verbs()) == {
        usage.split()[2].split("|")[0]
        for usage, _d in daemon_hscc._ORCH_GROUP_ENTRIES}
    assert set(audit_mod.cards_group_verbs()) == {"monitor"}


def test_findings_match_expected(report, rules):
    """No NEW violations; no STALE expectations (see module docstring)."""
    unexpected, stale, over = _greedy_bipartite(report["findings"], rules)
    lines = []
    if unexpected:
        lines.append(f"{len(unexpected)} UNEXPECTED violation(s) — new "
                     "regressions the gate must not absorb:")
        for f in unexpected[:25]:
            lines.append(f"  NEW [{f['kind']}] {f['command']} :: "
                         f"{f['detail'][:180]}")
        if len(unexpected) > 25:
            lines.append(f"  …and {len(unexpected) - 25} more")
    if over:
        lines.append(f"{len(over)} rule(s) matched MORE findings than "
                     "baseline (new instances of a known violation):")
        for rule, n in over[:15]:
            lines.append(f"  EXTRA x{n} (baseline "
                         f"{rule['count_at_baseline']}) [{rule['kind']}] "
                         f"{rule['command']} contains {rule['needle']!r}")
    if stale:
        lines.append(f"{len(stale)} STALE rule(s): the violation is gone or "
                     "renamed — prune its entry in "
                     "scripts/tests/cli_help_expected.json (that is the "
                     "fix-card contract working as designed):")
        for rule in stale[:25]:
            lines.append(f"  STALE [{rule['kind']}] {rule['command']} "
                         f"contains {rule['needle']!r} (owner {rule['owner']})")
        if len(stale) > 25:
            lines.append(f"  …and {len(stale) - 25} more")
    if lines:
        pytest.fail("\n".join(lines))


def test_write_findings_artifact(report):
    """Emit the machine findings artifact on demand for the operator/fixers.

    ``HSCC_CLI_HELP_WRITE_ARTIFACT=1`` writes
    docs/audits/artifacts/CLI_HELP_FINDINGS.json; off by default so a normal
    test run never dirties the tree. That path is gitignored on purpose
    (decision, card t_36580090): the artifact is a pure function of the tree,
    so committing it only guarantees it rots as fix cards land. Regenerate
    instead of reading a stale snapshot (or run the audit CLI directly:
    ``python scripts/hscc_cli_help_audit.py --json <path>``).
    """
    if not os.environ.get("HSCC_CLI_HELP_WRITE_ARTIFACT"):
        pytest.skip("set HSCC_CLI_HELP_WRITE_ARTIFACT=1 to emit the findings "
                    "artifact (docs/audits/artifacts/CLI_HELP_FINDINGS.json)")
    out = REPO / "docs" / "audits" / "artifacts" / "CLI_HELP_FINDINGS.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    assert out.exists()
