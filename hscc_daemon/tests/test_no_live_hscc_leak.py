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


def _plant_at(target, content):
    """Same as `_plant` but for a path given relative to the sandbox HOME
    itself (t_35d9f6ea: ~/Library/LaunchAgents is not under ~/.hscc)."""
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w") as f:
        f.write(content)
    return target


# A minimal but VALID launchd plist carrying the marker the manifest diff
# proves survived (t_35d9f6ea). Content matters: a byte-level manifest diff
# catches an overwrite as well as a removal.
_PLANTED_PLIST = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    '<plist version="1.0">\n<dict>\n'
    '\t<key>planted</key><true/>\n'
    '</dict>\n</plist>\n')


def test_suite_never_writes_live_hscc(tmp_path):
    """Run the daemon/lifecycle/trigger/autodown tests in a sandboxed HOME
    with realistic ~/.hscc planted; assert NOTHING under it changes."""
    ws = str(tmp_path / "sandbox_home")
    os.makedirs(ws, exist_ok=True)

    # Plant realistic ~/.hscc top-level + state files. These mirror the real
    # cluster's files (watchdog-block.json, autodown.json, agents.json,
    # lifecycle.json, events.jsonl, triggers.json, cooldowns.json, bridge.json,
    # daemon.pid, daemon.log, orch-endpoint, activity.json, state/dgx.json,
    # gateway.json, watchdog.json). Any test leaking
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
    # update-check.json (t_a4055572): the 24h GitHub-release cache. Planted so
    # the manifest diff catches BOTH leak shapes — a new file in ~/.hscc and
    # an overwrite of an existing cached check (the status notice and the
    # daemon's daily tick read this file, so plenty of tests touch it).
    _plant(ws, "update-check.json",
           '{"checked_at": "2026-01-01T00:00:00+00:00", '
           '"latest_tag": "v0.0.1"}\n')
    # node_pauses.json (t_28caea71): the durable node-pause overlay. Planted
    # with a live-looking pause so the manifest diff catches BOTH leak shapes —
    # a test that writes the overlay to the real root, and one that overwrites
    # (or clears) a pause the operator actually has in force.
    _plant(ws, "node_pauses.json",
           '{"pauses": {"192.0.2.10": {"reason": "planted", '
           '"since": "2026-01-01T00:00:00+00:00", "until": null, '
           '"units": ["planted-unit"], "span_nodes": ["192.0.2.10"], '
           '"stopped": true}}}\n')
    # serving.json + profile config.yaml — added (t_2985e00b) because
    # serving.update_orchestrator_followers() rewrites orchestrator-tracking
    # profile config.yaml files. The sandbox must plant those so a regression
    # that stops patching serving.PROFILES_DIR trips the manifest diff.
    _plant(ws, "serving.json",
           '{"port": 8000, "units": [{"id": "o", "role": "orchestrator", '
           '"nodes": ["192.0.2.10"], "recipe": "r", "model": "m"}]}\n')
    # Realistic managed profiles: one tracking the orchestrator endpoint
    # (rewriteable by update_orchestrator_followers) and one worker pinned to
    # its own node (never matched, must stay untouched).
    _plant(ws, "profiles/default/config.yaml",
           "model:\n  provider: custom\n  base_url: http://192.0.2.10:8000/v1\n")
    _plant(ws, "profiles/worker-246/config.yaml",
           "model:\n  provider: custom\n  base_url: http://192.0.2.11:8000/v1\n")

    # ── ~/Library/LaunchAgents (t_35d9f6ea) ─────────────────────────────
    # The launchd plist directory sits OUTSIDE ~/.hscc, so the autouse
    # fixture's ``os.path.expanduser`` rewrite — which only redirects paths
    # under ~/.hscc — cannot reach it. Those paths are reachable at all only
    # through the fixture's explicit constant list, and one of those constants
    # is a DELETE target: event_driven.LaunchdJobGenerator.uninstall_job does
    # ``os.remove(<PLIST_DIR>/<label>.plist)`` for every stream, no subprocess
    # involved. A test that forgets to patch would therefore silently
    # de-Register the operator's periodic cluster checks. Plant the real set
    # of files the code would touch, derived from the production constants so
    # a renamed label cannot shrink the coverage unnoticed.
    from hscc_daemon import event_driven as _ed

    planted_plists = [
        f"{_ed.PERIODIC_LABEL_PREFIX}.{stream}.plist"
        for stream in _ed.PERIODIC_STREAMS
    ]
    assert len(planted_plists) >= 4, (
        "event_driven.PERIODIC_STREAMS shrank — the planted launchd set is "
        "no longer a faithful mirror of what uninstall_all_periodic removes")
    for fn in planted_plists:
        _plant_at(os.path.join(ws, "Library", "LaunchAgents", fn),
                  _PLANTED_PLIST)
    # The two other $HOME-rooted plist surfaces in this package, sealed by
    # t_2f09d480: install.py writes/removes com.hermes.hscc_daemon.plist,
    # api_launchd.py writes/removes com.hscc.api.plist. Planting them turns
    # the planted tree into the whole directory a forgetful test could damage,
    # and test_plist_seal_install_api_t_2f09d480.py drives both paths here
    # without patching them.
    from hscc_daemon import api_launchd as _api_launchd
    from hscc_daemon import install as _install

    for fn in (os.path.basename(_install.PLIST_FILE),
               os.path.basename(_api_launchd.API_PLIST_PATH)):
        _plant_at(os.path.join(ws, "Library", "LaunchAgents", fn),
                  _PLANTED_PLIST)

    before = _manifest(os.path.join(ws, ".hscc"))
    before_plists = _manifest(os.path.join(ws, "Library", "LaunchAgents"))
    assert before_plists, "the launchd plant did not land"

    # Run the test files most likely to leak (the historical writers) plus the
    # whole package — those tests exercise cycle(), pipeline_watchdog(),
    # trigger_engine(), save_watchdog_block(), save_config(), etc.
    tests_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))
    targets = [
        "test_daemon_ops.py", "test_lifecycle.py", "test_trigger.py",
        "test_health.py", "test_autodown.py", "test_autodown_cli.py",
        "test_state.py", "test_desktop.py",
        # t_2985e00b: serving tests exercise update_orchestrator_followers(),
        # which rewrites profile config.yaml — must run in the sandbox so the
        # planted serving.json/profiles are asserted unchanged.
        "test_serving.py", "test_serving_extra.py",
        # t_52bd498e: the single-instance claim WRITES a lock file — run it in
        # the sandbox so a regression (e.g. someone rebinds DAEMON_LOCK to a
        # literal ~/.hscc path) shows up as a new planted-tree file.
        "test_daemon_singleton.py",
        # t_28caea71: the node-pause overlay is a NEW durable file under
        # ~/.hscc. Its own tests are the only code that writes it, so they are
        # the ones that must run against the planted tree — a test that dropped
        # its `directory=` seam would rewrite the planted pause and trip the
        # manifest diff below.
        "test_node_pauses.py",
        # t_af9901e2: the wake path READS that overlay through node_pauses'
        # `directory=` seam. These tests write pauses of their own, so they
        # belong in the same sandbox — a wake test that lost its
        # `pauses_directory=` argument would read (and a CLI-style test could
        # write) the operator's real pause record.
        "test_autodown_paused_wake.py",
        # t_411401a0: the CLI-side twin of that wake seam. ``_cmd_wake`` starts
        # with ``record_activity("cli")`` — a WRITE of autodown.json — and the
        # all-paused branch reads the config back for the machine view, so this
        # is exactly the CLI-style test the note above means: if its
        # ``AUTODOWN_FILE`` seal were ever dropped, the planted tree is what
        # catches the rewrite of the operator's own state.
        "test_autodown_cli_paused_wake.py",
        # t_35d9f6ea: the event-driven CLI is the code that makedirs, writes
        # AND os.removes launchd plists, and it no longer patches any path of
        # its own — it runs entirely on the autouse seal. This sandbox is what
        # proves that: if the seal ever stops redirecting
        # ``event_driven.PLIST_DIR``, the planted
        # ``com.nousresearch.hscc-periodic.*.plist`` files above go missing
        # (uninstall) or the planted tree gains a new one (install), and the
        # manifest diff below fails. No fence or fork-spy can catch that class
        # of leak — it is a bare ``os.remove``.
        "test_event_driven_cli_no_ansi.py",
        # t_2f09d480: install.py and api_launchd.py WRITE and bare-``os.remove``
        # their launchd plists (and install.py's systemd units). This file
        # patches no path of its own, so it is the sandboxed proof that both
        # new seal entries are load-bearing: with either entry removed, the
        # planted com.hermes.hscc_daemon.plist / com.hscc.api.plist is
        # rewritten or deleted and the manifest diff below goes red.
        # Only the write/remove class: its seal-constant cases self-assert the
        # tmp redirect, which is the fixture pin's job, not this sandbox's.
        "test_plist_seal_install_api_t_2f09d480.py"
        "::TestWriteRemoveLandsInsideTheSeal",
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

    # ~/Library/LaunchAgents (t_35d9f6ea): the same promise one directory
    # up. This is the ONLY place the launchd seal is *measured* rather than
    # assumed — a per-test ``monkeypatch.setattr`` in the test file itself
    # would keep the tests green while the seal was gone, so the load-bearing
    # proof has to be a planted file under the path the code would use.
    after_plists = _manifest(os.path.join(ws, "Library", "LaunchAgents"))
    assert before_plists == after_plists, (
        "TEST SUITE WROTE TO LIVE ~/Library/LaunchAgents! Planted launchd "
        "plists changed.\nA test reached "
        "hscc_daemon/event_driven.py (LaunchdJobGenerator.install_job / "
        "uninstall_job, cmd_install_event_driven) or an equally unsealed "
        "plist path without the autouse seal redirecting it. See "
        "conftest.py._isolate_hscc._module_attrs() (the event_driven block) "
        "— uninstall_job os.remove()s <PLIST_DIR>/<label>.plist, which on the "
        "real host de-Registers the operator's periodic cluster checks.\n"
        "changed files:\n"
        + "\n".join(
            f"  {p}: before={before_plists.get(p)} after={after_plists.get(p)}"
            for p in sorted(set(before_plists) | set(after_plists))
            if before_plists.get(p) != after_plists.get(p)
        )
    )
