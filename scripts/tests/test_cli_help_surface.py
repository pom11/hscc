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
        # "Candidate" is not "runnable": an entry may point into a HOME that
        # does not exist (sandboxed-HOME tests, another operator's absolute
        # path baked into the list). Only a real file is a sibling.
        if not os.path.exists(path):
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
