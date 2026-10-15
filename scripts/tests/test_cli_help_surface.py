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
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "scripts"))

import hscc_cli_help_audit as audit_mod  # noqa: E402

EXPECTED_PATH = HERE / "cli_help_expected.json"

# Surface floors. If enumeration shrinks, the walker broke (or commands were
# removed without amending the audit) — that must fail, not silently pass.
MIN_SURFACE_PATHS = 150
MIN_PROBES = 180
MIN_EXAMPLES = 60


@pytest.fixture(scope="module")
def report():
    """One audit run shared by every assertion (full CLI walk ~35 s)."""
    return audit_mod.audit(str(REPO))


@pytest.fixture(scope="module")
def rules():
    data = json.loads(EXPECTED_PATH.read_text())
    return data["rules"]


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
    test run never dirties the tree.
    """
    if not os.environ.get("HSCC_CLI_HELP_WRITE_ARTIFACT"):
        pytest.skip("set HSCC_CLI_HELP_WRITE_ARTIFACT=1 to emit the findings "
                    "artifact (docs/audits/artifacts/CLI_HELP_FINDINGS.json)")
    out = REPO / "docs" / "audits" / "artifacts" / "CLI_HELP_FINDINGS.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    assert out.exists()
