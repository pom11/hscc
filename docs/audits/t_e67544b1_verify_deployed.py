"""Verify the DEPLOYED installed hscc-cluster plugin (post-install_payload).

Confirms the themed code installed at ~/.hermes/plugins/hscc-cluster works:
  - `_theme` imports and degrades to plain (no ANSI) on non-tty
  - `--json` result paths emit byte-identical raw json
Commonly run from the plugin dir itself. Does NOT restart the gateway.
"""
import io
import json
import subprocess
import sys
from contextlib import redirect_stdout

PLUGIN = "/Users/desac/.hermes/plugins/hscc-cluster"
ok = True

def check(label, cond, detail=""):
    global ok
    print(f"{label}: {'OK' if cond else 'FAIL'} {detail}")
    if not cond:
        ok = False

sys.path.insert(0, PLUGIN)
import _theme  # noqa
import gateway_restart  # noqa
import hscc  # noqa

# 1. no-ANSI on non-tty for each entry's human render
for name, fn in [
    ("hscc._emit_result", lambda: hscc._emit_result("cluster-status",
        {"workloads": [{"name": "@official/qwen3", "tp": 2, "pp": 1,
                        "container_id": "abc123"}],
         "idle_hosts": [], "total_hosts": 1})),
    ("gateway_restart._emit_result", lambda: gateway_restart._emit_result(
        {"success": True, "note": "Gateway kicked"})),
]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    check(f"{name} no-ANSI on non-tty", "\x1b" not in buf.getvalue())

# 2. --json byte-identity on the deployed code
for label, fn, res in [
    ("hscc._emit_result --json",
     lambda: hscc._emit_result("jobs", res, as_json=True),
     {"success": True, "returncode": 0, "output": "Job: x"}),
    ("gateway_restart._emit_result --json",
     lambda: gateway_restart._emit_result(res, as_json=True),
     {"success": True, "note": "G"}),
]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    exp = json.dumps(res, indent=2, default=str) + "\n"
    check(f"{label} byte-identical", buf.getvalue() == exp)

# 3. cluster_template_cli --json as a real subprocess on the deployed plugin
r = subprocess.run(
    [sys.executable, f"{PLUGIN}/cluster_template_cli.py", "list", "--json"],
    capture_output=True, text=True, cwd=PLUGIN, timeout=120)
check("deployed cluster_template_cli --json rc=0", r.returncode == 0, r.stderr)
if r.returncode == 0:
    payload = json.loads(r.stdout)
    canon = json.dumps(payload, indent=2, default=str) + "\n"
    check("deployed cluster_template_cli --json byte-identical",
          r.stdout == canon)
    check("deployed --json no-ANSI", "\x1b" not in r.stdout)

# 4. cluster_template_cli default (human) is themed, not raw JSON
r2 = subprocess.run(
    [sys.executable, f"{PLUGIN}/cluster_template_cli.py", "list"],
    capture_output=True, text=True, cwd=PLUGIN, timeout=120)
check("deployed default human NOT raw JSON (themed)",
      r2.returncode == 0 and not r2.stdout.lstrip().startswith("{"))
check("deployed default human no-ANSI", "\x1b" not in r2.stdout)

print()
print("RESULT:", "ALL INVARIANT CHECKS PASSED" if ok else "FAILURES ABOVE")
sys.exit(0 if ok else 1)
