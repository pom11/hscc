"""Verify the inline write script actually runs under sparkrun's venv python
against a TEMP config root (never the operator's ~/.config/sparkrun).

This is a manual verification probe, not part of the suite (the suite must not
require sparkrun on PATH). Run it explicitly:

    python3 hscc-cluster/tests/_probe_write_script.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import sparkrun_bridge as sb  # noqa: E402

tmp = tempfile.mkdtemp(prefix="bridge-probe-")
# sparkrun resolves its config root from SPARKRUN_CONFIG_DIR (product_env
# ("CONFIG_DIR")) — redirect it so the probe cannot touch the operator's real
# ~/.config/sparkrun.
os.environ["SPARKRUN_CONFIG_DIR"] = tmp

venv_py = sb._sparkrun_venv_python()
print("venv python:", venv_py)
if not venv_py:
    print("SKIP: sparkrun not on PATH")
    sys.exit(0)

cfg = os.path.join(tmp, "proxy.yaml")
os.makedirs(os.path.dirname(cfg), exist_ok=True)
with open(cfg, "w") as fh:
    fh.write("proxy:\n  port: 4000\n  host: 127.0.0.1\ngateways:\n  litellm:\n"
             "    router_settings:\n      routing_strategy: simple-shuffle\n"
             "aliases:\n  foo: bar\n")
before = open(cfg).read()

regions = {"gateways": {"litellm": {"balancing": "least-in-flight"}}}
proc = sb._run_venv_script(json.dumps(regions, sort_keys=True), "direct")
print("rc:", proc.returncode)
print("stdout:", proc.stdout.strip()[:400])
print("stderr:", proc.stderr.strip()[:800])
after = open(cfg).read()
print("--- before ---\n" + before)
print("--- after ---\n" + after)
ok = proc.returncode == 0 and "balancing: least-in-flight" in after \
    and "port: 4000" in after and "foo: bar" in after \
    and "routing_strategy: simple-shuffle" in after
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
