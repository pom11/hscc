#!/usr/bin/env python3
"""t_5db4cbba: what does an untrapped SIGPIPE actually cost a leg pack?

`t_b94a2b39` left SIGPIPE out of the fatal-signal trap family on the grounds that
it "measured never to leak the dir". That is true of the posture it measured —
one signal delivered to the runner's pid from outside. It says nothing about the
posture that PRODUCES a SIGPIPE in this script in the first place, which is the
one every operator and every agent uses when they want the top of a pack:

    bash scripts/run_tests.sh | head -40

Here the writer that dies is the SHELL (its own `echo`s), not an external
producer, and the consumer closing early is the caller. This probe measures both
postures against the SHIPPED runner through its own `HSCC_TEST_PY` seam, and for
each one reports the two things that matter:

    mean_orphans  legs still in flight after the runner died (CPU burned on the
                  host that runs the fleet control plane)
    leaked        the pack's /tmp/hscc-legs-* dir surviving it

Usage:  sigpipe_probe.py <repo> <workdir> [POSTURES...]
        SIGPROBE_REPEATS=n  trials per posture (default 3)

Postures:
    external   kill the runner pid with SIGPIPE (the t_b94a2b39 posture)
    head       stdout is a pipe; the reader takes N bytes and closes
    control_term   same as head but SIGTERM-shaped: the reader closes and the
                   runner is killed with TERM (what on_sigterm already covers)
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
HEAD_BYTES = int(os.environ.get("SIGPROBE_HEAD_BYTES", "200"))
#: The per-leg (seconds, rc) plan. The default is uniform and DELIBERATELY NOT
#: what to read for the closed-reader posture: with every leg the same length,
#: the runner's next write to stdout falls exactly when those legs have just
#: finished, so `head_live` scores a vacuous 0 orphans. `SIGPROBE_PLAN` with a
#: short first leg makes the runner write to a closed pipe while the OTHER two
#: are still in flight — which is the posture that can orphan legs.
#:   SIGPROBE_PLAN='hscc-commands:2:0 sparkrun-hermes:30:0 memori_byodb:30:0'
DEFAULT_PLAN = f"{LEG_A}:30:0 {LEG_B}:30:0 {LEG_C}:30:0"
PLAN = os.environ.get("SIGPROBE_PLAN", DEFAULT_PLAN)

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


def env_for(tag):
    trace, argv = WORK / f"trace.{tag}", WORK / f"argv.{tag}"
    trace.write_text("")
    argv.write_text("")
    env = dict(os.environ)
    env.update({
        "HSCC_TEST_PY": str(stub),
        "STUB_TRACE": str(trace),
        "STUB_ARGV": str(argv),
        "STUB_PLAN": PLAN,
        "HSCC_TEST_LEGS": THREE,
        "HSCC_TEST_PARALLEL": "2",
        "HSCC_LEG_HEARTBEAT_SECONDS": "0",
    })
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    return env, trace


def survivors(trace, pids):
    """Legs the runner did not reap — counted BEFORE the session is taken down."""
    live = []
    for pid in pids:
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                            capture_output=True, text=True).stdout.strip()
        if st not in ("", "Z"):
            live.append(pid)
    return live


def launch(tag, *, pipe_stdout):
    env, trace = env_for(tag)
    log = WORK / f"run.{tag}.log"
    fh = None if pipe_stdout else open(log, "wb")
    p = subprocess.Popen(["bash", str(RUNNER)], stdout=fh or subprocess.PIPE,
                         stderr=subprocess.STDOUT if pipe_stdout else None,
                         env=env, cwd=str(REPO), start_new_session=True)
    return p, trace, log, fh


def watch(p, log, fh, trace, n=2, timeout=60):
    deadline = time.time() + timeout
    m = None
    while time.time() < deadline:
        try:
            m = re.search(r"leg logs in (/tmp/hscc-legs-\S+)",
                          (fh and log.read_text()) or "")
        except Exception:
            m = None
        if m and len(trace.read_text().splitlines()) >= n:
            return m.group(1)
        time.sleep(0.1)
    return m.group(1) if m else None


def trial(posture, tag):
    before = set(Path("/tmp").glob("hscc-legs-*"))
    pipe = posture in ("head", "head_live")
    p, trace, log, fh = launch(tag, pipe_stdout=pipe)
    banner = None
    drained = b""
    try:
        if pipe:
            # Drain continuously so the runner is never blocked on a full pipe —
            # the reader stays OPEN until the pack is genuinely mid-flight, and
            # only then goes away. `head` closes as soon as it has its bytes
            # (which, measured, is BEFORE any leg launches — so it scores a
            # vacuous zero orphans); `head_live` is the posture that can
            # actually orphan legs.
            os.set_blocking(p.stdout.fileno(), False)
            deadline = time.time() + 60
            while time.time() < deadline:
                chunk = p.stdout.read(4096)
                if chunk:
                    drained += chunk
                    continue
                if posture == "head" and len(drained) >= HEAD_BYTES:
                    break
                if posture == "head_live" and len(trace.read_text().splitlines()) >= 2:
                    time.sleep(1.0)     # let the launches land in the trace
                    break
                if p.poll() is not None:
                    break
                time.sleep(0.05)
            time.sleep(1.0)
            os.close(p.stdout.fileno())
            p.stdout = None          # the finalizer must not close it twice
        else:
            banner = watch(p, log, fh, trace)
            time.sleep(6.0)
            os.kill(p.pid, getattr(signal, "SIG" +
                                   ("TERM" if posture == "control_term" else "PIPE")))
        try:
            rc = p.wait(timeout=45)
        except subprocess.TimeoutExpired:
            p.kill()
            rc = f"TIMEOUT->{p.wait(timeout=30)}"
    finally:
        if fh:
            fh.close()
    time.sleep(0.5)
    pids = [int(l.split()[-1]) for l in trace.read_text().splitlines()
            if l.startswith("start")]
    live = survivors(trace, pids)
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    after = set(Path("/tmp").glob("hscc-legs-*"))
    leaked = sorted(after - before)
    banner_dir = None
    try:
        text = (log.read_text() if fh else drained.decode(errors="replace"))
    except Exception:
        text = ""
    m = re.search(r"leg logs in (/tmp/hscc-legs-\S+)", text)
    if m:
        banner_dir = m.group(1)
    shape = {}
    for d in leaked:
        try:
            shape[d] = {f.name: f.stat().st_size for f in sorted(Path(d).iterdir())}
        except OSError as exc:
            shape[d] = {"<unreadable>": str(exc)}
    for d in leaked:
        shutil.rmtree(d, ignore_errors=True)
    if banner_dir and Path(banner_dir).exists():
        shutil.rmtree(banner_dir, ignore_errors=True)
    return dict(rc=rc, orphans=len(live), leaked=[Path(d).name for d in leaked],
                banner=banner_dir, shape=shape, saw_sigpipe=("SIGPIPE" in text),
                text=text)


if __name__ == "__main__":
    postures = sys.argv[3:] or ["external", "head", "control_term"]
    repeats = int(os.environ.get("SIGPROBE_REPEATS", "3"))
    print(f"repo={REPO} runner_exists={RUNNER.exists()} repeats={repeats} "
          f"head_bytes={HEAD_BYTES}")
    for posture in postures:
        rows = [trial(posture, f"{posture}.{i}") for i in range(repeats)]
        n = len(rows)
        leaky = sum(1 for r in rows if r["leaked"])
        orph = sum(r["orphans"] for r in rows) / n
        rcs = ",".join(str(r["rc"]) for r in rows)
        noted = sum(1 for r in rows if r["saw_sigpipe"])
        print(f"{posture:13} leaked {leaky}/{n}  mean_orphans={orph:.1f}  "
              f"rc=[{rcs}]  banner_named_sigpipe={noted}/{n}")
        for r in rows:
            for d, sh in r["shape"].items():
                print(f"              {d} -> {sh}")
