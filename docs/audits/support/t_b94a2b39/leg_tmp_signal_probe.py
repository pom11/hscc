#!/usr/bin/env python3
"""t_b94a2b39: which signals leave /tmp/hscc-legs-* behind?

Runs the SHIPPED scripts/run_tests.sh against the same HSCC_TEST_PY stub seam the
parallelism tests use, with 30 s stub legs, and kills the runner with one signal
per trial. Reports rc and whether the leg-log dir survived.
"""
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(sys.argv[1]).resolve()
RUNNER = REPO / "scripts" / "run_tests.sh"
WORK = Path(sys.argv[2]).resolve()
WORK.mkdir(parents=True, exist_ok=True)

LEG_A, LEG_B, LEG_C = "hscc-commands", "sparkrun-hermes", "memori_byodb"
THREE = f"{LEG_A} {LEG_B} {LEG_C}"

STUB = """#!/usr/bin/env bash
set -u
tests_arg=""
for a in "$@"; do case "$a" in */tests) tests_arg="$a" ;; esac; done
leg="(none)"
[ -n "$tests_arg" ] && leg="$(basename "$(dirname "$tests_arg")")"
printf '%s\\t%s\\n' "$leg" "$*" >> "$STUB_ARGV"
secs=0.2; rc=0
for entry in $STUB_PLAN; do
  name="${entry%%:*}"; rest="${entry#*:}"
  if [ "$name" = "$leg" ]; then secs="${rest%%:*}"; rc="${rest#*:}"; break; fi
done
echo "start $leg $$" >> "$STUB_TRACE"
sleep "$secs"
echo "end $leg $$" >> "$STUB_TRACE"
if [ "$rc" -ne 0 ]; then
  echo "F"
  echo "FAILED $leg/tests/test_stub.py::test_${leg//-/_}_breaks - assert 1 == 2"
  echo "1 failed in ${secs}s"
else
  echo "."
  echo "7 passed in ${secs}s"
fi
exit "$rc"
"""

stub = WORK / "stub_py.sh"
stub.write_text(STUB)
stub.chmod(0o755)


def trial(signame, hold=6.0):
    trace = WORK / f"trace.{signame}"
    argv = WORK / f"argv.{signame}"
    log = WORK / f"run.{signame}.log"
    trace.write_text("")
    argv.write_text("")
    env = dict(os.environ)
    env.update({
        "HSCC_TEST_PY": str(stub),
        "STUB_TRACE": str(trace),
        "STUB_ARGV": str(argv),
        "STUB_PLAN": f"{LEG_A}:30:0 {LEG_B}:30:0 {LEG_C}:30:0",
        "HSCC_TEST_LEGS": THREE,
        "HSCC_TEST_PARALLEL": "2",
        "HSCC_LEG_HEARTBEAT_SECONDS": "0",
    })
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    before = set(Path("/tmp").glob("hscc-legs-*"))
    with open(log, "wb") as fh:
        p = subprocess.Popen(["bash", str(RUNNER)], stdout=fh, stderr=subprocess.STDOUT,
                             env=env, cwd=str(REPO), start_new_session=True)
        # wait for the banner (dir created + trap installed) and for 2 legs to be live
        deadline = time.time() + 40
        m = None
        while time.time() < deadline:
            try:
                m = re.search(r"leg logs in (/tmp/hscc-legs-\S+)", log.read_text())
            except Exception:
                m = None
            if m and len(trace.read_text().splitlines()) >= 2:
                break
            time.sleep(0.1)
        banner_dir = m.group(1) if m else None
        time.sleep(hold)
        sig = getattr(signal, "SIG" + signame)
        os.kill(p.pid, sig)
        try:
            rc = p.wait(timeout=45)
        except subprocess.TimeoutExpired:
            p.kill()
            rc = p.wait(timeout=30)
            rc = f"TIMEOUT->{rc}"
    # legs the runner did not reap are still sleeping; count them BEFORE taking
    # the session down — an untrapped fatal signal that skips the reaper leaves
    # N orphan pytests burning a host that runs the fleet control plane.
    time.sleep(0.5)
    stub_pids = [int(l.split()[-1]) for l in trace.read_text().splitlines()
                 if l.startswith("start")]
    survivors = []
    for pid in stub_pids:
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                            capture_output=True, text=True).stdout.strip()
        if st not in ("", "Z"):
            survivors.append(pid)
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    after = set(Path("/tmp").glob("hscc-legs-*"))
    leaked = sorted(after - before)
    shape = {}
    for src in leaked:
        try:
            shape[src] = {f.name: f.stat().st_size for f in sorted(Path(src).iterdir())}
        except OSError as exc:
            shape[src] = {"<unreadable>": str(exc)}
    # clean up anything this trial leaked so the next trial starts clean
    for d in leaked:
        shutil.rmtree(d, ignore_errors=True)
    if banner_dir and Path(banner_dir).exists():
        shutil.rmtree(banner_dir, ignore_errors=True)
    return dict(sig=signame, rc=rc, banner_dir=banner_dir or "-",
                leaked=leaked, survivors=len(survivors), shape=shape)


if __name__ == "__main__":
    sigs = sys.argv[3:] or ["TERM", "INT", "HUP", "QUIT", "KILL", "PIPE"]
    repeats = int(os.environ.get("SIGPROBE_REPEATS", "1"))
    print(f"repo={REPO} runner_exists={RUNNER.exists()} repeats={repeats}")
    for s in sigs:
        rows = [trial(s) for _ in range(repeats)]
        n = len(rows)
        leaky = sum(1 for r in rows if r["leaked"])
        orph = sum(r["survivors"] for r in rows) / n
        rcs = ",".join(str(r["rc"]) for r in rows)
        print(f"{s:6} leaked {leaky}/{n}  mean_orphans={orph:.1f}  rc=[{rcs}]")
        for r in rows:
            for d, sh in r["shape"].items():
                print(f"        {d} -> {sh}")
