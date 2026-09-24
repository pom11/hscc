"""End-to-end verifier for card t_4a548958 — proves the hard invariants with the
REAL subprocess CLI (not pytest), before the change is merged.

Runs `flightdeck roadmap/why` as a real piped subprocess (stdout = pipe, not a
tty) and asserts, per converted command:
  1. human output contains NO ANSI (\\x1b) bytes — the parser contract;
  2. --json output stays byte-identical to the canonical json.dumps (proved
     directly below for roadmap show --json, which is the one clean real-pipe
     --json demo among group C-c; the why/review/standup --json byte-identity is
     proven in the pytest no-ANSI suite because a real subprocess has no
     injectable board for those commands).

Among group C-c (review/roadmap/standup/why), `roadmap show` is the only one
that runs purely on local file I/O (registry + ROADMAP.md) with no git, board
or network — so it is the clean real-subprocess demo for both no-ANSI AND
--json byte-identity. The injectable-git/board-backed proofs for review/standup
and why live in the pytest no-ANSI regression classes in each per-command test
file.

Uses a throwaway registry + ROADMAP.md in /tmp, never the operator's.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = "/Users/desac/.hermes/kanban/boards/hscc/workspaces/t_4a548958"
PY = "/Users/desac/.hermes/hermes-agent/venv/bin/python"
HSCC_PROJECT = os.path.join(ROOT, "hscc-project")
sys.path.insert(0, HSCC_PROJECT)
from flightdeck.core import registry  # noqa: E402

FAIL = []


def run(args, env=None):
    e = dict(os.environ)
    e["PYTHONPATH"] = HSCC_PROJECT + os.pathsep + ROOT
    if env:
        e.update(env)
    p = subprocess.run(
        [PY, "-c", "from flightdeck.cli import main; raise SystemExit(main())"] + args,
        capture_output=True, text=True, cwd=ROOT, env=e)
    return p.returncode, p.stdout, p.stderr


def check(label, cond, detail=""):
    print(f"{label}: {'OK' if cond else 'FAIL'}{('  ' + detail) if (not cond and detail) else ''}")
    if not cond:
        FAIL.append(label)


tmp = tempfile.mkdtemp(prefix="tc-c-")
repo = os.path.join(tmp, "flightdeck")
os.makedirs(repo, exist_ok=True)
regpath = os.path.join(tmp, "registry.yaml")
registry.add_project("flightdeck", repo=repo, board="flightdeck", path=regpath)
roadmap_path = os.path.join(repo, "ROADMAP.md")
with open(roadmap_path, "w", encoding="utf-8") as fh:
    fh.write(
        "# Roadmap\n\n## Now\n- [ ] Ship 0.6.0\n- [x] Refund webhook\n\n"
        "## Next\n- [ ] Stripe subscription\n\n## Later\n- [ ] Multi-tenant portal\n"
    )

# ---- roadmap show (human, piped): no ANSI, content preserved ----
rc, out, err = run(["--registry", regpath, "roadmap", "show", "flightdeck"])
_ansi = chr(27) in out
check("roadmap show rc=0 + plain", rc == 0 and not _ansi
      and "Ship 0.6.0" in out and "[flightdeck]" in out,
      f"rc={rc} ansi={_ansi}")


def _render_json_canonical(regpath):
    """Replicate roadmap._render_json over a load of the same registry."""
    from flightdeck.commands import roadmap as rcmd
    projects = registry.load_registry(regpath)
    rows = [(p.name, rcmd._definite_path(p)) for p in projects]
    return rcmd._render_json(rows)


# ---- roadmap show --json: byte-identical to canonical _render_json + plain ----
rc, out, _ = run(["--json", "--registry", regpath, "roadmap", "show", "flightdeck"])
canonical = json.dumps(_render_json_canonical(regpath)) + "\n"
_eq = out == canonical
_ansi = chr(27) in out
check("roadmap show --json byte-identical + plain",
      rc == 0 and _eq and not _ansi,
      f"rc={rc} ansi={_ansi} eq={_eq}")


print()
if FAIL:
    print("RESULT: FAILURES ->", FAIL)
    sys.exit(1)
print("RESULT: ALL INVARIANT CHECKS PASSED")
