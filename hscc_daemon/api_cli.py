"""HSCC `api` CLI verb — start / stop / status for the HSCC HTTP API server.

Thin lifecycle wrapper (Phase A5). It reuses the EXISTING daemon PID/log
conventions from ``daemon_ops`` (parameterized to the API's own ``api.pid`` /
``api.log`` files) and calls into A1's bind/config/token resolution in
``hscc-api/api_server.py`` — it does NOT re-implement or weaken the bind rules
(loopback default, tailnet opt-in, NEVER 0.0.0.0). All HTTP handling lives in
the server module; this file only manages the background process lifecycle.

On ``start`` we also run the payload-drift check (``verify.check_plugin_payload``)
so an API that will serve an installed (possibly stale) payload cannot start
silently. Reference: docs/DESIGN-api.md §C (Operational shape, `hscc api` verb
group).

Human output routes through ``hscc_daemon.cli_theme`` (the shared Hermes
palette) via ``_console``; on a non-tty stdout Rich degrades to PLAIN text with
NO ANSI escape codes, so piped/scripted capture stays clean. There is no
``--json`` machine path in this group (start/stop/status are operator verbs);
the only byte-exact contract is the QR payload (``_build_qr_payload``), which
is WIRE DATA encoded into the QR matrix — never themed. ``--theme <name>``
selects the human palette and is stripped before dispatch (mirrors autodown_cli
/ kanban_cli).
"""

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from hscc_daemon import cli_theme as theme
from hscc_daemon import daemon_ops
from hscc_daemon import qr_code
from hscc_daemon import verify

# The API server is a SEPARATE background process from the monitoring daemon,
# so it gets its own PID + log files (distinct from daemon.pid / daemon.log).
API_PID_FILE = os.path.expanduser("~/.hscc/api.pid")
API_LOG_FILE = os.path.expanduser("~/.hscc/api.log")

VALID_SUBCOMMANDS = ("start", "stop", "status", "install-service", "uninstall-service")

# Flags that mean "print help, execute nothing" at the verb level
# (BROKEN B1/B2/B3, card t_bf9f1909). Intercepted in cmd_api BEFORE dispatch —
# see SUBCOMMAND_HELP.
SUB_HELP = ("--help", "-h")

# Per-verb help, printed INSTEAD of running the verb when --help/-h appears
# anywhere in the verb's argv. Every verb acts before it reports (start forks
# a server that binds the API port, status can mint ~/.hscc/api-token and
# print a QR, install-service/uninstall-service write/remove the LaunchAgent
# plist) — so the intercept must come BEFORE dispatch. Lines stay <= 78
# columns, matching the group HELP_TEXT convention (keeps the CLI help-surface
# gate's wrap analysis clean at width 80). Flag-shaped bracket tokens
# ([--no-qr]) are safe through Rich markup; bare words in brackets are not
# (the group line's "[args]" renders as markup), so verb usage lines avoid
# them.
SUBCOMMAND_HELP = {
    "start": """\
Usage: hscc api start [--tailscale] [--bind <ip>] [--port <n>] [--no-qr]

Start the HSCC API server in the background (double-fork, PID at
~/.hscc/api.pid, log at ~/.hscc/api.log). Refuses to start a second
instance — prints the running PID instead.

  --tailscale     Bind the tailnet address instead of loopback
  --bind <ip>     Bind this specific address (0.0.0.0 is always refused)
  --port <n>      Override the configured port (default 8787)
  --no-qr         Suppress the scannable connection QR

Bind defaults to loopback (127.0.0.1). The QR encodes the live auth token
from ~/.hscc/api-token — treat it like a password. '--theme <name>' selects
the human palette.""",
    "stop": """\
Usage: hscc api stop

Stop the running API server. If the launchd unit is loaded, this runs
'launchctl bootout gui/$UID/com.hscc.api' — a SIGTERM would be undone by
KeepAlive. Otherwise it reads ~/.hscc/api.pid and SIGTERMs it (SIGKILL
after 10s), then removes the PID file. Harmless when nothing is running.

A bootout is a SESSION-SCOPED stop: the unit file stays installed and
RunAtLoad brings the API back at the next login. Use 'hscc api
uninstall-service' to remove the unit for good.

'--theme <name>' selects the human palette.""",
    "status": """\
Usage: hscc api status [--no-qr]

Read-only report: running / not running and the configured host:port.
Consults BOTH ~/.hscc/api.pid and 'launchctl print gui/$UID/com.hscc.api',
so an API that launchd started at boot reads as running (it used to look
stopped here). NEVER prints the auth token. Shows the scannable connection
QR when a token exists (it encodes that credential — keep it off streams
and screen-shares); '--no-qr' suppresses the QR.

'--theme <name>' selects the human palette.""",
    "install-service": """\
Usage: hscc api install-service [--tailscale] [--bind <ip>] [--port <n>]

OPT-IN boot-resilience (macOS only): write the launchd unit
~/Library/LaunchAgents/com.hscc.api.plist (RunAtLoad + KeepAlive) so a
reboot no longer takes the API offline. The bind flags are carried into
the unit exactly as 'start' honours them; no flags keeps the loopback
unit. '--no-qr' is accepted and ignored (a launchd unit has no terminal).
Deliberately does NOT load the unit — activating stays your call:

  plutil -lint ~/Library/LaunchAgents/com.hscc.api.plist
  launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.hscc.api.plist

'--theme <name>' selects the human palette.""",
    "uninstall-service": """\
Usage: hscc api uninstall-service

Remove the launchd unit file written by install-service (a file operation
only — it never execs launchctl). If the unit was loaded, boot it out
yourself first:

  launchctl bootout gui/$UID/com.hscc.api

'--theme <name>' selects the human palette.""",
}

# Two-line layout per verb (F4, docs/audits/CLI_HELP_2026-10-15.md Class B):
# the usage line stands alone, the description goes on an indented continuation
# line. The old two-column block overflowed the help-surface gate at BOTH
# mandated widths (the install-service/uninstall-service entries at 116 cols
# failed even width 120) because Rich does NOT wrap a pre-formatted string —
# on a non-tty stdout the group console is fixed at width 200 (see _console),
# so COLUMNS is ignored and a hard line simply stays hard. That is why every
# line below is authored <= 74 columns: it must survive the widest console this
# group can print from, not just the 80/120 the gate measures.
# Wording is the base's verbatim — this surface is re-flowed, never reworded.
HELP_TEXT = """\
HSCC API server — HTTP API for external apps (e.g. the iOS client).

Usage: hscc api <subcommand> [args]

  hscc api start [--tailscale] [--bind <ip>] [--port <n>] [--no-qr]
      Start the API server in the background
  hscc api stop
      Stop the running API server
  hscc api status [--no-qr]
      Show running/stopped + bound host:port
  hscc api install-service [--tailscale] [--bind <ip>] [--port <n>]
      OPT-IN: write the launchd unit (boot-resilient API;
      loading stays manual)
  hscc api uninstall-service
      Remove the launchd unit file written by install-service
  hscc api --help
      This help

Bind defaults to loopback (127.0.0.1). '--tailscale' or '--bind <ip>' opt in
to exposing it on the tailnet / a specific IP. 0.0.0.0 is always refused.
The auth token lives at ~/.hscc/api-token and is ENCODED into the QR, so the
QR must be treated like a password — do not show it on a stream or
screen-share.
'--no-qr' suppresses the scannable connection QR shown by start/status."""


def _console(theme_name=None, **kwargs):
    """A Rich console bound to the resolved theme, with the anti-collapse fix.

    On a TTY the console uses the actual terminal width (no explicit ``width=``
    wins otherwise); on a non-tty stdout (pipe / capture) it is fixed at 200 so
    wide piped rows keep their content instead of wrapping. Mirrors kanban_cli.
    """
    if sys.stdout.isatty():
        kwargs.pop("width", None)
    else:
        kwargs.setdefault("width", 200)
    return theme.make_console(theme_name, **kwargs)


def _strip_theme(args):
    """Remove ``--theme <name>`` / ``--theme=<name>`` tokens from an argv slice.

    ``--theme`` only selects the palette for the human view; it is not part of
    the (non-existent) machine contract for this group. Mirrors
    ``autodown_cli._strip_theme`` so the api group honors the same flag via its
    own argv (hscc.py passes the api argv through unstripped). Returns
    ``(cleaned, theme_name_or_None)``.
    """
    theme_name = None
    cleaned = []
    i = 0
    n = len(args)
    while i < n:
        a = args[i]
        if a == "--theme":
            if i + 1 < n:
                theme_name = args[i + 1]
                i += 2
                continue
            i += 1
            continue
        if a.startswith("--theme="):
            theme_name = a.split("=", 1)[1]
            i += 1
            continue
        cleaned.append(a)
        i += 1
    return cleaned, theme_name


def _resolve_api_dir() -> Path:
    """Locate the hscc-api plugin dir (a sibling of hscc_daemon)."""
    return Path(__file__).resolve().parent.parent / "hscc-api"


def _load_api_server():
    """Import the A1 server module from the hscc-api plugin dir.

    The server imports ``routes_cluster`` / ``routes_project`` by bare name,
    so the plugin dir must be on ``sys.path`` (we put it there once, mirroring
    how ``_handle_project`` inserts ``hscc-project/``). Returns the loaded
    module; raises RuntimeError with a clear message if the plugin is missing.
    """
    api_dir = _resolve_api_dir()
    if not api_dir.is_dir():
        raise RuntimeError(f"hscc-api plugin not found at {api_dir}")
    if str(api_dir) not in sys.path:
        sys.path.insert(0, str(api_dir))
    try:
        import api_server  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            f"could not import hscc-api server from {api_dir}: {exc}"
        ) from exc
    return api_server


def _load_gateway_bridge():
    """Import the hscc-api ```gateway_bridge`` module (optionally).

    Returns the module, or ``None`` when it cannot be imported — so the live
    serve-sidecar wiring fails safe: a missing/new plugin never stops the API
    from serving. ``_load_api_server`` has already put the hscc-api dir on
    sys.path; we reuse that.
    """
    _load_api_server()  # ensure the hscc-api dir is on sys.path
    try:
        import gateway_bridge  # noqa: F401
    except ImportError:
        return None
    return gateway_bridge


def _has_flag(argv, name):
    """True if ``name`` appears in ``argv`` (a bare boolean flag)."""
    return name in argv


def _is_loopback(host) -> bool:
    """True if ``host`` is a loopback address a phone cannot reach."""
    if not host:
        return True
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    return host.startswith("127.") or host == "[::1]"


def _build_qr_payload(host, port, token) -> str:
    """Connection-settings JSON for the QR.

    Single line, NO trailing newline, and key order fixed (v, host, port,
    token) — the iOS scanner depends on this byte-for-byte. No spaces after
    the colons. ``token`` is the real live credential read from the token
    file; the caller prints it ONLY to stdout and never to a log/error.
    """
    return '{"v":1,"host":"%s","port":%d,"token":"%s"}' % (host, port, token)


def _print_api_qr(host, port, token, *, force_ascii=False, theme_name=None):
    """Print the scannable connection-settings QR with a security warning.

    The warning is printed unconditionally (it accompanies the QR itself, not
    the flag state): whoever scans this QR is handed a live credential. When
    the bind is loopback-only we additionally warn that a phone cannot reach
    it. `--no-qr` suppresses this entire block at the call site.

    Only the surrounding prose is themed; the QR matrix itself is fixed-width
    block art rendered verbatim (theming it would break its alignment). The
    QR payload (``_build_qr_payload``) is WIRE DATA for the iOS scanner and is
    never altered. On a non-tty stdout Rich degrades to plain text — no ANSI.
    """
    payload = _build_qr_payload(host, port, token)
    matrix = qr_code.make_qr(payload.encode("utf-8"))
    console = _console(theme_name)
    console.print("  Scan to connect: grants API access to this host.")
    if _is_loopback(host):
        console.print(
            "  [warn]Warning:[/warn] bound to loopback — a phone cannot reach "
            "this host."
        )
    console.print(qr_code.render_text(matrix, force_ascii=force_ascii))
    console.print()


def _parse_start_flags(argv):
    """Parse ``start`` flags into (bind_override, port_override, no_qr).

    Supports ``--tailscale`` (→ bind 'tailscale'), ``--bind <ip>``, ``--port
    <n>``, and ``--no-qr`` (suppress the QR), matching the design §C config
    precedence (flags are the highest precedence). Unknown flags are ignored.
    """
    bind_override = None
    port_override = None
    no_qr = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--tailscale":
            bind_override = "tailscale"
        elif arg == "--no-qr":
            no_qr = True
        elif arg == "--bind":
            if i + 1 < len(argv):
                bind_override = argv[i + 1]
                i += 1
        elif arg == "--port":
            if i + 1 < len(argv):
                try:
                    port_override = int(argv[i + 1])
                except ValueError:
                    pass
                i += 1
        i += 1
    return bind_override, port_override, no_qr


def _serve(api, bind_override, port_override):
    """Run the API server loop in the foreground (grandchild of `start`).

    Resolves config + token again here (the child has the same disk state) via
    A1's ``create_server`` — which honors the bind rules and fails closed on an
    unreadable/empty token. Cleans up the PID file on exit.
    """
    try:
        server = api.create_server(
            bind_override=bind_override,
            port_override=port_override,
        )
    except RuntimeError as exc:
        daemon_ops.log(
            f"HSCC API start failed: {exc}", "ERROR",
            log_file=API_LOG_FILE, pid_file=API_PID_FILE,
        )
        daemon_ops.write_stopped(API_PID_FILE)
        os._exit(1)

    addr = server.ctx.config
    daemon_ops.log(
        f"HSCC API listening on {addr['host']}:{addr['port']}",
        log_file=API_LOG_FILE, pid_file=API_PID_FILE,
    )
    # t_29e033a4: own the live ``hermes serve`` sidecar as a supervised child
    # of the API process so the session-continuity bridge actually delivers:
    # the app can live-view the operator's project session because a driver
    # can connect to this serve. Best-effort — if the serve cannot boot, the
    # API still runs (the WS relay's REST fallback stays authoritative) and we
    # log why. The serve is killed when the API exits (finally below).
    try:
        _bridge = _load_gateway_bridge()
        if _bridge is not None:
            port = _bridge_default_serve_port(server)
            if not _bridge.start_serve(port=port):
                daemon_ops.log(
                    "HSCC API: live gateway sidecar NOT started (bridge "
                    "inert; WS relay REST fallback stays authoritative)",
                    log_file=API_LOG_FILE, pid_file=API_PID_FILE,
                )
    except Exception as exc:  # never let a sidecar failure stop the API
        daemon_ops.log(
            f"HSCC API: gateway sidecar startup skipped ({exc})",
            log_file=API_LOG_FILE, pid_file=API_PID_FILE,
        )
    try:
        server.serve_forever()
    except Exception as exc:
        daemon_ops.log(
            f"HSCC API crashed: {exc}", "ERROR",
            log_file=API_LOG_FILE, pid_file=API_PID_FILE,
        )
    finally:
        # Tear down the sidecar (drivers release their turn leases, then the
        # serve process is terminated). Best-effort, never blocks shutdown.
        try:
            _bridge2 = _load_gateway_bridge()
            if _bridge2 is not None:
                _bridge2.shutdown()
        except Exception:
            pass
        daemon_ops.write_stopped(API_PID_FILE)


def _bridge_default_serve_port(server) -> int:
    """Resolve the serve-sidecar port for the live API.

    Uses the documented default 9119 unless ``~/.hscc/api.json`` pins a
    ``gateway.serve_port`` (a forward-compatible override; the config is read
    through the server's own ctx so it cannot diverge from how the API itself
    resolved its config). Fails safe to 9119 on any error.
    """
    import os
    try:
        cfg_path = os.path.expanduser("~/.hscc/api.json")
        import json as _json
        if os.path.exists(cfg_path):
            raw = _json.loads(open(cfg_path).read())
            sp = (raw or {}).get("gateway", {}).get("serve_port")
            if isinstance(sp, int) and 0 < sp < 65536:
                return sp
    except Exception:
        pass
    return 9119


def _sigterm_handler(signum, frame):
    """Graceful shutdown on SIGTERM/SIGINT: remove PID file, exit."""
    daemon_ops.log(
        f"Received signal {signum}, shutting down...",
        log_file=API_LOG_FILE, pid_file=API_PID_FILE,
    )
    daemon_ops.write_stopped(API_PID_FILE)
    os._exit(0)


def _warn_payload_drift(repo_root=None, plugins_dir=None, theme_name=None):
    """Run the payload-drift check and warn LOUDLY on drift, silently otherwise.

    Uses ``verify.check_plugin_payload`` — the SAME check already wired into
    ``hscc verify`` (run_all), so there is one source of truth for the diff. On
    a real user install there is no repo checkout to diff against, in which
    case the check returns ``ok=None`` and we say nothing and start normally
    (graceful degrade). Only ``ok=False`` (actual drift) prints the warning
    naming the drifted plugins and the exact remedy.

    ``repo_root`` / ``plugins_dir`` are injectable for tests (defaults go to
    the real repo root and ``~/.hermes/plugins``). ``theme_name`` selects the
    human palette for the warning banner.
    """
    try:
        res = verify.check_plugin_payload(
            repo_root=repo_root, plugins_dir=plugins_dir)
    except Exception as exc:  # never let the drift check block startup
        print(f"Warning: payload-drift check failed ({exc})", file=sys.stderr)
        return
    if res is None or res.get("ok") is not False:
        # ok=None: no repo checkout -> nothing to diff, start normally.
        # ok=True:  installed payload matches the repo.
        return
    console = _console(theme_name)
    console.print(
        "\n"
        "====================================================================\n"
        "[warn]WARNING:[/warn] the INSTALLED payload differs from this repo. "
        "`hscc api`\n"
        "serves ~/.hermes/plugins, NOT the repo — merging here did not deploy.\n"
        "\n"
        + "  " + (res.get("detail") or "installed payload differs from repo")
        + "\n"
        "\n"
        "Fix: re-deploy the repo state to ~/.hermes/plugins:\n"
        "    python3 hscc-bootstrap/install_payload.py\n"
        "then restart the API. Startup continues anyway (a stale payload that\n"
        "still boots is better than an API that refuses to start).\n"
        "====================================================================\n"
    )


def _strict_serve_flag_values(argv):
    """Refuse malformed ``--bind``/``--port`` VALUES for install-service.

    ``_parse_start_flags`` is deliberately forgiving for `start`: an
    unparseable ``--port`` value falls through ``except ValueError: pass`` and
    the server starts on the configured port, and ``--bind`` with no value
    after it is ignored. That forgiveness is inherited by install-service
    because the flags are parsed by the same helper — and for a launchd unit it
    would be the very defect this card fixes: the operator asks for
    ``--port 88ab``, the flag is dropped, and the unit comes up on 8787 at
    every boot with no complaint. A unit is not a one-off command line — it is
    the thing that runs unattended at boot — so a value the parser cannot read
    is refused here, loudly, before the plist file is written.

    Only the SHAPE is checked here (a value exists, a port is an int); the
    address itself is validated by ``api_launchd.serve_args``. Returns nothing;
    raises ValueError with an actionable message.
    """
    for i, arg in enumerate(argv):
        if arg == "--port":
            if i + 1 >= len(argv):
                raise ValueError("--port needs a value, e.g. --port 8787")
            raw = argv[i + 1]
            try:
                int(raw)
            except ValueError:
                raise ValueError(
                    f"invalid --port value {raw!r} — expected a port number"
                ) from None
        elif arg == "--bind":
            if i + 1 >= len(argv):
                raise ValueError(
                    "--bind needs a value, e.g. --bind 100.64.0.1 (or use "
                    "--tailscale)")


def _launchd_label():
    """The LaunchAgent label, imported lazily so this module stays importable
    on Linux (where the api_launchd module's launchd assumptions do not hold).
    """
    from hscc_daemon import api_launchd
    return api_launchd.API_LABEL


#: ``launchctl print`` reports the running pid as an indented ``pid = 12345``
#: property line. Deliberately the SAME regex shape as
#: hscc-bootstrap/doctor_supervision.py:_LAUNCHCTL_PID_RE — the CLI and doctor
#: must never disagree about who owns the job.
_LAUNCHCTL_PID_RE = re.compile(r"^\s*pid\s*=\s*(\d+)\s*$", re.MULTILINE)

#: ``launchctl print`` reports ``\tstate = running`` at the top level (a tab
#: indent), while nested sections print deeper-indented ``state =`` lines and
#: sibling keys such as ``plist state =``. Anchoring on one tab keeps the
#: top-level job state; ``.+?`` because launchd's own states are multi-word
#: ("not running"), which a ``\S+`` would silently truncate to "not".
_LAUNCHCTL_STATE_RE = re.compile(r"^\tstate\s*=\s*(.+?)\s*$", re.MULTILINE)


def _launchctl_run(argv, timeout=10):
    """Run one launchctl command; (returncode, output) or (None, error).

    ``returncode is None`` means the command could not be run at all (no
    launchctl binary — a non-macOS host, or a container). The caller must read
    that as "the supervisor could not be asked", NEVER as a statement about
    the unit, which is the same rule doctor_supervision._run follows.
    """
    try:
        cp = subprocess.run([str(a) for a in argv], capture_output=True,
                            text=True, timeout=timeout)
        return cp.returncode, (cp.stdout or "") + (cp.stderr or "")
    except FileNotFoundError:
        return None, f"command not found: {argv[0]}"
    except (subprocess.SubprocessError, OSError) as exc:
        return None, str(exc)


def _launchd_matters() -> bool:
    """Whether asking launchd is worth a subprocess for this invocation.

    macOS only (launchd does not exist elsewhere), and only when a unit could
    plausibly be involved: either the plist file is on disk, or there is a PID
    file whose owner we must check before signalling. An operator who never
    ran `install-service` keeps the old cheap behaviour — `hscc api status`
    does not fork a `launchctl` on every call — and, just as important, a
    suite that forgets to inject the probe seam cannot reach into the
    OPERATOR's live gui domain from a test process.
    """
    if sys.platform != "darwin":
        return False
    if os.environ.get("HSCC_LAUNCHD_PROBE"):  # test hook: force the probe on
        return True
    try:
        from hscc_daemon import api_launchd
        if os.path.exists(api_launchd.API_PLIST_PATH):
            return True
    except Exception:
        pass
    return os.path.exists(API_PID_FILE)


def _refused_launchd_status() -> dict:
    """In-suite stand-in for the supervisor probe when no seam was injected.

    `launchctl print` reads the OPERATOR's live gui domain — the real machine
    when a dispatched worker runs the suite beside it. Same fence shape as
    hscc-bootstrap/doctor.py's `_refused_supervision_check`: a refusal that is
    VISIBLE in the caller's output, never a silent "not loaded" that lets a
    test pass for the wrong reason while a real unit sits loaded.
    """
    return {"available": False, "loaded": False, "pid": None, "state": None,
            "refused": True,
            "note": "launchd probe refused inside a pytest process — inject "
                    "launchd_status(_run_probe=...) or monkeypatch the function"}


def launchd_status(_run_probe=None):
    """Ask launchd about com.hscc.api — READ-ONLY, `launchctl print` only.

    Returns a dict:
      available  False only when launchctl itself could not be asked (Linux,
                 container). The caller then falls back to the PID-file view
                 and says so, exactly as it did before this existed.
      loaded     True when the job is loaded in gui/$UID.
      pid        the pid launchd currently runs, or None (loaded but throttled
                 / exited, which is a real state for a failing tailnet unit).
      state      launchd's own `state =` word ("running", "not running", ...)
                 or None when the print output had no such line.

    The probe is the one doctor_supervision.py trusts for the daemon unit, so
    `hscc doctor` and `hscc api status` cannot diverge on who owns the process.
    """
    if _run_probe is None and "pytest" in sys.modules:
        return _refused_launchd_status()
    runner = _run_probe or _launchctl_run
    target = f"gui/{os.getuid()}/{_launchd_label()}"
    rc, out = runner(["launchctl", "print", target])
    if rc is None:
        return {"available": False, "loaded": False, "pid": None,
                "state": None, "note": out}
    if rc != 0:
        # launchd answers rc!=0 for "no such job" — that is an answer, not a
        # fault: the unit is simply not loaded (never installed, or booted out).
        return {"available": True, "loaded": False, "pid": None,
                "state": None, "note": out.strip()}
    m = _LAUNCHCTL_PID_RE.search(out)
    s = _LAUNCHCTL_STATE_RE.search(out)
    return {"available": True, "loaded": True,
            "pid": int(m.group(1)) if m else None,
            "state": s.group(1) if s else None,
            "note": out.strip()}


def _is_launchd_supervised(pid, sup_pid):
    """True when `pid` is the process launchd is currently running.

    The ownership test `hscc api stop` needs: SIGTERM-ing a KeepAlive-supervised
    process is a stop that silently un-stops (launchd resurrects it within the
    throttle window), so `stop` must not do it — it has to bootout instead.
    """
    return bool(pid) and sup_pid is not None and int(pid) == int(sup_pid)


def _handle_start(argv, theme_name=None):
    """`hscc api start` — resolve bind/config + token, fork into background.

    Returns 0 on start (or already-running), non-zero on a fail-closed error
    (refused bind, empty/unreadable token, plugin missing).
    """
    argv, theme_name_ = _strip_theme(argv)
    theme_name = theme_name_ if theme_name_ is not None else theme_name
    api = _load_api_server()
    bind_override, port_override, no_qr = _parse_start_flags(argv)

    existing = daemon_ops.get_pid(API_PID_FILE)
    if existing:
        _console(theme_name).print(f"HSCC API already running (PID {existing})")
        return 0

    # Surface installed-payload drift at the moment it matters: the API serves
    # the INSTALLED payload, so before we start it, check that payload against
    # the repo. Silent on ok=None (no repo) / ok=True (match); loud on drift.
    _warn_payload_drift(theme_name=theme_name)

    # Fail-closed BEFORE forking: resolve bind/port + validate/generate the
    # token. We never fork a child that would then fail on disk state we can
    # check now, and we never bind 0.0.0.0 (A1's resolve_config refuses it).
    try:
        config = api.resolve_config(
            bind_override=bind_override,
            port_override=port_override,
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    try:
        token_server = api.load_token()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    _console(theme_name).print(
        f"Starting HSCC API on {config['host']}:{config['port']}..."
    )

    # Print the scannable connection QR unless the user suppressed it. We do
    # this only after config+token are known to be valid, so the user gets a
    # useful QR of the endpoint they are about to start. The token in the QR
    # is the real live credential printed only to stdout, never logged.
    if not no_qr:
        _print_api_qr(config["host"], config["port"], token_server,
                      theme_name=theme_name)

    daemon_ops.log(
        "HSCC API starting", log_file=API_LOG_FILE, pid_file=API_PID_FILE,
    )

    # Fork into the background using the same double-fork pattern as the daemon
    # (cli.cmd_start): fork, setsid, re-fork, write PID, run the serve loop.
    pid = os.fork()
    if pid > 0:
        # Parent: record the child PID immediately (grandchild re-saves its own).
        try:
            daemon_ops.save_pid(API_PID_FILE)
            _console(theme_name).print(f"HSCC API started (PID {pid})")
        except Exception:
            _console(theme_name).print(f"HSCC API started (child PID {pid})")
        return 0

    # Child — become a daemon, no controlling terminal.
    os.setsid()
    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)
    os.chdir(os.path.expanduser("~"))
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)

    # Grandchild — write the real PID and run the serve loop.
    daemon_ops.save_pid(API_PID_FILE)
    _serve(api, bind_override, port_override)
    os._exit(0)


def _wait_for_pid_gone(pid, timeout_s=10.0, first_delay=0.1, delay=1.0):
    """Poll `kill(pid, 0)` until the process is gone; True if it died.

    Short first poll then 1 s steps: a `bootout` usually tears the process down
    well under a second, and making the operator watch a fixed 10 s loop for a
    stop that already worked is how a stop starts looking broken.
    """
    deadline = time.monotonic() + timeout_s
    wait = first_delay
    while time.monotonic() < deadline:
        time.sleep(wait)
        wait = delay
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False        # still there, just not ours to signal
        except OSError:
            return True
    return False


def _handle_stop(argv, theme_name=None):
    """`hscc api stop` — stop the API, whichever supervisor owns it.

    Two owners, two correct verbs, and this is the reason the pair with
    `status` had to change together (t_d2245e7e):

      * the launchd job is LOADED → `launchctl bootout gui/$UID/com.hscc.api`.
        A SIGTERM here is a stop that silently un-stops: `KeepAlive`
        resurrects the process within the throttle window, so the operator is
        told "stopped" while the API is serving again 30 s later. That lie is
        what the old note in the module docstring was apologising for; bootout
        removes it instead.
      * nobody supervises it (a plain `hscc api start`) → SIGTERM, SIGKILL
        after the grace window, exactly as before.

    Launchd is only asked when a unit could plausibly be involved (macOS, and
    either a unit file on disk or a PID file to check ownership against).
    `python -m api_server serve` deliberately writes no pid file, so an
    operator who never installed the unit must not pay a `launchctl` fork for
    `status`, and a suite that forgets to inject the seam cannot accidentally
    ask the operator's live gui domain.
    """
    argv, theme_name_ = _strip_theme(argv)
    theme_name = theme_name_ if theme_name_ is not None else theme_name
    console = _console(theme_name)
    pid = daemon_ops.get_pid(API_PID_FILE)
    sup = launchd_status() if _launchd_matters() else {
        "available": False, "loaded": False, "pid": None, "state": None}

    # The job is loaded, so launchd owns its future: bootout, do not signal.
    # The test is `loaded`, not "launchd's pid == the pid file's pid", on
    # purpose: `KeepAlive` stays armed while the job is loaded, so a SIGTERM
    # gets undone on the next throttle tick EVEN IF launchd has no pid right
    # now (crash-looping tailnet unit, mid-throttle). Any narrower test would
    # re-create the silent-un-stop this card exists to kill.
    if sup.get("available") and sup.get("loaded"):
        return _stop_launchd(console, sup, pid, theme_name=theme_name)

    if not pid:
        if sup.get("available"):
            console.print("HSCC API is not running "
                          "(launchd: unit not loaded, nothing to stop)")
        else:
            console.print("HSCC API is not running")
        daemon_ops.write_stopped(API_PID_FILE)
        return 0

    _console(theme_name).print(f"Stopping HSCC API (PID {pid})...")
    daemon_ops.log(
        "HSCC API stop requested", log_file=API_LOG_FILE, pid_file=API_PID_FILE,
    )
    try:
        os.kill(pid, signal.SIGTERM)
        if _wait_for_pid_gone(pid):
            _console(theme_name).print(f"HSCC API stopped (PID {pid})")
            return 0
        os.kill(pid, signal.SIGKILL)
        _console(theme_name).print(f"HSCC API force-killed (PID {pid})")
    except ProcessLookupError:
        _console(theme_name).print("HSCC API already stopped")
    except Exception as exc:
        print(f"Error stopping HSCC API: {exc}", file=sys.stderr)
        return 1
    finally:
        daemon_ops.write_stopped(API_PID_FILE)
    return 0


def _stop_launchd(console, sup, pid, theme_name=None):
    """`launchctl bootout` the unit — the only stop that sticks under KeepAlive.

    Honest about scope, because the word "stop" is where this could lie:
    bootout is SESSION-SCOPED. It unloads the job from this gui domain, so
    launchd stops respawning it — but the plist file stays on disk, so
    `RunAtLoad` brings the API back at the next login or reboot. Removing the
    unit for good is `hscc api uninstall-service`, and the screen says so
    rather than letting "stopped" read as permanent.
    """
    label = _launchd_label()
    target = f"gui/{os.getuid()}/{label}"
    daemon_ops.log(
        f"HSCC API stop requested (launchd bootout {target})",
        log_file=API_LOG_FILE, pid_file=API_PID_FILE,
    )
    owned_pid = sup.get("pid") or pid
    console.print(f"HSCC API is supervised by launchd ({label}"
                  + (f", pid {owned_pid}" if owned_pid else "") + ") — "
                  "booting the unit out (a SIGTERM would be undone by "
                  "KeepAlive)...")
    rc, out = _launchctl_run(["launchctl", "bootout", target])
    if rc is None:
        # Could not exec launchctl at all — say so instead of claiming a stop.
        print(f"error: could not run launchctl: {theme.esc(out)}", file=sys.stderr)
        return 1
    if rc != 0:
        # rc 3 = "Could not find service ... in domain" — already not loaded.
        # Anything else is launchd's own complaint, passed through as DATA.
        low = (out or "").lower()
        if "could not find" in low or "not found" in low:
            console.print(f"HSCC API unit already unloaded ({label})")
            daemon_ops.write_stopped(API_PID_FILE)
            return 0
        print(f"error: launchctl bootout failed: {theme.esc((out or '').strip())}",
              file=sys.stderr)
        return 1
    if owned_pid and not _wait_for_pid_gone(owned_pid):
        console.print(f"[warn]bootout accepted but pid {owned_pid} is still "
                      "exiting[/warn] — check: "
                      f"launchctl print gui/$UID/{label}")
        return 0
    daemon_ops.write_stopped(API_PID_FILE)
    console.print(f"HSCC API stopped (launchd unit {label} booted out)")
    console.print(
        "This is a SESSION-SCOPED stop: the unit file is still installed, so "
        "RunAtLoad\nstarts the API again at the next login or reboot. To remove "
        "it for good:\n  hscc api uninstall-service")
    return 0


def _handle_status(argv, theme_name=None):
    """`hscc api status` — report running/stopped + the bound host:port.

    Launchd-aware since t_d2245e7e, as the PAIR to `stop`: the serve entrypoint
    deliberately writes no api.pid, so the PID-file read alone reported
    "not running" for an API that launchd had been serving since boot. `stop`
    now boots the unit out, so `status` must tell the truth about it too —
    changing one verb without the other is what made the two wrong in
    opposite directions. The launchd row is added on top of the PID-file row
    (never replacing it): the PID-file path is still how a plain
    `hscc api start` is running, and both can be true on one machine.

    NEVER prints the auth token. Uses A1's resolve_config only to report the
    configured bind address (loopback default honored); does not bind anything.
    Prints a scannable connection QR (suppressed by ``--no-qr``) when a token
    exists — if the token is missing/unreadable, says so and exits cleanly.
    """
    argv, theme_name_ = _strip_theme(argv)
    theme_name = theme_name_ if theme_name_ is not None else theme_name
    no_qr = _has_flag(argv, "--no-qr")

    api = None
    try:
        api = _load_api_server()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    host = port = None
    try:
        cfg = api.resolve_config()
        host, port = cfg["host"], cfg["port"]
    except RuntimeError as exc:
        # Config issues shouldn't stop the status report entirely.
        print(f"Warning: could not resolve bind address: {exc}", file=sys.stderr)

    pid = daemon_ops.get_pid(API_PID_FILE)
    sup = launchd_status() if _launchd_matters() else {
        "available": False, "loaded": False, "pid": None, "state": None}
    if pid:
        status, state_line = "ok", f"HSCC API is running (PID {pid})"
    elif os.path.exists(API_PID_FILE):
        status, state_line = "warn", "stale PID file (not running)"
    elif sup.get("available") and sup.get("loaded") and sup.get("pid"):
        # THE bug this card exists for: launchd has served this API since boot
        # and the PID-file read alone said "not running".
        status = "ok"
        state_line = (f"HSCC API is running under launchd "
                      f"(pid {sup['pid']}, {_launchd_label()})")
    elif sup.get("available") and sup.get("loaded"):
        # Loaded but no pid: throttled after a failure (a --tailscale unit with
        # Tailscale down is exactly this) or exiting. Report launchd's own
        # word — "not running" here is the truth, not a false alarm.
        status = "warn"
        state_line = (f"launchd unit loaded, no process "
                      f"(state: {sup.get('state') or 'unknown'})")
    else:
        status, state_line = "warn", "not running"

    console = _console(theme_name)
    rows = [state_line]
    if sup.get("refused"):
        # Suite-only (see _refused_launchd_status): a test that forgot to inject
        # the seam must SEE that, not silently read the PID-file fallback.
        rows.append("Supervisor:    launchd probe refused "
                    "(inject launchd_status(_run_probe=...))")
    if sup.get("available") and sup.get("loaded"):
        # The supervisor's own account, printed even when the PID-file row
        # already said "running" (a `hscc api start`-owned process beside a
        # loaded unit is the orphan class doctor_supervision hunts — an
        # operator must be able to SEE that).
        rows.append(f"Supervisor:    launchd {_launchd_label()} loaded"
                    + (f", pid {sup['pid']}" if sup.get("pid") else ", no pid")
                    + (f", state {sup['state']}" if sup.get("state") else ""))
    if host is not None:
        rows.append(f"Listening:     {host}:{port}")
    console.print(theme.make_status_panel(
        "\n".join(rows), status=status, title="api"))

    # The QR is only useful when there is an endpoint AND a valid token. If
    # the token is missing/unreadable, say so and exit cleanly (no QR). The
    # token is passed straight into the QR payload (stdout only, never logged).
    if not no_qr and host is not None:
        try:
            token = api.load_token()
        except RuntimeError as exc:
            # exc text embeds the token-file path — DATA.
            console.print(f"No connection QR: could not read auth token ({theme.esc(exc)})")
        else:
            _print_api_qr(host, port, token, theme_name=theme_name)
    return 0


def _handle_install_service(argv, theme_name=None):
    """OPT-IN boot-resilience for the API (REPL-injection 2026-10-13).

    Writes ~/Library/LaunchAgents/com.hscc.api.plist — RunAtLoad+KeepAlive so
    a REBOOT no longer takes the iPhone offline (a PID-file start does not
    survive login). Deliberately does NOT load the unit: `launchctl bootstrap`
    stays a manual operator step, same opt-in spirit as `hscc daemon install`
    but one notch stricter — writing the file changes nothing until you load
    it. macOS only (launchd).

    ``--tailscale`` / ``--bind <ip>`` / ``--port <n>`` are forwarded into the
    unit's ProgramArguments so a tailnet-served API can move onto launchd
    without silently losing its bind (t_37948685 — the operator's running
    `hscc api start --tailscale` had no launchd equivalent to migrate to).
    Parsed by the SAME helper `start` uses, so a flag combination that means
    one thing to `start` means the same thing here; ``--no-qr`` is accepted and
    dropped because it is display-only and a launchd unit has no terminal.
    """
    argv, theme_name_ = _strip_theme(argv)
    if sys.platform != "darwin":
        print("install-service is macOS-only (launchd)", file=sys.stderr)
        return 1
    from hscc_daemon import api_launchd
    try:
        _strict_serve_flag_values(argv)
        bind_override, port_override, _no_qr = _parse_start_flags(argv)
        extra = api_launchd.serve_args(bind=bind_override, port=port_override)
    except ValueError as exc:
        # Refused BEFORE the file is touched — a unit that cannot start is
        # worse than no unit (launchd would respawn the failure forever).
        print(f"error: {theme.esc(exc)}", file=sys.stderr)
        return 1
    api_dir = _resolve_api_dir()
    if not api_dir.is_dir():
        print(f"error: hscc-api dir not found at {api_dir}", file=sys.stderr)
        return 1
    try:
        res = api_launchd.install_api_plist(str(api_dir),
                                            serve_extra_args=extra)
    except (RuntimeError, OSError) as exc:
        print(f"error: {theme.esc(exc)}", file=sys.stderr)
        return 1
    console = _console(theme_name)
    state = "written" if res["changed"] else "already up to date"
    console.print(
        f"launchd unit {state}: {res['path']}\n"
        f"  interpreter: {res['python']}\n"
        + (f"  serve flags: {' '.join(extra)}\n" if extra else "")
        + "NOT loaded yet (opt-in). To activate:\n"
        f"  plutil -lint {res['path']}\n"
        f"  launchctl bootstrap gui/$UID {res['path']}\n"
        f"To check: launchctl print gui/$UID/{api_launchd.API_LABEL} | head -20\n"
        # t_d2245e7e: this used to warn that the API would LOOK STOPPED here.
        # `hscc api status` now asks launchd too and `hscc api stop` boots the
        # unit out, so the asymmetry is gone — say what the verbs do instead.
        "'hscc api status' reports this unit's real state (launchctl, not "
        "just the\nPID file) and 'hscc api stop' boots it out rather than "
        "signalling a\nKeepAlive-supervised process.")
    return 0


def _display_plist_path(path: str) -> str:
    """Render a LaunchAgent path as ``~/Library/…`` when it is under HOME.

    F4 (docs/audits/CLI_HELP_2026-10-15.md Class B): the base emitted this
    path inline in one hard sentence, so a HOME with an unusually long root
    (the audit probes under a sandbox HOME) produced an 87-120+ column line
    that overflowed the help-surface gate at BOTH mandated widths. Rich cannot
    help: a filesystem path has no break opportunity, and on a non-tty stdout
    this group's console is fixed at width 200, so nothing re-wraps it.

    ``API_PLIST_PATH`` is always ``~/Library/LaunchAgents/…`` by construction
    (api_launchd.API_PLIST_PATH), so the ``~``-relative form loses nothing an
    operator needs and is width-independent. A path outside HOME (only
    possible if something rebinds API_PLIST_PATH) is returned verbatim.
    """
    home = os.path.expanduser("~")
    if home and path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _handle_uninstall_service(argv, theme_name=None):
    """Remove the launchd unit FILE. Bootout first, yourself:
    `launchctl bootout gui/$UID/com.hscc.api` (this command never execs
    launchctl — removal is a file op only)."""
    argv, theme_name_ = _strip_theme(argv)
    from hscc_daemon import api_launchd
    res = api_launchd.uninstall_api_plist()
    console = _console(theme_name)
    # The path always gets its OWN indented line (F4, see _display_plist_path):
    # the absolute path must never be padded into a sentence, whatever its
    # length — that is what made this line overflow the (B) gate.
    if res["removed"]:
        console.print(f"removed (launchd unit file):\n  {_display_plist_path(res['path'])}")
        console.print(
            "If it was loaded, unload it now:\n"
            f"  launchctl bootout gui/$UID/{api_launchd.API_LABEL}")
    else:
        console.print("no unit file — nothing to remove:\n"
                      f"  {_display_plist_path(res['path'])}")
    return 0


def cmd_api(argv):
    """Dispatch `hscc api <subcommand>`; returns an exit code (never raises).

    With no subcommand (or ``--help``/``-h``) prints the group help and exits 0
    — matching how the other group verbs handle their no-subcommand/``--help``
    case. ``<verb> --help``/``-h`` prints that VERB's help and exits 0 without
    executing anything (BROKEN B1/B2/B3 — every verb acts before it reports:
    start forks a server that binds the API port, status can mint the token
    and print a QR, install-service/uninstall-service write/remove the
    LaunchAgent plist), so the help scan runs on the RAW argv BEFORE the
    ``--theme`` strip and before dispatch — a help flag can never be swallowed
    as a flag value (e.g. ``start --theme --help``). Unknown subcommands exit
    non-zero. ``--theme`` selects the human palette (stripped before dispatch,
    like autodown_cli / kanban_cli).
    """
    if not argv or argv[0] in ("--help", "-h"):
        _console().print(HELP_TEXT)
        return 0

    # --help/-h ANYWHERE after the verb prints that verb's help and executes
    # NOTHING (BROKEN B1/B2/B3). Raw-argv scan, before --theme stripping and
    # before dispatch: dispatch is what the bug was. An unknown subcommand +
    # --help shows the group help (there is no verb-specific help to print).
    if len(argv) > 1 and any(a in SUB_HELP for a in argv[1:]):
        _console().print(SUBCOMMAND_HELP.get(argv[0], HELP_TEXT))
        return 0

    argv, theme_name = _strip_theme(argv)

    sub = argv[0]
    rest = argv[1:]
    if sub == "start":
        return _handle_start(rest, theme_name=theme_name)
    if sub == "stop":
        return _handle_stop(rest, theme_name=theme_name)
    if sub == "status":
        return _handle_status(rest, theme_name=theme_name)
    if sub == "install-service":
        return _handle_install_service(rest, theme_name=theme_name)
    if sub == "uninstall-service":
        return _handle_uninstall_service(rest, theme_name=theme_name)

    _console(theme_name).print(f"Error: unknown api subcommand: {theme.esc(sub)}")
    _console(theme_name).print(
        f"Valid subcommands: {', '.join(VALID_SUBCOMMANDS)}"
    )
    return 1
