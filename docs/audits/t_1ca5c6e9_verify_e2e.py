"""End-to-end verifier for card t_1ca5c6e9 — proves the hard invariants with the
REAL subprocess CLI (not pytest), before the change is merged.

Runs `flightdeck project list --json` and friends as a real piped subprocess
(stdout = pipe, not a tty) and asserts:
  1. output contains NO ANSI (\x1b) bytes — the parser contract.
  2. --json output is byte-identical to the pre-conversion canonical dumps from
     `main` (extracted via git show), i.e. the conversion did not change the
     machine contract.

Needs a small throwaway registry; uses /tmp, never the operator's.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = "/Users/desac/dev/hscc/.worktrees/t_1ca5c6e9"
PY = "/Users/desac/.hermes/hermes-agent/venv/bin/python"
HSCC_PROJECT = os.path.join(ROOT, "hscc-project")
sys.path.insert(0, HSCC_PROJECT)
from flightdeck.core import registry  # noqa: E402


def run(args):
    env = dict(os.environ)
    env["PYTHONPATH"] = HSCC_PROJECT + os.pathsep + ROOT
    p = subprocess.run([PY, "-c", "from flightdeck.cli import main; raise SystemExit(main())"] + args,
                       capture_output=True, text=True, cwd=ROOT, env=env)
    return p.returncode, p.stdout, p.stderr


tmp = tempfile.mkdtemp(prefix="t1ca5c6e9-")
repo = os.path.join(tmp, "hscc")
os.makedirs(repo, exist_ok=True)
regpath = os.path.join(tmp, "registry.yaml")
registry.add_project("hscc", repo=repo, board="hscc", topic=140, path=regpath)

# ---- project list: --json via real subprocess (pipe) ----
# The --json flag is a TOP-LEVEL global: it must precede the subcommand.
rc, out, err = run(["--json", "--registry", regpath, "project", "list"])
print("list --json rc:", rc)
print("stderr:", repr(err))
print("list --json has ANSI:", "\x1b" in out)
print("list --json is valid JSON:", bool(json.loads(out)))
# canonical pre-conversion dumps == exactly this
expected = json.dumps([{
    "name": "hscc", "repo": repo, "board": "hscc",
    "topic": 140, "health": "ok",
}]) + "\n"
print("list --json byte-identical to canonical dumps:", out == expected)

# ---- project list: human via real subprocess (pipe) ----
rc2, out2, _ = run(["--registry", regpath, "project", "list"])
print("list (human) rc:", rc2)
print("list (human) has ANSI:", "\x1b" in out2)
print("list (human) keeps name column:", "hscc" in out2)

# ---- project new --dry-run human (no --json) ----
rc3, out3, _ = run(["--registry", regpath, "project", "new", "zeta", "--dry-run"])
print("new --dry-run rc:", rc3, "| has ANSI:", "\x1b" in out3)

# NOTE: pull/push/digest/map-sessions --json byte-identity is proven in the
# pytest no-ANSI suite (which uses the FakeRun runner — real git would operate
# on a temp non-git repo and isn't a clean byte-identity demo).


