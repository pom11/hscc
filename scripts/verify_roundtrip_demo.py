"""Reproduce the four full-verify round-trip scenarios the task requires:
down (PASS), waking (PASS), waking-without-block (FAIL), real-fault-during-
waking (FAIL). Uses isolated tmp dirs so it never touches the live ~/.hscc.
Mirrors the exact formatting of hscc_daemon.hscc._handle_verify.

Each scenario points the block/autodown MODULE CONSTANTS at a fresh tmp dir
(so classify() reads only that scenario's files) and passes the fleet surface
through run_all() overrides. This is the same mechanism the tests exercise.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "hscc_daemon"))
sys.path.insert(0, _REPO_ROOT)

from hscc_daemon import lifecycle as _lc  # noqa: E402
from hscc_daemon import autodown as _ad  # noqa: E402


def point_files_at(hfcc_dir):
    """Point the block/autodown MODULE CONSTANTS at hfcc_dir (fresh each
    scenario) so classify() reads only that scenario's files."""
    _lc.WATCHDOG_BLOCK_FILE = os.path.join(hfcc_dir, "watchdog-block.json")
    _ad.AUTODOWN_FILE = os.path.join(hfcc_dir, "autodown.json")


def arm_intentional(hfcc_dir, state):
    """Write the intentional watchdog block + autodown config into hfcc_dir."""
    point_files_at(hfcc_dir)
    _lc.save_watchdog_block({
        "blocked": True, "intentional": "autodown",
        "reason": "autodown: intentional idle teardown"})
    _ad.save_config({**_ad.DEFAULT_CONFIG, "enabled": True,
                     "state": state, "down_since": "2026-01-01T00:00:00+00:00"})


def build_fleet(tmp_path, add_real_fault=False):
    """Plant a synthetic fleet surface; return run_all overrides."""
    import yaml
    from hscc_daemon.state import now_iso

    plugins_dir = tmp_path / "plugins" / "hscc-commands"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "__init__.py").write_text(
        'register("workers-up")\nregister("cluster-restart")\n'
        'register("template")\n')

    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({
        "multiplex_profiles": True,
        "kanban": {"max_in_progress": 3},
        "toolsets": ["hscc-cluster"],
    }))

    gw_state = tmp_path / "gateway_state.json"
    profiles = tmp_path / "profiles"
    (profiles / "backend-engineer").mkdir(parents=True)
    (profiles / "writer").mkdir(parents=True)
    json.dump({"served_profiles": ["backend-engineer", "writer"]},
              open(gw_state, "w"))

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    for name in ("watchdog", "dgx", "gateway", "proxy", "workers"):
        data = {"ok": False, "timestamp": now_iso(), "last_check": now_iso(),
                "stream": name, "intentional": "autodown",
                "message": f"intentional autodown ({name})"}
        (state_dir / f"{name}.json").write_text(json.dumps(data))

    if add_real_fault:
        (state_dir / "heartbeat.json").write_text(json.dumps({
            "ok": False, "timestamp": now_iso(), "last_check": now_iso(),
            "stream": "heartbeat"}))

    return {
        "plugins_dir": str(tmp_path / "plugins"),
        "config": str(config),
        "gateway_state": str(gw_state),
        "profiles_dir": str(tmp_path / "profiles"),
        "state_dir": str(state_dir),
        "url": "http://localhost:4000/v1/models",
    }


def proxy_no_models():
    import urllib.request
    from unittest.mock import patch, MagicMock
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps({"data": []}).encode()
    mock_resp.__enter__ = lambda self: self
    mock_resp.__exit__ = lambda self, *a: None
    return patch("hscc_daemon.verify.urllib.request.urlopen",
                 return_value=mock_resp)


def print_formatted(result):
    checks = result.get("checks", [])
    max_name = max(len(c.get("name", "")) for c in checks) if checks else 4
    max_name = max(max_name, 4)
    for c in checks:
        glyph = "\u2713" if c.get("ok") else "\u2717"
        name = c.get("name", "").ljust(max_name)
        print(f"  {glyph} {name}  {c.get('detail','')}")
    overall = "\u2713 All checks passed" if result.get("ok") else "\u2717 Some checks failed"
    print(f"\n  {overall}")
    print()
    return result.get("ok")


def run_scenario(label, state=None, armed=True, real_fault=False):
    print("=" * 78)
    print(f"SCENARIO: {label}")
    print("=" * 78)
    tmp = Path(tempfile.mkdtemp(prefix="verify-roundtrip-"))
    hfcc = str(tmp / "hscc")
    os.makedirs(hfcc)
    point_files_at(hfcc)  # always point constants at THIS scenario's tmp dir
    if armed:
        arm_intentional(hfcc, state)
    overrides = build_fleet(tmp, add_real_fault=real_fault)
    from hscc_daemon import verify as verify_mod
    with proxy_no_models():
        result = verify_mod.run_all(**overrides)
    return print_formatted(result)


# 1. down (PASS)
p1 = run_scenario("down  (intentional block, autodown state=down)  -> EXPECT PASS",
                  state="down", armed=True)
# 2. waking (PASS)
p2 = run_scenario("waking (intentional block, autodown state=waking) -> EXPECT PASS",
                  state="waking", armed=True)
# 3. waking without block (FAIL)
p3 = run_scenario("waking WITHOUT intentional block -> EXPECT FAIL",
                  state="waking", armed=False)
# 4. real fault during waking (FAIL)
p4 = run_scenario("waking + block + unrelated real fault (heartbeat) -> EXPECT FAIL",
                  state="waking", armed=True, real_fault=True)

assert p1 is True, "down must PASS"
assert p2 is True, "waking must PASS"
assert p3 is False, "waking-without-block must FAIL"
assert p4 is False, "real-fault-during-waking must FAIL"
print("OK: all four scenarios produced the expected PASS/FAIL.")
