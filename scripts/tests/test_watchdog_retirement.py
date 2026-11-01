"""GAP-B-5 guard: the retired watchdog schedules must stay retired.

Integration audit B (t_72b03930) found that ``hscc_proxy_watchdog.sh`` told the
reader to schedule it every 5 minutes, while the daemon's own ``proxy`` check
stream already supervises that proxy (``PERIODIC_INTERVALS["proxy"]``,
``daemon_ops.py`` -> ``health.check_proxy``) and throttles its relaunches with a
durable cooldown. A cron copy of the script therefore fights the daemon: a
different health rule (reachability vs. catalogue) and no cooldown.

Both scripts are still legitimate as one-shot manual probes. What is retired is
their *scheduling* — so what this test pins is the scheduling surface, not the
scripts' existence:

1. no doc or script header anywhere in the repo recommends registering either
   watchdog on a cron, and
2. the two scripts still exist and still work as manual probes, and
3. the README carries the exact retire commands for the two live cron jobs.

If someone re-adds a ``cron create`` line for either job, this fails — that is
the regression GAP-B-5 is about.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
PROXY_SCRIPT = SCRIPTS / "hscc_proxy_watchdog.sh"
NAS_SCRIPT = SCRIPTS / "hscc_nas_watchdog.sh"
SCRIPTS_README = SCRIPTS / "README.md"

#: The two Hermes cron job names that are retired (audit B, GAP-B-5).
RETIRED_JOBS = ("hscc-proxy-watchdog", "hscc-nas-watchdog")

#: What a "schedule me" recommendation looks like, in prose or in a shell line.
SCHEDULING_PATTERNS = (
    "cron create 'every 5m'   --name 'hscc-proxy-watchdog'",
    "cron create 'every 4h'   --name 'hscc-nas-watchdog'",
    "cron create '5m' --no-agent --script hscc_proxy_watchdog.sh",
    "cron create '4h' --no-agent --script hscc_nas_watchdog.sh",
    "Run via: hermes cron create",
)


def _read(path: Path) -> str:
    assert path.is_file(), f"expected {path} to exist"
    return path.read_text(encoding="utf-8")


def test_scripts_still_exist_as_manual_probes():
    """Retiring the schedule must not delete the probes (card: code-side only)."""
    for path in (PROXY_SCRIPT, NAS_SCRIPT):
        assert path.is_file(), f"{path.name} must stay available as a manual probe"
        assert path.read_text(encoding="utf-8").startswith("#!/bin/bash")


@pytest.mark.parametrize("path", [PROXY_SCRIPT, NAS_SCRIPT, SCRIPTS_README])
def test_no_recommended_cron_schedule_for_retired_watchdogs(path):
    text = _read(path)
    for pattern in SCHEDULING_PATTERNS:
        assert pattern not in text, (
            f"{path.relative_to(REPO_ROOT)} recommends scheduling a retired "
            f"watchdog ({pattern!r}). The daemon owns this supervision — see "
            f"scripts/README.md 'Retiring the superseded watchdog cron jobs'."
        )


def test_proxy_header_states_daemon_owns_supervision():
    """The header must name the owner, not just forbid the old advice."""
    header = "\n".join(_read(PROXY_SCRIPT).splitlines()[:45])
    assert "SUPERVISION OWNER" in header, "proxy watchdog header must name the owner"
    # The concrete proof, not just the prohibition: the interval dict + check fn.
    assert 'PERIODIC_INTERVALS["proxy"]' in header
    assert "check_proxy" in header
    assert "DO NOT SCHEDULE THIS ON A CRON" in header


def test_nas_header_states_daemon_owns_supervision():
    header = "\n".join(_read(NAS_SCRIPT).splitlines()[:30])
    assert "SUPERVISION OWNER" in header
    assert 'PERIODIC_INTERVALS["nas"]' in header
    assert "DO NOT SCHEDULE THIS ON A CRON" in header


def test_readme_carries_retire_commands_for_both_jobs():
    """The card's acceptance: retire commands listed, for both stale entries."""
    text = _read(SCRIPTS_README)
    assert "Retiring the superseded watchdog cron jobs" in text
    for job in RETIRED_JOBS:
        assert job in text, f"{job} must be named in the retire section"
    # Each retired job needs its delete command AND its delete verb.
    assert text.count("cron remove") >= 2, "one retire command per retired job"
    assert "cron list" in text, "look-before-you-delete step required"
    # GAP-B-5 was filed as "cron entries"; the store is Hermes cron, and an
    # operator who greps the OS crontab finds nothing and stops looking.
    assert "crontab" in text, (
        "the retire section must say these are Hermes cron jobs, not OS crontab"
    )


def test_citations_in_the_new_docs_are_real():
    """The headers/README point at code by path — those paths must exist."""
    for rel in ("hscc_daemon/daemon_ops.py", "hscc_daemon/health.py",
                "hscc-bootstrap/triggers.default.json"):
        assert (REPO_ROOT / rel).is_file(), f"cited file gone: {rel}"

    ops = _read(REPO_ROOT / "hscc_daemon" / "daemon_ops.py")
    assert 'PERIODIC_INTERVALS = {' in ops
    assert '"proxy": 60' in ops or '"proxy":60' in ops.replace(" ", "")
    assert '"nas": 900' in ops or '"nas":900' in ops.replace(" ", "")

    health = _read(REPO_ROOT / "hscc_daemon" / "health.py")
    assert "def check_proxy(" in health
    assert "def check_nas(" in health
