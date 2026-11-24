"""HSCC ``main`` CLI verb — the main orchestrator's own tmux pane (M4).

The 2026-11-19 design (docs/design/2026-11-19-main-orchestrator-design.md) gives
the cluster ONE main orchestrator, named at bootstrap (default ``general``).
It lives in a normal interactive Hermes REPL so the operator can talk to it and
so ``hscc orch inject`` can reach it. This module owns that pane's lifecycle:

* :func:`start` — ensure EXACTLY ONE tmux pane runs ``hermes -p <main> chat``.
  An existing pane is reused. When none exists, a window named after the
  profile is opened in the operator's tmux session (or, with no server at all,
  a detached session named after the profile). It NEVER kills another pane or
  window, and never selects/switches the operator's current window.
* :func:`status` — read-only report: name, board, pane and busy/idle. Pane
  discovery and the busy marker are ``hscc-api/repl_inject``'s, the same ones
  ``hscc orch inject`` uses, so the two surfaces cannot disagree.

The pane matcher is never copied here (GAP-C-3): discovery calls
``repl_inject.hermes_panes()`` and busy state calls ``repl_inject.is_busy()``.
Tests inject both the repl module and the tmux runner, so no suite touches a
real tmux server.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import main_wakeups
from . import suite_fence
from .hermes_python import kanban_root_refusal

VALID_SUBCOMMANDS = ("start", "status", "wakeups")
SUB_HELP = ("--help", "-h")

HELP_TEXT = """\
hscc main — the main orchestrator's pane and wake-ups.

Subcommands:
  start            Ensure one tmux pane runs the main orchestrator
  status [--json]  Report name, board, pane and busy/idle state
  wakeups list|set|enable|disable
                   Daemon-owned wake-up schedule (see below)

`start` reuses a pane that already runs the profile, opens a window named
after the profile in the operator's tmux session when none exists, and never
kills another pane or window. With no tmux server it creates a detached
session named after the profile.
`status` uses the same pane matcher and busy marker as `hscc orch inject`.
`wakeups` edits the schedule the hscc daemon fires into that pane with
`--mode auto` (queued while busy); see `hscc main wakeups --help`.
"""

SUBCOMMAND_HELP = {
    "start": """\
Usage: hscc main start

Ensure exactly ONE tmux pane is running the main orchestrator's REPL
(`hermes -p <main profile> chat`). A running pane is reused as-is.
When none exists, a window named after the profile is created in the
operator's tmux session; if no tmux server is running, a detached session
named after the profile is created instead.

Never kills another pane or window and never switches the operator's
current window. Re-run is idempotent.
""",
    "status": """\
Usage: hscc main status [--json]

Read-only report for the main orchestrator: its name, board, the tmux pane
running it, and busy/idle. Pane discovery and the busy marker are the same
ones `hscc orch inject` uses, so the two always agree.

--json prints the machine-readable receipt (name, profile, board, state,
running, busy, pane, target).
""",
    "wakeups": """\
Usage: hscc main wakeups <list|set|enable|disable> [args] [--json]

The hscc daemon owns four wake-ups and fires each into the main
orchestrator's pane through the `hscc orch inject` path with --mode auto
(queued when the pane is busy; skipped and logged when it is missing).

  list                       Show schedule, enabled and last fire
  set <name> <schedule>      Change one schedule
  enable|disable <name>      Turn one wake-up on or off

Schedules (local time for daily/weekly):
  every 2h           interval from the last fire
  daily 07:00        every day at 07:00
  weekly Monday 07:30  every Monday at 07:30

Names: health, sweep, memory, settings.
--json prints the machine-readable receipt (schedule, enabled, last_fired,
next_due, last_result and prompt per wake-up).
""",
}


class MainPaneError(Exception):
    """A pane operation could not be completed (reported, never a traceback)."""


# --------------------------------------------------------------------------- #
# Identity + module seams
# --------------------------------------------------------------------------- #

def _load_main_orch():
    """Import ``hscc-roles/main_orch.py`` (the one identity resolver)."""
    roles_dir = Path(__file__).resolve().parent.parent / "hscc-roles"
    if str(roles_dir) not in sys.path:
        sys.path.insert(0, str(roles_dir))
    import main_orch  # noqa: PLC0415 — sibling plugin, path added above
    return main_orch


def load_repl_inject():
    """Import ``repl_inject`` from the sibling hscc-api plugin dir, or None.

    Same loader shape as ``orch_waker.load_repl_inject`` / ``kanban_pause``:
    the module may already be importable in-process, otherwise hscc-api sits
    beside hscc_daemon in the repo AND the deployed plugins layout.
    """
    try:
        import repl_inject  # noqa: F401  (already importable)
        return repl_inject
    except ImportError:
        pass
    api_dir = Path(__file__).resolve().parent.parent / "hscc-api"
    if not api_dir.is_dir():
        return None
    if str(api_dir) not in sys.path:
        sys.path.insert(0, str(api_dir))
    try:
        import repl_inject  # noqa: F401
        return repl_inject
    except ImportError:
        return None


def load_orch_subscribe():
    """Import ``hscc-roles/orch_subscribe.py`` (the subscription policy), or None.

    Same sibling-plugin loader shape as ``load_repl_inject``: the module may
    already be importable in-process (repo/plugin layout), otherwise hscc-roles
    sits beside hscc_daemon in both layouts. Returns None only when the plugin
    is genuinely absent; the caller reports and continues.
    """
    try:
        import orch_subscribe  # noqa: F401  (already importable)
        return orch_subscribe
    except ImportError:
        pass
    roles_dir = Path(__file__).resolve().parent.parent / "hscc-roles"
    if not roles_dir.is_dir():
        return None
    if str(roles_dir) not in sys.path:
        sys.path.insert(0, str(roles_dir))
    try:
        import orch_subscribe  # noqa: F401
    except ImportError:
        return None
    return orch_subscribe


def _subscription_policy(profile, *, pane_backed=True):
    """Apply the t_31c8b258 panes-wake-this-orch policy; never raise."""
    policy = load_orch_subscribe()
    if policy is None:
        return {"action": "unavailable", "changed": False,
                "profile": profile}
    try:
        return policy.apply_subscription_policy(profile, pane_backed=pane_backed)
    except Exception as exc:  # noqa: BLE001 — policy is best-effort
        return {"action": "error", "changed": False, "profile": profile,
                "error": str(exc)}


def _hermes_root() -> str:
    """The Hermes install root from ``HERMES_HOME`` (profile-scoped aware)."""
    home = (os.environ.get("HERMES_HOME")
            or os.path.join(os.path.expanduser("~"), ".hermes"))
    home = os.path.expanduser(home)
    if os.path.basename(os.path.dirname(home)) == "profiles":
        return os.path.dirname(os.path.dirname(home))
    return home


def find_hermes_bin(explicit=None):
    """Locate the ``hermes`` CLI: explicit arg, ``HERMES_BIN``, venv, PATH."""
    root = _hermes_root()
    candidates = [
        explicit,
        os.environ.get("HERMES_BIN"),
        os.path.join(root, "hermes-agent", "venv", "bin", "hermes"),
        os.path.join(root, "bin", "hermes"),
        shutil.which("hermes"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _under_pytest() -> bool:
    """This package's in-suite predicate — asked of :mod:`hscc_daemon.suite_fence`.

    Not a copy of it. The guard below is about tmux, and the tmux module it guards
    against (``hscc-api/repl_inject.py``) carries its own stdlib-only copy because the
    two plugins share no namespace; a THIRD copy inside this package would be free to
    drift from the one its own package ruled (t_fdbc24f9: one predicate per package,
    every duplication pinned to agree). The marker-only version that lived here left
    every phase OUTSIDE an item unguarded — ``PYTEST_CURRENT_TEST`` is unset while a
    module is being imported (measured: unset at module import, set from session-fixture
    setup through item teardown), so a live tmux exec reached from collection, a plugin
    hook or an atexit path answered "no suite" and exec'd.
    """
    return suite_fence.under_pytest()


def _socket_args(repl):
    """The ``-S <socket>`` prefix from repl_inject, or ``()`` when unavailable.

    THE resolver is ``repl_inject.tmux_socket_args()`` (t_4c5855a6) — the same
    one ``hscc orch inject`` uses. It is never re-derived here: a second copy
    of the discovery rules is exactly the drift this seam exists to prevent.
    A fake repl injected by a test need not carry the resolver; a real
    hscc-api module always does. Its two-server refusal (kind
    ``ambiguous_server``) propagates so :func:`start` can name it.
    """
    resolver = getattr(repl, "tmux_socket_args", None)
    return tuple(resolver()) if callable(resolver) else ()


def run_tmux(argv, *, socket_args=None, repl=None):
    """Run one tmux command; return ``(returncode, stdout, stderr)``.

    Every argv is aimed at the server repl_inject resolved: a fresh shell has
    no ``TMUX_TMPDIR``, and without the ``-S`` prefix tmux would create a
    SECOND detached session on a parallel server while the operator watches
    their own server (t_4b3e3806). ``socket_args`` lets :func:`start` pass the
    one resolution it made; a direct caller gets the same resolver here.

    Hermeticity guard: under pytest the ORIGINAL seam raises instead of
    executing, so a test that forgets to inject a runner cannot touch the
    operator's real tmux server. Pointing ``repl_inject.TMUX_BIN`` at a fake
    (as the socket tests do) is ownership, so that path may exec.
    """
    repl = repl if repl is not None else load_repl_inject()
    binary = "tmux"
    if repl is not None:
        binary = getattr(repl, "TMUX_BIN", None) or binary
    if socket_args is None:
        socket_args = _socket_args(repl)
    if _under_pytest() and run_tmux is _ORIG_RUN_TMUX and binary == "tmux":
        raise MainPaneError(
            "real tmux execution is disabled under pytest "
            "(hermeticity guard) — inject a runner")
    cp = subprocess.run([binary, *socket_args, *[str(a) for a in argv]],
                        capture_output=True, text=True)
    return cp.returncode, cp.stdout or "", cp.stderr or ""


_ORIG_RUN_TMUX = run_tmux

_SESSION_FORMAT = "#{session_name}\t#{session_attached}\t#{session_activity}"


# --------------------------------------------------------------------------- #
# Discovery + tmux session choice
# --------------------------------------------------------------------------- #

def _profile_panes(repl, profile):
    """Panes running ``profile``'s Hermes REPL, via repl_inject's discovery."""
    if repl is None:
        raise MainPaneError(
            "repl_inject not importable (hscc-api plugin missing) — cannot "
            "resolve the main orchestrator's pane")
    try:
        panes = repl.hermes_panes()
    except Exception as exc:  # noqa: BLE001 — report, never crash the CLI
        raise MainPaneError(f"cannot discover Hermes panes: {exc}") from exc
    return [p for p in panes if p.get("profile") == profile]


def _sessions(runner):
    """Existing tmux sessions as dicts, or ``[]`` when no server is running."""
    rc, out, err = runner(["list-sessions", "-F", _SESSION_FORMAT])
    if rc != 0:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0]:
            continue
        try:
            activity = int(parts[2])
        except ValueError:
            activity = 0
        rows.append({"name": parts[0], "attached": parts[1] == "1",
                     "activity": activity})
    return rows


def _choose_session(rows, runner):
    """The operator's tmux session: current if inside tmux, else newest.

    Prefers an attached session over a detached one, then the most recently
    active. Deterministic for tests and for an operator with several sessions.
    """
    if os.environ.get("TMUX"):
        rc, out, _err = runner(["display-message", "-p", "#S"])
        if rc == 0 and out.strip():
            return out.strip()
    attached = [r for r in rows if r["attached"]]
    pool = attached or rows
    pool = sorted(pool, key=lambda r: (r["activity"], r["name"]), reverse=True)
    return pool[0]["name"]


def _receipt(name, profile, board, **extra):
    return {"name": name, "profile": profile, "board": board, **extra}


# --------------------------------------------------------------------------- #
# start
# --------------------------------------------------------------------------- #

def start(*, profile=None, hscc_dir=None, runner=None, repl=None,
          hermes_bin=None):
    """Ensure exactly one tmux pane runs the main orchestrator's REPL.

    Returns ``{ok, action, name, profile, board, pane, target, session,
    command, detail, subscription}``. ``subscription`` is the
    t_31c8b258 policy receipt (a pane start turns Hermes' gateway
    auto-subscribe gate OFF for this profile). Never raises for an expected
    refusal (ambiguous panes, missing Hermes/tmux, a failed tmux command) —
    the caller prints the detail and exits non-zero.
    """
    try:
        main_orch = _load_main_orch()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "action": "resolver-error", "detail": str(exc)}
    name = main_orch.main_name(hscc_dir)
    profile = profile or main_orch.main_profile(hscc_dir)
    board = main_orch.main_board(hscc_dir)
    # Fail closed before launching a REPL that would see a foreign board: the
    # same resolved-root rule bootstrap's main_board.ensure enforces. Starting
    # the pane is the other path that shares find_hermes_bin, and an orchestrator
    # whose board lives outside its own home is exactly the wrong-target state
    # this guard exists to make impossible.
    refusal = kanban_root_refusal(_hermes_root())
    if refusal:
        return {**_receipt(name, profile, board), "ok": False,
                "action": "kanban-root-refused", "pane": None, "target": None,
                "session": None, "command": None, "detail": refusal}
    repl = repl if repl is not None else load_repl_inject()
    try:
        socket_args = _socket_args(repl)
    except Exception as exc:  # noqa: BLE001 — report the refusal, never crash
        kind = getattr(exc, "kind", None)
        return {**_receipt(name, profile, board), "ok": False,
                "action": ("ambiguous-server"
                           if kind == "ambiguous_server" else "tmux-error"),
                "pane": None, "target": None, "session": None,
                "command": None, "detail": str(exc)}

    try:
        panes = _profile_panes(repl, profile)
    except MainPaneError as exc:
        return {**_receipt(name, profile, board), "ok": False,
                "action": "discovery-failed", "detail": str(exc)}
    if len(panes) == 1:
        pane = panes[0]
        return {**_receipt(name, profile, board), "ok": True,
                "action": "reused", "pane": pane.get("pane"),
                "target": pane.get("target"), "session": None,
                "command": None, "detail": "already running",
                "subscription": _subscription_policy(profile)}
    if len(panes) > 1:
        where = ", ".join(f"{p.get('pane')}@{p.get('target')}" for p in panes)
        return {**_receipt(name, profile, board), "ok": False,
                "action": "ambiguous", "pane": None, "target": None,
                "session": None, "command": None,
                "detail": (f"{len(panes)} tmux panes run profile {profile!r} "
                           f"({where}); close the extras — refusing to pick "
                           "one or kill another pane")}

    binary = find_hermes_bin(hermes_bin)
    if not binary:
        return {**_receipt(name, profile, board), "ok": False,
                "action": "missing-hermes", "pane": None, "target": None,
                "session": None, "command": None,
                "detail": ("hermes CLI not found (set HERMES_BIN or install "
                           "the Hermes venv)")}

    # `chat` is what repl_inject recognises as a REPL; a bare `hermes -p X`
    # would run, but `hscc orch inject` could never resolve it.
    command = [binary, "-p", profile, "chat"]
    run = runner or (lambda argv: run_tmux(argv, socket_args=socket_args,
                                            repl=repl))
    try:
        sessions = _sessions(run)
        if sessions:
            session = _choose_session(sessions, run)
            # The trailing colon matters: `=NAME` alone is tmux's *window*
            # target, so a session named `0` resolves to window 0 and
            # `new-window` fails with "index 0 in use". `=NAME:` pins the
            # session and leaves the index to tmux.
            rc, out, err = run(
                ["new-window", "-d", "-t", f"={session}:", "-n", profile,
                 "-P", "-F", "#{pane_id}", "--", *command])
            action = "created-window"
        else:
            session = profile
            rc, out, err = run(
                ["new-session", "-d", "-s", profile, "-n", profile,
                 "-P", "-F", "#{pane_id}", "--", *command])
            action = "created-session"
    except (OSError, MainPaneError) as exc:
        return {**_receipt(name, profile, board), "ok": False,
                "action": "tmux-error", "pane": None, "target": None,
                "session": None, "command": command, "detail": str(exc)}
    if rc != 0:
        return {**_receipt(name, profile, board), "ok": False,
                "action": "create-failed", "pane": None, "target": None,
                "session": session, "command": command,
                "detail": (err or out or
                           f"tmux exited {rc}")}
    return {**_receipt(name, profile, board), "ok": True, "action": action,
            "pane": out.strip() or None, "target": None, "session": session,
            "command": command, "detail": "started",
            "subscription": _subscription_policy(profile)}


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #

def status(*, profile=None, hscc_dir=None, repl=None):
    """Read-only report: name, board, pane and busy/idle.

    ``state`` is ``busy`` / ``idle`` for exactly one pane, ``not-running``
    when none exists, ``ambiguous`` when several do (never guessed between),
    and ``unknown`` when the pane exists but its screen could not be read.
    """
    try:
        main_orch = _load_main_orch()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "state": "error", "detail": str(exc)}
    name = main_orch.main_name(hscc_dir)
    profile = profile or main_orch.main_profile(hscc_dir)
    board = main_orch.main_board(hscc_dir)
    repl = repl if repl is not None else load_repl_inject()

    try:
        panes = _profile_panes(repl, profile)
    except MainPaneError as exc:
        return {**_receipt(name, profile, board), "ok": False,
                "state": "unknown", "running": False, "busy": None,
                "pane": None, "target": None, "panes": [],
                "detail": str(exc)}

    if not panes:
        return {**_receipt(name, profile, board), "ok": True,
                "state": "not-running", "running": False, "busy": None,
                "pane": None, "target": None, "panes": [],
                "detail": "no pane"}
    if len(panes) > 1:
        return {**_receipt(name, profile, board), "ok": True,
                "state": "ambiguous", "running": False, "busy": None,
                "pane": None, "target": None,
                "panes": [p.get("target") for p in panes],
                "detail": f"{len(panes)} panes run profile {profile!r}"}

    pane = panes[0]
    try:
        busy = bool(repl.is_busy(pane.get("pane")))
        state = "busy" if busy else "idle"
        detail = "busy" if busy else "idle"
    except Exception as exc:  # noqa: BLE001 — honest unknown, never a guess
        busy = None
        state = "unknown"
        detail = f"cannot read pane state: {exc}"
    return {**_receipt(name, profile, board), "ok": True, "state": state,
            "running": True, "busy": busy, "pane": pane.get("pane"),
            "target": pane.get("target"), "panes": [pane.get("target")],
            "detail": detail}


# --------------------------------------------------------------------------- #
# Human rendering + CLI dispatch
# --------------------------------------------------------------------------- #

def _start_lines(result):
    if result.get("ok") and result.get("action") == "reused":
        return [f"main orchestrator: {result['name']} already running in "
                f"pane {result.get('pane')} ({result.get('target')})"]
    if result.get("ok"):
        where = (f"tmux window {result['profile']} in session "
                 f"{result.get('session')}" if result.get("action") ==
                 "created-window" else
                 f"tmux session {result.get('session')}")
        return [f"main orchestrator: {result['name']} started in {where} "
                f"(pane {result.get('pane')})"]
    lines = [f"error: {result.get('detail') or 'could not start the main orchestrator'}"]
    if result.get("action") == "missing-hermes":
        lines.append("  fix: install the Hermes venv or set HERMES_BIN")
    elif result.get("action") == "kanban-root-refused":
        lines.append("  fix: unset HERMES_KANBAN_HOME, or point HERMES_HOME "
                     "at that root")
    elif result.get("action") == "ambiguous-server":
        lines.append("  fix: set TMUX_TMPDIR to the tmux server that holds "
                     "your panes, then retry")
    elif result.get("action") in ("missing-repl", "discovery-failed"):
        lines.append("  fix: deploy the hscc-api plugin beside hscc_daemon")
    elif result.get("action") in ("create-failed", "tmux-error"):
        lines.append("  fix: check that tmux can start (tmux list-sessions)")
    return lines


def _status_lines(result):
    lines = [f"main orchestrator: {result.get('name')}",
             f"  board: {result.get('board')}"]
    state = result.get("state")
    if state in ("busy", "idle", "unknown"):
        lines.append(f"  pane: {result.get('pane')} "
                     f"({result.get('target')})")
        lines.append(f"  state: {state}")
    elif state == "ambiguous":
        where = ", ".join(str(t) for t in result.get("panes") or [])
        lines.append("  pane: ambiguous")
        lines.append(f"  state: {len(result.get('panes') or [])} panes run "
                     f"the profile ({where}); close the extras")
    else:
        lines.append("  pane: none")
        lines.append("  state: not-running")
        lines.append("  start it: hscc main start")
    if not result.get("ok") and result.get("detail"):
        lines.append(f"  detail: {result['detail']}")
    return lines


def _fmt_ts(ts):
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return "never"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def _wakeup_lines(receipt):
    """Human view of ``hscc main wakeups list`` (one line per wake-up)."""
    lines = [f"main wakeups ({receipt.get('profile')}):"]
    for item in receipt.get("wakeups") or []:
        state = "enabled " if item.get("enabled") else "disabled"
        line = (f"  {item.get('name'):<8} {state}  "
                f"{item.get('schedule'):<20}")
        result = item.get("last_result") or {}
        if item.get("last_fired") is None:
            line += " never fired"
        else:
            line += f" last {_fmt_ts(item.get('last_fired'))}"
            detail = result.get("detail") or result.get("skipped")
            if detail:
                line += f" ({detail})"
            if item.get("next_due") is not None:
                line += f"  next {_fmt_ts(item.get('next_due'))}"
        lines.append(line)
    return lines


def _wakeup_edit_line(receipt):
    action = receipt.get("action")
    name = receipt.get("name")
    if action == "set":
        return (f"main wakeups: {name} schedule {receipt.get('schedule')} "
                f"(was {receipt.get('previous')})")
    return (f"main wakeups: {name} "
            f"{'enabled' if receipt.get('enabled') else 'disabled'}")


def cmd_wakeups(argv):
    """Dispatch ``hscc main wakeups <list|set|enable|disable>``."""
    raw = list(argv)
    if not raw or raw[0] in SUB_HELP:
        print(SUBCOMMAND_HELP["wakeups"])
        return 0

    json_mode = "--json" in raw
    args = [a for a in raw if a != "--json"]
    if not args or args[0] in SUB_HELP:
        print(SUBCOMMAND_HELP["wakeups"])
        return 0
    sub, rest = args[0], args[1:]
    try:
        if sub == "list":
            if rest:
                print(f"error: unknown argument for 'main wakeups list': "
                      f"{rest[0]}")
                return 1
            receipt = main_wakeups.list_receipt()
            if json_mode:
                import json
                print(json.dumps(receipt, indent=2, default=str))
            else:
                print("\n".join(_wakeup_lines(receipt)))
            return 0
        if sub == "set":
            if len(rest) != 2:
                print("usage: hscc main wakeups set <name> <schedule>")
                return 1
            receipt = main_wakeups.set_schedule(rest[0], rest[1])
        elif sub in ("enable", "disable"):
            if len(rest) != 1:
                print(f"usage: hscc main wakeups {sub} <name>")
                return 1
            receipt = main_wakeups.set_enabled(rest[0], sub == "enable")
        else:
            print(f"Error: unknown main wakeups subcommand: {sub}")
            print("Valid subcommands: list, set, enable, disable")
            return 1
    except (ValueError, OSError) as exc:
        print(f"error: {exc}")
        return 1

    if json_mode:
        import json
        updated = main_wakeups.list_receipt()
        print(json.dumps({**receipt, "wakeups": updated["wakeups"],
                          "profile": updated["profile"]}, indent=2,
                         default=str))
    else:
        print(_wakeup_edit_line(receipt))
    return 0


def cmd_main(argv):
    """Dispatch ``hscc main <subcommand>``; returns an exit code."""
    argv = list(argv)
    if not argv or argv[0] in SUB_HELP:
        print(HELP_TEXT)
        return 0

    raw = list(argv)
    if len(raw) > 1 and any(a in SUB_HELP for a in raw[1:]):
        print(SUBCOMMAND_HELP.get(raw[0], HELP_TEXT))
        return 0

    json_mode = "--json" in raw
    args = [a for a in raw if a != "--json"]
    sub = args[0]
    rest = args[1:]

    if sub == "start":
        if rest:
            print(f"error: unknown argument for 'main start': {rest[0]}")
            return 1
        result = start()
        if json_mode:
            import json
            print(json.dumps(result, indent=2, default=str))
        else:
            print("\n".join(_start_lines(result)))
        return 0 if result.get("ok") else 1
    if sub == "status":
        if rest:
            print(f"error: unknown argument for 'main status': {rest[0]}")
            return 1
        result = status()
        if json_mode:
            import json
            print(json.dumps(result, indent=2, default=str))
        else:
            print("\n".join(_status_lines(result)))
        return 0 if result.get("ok") else 1
    if sub == "wakeups":
        # Pass the raw tail: cmd_wakeups owns its own --json parsing.
        return cmd_wakeups(raw[1:])

    print(f"Error: unknown main subcommand: {sub}")
    print(f"Valid subcommands: {', '.join(VALID_SUBCOMMANDS)}")
    return 1
