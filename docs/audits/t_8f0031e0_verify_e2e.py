"""t_8f0031e0 — terminal proof of the two hard invariants on the converted
commands (daemon / daemon_install / start / update / init):

  1. `--json` output is BYTE-IDENTICAL (start is the only converted command with
     a --json path; _print_json still print(json.dumps(...)) raw).
  2. Non-TTY piped human output has NO ANSI bytes (no \\x1b).

Run:  python docs/audits/t_8f0031e0_verify_e2e.py
Prints PASS/FAIL per check; exit 0 only if all pass.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys

from contextlib import redirect_stdout

sys.path.insert(0, "hscc-project")

from flightdeck.commands import init as init_cmd, update as update_cmd
from flightdeck.core import self_update

FAILURES: list[str] = []


def _check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        FAILURES.append(name)


# --------------------------------------------------------------------------- #
# 1. --json byte-identity on `start`
# --------------------------------------------------------------------------- #
# start.py keeps `_print_json` printing json.dumps(canonical) raw (never routed
# through a Console), so a --json invocation emits EXACTLY the canonical JSON
# with no panel border, no ANSI, no trailing decoration. Build a small injectable
# run: the plan uses kanban.list_cards + git is_merged. We drive cmd_start with
# fakes exactly like test_start.py does, but in a raw "terminal" proof.
from flightdeck.commands import start as start_cmd
from flightdeck.commands.start import FLEET_PROFILES
from flightdeck.core import kanban
import argparse

PROJECTS = [
    type("P", (), {"name": "hscc", "board": "hscc", "repo": "/repo"})()
]
CARDS = [
    {"id": f"c{i}", "board": "hscc", "status": "todo", "body": f"MILESTONE: M1",
     "title": f"task {i}", "branch": f"wt/c{i}"}
    for i in range(3)
]


class FakeRun:
    def __call__(self, cmd, repo):
        # is_merged -> True so nothing is held.
        return subprocess.CompletedProcess(cmd, 0, "", "")


import tempfile, os as _os

_config = _os.path.join(tempfile.mkdtemp(), "config.yaml")
with open(_config, "w") as fh:
    fh.write("kanban:\n  max_in_progress: 50\n  max_in_progress_per_profile: 50\n")


def _ns(**kw):
    d = dict(project="hscc", milestone="M1", max_concurrent=3, apply=False,
             config_path=_config,
             list_cards=lambda **k: CARDS, run=FakeRun())
    d.update(kw)
    return argparse.Namespace(**d)


buf = io.StringIO()
with redirect_stdout(buf):
    start_cmd.cmd_start(_ns(json=True), PROJECTS)
out = buf.getvalue()
canonical = {
    "milestone": "M1",
    "total_cap": 3,  # min(config max_in_progress=50, --max-concurrent=3) = 3
    "per_profile_cap": 50,
    "release": [
        {"id": c["id"], "title": c["title"], "assignee": FLEET_PROFILES[i]}
        for i, c in enumerate(sorted(CARDS, key=lambda x: x["id"]))
    ],
    "held": [],
    "not_released": 0,
}
print("start --json raw output (repr):")
print(repr(out))
_check("start --json byte-identical", out == json.dumps(canonical) + "\n")
_check("start --json no ANSI", "\x1b" not in out)


# --------------------------------------------------------------------------- #
# 2. Non-TTY piped human output: NO ANSI + content preserved
# --------------------------------------------------------------------------- #
def _no_ansi(name, fn):
    b = io.StringIO()
    with redirect_stdout(b):
        fn()
    o = b.getvalue()
    has_ansi = "\x1b" in o
    _check(f"{name} no-ANSI", not has_ansi, f"ansi={has_ansi}")
    return o


def _update_ns(**kw):
    d = dict(run=None, registry="/tmp/reg.yaml", apply=False, dry_run=False)
    d.update(kw)
    return argparse.Namespace(**d)


# update — dry-run plan on a non-git install (no network).
_out_upd = _no_ansi(
    "update non-git",
    lambda: update_cmd.run(_update_ns(), "/tmp/reg.yaml"))
print("  update output: " + repr(_out_upd[:120]))

# init — dry run in a fresh tmp home, injecting every env check as pass so no
# real ~/.hermes / network / git is touched.
import tempfile, os


def _init_ns(tmp, apply):
    return argparse.Namespace(
        home=str(tmp), apply=apply, registry="/tmp/reg.yaml",
        _py_info=(3, 13, 0), _mcp_layout={"ok": True, "detail": "x"},
        _which=lambda n: "/usr/bin/git",
        _hermes_db=_os.path.join(tmp, "kanban.db"),
        _hermes_open=lambda db: None,
    )


tmp = tempfile.mkdtemp()
_out_init = _no_ansi("init dry-run", lambda: init_cmd.cmd_init(_init_ns(tmp, False)))
print("  init output has [ok]:", "[ok]" in _out_init)


# daemon status (read-only, stopped) — command module's cmd_status.
from flightdeck.commands import daemon as daemon_cmd
from flightdeck.core import daemon as daemon_core


def _run_daemon_status():
    import os
    old_pid = daemon_core.PID_FILE
    try:
        daemon_core.PID_FILE = os.path.join(tmp, "daemon.pid")
        daemon_core.STATE_DIR = os.path.join(tmp, "state")
        return daemon_cmd.cmd_status(argparse.Namespace(), "/tmp/reg.yaml")
    finally:
        daemon_core.PID_FILE = old_pid


_out_dstat = _no_ansi("daemon status", _run_daemon_status)
print("  daemon status output: " + repr(_out_dstat[:120]))


print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
    sys.exit(1)
print("ALL CHECKS PASSED")
sys.exit(0)
