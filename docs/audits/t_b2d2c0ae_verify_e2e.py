"""End-to-end verifier for card t_b2d2c0ae — proves the hard invariants with the
REAL subprocess CLI (not pytest), before the change is merged.

Runs `flightdeck message/ask/...` as a real piped subprocess (stdout = pipe,
not a tty) and asserts, per converted command:
  1. human output contains NO ANSI (\\x1b) bytes — the parser contract;
  2. --json output stays byte-identical to the canonical json.dumps (proved
     directly below for ask template list, which is the one clean real-pipe
     --json demo among group B; the qa --json byte-identity is proven in the
     pytest no-ANSI suite because a real subprocess has no injectable board).

The fully-injectable git/kanban-backed proofs (report dry-run/apply, qa human,
message dispatch --apply, ask render) live in the pytest no-ANSI regression
classes in each per-command test file. This script proves the transport-level
invariant (piped stdout is plain, and --json is canonical bytes) with the REAL
binary for the commands that need no external fakery.

Uses a throwaway registry + templates home in /tmp, never the operator's.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = "/Users/desac/dev/hscc/.worktrees/t_b2d2c0ae"
PY = "/Users/desac/.hermes/hermes-agent/venv/bin/python"
HSCC_PROJECT = os.path.join(ROOT, "hscc-project")
sys.path.insert(0, HSCC_PROJECT)
from flightdeck.core import registry  # noqa: E402
from flightdeck.core import templates  # noqa: E402

FAIL = []


def run(args, env=None):
    e = dict(os.environ)
    e["PYTHONPATH"] = HSCC_PROJECT + os.pathsep + ROOT
    if env:
        e.update(env)
    p = subprocess.run([PY, "-c", "from flightdeck.cli import main; raise SystemExit(main())"] + args,
                       capture_output=True, text=True, cwd=ROOT, env=e)
    return p.returncode, p.stdout, p.stderr


def check(label, cond, detail=""):
    print(f"{label}: {'OK' if cond else 'FAIL'}{('  ' + detail) if (not cond and detail) else ''}")
    if not cond:
        FAIL.append(label)


tmp = tempfile.mkdtemp(prefix="tb2d2c0ae-")
repo = os.path.join(tmp, "hscc")
os.makedirs(repo, exist_ok=True)
regpath = os.path.join(tmp, "registry.yaml")
registry.add_project("hscc", repo=repo, board="hscc", topic=140, path=regpath)
tplhome = os.path.join(tmp, "tpl")
templates.ensure_seeded(tplhome)

# ---- message send (human, piped) ----
rc, out, err = run(["--registry", regpath, "message", "send", "hscc", "standing up now"])
_ansi = chr(27) in out
check("message send rc=0 + plain", rc == 0 and not _ansi and "would post to hscc" in out,
      f"rc={rc} ansi={_ansi}")

# ---- message read (human, piped) ----
rc, out, err = run(["--registry", regpath, "message", "read", "hscc"])
_ansi = chr(27) in out
check("message read rc=0 + plain", rc == 0 and not _ansi and "no source" in out,
      f"rc={rc} ansi={_ansi}")

# ---- message dispatch (dry-run, piped) ----
rc, out, err = run(["--registry", regpath, "message", "dispatch", "hscc", "build the widget"])
_ansi = chr(27) in out
check("message dispatch dry-run rc=0 + plain", rc == 0 and not _ansi and "card title: build the widget" in out,
      f"rc={rc} ansi={_ansi}")

# ---- message broadcast (human, piped) ----
rc, out, err = run(["--registry", regpath, "message", "broadcast", "outage over"])
_ansi = chr(27) in out
check("message broadcast rc=0 + plain", rc == 0 and not _ansi and "outage over" in out,
      f"rc={rc} ansi={_ansi}")

# ---- ask render + template list/show (human, piped) ----
rc, out, _ = run(["--registry", regpath, "ask", "template", "list"])
_ansi = chr(27) in out
check("ask template list rc=0 + plain", rc == 0 and not _ansi and "decompose" in out,
      f"rc={rc} ansi={_ansi}")

rc, out, _ = run(["--registry", regpath, "ask", "template", "show", "decompose"])
_ansi = chr(27) in out
check("ask template show rc=0 + plain", rc == 0 and not _ansi and "GOAL" in out,
      f"rc={rc} ansi={_ansi}")

# ---- ask template list --json: the ONE clean real-pipe --json demo here ----
# The --json flag is a TOP-LEVEL global; it must precede the subcommand.
rc, out, _ = run(["--json", "--registry", regpath, "ask", "template", "list"])
names = sorted(templates.list_templates(home=tplhome))
canonical = json.dumps(names) + "\n"
_eq = out == canonical
_ansi = chr(27) in out
check("ask template list --json byte-identical + plain",
      rc == 0 and _eq and not _ansi,
      f"rc={rc} ansi={_ansi} eq={_eq}")

print()
if FAIL:
    print("RESULT: FAILURES ->", FAIL)
    sys.exit(1)
print("RESULT: ALL INVARIANT CHECKS PASSED")
