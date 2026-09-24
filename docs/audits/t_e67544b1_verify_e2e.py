"""End-to-end invariant verification for t_e67544b1 (theme hscc-cluster standalone CLI).

Run:  <interpreter> docs/audits/t_e67544b1_verify_e2e.py
Prints RESULT: ALL INVARIANT CHECKS PASSED (or FAILED with details).

Checks:
  1. `cluster-template` standalone entry (cluster_template_cli.py __main__):
     - default (human) output is themed (NOT a raw JSON blob) and has NO ANSI
       on a non-tty pipe;
     - `--json` output is raw, byte-identical to json.dumps(indent=2, default=str),
       valid JSON, no ANSI.
  2. gateway_restart._emit_result --json is byte-identical to the canonical dump.
  3. hscc._emit_result --json is byte-identical.
All paths run against THIS worktree's plugin dir, never the primary checkout.
"""
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout

WORKSPACE = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))  # repo root (script lives in docs/audits/)
PLUGIN_DIR = os.path.join(WORKSPACE, "hscc-cluster")

# The interpreter running THIS verify script drives the subprocess checks too.
PY = sys.executable
ok = True


def check(label, cond, detail=""):
    global ok
    status = "OK" if cond else "FAIL"
    if not cond:
        ok = False
    print(f"{label}: {status} {detail}")


def run(args, cwd=WORKSPACE):
    return subprocess.run([PY, *args], capture_output=True, text=True,
                          cwd=cwd, timeout=120)


# 1a. cluster_template_cli.py list (human default) — themed, no ANSI, not raw JSON
r = run([os.path.join("hscc-cluster", "cluster_template_cli.py"), "list"])
out = r.stdout
check("cluster-template list human rc=0", r.returncode == 0, r.stderr)
check("cluster-template list human no-ANSI", "\x1b" not in out)
check("cluster-template list human themed (not raw JSON)", not out.lstrip().startswith("{"))

# 1b. cluster_template_cli.py list --json — raw byte-identical, no ANSI
r2 = run([os.path.join("hscc-cluster", "cluster_template_cli.py"), "list", "--json"])
check("cluster-template list --json rc=0", r2.returncode == 0, r2.stderr)
check("cluster-template list --json no-ANSI", "\x1b" not in r2.stdout)
payload = json.loads(r2.stdout)
canon = json.dumps(payload, indent=2, default=str) + "\n"
check("cluster-template list --json byte-identical", r2.stdout == canon)

# 2. gateway_restart._emit_result --json byte-identity
sys.path.insert(0, PLUGIN_DIR)
import gateway_restart
res = {"success": True, "note": "Gateway kicked"}
buf = io.StringIO()
with redirect_stdout(buf):
    gateway_restart._emit_result(res, as_json=True)
exp = json.dumps(res, indent=2, default=str) + "\n"
check("gateway_restart --json byte-identical", buf.getvalue() == exp)

# 3. hscc._emit_result --json byte-identity
import hscc
res2 = {"success": True, "returncode": 0, "output": "Job: x"}
buf2 = io.StringIO()
with redirect_stdout(buf2):
    hscc._emit_result("jobs", res2, as_json=True)
exp2 = json.dumps(res2, indent=2, default=str) + "\n"
check("hscc._emit_result --json byte-identical", buf2.getvalue() == exp2)

# 4. Anti-collapse: non-tty console width == exported constant (not a literal)
import _theme
check("make_console non-tty width == constant",
      _theme.make_console().width == _theme.DEFAULT_NONTTY_WIDTH == 200)

print()
print("RESULT:", "ALL INVARIANT CHECKS PASSED" if ok else "FAILURES ABOVE")
sys.exit(0 if ok else 1)
