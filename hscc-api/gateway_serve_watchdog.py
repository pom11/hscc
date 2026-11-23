"""Parent-liveness watchdog for the API's supervised ``hermes serve`` sidecar.

WHY THIS FILE EXISTS (card t_ed2f1abe). ``gateway_bridge.start_serve`` spawns
``hermes serve`` with ``start_new_session=True``, so the sidecar is a session
leader of its own, and ``gateway_bridge.shutdown()`` — called from the one
``finally:`` in ``hscc_daemon.api_cli._serve`` — is the ONLY thing that ever
stops it. Anything that kills the API without running that ``finally:`` leaves
the serve alive: ``hscc api stop`` escalates to SIGKILL after its grace window,
and a crash or a teardown that blocks has the same effect. The orphan reparents
to launchd (pid 1) holding the DEAD API process's session token, which is the
orphaned-sidecar / stale-token 403 the port-occupancy heal in ``start_serve``
then has to clean up on the *next* start.

So the durability lives here, in the sidecar's own supervision, rather than in
whoever is trying to stop it. ``start_serve`` no longer spawns ``hermes serve``
directly; it spawns THIS process as its direct child, and this process spawns
the serve. That buys one thing nothing else can on macOS: a process of ours
that is watching, in the same tree, for the API to disappear.

THE SIGNAL, AND WHAT WAS MEASURED (macOS 27.2, 5 trials per arm, real SIGKILL
of a real parent, chain parent -> wrapper -> serve):

  pipe EOF (primary)      detect latency 0.6 - 1.0 ms   (kernel-side: the read
                                 returns b'' when the last write-end reference
                                 closes, i.e. when the parent's fd table is
                                 destroyed)          blind spot: none
  reparenting poll        detect latency = poll interval + up to one interval
      (fallback)          0.25 s -> 203 - 278 ms      blind spot: the interval
                          1.00 s -> 1013 - 1058 ms    (an orphan lives for that
                                                      long, undetected)

So the pipe wins and is primary: the parent holds the WRITE end of an
``os.pipe()`` and hands the READ end down to this process (``pass_fds``, which
is why the fd keeps its number across the exec). The write end is
close-on-exec, so no descendant of the API ever holds it and EOF therefore means
exactly "that API process is gone" — not "nobody in this tree wants it".

The reparenting poll stays as the FALLBACK (no usable fd, or ``select``/``read``
fails on the fd): ``os.getppid()`` is recorded at startup and compared every
``_DEFAULT_POLL_S``. Its blind spot is the poll interval, which is the number
the caller documents when it ships the fallback.

WHAT THIS DOES NOT COVER, stated rather than hidden: a SIGKILL of *this*
watchdog still orphans the serve — one level up the same hole, uncloseable
without ``PR_SET_PDEATHSIG``, which macOS does not have. Measured (card
t_b952885e: real chain, real SIGKILL of the watcher, 10/10 trials): the
serve was alive at the 4.0 s bound and at 6.0 s with the port still held,
and a cooperative ``shutdown()`` after that kill no-ops past the dead
watchdog and leaves it too (3/3) — while the next ``start_serve``'s
port-occupancy heal kills it before any supervised serve starts, so the
orphan never survives a start cycle. The decision recorded with the
measurement is "documented residual", not a second watcher: chaining this
module only moves the last-watcher hole one level down (the top watcher is
what today's single one already covers), and an API-side re-attach would
have to signal the serve's pid WITHOUT a child handle — identity-blind pid
signalling, the trap that already reddened this lane's harness twice.
Pinned in ``tests/test_gateway_serve_watchdog.py`` as one strict-xfail
acceptance case plus two green pins; a real fix reddens all three at once.

Hermetic: this process touches nothing outside the fd it reads, the child it
spawns, and the env it was handed. It writes no file, opens no socket, and does
not read the operator's home. The serve's session token passes through its env
untouched and is never logged or written by this module.
"""

from __future__ import annotations

import errno
import os
import select
import signal
import stat
import subprocess
import sys
import time

#: Env contract. The fd number is preserved across the exec by ``pass_fds``;
#: the poll/grace knobs are env-only on purpose, so ``argv`` stays EXACTLY the
#: supervised command (the ``lsof``+``ps`` cmdline guard in gateway_bridge and
#: any human reading ``ps`` must still see a plain ``hermes serve ...``).
ENV_LIVENESS_FD = "HSCC_GATEWAY_LIVENESS_FD"
ENV_POLL_S = "HSCC_GATEWAY_POLL_S"
ENV_TERM_GRACE_S = "HSCC_GATEWAY_TERM_GRACE_S"
ENV_KILL_GRACE_S = "HSCC_GATEWAY_KILL_GRACE_S"

#: Blind spot of the reparenting-poll fallback (and of the "serve exited on its
#: own" check, which shares the same tick). The pipe path detects in ~1 ms and
#: does not use this.
_DEFAULT_POLL_S = 0.25

#: How long the serve gets to answer SIGTERM before this escalates to SIGKILL,
#: and then how long it waits for that kill to land. Both MUST stay below the
#: ``_SHUTDOWN_WAIT_S`` the API uses in ``gateway_bridge.shutdown()``: if this
#: process is still waiting on the serve when the API stops waiting on THIS
#: process, the API kills the watchdog mid-teardown and the serve survives — the
#: exact orphan this module exists to prevent, rebuilt out of the fix.
_DEFAULT_TERM_GRACE_S = 2.0
_DEFAULT_KILL_GRACE_S = 1.0


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def liveness_fd() -> int:
    """The inherited read end of the parent-liveness pipe, or -1.

    Validated, not trusted: it has to be a real FIFO. A stale
    ``HSCC_GATEWAY_LIVENESS_FD`` inherited from some earlier spawn (or a number
    that collides with an unrelated fd) must send us to the reparenting-poll
    fallback, not to a blocking read on something that will never EOF.
    """
    try:
        fd = int(os.environ.get(ENV_LIVENESS_FD, ""))
    except (TypeError, ValueError):
        return -1
    if fd < 0:
        return -1
    try:
        return fd if stat.S_ISFIFO(os.fstat(fd).st_mode) else -1
    except OSError:
        return -1


def _reap(child) -> bool:
    """True while the supervised child is still running.

    ``poll()`` (waitpid), NOT ``kill(pid, 0)``: the child is OURS, so a child
    that just died from our own SIGTERM is a zombie that ``kill(pid, 0)`` still
    reports as alive — which measured as a 2 s stall on every clean stop.
    """
    return child.poll() is None


def _kill_child(child, term_grace: float, kill_grace: float) -> None:
    """TERM the child's whole process group, escalate to KILL, then reap.

    The group (not just the pid) because ``hermes serve`` is itself a parent:
    the orphan problem is only solved for the tree, not for the leader.
    """
    try:
        pgid = os.getpgid(child.pid)
    except OSError:
        pgid = child.pid
    for sig, wait in ((signal.SIGTERM, term_grace), (signal.SIGKILL, kill_grace)):
        if not _reap(child):
            return
        try:
            os.killpg(pgid, sig)
        except OSError as exc:
            if exc.errno not in (errno.ESRCH, errno.EPERM):
                raise
            break
        deadline = time.monotonic() + wait
        while _reap(child) and time.monotonic() < deadline:
            time.sleep(0.02)
    _reap(child)


def watch(child, fd: int, spawn_ppid: int, poll_s: float) -> str:
    """Block until the API parent is gone (or the child is). Returns the reason.

    One loop, one tick: ``select`` returns immediately when the pipe EOFs, so
    ``poll_s`` only sets how fast the OTHER two facts are seen — the child
    exiting on its own, and (in the fallback) the reparenting.

    The second select fd is the ``signal.set_wakeup_fd`` pipe, so a SIGTERM from
    ``gateway_bridge.shutdown()`` wakes this loop at once instead of at the next
    tick: the API is waiting on us with a 5 s budget there, and a woken-late
    watchdog is how a fix re-creates the orphan.
    """
    stopped = []

    def _on_signal(signum, _frame):
        # Async-signal-safe: record and return. Everything with a lock or a
        # syscall sequence happens back in the loop (the lesson the API's own
        # handler learned the hard way on t_56552ecc).
        stopped.append(signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError, RuntimeError):
            pass   # not on the main thread / no handler available for this one

    wake_r, wake_w = -1, -1
    try:
        wake_r, wake_w = os.pipe()
        os.set_blocking(wake_r, False)
        os.set_blocking(wake_w, False)
        signal.set_wakeup_fd(wake_w)
    except (OSError, ValueError):
        wake_r = wake_w = -1     # fall back to waking on the tick, as before

    try:
        while True:
            if stopped:
                return f"signal:{stopped[0]}"
            fds = [f for f in (fd, wake_r) if f >= 0]
            if fds:
                try:
                    readable, _, _ = select.select(fds, [], [], poll_s)
                except InterruptedError:
                    continue     # our own handler fired; re-check `stopped`
                except OSError as exc:
                    if fd >= 0 and exc.errno == errno.EBADF:
                        fd = -1  # liveness fd died -> reparenting poll
                        continue
                    if wake_r >= 0 and exc.errno == errno.EBADF:
                        wake_r = -1
                        continue
                    raise
                if wake_r >= 0 and wake_r in readable:
                    try:                       # drain; the flag is the truth
                        os.read(wake_r, 64)
                    except OSError:
                        pass
                    continue                   # loop top reports the signal
                if fd >= 0 and fd in readable:
                    try:
                        chunk = os.read(fd, 1)
                    except OSError as exc:
                        if exc.errno == errno.EINTR:
                            continue
                        fd = -1                # fd unusable -> reparenting poll
                        continue
                    if chunk == b"":
                        return "parent-gone:pipe-eof"
                    # Somebody wrote to the pipe. Nothing does; keep reading so
                    # a full pipe can never wedge the guarantee.
                    continue
            if fd < 0 and os.getppid() != spawn_ppid:
                return f"parent-gone:reparented-to-{os.getppid()}"
            if not _reap(child):
                return "child-exited"
    finally:
        if wake_w >= 0:
            try:
                signal.set_wakeup_fd(-1)
            except (OSError, ValueError):
                pass
        for f in (wake_r, wake_w):
            if f >= 0:
                try:
                    os.close(f)
                except OSError:
                    pass



def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--"]:        # tolerated, not required
        argv = argv[1:]
    if not argv:
        print("gateway_serve_watchdog: no command to supervise",
              file=sys.stderr)
        return 2

    fd = liveness_fd()
    poll_s = _env_float(ENV_POLL_S, _DEFAULT_POLL_S)
    term_grace = _env_float(ENV_TERM_GRACE_S, _DEFAULT_TERM_GRACE_S)
    kill_grace = _env_float(ENV_KILL_GRACE_S, _DEFAULT_KILL_GRACE_S)
    spawn_ppid = os.getppid()

    # The fd is ours alone: clear its inheritability BEFORE the spawn so the
    # supervised serve never sees it (an extra read end would not delay the EOF
    # — EOF follows the write end — but it is an fd nobody asked for).
    if fd >= 0:
        try:
            os.set_inheritable(fd, False)
        except OSError:
            pass
    env = dict(os.environ)
    env.pop(ENV_LIVENESS_FD, None)

    child = subprocess.Popen(argv, env=env,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True)
    reason = watch(child, fd, spawn_ppid, poll_s)
    _kill_child(child, term_grace, kill_grace)
    rc = child.returncode
    print(f"gateway_serve_watchdog: {reason}; serve {child.pid} gone "
          f"(rc={rc})", file=sys.stderr)
    return 0 if rc is None or rc == 0 else (abs(rc) if rc < 0 else rc)


if __name__ == "__main__":
    sys.exit(main())
