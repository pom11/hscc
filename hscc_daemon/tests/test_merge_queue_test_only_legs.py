"""A wholly test-only diff narrows the local gate to the owning legs.

Card t_1d24f940. These pins are about ``hscc_daemon/merge_queue.py``'s LOCAL
gate only: ``.github/scripts/leg_select.py`` still owns the cross-leg edges and
GitHub CI still runs the full roster on push to main, so the backstop is
unchanged. A mixed diff (a test plus any source path) keeps the ordinary
selection exactly as it was.
"""

from types import SimpleNamespace

import pytest

from hscc_daemon import merge_queue as mq

ROSTER = ("hscc-bootstrap", "hscc_daemon", "hscc-api", "scripts",
          ".github/scripts")


def _legs(paths, roster=ROSTER):
    return mq._local_test_legs(list(paths), list(roster))


def _write_leg_roster(worktree, legs=ROSTER):
    path = worktree / "scripts" / "run_tests.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("DIRS=(" + " ".join(legs) + ")\n", encoding="utf-8")


def _diff_git(*paths):
    def fake_git(argv, cwd=None, **kwargs):
        assert argv[:3] == ["git", "diff", "--name-only"], argv
        return SimpleNamespace(returncode=0,
                               stdout="\n".join(paths) + "\n", stderr="")

    return fake_git


def test_test_only_in_one_leg_selects_only_that_leg():
    assert _legs(["hscc_daemon/tests/test_merge_queue.py"]) == ["hscc_daemon"]


def test_test_only_across_two_legs_selects_those_two():
    assert _legs([
        "hscc_daemon/tests/test_merge_queue.py",
        "hscc-api/tests/test_routes.py",
    ]) == ["hscc_daemon", "hscc-api"]


def test_a_test_beside_its_module_also_counts_as_test_only():
    assert _legs(["hscc_daemon/test_merge_queue.py"]) == ["hscc_daemon"]


def test_a_conftest_under_a_legs_tests_dir_counts_as_test_only():
    assert _legs(["hscc_daemon/tests/conftest.py"]) == ["hscc_daemon"]


def test_test_plus_changelog_stays_on_the_owning_leg():
    assert _legs([
        "hscc_daemon/tests/test_merge_queue.py",
        "changelog.d/t_1d24f940.md",
    ]) == ["hscc_daemon"]


def test_test_plus_one_source_file_keeps_the_ordinary_selection():
    # The test-only shortcut must not fire. In this tree the local gate has no
    # cross-leg edge table (leg_select.py owns that, and CI still runs it), so
    # today's behaviour for this mixed diff is the ordinary owning-leg rule:
    # the source claims hscc_daemon and the fragment claims scripts.
    assert mq._test_only_legs([
        "hscc_daemon/merge_queue.py",
        "hscc_daemon/tests/test_merge_queue.py",
        "changelog.d/t_1d24f940.md",
    ], list(ROSTER)) is None
    assert _legs([
        "hscc_daemon/merge_queue.py",
        "hscc_daemon/tests/test_merge_queue.py",
        "changelog.d/t_1d24f940.md",
    ]) == ["hscc_daemon", "scripts"]


def test_root_conftest_keeps_the_ordinary_selection():
    assert _legs(["conftest.py", "hscc_daemon/tests/test_merge_queue.py"]) is None


@pytest.mark.parametrize("shared", [
    "scripts/run_tests.sh",
    "pytest.ini",
    ".github/workflows/pytest-legs.yml",
])
def test_a_shared_gate_path_keeps_the_ordinary_selection(shared):
    assert _legs([shared, "hscc_daemon/tests/test_merge_queue.py"]) is None


def test_more_than_three_test_only_legs_falls_back_to_the_full_suite():
    assert _legs([
        "hscc-bootstrap/tests/test_x.py",
        "hscc_daemon/tests/test_y.py",
        "hscc-api/tests/test_z.py",
        "scripts/tests/test_w.py",
    ]) is None


def test_a_changelog_only_diff_keeps_todays_scripts_leg():
    # No owned test file: the test-only branch declines rather than selecting
    # zero legs, so the existing fragment -> scripts rule still applies.
    assert _legs(["changelog.d/t_1d24f940.md"]) == ["scripts"]


def test_the_test_only_decision_is_logged(tmp_path):
    _write_leg_roster(tmp_path)
    calls = []

    gates = mq._resolve_gates(
        {"name": "hscc"}, str(tmp_path),
        _diff_git("hscc_daemon/tests/test_merge_queue.py",
                  "changelog.d/t_1d24f940.md"),
        log=lambda message, level: calls.append((message, level)))

    assert gates[1] == ('HSCC_TEST_LEGS="hscc_daemon" '
                        'bash scripts/run_tests.sh')
    assert ("legs: test-only diff -> hscc_daemon", "INFO") in calls
