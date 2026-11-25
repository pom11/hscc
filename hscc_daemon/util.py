"""Utility functions for the HSCC daemon."""

import ipaddress
import json
import os
import signal
import subprocess
import socket
import urllib.request
import urllib.error
import urllib.parse


def unblock_stop_signals():
    """Clear an inherited BLOCK on SIGTERM/SIGINT.

    ``signal.signal()`` installs a DISPOSITION; it does not clear the calling
    thread's signal MASK, and a mask survives fork/exec (``subprocess``
    restores ignored dispositions, never the mask). A daemon started by a
    process that runs with SIGINT or SIGTERM blocked therefore installs its
    handler, is sent the signal, and never dispatches it: the stop stays
    pending and the only exit left is SIGKILL.

    ``pthread_sigmask`` is POSIX-only; where it is absent there is no
    inherited pthread mask to clear. Every step is best-effort by design — a
    process that cannot clear its mask must still start.
    """
    try:
        signal.pthread_sigmask(signal.SIG_UNBLOCK,
                               {signal.SIGTERM, signal.SIGINT})
    except (AttributeError, OSError, ValueError):
        pass


def _ready_says_go(ready):
    """Call a readiness probe, turning ANY exception into "not ready".

    A probe asks a live endpoint; an endpoint that refuses, times out or answers
    garbage is exactly the "not up yet" answer the caller must keep waiting
    through. A probe that raises must never abort a launch that is in flight.
    """
    try:
        return bool(ready())
    except Exception:
        return False


def run_cmd_until_ready(args, ready, timeout=120, poll=2.0, env=None,
                        verified_by="health probe"):
    """Run ``args`` with a BOUNDED wait that can end EARLY because the workload
    it starts became reachable.

    Why this exists (t_09708c17, the reported live resume): ``sparkrun run
    --no-follow --ensure`` is a CLIENT, and a client can wedge on a child ssh
    session long after the container it launched is up and serving — the
    operator's manual launch sat 40 minutes on a machine whose model was
    answering in 10. Treating
    "the client returned" as the success signal therefore reports a FAILURE for
    a restart that worked, and a resume that reports failure keeps the node
    parked. Here the success signal is the unit itself: as soon as ``ready()``
    says the workload answers, the client is cut loose and the result says so.

    The wait is bounded in BOTH directions — never a silent hang, never an
    unbounded kill-loop:

      * the child exits on its own  -> ``run_cmd``'s shape, ``detached`` False
        (a clean ``--ensure`` no-op still reads as a normal success);
      * ``ready()`` turns true      -> SIGTERM the CLIENT only, ``ok`` True with
        ``detached``/``verified_by`` set, so the caller can say plainly that the
        health probe is what proved the restart, not the client;
      * the deadline passes first   -> SIGTERM the client, ``ok`` False with
        ``timed_out``/``detached`` set and the same "timed out after Ns" text
        ``run_cmd`` has always returned for a timeout.

    Only the CLIENT is ever terminated: its own children (the ssh session
    driving the launch) are left alone, which is what keeps a cut-loose launch
    from losing the container it is still bringing up. stdout/stderr go to
    temporary FILES, not pipes — a client we are not draining can otherwise
    fill a 64 KB pipe and block in write() forever.
    """
    import tempfile
    import time

    run_env = None
    if env:
        run_env = os.environ.copy()
        run_env.update(env)
    deadline = time.monotonic() + timeout
    detached = timed_out = False
    code = None
    with tempfile.TemporaryFile("w+") as out_f, \
            tempfile.TemporaryFile("w+") as err_f:
        try:
            proc = subprocess.Popen(args, stdout=out_f, stderr=err_f,
                                    text=True, env=run_env)
        except Exception as e:
            return {"ok": False, "output": f"Command failed: {e}",
                    "stderr": "", "detached": False}
        while True:
            code = proc.poll()
            if code is not None:
                break
            if _ready_says_go(ready):
                detached = True
                break
            if time.monotonic() >= deadline:
                detached = timed_out = True
                break
            time.sleep(poll)
        if detached:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            code = proc.returncode
        out_f.seek(0)
        err_f.seek(0)
        stdout, stderr = out_f.read().strip(), err_f.read().strip()

    seconds = int(timeout)
    if not detached:
        if code == 0:
            return {"ok": True, "output": stdout, "stderr": stderr,
                    "detached": False}
        folded = "\n".join(p for p in (stdout, stderr) if p)
        return {"ok": False, "output": folded, "stderr": stderr,
                "detached": False}
    if timed_out:
        return {"ok": False, "output": f"Command timed out after {seconds}s",
                "stderr": stderr, "detached": True, "timed_out": True}
    note = (f"launch client did not exit within {seconds}s and was cut loose; "
            f"restart verified by the {verified_by}")
    return {"ok": True, "output": note, "stderr": stderr, "detached": True,
            "timed_out": False, "verified_by": verified_by}


def run_cmd(args, timeout=30, as_json=False, shell=False, env=None,
            ready=None, poll=2.0):
    """Run a command and return structured output.

    On a non-zero exit, ``output`` folds stdout AND stderr together so the
    error text is never lost (tools like sparkrun write their errors to stderr
    — autodown's failure reason was empty for exactly this reason); a separate
    ``stderr`` field is also kept for structured access. ``as_json`` parses
    stdout as JSON instead.

    ``env`` overlays extra variables on the CURRENT environment for this
    child only (``None`` keeps today's inherit-everything behaviour). Callers
    use it for per-invocation pins such as ``UV_PYTHON`` — never to replace
    the environment wholesale, which would drop PATH and the operator's
    variables.

    sparkrun argvs are made PATH-independent here (t_b543e530): a logical
    ``["sparkrun", ...]`` argv gets argv[0] swapped to the resolved ABSOLUTE CLI
    path just before the exec, because a launchd-supervised daemon PATH omits
    ``~/.local/bin`` and a bare ``sparkrun`` then dies with ``[Errno 2] No such
    file or directory`` — which is how the auto-heal stop/relaunch path and the
    ``sparkrun status`` fallback were dead for a day while every DGX check still
    logged ok=True. Command BUILDERS keep emitting the logical command (stable
    for tests and grep); resolution happens at this single execution point, so
    every non-shell sparkrun shell-out in the daemon package inherits the fix.
    When sparkrun cannot be resolved anywhere the argv passes through unchanged
    and the subprocess fails loudly the way it does today — never a silent
    no-op. ``shell=True`` strings are not rewritten (the argv rewrite would be
    a string-inspection game; those callers are informational paths).

    ``ready`` (t_09708c17) turns this into a BOUNDED wait that can end EARLY:
    give it a zero-arg callable that says whether the workload the command
    starts has come up, and the child is cut loose (client only) the moment it
    does, with ``ok`` true and ``detached``/``verified_by`` set — the shape a
    wedged ``sparkrun run`` client needs so a restart that WORKED is not
    reported as a failure. ``None`` (every other caller in the daemon) keeps
    this function byte-identical to what it has always been: one
    ``subprocess.run`` with a hard timeout.
    """
    if not shell:
        from . import sparkrun_bin
        args = sparkrun_bin.exec_argv(args)
    if ready is not None:
        return run_cmd_until_ready(args, ready, timeout=timeout, poll=poll,
                                   env=env)
    try:
        run_env = None
        if env:
            run_env = os.environ.copy()
            run_env.update(env)
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, shell=shell,
            env=run_env
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if as_json and stdout:
            try:
                return {"ok": True, "output": json.loads(stdout),
                        "stderr": stderr}
            except json.JSONDecodeError:
                return {"ok": False, "output": stdout, "stderr": stderr}
        if result.returncode != 0:
            # Fold stderr into output on failure so diagnostics are never lost
            # (sparkrun writes errors to stderr — the empty-reason bug). Keep a
            # separate `stderr` field too for structured access.
            folded = "\n".join(p for p in (stdout, stderr) if p)
            return {"ok": False, "output": folded, "stderr": stderr}
        return {"ok": True, "output": stdout, "stderr": stderr}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"Command timed out after {timeout}s",
                "stderr": ""}
    except Exception as e:
        return {"ok": False, "output": f"Command failed: {e}", "stderr": ""}


def ssh_cmd(host, command, timeout=20):
    """Run a command on a remote host via SSH."""
    cmd = f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout={timeout} {host} \"{command}\""
    return run_cmd(cmd, timeout=timeout + 5, shell=True)


_LAN_HINT = (
    " (if host is a LAN/private IP, macOS may be blocking this app's "
    "Local Network access: grant it in System Settings > Privacy & "
    "Security > Local Network, or verify the host is actually up)"
)


def _errno_from_urlerror(reason):
    """Extract an errno int from a URLError reason, or None."""
    if isinstance(reason, OSError):
        return reason.errno
    if isinstance(reason, Exception):
        # socket.gaierror / OSError subclasses already covered above.
        return getattr(reason, "errno", None)
    return None


def _is_private_host(url):
    """True if the URL's host is a private/LAN IP or a localhost name."""
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        return False
    lowered = host.lower()
    if lowered in ("localhost", "localhost.localdomain") or lowered.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def http_check(url, timeout=5):
    """Check if an HTTP endpoint is reachable.

    Note: on macOS, an app that does not hold the "Local Network" privacy
    permission gets ``errno 65 (EHOSTUNREACH, "No route to host")`` for every
    connect() to a LAN/private address — even when the endpoint is up and
    reachable from other processes (nc/ssh/ping). We detect that pattern and
    append an actionable hint so operators are not misled into believing the
    fleet is down.
    """
    try:
        req = urllib.request.urlopen(url, timeout=timeout)
        return {"ok": True, "status": req.status}
    except urllib.error.URLError as e:
        errno = _errno_from_urlerror(e.reason)
        hint = _LAN_HINT if errno in (64, 65, 51) and _is_private_host(url) else ""
        return {"ok": False, "output": f"{e.reason}{hint}"}
    except Exception as e:
        return {"ok": False, "output": str(e)}


def ensure_dir(path):
    """Ensure a directory exists, creating it if needed."""
    os.makedirs(path, exist_ok=True)


def read_json_file(path, default=None):
    """Read a JSON file with a default fallback."""
    if not os.path.exists(path):
        return default if default is not None else {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return default if default is not None else {}


def write_json_file(path, data):
    """Write a JSON file atomically."""
    ensure_dir(os.path.dirname(path) or ".")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp, path)
