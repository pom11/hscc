"""Daemon lifecycle management (PID, log, stream watcher, loop)."""

import gzip
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import datetime
from logging.handlers import RotatingFileHandler


PID_FILE = os.path.expanduser("~/.hscc/daemon.pid")
HEARTBEAT_FILE = os.path.expanduser("~/.hscc/heartbeat")
LOG_FILE = os.path.expanduser("~/.hscc/daemon.log")
STATE_DIR = os.path.expanduser("~/.hscc/state")

# The live daemon's check cadence, in seconds, per health stream. This is the
# SINGLE source of truth for how often `run_daemon_loop` re-runs each check
# (daemon_ops.py) AND for how long `hscc verify` tolerates a stream being silent
# before calling it stale (verify.py). Keeping them in lockstep is what stops a
# healthy-but-slow stream (NAS at 900s) from being flagged stale by a verify
# window shorter than its own cadence — the "cries wolf on a healthy cluster"
# bug this constant exists to prevent. NOTE: event_driven.py has its own
# PERIODIC_STREAMS with different values (e.g. nas: 30) — that module is NOT
# wired into the live daemon path (cli.py ed-* commands are placeholders), so
# it is inert; this dict is what actually runs.
PERIODIC_INTERVALS = {
    "dgx": 60, "gateway": 60, "local": 60, "heartbeat": 300,
    "nas": 900, "idle": 300, "workers": 60, "proxy": 60, "engine_wedge": 60,
    "dispatcher": 60,
    # t_576964d5: post-boot relaunch adjudication. Coarse on purpose — its
    # only real work is one sparkrun boot sweep + health probes per tick, and
    # post-crash recovery tolerates minutes, not seconds (fast per-repair
    # healing stays with engine-wedge recovery / check_workers).
    "post_boot": 180,
    # t_2b34b5c7 (orch-waker W3): the built-in orchestrator waker tick
    # (check_orch_waker -> orch_waker.waker_tick -> run_tick). 30 s matches
    # the stopgap's POLL_S and orch_waker.CONFIG_DEFAULTS["poll_s"]; the
    # loop's standard per-stream supervision (=_run_supervised_periodic)
    # gives it the same error isolation every stream gets, and verify's
    # per-stream staleness rule widens it to max(600, 2*30+90) = the 600 s
    # floor like any other stream. With orch_waker.enabled=false the tick
    # stays a no-op that STILL stamps, so the stream never goes dark and
    # staleness never false-fires on a deliberately-disabled feature.
    "orch_waker": 30,
    # GAP-B-6 (t_2039a319): the two loops the daemon runs OUTSIDE the CHECKS
    # table — pipeline_watchdog (lifecycle.py) and trigger_engine (trigger.py),
    # each with its own dedicated thread in run_daemon_loop. Their cadences used
    # to be private `stop_event.wait(30)` / `wait(15)` literals inside the loop
    # bodies, so the SOT said nothing about them even though BOTH already stamp
    # a stream state file every tick (watchdog.json / triggers.json). verify's
    # per-stream rule keys off this dict by STATE-FILE NAME, so those two
    # streams fell through to the flat `max_age_s` window and a tightened window
    # could cry wolf on a healthy daemon between ticks. They are keys here now —
    # read by the two loops (`run_watchdog_loop` / `run_trigger_loop`) instead of
    # being restated as literals.
    #
    # They are deliberately NOT in the CHECKS table: their tick bodies take
    # arguments and are already supervised by their own dedicated threads, so a
    # CHECKS row would start a SECOND thread ticking the same machinery (a real
    # behaviour change). Declaring the cadence here is what the SOT rule asks
    # for; how the tick is dispatched stays where it was.
    "watchdog": 30, "triggers": 15,
}


# ── Durable liveness heartbeat ────────────────────────────────────────────
# The daemon's ONLY durable liveness artifact besides the pid file is this
# heartbeat file. The pid file is REMOVED on clean stop (write_stopped), so an
# absent pid file is indistinguishable between "cleanly stopped" and
# "unexpectedly crashed" — precisely the gap that let `hscc status` report
# "RUNNING alive" while ~/.hscc/daemon.pid was gone. The heartbeat file is
# NEVER deleted on stop: a clean stop simply stops advancing it, so a dead
# daemon leaves a heartbeat that goes stale and is impossible to miss.
HEARTBEAT_INTERVAL = 300   # touch cadence (s). Aligned with
                           # PERIODIC_INTERVALS["heartbeat"] so the durable
                           # signal tracks the daemon's own heartbeat-stream
                           # cadence (single source of truth for "how often the
                           # daemon supervises").
# Staleness threshold: max(2x the cadence, a floor) — the daemon may miss up to
# two consecutive touches (slow/iowait ticks) before the heartbeat is judged
# stale, so we never cry wolf on a healthy-but-slow daemon. max(2*300, 120)=600s.
HEARTBEAT_STALE_AFTER = max(2 * HEARTBEAT_INTERVAL, 120)


def touch_heartbeat(heartbeat_file=None):
    """Write the current UTC ISO timestamp to the liveness heartbeat file.

    The daemon's own periodic supervision loop (``run_daemon_loop``) calls
    this on each supervision cycle so the file advances while the daemon
    lives. A clean stop never deletes it — it simply stops advancing.
    ``heartbeat_file`` defaults to HEARTBEAT_FILE. Creates the parent
    directory (~/.hscc) if needed. Never raises (a failed heartbeat write must
    never take the daemon down — same guarantee as ``log()``). Returns the
    ISO string written, or None on failure.
    """
    heartbeat_file = heartbeat_file or HEARTBEAT_FILE
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        parent = os.path.dirname(heartbeat_file) or "."
        os.makedirs(parent, exist_ok=True)
        with open(heartbeat_file, "w") as f:
            f.write(ts + "\n")
        return ts
    except Exception:
        return None


def daemon_liveness(pid_file=None, heartbeat_file=None):
    """Structured daemon liveness: pid-file pid + aliveness + heartbeat freshness.

    The durable liveness signal that the status path (next card) consumes. It
    reads BOTH the pid file and the heartbeat file — neither is trusted alone —
    and returns structured state so the caller can distinguish every case:

      state = "running-fresh"               pid alive + heartbeat present & fresh
              "running-stale-heartbeat"     pid alive + heartbeat present but stale
              "running-no-heartbeat"        pid alive but no heartbeat yet written
              "pid-gone"                    pid file absent, or pid not alive

    ``pid_file`` / ``heartbeat_file`` default to PID_FILE / HEARTBEAT_FILE.
    Returns a dict:
      pid               int|None — pid read from the pid file (None if absent/invalid)
      pid_file_present  bool     — whether the pid file existed on disk
      alive             bool     — whether that pid is a live process (os.kill(pid,0))
      last_heartbeat    str|None — last heartbeat ISO timestamp (None if absent/unparseable)
      heartbeat_present bool     — whether a heartbeat file existed and parsed
      stale             bool     — whether the heartbeat is older than HEARTBEAT_STALE_AFTER
      state             str      — one of the four labels above
    """
    pid_file = pid_file or PID_FILE
    heartbeat_file = heartbeat_file or HEARTBEAT_FILE

    # pid file → pid + aliveness
    pid = None
    pid_file_present = False
    if os.path.exists(pid_file):
        pid_file_present = True
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
        except (ValueError, OSError):
            pid = None

    alive = False
    if pid is not None:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False

    # heartbeat file → fresh/stale
    last_heartbeat = None
    heartbeat_present = False
    if os.path.exists(heartbeat_file):
        try:
            with open(heartbeat_file) as f:
                raw = f.read().strip()
            # Normalize so the stored value is always a clean ISO timestamp.
            last_heartbeat = datetime.datetime.fromisoformat(raw).isoformat()
            heartbeat_present = True
        except (ValueError, OSError):
            last_heartbeat = None
            heartbeat_present = False

    stale = False
    if last_heartbeat is not None:
        try:
            ts = datetime.datetime.fromisoformat(last_heartbeat)
            age = datetime.datetime.now(datetime.timezone.utc) - ts
            stale = age.total_seconds() > HEARTBEAT_STALE_AFTER
        except (ValueError, TypeError):
            stale = True

    if not alive:
        state = "pid-gone"
    elif heartbeat_present and not stale:
        state = "running-fresh"
    elif heartbeat_present and stale:
        state = "running-stale-heartbeat"
    else:
        state = "running-no-heartbeat"

    return {
        "pid": pid,
        "pid_file_present": pid_file_present,
        "alive": alive,
        "last_heartbeat": last_heartbeat,
        "heartbeat_present": heartbeat_present,
        "stale": stale,
        "state": state,
    }


#: How often the daemon's liveness loop may CONSIDER the update check (card
#: t_a4055572). The real "at most once/day" throttle is the cache itself
#: (``update_check.check_due`` refuses a fetch while the cached answer is
#: inside its 24h window) — that guard survives daemon restarts, which a
#: monotonic-clock counter cannot. This interval only bounds how often the
#: cache is re-consulted, and the first tick waits a full interval after
#: startup so a crash-looping daemon (restarting every minute) can never
#: turn the daily check into a per-restart one.
UPDATE_CHECK_INTERVAL = 3600

#: Grace before the FIRST update tick after the daemon starts (seconds). A
#: daemon in a launchd crash-loop restarts every few seconds; without a grace
#: period each restart would attempt a fetch the moment its (empty or
#: failed) cache is consulted. 120 s is far longer than any healthy restart
#: and far shorter than the 24h cadence being measured.
UPDATE_CHECK_STARTUP_DELAY = 120


def run_heartbeat_loop(stop_event, interval=None, update_check_fn=None,
                       update_interval=None, renew_lock_fn=None):
    """Durable-liveness heartbeat writer — the daemon's main loop body.

    Calls ``touch_heartbeat()`` once per ``interval`` (default
    HEARTBEAT_INTERVAL) until ``stop_event`` is set, sleeping 1s between
    checks. It is the body of ``run_daemon_loop``'s main ``while`` loop (the
    loop that runs the periodic supervisor), so it adds NO separate thread to
    leak: it runs in the daemon's main thread right where the old empty wait
    loop ran. On a clean stop it returns and the file simply stops advancing
    (never deleted), leaving a heartbeat that goes stale and cannot be missed.

    Each heartbeat tick also renews the daemon single-instance lock
    (t_52bd498e): the lock's age-based staleness fallback only adjudicates
    INDETERMINATE holders, and a live holder that renews every heartbeat
    (300s, well inside LOCK_STALE_AFTER) can never be judged abandoned no
    matter how long it has run — the same "a LIVE holder is NEVER broken"
    invariant autodown's lock guarantees. ``renew_lock_fn`` is injectable
    (tests); like the heartbeat touch itself a failed renewal must never take
    the loop down, so its exceptions are swallowed with a WARN.

    Extracted as a named function so the throttled-touch behaviour is directly
    testable without booting the monolithic ``run_daemon_loop``.

    The daily update notice (card t_a4055572) rides in HERE rather than as a
    new ``PERIODIC_INTERVALS`` stream, deliberately: ``PERIODIC_INTERVALS`` is
    the source of truth for *health streams* — each entry gets a ``check_*``
    function, a state file under ``~/.hscc/state`` and a row in
    ``hscc status``. An update check is none of those things (it is not
    health, and a row that says "OK: up to date" every 60 s would be noise),
    so the body's instruction "do NOT add a new interval stream" is honoured
    by borrowing this loop, which already wakes in the main thread on a slow
    cadence. The fetch itself is throttled twice: by ``update_interval``
    (default UPDATE_CHECK_INTERVAL, 24h) and by the cache's own freshness
    window, so a restarted daemon still cannot hammer the GitHub API.
    ``update_check_fn`` is injectable (tests); its exceptions are swallowed
    by ``update_check.daemon_tick`` itself, so the heartbeat can never be
    held hostage by a network failure.
    """
    interval = interval if interval is not None else HEARTBEAT_INTERVAL
    update_interval = (update_interval if update_interval is not None
                       else UPDATE_CHECK_INTERVAL)
    if update_check_fn is None:
        # The liveness loop must survive even a broken update-check module
        # (import-time failure == feature disabled, heartbeat keeps running).
        try:
            from .update_check import daemon_tick as update_check_fn
        except Exception as e:                      # pragma: no cover
            log(f"update check disabled: {e}", "WARN")
            update_check_fn = lambda: None          # noqa: E731
    if renew_lock_fn is None:
        # Same contract as the update tick: a missing/broken singleton module
        # disables renewal, it never kills the heartbeat.
        try:
            from .daemon_singleton import renew_daemon_lock as renew_lock_fn
        except Exception as e:                      # pragma: no cover
            log(f"daemon lock renewal disabled: {e}", "WARN")
            renew_lock_fn = lambda: None            # noqa: E731
    last = 0.0
    # The update tick deliberately does NOT fire on the first iteration (the
    # heartbeat does, because a starting daemon must be durably alive at
    # once). A short startup grace first: a daemon in a crash-loop —
    # restarting every few seconds under launchd — would otherwise issue one
    # GitHub request per restart whenever its cache is empty or failed, which
    # is exactly the API-hammering a "once a day" check must never become.
    # The cache guard (``check_due``) is still what enforces the 24h cadence
    # across restarts; this only bounds the attempt rate.
    next_update = time.monotonic() + UPDATE_CHECK_STARTUP_DELAY
    while not stop_event.is_set():
        now = time.monotonic()
        if now - last >= interval:
            touch_heartbeat()
            last = now
            # Renew the single-instance claim on the same tick (t_52bd498e):
            # a live holder's lock must never look abandoned. Errors are
            # swallowed here for the same reason touch_heartbeat never
            # raises — supervision continuity outranks lock bookkeeping.
            try:
                renew_lock_fn()
            except Exception as e:
                log(f"daemon lock renewal error: {e}", "WARN")
        if now >= next_update:
            next_update = now + update_interval
            # daemon_tick swallows everything; this guard is for the import
            # and for a patched-in fn that raises — the heartbeat must go on.
            try:
                update_check_fn()
            except Exception as e:
                log(f"update check tick error: {e}", "WARN")
        stop_event.wait(1)


def get_pid(pid_file=None):
    """Read PID from file, return None if not running.

    ``pid_file`` defaults to the daemon's PID_FILE. Pass an explicit path to
    reuse the same read/verify logic for a different service (e.g. the API
    server's ``~/.hscc/api.pid``) — one mechanism, not a parallel one.
    """
    pid_file = pid_file or PID_FILE
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, 0)
            return pid
        except OSError:
            return None
    except (FileNotFoundError, ValueError):
        return None


def save_pid(pid_file=None):
    """Write current PID to file."""
    with open(pid_file or PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def write_stopped(pid_file=None):
    """Remove PID file."""
    try:
        os.remove(pid_file or PID_FILE)
    except FileNotFoundError:
        pass


def get_daemon_log_tail(lines=50):
    """Read last N lines from daemon log."""
    try:
        with open(LOG_FILE) as f:
            all_lines = f.readlines()
        return all_lines[-lines:]
    except FileNotFoundError:
        return []


def stream_watcher(stream=None, interval=2):
    """Tail the state directory for updates in real-time."""
    from .state import read_all_states
    
    print(f"Watching {stream or 'all'} streams (Ctrl+C to stop)...\n")
    last_state = {}

    try:
        for fn in os.listdir(STATE_DIR):
            if fn.endswith(".json"):
                stream_name = fn[:-5]
                fp = os.path.join(STATE_DIR, fn)
                try:
                    with open(fp) as f:
                        last_state[stream_name] = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
    except FileNotFoundError:
        pass

    try:
        while True:
            stream_names = [stream] if stream and stream != "all" else list(last_state.keys())

            for sn in stream_names:
                fp = os.path.join(STATE_DIR, f"{sn}.json")
                try:
                    with open(fp) as f:
                        current = json.load(f)
                    if current != last_state.get(sn):
                        ts = current.get("timestamp", "?")[:19]
                        ok = current.get("ok", current.get("blocked", "n/a"))
                        msg = current.get("message", current.get("ok", ""))
                        print(f"\n[{ts}] {sn:12s} ok={str(ok):5s} | {msg}")
                        last_state[sn] = current
                except (FileNotFoundError, json.JSONDecodeError):
                    pass

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped watching.")


def ensure_state_dir():
    """Ensure the state directory exists."""
    os.makedirs(STATE_DIR, exist_ok=True)


HSCC_DIR = os.path.expanduser("~/.hscc")
_BAK_KEEP = 5  # newest N of each <file>.bak.* kept


def prune_dead_files(hscc_dir=None):
    """Remove dead daemon cruft on startup: .corrupt-* snapshots + .stale flags
    (both ignored by the daemon), and cap any <file>.bak.* group at _BAK_KEEP.

    Best-effort + idempotent — never raises. Returns a summary dict of counts.
    """
    import glob
    d = hscc_dir or HSCC_DIR
    removed_dead, pruned_bak = 0, 0
    # 1. dead files
    for pat in ("*.corrupt-*", "*.stale"):
        for f in glob.glob(os.path.join(d, pat)):
            try:
                os.remove(f)
                removed_dead += 1
            except OSError:
                pass
    # 2. cap each <stem>.bak.* group (e.g. serving.json.bak.*, models.json.bak.*)
    groups = {}
    for f in glob.glob(os.path.join(d, "*.bak.*")):
        stem = f.rsplit(".bak.", 1)[0]
        groups.setdefault(stem, []).append(f)
    for files in groups.values():
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for old in files[_BAK_KEEP:]:
            try:
                os.remove(old)
                pruned_bak += 1
            except OSError:
                pass
    return {"removed_dead": removed_dead, "pruned_bak": pruned_bak}


# ── Size-based log rotation ────────────────────────────────────────────────
# The daemon log (~/.hscc/daemon.log) had NO rotation and grew unbounded — 303
# MB over ~105 days on this host. Cap and retention are chosen from measured
# growth (see the task report): the daemon writes ~3.0 MB/day (recent stable
# days 3.6-4.0 MB, the 2026-09-08 outage day spiked to 5.4 MB). 25 MiB keeps
# roughly a week of active history; 3 gzipped generations extend that several
# weeks more while compressing to ~10% of their original size.
LOG_MAX_BYTES = 25 * 1024 * 1024      # rotate at ~25 MiB (≈ 8 days at today's rate)
LOG_BACKUP_COUNT = 3                  # retain 3 rolled generations

_handlers = {}        # resolved absolute log path -> (Logger, _GzipRotatingFileHandler)
_handlers_lock = threading.Lock()


class _GzipRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that gzips each rolled generation.

    The stdlib handler does everything boring and correct already: size-based
    rollover, generation retention, per-handler locking (thread-safe across the
    daemon's worker threads). The ONLY addition here is compressing each
    rotated file with gzip. ``doRollover`` calls ``rotation_filename`` to pick
    the destination name and ``rotate`` to move the file; we name the target
    ``*.log.N.gz`` and gzip into it instead of a plain rename.
    """

    def rotation_filename(self, default_name):
        return default_name + ".gz"

    def rotate(self, source, dest):
        with open(source, "rb") as f_in, \
                gzip.open(dest, "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out)
        try:
            os.remove(source)
        except OSError:
            pass


def _get_logger(log_file):
    """Return the (lazily created) rotating Logger for *absolute* log_file.

    A dedicated ``logging.Logger`` owns the handler (one per resolved path).
    RotatingFileHandler is a *Handler*, not a *Logger* — it has ``emit`` /
    ``handle`` but no ``info()``, so we route through a Logger + handler pair
    (the Python-logging idiom) to get the stdlib's thread-safe, size-based
    rollover for free. The Logger/Handler are both pinned to INFO so records
    are never filtered, and propagation is off so the daemon log line does not
    also leak to the root logger's handlers.
    """
    with _handlers_lock:
        entry = _handlers.get(log_file)
        if entry is None:
            handler = _GzipRotatingFileHandler(
                log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
            handler.setFormatter(logging.Formatter("%(message)s"))
            handler.setLevel(logging.INFO)
            logger = logging.getLogger("hscc_daemon.rotating." + log_file)
            logger.setLevel(logging.INFO)
            logger.propagate = False
            logger.addHandler(handler)
            _handlers[log_file] = (logger, handler)
            return logger
        return entry[0]


def append_rotating(log_file, line):
    """Append a fully-formatted line to ``log_file`` under size-based rotation.

    Shared by every daemon write path (daemon_ops.log, event_driven._event_log)
    so the whole log gets the same cap/retention — a rotation that only covered
    one writer would leave the other growing (the half-fixed bug this exists to
    prevent). Builds the handler against the CURRENT ``log_file`` so a test or
    another service passing a different path gets its own rotation. The line is
    always passed as the single positional message (never as a ``%``-style
    format + args), so the literal text is written verbatim. Never raises:
    RotatingFileHandler swallows IO hiccups via handleError, matching the old
    ``except IOError: pass`` guarantee.
    """
    try:
        _get_logger(os.path.abspath(log_file)).info(line)
    except Exception:
        # Absorb anything the handler could not handle (the old log() contract
        # was never to raise). The daemon must not die because of logging.
        pass


def log(msg, level="INFO", log_file=None, pid_file=None):
    """Write a timestamped log line to the daemon log file.

    ``log_file`` / ``pid_file`` default to the daemon's LOG_FILE / PID_FILE.
    Pass explicit paths to reuse the same timestamped format for a different
    service's log (~/.hscc/api.log) — one log convention, not a parallel one.

    Writes go through a stdlib RotatingFileHandler (see append_rotating) with
    gzip compression, so the log is size-capped and cannot grow unbounded.

    The foreground echo goes to **stderr**, never stdout. stdout is the machine
    channel for every HSCC CLI (`--json` is a raw `json.dumps` on stdout; the
    repo routes every diagnostic to stderr — `doctor.py`, `cluster_live.py`,
    `api_cli.py`, `autodown_cli._error`). A diagnostic echoed to stdout can
    therefore splice itself into a JSON document a caller is about to parse:
    `hscc autodown status --json` evaluates the PR/CI interlock inline, and one
    blipping `gh` probe made `_note_prci_unavailable()` prepend a WARN line to
    the JSON, so `json.loads` died with "Expecting ',' delimiter: line 1
    column 6" on any host with an intermittent GitHub API (t_18318fd9).

    Nothing loses visibility: the file write above is unconditional, and the
    launchd unit sends StandardOutPath and StandardErrorPath to the same
    `~/.hscc/daemon.log` (install.py), so the daemon's log is byte-identical
    either way. Only an interactive foreground run notices, and stderr is where
    it belongs.
    """
    log_file = log_file or LOG_FILE
    pid_file = pid_file or PID_FILE
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    line = f"[{ts}] [{level:>5s}] {msg}"
    append_rotating(log_file, line)
    # Also print if daemon is running in foreground mode — on stderr, so a
    # diagnostic can never corrupt a `--json` payload on stdout (see docstring).
    if not os.path.exists(pid_file):
        print(line, file=sys.stderr)


def _run_periodic(stop_event, check_fn, interval, stream_name):
    """Run one check on a loop until the daemon stops (supervised below).

    Catches BaseException (not just Exception) so a KeyboardInterrupt /
    SystemExit raised *inside a child thread* — or any other abnormal
    termination — does NOT silently kill the loop: it is logged LOUDLY and the
    function RETURNS so the caller (``_run_supervised_periodic``) can see the
    thread died and restart it. The old ``except Exception`` let a
    BaseException propagate out of the thread with no one watching, which is
    precisely how a check "stopped silently". A check that BLOCKS (e.g. a
    wedged NFS handle in check_nas) never returns here — that class is handled
    by bounding every blocking probe inside the check functions themselves
    (health._run_bounded / the bounded existence checks).
    """
    while not stop_event.is_set():
        try:
            check_fn()
        except Exception as e:
            # Ordinary exception (the common transient blip) — log and keep
            # the SAME thread alive; this is not a death.
            log(f"Check {stream_name} error: {e}", "ERROR")
        except BaseException as e:  # noqa: B036 — SystemExit/KeyboardInterrupt
            # A genuine abnormal exit. Only BaseException subclasses that are
            # NOT Exception reach here (ordinary Exceptions were handled above),
            # so this is a real thread death: log it LOUDLY and return so the
            # supervisor can restart the thread.
            log(f"Check {stream_name} thread died: {type(e).__name__}: {e}",
                "ERROR")
            return
        stop_event.wait(interval)


def _run_supervised_periodic(stop_event, check_fn, interval, stream_name):
    """Keep a check thread alive: respawn it if it ever dies.

    A periodic health check must NOT be able to stop silently — a dead stream
    makes ``hscc verify`` cry wolf. If the check thread exits for any reason
    (BaseException, swallowed fatal, unexpected return) while the daemon should
    still be running, log it loudly and start a replacement thread. The
    ``stop_event`` is the only thing that ends this loop: once set, it unwinds
    and no further respawns happen. Module-level (not nested in
    ``run_daemon_loop``) so the respawn-on-death behaviour is directly
    testable; ``run_daemon_loop`` runs one of these per check stream in its own
    daemon thread.
    """
    while not stop_event.is_set():
        t = threading.Thread(
            target=_run_periodic,
            args=(stop_event, check_fn, interval, stream_name),
            daemon=True,
        )
        t.start()
        t.join()
        if stop_event.is_set():
            return
        # Fell out without the daemon stopping ⇒ the thread died. Log loudly
        # and immediately replace it (never leave the stream dark).
        log(f"Check {stream_name} thread died unexpectedly — "
            f"restarting (interval={interval}s)", "ERROR")


def run_daemon_loop():
    """Main daemon event loop (polling mode fallback)."""
    # ── SINGLE-INSTANCE CLAIM (t_52bd498e) ────────────────────────────────
    # First thing, before ANY probe, state write or thread: take the
    # ~/.hscc/daemon.lock O_EXCL claim (daemon_singleton, same convention as
    # autodown._acquire_lock). This is the enforcement point precisely
    # because BOTH entry points — cmd_start's forked grandchild AND
    # service-supervised cmd_start_daemon — land here: the pid file is only a
    # shared label (launchd's start-daemon overwrote it over a live orphan
    # loop and both ran for 2+ days), a claim cannot be clobbered silently.
    # The claim is IDEMPOTENT for the same process, so the CLI entry points
    # may (and do) claim EARLIER — before any pid-file bookkeeping — and this
    # call is what makes the guarantee unbypassable even for a future caller
    # that invokes the loop directly.
    # A contender that finds a LIVE holder exits nonzero (3) instead of
    # running alongside. Nonzero is deliberate: launchd KeepAlive
    # (SuccessfulExit=false) then RETRIES the supervised start on its own
    # throttle, so as soon as the twin releases the lock the supervised
    # daemon takes over — supervision self-heals instead of staying down on a
    # clean exit nobody supervises. Each retry logs the refusal loudly. An
    # un-adjudicable lock (unreadable dir, exotic OS) fails OPEN with a loud
    # WARN — a daemon that will not boot over a lock file is worse than the
    # pre-fix status quo — but a positively-observed twin is always refused.
    from . import daemon_singleton as _singleton
    ok, why = _singleton.hold_daemon_loop_lock_fail_open()
    if not ok:
        log(f"Daemon refused to start: {why}", "ERROR")
        raise SystemExit(3)
    if why:
        log(f"Daemon single-instance WARN: {why}", "WARN")

    # Operator state under ~/.hscc is private — it carries cluster topology,
    # node addresses and serving config. The files were being created 0644 by
    # the inherited umask. Set it HERE rather than in a cmd_* entry point:
    # `hscc start` and the service-supervised `start-daemon` are two different
    # paths, and putting it in only one left the running daemon still writing
    # 0644 (observed: autodown.json and watchdog-block.json reverted within 90s
    # of a chmod). run_daemon_loop is the single point both paths reach.
    os.umask(0o077)
    from .health import check_dgx, check_gateway, check_local, check_heartbeat, check_nas, check_idle_monitor, check_workers, check_proxy, check_engine_wedge
    from .post_boot import check_post_boot_relaunch
    from .dispatcher_wedge import check_dispatcher_wedge
    from .dispatcher_wedge import DISPATCHER_RECOVER_CHECK_INTERVAL as _DWE_INTERVAL
    from .trigger import trigger_engine
    from .lifecycle import pipeline_watchdog, restart_vllm, load_watchdog_block
    from . import recover
    from . import orch_waker
    from .state import now_iso, write_state
    from . import sparkrun_bin

    # Service-supervised PATHs (launchd/systemd) routinely omit ~/.local/bin,
    # where the sparkrun CLI actually lives (t_b543e530). argv-based calls are
    # already PATH-independent (util.run_cmd resolves sparkrun_bin.exec_argv);
    # this covers the handful of shell=True sparkrun call sites an argv rewrite
    # cannot reach. Additive + idempotent — an already-working PATH is not
    # reordered. Loud when sparkrun is nowhere to be found, so a daemon that
    # starts blind says so at boot instead of failing open for a day.
    _spark_dir = sparkrun_bin.ensure_on_path()
    if _spark_dir:
        log(f"sparkrun CLI resolved: {_spark_dir}/sparkrun")
    else:
        log("sparkrun CLI NOT RESOLVED at daemon start (no PATH entry, no "
            "candidate path) — fleet status will report DEGRADED and worker "
            "auto-heal cannot relaunch; install sparkrun or fix the service "
            "PATH", "ERROR")

    # Topology resolution is LAZY since t_8d2eb400 (a topology READ resolves,
    # not the import — so `hscc --help` and fresh-HOME/CI processes execute
    # nothing). The daemon is the one process that ALWAYS needs the topology,
    # so resolve it once HERE at startup — before the first check tick —
    # exactly as the old module-level call at serving.py import did.
    from . import serving as _serving
    _serving.resolve_cluster_config()

    ensure_state_dir()
    log("Daemon loop started")
    # Autodown daemon-start recovery (§8): if the layer was intentionally down
    # or mid-wake when the previous daemon process died, reconcile it now —
    # re-assert the intentional block (so this daemon's watchdog doesn't
    # resurrect it) or finish the wake. Fully defensive: a broken autodown can
    # never prevent the daemon from booting.
    try:
        from . import autodown
        autodown.resume_from_restart_defensive()
    except Exception as e:
        log(f"Autodown startup hook error (daemon starting anyway): {e}",
            "ERROR")
    # Self-clean dead cruft on startup: .corrupt-*/.stale + uncapped .bak.* groups.
    try:
        pruned = prune_dead_files()
        if pruned["removed_dead"] or pruned["pruned_bak"]:
            log(f"Startup cleanup: removed {pruned['removed_dead']} dead, "
                f"{pruned['pruned_bak']} excess backups")
    except Exception as e:
        log(f"Startup cleanup skipped: {e}", "WARN")

    stop_event = threading.Event()

    def stop_handler(signum, frame):
        log(f"Received signal {signum}, stopping...")
        stop_event.set()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)

    STREAMS = PERIODIC_INTERVALS

    # The check_* fns are imported as locals above (not module globals), so map
    # them explicitly — globals().get("check_<name>") would return None and no
    # check thread would ever start.
    CHECKS = {
        "dgx": check_dgx, "gateway": check_gateway, "local": check_local,
        "heartbeat": check_heartbeat, "nas": check_nas,
        "idle": check_idle_monitor, "workers": check_workers,
        "proxy": check_proxy, "engine_wedge": check_engine_wedge,
        "dispatcher": check_dispatcher_wedge,
        "post_boot": check_post_boot_relaunch,
        # orch-waker W3 (t_2b34b5c7): the stream's check body. Every stream in
        # this table gets its own supervised _run_supervised_periodic thread,
        # so the waker inherits the loop's standard error isolation — a waker
        # failure (or a thread death) is logged and respawned, never able to
        # take another stream down — and its state file feeds the same
        # per-stream staleness math as every other stream.
        "orch_waker": orch_waker.check_orch_waker,
    }

    # GAP-B-6 (t_2039a319): both cadences come from PERIODIC_INTERVALS — the
    # same dict the CHECKS threads and verify's staleness math read. The
    # previous `wait(30)` / `wait(15)` literals made these two streams
    # unrepresentable in the SOT even though each stamps its state file every
    # tick; the cadence values themselves are unchanged.
    def run_watchdog_loop():
        watchdog_interval = STREAMS["watchdog"]
        while not stop_event.is_set():
            try:
                pipeline_watchdog()
            except Exception as e:
                log(f"Watchdog error: {e}", "ERROR")
            stop_event.wait(watchdog_interval)

    def run_trigger_loop():
        trigger_interval = STREAMS["triggers"]
        while not stop_event.is_set():
            try:
                trigger_engine(
                    check_dgx_fn=check_dgx,
                    check_gateway_fn=check_gateway,
                    pipeline_watchdog_fn=pipeline_watchdog,
                    watchdog_block_fn=load_watchdog_block,
                    restart_vllm_fn=restart_vllm,
                )
            except Exception as e:
                log(f"Trigger engine error: {e}", "ERROR")
            stop_event.wait(trigger_interval)

    def run_engine_wedge_recover_loop():
        """Guarded auto-recovery for a wedged serving unit (E stream).

        Runs at the same cadence as the ``engine_wedge`` probe. Each tick
        calls ``recover.recover_engine_wedge()``, which consumes the probe's
        latest verdict and — only after N consecutive wedge detections and every
        other guard passes — restarts JUST the wedged unit via the hscc
        wrappers. A raising tick must never kill the thread: swallow it loudly
        and continue.
        """
        while not stop_event.is_set():
            try:
                recover.recover_engine_wedge()
            except Exception as e:
                log(f"Engine-wedge recovery error: {e}", "ERROR")
            stop_event.wait(recover.RECOVER_CHECK_INTERVAL)

    def run_dispatcher_wedge_recover_loop():
        """Guarded self-heal for a wedged kanban dispatcher.

        Runs at the same cadence as the ``dispatcher`` probe. Each tick calls
        ``dispatcher_wedge.recover_dispatcher_wedge()``, which consumes the
        probe's latest verdict and — only after a genuine stall is declared and
        confirmed for M consecutive detections, then every cooldown/attempt-cap
        guard passes — restarts the gateway. The restart action is injectable
        and defaults to the real ``launchctl kickstart``. A raising tick must
        never kill the thread: swallow it loudly and continue.
        """
        from . import dispatcher_wedge as _dwe
        while not stop_event.is_set():
            try:
                _dwe.recover_dispatcher_wedge()
            except Exception as e:
                log(f"Dispatcher-wedge recovery error: {e}", "ERROR")
            stop_event.wait(_dwe.DISPATCHER_RECOVER_CHECK_INTERVAL)

    threads = []
    for stream_name, interval in STREAMS.items():
        check_fn = CHECKS.get(stream_name)
        if check_fn:
            t = threading.Thread(
                target=_run_supervised_periodic,
                args=(stop_event, check_fn, interval, stream_name),
                daemon=True,
            )
            t.start()
            threads.append(t)
            log(f"Started {stream_name} check thread (interval={interval}s)")

    ad = threading.Thread(target=run_autodown_loop, args=(stop_event,), daemon=True)
    ad.start()
    threads.append(ad)
    log("Started autodown thread (interval=30s)")

    er = threading.Thread(target=run_engine_wedge_recover_loop, daemon=True)
    er.start()
    threads.append(er)
    log("Started engine-wedge recovery thread "
        "(interval=%ss)" % recover.RECOVER_CHECK_INTERVAL)

    dwr = threading.Thread(target=run_dispatcher_wedge_recover_loop, daemon=True)
    dwr.start()
    threads.append(dwr)
    log("Started dispatcher-wedge recovery thread "
        "(interval=%ss)" % _DWE_INTERVAL)

    wd = threading.Thread(target=run_watchdog_loop, daemon=True)
    wd.start()
    threads.append(wd)
    log("Started watchdog thread (interval=%ss)" % STREAMS["watchdog"])

    te = threading.Thread(target=run_trigger_loop, daemon=True)
    te.start()
    threads.append(te)
    log("Started trigger engine thread (interval=%ss)" % STREAMS["triggers"])

    log("All threads started, daemon loop running (polling mode)")

    # Durable liveness heartbeat: run_heartbeat_loop is the main-loop body
    # here — it touches ~/.hscc/heartbeat on each supervision cycle (throttled
    # to HEARTBEAT_INTERVAL) in the SHARED main thread both entry points
    # (cmd_start + cmd_start_daemon) reach, so the heartbeat is written exactly
    # once per cadence by whichever main loop runs the periodic supervisor,
    # with NO new thread to leak. On a clean stop the loop simply stops
    # advancing it (never deleted): a dead daemon leaves a heartbeat that goes
    # stale and cannot be missed.
    run_heartbeat_loop(stop_event)

    log("Daemon loop stopped")
    write_stopped()
    # Release the single-instance claim (t_52bd498e) — only if WE still hold
    # it (release_daemon_lock never unlinks another holder's lock). A hard
    # kill (SIGKILL) skips this; the next claimant then adjudicates the
    # holder DEAD (pid gone / start-time mismatch) and breaks the stale lock,
    # so a leaked lock can never wedge supervision (autodown's invariant,
    # same mechanism).
    try:
        from .daemon_singleton import release_daemon_lock
        release_daemon_lock()
    except Exception as e:                          # pragma: no cover
        log(f"daemon lock release error (next claim adjudicates): {e}",
            "WARN")


def run_autodown_loop(stop_event, interval=30):
    """Idle autodown/autoup daemon thread (Phase 2).

    Runs at ``interval`` seconds (default 30, matching the watchdog cadence)
    as a sibling of the watchdog and trigger threads inside
    ``run_daemon_loop``. Exits cleanly when ``stop_event`` is set.

    All *real* logic lives in ``_autodown_tick``. A raising tick must NEVER
    kill the thread or take the daemon down — we swallow it here and just
    continue to the next wait, so a crashed autodown tick cannot disrupt the
    other loops.
    """
    while not stop_event.is_set():
        try:
            _autodown_tick()
        except Exception as e:
            # Defensive outer guard: a raising tick must never kill the loop —
            # swallow it and continue. But make it LOUD: a silent pass hides the
            # failure (e.g. load_config blowing up). (Phase 3 fix.)
            log(f"Autodown tick error: {e}", "ERROR")
        stop_event.wait(interval)


def _autodown_tick():
    """One autodown cycle invocation — short-circuits when disabled.

    Each tick reloads the config from disk (so a mid-run ``hscc autodown
    enable/disable`` takes effect on the next tick without a daemon restart).
    When ``enabled`` is false (C5: OFF by default) we return immediately and
    never touch the serving layer. When enabled, we call ``autodown.cycle()``
    lazily — ``cycle`` is Phase 3 and does not exist yet, so a missing cycle is
    a no-op, and a raising cycle is caught, logged, and the thread lives on.

    GAP-F-6 (audit F): every path — disabled short-circuit, normal cycle, and
    raising cycle — persists the interlock snapshot (``finally``) before
    returning, so ``hscc verify`` / ``hscc doctor`` in their OWN processes can
    see whether the interlocks are clear, holding on real work, or UNEVALUABLE
    (the #135 fail-safe-to-up that silently keeps the cluster scaled up).
    Before this, that truth lived only in this thread's module memory and the
    only surface that showed it re-evaluated everything itself
    (``hscc autodown status``).
    """
    from . import autodown

    cfg = autodown.load_config()
    try:
        if not cfg.get("enabled"):
            # OFF by default — do nothing, never touch the serving layer.
            return

        try:
            cycle = getattr(autodown, "cycle", None)
            if cycle is not None:
                cycle()
        except Exception as e:
            log(f"Autodown cycle error: {e}", "ERROR")
    finally:
        # Best-effort diagnostics write — must never break the tick, and must
        # never be the reason the loop dies (the outer loop also guards).
        try:
            autodown.write_interlock_snapshot(cfg=cfg)
        except Exception as e:  # noqa: BLE001 - diagnostics never break safety
            log(f"Autodown interlock snapshot error: {e}", "ERROR")
