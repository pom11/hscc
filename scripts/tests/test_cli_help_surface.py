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
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "scripts"))

import hscc_cli_help_audit as audit_mod  # noqa: E402

EXPECTED_PATH = HERE / "cli_help_expected.json"
A_COUNTS_PATH = HERE / "cli_help_a_counts.json"

# Surface floors. If enumeration shrinks, the walker broke (or commands were
# removed without amending the audit) — that must fail, not silently pass.
# Probe floor amended 180 -> 178 by t_fc307a51 (F5): the ed-* placeholder trio
# was DELETEd from the CLI (operator fan-out ruling), and the audit's
# enumeration loses the three commands in the same commit — they are gone
# from COMMAND_HELP too, so both the `--help` and the `hscc help` probes go
# (6 paths, 9 probes). 178 is EXACTLY what a healthy surface measures after
# that deletion — zero slack, so any further shrink trips this gate.
MIN_SURFACE_PATHS = 150
MIN_PROBES = 178
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


def _sibling_interpreters() -> list[str]:
    """Interpreters distinct from this one that can run the (A)-only pass.

    The gate must produce the SAME normalised (A) table on every CPython the
    suite runs on (t_cfdcefa9 / GATE-313: argparse 3.13 widened its help column
    by 2 chars and the gate went red on 3.13 only). Candidates: this leg's
    ``HSCC_TEST_PY`` siblings from run_tests.sh, plus PATH ``python3.x``.
    Deduped by realpath so aliases don't re-run the same interpreter."""
    cands = []
    for p in (os.environ.get("HSCC_SIBLING_PY"),
              os.path.expanduser("~/.hermes/hermes-agent/venv/bin/python"),
              "~/miniconda3/envs/p313/bin/python",
              "/Users/desac/miniconda3/envs/p313/bin/python",
              "python3.11", "python3.12", "python3.13"):
        if not p:
            continue
        cands.append(os.path.expanduser(p))
    here = os.path.realpath(sys.executable)
    seen, out = {here}, []
    for c in cands:
        path = c if os.path.isabs(c) else _which(c)
        if not path:
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
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
    """Only 'api start --help' may be skipped (it would fork a live server)."""
    for s in report["skips"]:
        assert "api start" in s["surface"], (
            f"unexpected skip — every help path must actually run: {s}")
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


def test_a_table_identical_under_sibling_interpreters(report):
    """Cross-interpreter pin (the stronger half of GATE-313).

    Runs the audit's (A)-only pass for every surface that currently has an
    (A) line under a SIBLING CPython (one distinct from pytest's own) and
    asserts the tables are equal. argparse 3.13 widened its help column by 2
    chars, which is exactly the class of drift this catches inside a single
    leg — a sibling that renders the same logical line differently can no
    longer hide until its own full-suite leg runs. Skips only when this host
    has no runnable sibling."""
    surfaces = sorted(report["a_counts"])
    siblings = _sibling_interpreters()
    if not siblings:
        pytest.skip("no sibling interpreter distinct from this one is "
                    "runnable on PATH")
    for py in siblings:
        proc = subprocess.run(
            [py, str(REPO / "scripts" / "hscc_cli_help_audit.py"),
             "--repo", str(REPO), "--a-table", ",".join(surfaces)],
            capture_output=True, text=True, timeout=300)
        assert proc.returncode == 0, (
            f"sibling {py} failed the (A)-only pass: {proc.stderr[-400:]}")
        table = json.loads(proc.stdout)
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


def test_styles_recorded(report):
    """(E) every help path carries a style so the convergence card can act."""
    styles = set(report["styles"].values())
    assert {"rich", "argparse", "print"} <= styles, (
        f"expected all three help styles present, got {styles}")


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
