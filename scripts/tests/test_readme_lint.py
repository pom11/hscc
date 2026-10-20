"""R7 README-lint gate (audit docs/audits/README_REVIEW_2026-10-15.md §3).

Executes in the canonical suite via the ``scripts`` leg of
``scripts/run_tests.sh``. The engine is ``scripts/readme_lint.py``; the
fixture repos under ``fixtures/readme_lint/`` pin BOTH directions —
anti-vacuity is the contract here (card t_573287aa):

  * the CLEAN fixture (badge == VERSION, only real commands) must produce
    ZERO findings — the gate is not hallucinating violations;
  * the BROKEN fixture must produce EVERY rule's finding kind — the gate
    is not silently waving the exact defects the audit found through;
  * the REPO's own README set must be CLEAN (rule (1) badge==VERSION and
    rule (2) quoted-command existence green on the post-R1 README set).

The oracle import is the SAME code-derived CLI enumeration the CLI-help
audit walks (hscc_cli_help_audit.ExampleParser) — this suite never
re-implements it, so deleting a real command from the CLI turns
test_repo_readmes_pass_the_lint red by itself, with no fixture edit.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "readme_lint"
sys.path.insert(0, str(REPO / "scripts"))

import readme_lint as lint_mod  # noqa: E402


@pytest.fixture(scope="module")
def oracle():
    """One CLI oracle for the module (builds the real parser tables)."""
    return lint_mod.build_oracle()


def _lint_fixture(name: str, oracle):
    root = FIXTURES / name
    return lint_mod.lint_repo(root, paths=[root / "README.md"],
                              oracle=oracle)


# ── anti-vacuity: the two negative/positive fixture legs ────────────────

def test_clean_fixture_passes_every_rule(oracle):
    """POSITIVE leg: a README with a badge == VERSION and only real
    commands (hscc verbs, allow-listed externals, an existing repo script)
    must yield ZERO findings. If the lint ever flags these forms it is
    misfiring, and this test is where that shows up."""
    rep = _lint_fixture("clean", oracle)
    assert rep["version_expected"] == "9.9.9"
    assert rep["badges_checked"] == 1, "the version badge was not even seen"
    assert rep["commands_checked"] >= 6, (
        f"the clean fixture quotes real commands but only "
        f"{rep['commands_checked']} were checked — classification regressed")
    assert rep["findings"] == [], (
        "clean fixture must pass:\n"
        + "\n".join(f"  [{f['kind']}] {f['quote']}: {f['detail']}"
                    for f in rep["findings"]))


def test_broken_fixture_fails_every_rule(oracle):
    """NEGATIVE leg (the anti-vacuity proof): both rules must FIRE on the
    broken fixture —
      * BADGE-VERSION  : badge 1.2.3 vs VERSION 7.7.7 (the audit's
        motivating drift, root README badge vs VERSION);
      * CMD-UNKNOWN    : `hscc frobnicate --now` (the audit X-column
        `hscc doctor` shape) — a fake command must not wave through;
      * PATH-MISSING   : `python3 scripts/ghost_check.py` — a README
        pointing at a deleted script must not wave through.
    The matching release badge (== VERSION) must NOT be flagged: exactly
    one BADGE-VERSION finding proves the check is selective, and the
    fenced fake command must NOT be flagged (scope discipline)."""
    rep = _lint_fixture("broken", oracle)
    by_kind = {}
    for f in rep["findings"]:
        by_kind.setdefault(f["kind"], []).append(f)
    assert set(by_kind) == {"BADGE-VERSION", "CMD-UNKNOWN", "PATH-MISSING"}, (
        f"expected exactly one finding per rule, got "
        f"{ {k: len(v) for k, v in by_kind.items()} }: "
        + "\n".join(f"  [{f['kind']}] {f['quote']}: {f['detail']}"
                    for f in rep["findings"]))
    assert len(by_kind["BADGE-VERSION"]) == 1, (
        "exactly the mismatched badge fires — the matching release badge "
        "proves the check is selective, not blanket")
    assert "frobnicate" in by_kind["CMD-UNKNOWN"][0]["quote"]
    assert "ghost_check.py" in by_kind["PATH-MISSING"][0]["quote"]
    assert not any("also-made-up" in f["quote"] for f in rep["findings"]), (
        "a fake command inside a FENCED block leaked into the findings — "
        "fenced scope discipline broke")


def test_broken_fixture_rc_and_clean_fixture_rc(oracle, tmp_path):
    """CLI surface: --strict exits 1 on the broken fixture, 0 on the clean
    one (the exit-code contract address_guard.py also follows)."""
    rep_bad = _lint_fixture("broken", oracle)
    rep_ok = _lint_fixture("clean", oracle)
    assert rep_bad["findings"] and not rep_ok["findings"]
    # main() re-derives its own report; point it at each fixture repo
    rc_bad = lint_mod.main(["--repo", str(FIXTURES / "broken"),
                            "--files", str(FIXTURES / "broken" / "README.md"),
                            "--strict"])
    rc_ok = lint_mod.main(["--repo", str(FIXTURES / "clean"),
                           "--files", str(FIXTURES / "clean" / "README.md"),
                           "--strict"])
    assert (rc_bad, rc_ok) == (1, 0)


# ── the repo's own README set (the R1-dependent acceptance leg) ─────────

def test_repo_readmes_pass_the_lint(oracle):
    """REPO leg: every tracked README must satisfy both rules — every
    version badge equals VERSION, and every quoted command exists on the
    real CLI surface (audit X column included: `hscc doctor` is gone).
    This is the leg that depends on the R1–R6 README set landing."""
    rep = lint_mod.lint_repo(REPO, oracle=oracle)
    assert rep["version_expected"] == (REPO / "VERSION").read_text().strip(), \
        "VERSION resolution drifted from the file"
    assert rep["badges_checked"] >= 1, (
        "the root README carries a version badge — a 0 count means badge "
        "PARSE regression (the whole rule would be silently vacuous)")
    assert rep["commands_checked"] >= 250, (
        f"the README set quotes far more commands than "
        f"{rep['commands_checked']} — command extraction regressed and the "
        "gate would be silently vacuous")
    assert rep["findings"] == [], (
        f"{len(rep['findings'])} README lint finding(s):\n"
        + "\n".join(f"  [{f['kind']}] {f['file']}:{f['line']} "
                    f"`{f['quote']}` — {f['detail']} (fix: {f['fix']})"
                    for f in rep["findings"]))


# ── unit pins: each classification decision the gate leans on ───────────

def _cls(tok, oracle, readme=None, repo=None, names=None):
    repo = repo or REPO
    readme = readme or repo / "README.md"
    names = names if names is not None else lint_mod.tracked_basenames(repo)
    return lint_mod.classify_token(tok, readme, repo, names, oracle)


@pytest.mark.parametrize("tok", [
    "hscc status",
    "hscc cluster status",
    "hscc template validate 4node-balanced --structural-only",
    "hscc cluster up/down --dry-run",          # every half is a real sub
    "hscc project review <card>",              # verb + placeholder arg
    "hscc project X",                          # ALL-CAPS skeleton verb
    "hscc check [stream|all]",                 # alternative-list spelling
    "hscc api start --tailscale",
    "api status",                              # group shorthand, no prefix
    "hermes -p <profile> chat",
    "sparkrun cluster list",
    "git clone",
    "python3 scripts/address_guard.py --tracked",
    "scripts/run_tests.sh",
    "changelog_fragments.py",                  # bare existing script name
])
def test_real_forms_pass(tok, oracle):
    v, _ = _cls(tok, oracle)
    assert v == "pass", f"real form rejected: {tok!r} -> {v}"


@pytest.mark.parametrize("tok", [
    "hscc frobnicate --now",                   # fake verb
    "hscc cluster up/migrate",                 # one half is NOT a real sub
    "python3 scripts/does_not_exist.py",       # fake script
    "frobnicate-cli --please",                 # unknown binary WITH a flag
])
def test_fake_forms_fail(tok, oracle):
    v, payload = _cls(tok, oracle)
    assert v == "fail", f"fake form waved through: {tok!r} -> {v}"
    assert payload[0] in ("CMD-UNKNOWN", "PATH-MISSING", "CMD-BINARY")


@pytest.mark.parametrize("tok", [
    "100.64.0.1",                              # bare address
    "/v1/kanban/blocked",                      # HTTP route
    "/cluster",                                # Hermes slash command
    "~/dev/hscc/hscc-bootstrap/bootstrap.sh",  # home-anchored path
    "hscc://",                                 # URL scheme
    "HTTP/1.1",                                # protocol coordinate
    "feat/hscc-api",                           # branch name
    "Views/AppShell.swift",                    # foreign-layout file ref
    "deps not met",                            # prose
    "hscc = \"hscc_cli:main\"",                # console-script entry (TOML)
    "tp > 1",                                  # expression
])
def test_non_command_forms_skip_not_fail(tok, oracle):
    v, _ = _cls(tok, oracle)
    assert v == "skip", f"non-command flagged as failure: {tok!r} -> {v}"


def test_mutation_badge_drift(oracle, tmp_path):
    """A badge that DRIFTS after the fact is caught: copy the clean fixture
    to tmp, move VERSION under the badge, the finding appears; move the
    badge to match, it clears. (The fixture pair pins the static cases;
    this pins the comparison itself, live.)"""
    src = FIXTURES / "clean"
    root = tmp_path / "drift"
    shutil.copytree(src, root)
    rep = lint_mod.lint_repo(root, paths=[root / "README.md"], oracle=oracle)
    assert rep["findings"] == []
    (root / "VERSION").write_text("9.9.10\n")
    rep = lint_mod.lint_repo(root, paths=[root / "README.md"], oracle=oracle)
    assert [f["kind"] for f in rep["findings"]] == ["BADGE-VERSION"]
    # fix by BADGE (not by VERSION): the second truth source must agree
    (root / "README.md").write_text(
        (root / "README.md").read_text()
        .replace("badge/version-9.9.9", "badge/version-9.9.10"))
    rep = lint_mod.lint_repo(root, paths=[root / "README.md"], oracle=oracle)
    assert rep["findings"] == []


def test_mutation_dead_command(oracle, tmp_path):
    """A command quoted that IS real today, then removed from the quote set:
    the oracle is the CLI itself, so `hscc doctor` (deleted from the
    dispatcher long ago — the audit X column) must FAIL while its live
    sibling `hscc status` passes. This is the anti-vacuity mechanism for
    rule (2): the approved set is not a list someone can quietly stale."""
    v_live, _ = _cls("hscc status", oracle)
    v_dead, payload = _cls("hscc doctor", oracle)
    assert v_live == "pass"
    assert v_dead == "fail" and payload[0] == "CMD-UNKNOWN"


def test_cannot_run_is_never_a_pass(monkeypatch, capsys):
    """rc=2 discipline (same convention as address_guard): if the lint
    raises it must NOT report success."""
    def boom(*a, **k):
        raise RuntimeError("simulated oracle failure")
    monkeypatch.setattr(lint_mod, "build_oracle", boom)
    rc = lint_mod.main(["--repo", str(REPO)])
    assert rc == 2
    assert "CANNOT RUN" in capsys.readouterr().err
