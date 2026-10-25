"""Single-instance claim for the daemon loop (t_52bd498e).

WHY THIS EXISTS. The engine-wedge double-probe incident (t_e37a74c7) traced
its root emitter to TWO full ``run_daemon_loop`` processes ticking the same
streams side by side for 2+ days: an ``hscc start`` orphan whose parent fork
found the pid file dead, and launchd's ``start-daemon`` — which later ran
``write_stopped()`` + ``save_pid()``, overwriting the pid file with its own
pid without ever reaping the pre-existing loop. Every check ran twice, every
state file had two writers, and the wedge stream emitted false ERRORs because
two processes each held their own streak memory.

THE GAP this closes. The pid file is a *shared label*, not a claim: whoever
writes it last wins the label, and the loser keeps running unnoticed. Only a
mutual-exclusion claim taken by the loop itself, by EVERY entry point, makes
"a second loop cannot run" structural.

CONVENTION. Deliberately mirrors ``autodown._acquire_lock`` /
``_lock_is_stale`` / ``_lock_holder_alive`` (O_EXCL lockfile, holder pid +
acquire time recorded, liveness-first staleness, one break-and-retry) rather
than inventing a new one — one lock convention for daemon-local state. This
is LOCAL-by-definition daemon state: no sparkrun, no fleet transport.

PID REUSE. Unlike autodown (a lock held for minutes), the daemon lock is held
for DAYS, and POSIX pids are recycled. A recorded pid that is alive again may
be a *different* process — "alive" alone would let a fresh lock be wrongly
judged held (fail-closed for the operator's daemon, the wrong direction). The
lock therefore records the holder's process start time (``ps -o lstart= -p``
— no /proc dependency, macOS+Linux) and liveness means
"same pid AND the same start time". A pid whose recorded start cannot be
verified (ps failed / process vanished) is INDETERMINATE ⇒ the age rule
decides, exactly like autodown's fallback.

FAIL-OPEN vs FAIL-CLOSED. The claim itself never dies ambiguous: if the lock
cannot be adjudicated at all (unreadable HOME, EACCES, exotic OS) the daemon
logs loudly and RUNS (a daemon that won't start is worse than a daemon that
might be a twin — the old status quo). But a genuinely live twin is rejected
loudly and deterministically — that is the case this module exists for.

Test seams: every path/interval knob is a module constant (rebindable), and
each public function takes injectable ``now`` / ``pid`` / ``start_time`` /
``lock_path``.
"""

import errno
import os
import re
import subprocess
import time

# ── Knobs (rebindable module constants — the test seams) ─────────────────

DAEMON_LOCK = os.path.expanduser("~/.hscc/daemon.lock")

# Renewal cadence for the holder. The age-based staleness fallback only
# decides for an INDETERMINATE holder (start time unverifiable — e.g. a dead
# holder whose lock body was truncated, or a lock from a host whose pid
# space we cannot read). A LIVE holder renews well inside the window, so a
# daemon running for months is never mistaken for abandoned.
LOCK_RENEW_INTERVAL = 600

# Age beyond which an INDETERMINATE (unverifiable-holder) lock is presumed
# abandoned. 2x the renewal interval + margin: a live holder renews every
# LOCK_RENEW_INTERVAL, so a lock this old had no live renewal — and any
# holder whose start time we could verify would have been adjudicated by
# liveness, not age.
LOCK_STALE_AFTER = 2 * LOCK_RENEW_INTERVAL + 120

# How long a STARTUP contender waits for a previous loop's shutdown to
# finish before judging it wedged. The exit is bounded by design (every
# thread in run_daemon_loop is a daemon thread watching stop_event; the
# launchd ExitTimeOut caps a forced stop at 10s), so a contender that waits
# this long without the lock appearing/disappearing has found a loop that is
# neither alive-by-claim nor exiting — reject it rather than race it.
CONTEND_WAIT_SECONDS = 30
CONTEND_POLL_SECONDS = 1.0

# ps timeout so a wedged ps can never hang a daemon startup (fail-open path).
_PS_TIMEOUT = 5

# Test/ops seam (same pattern as update_check.SHADOW_SCAN_MODES): when not
# None, count_loop_processes() returns this value instead of running ps. The
# suite pins it to 0 so NO test's process scan can observe the operator's
# LIVE daemon (a real second loop on the dev machine would make every
# status-render test print the twin line and break byte-comparison tests);
# tests that want a specific count set it per-test.
_SCAN_OVERRIDE = None

# Test/ops seam: constant `ps -o lstart=` answer for ANY pid (None = run ps).
# The suite pins it so claim/renew/release adjudication is deterministic and
# ps-free; pid-reuse tests rebind it per-test to simulate a recycled start.
_PS_START_OVERRIDE = None

# Test/ops seam: when not None, _pid_alive() consults this collection instead
# of os.kill(pid,0). The suite pins it to an empty set (every FOREIGN pid is
# "dead") so a planted holder pid can never probe a real process on the test
# host; tests that need a live foreign pid set it per-test. The CURRENT
# process is always alive regardless.
_FAKE_ALIVE_PIDS = None


# ── Holder identity ───────────────────────────────────────────────────────

def _process_start_time(pid):
    """Canonical start time of ``pid`` from ``ps -o lstart= -p <pid>``, or
    None when it cannot be determined.

    ``lstart`` is a per-pid constant (it never changes while the process
    lives) and its format is identical on macOS and Linux ("Thu Oct 22
    17:06:42 2026"), which makes it a stable reuse-detector. None — not a
    raise — on any failure: the caller must treat identity as indeterminate
    (age rule), never assume liveness or death.
    """
    if _PS_START_OVERRIDE is not None:
        return _PS_START_OVERRIDE
    try:
        proc = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            capture_output=True, text=True, timeout=_PS_TIMEOUT,
        )
    except Exception:
        return None
    out = (proc.stdout or "").strip()
    return out or None


def process_identity(pid=None):
    """The lock-body identity for ``pid``: "pid=<p> start=<lstart>".

    ``start=unknown`` when ps cannot answer — a lock we wrote with an
    unverifiable identity is adjudicated by the age rule, which is the same
    fail-safe autodown uses for an unreadable holder.
    """
    pid = pid if pid is not None else os.getpid()
    return f"pid={pid} start={_process_start_time(pid) or 'unknown'}"


def _parse_identity(content):
    """(pid, start_str|None) from a lock body; (None, None) if unparseable.

    ``start=`` takes the REST of the line, not one token: lstart is
    "Thu Oct 22 17:06:42 2026" — spaces are part of the value.
    """
    m = re.search(r"\bpid=(\d+)", content or "")
    if not m:
        return None, None
    sm = re.search(r"\bstart=(.+)", content or "")
    val = sm.group(1).strip() if sm else None
    return int(m.group(1)), (val or None)


def _holder_state(lock_path=None, now=None):
    """Adjudicate the EXISTING lock at ``lock_path``.

    Returns exactly one of:
      "free"        — lock absent (nobody holds it)
      "mine"        — held by THIS process (same pid + same start identity)
      "live"        — held by a provably-live OTHER process (pid alive AND
                      its start time matches the recorded one) ⇒ a real twin
      "stale"       — holder provably dead, or the recorded start no longer
                      matches (pid was recycled), or an indeterminate holder
                      whose lock is older than LOCK_STALE_AFTER ⇒ breakable
    """
    lock_path = lock_path or DAEMON_LOCK
    now = time.time() if now is None else now
    try:
        with open(lock_path) as f:
            content = f.read()
    except FileNotFoundError:
        return "free"
    except OSError:
        # Unreadable (permissions/IO): cannot adjudicate ⇒ age rule.
        return _age_state(lock_path, now)
    pid, start = _parse_identity(content)
    if pid is None:
        # No parseable holder ⇒ indeterminate, same as autodown's rule.
        return _age_state(lock_path, now)
    if pid == os.getpid():
        # A claimed match on our own pid is only "mine" when the start
        # identity agrees — if it disagrees, our pid was recycled around a
        # foreign lock and that foreign holder owns it, not us.
        return "mine" if _start_matches(pid, start) else "stale"
    if not _pid_alive(pid):
        return "stale"
    # Pid is alive — is it still the SAME process the lock was written for?
    if _start_matches(pid, start):
        return "live"
    return "stale"          # pid recycled ⇒ lock belongs to a dead holder


def _age_state(lock_path, now):
    try:
        age = now - os.path.getmtime(lock_path)
    except OSError:
        return "stale"      # vanished between read and stat ⇒ free-ish
    return "stale" if age > LOCK_STALE_AFTER else "live"


def _pid_alive(pid):
    if _FAKE_ALIVE_PIDS is not None:
        # Hermetic mode (test suite): this process is trivially alive; any
        # other pid answers from the injected set — never os.kill against a
        # pid that happens to be a REAL process on the test host.
        return pid == os.getpid() or pid in _FAKE_ALIVE_PIDS
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True         # exists, another owner — respect it
    except OSError:
        return False
    return True


def _start_matches(pid, recorded_start):
    """Does ``pid``'s CURRENT start equal the lock's recorded one?

    A lock that recorded ``start=unknown`` cannot pin identity — we fall back
    to plain liveness (same as pre-this-module) but never claim a match we
    cannot see, so ``_holder_state`` treats disagreement as stale and
    agreement as live only when ps actually agrees.
    """
    if recorded_start in (None, "unknown"):
        return True         # cannot disprove ⇒ trust the liveness verdict
    return _process_start_time(pid) == recorded_start


# ── The claim ─────────────────────────────────────────────────────────────

def _write_claim(fd):
    try:
        os.write(fd, process_identity().encode())
    except OSError:
        pass                # a lock without a readable body still excludes
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def acquire_daemon_lock(lock_path=None, now=None):
    """Attempt the exclusive claim on the daemon loop. True iff we hold it.

    Mirrors autodown._acquire_lock: O_EXCL create; on contention adjudicate
    by holder liveness (start-pinned) first, age second; break-and-retry ONCE
    a provably-abandoned lock; never break a lock whose holder we cannot
    disprove while it is alive and inside the age window.

    Raises Nothing — every failure path returns False (the caller decides how
    loud to be); True means THIS process is now the single instance.
    """
    lock_path = lock_path or DAEMON_LOCK
    now = time.time() if now is None else now
    try:
        # mode=0700: ~/.hscc is private operator state (cluster topology,
        # node addresses) — a fresh-machine first-create must not land 755
        # just because the daemon lock needed the dir first.
        os.makedirs(os.path.dirname(lock_path) or ".", mode=0o700,
                    exist_ok=True)
    except OSError:
        return False
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        state = _holder_state(lock_path, now)
        if state == "mine":
            # A previous incarnation of THIS pid claimed it — e.g. an exec()
            # restart. Re-claim (rewrite the body + mtime) and hold.
            return _reclaim(lock_path, now)
        if state == "live":
            return False
        # stale ⇒ break and retry once (autodown's rule, same race answer).
        try:
            os.unlink(lock_path)
        except OSError:
            return False
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                         0o600)
        except FileExistsError:
            return False
    except OSError:
        return False
    _write_claim(fd)
    return True


def _reclaim(lock_path, now):
    try:
        with open(lock_path, "w") as f:
            f.write(process_identity())
        os.utime(lock_path, (now, now))
        return True
    except OSError:
        return False


def renew_daemon_lock(lock_path=None, now=None):
    """Refresh the holder identity + mtime while the loop runs. Never raises.

    Only rewrites when WE are the recorded holder — a lock that was broken
    and re-taken by another process while we were wedged is not ours to
    renew, and clobbering it would recreate the very twin this module kills.
    """
    lock_path = lock_path or DAEMON_LOCK
    now = time.time() if now is None else now
    if _holder_state(lock_path, now) != "mine":
        return False
    try:
        with open(lock_path, "w") as f:
            f.write(process_identity())
        os.utime(lock_path, (now, now))
        return True
    except OSError:
        return False


def release_daemon_lock(lock_path=None):
    """Remove the lock ONLY if this process is the recorded holder. Never
    raises (same contract as autodown._release_lock, plus the ownership gate
    that lock does not need because it is short-lived)."""
    lock_path = lock_path or DAEMON_LOCK
    try:
        if _holder_state(lock_path) == "mine":
            os.unlink(lock_path)
            return True
    except OSError:
        pass
    return False


def hold_daemon_loop_lock(stop_event=None, lock_path=None,
                          renew_interval=None, contender_wait=None,
                          poll=None):
    """Claim the daemon loop, or refuse to run alongside a live twin.

    Called at ``run_daemon_loop`` entry by BOTH paths (``cmd_start``'s
    grandchild and service-supervised ``cmd_start_daemon``), so no entry
    point can bypass the claim.

    Returns ``(True, None)`` when the caller may enter the loop, or
    ``(False, reason)`` when it must exit instead (reason is operator text —
    the caller logs it at ERROR and exits nonzero).

    Contention ladder:
      1. Claim succeeds ⇒ ours.
      2. Contender sees a LIVE holder ⇒ wait up to ``contender_wait`` for the
         previous loop to finish its (bounded) shutdown, re-claiming if the
         lock frees. A holder still live after the window ⇒ loud refusal.
         Waiting is correct and cheap: the classic race is a restart where
         the old loop is exiting right now — racing it with a second claim
         would recreate the twin we are preventing.
      3. Lock abandoned (holder provably dead / pid recycled / indeterminate
         and beyond the age window) ⇒ broken and re-taken in step 1's retry.
      4. The lock can't be adjudicated at all (unreadable dir, exotic OS) ⇒
         FAIL OPEN: log-worthy but the daemon must start (the old status quo
         beat a daemon that refuses to boot over a lock file we cannot see).
    """
    lock_path = lock_path or DAEMON_LOCK
    renew_interval = (LOCK_RENEW_INTERVAL if renew_interval is None
                      else renew_interval)
    contender_wait = (CONTEND_WAIT_SECONDS if contender_wait is None
                      else contender_wait)
    poll = CONTEND_POLL_SECONDS if poll is None else poll

    if acquire_daemon_lock(lock_path):
        return True, None

    state = _holder_state(lock_path)
    if state == "live":
        # Wait out a shutdown-in-progress instead of refusing instantly.
        deadline = time.monotonic() + max(0.0, contender_wait)
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                return False, ("shutdown requested while waiting for the "
                               "previous daemon loop to exit")
            if acquire_daemon_lock(lock_path):
                return True, None
            if _holder_state(lock_path) != "live":
                # Holder vanished / went stale ⇒ one decisive retry.
                if acquire_daemon_lock(lock_path):
                    return True, None
                return False, "lock contention changed mid-wait"
            time.sleep(poll)
        holder = read_lock_holder(lock_path)
        return False, (
            "another daemon loop is ALREADY holding "
            f"{lock_path} (holder {holder or 'unknown'}) and did not exit "
            f"within {int(contender_wait)}s — refusing to run a second "
            "supervision loop (t_52bd498e twin-loop guard). Investigate the "
            "holder pid; `hscc status` reports the twin-loop condition.")
    if state == "mine":
        # Race between acquire's "mine" path and this read — we hold it.
        return True, None
    # "stale"/"free" seen between acquire() and the read: one final try.
    if acquire_daemon_lock(lock_path):
        return True, None
    return False, "daemon lock vanished/re-taken during contention"


def hold_daemon_loop_lock_fail_open(lock_path=None, stop_event=None,
                                    contender_wait=None, log_fn=None):
    """The entry-point wrapper: like hold_daemon_loop_lock but FAIL-OPEN on
    infrastructure failure of the lock itself.

    Returns (ok, reason) — ok=False ONLY when a live twin was positively
    observed (the one case where refusing is certain). A lock we cannot even
    read/write (HOME on a weird mount, EACCES, EROFS) returns ok=True with a
    reason the caller logs as a WARN: a daemon that will not start over an
    unreadable lock file is strictly worse than the pre-fix status quo where
    nothing was enforced at all.
    """
    try:
        os.makedirs(os.path.dirname(lock_path or DAEMON_LOCK) or ".",
                    mode=0o700, exist_ok=True)
    except OSError:
        return True, (f"cannot prepare daemon lock dir at {lock_path} — "
                      "single-instance enforcement disabled")
    try:
        return hold_daemon_loop_lock(stop_event=stop_event,
                                     lock_path=lock_path,
                                     contender_wait=contender_wait)
    except OSError as e:
        # EROFS / EACCES / anything exotic: refuse-to-start would be the
        # wrong failure mode, but silence would hide a REAL twin, so the
        # caller logs whatever came back at ERROR.
        return True, (f"daemon lock error ({e}) — single-instance "
                      "enforcement disabled, proceeding")


def read_lock_holder(lock_path=None):
    """Operator-readable holder string, or None. Never raises."""
    try:
        with open(lock_path or DAEMON_LOCK) as f:
            return f.read().strip() or None
    except OSError:
        return None


def count_loop_processes():
    """How many distinct processes are running a daemon-loop entry point.

    The DIRECT surface for "is there a twin right now?" — a log-line
    fingerprint is indirect (and t_e37a74c7 made the wedge stream correct
    despite twins, so log fingerprints under-report), and scanning the
    process table works on macOS and Linux with one command. Matches the two
    entry-point shapes only (``hscc start`` / ``hscc.py start-daemon``); any
    other hscc process (check/status/api/...) is deliberately NOT counted —
    ``sample`` already told us the twin runs the same loop, and counting CLI
    helpers would cry wolf.

    Self is counted when this process's OWN command line matches (true when
    called from inside a loop; false when called from `hscc status`, which
    counts exactly the other loops). No +1 convention — the count is always
    "processes whose argv IS a loop entry point", whatever process asks.

    Returns an int >= 0; 0 on any scan failure (fail-open: a broken scan
    reports "no twin" and the caller falls back to the lock).
    """
    if _SCAN_OVERRIDE is not None:
        return int(_SCAN_OVERRIDE)
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,command="],
            capture_output=True, text=True, timeout=_PS_TIMEOUT,
        )
    except Exception:
        return 0
    pids = set()
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        cmd = parts[1]
        # Never count the ps scan itself or any wrapper that merely embeds
        # the pattern (a `bash -c 'ps ... start-daemon ...'` probe from a
        # test or monitor would otherwise self-count as a twin).
        base = cmd.split(None, 1)[0]
        if os.path.basename(base) in ("ps", "grep", "sh", "bash", "zsh"):
            continue
        if _is_loop_command(cmd):
            pids.add(int(parts[0]))
    return len(pids)


def _is_loop_command(cmd):
    """Match ``hscc start`` (forked entry) and ``hscc.py start-daemon``
    (supervised entry) across BOTH argv shapes:

        /…/bin/hscc start                              (console shim)
        /…/python /…/hscc_daemon/hscc.py start-daemon  (launchd unit)

    So: SOME token must be an hscc entry script (basename exactly ``hscc`` or
    ``hscc.py``) and the LAST token must be exactly ``start`` or
    ``start-daemon``. Anchoring on the last token is what keeps ``hscc api
    start --tailscale``, ``hscc status``, ``hscc cluster monitor --live`` and
    every other CLI process OUT of the count. Caveat: while ``hscc start``'s
    parent fork is mid-double-fork (microseconds) both it and the grandchild
    match; the parent exits immediately, so a transient +1 there is noise,
    not a twin.
    """
    toks = cmd.split()
    if len(toks) < 2:
        return False
    for i, t in enumerate(toks[:-1]):
        if re.fullmatch(r"hscc(\.py)?", os.path.basename(t)):
            # The VERB is the token right after the entry script: `hscc api
            # start` (api is the verb — must NOT match) vs `hscc start`
            # (start is the verb). Requiring the verb be the last token too
            # keeps `hscc start --extra`-style argv honest (the entry points
            # take no flags; a trailing flag means it is not our entry).
            return toks[i + 1] in ("start", "start-daemon") and \
                i + 1 == len(toks) - 1
    return False


def daemon_instance_report(lock_path=None, scan=True):
    """Structured single-instance posture for `hscc status` / doctor.

      lock_present     bool
      holder           str|None — lock body ("pid=N start=...")
      holder_pid       int|None
      holder_alive     bool|None — start-pinned liveness of the holder
      loop_processes   int|None — loop-entry-point processes found by the
                                scan (the CLI caller is never among them:
                                `hscc status`'s own argv does not match)
      twin             bool|None — True when >1 loop process was scanned;
                                None when scan was off/failed (UNKNOWN —
                                callers must not read None as "no twin")
      lock_stale_claim bool    — a lock exists whose holder is provably
                                dead/recycled (a loop died without releasing;
                                harmless, the next claim breaks it)

    Never raises; a missing lock is normal (daemon stopped) and twin=False.
    """
    lock_path = lock_path or DAEMON_LOCK
    rep = {"lock_present": False, "holder": None, "holder_pid": None,
           "holder_alive": None, "loop_processes": None, "twin": None,
           "lock_stale_claim": False}
    holder = read_lock_holder(lock_path)
    if holder:
        rep["lock_present"] = True
        rep["holder"] = holder
        pid, start = _parse_identity(holder)
        rep["holder_pid"] = pid
        if pid is not None:
            alive = _pid_alive(pid) and _start_matches(pid, start)
            rep["holder_alive"] = alive
            rep["lock_stale_claim"] = not alive
    if scan:
        try:
            n = count_loop_processes()
            rep["loop_processes"] = n
            rep["twin"] = n > 1
        except Exception:
            rep["loop_processes"] = None
            rep["twin"] = None
    return rep
