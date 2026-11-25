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
    """One audit run shared by every assertion.

    MEASURED cost, because every test in this file waits on it: 59.69 s on a
    clean `main` worktree at host load 1.9 (t_4c9feb44) — 961 CLI subprocesses
    over 331 probed surfaces. The "~35 s" this docstring used to claim was both
    wrong and load-blind; the same walk measured 307.64 s while `Prober` paid a
    0.25 s black-holed port probe per call, which is what made the whole `scripts`
    leg look hung from test ~233. Budget against the measured number, and check
    the census before believing any new per-probe cost.
    """
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
#: The PATH interpreters the gate considers. Kept in one place so the
#: independent capability probe below cannot test a version (e.g. 3.14) the
#: gate never looks at and then call the resulting skip "un-gated".
_SIBLING_PY_NAMES = ("python3.11", "python3.12", "python3.13")
_SIBLING_CANDIDATES = (
    "~/.hermes/hermes-agent/venv/bin/python",
    "~/miniconda3/envs/p313/bin/python",
    "/Users/desac/miniconda3/envs/p313/bin/python",
    *_SIBLING_PY_NAMES,
)

# path -> drop reason, filled by :func:`_sibling_interpreters`. Module level so
# a test can assert a drop is EXPLAINED rather than invisible.
SIBLING_DROPPED: dict = {}

#: Environment variables through which the CALLER can inject another
#: interpreter's packages into a probe child. None of them describes the
#: candidate's OWN dependencies, so the gate strips them: what a sibling can
#: import must not depend on who started the leg. MEASURED (t_d5698315): a
#: dep-less COPY venv does not import ``rich`` from a plain shell, but DOES as
#: soon as ``PYTHONPATH`` names a site-packages holding it — the scripts leg ran
#: under such an environment and the premise pin below failed in two clean
#: worktrees. ``__PYVENV_LAUNCHER__`` is macOS's venv launcher hand-off and is
#: dropped for the same reason: a child started inside a venv can otherwise
#: resolve back to it.
_PROBE_ENV_DROP = ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE",
                   "PYTHONSTARTUP", "__PYVENV_LAUNCHER__")


def _probe_env() -> dict:
    """The environment a child interpreter probe gets: caller path pins gone."""
    return {k: v for k, v in os.environ.items() if k not in _PROBE_ENV_DROP}


# Loads the audit module exactly the way the (A)-only pass does — same file,
# same module-level imports — so a candidate that passes this probe has proved
# the ONE precondition the pass needs (t_36d9d8fb). ``-B`` keeps it from
# dropping a sibling-version .pyc into the checkout. ``-I`` makes the probe
# answer for the CANDIDATE alone: it ignores every ``PYTHON*`` env var and the
# user site, so an inherited path cannot make a dep-less interpreter look
# runnable (see ``_PROBE_ENV_DROP``).
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
        proc = subprocess.run([py, "-I", "-B", "-c", _SIBLING_PROBE,
                               str(REPO / "scripts" / "hscc_cli_help_audit.py")],
                              capture_output=True, text=True, timeout=120,
                              env=_probe_env())
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
        capture_output=True, text=True, env=_probe_env(),
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
        env = {**_probe_env(),
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


# ── the sibling arm walks a SAMPLE, not the whole surface (t_49827b5d) ──────
#
# WHAT THIS COST AND WHY IT MOVED. `test_a_table_identical_under_sibling_
# interpreters` asked every sibling interpreter to walk the WHOLE surface:
# `--a-table '*'` -> A_TABLE_MIN_PROBES (329) CLI subprocesses, launched serially
# by the sibling. Measured in the 2.17.5 queue gate (PR #767, 02:46Z) and
# reproduced here at background QoS: **334.69 s** of the leg's 1009 s, with the
# leg's `[leg-heartbeat]` naming exactly this child —
# `first_child=/Users/desac/min... 02:56`, i.e. the whole leg parked on
# `~/miniconda3/envs/p313/bin/python` while it re-launched the CLI 329 times.
# That is 33 % of the leg spent re-proving, on a second CPython, a property the
# sibling cannot vary except through argparse's own rendering.
#
# WHAT THE CHECK IS FOR. GATE-313 was: CPython 3.13's argparse widened the help
# column by 2 chars, so an (A) detail became rendering-dependent and drifted on
# ONE interpreter only. That class is global (it comes from argparse's help
# formatter, not from a specific verb), so the arm does not need all 329
# surfaces to keep its teeth — it needs surfaces from every group, so a drift
# cannot hide in the one group sampled. The sample is therefore GROUP-BREADTH
# first, stride-filled to a floor, and the receipt is recalibrated to the work
# REQUESTED: `>= len(requested)`, never `>= 329` — so the arm still fails if the
# sibling answers without walking what it was asked to walk.
#
# WHAT IS NOT WEAKENED. The full 329-probe walk is one environment variable
# away (`HSCC_CLI_HELP_SIBLING_WALK=full`) and stays the shape used by a deep
# run; the local full-surface walk (`report`, the audit itself) still walks every
# surface at full strength on the leg interpreter, so the (A) table this arm
# compares against is never sampled. A drift on an unsampled surface is then
# caught by the local pass on the interpreter that has it — which is the half
# that actually broke in GATE-313.
SIBLING_WALK_ENV = "HSCC_CLI_HELP_SIBLING_WALK"
SIBLING_SAMPLE_FLOOR = 24


def _walkable_surface_names() -> list:
    """Every surface name the (A)-only pass would walk, in walk order.

    Derived from the SAME `build_surface()` / `invocations_for()` the pass
    consumes (the audit's own single source of truth), so a sampled name can
    never be a name the sibling would silently not find, and a surface that
    disappears shrinks the sample rather than lying about it. The `api start`
    exclusion is the pass's own (:func:`a_table`), not a new one.
    """
    paths, _fd = audit_mod.build_surface()
    out: list = []
    for _group, argv, _style, kind in paths:
        for inv, _flag in audit_mod.invocations_for(argv, kind):
            if inv[:2] == ["api", "start"]:
                continue
            name = audit_mod._norm(inv)
            if name not in out:
                out.append(name)
    return out


def _sibling_sample() -> list:
    """The surfaces the sibling arm walks by default: breadth first, then stride.

    Group breadth is the property (an argparse rendering change is not
    per-verb), so every `hscc <group>` gets its first surface, then the rest of
    the floor is filled by a stride across the whole list so the sample is not
    six near-identical `cluster` verbs.
    """
    names = _walkable_surface_names()
    if len(names) <= SIBLING_SAMPLE_FLOOR:
        return names
    picked: list = []
    seen_groups: set = set()
    for n in names:
        key = " ".join(n.split()[:2])
        if key in seen_groups:
            continue
        seen_groups.add(key)
        picked.append(n)
    room = SIBLING_SAMPLE_FLOOR - len(picked)
    if room > 0:
        step = max(1, len(names) // room)
        for i in range(0, len(names), step):
            if len(picked) >= SIBLING_SAMPLE_FLOOR:
                break
            if names[i] not in picked:
                picked.append(names[i])
    return picked


def _sibling_walk_request(report) -> tuple:
    """(surfaces_to_walk_or_None_for_whole_surface, min_receipt_probes, mode).

    Every surface with a local (A) line is ALWAYS walked — the sample only
    stands in for the "prove you also find nothing here" bulk, never for a real
    finding. ``None`` means the whole surface (`'*'`), which is also the only
    case the A_TABLE_MIN_PROBES receipt floor describes.

    The probe floor is the work REQUESTED because each surface costs exactly one
    probe in this pass (`A_TABLE_WIDTHS` picks one authoritative width), so
    `len(want)` is a real floor rather than a guess — and unlike the old
    constant it is a floor the sample walk can actually meet.
    """
    found = sorted(report["a_counts"])
    whole = ([])          # empty list == `'*'` == the whole surface, see below
    if os.environ.get(SIBLING_WALK_ENV, "").strip().lower() == "full":
        return (found or whole), (len(found) if found else A_TABLE_MIN_PROBES), "full"
    sample = [s for s in _sibling_sample() if s not in found]
    want = sorted(set(found) | set(sample))
    if not want:                          # no surface enumerated at all: never
        return whole, A_TABLE_MIN_PROBES, "full"   # vacuous-sample, walk all
    return want, len(want), "sample"



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
    surfaces, min_probes, mode = _sibling_walk_request(report)
    siblings = _sibling_interpreters()
    if not siblings:
        dropped = "; ".join(f"{p}: {w}" for p, w in sorted(SIBLING_DROPPED.items()))
        pytest.skip("no sibling interpreter distinct from this one is runnable"
                    " on PATH"
                    + (f" (dropped -> {dropped})" if dropped else ""))
    for py in siblings:
        budget = _a_table_timeout(py, n_probes_hint=min_probes)
        try:
            table, failed, receipt = _sibling_a_table(py, surfaces,
                                                      timeout=budget)
        except subprocess.TimeoutExpired as e:
            pytest.fail(
                f"sibling {py} did not finish the (A)-only pass inside its"
                f" calibrated budget of {budget}s (median CLI --help probe"
                f" measured just before the walk; see the measurement block"
                f" beside A_TABLE_TIMEOUT_SECS). This pass launches"
                f" ~{min_probes} CLI subprocesses ({mode} walk —"
                f" {SIBLING_WALK_ENV}=full asks for all"
                f" {A_TABLE_MIN_PROBES}); a timeout here"
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
            # green merely because the box happened to be free). The floor is
            # the work REQUESTED, so it travels with the mode instead of being
            # a constant the sample could never reach (t_49827b5d).
            assert _receipt_probes(receipt) >= min_probes, (
                f"sibling {py} reported only {receipt!r} for a {mode} (A)-only"
                f" walk of {len(surfaces) or 'all'} surface(s) — the pass was"
                f" asked for >={min_probes} probes and cannot buy its way into"
                f" the budget by doing less work. The sibling answered"
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
    lacked: it treated the interpreter as runnable and failed the leg.

    The caller's environment is made HOSTILE on purpose (t_d5698315): the leg
    that found this ran with a `PYTHONPATH` naming another interpreter's
    site-packages, and that was enough to hand `rich` to the dependency-less
    venv, invalidating the premise. The control below proves the hostile path
    really reaches an unfiltered child; the probes must not."""
    import venv

    sandbox = Path(tempfile.mkdtemp(prefix="sibling-nodeps-"))
    target = sandbox / "venv"
    try:
        # The exact leak shape, fabricated so the pin does not depend on this
        # host's real site-packages: any `import rich` that inherits this
        # PYTHONPATH succeeds. Measured on main at two clean worktrees, the
        # premise assertion below failed for this reason.
        hostile = sandbox / "hostile-site-packages"
        hostile.mkdir()
        (hostile / "rich.py").write_text("VERSION = 'injected-by-caller'\n")
        monkeypatch.setenv("PYTHONPATH", str(hostile))

        # A COPY venv, not a symlink one: under `symlinks=True` the venv's
        # bin/python resolves back to the leg's own interpreter and the
        # selector would dedupe it away as "this interpreter" before the probe
        # ever ran (measured: symlinked realpath == sys.executable, copied
        # realpath distinct). Build cost 0.02 s.
        venv.EnvBuilder(with_pip=False, symlinks=False).create(str(target))
        py = str(target / "bin" / "python")
        assert os.path.isfile(py), "venv build produced no interpreter"
        # Control: the hostile path IS reachable for a child that inherits the
        # caller's environment. If this ever fails the pin is vacuous, not green.
        leaked = subprocess.run([py, "-c", "import rich"],
                                capture_output=True, text=True, timeout=60)
        assert leaked.returncode == 0, (
            "pin premise broken: the hostile PYTHONPATH did not reach the "
            f"venv, so this test cannot prove the scrub: {leaked.stderr}")
        # Sanity: this interpreter really is alive and really lacks a dep the
        # audit needs, so a PASS here means the probe rejected it, not that the
        # venv was broken. `-I` + `_probe_env()` are the fix under test: the
        # candidate answers for itself, never through the caller's path.
        alive = subprocess.run([py, "-I", "-c",
                                "import sys;print(sys.version_info[:2])"],
                               capture_output=True, text=True, timeout=60,
                               env=_probe_env())
        assert alive.returncode == 0, alive.stderr
        missing = subprocess.run([py, "-I", "-c", "import rich"],
                                 capture_output=True, text=True, timeout=60,
                                 env=_probe_env())
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


def _capable_python_on_path():
    """The first PATH interpreter the gate could select that can import the
    audit's deps, if any.

    Scans the same names as :data:`_SIBLING_PY_NAMES`: a version the gate never
    considers (e.g. ``python3.14`` on the runner) is not evidence of an un-gated
    pin. The candidate must also be a DIFFERENT interpreter from the one running
    this leg. In the CI venv ``python3.13`` on PATH *is* this interpreter (the
    venv was built from it), and counting it made the anti-vacuity assertion
    below fail on a runner that has no second CPython — while the gate itself
    correctly deduped it as "this interpreter"."""
    here = os.path.realpath(sys.executable)
    for name in _SIBLING_PY_NAMES:
        path = _which(name)
        if not path or os.path.realpath(path) == here:
            continue
        probe = subprocess.run(
            [path, "-c", "import rich, yaml, pygments, markdown_it"],
            capture_output=True, text=True, timeout=60, env=_probe_env())
        if probe.returncode == 0:
            return path
    return None


def test_capable_python_probe_ignores_the_running_interpreter(monkeypatch):
    """CI's only ``python3.x`` is the venv's own base interpreter.

    setup-python puts ``python3.13`` on PATH and the leg builds its venv from
    it, so a capability probe that forgets distinctness reports a "capable
    sibling" that the selector has already (correctly) deduped. That is how
    test_sibling_gate_still_runs_a_real_sibling went red on Ubuntu with an
    empty drop map."""
    monkeypatch.setattr(sys.modules[__name__], "_which", lambda name: sys.executable)
    # The running interpreter can import the audit deps (the suite runs there),
    # so this is the exact shape CI hit: capable, but not a sibling.
    assert _capable_python_on_path() is None


def test_sibling_sample_stays_broad_and_arms_the_full_walk(monkeypatch):
    """The SAMPLE may be cheap, it may not be narrow or vacuous.

    Three properties the sampling decision depends on, each pinned without a
    subprocess (the sample is derived from `build_surface()`, not from a probe):

      * every surface GROUP on the CLI contributes at least one sampled surface
        — the drift class this arm guards (GATE-313: argparse's help column) is
        a formatter property, so it shows up wherever there is help text, and a
        sample drawn from two groups would be a way to un-gate it quietly;
      * the sample is a real fraction of the surface, i.e. the code is still
        choosing, not returning everything (which would silently restore the
        334 s walk) or nothing (which would assert nothing);
      * `HSCC_CLI_HELP_SIBLING_WALK=full` really does ask for the whole surface
        and restores the A_TABLE_MIN_PROBES receipt floor, so the deep run is a
        variable away and is not a second, drifting implementation.
    """
    names = _walkable_surface_names()
    # HEALTH FLOOR, not a restatement of MIN_PROBES: MIN_PROBES (331) counts
    # probe INVOCATIONS, this list is deduped by surface NAME (a `hscc help X`
    # invocation is reachable from two paths), and `a_table` restricts its walk
    # by name too. MEASURED at this commit: 314 distinct names for the 331
    # invocations. If the enumeration collapses, a sample drawn off it is
    # vacuous — the same reason the audit pins MIN_SURFACE_PATHS.
    assert len(names) >= 300, (
        f"the (A)-only walkable surface shrank to {len(names)} distinct names "
        f"(measured 314 at this commit, off the audit's {MIN_PROBES} "
        f"invocations) — a sample off a broken enumeration proves nothing")
    sample = _sibling_sample()
    all_groups = {" ".join(n.split()[:2]) for n in names}
    sample_groups = {" ".join(n.split()[:2]) for n in sample}
    assert sample_groups == all_groups, (
        "the sibling sample no longer covers every surface group, so an "
        f"argparse rendering drift could hide in: {sorted(all_groups - sample_groups)}")
    assert SIBLING_SAMPLE_FLOOR <= len(sample) < len(names) // 4, (
        f"sample is {len(sample)} of {len(names)} surfaces — it must stay a "
        f"bounded SAMPLE (the whole walk is what this card removed)")

    monkeypatch.delenv(SIBLING_WALK_ENV, raising=False)
    want, floor, mode = _sibling_walk_request({"a_counts": {}})
    assert mode == "sample" and floor == len(want) and want, (
        "the sample floor must be the work REQUESTED, so a sibling that "
        "walks nothing still fails the receipt")
    monkeypatch.setenv(SIBLING_WALK_ENV, "full")
    want, floor, mode = _sibling_walk_request({"a_counts": {}})
    assert mode == "full" and not want and floor == A_TABLE_MIN_PROBES, (
        "HSCC_CLI_HELP_SIBLING_WALK=full must request the WHOLE surface "
        f"(empty request list == `*`), got {mode!r}/{want!r}/{floor}")
    # A real (A) line is never left to the sample.
    victim = names[0]
    want, floor, _mode = _sibling_walk_request({"a_counts": {victim: ["x"]}})
    assert victim in want and floor >= 1


def test_sibling_gate_still_runs_a_real_sibling():
    """Anti-vacuity: the gate may not be 'fixed' into a permanent skip.

    Two receipts, both cheap:

      1. the selected sibling must actually EXECUTE the audit's (A)-only
         contract and answer with parseable JSON. Restricting the walk to a
         surface name that cannot exist makes it return ``{}`` without
         launching a probe subprocess, so this proves the whole
         spawn -> import -> argparse -> JSON path for real in seconds;
      2. if this host has ANY DISTINCT python3.x that can import the audit's
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

    have_capable = _capable_python_on_path() is not None
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


# ── the defensive port hold must not cost the leg a socket timeout (t_4c9feb44)
#
# `Prober` binds the api ports WITHOUT listen() as a defensive hold, and the
# comment beside it asserted that "no listen() means connect_ex still reports
# 'refused'". It does not. Measured on this host: nothing bound -> ECONNREFUSED
# in 0.000 s; bound-no-listen -> EAGAIN/EINPROGRESS only after the WHOLE 0.25 s
# socket timeout, because the SYN is never answered at all. `Prober.run` asks
# that question once per probe, so the leg paid a quarter second 961 times:
# 223.05 s of a 307.64 s audit wall (MEASURED, host load 2.1; the same audit
# with the hold closed is 60.00 s and produces the identical report). That is
# why the `scripts` leg looked hung from test ~233 — the module `report`
# fixture alone needed five minutes, so no test in this file ever completed.
#
# The pins below are deliberately `report`-free: they must stay seconds-cheap
# so they still run when the walk itself is the thing under investigation. They
# also move the sensor onto a throwaway loopback port (`API_DEFAULT_PORT` is
# resolved at call time) rather than contend for 8787 with the `report`
# fixture's own hold, which lives for the whole module and is released by
# nobody — a test that needed the real port would silently skip in a full-file
# run and un-gate the fix.

def _free_loopback_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_prober(tmp_path, request=None):
    """A real `Prober` on a throwaway sandbox — shipped hold included.

    `request` (when a test passes it) registers a finalizer: the hold has no
    teardown of its own, and a Prober that leaks 8787 would silently degrade
    every later hold on this host to "bind failed".
    """
    sandbox = tmp_path / "home"
    sandbox.mkdir(parents=True, exist_ok=True)
    canary = audit_mod.make_canary_dir(tmp_path)
    audit_mod._seed_sandbox(sandbox)
    prober = audit_mod.Prober(sandbox, canary, Path(os.path.expanduser("~")))
    if request is not None:
        request.addfinalizer(lambda: [s.close() for s in prober._tripwire_socks])
    return prober


@pytest.mark.skipif(sys.platform != "darwin",
                    reason="the black hole is macOS/BSD behaviour: Linux answers "
                           "a SYN to a bound-but-not-listening port with an RST, "
                           "so the refused-vs-in-progress split cannot exist")
def test_bound_without_listen_does_not_report_refused():
    """PIN THE TRAP: "bound" is not "refused", and the gap is a timeout.

    The hold is only safe if asking it a question is free. It is not, so the
    kernel behaviour is pinned here on a throwaway port rather than trusted
    from a comment: an unbound port answers instantly, a bound-but-not-listened
    port does not answer at all and the caller waits out its own timeout. If a
    future OS ever answers this instantly the pin fires and the cost model
    above needs re-measuring — either way the assumption is tested, not held.
    """
    import errno
    import socket

    port = _free_loopback_port()

    def ask(timeout):
        s = socket.socket()
        s.settimeout(timeout)
        t0 = time.monotonic()
        try:
            rc = s.connect_ex(("127.0.0.1", port))
        finally:
            secs = time.monotonic() - t0
            s.close()
        return rc, secs

    rc, secs = ask(2.0)
    assert rc == errno.ECONNREFUSED, (
        f"premise broken: with nothing bound, 127.0.0.1:{port} must answer "
        f"refused instantly, got {errno.errorcode.get(rc, rc)} after {secs:.3f}s")
    assert secs < 0.5, f"refused must be instant, took {secs:.3f}s"

    hold = socket.socket()
    hold.bind(("127.0.0.1", port))            # bound, NOT listen()ed = the hold
    try:
        rc, secs = ask(1.0)
    finally:
        hold.close()
    code = errno.errorcode.get(rc, rc)
    # macOS answers the un-started non-blocking connect with EAGAIN (35);
    # EINPROGRESS (36) is what the same call returns on other BSDs. Both mean
    # "still waiting", which is the whole point — see the seconds assertion.
    assert rc in (errno.EAGAIN, errno.EINPROGRESS), (
        f"a bound-but-not-listening port must black-hole the SYN and leave the "
        f"caller waiting; got {code} after {secs:.3f}s — the OS changed under "
        f"the audit's defensive hold, re-measure Prober._port_busy")
    assert secs >= 0.5, (
        f"{code} after only {secs:.3f}s of a 1.0s timeout means the wait did "
        f"not burn the timeout — the cost this card is about is gone, "
        f"re-measure before keeping the comment that describes it")


def test_prober_never_asks_a_port_it_holds(tmp_path, monkeypatch, request):
    """While the hold is up, asking THAT port may not open a socket.

    Deterministic on purpose: the assertion is "no syscall", not "fast", so a
    loaded box cannot flake it. A port this Prober holds cannot have been bound
    by anyone else (a stray grandchild dies on EADDRINUSE — that is what the
    hold IS for), so its answer is known without asking the kernel, and asking
    costs the 0.25 s black hole pinned above.

    t_e5a9469e narrowed what the sensor asks by DEFAULT: `prober._port_busy()`
    measures `prober.sensor_port` (a sealed port this walk reserves and pins
    into the sandbox config), never a defended product port. This pin keeps the
    cost rule for an explicitly named held port.
    """
    api_port = _free_loopback_port()
    monkeypatch.setattr(audit_mod, "API_DEFAULT_PORT", api_port)
    prober = _make_prober(tmp_path, request)
    assert api_port in {s.getsockname()[1] for s in prober._tripwire_socks}, (
        f"premise broken: the hold did not take the test api port {api_port} "
        f"— this cannot prove anything about a held port")

    def no_sockets(*_a, **_k):
        raise AssertionError(
            f"Prober._port_busy opened a socket while holding port {api_port}. "
            "A bound-but-not-listening port never answers a SYN (pinned by "
            "test_bound_without_listen_does_not_report_refused), so this "
            "connect cannot tell us anything and burns the full socket timeout "
            "on every one of ~1k probes (t_4c9feb44).")

    monkeypatch.setattr(audit_mod.socket, "socket", no_sockets)
    assert prober._port_busy(api_port) is False, (
        f"port {api_port} is held by this Prober: nothing else can have bound "
        "it, so 'busy' here would be a phantom (D) finding")


def test_prober_still_asks_the_kernel_for_a_port_it_does_not_hold(
        tmp_path, monkeypatch, request):
    """The seal must not blind the sensor: an un-held port is still measured.

    The sealed sensor port is deliberately NOT held (a held port cannot be
    bound by the stray server the check looks for), so the sensor's own answer
    must still come from a real connect — otherwise the (D) "a --help bound the
    api port" check dies silently. The blocker is bound WITHOUT listen() on
    purpose: a `listen()` here would make a CONCURRENT audit on this host read
    the port as busy and manufacture a false (D) finding in someone else's leg.
    """
    import socket

    api_port = _free_loopback_port()
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", api_port))     # bound, no listen: "already taken"
    request.addfinalizer(blocker.close)
    opened = []

    class _FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def settimeout(self, _t):
            pass

        def connect_ex(self, addr):
            opened.append(addr)
            return 0                          # accepted -> a server IS listening

    monkeypatch.setattr(audit_mod, "API_DEFAULT_PORT", api_port)
    # Build the Prober with REAL sockets first — its hold must genuinely FAIL
    # on the blocked port — and only then fake the sensor's socket factory.
    prober = _make_prober(tmp_path, request)
    monkeypatch.setattr(audit_mod.socket, "socket", _FakeSock)
    assert api_port not in {
        s.getsockname()[1] for s in prober._tripwire_socks}, (
        "premise broken: the Prober took the api port even though the blocker "
        "holds it, so this does not measure the un-held case")
    assert prober._port_busy(api_port) is True, (
        "with no hold on this port the sensor must ask the kernel, or the (D) "
        "port-flip check silently stops existing")
    assert opened == [("127.0.0.1", api_port)], opened


# ── the (D) port check must not read the operator's live api state ────────
#
# t_e5a9469e. `--help bound the api port 8787` was reported on an innocent diff
# by a sensor that asked `connect_ex("127.0.0.1", API_DEFAULT_PORT)` whenever
# its own defensive hold failed to acquire that port. MEASURED
# (card t_e5a9469e, and re-measurable in seconds by the first test below):
# while the hold is UP the check is unfireable (a port we hold cannot be bound
# by anyone), and when the hold FAILS the answer belongs to whoever owns the
# port — on this host that includes another audit run's own defensive hold,
# which was holding 127.0.0.1:8787 AND :8788 bound-without-listen while this
# card's tests were written (pid 35264, a concurrent `scripts` leg:
# `bind failed: EADDRINUSE`, `connect_ex=EAGAIN in 0.252s`). Same host state,
# silent when the listener predates the walk and loud when it appears during
# it — which is why the same tree passed alone and failed in a full leg.
#
# The seal: the sensor measures `Prober.sensor_port`, an ephemeral port this
# walk reserved, verified free, and pinned into the sandbox `~/.hscc/api.json`
# every probe child reads. A flip there can only be this walk's own probe.

def test_host_api_state_cannot_move_the_port_finding(tmp_path, monkeypatch,
                                                     request):
    """The incident, replayed: a foreign listener on the api port is NOT a finding.

    Reconstructs the exact state that manufactured five `--help bound the api
    port` findings on t_776d0cb8's gate: the api port bound-without-listen when
    the walk starts (so the defensive hold FAILS and the old baseline read
    "free"), turning into a real listener before the probe runs. Then asks the
    same probe the incident blamed — `hscc project init -h` — three times,
    because the finding is per-probe and one innocent probe must stay clean.

    Pre-fix this returns True (measured: the same five surfaces, verbatim). The
    companion below is the positive control — the same sensor DOES fire when
    the port it owns gets bound — so this cannot pass by being blind.
    """
    import socket

    api_port = _free_loopback_port()
    # The hold only ever binds the defended ports, so the blocker has to sit on
    # one of them for the replay to be the incident's posture.
    monkeypatch.setattr(audit_mod, "API_DEFAULT_PORT", api_port)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", api_port))         # the incident's S2
    request.addfinalizer(blocker.close)
    prober = _make_prober(tmp_path, request)
    # The hold must genuinely fail for the replay to mean anything: pre-fix,
    # that failure is what silently switched the sensor to reading live state.
    assert api_port not in {s.getsockname()[1]
                            for s in prober._tripwire_socks}, (
        f"premise broken: the Prober holds the test api port {api_port}, so "
        "this does not replay the posture the incident was in")
    assert prober.port_busy_at_start is False, (
        "premise broken: the walk baselined the api port as busy, so the old "
        "sensor would have stayed silent here for the wrong reason")
    blocker.close()
    listener = socket.socket()
    listener.bind(("127.0.0.1", api_port))        # the incident's S3
    listener.listen(8)
    request.addfinalizer(listener.close)
    assert prober._port_busy(api_port) is True, (
        "premise broken: the foreign listener is not answering, so the hostile "
        "posture was never established")

    for _ in range(3):
        r = prober.run(["project", "init", "-h"], 120)
        assert r["port_flip"] is False, (
            f"{' '.join(r['argv'])} (a help path that binds nothing) was "
            f"charged with binding the api port because a FOREIGN process "
            f"listens on {api_port}. The (D) port check may only measure "
            f"Prober.sensor_port ({prober.sensor_port}), which this walk owns "
            f"end to end (t_e5a9469e).")


def test_port_flip_fires_when_a_probe_binds_the_sealed_port(tmp_path, request):
    """TEETH: a stray server on the sealed port is still a finding.

    The seal moves the sensor off the operator's port; it must not move it off
    the phenomenon. This is the positive control for the test above — the same
    `Prober.run`, the same probe surface, and a listener on the port the walk
    actually measures (planted here; in the regression it guards, a --help
    whose intercept is lost and forks the server, which resolves THIS port
    because the sandbox pins it). Blind the sensor and this goes red.
    """
    import socket

    prober = _make_prober(tmp_path, request)
    listener = socket.socket()
    listener.bind(("127.0.0.1", prober.sensor_port))
    listener.listen(8)
    request.addfinalizer(listener.close)

    r = prober.run(["project", "init", "-h"], 120)
    assert r["port_flip"] is True, (
        f"the sealed api port {prober.sensor_port} — the one port the (D) port "
        "check is allowed to measure — was listening and the probe reported no "
        "flip: the check is dead, which is the other way this gate lies")
    assert r["sensor_port"] == prober.sensor_port


def test_port_sensor_never_opens_a_socket_to_a_defended_port(tmp_path,
                                                             monkeypatch,
                                                             request):
    """The sensor may not talk to 8787/8788 at all — defence, not measurement.

    The defensive hold stays (it is what keeps a stray grandchild off the
    operator's machine), but reading those ports is what made the finding a
    function of host state. This pins the separation by recording every
    `connect_ex` a walk makes across construction and a real probe: the only
    port it may name is the sealed one.
    """
    real_socket = audit_mod.socket.socket
    asked: list = []

    class _Spy(real_socket):
        def connect_ex(self, addr):
            asked.append(addr)
            return real_socket.connect_ex(self, addr)

    api_port = _free_loopback_port()
    smoke_port = _free_loopback_port()
    monkeypatch.setattr(audit_mod, "API_DEFAULT_PORT", api_port)
    monkeypatch.setattr(audit_mod, "API_SMOKE_PORT", smoke_port)
    monkeypatch.setattr(audit_mod.socket, "socket", _Spy)
    prober = _make_prober(tmp_path, request)
    r = prober.run(["project", "init", "-h"], 120)

    assert r["port_flip"] is False, r
    wrong = sorted({p for p in asked if p[1] in (api_port, smoke_port)})
    assert wrong == [], (
        f"the walk connected the defended api ports {wrong} — those two are "
        "hold-only since t_e5a9469e: reading them makes a --help finding depend "
        "on what the operator's api (or a sibling audit) is doing")
    assert ("127.0.0.1", prober.sensor_port) in asked, (
        "the sensor never asked its own sealed port either — an all-zero "
        "instrument is an unplanted probe, not a passing test")


def test_probe_child_resolves_the_sealed_api_port(tmp_path, request):
    """The seal must pin the SOURCE, not just the sensor.

    `hscc-api/api_server.py` resolves `defaults <- ~/.hscc/api.json <- flags`,
    and every probe runs with HOME=<sandbox>, so the seeded api.json is what a
    probe child would bind if a --help ever forks the server. Proved through
    the real CLI rather than by reading the file back: `hscc api status` prints
    the address it resolved, and under the walk's own env it must print the
    sealed port — not 8787. Drop the seed and the child resolves DEFAULT_PORT
    again, which is the posture where a real leak is invisible to the gate.
    """
    prober = _make_prober(tmp_path, request)
    seed = prober.sandbox / ".hscc" / "api.json"
    assert seed.exists(), (
        "the walk seeded no api config into the sandbox: a probe child then "
        "resolves DEFAULT_PORT (8787), so a genuine --help leak lands on a "
        "port the sensor does not read (t_e5a9469e)")
    assert json.loads(seed.read_text())["port"] == prober.sensor_port
    assert prober.sensor_port not in (audit_mod.API_DEFAULT_PORT,
                                      audit_mod.API_SMOKE_PORT)

    r = prober.run(["api", "status"], 120)   # the verb that reports its port
    assert r["rc"] == 0, r["err"][-400:]
    assert f"127.0.0.1:{prober.sensor_port}" in (r["out"] + r["err"]), (
        f"`hscc api status` under the walk's own environment did not resolve "
        f"the sealed port {prober.sensor_port}; output: "
        f"{(r['out'] + r['err'])[:300]!r}")
    assert "8787" not in (r["out"] + r["err"]), (
        "a probe child still names the operator's default api port — the seal "
        "is not holding for this surface")



# ── Probe fan-out (t_71f9fc17) ───────────────────────────────────────────
#
# The audit's ~945 probe children used to launch one at a time, and at the queue
# gate's background QoS that walk WAS the `scripts` leg (397.77 s of the module
# fixture, t_49827b5d). The fan-out buys wall time by giving each worker its OWN
# sandbox, canary tripwire log and sealed api port — the three things
# `Prober.run` brackets every spawn against, and the reason a thread pool over
# ONE Prober would race the (C)/(D) sensors instead of speeding them up.
# These pins hold the two properties that make it safe: workers cannot see each
# other's sensors, and the walk still pays for EXACTLY the planned launches, so a
# fan-out failure costs seconds and never a finding.

#: Two surfaces, 11 probe keys — enough to have a real plan, small enough that a
#: test which actually spawns the plan costs a second, not the 397 s the fixture
#: costs. `(group, argv, style, kind)`, the shape `build_surface` returns.
_TINY_SURFACE = [
    ("top-level", ["status"], "plain", "flag"),
    ("top-level", ["help", "status"], "plain", "help"),
]

#: The plan size for `_TINY_SURFACE`: 3 invocations × 3 widths + 1 × 2.
_TINY_PLAN = 11


def _stub_pre_walk_probes(monkeypatch):
    """Cut the audit's two PRE-walk probe children out of a tiny run.

    The engine-router probe and the systemic import probe are pinned by their own
    cases; in a fan-out test they would only add interpreters and a second
    sandbox to what is about the plan.
    """
    monkeypatch.setattr(audit_mod, "_orch_router_verbs",
                        lambda *_a, **_k: ({"inject", "waker"}, None),
                        raising=True)
    monkeypatch.setattr(audit_mod, "_systemic_import_probe",
                        lambda *_a, **_k: None, raising=True)


class _SubprocessShim:
    """Stand-in for the `subprocess` MODULE inside the audit, recording spawns.

    Patched onto `audit_mod.subprocess` rather than the real module, so nothing
    else in the process — pytest's own machinery included — is spied on or
    slowed. `TimeoutExpired` rides along because `Prober.run` catches it out of
    this attribute, not out of the real module.
    """

    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self):
        self.calls: list = []

    def run(self, *args, **kwargs):
        argv = list(args[0]) if args else list(kwargs.get("argv", []))
        self.calls.append(argv)
        return subprocess.run(*args, **kwargs)


def _probe_launches(calls: list) -> list:
    """Only the CLI probe children: argv = [PY, "-m", "hscc_daemon.hscc", ...]."""
    return [c for c in calls if len(c) > 2 and c[1:3] == ["-m", "hscc_daemon.hscc"]]


def test_probe_workers_env_is_bounded(monkeypatch):
    """Default 4, `1` = the serial walk, garbage = serial, and a hard cap.

    The cap is not cosmetics: `run_tests.sh` can run sibling legs concurrently
    (t_6c2123f9), so an unbounded fan-out is a way to starve the rest of the
    queue at the gate.
    """
    monkeypatch.delenv(audit_mod.PROBE_WORKERS_ENV, raising=False)
    assert audit_mod.probe_workers() == audit_mod.DEFAULT_PROBE_WORKERS == 4
    for raw, want in (("1", 1), ("2", 2), ("4", 4), ("0", 1), ("-3", 1),
                      ("junk", 1), ("", 4), ("  3 ", 3),
                      ("999", audit_mod.MAX_PROBE_WORKERS)):
        monkeypatch.setenv(audit_mod.PROBE_WORKERS_ENV, raw)
        assert audit_mod.probe_workers() == want, f"env {raw!r}"


def test_probe_plan_is_exactly_what_the_walk_asks(monkeypatch):
    """The prefetch list may not drift from what the serial walk actually asks.

    Drift is the silent failure mode of any prefetch design. Missing keys are
    survivable by construction (the walk pays one serial probe), but a plan built
    from a different width set than the walk asks means the fan-out has quietly
    stopped covering the walk while still looking green. Both directions are
    measured over the REAL surface with `Prober.run` stubbed, so nothing spawns:
    the walk's own asks, in its own order, against `probe_plan`.
    """
    asked: list = []

    class _StubProber(audit_mod.Prober):
        def run(self, argv, width):
            asked.append((tuple(argv), width))
            return {"argv": list(argv), "width": width, "rc": 0, "hung": False,
                    "out": "", "err": "", "secs": 0.0, "new_files": [],
                    "trips": [], "plist_created": False, "port_flip": False,
                    "sensor_port": self.sensor_port}

    captured: dict = {}
    real_build_surface = audit_mod.build_surface

    def _capture_build_surface():
        built = real_build_surface()
        captured["paths"] = built[0]
        return built

    def _no_prefetch(*_a, **_k):
        # Without this the fan-out's cache seeding would answer the walk and the
        # recording would describe the PLAN, not the walk — a tautology.
        return ({"requested_workers": 1, "workers": 1, "planned": 0,
                 "prefetched": 0, "worker_errors": [],
                 "probe_prefetch_misses": 0}, [])

    monkeypatch.setattr(audit_mod, "Prober", _StubProber, raising=True)
    monkeypatch.setattr(audit_mod, "build_surface", _capture_build_surface,
                        raising=True)
    monkeypatch.setattr(audit_mod, "prefetch_renders", _no_prefetch, raising=True)
    _stub_pre_walk_probes(monkeypatch)
    rep = audit_mod.audit(str(REPO))

    plan = audit_mod.probe_plan(captured["paths"])
    assert rep["probes"] >= MIN_PROBES, (
        f"the walk stopped probing surfaces ({rep['probes']} < {MIN_PROBES}) — "
        "this comparison is then measuring the wrong surface")
    assert len(plan) == len(set(plan)), (
        "probe_plan must dedupe the way Prober.run's cache does — a repeated key "
        "would make one probe look like two")
    assert list(dict.fromkeys(asked)) == plan, (
        "the walk and the prefetch plan disagree: a walk ask missing from the "
        "plan is a serial probe the fan-out never covered, an extra plan key is "
        "a wasted interpreter. Neither may happen silently. "
        f"walk-only={sorted(set(asked) - set(plan))[:5]!r} "
        f"plan-only={sorted(set(plan) - set(asked))[:5]!r}")
    assert {w for _a, w in plan} <= set(audit_mod.WIDTHS_ALL), (
        "the plan asks a width the audit does not define — (B) would silently "
        "stop measuring a mandated width")


def test_fan_workers_own_isolated_sensors(tmp_path, monkeypatch, request):
    """Two probes may not share a sandbox, a canary log or a sealed port.

    This is the whole reason the fan-out is over Probers: `Prober.run` diffs
    `files_before`/`trips_before`/`port_flip` against ITS OWN state, so workers
    that share any of the three manufacture (C)/(D) findings out of each other's
    side effects.
    """
    sandbox = tmp_path / "home"
    sandbox.mkdir(parents=True, exist_ok=True)
    main_canary = audit_mod.make_canary_dir(tmp_path)
    audit_mod._seed_sandbox(sandbox)
    shared = audit_mod._SharedSensors()
    real_home = Path(os.path.expanduser("~"))
    workers = [audit_mod.Prober(sandbox, main_canary, real_home, shared=shared)]
    workers += [audit_mod._make_fan_prober(tmp_path, i, shared) for i in (1, 2, 3)]
    for p in workers:
        request.addfinalizer(p.close)
    try:
        assert len({str(p.sandbox) for p in workers}) == 4
        assert len({str(p.canary / "trips.log") for p in workers}) == 4
        assert len({p.sensor_port for p in workers}) == 4, (
            "two sealed sensor ports the same: one worker's stray server would "
            "read as a port flip in another worker's (D) findings")
        assert all(p._shared is shared for p in workers)
        # Every worker root stays UNDER the audit's one tmp root, because `add()`
        # normalises str(tmp) -> <SANDBOX>. Outside it, a finding detail could
        # carry a per-run temp path into the bipartite expectation table.
        assert all(str(p.sandbox).startswith(str(tmp_path)) for p in workers[1:]), (
            "a fan sandbox outside the audit's tmp root escapes the <SANDBOX> "
            "normalisation in add()")
        for p in workers:
            sealed = json.loads((p.sandbox / ".hscc" / "api.json").read_text())
            assert sealed["port"] == p.sensor_port, (
                "this child would resolve someone else's sealed port and the "
                "port sensor would be measuring a port it does not own")
        # A trip recorded by one worker's canary is invisible to the others...
        with open(workers[1].canary / "trips.log", "a") as fh:
            fh.write("sparkrun whoami\n")
        assert len(workers[1]._trips()) == 1
        assert all(len(p._trips()) == 0 for p in workers if p is not workers[1]), (
            "canary trips are landing in a shared log — the fan-out would hide a "
            "(D) 'shell out' finding from the bracket that spawned it")
        # ...and the child env names its OWN log even after the gate process's
        # global moves, which is exactly what `make_canary_dir` used to do.
        foreign = tmp_path / "foreign" / "trips.log"
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text("")
        monkeypatch.setenv("AUDIT_CANARY_LOG", str(foreign))
        for p in workers:
            assert p._env(80)["AUDIT_CANARY_LOG"] == str(p.canary / "trips.log"), (
                "probe children inherit the gate process's canary log: with N "
                "workers a trip lands in whichever log the global pointed at "
                "when the child spawned — a (D) finding nobody reads")
    finally:
        for p in workers:
            p.close()


def test_plist_sensor_reports_one_creation_for_the_whole_audit(tmp_path):
    """The LaunchAgents check is per-AUDIT, because its HOME is shared.

    `~/Library/LaunchAgents/com.hscc.api.plist` is in the operator's real home —
    the one sensor no Prober can own. N concurrent brackets would each see
    "absent before, present after" for ONE creation and emit N (D) findings under
    N surface names, where the serial walk emits exactly one.
    """
    shared = audit_mod._SharedSensors()
    home = tmp_path / "real-home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    plist = home / "Library" / "LaunchAgents" / audit_mod.API_PLIST_NAME
    plist.write_text("already there before the walk started\n")
    # A file that predates the bracket is never a creation.
    assert [shared.plist_created(True, home) for _ in range(3)] == [False] * 3
    # A file that appeared IS reported — exactly once, across every worker.
    assert [shared.plist_created(False, home) for _ in range(3)] == [
        True, False, False]


def test_fan_out_covers_the_plan_and_the_walk_relaunches_nothing(
        monkeypatch):
    """The acceptance property, on a 2-surface surface: 11 keys, 11 children.

    One child per planned key for the WHOLE audit means the prefetch covered the
    walk and the walk re-spawned none of it — the speedup without a second
    interpreter anywhere. `probes` (the number the gate floors on) is untouched.
    """
    monkeypatch.setattr(audit_mod, "build_surface",
                        lambda: (list(_TINY_SURFACE), {}), raising=True)
    monkeypatch.setenv(audit_mod.PROBE_WORKERS_ENV, "4")
    _stub_pre_walk_probes(monkeypatch)
    shim = _SubprocessShim()
    monkeypatch.setattr(audit_mod, "subprocess", shim, raising=True)
    rep = audit_mod.audit(str(REPO))

    assert rep["probe_fan_errors"] == [], rep["probe_fan_errors"]
    assert rep["probe_workers"] == 4, (
        f"the fan-out collapsed to {rep['probe_workers']} worker(s) — the "
        "isolation pins above prove the machinery, not that the walk used it")
    assert rep["probe_plan"] == _TINY_PLAN
    assert rep["probes_prefetched"] == _TINY_PLAN
    assert rep["probe_prefetch_misses"] == 0
    assert rep["probes"] == 4, "the (A)/(B)/(C)/(D) probe count must not move"
    launches = _probe_launches(shim.calls)
    assert len(launches) == rep["probe_plan"], (
        f"{len(launches)} CLI children for a {rep['probe_plan']}-key plan: the "
        "walk re-spawned prefetched renders, or the plan under-covered it")


def test_a_worker_that_cannot_be_built_costs_time_not_probes(monkeypatch):
    """A fan worker that cannot reserve a sealed port must not lose a probe.

    The fail-safe half: the plan's keys simply become cache misses, so the walk
    pays for them serially and every finding still lands — slower, never blind.
    """
    monkeypatch.setattr(audit_mod, "build_surface",
                        lambda: (list(_TINY_SURFACE), {}), raising=True)
    monkeypatch.setenv(audit_mod.PROBE_WORKERS_ENV, "4")

    def _no_worker(*_a, **_k):
        raise RuntimeError("cannot reserve a free loopback api sensor port")

    monkeypatch.setattr(audit_mod, "_make_fan_prober", _no_worker, raising=True)
    _stub_pre_walk_probes(monkeypatch)
    shim = _SubprocessShim()
    monkeypatch.setattr(audit_mod, "subprocess", shim, raising=True)
    rep = audit_mod.audit(str(REPO))

    assert rep["probe_workers"] == 1, (
        f"a walk that could not build a worker reported {rep['probe_workers']} "
        "workers — the posture would be a lie in the report")
    assert rep["probe_fan_errors"], (
        "the degradation is invisible: a serial walk reported by a fan-out "
        "report is exactly the silent fallback this card forbids")
    assert rep["probes_prefetched"] == 0
    assert rep["probes"] == 4 and rep["probe_plan"] == _TINY_PLAN
    launches = _probe_launches(shim.calls)
    assert len(launches) == rep["probe_plan"], (
        f"{len(launches)} children for {_TINY_PLAN} planned keys — the failed "
        "fan-out lost probes instead of falling back to serial")


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
