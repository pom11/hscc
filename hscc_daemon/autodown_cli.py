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
                                               serving is down it does NOT start it. Aborts non-zero if an
                                               ACTIVE model-requiring Hermes cron job exists (one that
                                               runs an agent and needs the GPU serving layer). CPU-only
                                               watchdogs (no_agent:true, model:null) do NOT block — they
                                               are noted.
  hscc autodown enable --force               Arm even when active model-requiring Hermes cron jobs
                                               exist (autodown may power the cluster down when they are
                                               due). Overridden jobs are recorded and shown in 'status'.
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


def _has_force(argv):
    """True when ``--force`` is present in the (subcommand) argv."""
    return "--force" in argv


def _cmd_status(rest, json_mode):
    """``hscc autodown status`` — read-only report (§7).

    Reads autodown.json (absent ⇒ disabled default; load_config does NOT create
    the file) plus the watchdog block for context, and reports the state. Never
    writes, never creates, never acts.
    """
    cfg = autodown.load_config()
    from hscc_daemon import lifecycle
    block = lifecycle.load_watchdog_block()

    # Which signal is CURRENTLY blocking teardown. This actively evaluates the
    # (read-only) kanban idle predicate in this process so status always shows
    # the truth rather than a bare ``state: up`` that hides whether autodown is
    # healthy-and-waiting or stuck on an interlock. Never mutates anything.
    blocking = None
    if autodown._has_active_work():
        blocking_board = autodown.kanban_blocking_board()
        if blocking_board and blocking_board != autodown._UNREADABLE_BOARD:
            # Name the SPECIFIC blocking task(s), not just the board, so the
            # operator can act without running a second command. Reuses the
            # same board enumeration the predicate just did (list_stale_tasks
            # → _enum_board_names) so status names exactly the work that blocks.
            tasks = autodown.list_stale_tasks(
                board=blocking_board, older_than=None)["tasks"]
            if tasks:
                names = ", ".join(
                    f"{t['id']} ({t['title']})" for t in tasks[:3])
                if len(tasks) > 3:
                    names += f", … and {len(tasks) - 3} more"
                blocking = f"kanban work on board '{blocking_board}': {names}"
            else:
                # Predicate said active but no task could be enumerated (e.g.
                # the interlock is held by an unreadable board). Name the board.
                blocking = f"kanban work on board '{blocking_board}'"
        else:
            blocking = "kanban work (board unknown)"

    # Kanban interlock resolution — read AFTER the predicate above, which
    # resolves the lib, so ok/reason reflect the live evaluation and let an
    # operator see when the interlock is unevaluable (and why) instead of
    # guessing why autodown never fires.
    kc = autodown.kanban_check_state()

    # Informational: active Hermes cron jobs, classified (feat t_c94f8b8c).
    # status is read-only — we only READ jobs.json (Hermes' source of truth),
    # never write it. cpu_only watchdogs are noted so the operator still knows
    # they exist, even though they never block arming; model-requiring ones are
    # surfaced because they are the only reason enable would (or, force-armed,
    # did) abort.
    active_crons = autodown.list_active_cron_jobs()
    if isinstance(active_crons, list):
        cpu_only_crons = [j for j in active_crons if j.get("cpu_only")]
        model_crons = [j for j in active_crons if not j.get("cpu_only")]
    else:
        cpu_only_crons, model_crons = [], []

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
        "kanban_ok": kc["ok"] if kc else None,
        "kanban_reason": kc["reason"] if kc else "",
        "blocked_by": blocking,
        # Force-armed without --force: set when the operator armed despite
        # active Hermes cron jobs. Surfaced so the operator can always see WHY
        # autodown is armed despite scheduled jobs (§7, feat t_2b711a94).
        "force_armed": bool(cfg.get("force_armed")),
        "force_armed_overrides": cfg.get("force_armed_overrides") or [],
        # Active cron jobs, classified (feat t_c94f8b8c): cpu_only watchdogs
        # never block arming; model-requiring jobs are the abort-able ones.
        "active_cron_cpu_only": [j.get("name") or j.get("id")
                                 for j in cpu_only_crons],
        "active_cron_model": [j.get("name") or j.get("id")
                              for j in model_crons],
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
    if status["force_armed"]:
        print("  force-armed:     YES (armed despite active Hermes cron jobs)")
        for job in status["force_armed_overrides"]:
            print(f"    overridden job: {job}")
    # Informational note for any active CPU-only watchdog(s) — they do not
    # block arming (and autodown never wakes for them), but the operator
    # should still know they exist (feat t_c94f8b8c).
    if status["active_cron_cpu_only"]:
        names = ", ".join(status["active_cron_cpu_only"])
        print(f"  active cron (cpu-only): {names} — CPU-side watchdog(s), "
              "not GPU-dependent, do not block arming.")
    if status["active_cron_model"]:
        names = ", ".join(status["active_cron_model"])
        print(f"  active cron (model):    {names} — model-requiring, "
              "the reason enable aborts/force-arms.")
    print(f"  state:           {status['state']}")
    if status["blocked_by"]:
        print(f"  blocked by:      {status['blocked_by']}")
    print(f"  last activity:   {status['last_activity_iso']}")
    if status["down_since"]:
        # Only show the down-since line when there is a value — a null/absent
        # down_since means the fleet is not down (never printed 'None').
        print(f"  down since:      {status['down_since']}")
    if status["wake_source"]:
        print(f"  wake source:     {status['wake_source']}")
    if status["reason"]:
        print(f"  reason:          {status['reason']}")
    print(f"  watchdog block:  {'set' if status['watchdog_blocked'] else 'clear'}"
          f"{' (intentional: ' + str(status['watchdog_intentional']) + ')' if status['watchdog_intentional'] else ''}")
    if kc is not None and kc["ok"] is False:
        print(f"  kanban interlock: UNEVALUABLE — {kc['reason']}")
    elif kc is not None and kc["ok"]:
        print("  kanban interlock: ok (board readable)")
    return 0


def _cmd_enable(rest, json_mode):
    """``hscc autodown enable [--idle-minutes N] [--force]`` — arm it (§7).

    Persists ``enabled: true`` (+ ``idle_minutes``) to autodown.json AND resets
    ``last_activity_iso = now`` (§1e first-window guard) so arming can never cause
    an immediate teardown. NON-ACTING: if the serving layer is currently down it
    does NOT start it — a separate ``wake`` (or an inbound event) brings it up.

    **Cron-guard (feat t_c94f8b8c, refined from t_2b711a94 §7):** before
    arming, enumerate ACTIVE Hermes scheduled jobs via
    ``autodown.list_active_cron_jobs`` (Hermes' on-disk source of truth,
    ``~/.hermes/cron/jobs.json``). The guard only aborts for jobs that ACTUALLY
    need the serving layer:

    - a **model-requiring** active job (one with a ``model``, or ``no_agent``
      false/absent — i.e. it runs an agent) ⇒ ABORT: prints the job (name,
      schedule, next run), explains that autodown may power the cluster down
      when it is due, and exits non-zero.
    - a **CPU-only** active job (``no_agent: true`` AND ``model: null`` — a
      script watchdog that never touches the GPU) ⇒ does NOT abort. It is
      mentioned as an informational note so the operator still knows it exists.
    - a job whose nature we **cannot determine** (missing/ambiguous fields) is
      treated as model-requiring and ABORTS — never arm on an unverifiable
      signal.
    - an unreadable/absent jobs.json is also "cannot determine" and aborts.

    Only ACTIVE jobs count; paused/disabled jobs never conflict.

    ``--force`` overrides the guard: it arms anyway, prints the model-requiring
    jobs that were overridden, and records ``force_armed: true`` + the
    overridden job names in autodown.json so ``hscc autodown status`` can
    always show why autodown is armed despite scheduled model-requiring jobs.
    """
    idle_minutes, err = _parse_idle_minutes(rest)
    if err:
        return _error(err, json_mode)
    if idle_minutes is None:
        idle_minutes = DEFAULT_IDLE_MINUTES

    force = _has_force(rest)

    # Cron-guard: read Hermes' scheduled jobs. Fail-closed — an unreadable/
    # absent jobs.json is "cannot determine" and blocks arming unless forced.
    active_jobs = autodown.list_active_cron_jobs()
    unreadable = active_jobs is autodown.CRON_UNREADABLE

    # Split ACTIVE jobs by whether they need the serving layer (feat
    # t_c94f8b8c). cpu_only (no_agent:true AND model:null) watchdogs never
    # touch the GPU and do NOT block arming — they are informational notes.
    # Every other active job (a real model, no_agent false/absent, or fields
    # we cannot read) is model-requiring and DOES block — fail-safe.
    model_jobs = ([j for j in active_jobs if not j.get("cpu_only")]
                  if isinstance(active_jobs, list) else [])
    cpu_only_jobs = ([j for j in active_jobs if j.get("cpu_only")]
                     if isinstance(active_jobs, list) else [])

    if not force:
        if unreadable:
            return _error(
                "cannot determine Hermes cron jobs — "
                f"{autodown.CRON_JOBS_FILE} is unreadable or absent. Autodown "
                "does NOT arm on an unverifiable signal. Fix the cron config "
                "(or pass --force to override).", json_mode)
        if model_jobs:
            # Abort: do NOT enable. Print the MODEL-REQUIRING jobs + why, exit
            # non-zero (§7). CPU-only jobs are never the blocker — only jobs
            # that need the serving layer conflict with powering it down.
            names = ", ".join(
                (j.get("name") or j.get("id")) for j in model_jobs)
            if json_mode:
                print(json.dumps({
                    "error": (
                        "aborting: active model-requiring Hermes cron jobs "
                        "present — autodown may power the cluster down when "
                        "they are due"),
                    "active_cron_jobs": model_jobs, "exit": 1,
                }))
            else:
                print("autodown: NOT armed — aborting: active "
                      "model-requiring Hermes cron jobs exist and autodown "
                      "may power the cluster down when they are due.",
                      file=sys.stderr)
                for j in model_jobs:
                    print(f"  scheduled job:   {j.get('name') or j.get('id')}",
                          file=sys.stderr)
                    print(f"    schedule:      {j.get('schedule_display') or '?'}",
                          file=sys.stderr)
                    print(f"    next run:      {j.get('next_run_at') or '?'}",
                          file=sys.stderr)
                print(f"  ({len(model_jobs)} active model-requiring job(s): "
                      f"{names})", file=sys.stderr)
                print("  Fix: pause/disable the conflicting job(s), or "
                      "re-run with --force to arm anyway.", file=sys.stderr)
            return 1

    # record_activity stamps last_activity_iso=now (creates the file if absent,
    # still disabled) — §1d.4 CLI source + §1e first-window guard.
    autodown.record_activity("cli")
    cfg = autodown.load_config()
    cfg["enabled"] = True
    cfg["idle_minutes"] = idle_minutes
    if force and model_jobs:
        # Force-armed: record that we overrode the MODEL-REQUIRING cron jobs
        # so status can always explain why autodown is armed despite them (§7).
        # CPU-only jobs never appear here — they do not conflict, so there is
        # nothing to override.
        cfg["force_armed"] = True
        cfg["force_armed_overrides"] = [
            j.get("name") or j.get("id") for j in model_jobs]
    else:
        # Normal arm (no model-requiring cron conflict), or force with nothing
        # to override, or force with an undeterminable config — clear any prior
        # force markers so a later clean arm is not mislabeled.
        cfg["force_armed"] = False
        cfg["force_armed_overrides"] = []
    autodown.save_config(cfg)

    if json_mode:
        print(json.dumps({
            "enabled": True,
            "idle_minutes": idle_minutes,
            "last_activity_iso": cfg["last_activity_iso"],
            "force_armed": cfg["force_armed"],
            "force_armed_overrides": cfg["force_armed_overrides"],
        }))
    else:
        if force and model_jobs:
            # Report which MODEL-REQUIRING jobs were overridden (§7).
            print(f"autodown: ENABLED (idle_minutes={idle_minutes}, FORCED)")
            for j in model_jobs:
                print(f"  overrode schedule: {j.get('name') or j.get('id')}"
                      f" (schedule {j.get('schedule_display') or '?'})")
        else:
            print(f"autodown: ENABLED (idle_minutes={idle_minutes})")
        # Informational: active CPU-only watchdogs exist but did not block —
        # the operator should still know they are scheduled (feat t_c94f8b8c).
        if cpu_only_jobs:
            names = ", ".join(
                (j.get("name") or j.get("id")) for j in cpu_only_jobs)
            print(f"  Note: {len(cpu_only_jobs)} active CPU-only cron "
                  f"watchdog(s) present ({names}) — not GPU-dependent, "
                  "so they do not block arming.")
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
            print("  Watchdog block cleared — normal supervision resumed so "
                  "the watchdog can heal whatever is actually there.")
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
