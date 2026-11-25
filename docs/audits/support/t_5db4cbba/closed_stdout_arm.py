#!/usr/bin/env python3
"""t_5db4cbba Q3 (clean): does `_on_fatal_signal` survive a closed stdout?

Same arm as closed_stdout_arm.py but with each channel isolated so no claim rests
on a merged stream:
  * stdout : a pipe the harness drains once with a RAW os.read and then closes
             with os.close (no Python BufferedReader swallowing bytes).
  * stderr : its own pipe, read after the wait.
  * actions: a separate file the handler appends to with `>>`.

If the handler's own banner echoes die on the closed pipe before it reaches
_kill_live_legs, the actions file will say so (REAPED missing) — that is the
failure mode this card must not ship blind.
"""
import os
import subprocess
import textwrap
from pathlib import Path

BASH = "/bin/bash"
TMP = Path(os.environ["SCRATCH"])

BODY = r"""
_kill_live_legs()  { printf 'REAPED\n'   >> "$ACTIONS"; }
_leg_tmp_cleanup() { printf 'CLEANED\n'  >> "$ACTIONS"; rm -rf -- "$LEGDIR" 2>/dev/null; }
_on_fatal_signal() {
  local _sig="$1" _fallback="$2"
  echo
  echo "!! run_tests.sh RECEIVED SIG${_sig} at $(date '+%H:%M:%S') — elapsed ${SECONDS}s, during suite: ${CURRENT_SUITE:-unknown}"
  echo "   pid=$$ ppid=$PPID; parent: $(ps -o pid=,ppid=,command= -p "$PPID" 2>/dev/null | tr -s ' ')"
  echo "   re-raising SIG${_sig}."
  _kill_live_legs
  _leg_tmp_cleanup
  trap - "$_sig" 2>/dev/null
  kill -"$_sig" $$ 2>/dev/null
  exit "$_fallback"
}
__TRAP__
i=0
while [ $i -lt 400000 ]; do echo "x$i"; i=$((i+1)); done
echo LOOP-DONE >&2
"""


def run(trap):
    d = TMP / f"arm-{os.urandom(3).hex()}"
    d.mkdir(parents=True)
    legdir = d / "legdir"
    legdir.mkdir()
    (legdir / "legs.tsv").write_text("x")
    actions = d / "actions"
    actions.write_text("")
    script = (BODY.replace("__TRAP__", "trap '_on_fatal_signal PIPE 141' PIPE" if trap
                          else ":")).strip()
    p = subprocess.Popen([BASH, "-c", f"LEGDIR='{legdir}' ACTIONS='{actions}'\n{script}"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    first = b""
    try:
        first = os.read(p.stdout.fileno(), 4)
    except OSError:
        pass
    os.close(p.stdout.fileno())
    try:
        rc = p.wait(timeout=25)
    except subprocess.TimeoutExpired:
        p.kill()
        rc = "TIMEOUT"
    err = p.stderr.read().decode(errors="replace")
    raw = actions.read_text()
    gone = not legdir.exists()
    subprocess.run(["rm", "-rf", str(d)])
    return rc, first, raw, gone, err


print("=== stdout is a pipe the reader closes after 4 bytes")
for label, trap in (("untrapped (today) ", False), ("trapped  (proposed)", True)):
    rc, first, raw, gone, err = run(trap)
    shell_rc = 128 - rc if isinstance(rc, int) and rc < 0 else rc
    print(f"  {label}  rc={str(rc):>7}  shell_sees={shell_rc}")
    print(f"      actions={raw.splitlines()}  legdir_removed={gone}")
    print(f"      stdout_first={first!r}  stderr_first={err.splitlines()[:1]!r}")
