"""RELEASE BLOCKER regression — the test suite must never write live ~/.hscc.

This is the THIRD leak of this class. First: test_daemon_ops.py arming idle
teardown by writing the real ``~/.hscc/autodown.json``. Second: the suite
leaking into the live ``~/.hscc/watchdog-block.json`` (test_trigger.py /
test_health.py not patching ``lifecycle.WATCHDOG_BLOCK_FILE``), silently
disabling cluster supervision for ~1 hour.

The autouse ``_isolate_hscc`` fixture (see conftest.py) is the primary defense:
it redirects EVERY ``~/.hscc/...`` path to a per-test tmp dir, covering the
directory as a whole. This file is the belt-and-braces regression GUARD: it
spawns the daemon/lifecycle/trigger/autodown test files inside a SANDBOXED
HOME where realistic ``~/.hscc`` files are pre-planted, and asserts that every
one of them is byte-identical afterwards (same set of files, same content).

Why a sandboxed HOME instead of hashing the operator's real ``~/.hscc``: the
live daemon rewrites ``~/.hscc/state/*.json`` on its own 60s tick throughout
the whole run, so real state hashes are a moving target and would produce
false failures. The sandbox is deterministic: the planted files are static and
MUST remain so — so this fails loudly (not flakily) if any future test writes
live state.

Stdlib only.
"""

import hashlib
import os
import subprocess
import sys


def _manifest(hscc_dir):
    """Return path->sha256 for every file under hscc_dir (tree walk)."""
    out = {}
    for root, _dirs, files in os.walk(hscc_dir):
        for fn in files:
            p = os.path.join(root, fn)
            with open(p, "rb") as f:
                out[os.path.relpath(p, hscc_dir)] = hashlib.sha256(
                    f.read()).hexdigest()
    return out


def _plant(ws, hscc_rel, content):
    """Write `content` into sandbox HOME at ~/.hscc/<hscc_rel>, creating
    parent dirs. `ws` is the sandbox HOME; hscc_rel is relative to .hscc."""
    target = os.path.join(ws, ".hscc", hscc_rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w") as f:
        f.write(content)
    return target


def test_suite_never_writes_live_hscc(tmp_path):
    """Run the daemon/lifecycle/trigger/autodown tests in a sandboxed HOME
    with realistic ~/.hscc planted; assert NOTHING under it changes."""
    ws = str(tmp_path / "sandbox_home")
    os.makedirs(ws, exist_ok=True)

    # Plant realistic ~/.hscc top-level + state files. These mirror the real
    # cluster's files (watchdog-block.json, autodown.json, agents.json,
    # lifecycle.json, events.jsonl, triggers.json, cooldowns.json, bridge.json,
    # daemon.pid, daemon.log, orch-endpoint, activity.json, state/dgx.json,
    # gateway.json, watchdog.json, telegram_probe.offset). Any test leaking
    # even one write/delete against these MUST trip the manifest diff.
    _plant(ws, "watchdog-block.json", '{"blocked": false, "failures": []}\n')
    _plant(ws, "autodown.json", '{"enabled": false}\n')
    _plant(ws, "agents.json", '{"agents": []}\n')
    _plant(ws, "lifecycle.json", '{"agents": {}}\n')
    _plant(ws, "events.jsonl", '{"event_type": "planted"}\n')
    _plant(ws, "triggers.json", '{"rules": []}\n')
    _plant(ws, "cooldowns.json", '{}\n')
    _plant(ws, "bridge.json", '{"bridge": "planted"}\n')
    _plant(ws, "daemon.pid", "99999\n")
    _plant(ws, "daemon.log", "[planted] log line\n")
    _plant(ws, "orch-endpoint", "planted\n")
    _plant(ws, "state/dgx.json", '{"ok": true}\n')
    _plant(ws, "state/gateway.json", '{"ok": true}\n')
    _plant(ws, "state/watchdog.json", '{"ok": true}\n')
    # activity.json is EVENT-DRIVEN and lives OUTSIDE state/ (at ~/.hscc root):
    _plant(ws, "activity.json", '{"ts": "planted"}\n')
    _plant(ws, "state/telegram_probe.offset", "0\n")

    before = _manifest(os.path.join(ws, ".hscc"))

    # Run the test files most likely to leak (the historical writers) plus the
    # whole package — those tests exercise cycle(), pipeline_watchdog(),
    # trigger_engine(), save_watchdog_block(), save_config(), etc.
    tests_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))
    targets = [
        "test_daemon_ops.py", "test_lifecycle.py", "test_trigger.py",
        "test_health.py", "test_autodown.py", "test_autodown_cli.py",
        "test_state.py", "test_desktop.py",
    ]
    paths = [os.path.join(tests_dir, t) for t in targets]

    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    env = dict(os.environ)
    env["HOME"] = ws
    # Make hscc_daemon importable in the sandbox regardless of HOME.
    env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *paths, "-p", "no:cacheprovider"],
        env=env, cwd=tests_dir, capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, (
        "sandboxed test run failed:\n" + proc.stdout + proc.stderr)

    after = _manifest(os.path.join(ws, ".hscc"))

    assert before == after, (
        "TEST SUITE WROTE TO LIVE ~/.hscc! Planted sandbox files changed.\n"
        "The autouse _isolate_hscc fixture is NOT covering the whole "
        "directory. See conftest.py._isolate_hscc._module_attrs() and add the "
        "missing path.\n"
        "changed files:\n"
        + "\n".join(
            f"  {p}: before={before.get(p)} after={after.get(p)}"
            for p in sorted(set(before) | set(after))
            if before.get(p) != after.get(p)
        )
    )
