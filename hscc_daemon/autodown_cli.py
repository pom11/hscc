"""HSCC `autodown` CLI verb — status / enable / disable / wake / cancel.

Phase 7 (§7). Thin CLI surface over ``hscc_daemon.autodown``: it reads/writes
``~/.hscc/autodown.json`` (via ``autodown.load_config/save_config``) and the
watchdog block (``~/.hscc/watchdog-block.json``, via ``lifecycle``), and calls
the acting verbs ``autoup()``/``teardown()``. It does NOT re-implement any
autodown logic — it is the operator-facing surface for the state the daemon
thread already manages.

Every MUTATING verb calls ``record_activity("cli")`` first — the Phase 6 CLI
activity source (§1d.4) — so any human CLI action advances the idle timer and,
if the layer was down, becomes a candidate wake trigger. ``status`` is
read-only and NEVER mutates anything.

Mirrors the ``api`` verb-group wiring: ``hscc.py:main()`` dispatches ``autodown``
→ ``cmd_autodown(args[1:])``, and unknown subcommands exit non-zero (same as
``api_cli.cmd_api``, api_cli.py:275-297).

Reference: docs/design/idle-autodown.md §7 (CLI surface), §3/§4/§5 (semantics).
"""

import json
import sys

from hscc_daemon import autodown

VALID_SUBCOMMANDS = ("status", "enable", "disable", "wake", "cancel")

# Default idle window (minutes) when `enable` is given no --idle-minutes (§7).
DEFAULT_IDLE_MINUTES = 10

HELP_TEXT = """\
HSCC idle autodown/autoup — bring the GPU serving layer down when the cluster is
idle, and back up automatically on the next inbound activity. Opt-in, OFF by default.

Usage: hscc autodown <subcommand> [args]

  hscc autodown status [--json]              Show enabled? state? idle_minutes? last activity? down_since?
  hscc autodown enable [--idle-minutes <n>]  Arm idle autodown (default 10). Resets the idle timer
                                               so arming never immediately tears down. Non-acting: if
                                               serving is down it does NOT start it.
  hscc autodown disable                      Disarm autodown and clear the intentional watchdog block so
                                               ordinary supervision resumes. Does NOT restart serving — if
                                               you want it up, run 'hscc autodown wake' or bring it up another way.
  hscc autodown wake                         Force autoup now (also resets the idle timer)
  hscc autodown cancel                       Abort an in-progress teardown
  hscc autodown --help                       This help

'status' is read-only and never mutates anything. Pass --json for machine-readable
output (scripting)."""


def _parse_idle_minutes(rest):
    """Pull ``--idle-minutes N`` out of ``rest``, validating it.

    Returns ``(idle_minutes, error)``. ``idle_minutes`` is ``None`` if the
    flag is absent. ``error`` is a human-readable message when the value is
    missing, non-integer, or negative — it is NEVER silently coerced (§7). A
    valid value is ``>= 0`` (0 = only via explicit wake / never auto).
    """
    if "--idle-minutes" not in rest:
        return None, None
    i = rest.index("--idle-minutes")
    if i + 1 >= len(rest):
        return None, "--idle-minutes requires a value (a non-negative integer)"
    raw = rest[i + 1]
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return None, f"--idle-minutes must be a non-negative integer, got {raw!r}"
    if value < 0:
        return None, f"--idle-minutes must be a non-negative integer, got {raw!r}"
    return value, None


def _error(msg, json_mode):
    """Emit a clear error and return a non-zero exit code."""
    if json_mode:
        print(json.dumps({"error": msg, "exit": 1}))
    else:
        print(f"Error: {msg}", file=sys.stderr)
    return 1


def _cmd_status(rest, json_mode):
    """``hscc autodown status`` — read-only report (§7).

    Reads autodown.json (absent ⇒ disabled default; load_config does NOT create
    the file) plus the watchdog block for context, and reports the state. Never
    writes, never creates, never acts.
    """
    cfg = autodown.load_config()
    from hscc_daemon import lifecycle
    block = lifecycle.load_watchdog_block()

    status = {
        "enabled": bool(cfg.get("enabled")),
        "state": cfg.get("state"),
        "idle_minutes": cfg.get("idle_minutes"),
        "last_activity_iso": cfg.get("last_activity_iso"),
        "down_since": cfg.get("down_since"),
        "wake_source": cfg.get("wake_source"),
        "reason": cfg.get("reason"),
        "watchdog_blocked": bool(block.get("blocked")),
        "watchdog_intentional": block.get("intentional"),
    }
    if json_mode:
        print(json.dumps(status))
        return 0

    enabled = status["enabled"]
    if not enabled:
        line = "autodown: DISABLED"
        if status["last_activity_iso"] is None and status["state"] == "up":
            # Never enabled / never touched — the fresh-config case.
            line += " (never enabled)"
        print(line)
    else:
        print(f"autodown: ENABLED (idle_minutes={status['idle_minutes']})")
    print(f"  state:           {status['state']}")
    print(f"  last activity:   {status['last_activity_iso']}")
    print(f"  down since:      {status['down_since']}")
    if status["wake_source"]:
        print(f"  wake source:     {status['wake_source']}")
    if status["reason"]:
        print(f"  reason:          {status['reason']}")
    print(f"  watchdog block:  {'set' if status['watchdog_blocked'] else 'clear'}"
          f"{' (intentional: ' + str(status['watchdog_intentional']) + ')' if status['watchdog_intentional'] else ''}")
    return 0


def _cmd_enable(rest, json_mode):
    """``hscc autodown enable [--idle-minutes N]`` — arm it (§7).

    Persists ``enabled: true`` (+ ``idle_minutes``) to autodown.json AND resets
    ``last_activity_iso = now`` (§1e first-window guard) so arming can never cause
    an immediate teardown. NON-ACTING: if the serving layer is currently down it
    does NOT start it — a separate ``wake`` (or an inbound event) brings it up.
    """
    idle_minutes, err = _parse_idle_minutes(rest)
    if err:
        return _error(err, json_mode)
    if idle_minutes is None:
        idle_minutes = DEFAULT_IDLE_MINUTES

    # record_activity stamps last_activity_iso=now (creates the file if absent,
    # still disabled) — §1d.4 CLI source + §1e first-window guard.
    autodown.record_activity("cli")
    cfg = autodown.load_config()
    cfg["enabled"] = True
    cfg["idle_minutes"] = idle_minutes
    autodown.save_config(cfg)

    if json_mode:
        print(json.dumps({
            "enabled": True,
            "idle_minutes": idle_minutes,
            "last_activity_iso": cfg["last_activity_iso"],
        }))
    else:
        print(f"autodown: ENABLED (idle_minutes={idle_minutes})")
        print("  Idle timer reset — will not tear down for at least "
              f"{idle_minutes} minutes.")
        if cfg["state"] == "down":
            print("  Note: serving layer is down; enable does not start it. "
                  "Run 'hscc autodown wake' to bring it up.")
    return 0


def _cmd_disable(rest, json_mode):
    """``hscc autodown disable`` — disarm + release the intentional block (§7).

    Sets ``enabled: false`` in autodown.json and leaves ``state`` as the current
    recorded reality (the single source of truth for the serving layer — disable
    does not probe or change the layer, it is non-acting). Then clears the
    watchdog block: ``blocked: false`` and the ``intentional`` marker removed so
    ordinary supervision resumes (the watchdog can now heal the layer back up if
    it is down). It does NOT run autoup — the operator runs ``wake`` (or another
    path) to bring an intentionally-down layer back up.
    """
    # Mutating verb ⇒ record the CLI as an activity source (§1d.4).
    autodown.record_activity("cli")
    cfg = autodown.load_config()
    cfg["enabled"] = False
    # state stays as-is: it already reflects the current reality of the serving
    # layer, which disable does not touch (non-acting).
    autodown.save_config(cfg)

    # Clear the intentional marker + blocked flag so the watchdog resumes
    # ordinary supervision (C2/§5). Non-acting: does not touch serving itself.
    autodown._clear_intentional_block(reason="autodown disabled by operator")

    if json_mode:
        print(json.dumps({"enabled": False, "state": cfg["state"]}))
    else:
        print("autodown: DISABLED")
        print(f"  state: {cfg['state']}")
        print("  Intentional watchdog block cleared — normal supervision resumed.")
        print("  Serving layer NOT restarted (use 'hscc autodown wake' to bring it up).")
    return 0


def _cmd_wake(rest, json_mode):
    """``hscc autodown wake`` — force autoup now (§7).

    Records the CLI as an activity source (which advances the idle timer AND is
    the Phase 6 CLI wake trigger), then calls ``autoup()`` to bring the serving
    layer back up. Idempotent: autoup() no-ops if a wake is already in flight.
    """
    # record_activity("cli") first — both resets the idle timer and marks the
    # CLI as the wake trigger (§1d.4).
    cfg = autodown.record_activity("cli")
    result = autodown.autoup()
    res = result.get("result", "?")

    if json_mode:
        print(json.dumps({"result": res, "state": "waking",
                          "wake_source": "cli"}))
    else:
        if res == "up":
            print("autodown: serving layer is UP (wake complete)")
            print("  watchdog block cleared; supervision resumed.")
        elif res == "already-waking":
            print("autodown: a wake is already in flight (no-op)")
        elif res == "busy":
            print("autodown: another teardown/wake is in progress, or the "
                  "layer is already up — no starts issued.")
            return 1
        elif res == "no-units":
            print("autodown: wake did NOT complete — empty wake plan "
                  "(no serving units to start).")
            print("  Check ~/.hscc/serving.json.")
            return 1
        else:
            # start-failed / not-ready — autoup() has recorded the failure and
            # cleared the block so the watchdog can heal. Report it.
            msg = cfg.get("reason") or result.get("reason", res)
            print(f"autodown: wake did NOT complete ({res})")
            print(f"  {msg}")
            return 1
    return 0


def _cmd_cancel(rest, json_mode):
    """``hscc autodown cancel`` — abort an in-progress teardown (§6/§7).

    Sets ``cancel_requested: true`` in autodown.json; the teardown sequence
    re-checks this between stops and, if set, stops cleanly, rolls the block
    back, and reports. Harmless if no teardown is running (flag is simply set
    and consumed/cleared on the next teardown).
    """
    # Mutating verb ⇒ record the CLI as an activity source (§1d.4).
    autodown.record_activity("cli")
    cfg = autodown.load_config()
    cfg["cancel_requested"] = True
    autodown.save_config(cfg)

    if json_mode:
        print(json.dumps({"cancel_requested": True}))
    else:
        print("autodown: cancel requested — an in-progress teardown will abort "
              "between stops.")
    return 0


def cmd_autodown(argv):
    """Dispatch ``hscc autodown <subcommand>``; returns an exit code (never raises).

    With no subcommand (or ``--help``/``-h``) prints the group help and exits 0,
    matching how the other group verbs handle their no-subcommand/``--help``
    case (api_cli.cmd_api, api_cli.py:275-284). Unknown subcommands exit non-zero.

    ``--json`` (for scripting) is honored by every subcommand.
    """
    if not argv or argv[0] in ("--help", "-h"):
        print(HELP_TEXT)
        return 0

    json_mode = "--json" in argv
    # Strip --json so no subcommand has to re-filter it from its own rest.
    argv = [a for a in argv if a != "--json"]

    sub = argv[0]
    rest = argv[1:]

    if sub == "status":
        return _cmd_status(rest, json_mode)
    if sub == "enable":
        return _cmd_enable(rest, json_mode)
    if sub == "disable":
        return _cmd_disable(rest, json_mode)
    if sub == "wake":
        return _cmd_wake(rest, json_mode)
    if sub == "cancel":
        return _cmd_cancel(rest, json_mode)

    print(f"Error: unknown autodown subcommand: {sub}")
    print(f"Valid subcommands: {', '.join(VALID_SUBCOMMANDS)}")
    return 1
