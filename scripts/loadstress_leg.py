#!/usr/bin/env python3
"""The ``loadstress`` leg: run the load-sensitive cases under bounded CPU pressure.

WHY THIS EXISTS (t_b0bcc49c)
----------------------------
The stop/sidecar teardown class is only dead when something actually runs these
files *under load*. Every one of those cases passes on an idle box and fails on
a loaded one — that is the whole class — and the fleet's history is "green
locally, red in the gate": t_8ef7fac7/#628 hardened one file and PR #673's
queue gate still bounced at 10:40Z on 2026-11-23, because nothing in the roster
enforces the property. A test that is only ever run on an idle machine proves
the test, not the product.

So this leg runs the cases marked ``@pytest.mark.loadstress`` in the two
affected files, N repeats deep, while disposable CPU burners hold the box
oversubscribed, and reports one quotable line:

    20 items x 3 repeats at load 11.42 -> 0 failures

WHAT THIS SCRIPT IS NOT
-----------------------
It is not a burner harness and it must not grow one. CPU pressure comes from
``scripts/load_burners.sh``, which owns the burners' process groups, traps
EXIT/INT/TERM/HUP and holds a hard timeout — the shape that exists because an
ad-hoc loop on 2026-11-23 orphaned twelve ``while :; do :; done`` burners at
ppid 1 for 3 h 40 m at load 24. Inlining a spinner here would re-create that
incident inside the guard written against it (and
``scripts/tests/test_load_burners.py`` fails the leg for it).

CONTRACT
--------
    scripts/loadstress_leg.py [options] [extra pytest args...]

      --collect-only        selection only: print the item count, run nothing
      --repeats N           whole-file passes per file        (default 3)
      --burners N           CPU burners while they run        (default see below)
      --timeout SECONDS     hard bound on the WHOLE stressed run (default 1200)
      --inner               internal: run passes; the burners are the parent's
      --print-selection     print one selected node id per line, then exit
      -h, --help            this text

Env: ``HSCC_LOADSTRESS_MARKER`` / ``_REPEATS`` / ``_BURNERS`` / ``_TIMEOUT``
override the defaults; ``HSCC_LOAD_BURNER_TAG`` is passed through to the harness.

Exit codes: 0 green · 1 a case failed · 2 the selection was empty (a stress leg
that selects nothing is a green lie, so it is an ERROR here, never a pass) ·
64 bad usage · 70 could not start the harness · 124 the hard timeout fired.

Posture: one pytest process per file per repeat, never two dirs in one process
(the cross-dir ``sys.path`` collision rule in ``scripts/run_tests.sh``), and
every path is passed explicitly so pytest's rootdir stays the repo root — the
marked files live in two legs and each leg's own ``pytest.ini`` would otherwise
own the marker registry.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "scripts" / "load_burners.sh"

#: The two files this class lives in. Explicit paths, not a shared dir.
FILES = (
    "hscc-api/tests/test_api_server_stop_signal.py",
    "hscc_daemon/tests/test_api_cli_stop_sidecar.py",
)

MARKER = os.environ.get("HSCC_LOADSTRESS_MARKER", "loadstress")
DEFAULT_REPEATS = int(os.environ.get("HSCC_LOADSTRESS_REPEATS", "3"))
DEFAULT_TIMEOUT = float(os.environ.get("HSCC_LOADSTRESS_TIMEOUT", "1200"))

# Tiny by design. This leg must never be the reason a box is unusable: the CI
# runner it lands on is 4 vCPU, so half a core-count is 2 burners there. The
# number of *burners* is not the claim — the measured load average printed in
# the summary is, which is why it is sampled and reported, never asserted.
CAP = int(os.environ.get("HSCC_LOADSTRESS_BURNER_CAP", "8"))


def default_burners() -> int:
    cores = os.cpu_count() or 4
    return max(2, min(CAP, (cores + 1) // 2))


# ── selection ───────────────────────────────────────────────────────────────


def _pytest_base(extra: list[str]) -> list[str]:
    """The pytest command shared by the probe and every pass.

    ``--continue-on-collection-errors`` is not decorative: if one of the two
    files fails to import, the count from ``--collect-only`` would come back
    non-zero items from the surviving file and the leg would stress half the
    class while reporting it whole.
    """
    py = os.environ.get("HSCC_TEST_PY") or sys.executable
    return [py, "-m", "pytest", "-p", "no:cacheprovider", "-m", MARKER, *extra]


def _run(cmd: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=REPO)


#: pytest's own summary line, in both shapes it uses: ``12 tests collected``
#: and — when a ``-m`` deselected something — ``8/12 tests collected (4
#: deselected)``. The leading number is the SELECTED count; reading the
#: denominator instead would have the leg certify a run of the wrong N, which is
#: the one number this leg exists to make trustworthy.
_COLLECT_RE = re.compile(r"^(\d+)(?:/\d+)? tests? collected", re.MULTILINE)


def collect(extra: list[str]) -> tuple[int, list[str], str]:
    """(item count, node ids, output) for this leg's selection.

    The count is read from pytest's own summary line rather than by counting
    lines, so a wrapped or de-duplicated node id cannot inflate it.
    """
    proc = _run(_pytest_base(["--collect-only", "-q", *FILES, *extra]))
    text = proc.stdout + proc.stderr
    ids = sorted({ln.strip() for ln in proc.stdout.splitlines()
                  if "::" in ln and any(ln.startswith(f) for f in FILES)})
    if proc.returncode != 0:
        raise SystemExit(
            f"loadstress: the selection probe exited {proc.returncode} — the "
            f"leg cannot certify a load run over a file it cannot even import."
            f"\n{text}")
    counts = _COLLECT_RE.findall(text)
    n = int(counts[-1]) if counts else len(ids)
    if n != len(ids):
        raise SystemExit(
            f"loadstress: collection is unreadable — pytest reported {n} "
            f"items but {len(ids)} node ids were listed. Refusing to run a "
            f"stress leg whose N is not a measured number.\n{text}")
    return n, ids, text


# ── load measurement ────────────────────────────────────────────────────────


class _Load:
    """Sample the 1-minute load average while the stressed run is in flight.

    The number this leg reports has to be the load it *created*, so the peak is
    sampled from inside the window, not read off the box afterwards (a 1-minute
    average read after the fact has already forgotten the run).
    """

    def __init__(self, interval: float = 2.0) -> None:
        self.interval = interval
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.samples.append(os.getloadavg()[0])
                except OSError:               # platform without loadavg
                    return
                self._stop.wait(self.interval)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> list[float]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 1)
        return self.samples


# ── the passes ──────────────────────────────────────────────────────────────


def _passes(repeats: int, extra: list[str]) -> tuple[int, int, int]:
    """Run every file ``repeats`` times. Returns (passes, passed, failed).

    One file per pytest process, always: two leg dirs in one process import
    their conftests against each other (``scripts/run_tests.sh``'s isolation
    rule), and a collision there looks like a product failure.

    The child's output is streamed, not swallowed — a stress run whose failures
    nobody can read is a leg an operator cannot triage — while its final
    summary line is mined for the real counts, so "the pass count" in the
    summary is measured rather than inferred from the exit status.
    """
    passes = passed = failed = 0
    for repeat in range(1, repeats + 1):
        for rel in FILES:
            passes += 1
            print(f"@@LOADSTRESS-RUN {repeat}/{repeats} {rel}", flush=True)
            proc = subprocess.Popen(
                _pytest_base(["-q", "-rs", rel, *extra]), cwd=REPO,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            assert proc.stdout is not None
            counts: dict[str, int] = {}
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                for key in ("passed", "failed", "error", "errors", "skipped",
                            "xfailed", "xpassed"):
                    hit = re.search(rf"(\d+) {key}\b", line)
                    if hit:
                        counts[key] = int(hit.group(1))
            rc = proc.wait()
            passed += counts.get("passed", 0)
            failed += counts.get("failed", 0) + counts.get("error", 0) \
                + counts.get("errors", 0)
            if rc:
                print(f"@@LOADSTRESS-FAIL {rel} pass {repeat} rc={rc}",
                      flush=True)
    return passes, passed, failed


# ── survivor accounting ─────────────────────────────────────────────────────


def survivors(tag: str) -> list[int]:
    """PIDs whose argv still carries the burner tag — i.e. live burners."""
    proc = subprocess.run(["pgrep", "-f", tag], capture_output=True, text=True)
    if proc.returncode == 1:                    # no match: the green state
        return []
    if proc.returncode != 0:
        print(f"loadstress: pgrep could not answer (rc={proc.returncode})",
              file=sys.stderr)
        return []
    return [int(pid) for pid in proc.stdout.split() if pid.strip().isdigit()]


def wait_until_gone(tag: str, timeout: float = 15.0) -> list[int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        left = survivors(tag)
        if not left:
            return []
        time.sleep(0.2)
    return survivors(tag)


def sweep(tag: str) -> int:
    """Kill tagged burners left by an earlier run that never reached its trap.

    A SIGKILL of the harness itself is the hole the harness cannot close (macOS
    has no ``PR_SET_PDEATHSIG``), so the leg sweeps its own tag on the way in:
    a crashed run must not stack burners onto the next one.
    """
    killed = 0
    for pid in survivors(tag):
        try:
            os.killpg(os.getpgid(pid), 9)
            killed += 1
        except (ProcessLookupError, PermissionError):
            pass
    return killed


# ── the two modes ───────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="loadstress_leg.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repeats", type=int,
                    default=int(os.environ.get("HSCC_LOADSTRESS_REPEATS",
                                               DEFAULT_REPEATS)))
    ap.add_argument("--burners", type=int,
                    default=int(os.environ.get("HSCC_LOADSTRESS_BURNERS",
                                               default_burners())))
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--collect-only", action="store_true")
    ap.add_argument("--print-selection", action="store_true")
    ap.add_argument("--inner", action="store_true", help=argparse.SUPPRESS)
    known, extra = ap.parse_known_args(argv)

    if known.repeats < 1:
        print("loadstress: --repeats must be >= 1", file=sys.stderr)
        return 64
    if known.burners < 0:
        print("loadstress: --burners must be >= 0", file=sys.stderr)
        return 64
    if known.timeout <= 0:
        # A harness whose timeout can be 0 is a harness that can skip its trap.
        print("loadstress: --timeout must be > 0", file=sys.stderr)
        return 64
    if extra and extra[0] == "--":
        extra = extra[1:]

    n, ids, _ = collect(extra)
    if known.print_selection:
        print("\n".join(ids))
        return 0
    if n == 0:
        print(f"loadstress: ZERO items carry the {MARKER!r} marker in "
              f"{', '.join(FILES)} — the leg would report green having selected "
              f"nothing. Register the marker in pytest.ini and mark the "
              f"load-sensitive cases before believing this leg.",
              file=sys.stderr)
        return 2
    if known.collect_only:
        print(f"loadstress: {n} items selected by -m {MARKER} "
              f"across {len(FILES)} files (collect-only; nothing was run)")
        return 0

    if known.inner:
        # The burners are the parent harness's, and it owns their death. All
        # this mode has to do is run the passes and report what they did — in a
        # line the outer mode can mine, because the outer one owns the summary.
        passes, passed, failed = _passes(known.repeats, extra)
        print(f"@@LOADSTRESS-TOTAL passes={passes} passed={passed} "
              f"failed={failed}", flush=True)
        return 1 if (failed or passed == 0) else 0

    # ── outer: own the pressure, then measure what it did ───────────────────
    if not HARNESS.is_file():
        print(f"loadstress: {HARNESS} is missing — this leg does not implement "
              f"its own burners, by design.", file=sys.stderr)
        return 70
    tag = os.environ.get("HSCC_LOAD_BURNER_TAG",
                         f"hscc-loadstress-{os.getpid()}")
    env = {**os.environ, "HSCC_LOAD_BURNER_TAG": tag}
    stale = sweep(tag)
    if stale:
        print(f"loadstress: swept {stale} burner(s) left by an earlier run "
              f"before adding any load of our own")

    inner = [sys.executable, str(Path(__file__).resolve()), "--inner",
             "--repeats", str(known.repeats), "--timeout", str(known.timeout),
             *extra]
    cmd = ["/bin/sh", str(HARNESS), str(known.burners), str(known.timeout),
           "--", *inner]
    if known.burners == 0:
        # Pressure is the point; 0 burners is how you run the selection without
        # it, and the summary says so rather than implying a load result.
        cmd = inner

    before = os.getloadavg()[0]
    print(f"loadstress: {n} items x {known.repeats} repeats "
          f"({len(FILES)} files, one pytest process per file per repeat) · "
          f"{known.burners} burners · cores {os.cpu_count()} · "
          f"hard timeout {known.timeout:.0f}s · interpreter {sys.executable}")
    print(f"loadstress: load 1m before = {before:.2f}")

    load = _Load()
    load.start()
    t0 = time.monotonic()
    totals = {"passes": 0, "passed": 0, "failed": 0}
    try:
        proc = subprocess.Popen(cmd, env=env, cwd=REPO,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            hit = re.search(r"@@LOADSTRESS-TOTAL passes=(\d+) passed=(\d+) "
                            r"failed=(\d+)", line)
            if hit:
                totals = {"passes": int(hit.group(1)),
                          "passed": int(hit.group(2)),
                          "failed": int(hit.group(3))}
        rc = proc.wait()
    except OSError as exc:
        print(f"loadstress: could not start the burner harness: {exc}",
              file=sys.stderr)
        return 70
    finally:
        samples = load.stop()
    peak = max(samples) if samples else 0.0
    elapsed = time.monotonic() - t0

    # The guard that the burners cannot outlive the harness. Not "should not":
    # the leg fails red if a single tagged burner is still alive here, which is
    # the same property t_71ed3b1c's harness was written for and the reason
    # this leg is allowed to load a box at all.
    left = wait_until_gone(tag)
    if left:
        print(f"loadstress: {len(left)} burner(s) {left} OUTLIVED the harness "
              f"— this is the t_71ed3b1c failure shape, not a test failure",
              file=sys.stderr)
        return 1

    status = {0: f"{totals['passed']} passed, 0 failures",
              124: "HARD TIMEOUT"}.get(rc, f"rc={rc}")
    print(f"loadstress: load 1m peak during the run = {peak:.2f} "
          f"({len(samples)} samples over {elapsed:.0f}s)")
    print(f"loadstress: {n} items x {known.repeats} repeats at load "
          f"{peak:.2f} -> {status}")
    if not totals["passes"]:
        # Nothing ran inside a run that reported success: the inner mode died
        # before its first pass, and a leg that silently ran zero passes is the
        # green lie this leg exists to refuse.
        print("loadstress: ZERO passes completed — the stress run executed "
              "nothing, which is not a green result", file=sys.stderr)
        return 2 if rc == 0 else rc
    print(f"loadstress: {totals['passes']} file-passes · survivors = 0 · "
          f"interpreter = {sys.executable}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
