"""launchd unit for the HSCC read-only API (REPL-injection 2026-10-13).

OPT-IN, exactly like the daemon's own unit (hscc daemon install): the
bootstrap offers `hscc api install` — nothing is loaded without it. One
consequence the operator called out: after a REBOOT the iPhone would find
no API at all (the current PID-file start does not survive login). KeepAlive
here covers crash respin AND boot; the unit runs the real entrypoint
(`python3 -m api_server serve`) so the bind rules are unchanged, not
weakened — loopback default, token required, 0.0.0.0 refused. The
`--tailscale`/`--bind`/`--port` tail (t_37948685) is the operator's explicit
`start`-level choice carried into the unit; it never widens anything by
itself, and the server still refuses a wide bind at every boot.

Honest scope note, closed by t_d2245e7e: `hscc api status` used to read only
its PID file, so a launchd-run instance looked stopped there. It now consults
`launchctl print gui/$UID/com.hscc.api` as well, and `hscc api stop` runs
`launchctl bootout` when the unit is loaded instead of SIGTERM-ing a
KeepAlive-supervised process (which launchd would simply resurrect). The
serve entrypoint still deliberately writes NO api.pid — under launchd the
supervisor is authoritative, and a pid file that only existed to be SIGTERM'd
was the source of the wrongness.

Pure-text module (build/recommend only) so the plist shape is unit-testable
without launchctl: the install path is `plutil -lint` + a read-back compare,
mirroring tests/test_install.py's fake-launchctl pattern.
"""
from __future__ import annotations

import os
import plistlib

try:  # package import — the deployed-plugin shape (`hscc api install-service`)
    from . import hermes_python
except ImportError:  # bare import — tests/ load this dir straight onto sys.path
    import hermes_python

API_LABEL = "com.hscc.api"
API_PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{API_LABEL}.plist")


def serve_args(bind: str | None = None,
               port: int | str | None = None) -> list[str]:
    """ProgramArguments tail carrying the operator's bind choice into the unit.

    Same flags `hscc api start` takes (api_cli._parse_start_flags), minus
    ``--no-qr`` which is display-only and meaningless without a terminal.
    ``bind`` is the *pre-resolution* value — ``"tailscale"``/``"loopback"`` or
    an explicit IP — so the unit records the operator's intent and
    ``api_server.resolve_bind()`` still does the resolution (and the refusing)
    at every boot: a unit written while Tailscale was down fails at boot with
    the server's own error rather than baking a stale IP into the plist.

    Validation mirrors what `api_cli` accepts for `start` (an address-shaped
    --bind, a numeric --port) so a nonsense combination is refused here, at
    install time, instead of producing a unit that crash-loops under KeepAlive.
    The always-refused binds (0.0.0.0 / ::) are refused here too; this module
    is deliberately stdlib-only (no `import api_server` — the plugin dir is not
    on sys.path at launchd-install time), so
    tests/test_api_launchd.py pins the refusal behaviour against the literals
    ``api_server._REFUSED_BINDS`` documents, keeping the copy honest.
    Returns ``[]`` for the no-flag case, keeping the default argv untouched.
    """
    import ipaddress

    args: list[str] = []
    if bind is not None:
        if bind == "tailscale":
            args.append("--tailscale")
        elif bind == "loopback":
            args += ["--bind", "loopback"]
        else:
            try:
                addr = ipaddress.ip_address(bind)
            except ValueError as exc:
                raise ValueError(
                    f"invalid --bind value {bind!r} — expected 'loopback', "
                    "'tailscale', or an explicit IP address"
                ) from exc
            if addr.is_unspecified:
                # ip_address('0.0.0.0').is_unspecified / same for '::' — the
                # same two literals api_server.resolve_bind always refuses,
                # expressed as a property instead of a duplicated literal set.
                raise ValueError(
                    f"refusing to bind {bind!r} — the HSCC API must never be "
                    "reachable from an untrusted network."
                )
            args += ["--bind", bind]
    if port is not None:
        try:
            port = int(port)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid --port value {port!r}") from exc
        if not (0 < port <= 65535):
            raise ValueError(f"port {port} out of range 1-65535")
        args += ["--port", str(port)]
    return args


def build_api_plist(python_path: str, hscc_api_dir: str, *,
                    log_path: str | None = None,
                    serve_extra_args: list[str] | None = None) -> dict:
    """Plist dict for the API under launchd.

    Runs ``<python> -m api_server serve`` with cwd=hscc_api_dir (the module
    lives there; api_server itself enforces the loopback bind + token).
    ``serve_extra_args`` (see :func:`serve_args`) is appended verbatim so the
    unit can carry the same ``--tailscale``/``--bind``/``--port`` the operator
    uses with ``hscc api start``; it defaults to nothing, which keeps a plain
    loopback install byte-identical to before the flags existed.
    """
    return {
        "Label": API_LABEL,
        "ProgramArguments": [python_path, "-m", "api_server", "serve",
                             *(serve_extra_args or [])],
        "WorkingDirectory": hscc_api_dir,
        "RunAtLoad": True,
        "KeepAlive": True,
        # t_d2245e7e: launchd's default throttle is 10s, and a KeepAlive unit
        # with no override respins on it forever. That stopped being
        # theoretical when t_37948685 added `--tailscale` passthrough: a unit
        # whose bind is the tailnet legitimately FAILS at boot whenever
        # Tailscale is down (`api_server.resolve_bind` refuses to widen the
        # bind — correct behaviour), and every retry appends a refusal to
        # ~/.hscc/logs/api.err.log. 30s, matching hscc_daemon/install.py's
        # PLIST_CONTENT, so the two units on one machine throttle alike.
        "ThrottleInterval": 30,
        # How long launchd waits after SIGTERM before SIGKILL. Same 10 as the
        # daemon unit: `python -m api_server serve` shuts its gateway sidecar
        # down on SIGTERM (t_37948685), and 10s is well past that teardown —
        # launchd's 20s default would only delay a `bootout`/logout.
        "ExitTimeOut": 10,
        "StandardOutPath": log_path or os.path.expanduser("~/.hscc/logs/api.log"),
        "StandardErrorPath": log_path or os.path.expanduser("~/.hscc/logs/api.err.log"),
        "EnvironmentVariables": {
            "HOME": os.path.expanduser("~"),
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:"
                    + os.path.pathsep.join(
                        p for p in (os.environ.get("PATH", "").split(os.path.pathsep))
                        if p),
        },
    }


def recommend_api_python() -> str | None:
    """Interpreter for the API unit, resolved at install time.

    Hermes' venv (which carries the websockets/starlette deps the API needs),
    never the system python3 — but resolved through the SHARED ladder in
    :mod:`hscc_daemon.hermes_python`, because ``$HERMES_HOME`` is the PROFILE
    dir whenever Hermes runs under a profile and ``<profile>/hermes-agent`` does
    not exist. The old one-path join here made `hscc api install-service`
    unusable from every profile session (t_314eb13c); ``hermes_venv_python()``
    tries the exported home, the ``profiles/``-grandparent root, ``~/.hermes``,
    then the venv this process is running in.

    Returns None only when NO candidate exists anywhere searched — the caller
    reports it rather than writing a plist that could never start.
    """
    return hermes_python.hermes_venv_python()


def install_api_plist(hscc_api_dir: str, python_path: str | None = None, *,
                      serve_extra_args: list[str] | None = None) -> dict:
    """Write the LaunchAgent plist. NEVER loads it — `launchctl bootstrap`
    stays a deliberate manual step (an opt-in unit must not become live from
    a file write). Idempotent: a byte-identical re-install is a no-op.

    ``serve_extra_args`` (build it with :func:`serve_args`) carries the bind
    flags into ProgramArguments; omitted, the unit is the plain loopback one.
    """
    py = python_path or recommend_api_python()
    if not py:
        raise RuntimeError(
            "no Hermes venv python found — looked at: "
            f"{hermes_python.describe_search()} "
            "— pass python_path explicitly or install Hermes first")
    data = build_api_plist(py, os.path.abspath(hscc_api_dir),
                           serve_extra_args=serve_extra_args)
    os.makedirs(os.path.dirname(API_PLIST_PATH), exist_ok=True)
    os.makedirs(os.path.expanduser("~/.hscc/logs"), exist_ok=True)
    new_bytes = plistlib.dumps(data, sort_keys=False)
    try:
        with open(API_PLIST_PATH, "rb") as fh:
            if fh.read() == new_bytes:
                return {"path": API_PLIST_PATH, "changed": False, "python": py}
    except FileNotFoundError:
        pass
    tmp = API_PLIST_PATH + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(new_bytes)
    os.replace(tmp, API_PLIST_PATH)
    return {"path": API_PLIST_PATH, "changed": True, "python": py}


def uninstall_api_plist() -> dict:
    """Remove the plist file (manual `launchctl bootout` first)."""
    try:
        os.remove(API_PLIST_PATH)
        return {"path": API_PLIST_PATH, "removed": True}
    except FileNotFoundError:
        return {"path": API_PLIST_PATH, "removed": False}


# t_d2245e7e: `plist_snippet()` — a copy-paste text block for the manual
# route — was deleted here along with its test. It had no caller anywhere in
# the repo (grep-verified twice), and the manual route it duplicated is already
# printed verbatim by `api_cli._handle_install_service` (plutil -lint +
# launchctl bootstrap + launchctl print, with the real path and interpreter).
# Two surfaces for one instruction means one of them drifts.
