"""`hscc api stop` must tear the gateway sidecar down, not just exit.

WHY THIS FILE EXISTS (card t_56552ecc, found while fixing t_6eff6258).
`api_cli._serve` owns the live ``hermes serve`` sidecar and tears it down in
its ``finally:`` (``_load_gateway_bridge()`` -> ``_bridge2.shutdown()``, then
``api.clear_api_state()``). But ``_sigterm_handler`` answered SIGTERM/SIGINT
with ``daemon_ops.write_stopped(...)`` followed by ``os._exit(0)``, and
``os._exit`` does not run ``finally:`` blocks. So the stop path — the ordinary
``hscc api stop`` verb, which SIGTERMs the pid-file instance in
``api_cli._handle_stop`` whenever the unit is not launchd-managed — never ran
the teardown: the supervised serve survived holding the API process's token,
which is the orphaned-sidecar / stale-token 403 outcome the handler was written
to prevent, reached from the other entry point.

The fix makes the stop path COOPERATIVE (the shape t_6eff6258 gave
``api_server.serve_main``): the handler sets a stop flag, ``_serve`` honours the
flag before serving and inside the serving loop, and the teardown stays in the
one ``finally:``. What the leg run then caught is the other half of the shape,
and it is why the handler here is allowed to do nothing but set that flag: a
signal handler runs on the thread it interrupts, so any work it does that takes
a lock can be a lock that thread already held — ``daemon_ops.log`` holds
``daemon_ops._handlers_lock`` (a plain non-reentrant ``threading.Lock``) while
it resolves the rotating handler, and a stop landing there deadlocked the
process, lost the stop, and left the sidecar for the escalation SIGKILL. The
blocking part (log line, PID-file bookkeeping, the shutdown request) therefore
lives on a helper thread, and the flag is a lock-free ``_StopFlag`` rather than
a ``threading.Event``, whose ``set()``/``is_set()`` take a lock the serving loop
holds on every pass.

WHAT IS RUN FOR REAL here: the actual ``hscc_daemon.api_cli`` verbs, in a
child process with a redirected HOME, a fixture token and an ephemeral loopback
port. Two things are made observable, and both are the point:

  * the sidecar, which is a REAL subprocess the fake bridge spawns and kills,
    so "torn down" is a dead pid rather than a flag a test set itself; and
  * whether the exit went THROUGH ``_serve``'s ``finally:``, which is exactly
    what ``os._exit`` skips.

Hermetic by construction: HOME points at a throwaway dir (so ``API_PID_FILE``
/ ``API_LOG_FILE`` / the token all resolve inside the sandbox), the API binds
an ephemeral loopback port, and ``gateway_bridge`` is a fake the test writes —
the live ``hermes serve`` is never spawned and the operator's live API is never
touched. The stop child also FENCES ``launchctl``: it stubs ``launchd_status``
to "not available" (the sandbox has a pid file, which would otherwise make
``_launchd_matters()`` ask the operator's live gui domain, and a loaded unit
would make the stop bootout the LIVE job) and makes ``_launchctl_run`` raise,
so a future edit that routes around the stub reddens here instead of touching
the machine.
"""
import ast
import collections
import inspect
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import hscc_daemon.api_cli as api_cli

REPO = Path(__file__).resolve().parents[2]

#: Fixture DATA, never a real credential, and never printed in a message.
TOKEN = "sandbox-dummy-token-for-stop-sidecar"

# Lines the fake sidecar / harness write, kept as the single constants the
# waits and the assertions share: a rename in the harness reddens the pin that
# couples them rather than turning a case into a 30 s timeout.
SIDECAR_STARTED = "sidecar started"
SIDECAR_SHUTDOWN = "sidecar shutdown called"
SIDECAR_STOPPED = "sidecar stopped"
SERVE_RETURNED = "harness: _serve returned"

#: What the `serve-lograce` child prints while it works, and what the parent
#: waits on. Constants for both sides, so a rename on one side reddens the
#: assertion that reads the other instead of turning a case into a timeout.
#: `stage:` lines carry the child's phase; the last one seen is in every failure
#: message, which is what makes a red here diagnosable without a re-run.
STAGE_PREFIX = "stage:"
#: Inside the log hook, holding `daemon_ops._handlers_lock`, BEFORE the signal is
#: raised: the window was entered.
RACE_ENTERED = "@@INSIDE-LOG-WRITE@@"
#: ...and the handler came back out of it. This is the line the case asserts on.
INTERRUPTED_INSIDE_LOG = "@@INTERRUPTED-INSIDE-LOG@@"
#: The child's watchdog thread observed that the handler had still not returned
#: `HANDLER_GRACE` seconds after the raise. An OBSERVATION, not a verdict: a
#: throttled task can also simply not have been running (measured: under ~100x
#: oversubscription this line appears while the handler is merely unscheduled),
#: so nothing reddens on it. It is what lets a child that then goes silent be
#: named — see :func:`_await_child`.
HANDLER_LATE = "@@HANDLER-LATE@@"
#: The handler WAS dispatched and reached its first write: the child's watchdog
#: read `api_cli._last_signal` — the product's own record, written by the first
#: line of `_sigterm_handler` — and saw it. Informational only: a healthy child
#: can exit before the watchdog thread is scheduled to say anything, so nothing
#: asserts on this line; it is there so a child that goes silent afterwards can
#: be told apart from one whose stop was never dispatched at all.
HANDLER_RAN = "@@HANDLER-RAN@@"

#: What the `serve-wedged` child writes into its marker file from INSIDE the
#: blocked `service_actions`. The write is the announcement that the listener loop
#: has actually reached the seam, and the parent waits on it before signalling:
#: a stop that arrives first lets that loop exit before `service_actions` is ever
#: called, `shutdown()` returns at once, and the case passes on a build that still
#: hangs (the trap t_60932269 recorded for its own wedge).
WEDGE_MARKER_TEXT = "wedge entered"
#: Prefix of the line the `serve-wedged` arm prints with the code `_serve` itself
#: returned. The child's own exit status is only the harness repeating it, so this
#: is what tells a wrong verdict from a harness that stopped forwarding one.
SERVE_RC_PREFIX = "rc="

#: The message the `serve-crashed` arm raises with, and the string the parent
#: looks for in the child's API log — one constant for both sides, so a rename
#: reddens the assertion that reads the other instead of silencing the case.
#: "harness-injected" is part of it on purpose: a line that says so cannot be
#: mistaken later for a failure the product itself had.
CRASH_TEXT = "harness-injected serving-loop failure"
#: The `serve-crashed` arm's mode argument. `NO_CRASH` is the control — the same
#: arm with the seam left alone, stopped by SIGTERM like any ordinary stop — and
#: it is `'-'` for the same reason the wedge arm's control is: the two arms then
#: differ in one argument rather than in a code path.
CRASH_MODE = "crash"
NO_CRASH = "-"

# ━━━ load budgets, not behaviour budgets ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# None of these can turn a wrong answer into a right one: they only decide how
# long a LIVE child may take. Every one of them is measured, and the measurement
# is why they are this size.
#
#   idle 14-core host: the child's first stdout line lands ~0.15 s after spawn
#   and the whole lograce arm (both signals) runs in 1.76 s.
#   contended GitHub runner, run 45081900690: the same child had not written ONE
#   byte of stdout — and no `api.log` at all — in the 25 s the old
#   `communicate(timeout=25)` allowed, i.e. it was still inside its imports.
#   The child had not reached the window the case is about, yet the case blamed
#   the signal handler.
#
# So the budgets are sized for a runner that is two orders of magnitude slower
# than an idle one, and a stall is judged against the child's own progress rather
# than against its whole life.
#: Silence from a live child between two announced stages (or, once the parent
#: has asked it to stop, between the signal and its exit) before it is called
#: stuck. ~1000x the idle gap, ~7x the worst CI observation.
CHILD_STAGE_STALL = 180.0
#: One child's entire life, whatever it is doing — the backstop behind the stall.
CHILD_LIFE_CAP = 420.0
#: Spawn -> the child's own `HSCC API listening on ...` line. Same import phase
#: as above, so the same order of budget.
CHILD_READY_BUDGET = 180.0
#: How long the child's watchdog waits, AFTER the signal has been raised, for the
#: handler to come back out of the log-write window before it says the handler is
#: still in there. The handler's body is an attribute write and a return, so this
#: is generous about the work; it is only ever an observation, because a
#: throttled task can also be unscheduled (see :data:`HANDLER_LATE`).
HANDLER_GRACE = 8.0
#: How long a marker the child writes BEFORE it exits may take to land on disk.
CHILD_MARKER_BUDGET = 60.0

# What the fake bridge / harness read out of the environment. Named once, here.
ENV_SIDECAR_DIR = "HSCC_FAKE_SIDECAR_DIR"
ENV_GATEWAY_DIR = "HSCC_FAKE_GATEWAY_DIR"
ENV_REPO_ROOT = "HSCC_REPO_ROOT"
#: Where the `serve-wedged` child announces that its listener loop has reached the
#: seam it is about to block in. A path, like the two above, so no
#: machine-derived path has to be a spawn argument of the caller's Popen.
ENV_WEDGE_MARKER = "HSCC_FAKE_WEDGE_MARKER"


# --------------------------------------------------------------------------- #
# the fake sidecar: a real subprocess, so "gone" means a dead pid
# --------------------------------------------------------------------------- #

FAKE_BRIDGE = '''\
"""Stand-in for hscc-api/gateway_bridge.py, written by the test.

Only the two functions `api_cli._serve` calls are real work: `start_serve`
spawns an actual subprocess (the "sidecar") and records its pid, `shutdown`
terminates it. Everything else is the inert surface the module contract
requires. The lifecycle it records is the assertion: a hard exit skips
`shutdown`, and the pid it started stays alive.
"""
import os
import subprocess
import sys
import threading

STATE = os.environ["@@SIDECAR_DIR_ENV@@"]
_LOCK = threading.Lock()
_proc = None


def _mark(text):
    with open(os.path.join(STATE, "marker.log"), "a") as fh:
        fh.write(text + "\\n")
        fh.flush()


def start_serve(port=None, bin_path=None, **flags):
    global _proc
    with _LOCK:
        if _proc is not None and _proc.poll() is None:
            return True
        _proc = subprocess.Popen(
            [sys.executable, os.path.join(STATE, "dummy_sidecar.py"), STATE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(os.path.join(STATE, "sidecar.pid"), "w") as fh:
            fh.write(str(_proc.pid))
        _mark("@@STARTED@@ pid=%s port=%s" % (_proc.pid, port))
        return True


def shutdown():
    global _proc
    with _LOCK:
        proc, _proc = _proc, None
    _mark("@@SHUTDOWN@@")
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _mark("@@STOPPED@@")


def is_serve_up():
    return _proc is not None and _proc.poll() is None


def serve_endpoint():
    return None


def ensure_mounted(project, registry_path=None):
    return False


def mounted(project):
    return False


def _reset_for_tests():
    pass
'''
# Tokens rather than %-formatting: this template is itself Python source that
# uses %, and `%%`-escaping inside a nested code literal is how such a harness
# silently stops matching the constants it is supposed to embed.
FAKE_BRIDGE = (FAKE_BRIDGE
               .replace("@@SIDECAR_DIR_ENV@@", ENV_SIDECAR_DIR)
               .replace("@@STARTED@@", SIDECAR_STARTED)
               .replace("@@SHUTDOWN@@", SIDECAR_SHUTDOWN)
               .replace("@@STOPPED@@", SIDECAR_STOPPED))

DUMMY_SIDECAR = '''\
"""The stand-in `hermes serve`: exist, hold a pid, wait to be killed.

Deliberately does nothing else. Its only job is to be a process that outlives
the API when the API hard-exits without calling `shutdown()`.
"""
import time

time.sleep(600)
'''

HARNESS = '''\
"""Sandbox harness: run the REAL `hscc api` verbs (or the serve loop alone).

argv: <verb> [verb args...]   and the paths out of the environment, so no
machine-derived path has to be a spawn argument of the caller's Popen.

  start / stop  -> straight into `api_cli.cmd_api`, the dispatch `hscc` itself
                   reaches. `start` double-forks, so this process exits once the
                   server grandchild is on its own — like the real CLI.
  serve         -> `api_cli._serve` in THIS process, for the arm that needs an
                   observable exit code and a witness that the run came back
                   through _serve's finally: instead of being cut short.
  serve-wedged  -> the same, with one listener's accept loop parked inside
                   `service_actions`, so the bounded stop has to abandon it. The
                   exit code and the WARN are what the arm exists to make
                   observable; see `test_a_wedged_listener_is_abandoned_reported`.
  serve-crashed -> the same, with the serving loop replaced by one that raises,
                   after the binds and the sidecar startup have run for real. The
                   exit code and the ERROR line are what the arm exists to make
                   observable; see `test_a_crash_in_the_serving_loop_reports_1`.

The fake gateway_bridge is imported BEFORE anything else:
`api_cli._load_gateway_bridge()` puts the hscc-api dir on sys.path and then
`import gateway_bridge`, so the only reliable seam is a module that is already
in sys.modules — that is what makes this process start a FAKE sidecar.
"""
import os
import sys

# The child's first words, BEFORE anything that a loaded runner can stretch.
# The parent waits on this file's progress (:func:`_await_child`), so a stage
# line has to exist before the imports that ran 25 s long on CI — otherwise the
# parent has nothing to be patient about and guesses from a stopwatch.
print("@@MARK_STAGE@@ harness-up", flush=True)

sys.path.insert(0, os.environ["@@REPO_ROOT_ENV@@"])
sys.path.insert(0, os.environ["@@GATEWAY_DIR_ENV@@"])
import gateway_bridge  # noqa: F401  (the fake; claimed before hscc-api's dir)
print("@@MARK_STAGE@@ fake-bridge-imported", flush=True)

from hscc_daemon import api_cli
print("@@MARK_STAGE@@ api_cli-imported", flush=True)

verb, rest = sys.argv[1], sys.argv[2:]

if verb == "serve":
    # Install the stop handlers EXACTLY the way `hscc api start` does in its
    # forked child (api_cli._handle_start). This arm skips the double fork only
    # so the exit code stays observable, and skipping the registration would
    # leave the SIGTERM answered by the kernel's default action — the case
    # would then measure the harness, not the handler. Pin
    # TestStopPathIsCooperative.test_the_serving_process_is_given_the_handler
    # keeps `_handle_start` registering both signals, so this line cannot drift
    # away from production on its own.
    import signal as _signal
    _signal.signal(_signal.SIGTERM, api_cli._sigterm_handler)
    _signal.signal(_signal.SIGINT, api_cli._sigterm_handler)

    print("@@MARK_STAGE@@ serve-enter", flush=True)
    _api = api_cli._load_api_server()
    # The verdict `_serve` returns IS the serving process's exit status in
    # production (`_handle_start` does `os._exit(_serve(...))`), so this arm
    # forwards it. Swallowing it here — `raise SystemExit(0)` regardless — is how
    # a harness keeps passing while the entry path it imitates reports something
    # else; `@@SERVE_RETURNED@@` below stays the witness that the run came back
    # through `_serve` rather than being cut short.
    _rc = api_cli._serve(_api, ["127.0.0.1"], False, int(rest[0]))
    print("@@SERVE_RETURNED@@ @@SERVE_RC_PREFIX@@%s" % _rc, flush=True)
    raise SystemExit(_rc)

if verb == "serve-wedged":
    # The BOUNDED stop seen from this entry path — the one `hscc api start` takes.
    # One listener's accept loop is parked inside `service_actions`, the seam the
    # stdlib calls on every pass of `serve_forever`, so that listener's
    # `BaseServer.shutdown()` can never return and the stop has to give up on it
    # to reach the teardown at all. t_60932269 put the bound in
    # `_MultiServer.serve_forever`'s flag branch; this arm is about the caller that
    # has to honour what it reports (t_54159e29), which is why the wedge goes
    # through the real `create_servers` and the real `_serve` rather than a stub.
    #
    # argv: <port> <binds-csv> <host-to-wedge|->   — '-' wedges nothing, which is
    # the control arm: everything below the wedge is byte-identical, so the pair
    # says whether the WARN and the exit code track the wedge or just happen to
    # appear on every stop.
    import signal as _signal
    import time as _time
    _signal.signal(_signal.SIGTERM, api_cli._sigterm_handler)
    _signal.signal(_signal.SIGINT, api_cli._sigterm_handler)

    _port, _binds, _to_wedge = int(rest[0]), rest[1].split(","), rest[2]
    _marker = os.environ["@@WEDGE_MARKER_ENV@@"]
    _api = api_cli._load_api_server()
    _real_create_servers = _api.create_servers

    def _create_servers_with_a_wedge(*a, **kw):
        _srv = _real_create_servers(*a, **kw)
        for _s in _srv.servers:
            if _s.server_address[0] != _to_wedge:
                continue
            # NO `self` in the signature. This is an INSTANCE attribute
            # replacement, and the stdlib calls it as `self.service_actions()` with
            # no arguments: a def that takes one raises TypeError inside the
            # listener instead of wedging it, and the case passes for the wrong
            # reason (the trap t_60932269 hit with its own wedge).
            def _wedged_service_actions():
                # Announce ENTRY, then block. Writing this from inside the blocked
                # call is what makes the parent's wait meaningful — see
                # WEDGE_MARKER_TEXT. And NO trailing newline in what it writes:
                # this template is itself Python source inside a string, so an
                # escape sequence typed here lands as a real newline in the
                # GENERATED file — a SyntaxError there, not here, where every arm
                # of the harness dies at import and each case reports a stalled
                # child instead. TestTheHarnessIsStillPython compiles it here.
                with open(_marker, "w") as _fh:
                    _fh.write("@@WEDGE_ENTERED@@")
                while True:
                    _time.sleep(3600)
            _s.service_actions = _wedged_service_actions
        return _srv

    _api.create_servers = _create_servers_with_a_wedge
    print("@@MARK_STAGE@@ serve-wedged-enter", flush=True)
    _rc = api_cli._serve(_api, _binds, False, _port)
    print("@@SERVE_RETURNED@@ @@SERVE_RC_PREFIX@@%s" % _rc, flush=True)
    raise SystemExit(_rc)

if verb == "serve-crashed":
    # The CRASH path seen from this entry path — the one `hscc api start` takes.
    # An exception escapes the serving loop AFTER every listener is bound and the
    # fake sidecar is really up, so `_serve`'s `except Exception` arm runs, its
    # `finally:` still owns the sidecar teardown, and the process's exit status is
    # the only thing left that can say the API died rather than stopped. See
    # `test_a_crash_in_the_serving_loop_reports_1_and_still_tears_down`.
    #
    # The raise is injected AT THE `serve_forever` SEAM rather than deep inside it,
    # deliberately and for the reason t_60932269 gave for injecting its wedge at
    # `service_actions`: nothing in the product raises out of
    # `_MultiServer.serve_forever` today — the listener loops run on their own
    # threads (so an exception there dies in the thread, it does not reach
    # `_serve`) and the bounded stop RETURNS rather than raising. The arm therefore
    # stands in for the class of failure `serve_main`'s own `except Exception`
    # catches, and everything it makes observable is real: a real `except Exception`
    # in the product's `_serve`, a real teardown of a real child process, a real
    # exit status.
    #
    # argv: <port> <binds-csv> <crash|->   — '-' injects nothing, which is the
    # control arm: byte-identical below the seam, stopped by SIGTERM like any
    # ordinary stop, so the pair says whether the 1 and the ERROR line track the
    # crash or merely accompany every exit.
    import signal as _signal
    _signal.signal(_signal.SIGTERM, api_cli._sigterm_handler)
    _signal.signal(_signal.SIGINT, api_cli._sigterm_handler)

    _port, _binds, _mode = int(rest[0]), rest[1].split(","), rest[2]
    _api = api_cli._load_api_server()
    if _mode != "@@NO_CRASH@@":
        _real_create_servers = _api.create_servers

        def _create_servers_with_a_crash(*a, **kw):
            # The real `create_servers` still runs, so the binds, the bind report
            # and the sidecar startup are the product's own; only the serving loop
            # that `_serve` then calls is replaced.
            _srv = _real_create_servers(*a, **kw)

            def _serving_loop_that_raises(**_kw):
                # `**_kw` is load-bearing: `_serve` calls
                # `serve_forever(stop_event=...)`, so a def that accepts nothing
                # would die of TypeError before the injected failure ever ran and
                # the case would pass on the wrong exception. And NO `self` — this
                # is an INSTANCE attribute replacement, the same trap the wedge arm
                # documents at its own seam.
                raise RuntimeError("@@CRASH_TEXT@@")

            _srv.serve_forever = _serving_loop_that_raises
            return _srv

        _api.create_servers = _create_servers_with_a_crash
    print("@@MARK_STAGE@@ serve-crashed-enter", flush=True)
    _rc = api_cli._serve(_api, _binds, False, _port)
    print("@@SERVE_RETURNED@@ @@SERVE_RC_PREFIX@@%s" % _rc, flush=True)
    raise SystemExit(_rc)

if verb == "serve-lograce":
    # The state a stop has to survive: the signal lands while THIS thread is
    # inside a log write. `daemon_ops.log` -> `append_rotating` -> `_get_logger`
    # holds `daemon_ops._handlers_lock`, a plain non-reentrant `threading.Lock`,
    # and a signal handler runs on the thread it interrupts — so a handler that
    # logs (as `api_cli._sigterm_handler` did until t_56552ecc measured the
    # trap) waits on a lock only its own thread can release. Nothing else in the
    # stop path then happens: no flag, no return from `_serve`, no sidecar
    # teardown, and `hscc api stop` escalates to SIGKILL, which skips the
    # teardown too.
    #
    # Not a pretend log write: it takes the same lock the real write takes. It
    # fires on the FIRST log line of startup, which is where a stop from the
    # ordinary `stop` verb actually lands — the API logs several times before an
    # accept loop exists, and the leg run that first caught this hit exactly
    # that window. `_serve` then goes on to bind and start the sidecar, so the
    # teardown still has a real child to kill, and the stop flag the handler set
    # is what keeps the accept loops from ever starting.
    import signal as _signal
    import threading as _threading
    import time as _time
    from hscc_daemon import daemon_ops

    _signum = getattr(_signal, rest[1])
    _real_log = daemon_ops.log
    _fired = []
    _raised = _threading.Event()

    def _watchdog():
        # The trap this arm exists for is a handler that never comes back, and the
        # only witness that can see it is a thread the blocked thread is not
        # holding: `threading.Lock.acquire()` gives up the GIL while it waits, so
        # this thread still runs while the main thread is stuck inside the
        # handler.
        #
        # What it waits on is the product's OWN record — `api_cli._last_signal`,
        # written by the first line of `_sigterm_handler` — and not a line the
        # harness could print whether or not the handler ever ran. `raise_signal`
        # returning is NOT proof the handler was dispatched: measured on the full
        # leg run, a `serve-lograce` child printed `@@INTERRUPTED-INSIDE-LOG@@`,
        # started its sidecar and then served forever on a stop it had already
        # been given, and a harness-side "it came back" flag had no way to tell
        # that apart from a handler that ran and was ignored.
        #
        # It measures from the RAISE, not from its own birth — measured the hard
        # way under ~100x oversubscription, a grace clocked from here accused the
        # handler of a deadlock while the child was still crossing
        # `_load_api_server()` and had not raised anything yet.
        #
        # And it only OBSERVES. A task the kernel has simply not scheduled looks
        # identical to a task deadlocked on a lock from the outside, so this line
        # can be a slow runner as easily as a bug; what turns it into a verdict is
        # the child then going silent for the whole stall budget, which is what
        # :func:`_await_child` decides.
        _raised.wait()
        deadline = _time.monotonic() + @@HANDLER_GRACE@@
        while _time.monotonic() < deadline:
            if api_cli._last_signal is not None:
                # Positive, informational: the handler was dispatched and got
                # past its first write. Deliberately NOT asserted on — printing
                # this is the watchdog's job and a healthy child can exit before
                # the thread is scheduled to say anything.
                print("@@MARK_RAN@@ signum=%s" % api_cli._last_signal, flush=True)
                return
            _time.sleep(0.05)
        print("@@MARK_LATE@@ %s had still not returned %.0fs after the raise "
              "(_last_signal=%r)"
              % (rest[1], @@HANDLER_GRACE@@, api_cli._last_signal), flush=True)

    _threading.Thread(target=_watchdog, daemon=True).start()

    def _log(msg, *a, **kw):
        if not _fired:
            _fired.append(str(msg))
            with daemon_ops._handlers_lock:   # exactly where the real write stands
                print("@@MARK_RACE@@", flush=True)   # the window is entered
                _raised.set()                # the grace clock starts HERE
                _signal.raise_signal(_signum)  # the stop lands here, on this thread
                _time.sleep(0.4)              # a handler that waits, waits here
            print("@@MARK_INTERRUPTED@@", flush=True)
        return _real_log(msg, *a, **kw)

    daemon_ops.log = _log
    _signal.signal(_signal.SIGTERM, api_cli._sigterm_handler)
    _signal.signal(_signal.SIGINT, api_cli._sigterm_handler)

    print("@@MARK_STAGE@@ serve-enter", flush=True)
    _api = api_cli._load_api_server()
    api_cli._serve(_api, ["127.0.0.1"], False, int(rest[0]))
    daemon_ops.log = _real_log
    print("@@SERVE_RETURNED@@", flush=True)
    raise SystemExit(0)

if verb == "stop":
    # Never ask the operator's live gui domain. A sandbox pid file makes
    # _launchd_matters() True, so the probe would otherwise really run here;
    # the stub says "no supervisor", which is the truth inside this sandbox,
    # and the fence turns any route around it into a red test.
    api_cli.launchd_status = lambda **kw: {
        "available": False, "loaded": False, "pid": None, "state": None}

    def _fence(argv, timeout=10):
        raise AssertionError(
            "a sandboxed stop asked the live supervisor: %s" % (argv,))

    api_cli._launchctl_run = _fence

raise SystemExit(api_cli.cmd_api([verb] + list(rest)))
'''
HARNESS = (HARNESS
           .replace("@@REPO_ROOT_ENV@@", ENV_REPO_ROOT)
           .replace("@@GATEWAY_DIR_ENV@@", ENV_GATEWAY_DIR)
           .replace("@@WEDGE_MARKER_ENV@@", ENV_WEDGE_MARKER)
           .replace("@@WEDGE_ENTERED@@", WEDGE_MARKER_TEXT)
           .replace("@@SERVE_RC_PREFIX@@", SERVE_RC_PREFIX)
           .replace("@@SERVE_RETURNED@@", SERVE_RETURNED)
           .replace("@@CRASH_TEXT@@", CRASH_TEXT)
           .replace("@@NO_CRASH@@", NO_CRASH)
           .replace("@@MARK_STAGE@@", STAGE_PREFIX)
           .replace("@@MARK_RACE@@", RACE_ENTERED)
           .replace("@@MARK_INTERRUPTED@@", INTERRUPTED_INSIDE_LOG)
           .replace("@@MARK_LATE@@", HANDLER_LATE)
           .replace("@@MARK_RAN@@", HANDLER_RAN)
           .replace("@@HANDLER_GRACE@@", repr(HANDLER_GRACE)))


# --------------------------------------------------------------------------- #
# sandbox
# --------------------------------------------------------------------------- #

def _free_port():
    """An ephemeral loopback port, released before the child binds it."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _child_env(home):
    """Env for a sandboxed child: the operator's state must not be reachable.

    HOME is the redirect that matters (it is what `os.path.expanduser` on
    ``API_PID_FILE`` / ``API_LOG_FILE`` and the server's ``DEFAULT_HSCC_DIR``
    resolve through), and the drops keep a real profile home, a real hermes
    home, a real profiles-root intent, or a forced launchd probe from leaking
    into a child that is supposed to believe it lives in tmp.
    """
    drop = {"PYTHONPATH", "VIRTUAL_ENV", "HERMES_HOME", "HERMES_REAL_HOME",
            "HSCC_HOME", "HSCC_DIR", "HSCC_LAUNCHD_PROBE",
            "HSCC_API_PROFILES_ROOT", "HSCC_SPARKRUN_BIN",
            "HSCC_TEST_PY"}
    env = {k: v for k, v in os.environ.items()
           if k not in drop and not k.startswith("CONDA_")
           and not k.startswith("HERMES_")}
    env.update({"HOME": str(home), "NO_COLOR": "1", "TERM": "dumb"})
    return env


@pytest.fixture()
def sandbox(tmp_path):
    """A throwaway HOME plus the fake bridge, harness and sidecar script."""
    home = tmp_path / "home"
    hscc = home / ".hscc"
    hscc.mkdir(parents=True)
    (hscc / "api-token").write_text(TOKEN + "\n")

    gateway = tmp_path / "fake_gateway"
    gateway.mkdir()
    (gateway / "gateway_bridge.py").write_text(FAKE_BRIDGE)

    sidecar_dir = tmp_path / "sidecar"
    sidecar_dir.mkdir()
    (sidecar_dir / "dummy_sidecar.py").write_text(DUMMY_SIDECAR)

    harness_dir = tmp_path / "harness"
    harness_dir.mkdir()
    harness = harness_dir / "api_harness.py"
    harness.write_text(HARNESS)

    env = _child_env(home)
    env.update({ENV_SIDECAR_DIR: str(sidecar_dir),
                ENV_GATEWAY_DIR: str(gateway),
                ENV_REPO_ROOT: str(REPO),
                ENV_WEDGE_MARKER: str(tmp_path / "wedge.log")})
    box = {"home": home, "hscc": hscc, "gateway": gateway,
           "sidecar": sidecar_dir, "harness": harness, "env": env,
           "marker": sidecar_dir / "marker.log",
           "wedge": tmp_path / "wedge.log",
           "sidecar_pid": sidecar_dir / "sidecar.pid",
           "pid_file": hscc / "api.pid", "log": hscc / "api.log"}
    yield box
    # No test may leave a bound API or an orphaned sidecar behind for the next
    # one: both are read from the sandbox files the children wrote.
    for path, sig in ((box["pid_file"], signal.SIGKILL),
                      (box["sidecar_pid"], signal.SIGKILL)):
        pid = _read_int(path)
        if pid is not None and _alive(pid):
            try:
                os.kill(pid, sig)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# small observations
# --------------------------------------------------------------------------- #

def _read_int(path):
    try:
        return int(Path(path).read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _marker_lines(sandbox):
    try:
        return (sandbox["marker"]).read_text().splitlines()
    except FileNotFoundError:
        return []


def _have(sandbox, needle, timeout=CHILD_MARKER_BUDGET):
    """True once the fake bridge has recorded `needle`.

    The bridge writes these from the API process, so the wait crosses a process
    boundary and is a load wait like every other one in this file.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(needle in line for line in _marker_lines(sandbox)):
            return True
        time.sleep(0.05)
    return False


def _describe(sandbox, extra=""):
    """Everything a reviewer needs when a case goes red, read from disk."""
    bits = [extra] if extra else []
    bits.append(f"marker: {_marker_lines(sandbox)}")
    for key in ("pid_file", "log", "sidecar_pid"):
        path = sandbox[key]
        try:
            text = Path(path).read_text()
        except FileNotFoundError:
            text = "<absent>"
        bits.append(f"{key} ({path.name}): {text[:2000]}")
    return "\n".join(bits)


#: A verb that ran to completion. `pid` is the harness's own pid — the value
#: `hscc api start` transiently writes into the pid file before the server
#: grandchild overwrites it, which is what the readiness wait filters on.
Done = collections.namedtuple("Done", ("returncode", "stdout", "stderr", "pid"))

#: Where the `start` verb's output goes. A FILE, and that is load-bearing —
#: see :func:`_start_api`.
START_OUTPUT = "start.out"


def _harness_argv(sandbox, verb, *args):
    return [sys.executable, "-u", str(sandbox["harness"]), verb, *args]


def _start_api(sandbox, port):
    """`hscc api start`, with the child's output redirected to a FILE.

    Why not `capture_output=True`, measured rather than guessed: `start`
    double-forks and the serving grandchild INHERITS the CLI parent's
    descriptors. With a pipe, the grandchild holds the write end open, so
    `communicate()` waits for an EOF that cannot arrive until the server
    exits — the first draft of this case sat there through the whole 90 s
    timeout and then hung again on the cleanup read. A redirected file has no
    EOF to wait for, and `wait()` returns when the CLI parent exits, which is
    exactly the promise `start` makes.

    stderr is folded into the same file: the CLI's own report and the
    `daemon_ops.log` echo land together, and nothing here asserts on one
    without the other.
    """
    argv = _harness_argv(sandbox, "start", "--no-qr", "--port", str(port))
    out_path = sandbox["home"].parent / START_OUTPUT
    with open(out_path, "w") as fh:
        proc = subprocess.Popen(argv, env=sandbox["env"], stdout=fh,
                                stderr=subprocess.STDOUT, text=True)
        try:
            rc = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
            raise AssertionError(
                f"`hscc api start` did not return within 60s; its own output:\n"
                f"{out_path.read_text()}\n{_describe(sandbox)}") from None
    return Done(rc, out_path.read_text(), "", proc.pid)


def _run(sandbox, verb, *args):
    """Run a NON-FORKING verb to completion and collect its output.

    `stop` fits: it signals someone else's pid and returns when that pid is
    gone, so the pipes belong to this child alone and `communicate()` ends
    when it ends. A verb that forks must go through :func:`_start_api`
    instead, for the reason written there.
    """
    proc = subprocess.Popen(_harness_argv(sandbox, verb, *args),
                            env=sandbox["env"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return Done(proc.returncode, out, err, proc.pid)


def _popen(sandbox, verb, *args):
    """Run one verb in the background (the arm that signals it afterwards)."""
    return subprocess.Popen(_harness_argv(sandbox, verb, *args),
                            env=sandbox["env"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)


# --------------------------------------------------------------------------- #
# waiting on a child: progress, not elapsed time
# --------------------------------------------------------------------------- #

class _ChildTrail:
    """A live child's stdout, kept as it arrives, with a silence clock on it.

    Why not `communicate(timeout=N)`: that judges the child's WHOLE LIFE against
    one stopwatch, so a runner that is merely slow at `import` reddens a case
    whose behaviour is fine. Measured, run 45081900690: the `serve-lograce` child
    had written no stdout and no `api.log` in 25 s — it never reached the
    log-write window the case is about — and the case reported the signal handler
    wedged. Nothing was wedged; the child had not been asked yet.

    Here the wait is keyed on progress. The harness announces bounded stages
    (every gap between two of them is one import step), each line resets this
    clock, and the budget therefore only runs out on a child that has stopped
    doing anything — not on one that is doing a lot of it slowly.
    """

    def __init__(self, proc):
        self._proc = proc
        self._lines = []
        self._lock = threading.Lock()
        self._since = time.monotonic()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        try:
            for line in self._proc.stdout:
                with self._lock:
                    self._lines.append(line.rstrip("\n"))
                    self._since = time.monotonic()
        except (ValueError, OSError):
            # the pipe went away under us (the caller closed it after the child
            # died); whatever was collected is still the truth up to that point.
            pass

    def mark(self):
        """Restart the silence clock: the parent is about to ask for something.

        A serving child is SILENT by design, so a stall measured across the whole
        run would execute a healthy server. The parent marks at the moment it
        signals, and from there the clock measures exactly the property under
        test — how long the stop takes to produce a word.
        """
        with self._lock:
            self._since = time.monotonic()

    def idle(self):
        with self._lock:
            return time.monotonic() - self._since

    def text(self):
        with self._lock:
            return "\n".join(self._lines)

    def saw(self, needle):
        return needle in self.text()

    def last(self):
        with self._lock:
            return self._lines[-1] if self._lines else "<nothing at all>"

    def finish(self, timeout=10.0):
        """Once the child is gone: drain what it buffered, then close the pipe."""
        self._thread.join(timeout)
        try:
            self._proc.stdout.close()
        except (ValueError, OSError):
            pass


#: What :func:`_await_child` settled for. `clean` is the only verdict the child
#: earned by itself; `stalled` and `wedged` both mean the parent had to kill it,
#: and `detail` says which clock ran out.
ChildRun = collections.namedtuple(
    "ChildRun", ("returncode", "output", "verdict", "detail"))


def _kill_child(proc):
    if proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def _stuck_verdict(trail):
    """`wedged` when the child itself said the handler was still in the window.

    The `@@HANDLER-LATE@@` line ALONE is not a verdict — a task the scheduler
    simply passed over looks identical from here, and under ~100x oversubscription
    this file's own rig measured it firing on a child that then finished fine.
    But a child that said it and then produced NOTHING for the whole stall budget
    is describing a lock it cannot leave, which is the defect this file exists for.
    The line supplies the diagnosis; the silence supplies the verdict.
    """
    return "wedged" if trail.saw(HANDLER_LATE) else "stalled"


def _await_child(proc, trail, *, stall=CHILD_STAGE_STALL, cap=CHILD_LIFE_CAP):
    """Wait for a backgrounded child to exit, judging SILENCE not elapsed time.

    Returns a :data:`ChildRun`:

      ``clean``    the child exited on its own; `returncode` is its and the
                   caller asserts on what it did.
      ``stalled``  the parent had to kill it: a live child announced nothing for
                   `stall` seconds, or outlived `cap` while still talking, with no
                   such observation on record. `detail` says which clock ran out.
      ``wedged``   the child went silent, and its own watchdog thread (see the
                   `serve-lograce` harness) had said the handler was still inside
                   the log-write window when the raise happened. The diagnosis is
                   the child's; the silence is what makes it a verdict.

    There is deliberately no separate ``capped`` verdict: a child that will not
    finish and a child that will not speak are the same finding for a caller, and
    which clock ran out belongs in `detail` rather than in a branch nobody reads.

    In either of the other two the child has already been killed when this
    returns. The price of the design is that a genuine wedge now costs the stall
    budget to
    report instead of a few seconds; a case that accuses a healthy child on a
    busy runner costs the fleet a re-run triage every time it fires, and this one
    has already done exactly that on CI.
    """
    deadline = time.monotonic() + cap
    while True:
        returncode = proc.poll()
        if returncode is not None:
            trail.finish()
            return ChildRun(returncode, trail.text(), "clean", "")
        idle = trail.idle()
        if idle > stall:
            _kill_child(proc)
            trail.finish()
            return ChildRun(None, trail.text(), _stuck_verdict(trail),
                            f"went silent for {idle:.0f}s after {trail.last()!r}")
        if time.monotonic() > deadline:
            _kill_child(proc)
            trail.finish()
            return ChildRun(None, trail.text(), _stuck_verdict(trail),
                            f"still alive after {cap:.0f}s, last line "
                            f"{trail.last()!r}")
        time.sleep(0.05)


def _wait_listening(sandbox, port, timeout=CHILD_READY_BUDGET):
    """Block until the child's OWN log says it is listening on `port`.

    The log line — not a port probe — is the readiness here: `_serve` writes it
    after every bind and it is the line the operator reads, and unlike a probe
    it also proves the child got far enough into startup to have a log.
    """
    needle = f"HSCC API listening on 127.0.0.1:{port}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = sandbox["log"].read_text()
        except FileNotFoundError:
            text = ""
        if needle in text:
            return
        time.sleep(0.05)
    raise AssertionError(
        f"the child never reached {needle!r} within {timeout}s.\n"
        f"{_describe(sandbox)}")


def _wait_serving_child(sandbox, port, not_pid, timeout=CHILD_READY_BUDGET):
    """:func:`_wait_listening` plus the promise `start` makes on top of it.

    `hscc api start` is a double fork, so the pid the caller must signal is not
    the pid of the CLI process it spawned: the CLI parent writes its OWN pid
    into the pid file and the server grandchild overwrites it a moment later.
    The pid to return therefore has to be alive AND not the harness's, or the
    caller would SIGTERM a process that has already exited and test nothing.
    """
    _wait_listening(sandbox, port, timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid = _read_int(sandbox["pid_file"])
        if pid and pid != not_pid and _alive(pid):
            return pid
        time.sleep(0.05)
    raise AssertionError(
        f"`hscc api start` served but never left a live pid of its own in "
        f"{sandbox['pid_file'].name} within {timeout}s (the caller would have "
        f"had nothing to signal).\n{_describe(sandbox)}")


# --------------------------------------------------------------------------- #
# the user-reachable path: `hscc api start`, then `hscc api stop`
# --------------------------------------------------------------------------- #

def test_api_stop_tears_the_sidecar_down(sandbox):
    """The defect, from the verb an operator actually types.

    `hscc api stop` SIGTERMs the pid-file instance. Before the fix the handler
    answered with `os._exit(0)`, which skips `_serve`'s `finally:` — so the
    sidecar this process started stayed alive holding the token, and the marker
    showed a sidecar that was started and never shut down.

    Discriminators, all three measured on the child rather than asserted about
    it: the sidecar pid is GONE, the fake bridge recorded the shutdown, and the
    stop reported a clean stop rather than having to escalate to SIGKILL.
    """
    port = _free_port()
    started = _start_api(sandbox, port)
    assert started.returncode == 0, (
        f"`hscc api start` failed: {started.stdout}\n{started.stderr}")
    pid = _wait_serving_child(sandbox, port, not_pid=started.pid)

    stopped = _run(sandbox, "stop")
    try:
        assert stopped.returncode == 0, (
            f"`hscc api stop` failed: {stopped.stdout}\n{stopped.stderr}\n"
            f"{_describe(sandbox)}")
        out = stopped.stdout + stopped.stderr
        assert "force-killed" not in out, (
            f"the stop had to escalate to SIGKILL, which cannot be caught and "
            f"would orphan the sidecar again: {out}\n{_describe(sandbox)}")
        assert "live supervisor" not in out, (
            f"the sandboxed stop reached for the operator's launchd: {out}")

        assert _have(sandbox, SIDECAR_STARTED), (
            f"the sidecar never reported starting, so the case proved nothing "
            f"about tearing it down:\n{_describe(sandbox)}")
        assert _have(sandbox, SIDECAR_SHUTDOWN), (
            f"`hscc api stop` did NOT tear the gateway sidecar down — the stop "
            f"path exited past _serve's finally:, which is the orphaned-sidecar "
            f"defect this file exists for.\n{_describe(sandbox)}")
        assert _have(sandbox, SIDECAR_STOPPED), (
            f"the shutdown started but the sidecar was not killed: "
            f"{_describe(sandbox)}")

        sidecar_pid = _read_int(sandbox["sidecar_pid"])
        assert sidecar_pid is not None, _describe(sandbox)
        deadline = time.time() + 10
        while _alive(sidecar_pid) and time.time() < deadline:
            time.sleep(0.05)
        assert not _alive(sidecar_pid), (
            f"the sidecar (pid {sidecar_pid}) outlived `hscc api stop` — it is "
            f"orphaned, still holding this API's token.\n{_describe(sandbox)}")
        assert not sandbox["pid_file"].exists(), (
            f"the pid file survived the stop: {_describe(sandbox)}")
        # The line `_serve`'s finally: writes on the way out — the same closing
        # line `serve_main` prints (t_6eff6258), and the one thing in the log
        # that says the SERVER finished rather than that a caller signalled it.
        assert "HSCC API stopped" in sandbox["log"].read_text(), (
            f"`_serve` never reported its teardown, so the stop left no trace "
            f"of the server having finished: {_describe(sandbox)}")
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)


# --------------------------------------------------------------------------- #
# the same exit, with an observable exit code
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_the_serving_loop_returns_instead_of_being_cut_short(sandbox, sig):
    """A signalled `_serve` must RETURN, so its `finally:` gets to run.

    `os._exit(0)` exits 0 too, so the return code alone is not the point: this
    asserts the run came back through `_serve` (the harness's own line after
    the call) AND that the sidecar died. Before the fix the child exited 0 with
    the sidecar alive and no `_serve`-returned line — three witnesses for one
    mechanism.

    Both signals the handler installs are covered, and they are not equally
    sharp: measured at the base of this card, the SIGINT arm ALREADY ended
    cleanly without a handler, because an unhandled SIGINT raises
    KeyboardInterrupt and `_MultiServer.serve_forever` swallows it — so the SIGTERM
    arm is the one that separates the two behaviours (measured: rc -15 by the
    default action at base with no handler, rc 0 with the cooperative one). The
    SIGINT arm stays because it guards the other failure this fix could have
    introduced: a handler that blocks instead of setting the flag hangs the same
    signal an operator in the foreground sends.
    """
    port = _free_port()
    proc = _popen(sandbox, "serve", str(port))
    trail = _ChildTrail(proc)
    try:
        # No pid file here: this arm runs `_serve` in the foreground exactly as
        # `python -m api_server serve` does, and that entry path deliberately
        # writes none. The signal goes to the pid WE spawned, so there is no
        # double-fork to resolve and no pid file to wait for.
        _wait_listening(sandbox, port)
        assert _have(sandbox, SIDECAR_STARTED), (
            f"the sidecar never started, so nothing below proves a teardown: "
            f"{_describe(sandbox)}")
        sidecar_pid = _read_int(sandbox["sidecar_pid"])
        # From here the silence IS the measurement. A serving child has nothing
        # to say, so the stall clock is restarted at the moment we ask it to stop
        # (see :meth:`_ChildTrail.mark`) instead of running from its spawn — the
        # one window that must not be patient is the one we just opened.
        trail.mark()
        os.kill(proc.pid, sig)
        run = _await_child(proc, trail)
        out = run.output
        if run.verdict != "clean":
            pytest.fail(f"{sig.name} did not stop the serving loop: the child "
                        f"{run.verdict} ({run.detail}).\n"
                        f"child output: {out}\n{_describe(sandbox)}")
        assert proc.returncode == 0, (
            f"{sig.name} ended the process with rc={proc.returncode} instead "
            f"of a clean cooperative stop.\n{out}\n{_describe(sandbox)}")
        assert SERVE_RETURNED in out, (
            f"the process exited WITHOUT returning through `_serve` — that is "
            f"what `os._exit` does, and it skips the finally: that owns the "
            f"sidecar teardown.\nchild output: {out}\n{_describe(sandbox)}")
        assert _have(sandbox, SIDECAR_SHUTDOWN, timeout=5), (
            f"{sig.name} never reached the sidecar teardown: "
            f"{_describe(sandbox)}")
        assert sidecar_pid is not None and not _alive(sidecar_pid), (
            f"the sidecar (pid {sidecar_pid}) survived {sig.name}: "
            f"{_describe(sandbox)}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_landing_inside_a_log_write_still_stops(sandbox, sig):
    """A stop arriving mid-log-write must still tear the sidecar down.

    The stop path is a signal handler, and a handler runs on the thread it
    interrupted. `daemon_ops.log` holds `daemon_ops._handlers_lock` — a plain,
    NON-reentrant `threading.Lock` — while `_get_logger` resolves the rotating
    handler, so a handler that logs (as this file's did) waits on a lock only its
    own thread can release. Measured at the base of this card, both signals hang
    the child there forever: the flag is never set, `_serve` never returns, and
    the SIGKILL that `hscc api stop` escalates to then skips the teardown the
    card exists to guarantee.

    This is the case the leg run wrote itself: `..._being_cut_short`'s SIGINT arm
    failed once in ~4800 items with exactly that signature — the child's log
    ending at its startup lines, no `Received signal` line, the sidecar alive. The
    window is milliseconds wide (a few log writes during startup) and the
    scheduler decides, so the harness takes the lock on purpose instead of waiting
    for the lottery.

    What stays production-shaped afterwards: the binds happen, the fake sidecar
    really starts, the stop flag the handler set is what keeps the accept loops
    from starting, and the `finally:` still has a real child to kill.

    How it waits, and why not on a clock: the signal is raised by the CHILD, so
    there is no parent-side moment to aim at — all the parent can observe is what
    the child says. The child therefore announces the window it is standing in
    (`@@INSIDE-LOG-WRITE@@`, lock held and all) and the parent waits on SILENCE
    rather than on elapsed life (:func:`_await_child`). A runner slow enough to
    spend this case's whole old 25 s budget inside `import` reddened it on CI
    (run 45081900690, both arms, empty stdout and no `api.log`) with the child not
    yet entered into the handler at all — and the child had no way to say so.
    """
    port = _free_port()
    proc = _popen(sandbox, "serve-lograce", str(port), sig.name)
    trail = _ChildTrail(proc)
    try:
        run = _await_child(proc, trail)
        out = run.output
        if run.verdict == "wedged":
            # The child's own watchdog thread — the only witness that can still
            # run while the main thread is blocked in `Lock.acquire()` — had said
            # the handler was still inside the window, and the child then said
            # nothing at all for the whole stall budget. Defect named by the
            # process that has it, and confirmed by the silence after it.
            pytest.fail(
                f"{sig.name} arriving inside a log write never stopped the "
                f"process — the handler is blocked on a lock its own thread holds "
                f"(`daemon_ops._handlers_lock`), so the stop flag was never set "
                f"and the sidecar teardown was never reached ({run.detail})."
                f"\nchild output: {out}\n{_describe(sandbox)}")
        if run.verdict != "clean":
            # No verdict from the child, so no claim about its mechanism. The old
            # case asserted the deadlock here on nothing but a stopwatch and it
            # was wrong: on CI the child had not reached the window at all.
            pytest.fail(
                f"{sig.name} was raised inside a log write and the child never "
                f"came back — it {run.verdict} ({run.detail}). Nothing above it "
                f"was reached, so this says nothing about the handler."
                f"\nchild output: {out}\n{_describe(sandbox)}")
        assert INTERRUPTED_INSIDE_LOG in out, (
            "the signal never landed inside a log write, so this case says "
            f"nothing about that window.\nchild output: {out}\n"
            f"{_describe(sandbox)}")
        assert proc.returncode == 0, (
            f"{sig.name} landed inside a log write and the process left by a "
            f"door other than `_serve`: rc={proc.returncode}.\n{out}\n"
            f"{_describe(sandbox)}")
        assert SERVE_RETURNED in out, (
            f"the run ended without returning through `_serve`, so the "
            f"finally: that owns the sidecar was skipped.\n{out}\n"
            f"{_describe(sandbox)}")
        assert _have(sandbox, SIDECAR_STARTED, timeout=5), (
            "the sidecar never started, so a teardown here would prove nothing "
            f"about the stop path: {_describe(sandbox)}")
        sidecar_pid = _read_int(sandbox["sidecar_pid"])
        assert _have(sandbox, SIDECAR_SHUTDOWN, timeout=5), (
            f"{sig.name} arrived inside a log write and the sidecar teardown was "
            f"never reached: {_describe(sandbox)}")
        assert sidecar_pid is not None and not _alive(sidecar_pid), (
            f"the sidecar (pid {sidecar_pid}) survived a {sig.name} that arrived "
            f"inside a log write: {_describe(sandbox)}")
        # The closing line is written by `_serve`'s finally:, on the thread that
        # was interrupted. The handler's own line is written by the helper thread
        # and is best-effort by construction — this is the one an operator has a
        # right to find, so it is the one pinned here.
        assert "HSCC API stopped" in sandbox["log"].read_text(), (
            "no closing line in the API log: the teardown did not finish, or it "
            f"moved off the guaranteed path: {_describe(sandbox)}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# --------------------------------------------------------------------------- #
# the bounded stop on THIS entry path: a listener the stop has to give up on
# --------------------------------------------------------------------------- #

#: Two loopback binds, not one. The number is the point: with a single listener,
#: "it named the wedged host" and "it named every host it has" are the same
#: sentence, and a report that blames every listener for one loop's wedge would
#: pass. `::1` is what the sibling `test_api_multibind.py` binds unconditionally.
BOTH_BINDS = ["127.0.0.1", "::1"]
WEDGED_HOST = "::1"
HEALTHY_HOST = "127.0.0.1"

#: Identifies the abandoned-listener WARN in the API log — the line the operator of
#: THIS entry path gets, since stdout here is not a channel (see the note on the
#: two cases below). One constant shared by the cases and the pins, so a reword
#: reddens the pair instead of quietly silencing a case.
WEDGED_WARN = "HSCC API: WARNING: listener loop"

_WEDGE_WAIT = re.compile(r"within \d+(?:\.\d+)?s")


def _wait_wedged(sandbox, timeout=CHILD_READY_BUDGET):
    """Block until the wedged listener has ANNOUNCED itself inside `service_actions`.

    Not a courtesy. A stop that lands before the loop reaches that seam lets the
    loop exit at its own `while not __shutdown_request` check without ever calling
    `service_actions`, `shutdown()` comes back at once, the bounded stop reports
    nothing abandoned — and the case passes on a build that still hangs. The wedge
    writes the marker from inside the call it then blocks in, so waiting on this
    file is waiting on the exact state the signal is about to arrive into.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if WEDGE_MARKER_TEXT in sandbox["wedge"].read_text():
                return
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    raise AssertionError(
        f"no listener announced that it had reached `service_actions` ({sandbox['wedge'].name} "
        f"never said {WEDGE_MARKER_TEXT!r} within {timeout}s), so the stop below "
        f"would have raced the loop into its own body rather than interrupting it.\n"
        f"{_describe(sandbox)}")


def _warn_lines(sandbox):
    """The abandoned-listener lines in the child's API log (usually zero or one)."""
    try:
        text = sandbox["log"].read_text()
    except FileNotFoundError:
        return []
    return [line for line in text.splitlines() if WEDGED_WARN in line]


def _stop_a_serving_child(sandbox, port, wedge_host):
    """Run `serve-wedged`, wait for what it promises, SIGTERM it. -> (run, proc, pid).

    The ORDER is part of the measurement: listening first (accept loops exist, the
    fake sidecar is really up), then — only for the wedge arm — the wedge's own
    entry marker, and only then the signal.
    """
    proc = _popen(sandbox, "serve-wedged", str(port), ",".join(BOTH_BINDS),
                  wedge_host)
    trail = _ChildTrail(proc)
    _wait_listening(sandbox, port)
    assert _have(sandbox, SIDECAR_STARTED), (
        f"the sidecar never started, so no stop below can prove a teardown: "
        f"{_describe(sandbox)}")
    sidecar_pid = _read_int(sandbox["sidecar_pid"])
    if wedge_host != "-":
        _wait_wedged(sandbox)
    # From here the silence is the measurement (see :meth:`_ChildTrail.mark`): a
    # serving child has nothing to say, and the wedge arm will say nothing for the
    # whole 5 s the bounded stop is willing to wait.
    trail.mark()
    os.kill(proc.pid, signal.SIGTERM)
    return _await_child(proc, trail), proc, sidecar_pid


def test_a_wedged_listener_is_abandoned_reported_and_the_stop_says_so(sandbox):
    """One listener cannot stop: the API must still tear down, and must SAY so.

    `_MultiServer.serve_forever`'s flag branch bounds the wait and names what it
    abandoned (t_60932269) — but only for a caller that reads the answer.
    `api_cli._serve` passed the flag down and dropped the verdict, so on the path
    `hscc api start` takes — the one writing `~/.hscc/api.pid` that `hscc api stop`
    SIGTERMs — a stop that had given up on a loop looked exactly like one that had
    given up nothing: no line in `~/.hscc/api.log`, and exit 0 either way.

    Four witnesses, all read off the child rather than asserted about it: the
    teardown ran (the bound bought it — that is the entire point of bounding the
    wait), the log names the wedged host, it names ONLY the wedged host, and the
    process reports 1 where the healthy arm reports 0.
    """
    port = _free_port()
    run, proc, sidecar_pid = _stop_a_serving_child(sandbox, port, WEDGED_HOST)
    try:
        if run.verdict != "clean":
            pytest.fail(
                f"a wedged listener kept the serving process alive: it {run.verdict} "
                f"({run.detail}) instead of coming back through `_serve` with the "
                f"stop bounded. That is the t_60932269 defect on this entry path: "
                f"the abandoned loop holds `shutdown()`, `serve_main`'s equivalent "
                f"hangs its `finally:`, and `hscc api stop`'s escalation to SIGKILL "
                f"skips the sidecar teardown the way the hang does.\n"
                f"child output: {run.output}\n{_describe(sandbox)}")
        assert SERVE_RETURNED + " " + SERVE_RC_PREFIX + "1" in run.output, (
            f"the run came back through `_serve` but reported "
            f"{run.output.splitlines()[-1]!r}: `_serve` did not return 1 for a stop "
            f"that abandoned a listener, so the verdict is being dropped somewhere "
            f"between `serve_forever` and the process.\n"
            f"child output: {run.output}\n{_describe(sandbox)}")
        assert proc.returncode == 1, (
            f"the process exited {proc.returncode}: `hscc api start`'s grandchild "
            f"must exit with `_serve`'s verdict, or a stop that abandoned a loop "
            f"is indistinguishable from a clean one at the exit status too.\n"
            f"child output: {run.output}\n{_describe(sandbox)}")

        warns = _warn_lines(sandbox)
        assert warns, (
            f"nothing in the API log says a listener was abandoned. The exit code "
            f"alone is no use to the operator of this path — this is the entry point "
            f"whose stdout is the CLI's own and whose pid file closes even "
            f"`daemon_ops.log`'s stderr echo — so the report has to be the log line "
            f"(t_54159e29).\nchild output: {run.output}\n{_describe(sandbox)}")
        line = warns[0]
        assert WEDGED_HOST in line, (
            f"the abandoned-listener line does not name the host that wedged: "
            f"{line!r}. An unnamed give-up is a mystery the operator cannot go "
            f"and look at.\n{_describe(sandbox)}")
        assert HEALTHY_HOST not in line, (
            f"the abandoned-listener line names the healthy listener too: {line!r}. "
            f"`_shutdown_bounded` returns only the loops still blocked, and pointing "
            f"at both sends someone to debug a listener that stopped fine.\n"
            f"{_describe(sandbox)}")
        assert _WEDGE_WAIT.search(line), (
            f"the line does not say how long it waited: {line!r}. The bound is the "
            f"promise that this is a give-up and not a hang.\n{_describe(sandbox)}")

        # The point of the bound: the teardown ran even though a loop never came
        # back. Everything below is what t_56552ecc made this file guarantee, and
        # an unbounded stop would have lost it to the SIGKILL escalation instead.
        assert _have(sandbox, SIDECAR_SHUTDOWN, timeout=10), (
            f"the stop abandoned a listener and then never tore the sidecar down — "
            f"the abandoned loop held `shutdown()`, so the bound is not being kept "
            f"on this path: {_describe(sandbox)}")
        assert sidecar_pid is not None and not _alive(sidecar_pid), (
            f"the sidecar (pid {sidecar_pid}) outlived a stop that abandoned a "
            f"listener: {_describe(sandbox)}")
        assert "HSCC API stopped" in sandbox["log"].read_text(), (
            f"the closing line is missing, so the `finally:` did not finish: "
            f"{_describe(sandbox)}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_a_stop_that_lost_nothing_reports_nothing(sandbox):
    """The control for the case above, identical but for the wedge.

    Without it, "reports an abandoned listener" is satisfied by a build that
    reports an abandoned listener ALWAYS: the WARN and the 1 would then be noise on
    every ordinary `hscc api stop`, and the loud line would stop being a signal.
    Same binds, same harness arm, same signal — the only difference is that no
    listener is parked inside `service_actions`, and every one of them comes back.
    """
    port = _free_port()
    run, proc, sidecar_pid = _stop_a_serving_child(sandbox, port, "-")
    try:
        assert run.verdict == "clean", (
            f"an ordinary stop did not return through `_serve`: it {run.verdict} "
            f"({run.detail}).\nchild output: {run.output}\n{_describe(sandbox)}")
        assert SERVE_RETURNED + " " + SERVE_RC_PREFIX + "0" in run.output, (
            f"a stop that abandoned nothing should report 0 and reported "
            f"{run.output.splitlines()[-1]!r} — a report that fires on every stop "
            f"is not a report.\nchild output: {run.output}\n{_describe(sandbox)}")
        assert proc.returncode == 0, (
            f"an ordinary cooperative stop exited {proc.returncode}; the non-zero "
            f"is reserved for the stop that gave something up.\n"
            f"child output: {run.output}\n{_describe(sandbox)}")
        assert not _warn_lines(sandbox), (
            f"a clean stop wrote an abandoned-listener line: {_warn_lines(sandbox)}. "
            f"An operator reading that on every `hscc api stop` learns to ignore it, "
            f"which is worse than the silence it replaced.\n{_describe(sandbox)}")
        assert _have(sandbox, SIDECAR_SHUTDOWN, timeout=10), (
            f"the healthy stop did not tear the sidecar down: {_describe(sandbox)}")
        assert sidecar_pid is not None and not _alive(sidecar_pid), (
            f"the sidecar (pid {sidecar_pid}) survived an ordinary stop: "
            f"{_describe(sandbox)}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# --------------------------------------------------------------------------- #
# the crash path: the serving loop raises instead of stopping cleanly
# --------------------------------------------------------------------------- #

#: Identifies the crash report in the child's API log. `daemon_ops.log` renders the
#: level right-aligned in five columns (`f"[{level:>5s}] {msg}"`), so the level half
#: of the line is matched by pattern rather than by a literal `[ERROR]` that never
#: occurs in the file.
CRASH_ERROR = "HSCC API crashed:"
_ERROR_LEVEL = re.compile(r"\[\s*ERROR\]")


def _crash_lines(sandbox):
    """The crash-report lines in the child's API log (usually zero or one).

    Filtered on BOTH the message and the level, because the two halves of this
    card's claim are different: the message says the crash was noticed, the level
    says an operator filtering by severity finds it. A line that lost either one
    would otherwise still satisfy a case that read only the other.
    """
    try:
        text = sandbox["log"].read_text()
    except FileNotFoundError:
        return []
    return [line for line in text.splitlines()
            if CRASH_ERROR in line and _ERROR_LEVEL.search(line)]


def _run_the_crash_arm(sandbox, port, mode):
    """Run `serve-crashed`, wait for a sidecar that really exists, then act.

    -> (run, proc, sidecar_pid)

    Both modes wait for the same two things — the child's own `listening` line, then
    a sidecar that is actually up — because a crash is only interesting if there was
    something below it to tear down. Only the ending differs: the crash arm dies of
    its own injected failure, the control has to be stopped, and signalling the crash
    arm would measure the signal instead of the crash.
    """
    proc = _popen(sandbox, "serve-crashed", str(port), ",".join(BOTH_BINDS), mode)
    trail = _ChildTrail(proc)
    _wait_listening(sandbox, port)
    assert _have(sandbox, SIDECAR_STARTED), (
        f"the sidecar never started, so nothing below can prove a teardown: "
        f"{_describe(sandbox)}")
    sidecar_pid = _read_int(sandbox["sidecar_pid"])
    # From here the silence is the measurement (:meth:`_ChildTrail.mark`): the
    # control has nothing to say until it stops, and the crash arm says its last
    # line from inside `_serve`.
    trail.mark()
    if mode == NO_CRASH:
        os.kill(proc.pid, signal.SIGTERM)
    return _await_child(proc, trail), proc, sidecar_pid


def test_a_crash_in_the_serving_loop_reports_1_and_still_tears_down(sandbox):
    """The API died rather than stopped, and the exit status has to say so.

    `_serve`'s `except Exception` arm already wrote its ERROR line, and then fell
    through to the same `finally:` a clean stop uses — so it returned
    `0 if stop_clean else 1` with `stop_clean` still True, and the serving
    grandchild of `hscc api start` exited **0** having crashed. `serve_main`
    returns 1 for the identical failure (`except Exception as exc: print(...);
    return 1`), which is the same disagreement t_54159e29 closed one path over:
    two entrypoints, one failure, two prices.

    Three witnesses, and the third is the one that constrains the fix. The run came
    back through `_serve` reporting 1 (so the verdict is carried, not hard-exited
    past), the process agrees, and the sidecar a crashed process started is really
    gone — the teardown that `finally:` owns still ran, which is the half an
    `os._exit` in that handler would have lost. The log assertions hold the report
    to its own shape: an ERROR line naming the failure, and NO abandoned-listener
    WARN, because a crash abandoned nothing — it never got as far as stopping.
    """
    port = _free_port()
    run, proc, sidecar_pid = _run_the_crash_arm(sandbox, port, CRASH_MODE)
    try:
        if run.verdict != "clean":
            pytest.fail(
                f"a crash in the serving loop did not return through `_serve`: it "
                f"{run.verdict} ({run.detail}) instead of exiting on its own. The "
                f"crash arm must fall through to the `finally:` that owns the "
                f"teardown, not escape it or be cut short.\n"
                f"child output: {run.output}\n{_describe(sandbox)}")
        assert SERVE_RETURNED + " " + SERVE_RC_PREFIX + "1" in run.output, (
            f"the run came back through `_serve` but reported "
            f"{run.output.splitlines()[-1]!r}: a crashed API still returns the clean "
            f"verdict, so `hscc api start`'s grandchild exits 0 on a death — the "
            f"divergence from `serve_main`'s `return 1` this card is about.\n"
            f"child output: {run.output}\n{_describe(sandbox)}")
        assert proc.returncode == 1, (
            f"the process exited {proc.returncode} after a crash: `_handle_start` "
            f"exits with `_serve`'s verdict, so a 0 here means the verdict was "
            f"computed clean rather than that the handoff was dropped (the harness "
            f"line above says which).\nchild output: {run.output}\n"
            f"{_describe(sandbox)}")

        crash = _crash_lines(sandbox)
        assert crash, (
            f"the API log has no ERROR-level {CRASH_ERROR!r} line. The exit code is "
            f"the machine's witness; this is the operator's, and it is the only "
            f"channel this entry path has (stdout here is the CLI's own, and the pid "
            f"file closes even `daemon_ops.log`'s stderr echo).\n"
            f"{_describe(sandbox)}")
        assert CRASH_TEXT in crash[0], (
            f"the crash line does not name the failure that reached it: "
            f"{crash[0]!r}. A crash the log cannot quote is a crash nobody can "
            f"diagnose.\n{_describe(sandbox)}")
        assert not _warn_lines(sandbox), (
            f"a crash wrote the abandoned-listener WARN: {_warn_lines(sandbox)}. The "
            f"two conditions are not the same and must not share a report: that line "
            f"says a stop gave up on a listener loop that could not return, and a "
            f"crash never reached a stop. Reporting one as the other sends someone to "
            f"debug a wedged listener that was never wedged.\n{_describe(sandbox)}")

        # The point of keeping the crash inside the try instead of letting it out:
        # the teardown below it still runs. Everything from here is what
        # t_56552ecc made this file guarantee on the stop path, on the crash path.
        assert _have(sandbox, SIDECAR_SHUTDOWN, timeout=10), (
            f"the API crashed and never tore its sidecar down — the crash left the "
            f"`finally:` that owns it: {_describe(sandbox)}")
        assert sidecar_pid is not None and not _alive(sidecar_pid), (
            f"the sidecar (pid {sidecar_pid}) outlived a crashed API: it is still "
            f"alive holding this process's token, which is the orphan the teardown "
            f"exists to prevent. {_describe(sandbox)}")
        assert "HSCC API stopped" in sandbox["log"].read_text(), (
            f"the closing line is missing, so the `finally:` did not finish: "
            f"{_describe(sandbox)}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_a_serving_loop_that_returns_normally_reports_no_crash(sandbox):
    """The control for the case above: same arm, same binds, no injected raise.

    Without it, "reports a crash on 1" is satisfied by a build that reports a crash
    on every exit, and both halves of the report stop being signals: the ERROR line
    would greet an operator on every ordinary `hscc api stop`, and the 1 would stop
    separating anything. Byte-identical below the seam — the real `serve_forever`
    runs, the stop is the ordinary SIGTERM — and every one of those witnesses is
    absent.
    """
    port = _free_port()
    run, proc, sidecar_pid = _run_the_crash_arm(sandbox, port, NO_CRASH)
    try:
        assert run.verdict == "clean", (
            f"an ordinary stop on this arm did not return through `_serve`: it "
            f"{run.verdict} ({run.detail}).\nchild output: {run.output}\n"
            f"{_describe(sandbox)}")
        assert SERVE_RETURNED + " " + SERVE_RC_PREFIX + "0" in run.output, (
            f"the same arm without a crash reported "
            f"{run.output.splitlines()[-1]!r} instead of 0 — a 1 that appears whether "
            f"or not the serving loop raised is not a verdict.\n"
            f"child output: {run.output}\n{_describe(sandbox)}")
        assert proc.returncode == 0, (
            f"a healthy cooperative stop exited {proc.returncode} on this arm; the "
            f"non-zero is reserved for a stop that gave something up and for a "
            f"crash.\nchild output: {run.output}\n{_describe(sandbox)}")
        assert not _crash_lines(sandbox), (
            f"a clean stop wrote a crash line: {_crash_lines(sandbox)}. An operator "
            f"who finds `HSCC API crashed:` in the log after every `hscc api stop` "
            f"learns to ignore it, which is worse than the silence it replaced.\n"
            f"{_describe(sandbox)}")
        assert _have(sandbox, SIDECAR_SHUTDOWN, timeout=10), (
            f"the healthy stop on this arm did not tear the sidecar down: "
            f"{_describe(sandbox)}")
        assert sidecar_pid is not None and not _alive(sidecar_pid), (
            f"the sidecar (pid {sidecar_pid}) survived an ordinary stop on this arm: "
            f"{_describe(sandbox)}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


class TestTheVerdictIsCarried:
    """Source pins for the handoff the behavioural cases cannot enforce.

    The two cases above can only show the verdict being honoured on the one
    machine that ran them. What they cannot stop is the next commit quietly
    re-dropping it — `stop_clean = ...` reverting to a bare call, or
    `_handle_start` going back to `os._exit(0)` — and both of those leave today's
    log line and today's exit code in place for the healthy stop, so no
    behavioural case on a healthy stop would notice. Same doctrine as
    :class:`TestStopPathIsCooperative`.
    """

    # `TestStopPathIsCooperative` is defined further down this file, so these are
    # lazy delegations rather than class-level aliases — an alias in a class body
    # is a NameError at import. Delegation keeps one copy of each helper instead
    # of a second implementation to drift.
    @staticmethod
    def _body(func):
        return TestStopPathIsCooperative._body(func)

    @classmethod
    def _walk(cls, nodes):
        return TestStopPathIsCooperative._walk(nodes)

    @classmethod
    def _calls(cls, body, attr):
        return TestStopPathIsCooperative._calls(body, attr)

    def test_serve_keeps_the_verdict_serve_forever_returns(self):
        """The verdict must be stored, not evaluated and thrown away.

        `serve_forever(...)` as a bare statement is syntactically fine and means
        the entry path has silently un-diverged itself back into the defect this
        file's two cases above exist to catch on a machine with a wedged loop.
        """
        body = self._body(api_cli._serve)
        served = self._calls(body, "serve_forever")
        assert served, "_serve no longer calls serve_forever at all"
        stored = [n for n in ast.walk(body)
                  if isinstance(n, ast.Assign)
                  and any(n.value is call for call in served)]
        assert stored, (
            f"the result of `serve_forever` at line(s) {[n.lineno for n in served]} "
            f"is evaluated and dropped: its return value is the verdict on the stop "
            f"— False when a listener loop had to be abandoned — and not keeping it "
            f"is the divergence t_54159e29 closes. `_serve` must assign it "
            f"(`stop_clean = ...`) or return it")

    def test_a_crash_changes_what_serve_returns(self):
        """The crash arm must reach the verdict, not only the log.

        An `except Exception` that logs and falls through is syntactically legal and
        was the defect: `stop_clean` was still True when the `return` ran, so an API
        whose serving loop raised reported the same code as one that stopped with
        every listener back. Nothing in the log distinguishes them either — the ERROR
        line was already there before the fix — so the only witness is the exit
        status, and only this arm decides it.

        Pinned structurally rather than by name: whatever the handler writes has to
        be something the returned expression reads. `crashed = True` satisfies that,
        and so would `stop_clean = False` — the behavioural pair above is what says
        which of the two is honest, because the second one also turns the
        abandoned-listener WARN on for a crash that abandoned nothing.
        """
        body = self._body(api_cli._serve)
        guarded = [n for n in self._walk(body)
                   if isinstance(n, ast.Try) and n.finalbody and n.handlers]
        assert guarded, (
            "_serve no longer has a try/except with the teardown in its finally: — "
            "the shape t_56552ecc pinned, and the only place a crash can be turned "
            "into a verdict instead of an exit code nobody computed")
        returns = [n for n in self._walk(body)
                   if isinstance(n, ast.Return) and n.value is not None]
        assert returns, (
            "_serve no longer returns a verdict, so `_handle_start`'s "
            "`os._exit(_serve(...))` has nothing to exit with")
        read = {node.id for r in returns for node in self._walk(r.value)
                if isinstance(node, ast.Name)}
        written = set()
        for handler in (h for t in guarded for h in t.handlers):
            for node in self._walk(handler):
                if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                    targets = (node.targets if isinstance(node, ast.Assign)
                               else [node.target])
                    for target in targets:
                        for name in self._walk(target):
                            if isinstance(name, ast.Name):
                                written.add(name.id)
        assert written & read, (
            f"the except arm(s) at line(s) "
            f"{[h.lineno for t in guarded for h in t.handlers]} write "
            f"{sorted(written) or 'nothing'}, while the return at line(s) "
            f"{[r.lineno for r in returns]} reads {sorted(read)}: the crash is logged "
            f"and then falls through to the clean verdict — `serve_main` returns 1 "
            f"for the same exception, and that disagreement is this card")

    def test_the_start_path_exits_with_serves_verdict(self):
        """`os._exit(0)` after `_serve` reports every stop as clean.

        The grandchild of `hscc api start` is the process `hscc api stop`
        SIGTERMs; its exit status is the only thing that ever says what the stop
        cost, so it has to be `_serve`'s verdict rather than a literal.
        """
        body = self._body(api_cli._handle_start)
        served = self._calls(body, "_serve")
        assert served, "_handle_start no longer calls _serve"
        serve_line = min(n.lineno for n in served)
        carried = [n for n in self._walk(body)
                   if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "_exit"
                   and any(isinstance(a, ast.Call)
                           and isinstance(a.func, ast.Name)
                           and a.func.id == "_serve" for a in n.args)]
        assert carried, (
            f"nothing after the `_serve` call at line {serve_line} exits with "
            f"`_serve`'s return value: the serving process hard-exits with a fixed "
            f"code again, so a stop that abandoned a listener reports 0 like one "
            f"that did not")
        hard_zero = [n for n in self._walk(body)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "_exit"
                     and n.lineno >= serve_line
                     and len(n.args) == 1
                     and isinstance(n.args[0], ast.Constant)
                     and n.args[0].value == 0]
        assert not hard_zero, (
            f"`os._exit(0)` is back at line(s) {[n.lineno for n in hard_zero]} "
            f"after the serve call — that is the fixed-code exit again")

    def test_the_report_is_logged_and_never_printed(self):
        """The abandoned-listener report goes to the API log at WARN, not stdout.

        This entry path's stdout is the CLI's own (t_22f4a99f's DEVNULL redirect is
        unmerged, so the grandchild still holds the caller's descriptors), and
        `daemon_ops.log` echoes to stderr only while the pid file is ABSENT — which
        it is not here. A `print` would therefore reach nobody, and one that raised
        (`BrokenPipeError` on a pipe the caller closed) would abort the `finally:`
        that owns the gateway sidecar teardown. So the channel is pinned, not
        preferred.
        """
        body = self._body(api_cli._serve)
        printed = [n for n in self._walk(body)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "print"]
        assert not printed, (
            f"`print` is back in `_serve` at line(s) {[n.lineno for n in printed]}: "
            f"on this path stdout belongs to whoever ran `hscc api start`, and a "
            f"failed write there would abort the finally: that tears the sidecar "
            f"down")
        reports = [n for n in self._walk(body)
                   if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "log"
                   and any(isinstance(a, ast.Call)
                           and isinstance(a.func, ast.Name)
                           and a.func.id == "_abandoned_listener_report"
                           for a in n.args)]
        assert reports, (
            "the abandoned-listener report no longer goes through "
            "`daemon_ops.log`, so it is not on the API log the operator of this "
            "path reads")
        line = reports[0]
        levels = [a.value for a in line.args[1:]
                  if isinstance(a, ast.Constant) and a.value == "WARN"]
        assert levels, (
            f"the abandoned-listener line is not written at WARN "
            f"(args: {[getattr(a, 'value', type(a).__name__) for a in line.args]}): "
            f"it is a stop that happened by giving something up, and an operator "
            f"filtering the log by level has to be able to find it")
        files = {k.value.id for k in line.keywords
                 if k.arg == "log_file" and isinstance(k.value, ast.Name)}
        assert "API_LOG_FILE" in files, (
            f"the report writes {sorted(files) or 'the default log'}, not "
            f"API_LOG_FILE — the API's own log is where an operator looks for the "
            f"API's stop")


class TestTheReportLineItself:
    """The sentence, on its own — no child, no clock, nothing to race.

    Built from stand-in objects so the two claims it makes are both forced: the
    hosts it is handed, and the number it quotes. The number matters more than it
    looks: quoting the bound is what distinguishes a give-up from a hang, and if it
    were a second literal in `api_cli` the two entry paths would drift into
    reporting different seconds for the same wait.
    """

    class _Server:
        def __init__(self, wedged):
            self.wedged_listeners = wedged

    class _Api:
        _SHUTDOWN_WAIT_SECONDS = 7.25

    def test_it_names_the_hosts_it_was_handed(self):
        line = api_cli._abandoned_listener_report(self._Server(["::1"]),
                                                  self._Api())
        assert "::1" in line, line
        line2 = api_cli._abandoned_listener_report(
            self._Server(["127.0.0.1", "::1"]), self._Api())
        assert "127.0.0.1" in line2 and "::1" in line2, line2

    def test_it_quotes_the_bound_from_the_module_that_owns_it(self):
        """The seconds come from the argument, never from a literal in api_cli."""
        line = api_cli._abandoned_listener_report(self._Server(["::1"]),
                                                  self._Api())
        assert "7.25s" in line, (
            f"the line does not quote the bound it was given: {line!r} — a hard-coded "
            f"number here is how the two entry paths end up promising different waits")
        assert not re.search(r"\b5(?:\.0)?s", line), (
            f"the line carries a hard-coded 5s rather than the bound it was handed "
            f"(7.25s): {line!r}")
        # The absent-attribute arm: a caller that cannot say how long it waited
        # still has to say that it gave up, and must not print `None`.
        bare = api_cli._abandoned_listener_report(
            self._Server(["::1"]), type("_Api", (), {})())
        assert "None" not in bare and "did not stop" in bare, bare

    def test_it_survives_a_server_that_reported_nothing(self):
        """`wedged_listeners` missing must not turn the report into a crash.

        The report is built inside `_serve`'s `finally:`. An exception raised there
        does not just lose the line — it abandons the gateway sidecar teardown that
        shares the block, which is the defect t_56552ecc closed.
        """
        line = api_cli._abandoned_listener_report(object(), self._Api())
        assert "listener loop" in line and "None" not in line, line


class TestTheHarnessIsStillPython:
    """Compile the child sources here, where a mistake costs milliseconds.

    `HARNESS`, `FAKE_BRIDGE` and `DUMMY_SIDECAR` are Python source held in
    strings; nothing in this file's own import checks them, so the first thing to
    notice a typo is a child that dies at exec. What that looks like from the
    parent is not a SyntaxError but a case whose child never says anything — and
    the waits here are deliberately patient (180 s of silence before a verdict),
    so a stray newline in a template costs three minutes per case and reports a
    signal handler that never had a process to run in. Measured: one `\\n` inside
    a nested string literal reddened four cases this way on this very card.
    """

    @pytest.mark.parametrize("name,source", [
        ("HARNESS", HARNESS),
        ("FAKE_BRIDGE", FAKE_BRIDGE),
        ("DUMMY_SIDECAR", DUMMY_SIDECAR),
    ])
    def test_the_child_source_compiles(self, name, source):
        try:
            compile(source, f"<{name}>", "exec")
        except SyntaxError as exc:
            lines = source.splitlines()
            where = "\n".join(
                f"  {n}: {lines[n - 1]}"
                for n in range(max(1, (exc.lineno or 1) - 2),
                               min(len(lines), (exc.lineno or 1) + 2) + 1))
            pytest.fail(
                f"{name} is not valid Python: {exc.msg} at line {exc.lineno} of "
                f"the generated file.\n{where}\nFix it here rather than reading "
                f"it out of a child that dies before it can say anything.")

    def test_the_harness_arms_are_the_ones_the_cases_spawn(self):
        """Every verb a case asks for must be dispatched by the harness.

        An arm renamed on one side only becomes the failure above in slower form:
        the child falls through the dispatch and exits without serving, and the
        parent waits a full stall budget on a server that was never started.

        Two doors, because the harness has two: the named arms, and the tail that
        hands the verb to the product's own `cmd_api` (that is how `start` and
        `stop` are reached, and it is the point of those cases — they run the real
        dispatch). The tail is pinned to exist, or "falls through" would stop
        meaning anything.
        """
        arms = {m.group(1) for m in re.finditer(
            r'^if verb == "([\w-]+)":', HARNESS, re.MULTILINE)}
        falls_through = re.search(
            r"^raise SystemExit\(api_cli\.cmd_api\(\[verb\]", HARNESS,
            re.MULTILINE)
        assert falls_through, (
            "the harness no longer dispatches unmatched verbs through "
            "`api_cli.cmd_api`, so `start`/`stop` cases would exit without "
            "running the verb they asked for")
        # The verbs appear as literals at the call sites; the three helpers are the
        # only ways a case reaches the harness (`_start_api` wraps `_harness_argv`
        # because a forking verb must not have its stdout on a pipe).
        text = Path(__file__).read_text()
        spawned = {m.group(1) for m in re.finditer(
            r'(?:_harness_argv|_popen|_run)\(sandbox, "([\w-]+)"', text)}
        assert spawned, "no case spawns a harness arm any more — this pin is blind"
        missing = sorted(spawned - arms - {"start", "stop"})
        assert not missing, (
            f"cases spawn {missing}, which the harness neither has an arm for nor "
            f"forwards to `cmd_api`; its arms are {sorted(arms)}")
        dead = sorted(arms - spawned)
        assert not dead, (
            f"the harness still dispatches {dead} but no case spawns it: an arm "
            f"nothing exercises is a template nothing compiles-checks any more")


# --------------------------------------------------------------------------- #
# the shape, pinned at source level (the behavioural cases cannot enforce it)
# --------------------------------------------------------------------------- #

class TestStopPathIsCooperative:
    """Source pin: the stop path exits THROUGH `_serve`, not past it.

    The cases above prove today's answer and would catch a hard exit that
    happens to be reached — but the mechanism they test is a signal handler,
    and a handler is a callback the interpreter runs whenever it chooses. The
    one thing that must never come back is `os._exit` in the handler, and the
    one thing that must never move out of the `finally:` is the sidecar
    teardown. Both are structural facts about two functions, so both are
    pinned structurally (same doctrine t_6eff6258 used for the ordering there).
    """

    @staticmethod
    def _body(func):
        """AST of `func`, with linenos re-based onto the real file.

        `inspect.getsource` returns only the function's own lines, so without
        the shift every number in a failure message would be a line of the
        excerpt rather than a line of api_cli.py a reviewer can open.
        """
        source, start = inspect.getsourcelines(func)
        body = ast.parse("".join(source))
        ast.increment_lineno(body, start - 1)
        return body

    @staticmethod
    def _walk(nodes):
        """`ast.walk` over one node OR a list of nodes.

        `ast.walk` accepted an iterable only from 3.13, and the CI legs run on
        3.13 exactly — so passing `Try.finalbody` straight through is one
        interpreter bump away from a TypeError inside a pin.
        """
        if isinstance(nodes, ast.AST):
            yield from ast.walk(nodes)
            return
        for node in nodes:
            yield from ast.walk(node)

    @classmethod
    def _calls(cls, body, attr):
        """Every Call whose callee ends in `attr` (bare name or attribute)."""
        found = []
        for node in cls._walk(body):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == attr:
                found.append(node)
            elif isinstance(func, ast.Attribute) and func.attr == attr:
                found.append(node)
        return found

    @staticmethod
    def _is_stop_request_check(test):
        """True for exactly `_stop_requested.is_set()`, not for something wrapping it.

        `if False and _stop_requested.is_set()` mentions the same call and means
        the opposite — a pin that matched the BoolOp would bless the mutant the
        behavioural cases cannot see (t_6eff6258 learned this the same way).
        """
        return (isinstance(test, ast.Call)
                and isinstance(test.func, ast.Attribute)
                and test.func.attr == "is_set"
                and isinstance(test.func.value, ast.Name)
                and test.func.value.id == "_stop_requested")

    def test_the_handler_does_not_hard_exit(self):
        """`os._exit(0)` in `_sigterm_handler` is the defect, so it is banned.

        `os._exit` bypasses `finally:` (and atexit, and buffered output). The
        handler may set flags and ask the server to shut down; it may not leave
        the process by any door that skips `_serve`'s teardown.
        """
        body = self._body(api_cli._sigterm_handler)
        hard = [n for n in self._walk(body)
                if isinstance(n, ast.Call)
                and ((isinstance(n.func, ast.Attribute)
                      and n.func.attr in ("_exit", "abort", "kill"))
                     or (isinstance(n.func, ast.Name)
                         and n.func.id in ("_exit", "abort")))]
        assert not hard, (
            f"_sigterm_handler hard-exits at line(s) "
            f"{[n.lineno for n in hard]}: os._exit skips _serve's finally:, so "
            f"the supervised `hermes serve` is orphaned holding the API token")

    def test_the_handler_sets_the_stop_flag_the_serving_loop_honours(self):
        """Handler and loop must share ONE flag, and the loop must pass it down.

        A handler that sets a flag nobody reads is as silent as an `os._exit`.
        Pinned as a pair on purpose: the handler setting `_stop_requested`, and
        `_serve` handing that same name to `serve_forever(stop_event=...)`, so
        a stop landing while the accept loops exist ends the loops instead of
        being filed and forgotten.
        """
        handler = self._body(api_cli._sigterm_handler)
        sets = [n for n in ast.walk(handler)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "set"
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "_stop_requested"]
        assert sets, (
            "_sigterm_handler no longer sets `_stop_requested`, so the stop "
            "request has nowhere to go and the loops keep accepting")

        serve = self._body(api_cli._serve)
        served = self._calls(serve, "serve_forever")
        assert served, "_serve no longer calls serve_forever at all"
        carried = [n for n in served
                   if any(kw.arg == "stop_event" for kw in n.keywords)]
        assert carried, (
            "_serve calls serve_forever without `stop_event=`, so a stop "
            "requested after the loops exist is never acted on (the flag would "
            "be set and ignored — the orphaned-sidecar bug by another route)")
        names = {kw.value.id for n in carried for kw in n.keywords
                 if kw.arg == "stop_event" and isinstance(kw.value, ast.Name)}
        assert "_stop_requested" in names, (
            f"_serve passes {sorted(names) or 'something unnamed'} to "
            f"stop_event, not the flag the handler sets — the two halves of the "
            f"handshake would not be the same object")

    def test_the_handler_keeps_the_pid_file_bookkeeping(self):
        """The bookkeeping the handler always did stays (the card's condition).

        `hscc api stop` reads the pid file and `hscc api status` answers from it;
        making the stop cooperative must not quietly turn "stopped" into "still
        claims a server" while the teardown runs.

        It lives in `_serve`'s `finally:` — the block a cooperative stop always
        reaches — and that is where this pin looks. It does NOT live in the
        handler, and must not: the handler may not call anything at all (see
        `test_the_handler_holds_nothing_the_serving_thread_holds`), and the
        `finally:` already wrote this file before the card existed. The invariant
        the card set — a stop must still mark the pid file stopped — is unchanged;
        only the function that owns it is, and it owns it for every exit path
        rather than only the signalled one.
        """
        body = self._body(api_cli._serve)
        assert self._calls(body, "write_stopped"), (
            "nothing in the stop path marks the pid file stopped: a stop that "
            "takes a moment to tear the sidecar down would leave the pid file "
            "claiming a live server (`hscc api status` answers from that file)")

    def test_the_handler_holds_nothing_the_serving_thread_holds(self):
        """The handler body is two operations, and neither one may block.

        A signal handler runs on the thread it interrupts, so anything it does
        that takes a lock can be a lock that thread was already holding. That is
        not hypothetical in this file: the handler opened with `daemon_ops.log`,
        which holds `daemon_ops._handlers_lock` (a plain non-reentrant
        `threading.Lock`) while `_get_logger` resolves the rotating handler, and a
        stop landing there deadlocked the process — the flag was never set,
        `_serve` never returned, and the sidecar was orphaned by the escalation
        SIGKILL. The behavioural case
        `test_a_signal_landing_inside_a_log_write_still_stops` reaches that state;
        this pin is what keeps a third operation from being added back.

        So the body is allowed one call — `_stop_requested.set()` — plus the
        plain module write that records which signal arrived. Nothing else: no
        logging, no pid-file write, no `server.shutdown()`, and no
        `threading.Thread(...).start()` either (this fix used that for one commit,
        to move the log line off the handler; starting a thread takes threading's
        bookkeeping locks and then waits for the new thread to report itself
        started, so the handler is no longer bounded by the work it delegated).
        The flag is deliberately not a `threading.Event` either, whose `set()` /
        `is_set()` take a lock the serving loop holds on every pass.
        """
        body = self._body(api_cli._sigterm_handler)
        # `_body` parses the function's own source, so the module it lands in has
        # the def in it — the statements to judge are the FunctionDef's body.
        fn = body.body[0]
        assert isinstance(fn, ast.FunctionDef), (
            "the excerpt of `_sigterm_handler` no longer parses to a function, so "
            "this pin is reading something else")
        stmts = list(fn.body)
        if (stmts and isinstance(stmts[0], ast.Expr)
                and isinstance(stmts[0].value, ast.Constant)
                and isinstance(stmts[0].value.value, str)):
            stmts = stmts[1:]                     # the docstring, not a statement

        def _is_flag_set(node):
            return (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "set"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "_stop_requested")

        def _callee(calls):
            """Readable names for a failure message, without assuming the shape."""
            names = []
            for c in calls:
                func = c.func if isinstance(c, ast.Call) else None
                if isinstance(func, ast.Attribute):
                    names.append(func.attr)
                elif isinstance(func, ast.Name):
                    names.append(func.id)
                else:
                    names.append(type(func).__name__)
            return names

        calls = [n.value for n in stmts if isinstance(n, ast.Expr)]
        flag = [c for c in calls if _is_flag_set(c)]
        assert len(calls) == len(flag) == 1, (
            f"_sigterm_handler makes {len(calls)} call(s) ({_callee(calls)}); the "
            "body is allowed exactly one — `_stop_requested.set()`. A handler runs "
            "on the thread it interrupts, so any call can wait on a lock that "
            "thread already holds, and a handler that never returns loses the stop "
            "outright: no flag, no return from `_serve`, no sidecar teardown, and "
            "`hscc api stop` escalating to the SIGKILL that skips the teardown as "
            "well. Measured three ways at t_56552ecc — `daemon_ops.log` "
            "(`daemon_ops._handlers_lock`), `threading.Thread(...).start()` "
            "(threading's bookkeeping, plus waiting for the thread it just made), "
            "and `threading.Event` (below)")
        assert all(isinstance(n, (ast.Global, ast.Assign, ast.Expr))
                   for n in stmts), (
            f"_sigterm_handler contains "
            f"{[type(n).__name__ for n in stmts]}; only the record of which signal "
            "arrived and the flag write are allowed, and both are plain writes")
        stray = [t.id for n in stmts if isinstance(n, ast.Assign)
                 for t in n.targets
                 if isinstance(t, ast.Name) and t.id != "_last_signal"]
        assert not stray, (
            f"_sigterm_handler writes {stray}; the only module state a handler may "
            "touch is `_last_signal` (a plain write) and the flag — anything else "
            "is state some other thread is holding a lock over")
        assert not isinstance(api_cli._stop_requested, threading.Event), (
            "`_stop_requested` is a threading.Event again: Event.set()/is_set() "
            "take the event's internal lock, and the serving loop calls is_set() "
            "on every pass — a stop landing there would have the handler wait on "
            "a lock its own thread holds, which is the deadlock this pin exists "
            "for. `_StopFlag` is the lock-free answer")

    def test_the_serving_process_is_given_the_handler(self):
        """The process that serves must HAVE the handler in the first place.

        `_handle_start` installs the handlers in the child, before the second
        fork, so the grandchild that goes on to serve already answers the stop
        signal — the card is explicit that the ordering here is not the defect
        (unlike `serve_main`, where the registration used to sit after the
        bind). What is worth pinning is the existence and the wiring: a child
        that never registers them falls back to the kernel's default action,
        which is the same orphaned sidecar by a third route.

        This is also what makes the harness's `serve` arm honest: it registers
        the same two signals for the same callable, and this pin is what stops
        that copy drifting from production.
        """
        body = self._body(api_cli._handle_start)
        registered = self._calls(body, "signal")
        assert registered, (
            "`hscc api start`'s child registers no signal handler, so the "
            "process that ends up serving has no answer to the stop signal and "
            "the kernel's default action skips _serve's teardown")
        # The argument of `signal.signal(signal.SIGTERM, ...)` is itself an
        # Attribute: `signal.SIGTERM`, i.e. Name('signal') + attr 'SIGTERM'.
        handled = {arg.attr for node in registered for arg in node.args
                   if isinstance(arg, ast.Attribute)
                   and isinstance(arg.value, ast.Name)
                   and arg.value.id == "signal"}
        assert {"SIGTERM", "SIGINT"} <= handled, (
            f"the child handles {sorted(handled) or 'nothing'}; `hscc api stop` "
            f"sends SIGTERM and an operator in the foreground sends SIGINT, and "
            f"either unhandled signal skips the sidecar teardown")
        served = self._calls(body, "_serve")
        assert served, "_handle_start no longer calls _serve at all"
        assert (min(n.lineno for n in registered)
                < min(n.lineno for n in served)), (
            "the registration now sits after the serve call, so the process "
            "reaches the accept loop before it can answer a stop")

    def test_the_sidecar_teardown_lives_in_the_finally(self):
        """The teardown must be in the `finally:` that wraps the serving call.

        This is the invariant the whole card turns on: `os._exit` skips
        `finally:`, so the teardown is only as reliable as the block it sits
        in. Move it after the try (or into the `else:`) and it stops being
        reached by the paths that most need it — which is the defect, restated
        as a code movement.
        """
        body = self._body(api_cli._serve)
        served = self._calls(body, "serve_forever")
        assert served, "_serve no longer calls serve_forever"
        serving_line = min(n.lineno for n in served)
        holders = [n for n in self._walk(body)
                   if isinstance(n, ast.Try) and n.finalbody
                   and n.lineno <= serving_line
                   and any(isinstance(c, ast.Call)
                           and c.lineno == serving_line
                           for c in self._walk(n.body))]
        assert holders, (
            "no try/finally wraps the serving loop in _serve, so nothing is "
            "guaranteed to tear the sidecar down when the loop ends")
        # Innermost wrapper: the block the serving call actually sits in. An
        # outer finally would still be a guarantee; teardown moved to after the
        # try is not, and that move is what this pin reddens on.
        holder = max(holders, key=lambda n: n.lineno)
        final_calls = {c.func.attr for c in self._walk(holder.finalbody)
                       if isinstance(c, ast.Call)
                       and isinstance(c.func, ast.Attribute)}
        assert "shutdown" in final_calls, (
            f"the finally: at line {holder.lineno} no longer shuts the gateway "
            f"sidecar down ({sorted(final_calls)}): the supervised `hermes "
            f"serve` would outlive the API holding its token")
        assert "clear_api_state" in final_calls, (
            f"the finally: at line {holder.lineno} no longer clears the API "
            f"state, so a restart inherits this instance's view")

    def test_a_stop_requested_before_serving_never_starts_the_loops(self):
        """The early exit is a promise, pinned because it cannot be run.

        Between the handler being installed and the accept loops existing is a
        window in which a stop arrives with nothing serving yet. The answer has
        to be "then we never serve": fall straight through to the same
        teardown. No case reaches that branch on an idle machine — signalling
        that early needs the scheduler to cooperate, which is exactly why
        t_6eff6258 pinned its equivalent rather than testing it.
        """
        body = self._body(api_cli._serve)
        early = [n for n in ast.walk(body)
                 if isinstance(n, ast.If) and self._is_stop_request_check(n.test)]
        assert early, (
            "_serve no longer asks the stop flag before serving, so a stop "
            "requested during startup serves from the backlog instead of "
            "going straight to the teardown")
        served = self._calls(body, "serve_forever")
        serving_line = min(n.lineno for n in served)
        for node in early:
            assert node.lineno < serving_line, (
                f"the stop guard at :{node.lineno} sits at or after the "
                f"serving call at :{serving_line}, so it can no longer keep "
                f"the loops from starting")
