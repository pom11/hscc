"""The touched-path -> leg map must cover the live roster (t_23bf61f3).

What this pins, and why each one is a real risk rather than ceremony:

  1. **Every leg in scripts/run_tests.sh's DIRS array is selected by a change
     inside its own directory.** A leg added to the runner without a map entry
     would silently never run on CI — the same "green because never executed"
     failure mode that let #443/#446 merge with hscc-api red, re-created inside
     the thing built to stop it. This is the card's named gate test.
  2. The roster is READ from the runner, not copied here or in the selector, so
     there is one list and a drift between them is a hard error.
  3. A path no rule claims widens to the FULL suite and names the offender.
     Under-selection is the dangerous direction; over-selection only costs time.
  4. The harness (.github/, the runner itself, requirements, install/) widens to
     everything, because a broken harness makes every leg green for the wrong
     reason and a per-leg map is structurally blind to that.
  5. The cross-leg edges (hscc_daemon is imported by three other legs;
     hscc_daemon tests read scripts/address_guard.py) are wired.

Executed, not text-matched: every case calls the shipped selector's
``select_legs`` / CLI, the same way .github/scripts/tests/test_ci_log.py drives
the real ci_log.sh.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
CI_SCRIPTS = HERE.parent
REPO = CI_SCRIPTS.parents[1]
RUNNER = REPO / "scripts" / "run_tests.sh"
SELECTOR = CI_SCRIPTS / "leg_select.py"

_spec = importlib.util.spec_from_file_location("leg_select_under_test", SELECTOR)
assert _spec is not None and _spec.loader is not None
leg_select = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(leg_select)


def _roster_from_runner() -> list[str]:
    """Independent parse of the runner's DIRS array (a copy of the selector's
    parse would pass even if both were wrong about the same line)."""
    text = RUNNER.read_text(encoding="utf-8")
    marker = "DIRS=("
    start = text.index(marker) + len(marker)
    body = text[start : text.index(")", start)]
    return body.split()


def _sel(*paths: str) -> list[str]:
    return leg_select.select_legs(list(paths))[0]


def test_the_roster_this_covers_is_the_runners_own_roster():
    assert leg_select.roster() == _roster_from_runner()
    # Non-vacuity: an empty or one-element parse would make every loop below
    # pass while covering nothing.
    assert len(leg_select.roster()) == 12, leg_select.roster()


@pytest.mark.parametrize("leg", leg_select.roster())
def test_every_leg_in_the_roster_is_covered_by_a_change_in_its_own_dir(leg):
    """The card's gate: no leg may be selectable-by-accident only."""
    assert leg in _sel(f"{leg}/whatever.py"), f"{leg} is unreachable from its own dir"
    assert leg in _sel(f"{leg}/tests/test_thing.py"), f"{leg} is unreachable from its tests"


def test_the_roster_is_not_hardcoded_in_the_selector():
    """An unknown leg handed to the selector as a roster is still honoured, which
    is only true because the per-dir rules are GENERATED from the roster."""
    chosen, _ = leg_select.select_legs(["brand_new_leg/x.py"], legs=["brand_new_leg", "scripts"])
    assert chosen == ["brand_new_leg"]


def test_an_unattributable_path_widens_to_the_full_suite_and_says_why():
    all_legs = leg_select.roster()
    chosen, widen = leg_select.select_legs(["totally-new-top-level/app.py"])
    assert chosen == all_legs
    assert widen == ["totally-new-top-level/app.py"]


def test_a_single_unattributable_path_among_known_ones_widens_everything():
    # The conservative half of the contract: one unknown path is not allowed to
    # shrink to the legs the other paths did claim.
    assert _sel("hscc-api/app.py", "mystery/x.py") == leg_select.roster()


@pytest.mark.parametrize(
    "path",
    [
        "scripts/run_tests.sh",
        "scripts/leg_heartbeat.sh",
        ".github/workflows/pytest-legs.yml",
        ".github/scripts/leg_select.py",
        "requirements.txt",
        "requirements-dev.txt",
        "install/install_payload.py",
        "conftest.py",
        ".githooks/pre-commit",
    ],
)
def test_harness_paths_run_every_leg(path):
    assert _sel(path) == leg_select.roster(), f"{path} is harness, it cannot be scoped"


def test_hscc_daemon_widens_to_the_legs_that_import_it():
    # Provenance in leg_select.py's docstring; three legs import the daemon
    # library out of their own tests.
    chosen = _sel("hscc_daemon/health.py")
    assert {"hscc_daemon", "hscc-cluster", "hscc-project", "hscc-api"} <= set(chosen)


def test_scripts_widens_to_the_daemon_leg_that_reads_the_guard():
    # Roster order: hscc_daemon sits ahead of scripts in DIRS.
    assert _sel("scripts/address_guard.py") == ["hscc_daemon", "scripts"]


def test_a_changelog_fragment_selects_only_the_repo_lint_leg():
    """Every PR adds a changelog.d fragment, so if that widened to everything the
    map would buy nothing. The legs that mention changelog.d only CITE a fragment
    in a docstring; scripts/tests is the leg that parses them."""
    assert _sel("changelog.d/t_23bf61f3.md") == ["scripts"]


def test_selection_is_in_roster_order_and_deduplicated():
    chosen = _sel("hscc-api/app.py", "hscc-api/other.py", "hscc-roles/r.py", "hscc-api/x.py")
    assert chosen == ["hscc-roles", "hscc-api"], "order must follow the roster, not the diff"


def test_docs_only_changes_run_the_repo_lint_leg():
    assert _sel("docs/audits/something.md", "README.md") == ["scripts"]


@pytest.mark.parametrize(
    "path",
    [
        # app-build-checks compiles the Swift; the ONLY python reader of these
        # trees is scripts/tests (test_site_landing.py:46,49-50,305-347).
        "macos-app/Sources/HSCCMac/DashboardView.swift",
        "macos-app/scripts/publish_macos.sh",
        "macos-app/site/index.html",
        "ios-app/Sources/HSCC/HSCCClient.swift",
        "ios-app/scripts/publish_pages.sh",
        "ios-app/Assets.xcassets/AppIcon.appiconset/AppIcon.png",
    ],
)
def test_apple_client_changes_run_only_the_publisher_leg(path):
    """A Swift-only PR must not pay the 25-minute roster, and must not pay zero
    legs either — scripts/tests executes the publishers."""
    assert _sel(path) == ["scripts"]


# ── the CLI, executed ────────────────────────────────────────────────────────


def test_cli_writes_the_output_contract_a_workflow_reads(tmp_path):
    paths = tmp_path / "changed.txt"
    paths.write_text("hscc-api/api_server.py\nchangelog.d/t_x.md\n", encoding="utf-8")
    out = tmp_path / "gh_output"
    proc = subprocess.run(
        [sys.executable, str(SELECTOR), "--paths-file", str(paths), "--github-output", str(out)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    written = out.read_text(encoding="utf-8")
    assert "legs=hscc-api scripts\n" in written, written
    assert "full_suite=false\n" in written, written
    # ...and the human-readable half, because a job log has to answer "why these
    # legs" without re-running anything.
    assert "SELECTED 2/12" in proc.stdout, proc.stdout


def test_cli_reports_a_widening_in_the_log(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SELECTOR), "--path", "brand-new/x.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "widening to the FULL suite" in proc.stdout
    assert "brand-new/x.py" in proc.stdout


def test_cli_fails_closed_when_the_roster_cannot_be_read(tmp_path):
    # A selector that quietly emitted an empty leg list would hand CI a job that
    # runs zero tests and reports green.
    with pytest.raises((OSError, RuntimeError)):
        leg_select.roster(tmp_path / "no-such-runner.sh")
    missing = tmp_path / "empty.txt"
    missing.write_text("", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SELECTOR), "--paths-file", str(tmp_path / "definitely-absent.txt")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
