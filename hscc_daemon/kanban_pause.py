"""`hscc kanban pause | resume | paused | enforce`: hold whole boards by priority.

Hermes kanban has no per-board pause (the only global stop, ``hermes pause``,
stops every board). Projects have priorities, so when the fleet is saturated
the operator parks the low-priority boards and the slots go to the others.

Built only from Hermes' own per-card verbs, so nothing here patches Hermes:

* **pause <board>** — every ``running`` card is reclaimed (its worker stops;
  the worktree stays) and then, like every ``ready`` card, BLOCKED with a
  ``PAUSED`` reason. ``todo`` cards cannot be blocked and need not be: they only
  promote once their parents finish, and those parents are now blocked.
  ``triage`` cards cannot be blocked either and the auto-decomposer would split
  them, so while any board is paused ``kanban.auto_decompose`` is held off
  (its previous value is recorded and restored).
* **resume <board>** — unblocks EXACTLY the cards that pause blocked (Hermes
  sends each to ``ready`` or back to ``todo`` per its parents). Cards that were
  blocked before the pause, by design, stay blocked.
* **orchestrators** — pause INTERRUPTS the board's orchestrator (`<board>-orch`)
  and steers it: cancel the running turn, do not unblock/create/dispatch until
  resumed. resume tells it the board is live again (queued if it is busy). Sent
  through ``repl_inject`` — the same module the API and ``hscc orch inject``
  use (types into the orchestrator's own tmux REPL; t_75f17c83 replaced the
  old shell-out to ``~/bin/hermes-inject``, which is retired together with the
  ``INJECT_BIN``/``HSCC_INJECT_BIN`` knobs). A missing module, pane or refused
  injection is reported in the ``orchestrator`` report key, never fatal.
* **crons** — the orchestrator's ACTIVE cron jobs (board watchers that run an
  agent on their own schedule) are paused with the board and resumed with it.
* **enforce** — blocks any card that became ``ready``/``running`` on a paused
  board after the pause (an orchestrator creating new cards). Idempotent; safe
  to run from a loop or the fleet check — and since GAP-F-4 (t_51931733) it
  IS a loop: the daemon's ``kanban_enforce`` stream runs it every tick (see
  :func:`enforce_tick`), because "the pause is a guarantee" only holds when
  something re-holds continuously.

State lives in ``~/.hscc/state/kanban_paused.json``. The hermes CLI runner, the
inject function and the state path are injectable so the tests never touch a
real board or a real tmux pane.

The enforce STREAM stamp is deliberately a DIFFERENT file
(``~/.hscc/state/kanban_enforce.json``, written through ``state.write_state``)
— see :func:`STREAM_NAME` for why stamping the paused-state file itself would
be the t_fa9a2faf collision class with a second, nastier twist.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Callable, Optional

STATE_PATH = os.path.expanduser(
    os.environ.get("HSCC_KANBAN_PAUSE_STATE", "~/.hscc/state/kanban_paused.json"))
HERMES_CONFIG = os.path.expanduser("~/.hermes/config.yaml")
REASON_PREFIX = "PAUSED"

PAUSE_STEER = (
    "PAUSE — the operator paused board `{board}` (project priority{why}). Cancel what you are "
    "doing now. Your running cards were reclaimed and every running/ready card is blocked with "
    "a PAUSED reason (state: ~/.hscc/state/kanban_paused.json). Until the operator resumes: do "
    "NOT unblock, create, promote, dispatch, reassign or merge anything on `{board}`, and do "
    "not 'fix' the PAUSED blocks. Answer the operator if asked; otherwise stay idle. Resume is "
    "`hscc kanban resume {board}` — not yours to run.")
RESUME_NOTE = (
    "RESUMED — board `{board}` is live again: the cards the pause held were released "
    "({n} card(s), each back to ready or todo per its parents). Re-read the board and continue "
    "from where you were; re-check anything time-sensitive before acting on it.")


def _load_repl_inject():
    """Import ``repl_inject`` from the sibling hscc-api plugin dir.

    The inject implementation lives in hscc-api (the API write path,
    ``hscc orch inject`` and, since t_75f17c83, this module all share it —
    one pane-discovery, one exactly-one-pane rule, one verb table). hscc-api
    is a SIBLING plugin dir, not a package of this one, so loading it follows
    the established pattern (routes_ws.py imports it bare once its dir is on
    sys.path; _load_daemon_serving in hscc-cluster/hscc.py does the mirror
    image for hscc_daemon): try the plain import, else insert the sibling dir.
    Returns the module, or None if hscc-api is not deployed next to us.
    """
    try:
        import repl_inject  # noqa: PLC0415 — already importable in-process
        return repl_inject
    except ImportError:
        pass
    api_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hscc-api")
    if os.path.isdir(api_dir) and api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    try:
        import repl_inject
        return repl_inject
    except ImportError:
        return None


def _inject(profile: str, message: str, mode: str) -> dict:
    """Steer the orchestrator's REPL through the repl_inject MODULE (no shell-out).

    Returns the same ``{ok, error}`` shape the report has always carried, plus
    ``mode``/``pane`` from the receipt so ops see what was actually delivered
    (t_75f17c83: previously this exec'd ``~/bin/hermes-inject``; the module and
    the CLI now share one implementation). A module we cannot load, a refused
    injection (no pane / ambiguous pane / bad mode) or a tmux failure is
    REPORTED, never raised — a pause must hold the board even when the
    orchestrator's pane is gone.
    """
    repl = _load_repl_inject()
    if repl is None:
        return {"ok": False, "error": "repl_inject not importable (hscc-api plugin missing)",
                "mode": mode, "pane": None}
    try:
        receipt = repl.inject(profile, message, mode=mode)
    except Exception as exc:  # InjectError or anything a live tmux raises
        return {"ok": False, "error": str(exc)[-200:] or repr(exc), "mode": mode, "pane": None}
    return {"ok": True, "error": None,
            "mode": receipt.get("mode"), "pane": receipt.get("target")}


def _run_cron(profile: str, args: list) -> subprocess.CompletedProcess:
    return subprocess.run(["hermes", "-p", profile, "cron", *args], capture_output=True,
                          text=True, timeout=60)


def active_crons(profile: str, cron: Callable = _run_cron) -> list:
    """Ids of the profile's ACTIVE cron jobs (parsed from `hermes cron list`)."""
    out = cron(profile, ["list"]).stdout or ""
    ids = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "[active]":
            ids.append(parts[0])
    return ids


def orchestrator_for(board: str) -> str:
    """Per-project orchestrator naming convention: board `<p>` -> `<p>-orch`."""
    return f"{board}-orch"
HOLD_STATUSES = ("running", "ready")
_DEFAULT = object()   # "use the module-level runner", resolved at call time


def _run_hermes(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(["hermes", "kanban", *args], capture_output=True, text=True,
                          timeout=60)


def load_state(path: Optional[str] = None) -> dict:
    try:
        with open(path or STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: Optional[str] = None) -> None:
    p = path or STATE_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, p)


def paused_boards(state: dict) -> list:
    return sorted(b for b in state if not b.startswith("_"))


def list_cards(board: str, run: Callable = _run_hermes) -> list:
    """``[{"id", "status"}]`` for every card on ``board`` (``[]`` on error)."""
    r = run(["--board", board, "list", "--json"])
    try:
        rows = json.loads(r.stdout or "[]")
    except ValueError:
        return []
    return [{"id": c.get("id"), "status": c.get("status")}
            for c in rows if isinstance(c, dict) and c.get("id")]


def _hold(board: str, card: dict, reason: str, run: Callable) -> bool:
    if card["status"] == "running":
        run(["--board", board, "reclaim", card["id"], "--reason", reason])
    r = run(["--board", board, "block", "--kind", "dependency", card["id"], reason])
    return r.returncode == 0


def _set_auto_decompose(value: bool, config_path: Optional[str] = None):
    """Flip ``kanban.auto_decompose`` in hermes config; return the old value.

    A plain line edit (the dispatcher re-reads the key every tick), so the rest
    of the file is left byte-for-byte as it is.
    """
    path = config_path or HERMES_CONFIG
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    old, in_kanban = None, False
    for i, line in enumerate(lines):
        if line.startswith("kanban:"):
            in_kanban = True
            continue
        if in_kanban and line and not line.startswith((" ", "\t")) and line.strip():
            break
        if in_kanban and line.strip().startswith("auto_decompose:"):
            old = line.split(":", 1)[1].strip().lower() == "true"
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = f"{indent}auto_decompose: {'true' if value else 'false'}\n"
            break
    if old is not None and old != value:
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
    return old


def pause(boards: list, reason: str = "", *, run: Callable = _run_hermes,
          state_path: Optional[str] = None, config_path: Optional[str] = None,
          now: Optional[int] = None, inject: Optional[Callable] = _inject,
          cron=_DEFAULT) -> dict:
    cron = _run_cron if cron is _DEFAULT else cron
    state = load_state(state_path)
    stamp = int(now if now is not None else time.time())
    text = f"{REASON_PREFIX} by operator" + (f": {reason}" if reason else "") + \
        " — resume with `hscc kanban resume <board>`"
    report = {}
    for board in boards:
        entry = state.get(board) or {"paused_at": stamp, "reason": reason, "cards": [],
                                     "triage_left": []}
        held = {c["id"] for c in entry["cards"]}
        added, failed, triage = [], [], []
        for card in list_cards(board, run):
            if card["status"] == "triage":
                triage.append(card["id"])
            if card["status"] not in HOLD_STATUSES or card["id"] in held:
                continue
            if _hold(board, card, text, run):
                entry["cards"].append({"id": card["id"], "was": card["status"]})
                added.append(card["id"])
            else:
                failed.append(card["id"])
        entry["triage_left"] = sorted(set(entry.get("triage_left") or []) | set(triage))
        state[board] = entry
        report[board] = {"held": added, "failed": failed, "triage": triage}
        if cron is not None:
            # An orchestrator's cron agents (board watchers, heartbeats) act on
            # the board on their own schedule — park them with the board.
            prof = orchestrator_for(board)
            known = {c["job"] for c in entry.get("crons_paused") or []}
            for job in active_crons(prof, cron):
                if job not in known and cron(prof, ["pause", job]).returncode == 0:
                    entry.setdefault("crons_paused", []).append({"profile": prof, "job": job})
            report[board]["crons_paused"] = [c["job"] for c in entry.get("crons_paused") or []]
        if inject is not None:
            report[board]["orchestrator"] = inject(
                orchestrator_for(board),
                PAUSE_STEER.format(board=board, why=f": {reason}" if reason else ""),
                # Canonical name of the no-verb send (the parity contract,
                # t_ff665ff9): while the orchestrator is busy this is exactly
                # what interrupts its turn — which a PAUSE must do.
                "plain")
    if paused_boards(state) and "_auto_decompose_was" not in state:
        state["_auto_decompose_was"] = _set_auto_decompose(False, config_path)
    save_state(state, state_path)
    return report


def resume(boards: list, *, run: Callable = _run_hermes,
           state_path: Optional[str] = None, config_path: Optional[str] = None,
           inject: Optional[Callable] = _inject,
           cron=_DEFAULT) -> dict:
    cron = _run_cron if cron is _DEFAULT else cron
    state = load_state(state_path)
    report = {}
    for board in boards:
        entry = state.pop(board, None)
        if entry is None:
            report[board] = {"error": "not paused"}
            continue
        released, failed = [], []
        for card in entry.get("cards") or []:
            r = run(["--board", board, "unblock", card["id"]])
            (released if r.returncode == 0 else failed).append(card["id"])
        report[board] = {"released": released, "failed": failed,
                         "triage_left": entry.get("triage_left") or []}
        if cron is not None:
            report[board]["crons_resumed"] = [
                c["job"] for c in entry.get("crons_paused") or []
                if cron(c["profile"], ["resume", c["job"]]).returncode == 0]
        if inject is not None:
            report[board]["orchestrator"] = inject(
                orchestrator_for(board), RESUME_NOTE.format(board=board, n=len(released)),
                "auto")
    if not paused_boards(state) and "_auto_decompose_was" in state:
        was = state.pop("_auto_decompose_was")
        if was:
            _set_auto_decompose(True, config_path)
    save_state(state, state_path)
    return report


def enforce(*, run: Callable = _run_hermes, state_path: Optional[str] = None) -> dict:
    """Hold cards that reached ready/running on a paused board after the pause.

    The verb behind two callers: ``hscc kanban enforce`` (the operator re-assert)
    and the daemon's ``kanban_enforce`` stream (:func:`enforce_tick`), which is
    what makes the hold continuous instead of remembered (GAP-F-4, t_51931733).
    """
    state = load_state(state_path)
    if not paused_boards(state):
        return {}
    report = {}
    for board in paused_boards(state):
        entry = state[board]
        held = {c["id"] for c in entry.get("cards") or []}
        text = f"{REASON_PREFIX} by operator (board paused, enforced) — resume with " \
               f"`hscc kanban resume {board}`"
        added = []
        for card in list_cards(board, run):
            # A recorded card that is ready/running again was released behind
            # the pause's back (an orchestrator turn already in flight): hold it
            # again, but record it only once.
            if card["status"] in HOLD_STATUSES:
                if _hold(board, card, text, run):
                    if card["id"] not in held:
                        entry["cards"].append({"id": card["id"], "was": card["status"]})
                    added.append(card["id"])
        if added:
            report[board] = added
    if report:
        save_state(state, state_path)
    return report


# ── GAP-F-4 (t_51931733): enforce as a supervised daemon stream ─────────────
#
# Audit F (t_549a21e0) found that `hscc kanban enforce` — the verb that closes
# the leak an in-flight orchestrator turn leaves behind a pause — had NO
# scheduler anywhere (its only call site was the CLI) and no doctor/verify
# coverage. This module's own contract said otherwise ("Idempotent; safe to run
# from a loop or the fleet check"), and pause's own comments admit the leak is
# live ("released behind the pause's back"). A hold that only runs when someone
# remembers to type it is a stopgap, not a feature — per the operator's
# integration rule, long-running policy enforcement belongs to the hscc daemon.
#
# The wiring follows the W3 precedent (orch-waker, PR #139): cadence declared
# in daemon_ops.PERIODIC_INTERVALS (the SOT verify's staleness math reads), the
# CHECKS table maps the stream to a check fn, the tick stamps its stream file
# on EVERY path (including the deliberately-idle one) so a feature doing
# nothing never reads as a stream that died, and the verdict logic lives HERE
# with the feature — doctor.py only adapts it to its five Check fields.

#: The daemon's stream name for the paused-board enforcer — the key in
#: PERIODIC_INTERVALS (daemon_ops), the state file
#: ``~/.hscc/state/kanban_enforce.json`` (state.py), the row ``hscc verify``
#: sees, and the field the doctor check reads. Deliberately NOT the paused
#: state file: kanban_paused.json is pure operator policy state whose board
#: keys are enumerated by :func:`paused_boards` (it only skips ``_``-prefixed
#: bookkeeping keys), so stamping ``ok``/``timestamp``/``last_enforce`` into it
#: would (a) make those keys look like paused BOARD NAMES — auto_decompose
#: would never be restored — and (b) hand one document to two full-document
#: writers (the CLI pause/resume and the daemon tick), which is exactly the
#: t_fa9a2faf collision shape, cross-process. Separate files, zero race.
STREAM_NAME = "kanban_enforce"

#: Fallback cadence if PERIODIC_INTERVALS cannot be read (defensive only; the
#: dict is a constant and the real cadence lives there, not here).
_STREAM_CADENCE_FALLBACK = 60

#: The doctor's freshness slack on top of two stream cycles, same shape as
#: orch_waker.DOCTOR_SLACK_S: the budget is 2*cadence + slack.
DOCTOR_SLACK_S = 90

#: The row's name when the module itself cannot be imported by doctor — same
#: trick as _ORCH_WAKER_CHECK_NAME so the row still prints.
CHECK_NAME = "kanban enforce (paused-board hold)"

#: Daemon pid file the doctor's liveness probe stats (same file the waker's
#: check reads; ``hscc_dir`` relocates the whole ~/.hscc root around it).
DAEMON_PID_PATH = os.path.expanduser("~/.hscc/daemon.pid")


def _real_time() -> float:
    return time.time()


def stream_cadence() -> int:
    """The daemon loop's cadence for this stream (the SOT, 60 s).

    Reads ``daemon_ops.PERIODIC_INTERVALS`` — the same source
    ``verify._live_stream_intervals`` reads, so cadence and the staleness math
    cannot drift. Imported inside the function: daemon_ops imports THIS module
    for its CHECKS table, and a module-level import in the other direction
    would only work by accident (orch_waker.stream_cadence does the same).
    """
    try:
        from .daemon_ops import PERIODIC_INTERVALS
        return int(PERIODIC_INTERVALS[STREAM_NAME])
    except Exception:  # pragma: no cover — defensive; the dict is a constant
        return _STREAM_CADENCE_FALLBACK


def enforce_tick(now: Optional[float] = None, *, state_path: Optional[str] = None,
                 run: Callable = _run_hermes, write: Optional[Callable] = None) -> dict:
    """One supervised daemon tick: run enforce(), stamp the stream either way.

    The body of :func:`check_kanban_enforce` below — what the
    ``kanban_enforce`` stream in ``daemon_ops.PERIODIC_INTERVALS`` runs every
    60 s. Like ``orch_waker.waker_tick`` there are three outcomes and ALL of
    them stamp the stream (a stream the daemon is deliberately idling must not
    read as a dead one to verify's staleness math):

      * nothing paused -> no board reads at all, ``idle`` (zero subprocess
        cost — enforce() early-returns before enumerating any board);
      * hold made      -> ``held`` naming the boards/cards it just re-blocked;
      * failure        -> ``ok: False`` — a raising enforce still stamps, so
        the failure is VISIBLE instead of looking like a stalled stream
        (GAP-C-1's rule: an inert-but-healthy-looking stream is the worst
        read a watcher can have).

    The stamp goes through ``state.write_state`` under ``STREAM_NAME`` — a
    file of its own, never kanban_paused.json (see STREAM_NAME for why).
    NEVER raises.
    """
    if now is None:
        now = _real_time()
    if write is None:
        from .state import write_state as write
    entry: dict = {"last_enforce": now}
    try:
        report = enforce(run=run, state_path=state_path)
        held = sum(len(v) for v in report.values())
        entry.update(ok=True,
                     paused=len(report), held=held,
                     message=(f"held {held} card(s) on {len(report)} paused board(s)"
                              if held else
                              (f"{len(report)} board(s) paused, nothing new to hold"
                               if report else "no boards paused")))
        if report:
            entry["boards"] = report
        return entry
    except Exception as e:  # noqa: BLE001 — the stream must survive anything
        entry.update(ok=False, message=f"enforce tick failed: {e}")
        return entry
    finally:
        # write_state adds its own ISO `timestamp` (what verify parses);
        # `last_enforce` is the numeric wall-clock this feature's doctor row
        # reads, mirroring orch_waker's `last_tick`.
        try:
            write(STREAM_NAME, entry)
        except Exception:  # pragma: no cover — write_state itself never raises
            pass


def check_kanban_enforce() -> bool:
    """The daemon's ``kanban_enforce`` stream check (PERIODIC_INTERVALS body).

    The CHECKS-table entry in ``daemon_ops.run_daemon_loop`` — so the enforcer
    rides the loop's standard per-stream machinery: its own supervised thread,
    error isolation from every other stream (a raising check is logged and the
    thread respawned, never taken down), and the same per-stream staleness
    math in ``hscc verify``. ``enforce_tick`` stamps the stream itself on
    every path; this wrapper only converts the entry to the check-fn truth
    convention (``ok``) and guarantees a raise cannot escape even if the
    tick's own guard did (defence in depth, matching check_orch_waker).
    """
    try:
        return bool(enforce_tick().get("ok"))
    except Exception as e:  # pragma: no cover — enforce_tick swallows internally
        try:
            from .daemon_ops import log
            log(f"kanban-enforce: stream check raised: {e}", "ERROR")
        except Exception:
            pass
        return False


def _daemon_pid_alive(pid_file=None) -> bool:
    """Is the pid in the daemon pid file a live process? (same probe shape
    as orch_waker._daemon_pid_alive; injected in tests)."""
    try:
        with open(pid_file or DAEMON_PID_PATH) as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _daemon_predates_build(pid_file=None, build_file=None) -> bool:
    """Did the RUNNING daemon start before THIS build of the enforcer landed?

    The upgrade race the fatal verdict must never mis-fire on, verbatim from
    orch_waker._daemon_predates_build: doctor is bootstrap's Stage-1 gate and
    the daemon restart is a LATER stage, so during an upgrade the live daemon
    was started from code with no kanban_enforce stream at all. The pid file
    is written at daemon start, so ``pid mtime >= this file's mtime`` means the
    daemon could have stamped and did not (broken wiring); an older pid means
    it never could (upgrade). Either stat failing answers False — an
    unverdictable check must not sink an install.
    """
    try:
        started = os.path.getmtime(pid_file or DAEMON_PID_PATH)
        built = os.path.getmtime(build_file or __file__)
    except OSError:
        return False
    return started < built


def doctor_freshness_limit_s() -> float:
    """The doctor's freshness budget for a stream ticking every stream_cadence()."""
    return 2 * float(stream_cadence()) + DOCTOR_SLACK_S


def _read_stream_state(state_dir=None) -> dict:
    """The enforce stream's state entry ({} when absent/unreadable).

    Uses the daemon's own ``state`` reader when no explicit dir is given — the
    same funnel the tick writes through, including its test-time redirect —
    and a plain JSON read otherwise (doctor may be pointed at a throwaway
    tree). Never raises.
    """
    if state_dir is None:
        try:
            from .state import read_state
            return read_state(STREAM_NAME) or {}
        except Exception:  # pragma: no cover — defensive, read_state is local
            return {}
    path = os.path.join(os.path.expanduser(state_dir), f"{STREAM_NAME}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def doctor_check(now=None, state_dir=None, paused_path=None, stream_state=None,
                 _daemon_running=None, hscc_dir=None, _predates_build=None) -> dict:
    """Doctor verdict: is the daemon actually enforcing the paused-board policy?

    There is no enable/disable toggle for this feature — a pause IS the opt-in
    (the operator typed it), so unlike orch_waker there is no "disabled"
    verdict; what the row asserts is that the ENFORCER is alive whenever policy
    could be live, and that the policy state itself is coherent.

    * no boards paused + stream fresh            -> ok, ``idle`` (nothing to
      enforce and the enforcer is demonstrably ticking — the state a healthy
      fleet spends 99% of its life in).
    * boards paused + stream fresh               -> ok, detail names the
      board count and the last enforce age (the audit's "N board(s) paused,
      last enforce Ns ago" row).
    * boards paused + stream FAILED              -> ok=False, fatal=False: the
      loop is alive but the hold is erroring — the leak is OPEN right now, so
      the fix text says so.
    * boards paused + stream stale               -> ok=False, fatal=False:
      drift to fix, never a reason to abort an install.
    * no stream stamp + daemon running + not     -> ok=False, fatal=True: the
      predates-build                                        feature is needed,
      something is ticking, and this stream has never stamped — the wiring
      itself is broken (the orch-waker fatal class).
    * no stream stamp + daemon older than build  -> warn: upgrade race; goes
      green on its own after the daemon restart later in bootstrap.
    * no stream stamp + no daemon                -> warn: nothing ticks, which
      is honest at Stage 1 (before the daemon has ever started) — never fatal.
    * policy incoherent (paused board entry with no cards list) -> warn naming
      it, whatever the stream says.

    Reads only (the paused state file + the stream state file + daemon.pid), so
    it is safe to run while the daemon ticks and it never touches a board DB.
    ``stream_state=`` / ``_daemon_running=`` / ``_predates_build=`` inject the
    three probes (tests); ``hscc_dir=`` (the doctor_notifier_mask convention —
    run_doctor passes ``os.environ.get("HSCC_DIR")``) relocates the WHOLE
    ``~/.hscc`` root for this read so a sandboxed bootstrap (HOME stays real)
    reads the SANDBOX's state, not the operator's live daemon. Never raises:
    an unreadable paused-state file reads as "nothing paused" (fail-open for
    the ROW; enforce() itself re-reads the file and fails loud via the stream).
    """
    if now is None:
        now = _real_time()
    pid_file = None
    if hscc_dir:
        root = os.path.expanduser(hscc_dir)
        if state_dir is None:
            state_dir = os.path.join(root, "state")
        if paused_path is None:
            paused_path = os.path.join(root, "state", "kanban_paused.json")
        pid_file = os.path.join(root, "daemon.pid")
        if _daemon_running is None:
            _daemon_running = lambda: _daemon_pid_alive(pid_file)  # noqa: E731
    if _daemon_running is None:
        _daemon_running = _daemon_pid_alive
    if _predates_build is None:
        _predates_build = lambda: _daemon_predates_build(pid_file)  # noqa: E731

    state = load_state(paused_path)
    boards = paused_boards(state)
    incoherent = [b for b in boards if not isinstance(state[b], dict)
                  or not isinstance(state[b].get("cards"), list)]
    fix = ("restart the hscc daemon (`hscc start`) and re-run; if it stays "
           "red check ~/.hscc/daemon.log for a kanban-enforce error")

    if stream_state is None:
        stream_state = _read_stream_state(state_dir)
    last = stream_state.get("last_enforce")
    age = (now - float(last)) if isinstance(last, (int, float)) else None
    limit = doctor_freshness_limit_s()

    def _verdict(ok, fatal, detail, fix_text=""):
        res = {"name": CHECK_NAME, "ok": ok, "fatal": fatal, "detail": detail,
               "fix": fix_text}
        if incoherent:
            # The stream verdict stands, but a policy file the reader cannot
            # trust is always named — warn at minimum, never hidden behind
            # an "idle" green.
            res["detail"] += (f"; WARNING: {len(incoherent)} paused entr(ies) "
                              f"malformed ({', '.join(sorted(incoherent)[:3])}) "
                              "— re-run `hscc kanban paused` and resume/pause "
                              "if the state file is corrupt")
            res["fix"] = res["fix"] or "repair ~/.hscc/state/kanban_paused.json"
        return res

    boards_note = (f"{len(boards)} board(s) paused ({', '.join(sorted(boards)[:3])})"
                   if boards else "no boards paused")

    if age is None:
        state_hint = (os.path.join(os.path.expanduser(state_dir), f"{STREAM_NAME}.json")
                      if state_dir else f"~/.hscc/state/{STREAM_NAME}.json")
        running = _daemon_running()   # probe ONCE: a restart mid-check must not
        # flip the verdict between the liveness bit and the age comparison.
        if running and not _predates_build():
            return _verdict(False, True,
                            f"never enforced while the daemon runs — the "
                            f"kanban_enforce stream has no state at {state_hint} "
                            f"({boards_note})")
        if running:
            return _verdict(False, False,
                            "the RUNNING daemon predates this build (never "
                            f"ticked yet) — the daemon restart later in "
                            f"bootstrap starts the stream ({boards_note})", fix)
        return _verdict(False, False,
                        "the daemon is not running — nothing enforces the "
                        f"paused-board policy ({boards_note}; start it with "
                        "`hscc start`)", fix)

    if age > limit:
        return _verdict(False, False,
                        f"stale: last enforce {age:.0f}s ago (budget "
                        f"{limit:.0f}s = 2*{stream_cadence()}s+{DOCTOR_SLACK_S}s; "
                        f"{boards_note})", fix)
    if stream_state.get("ok") is False:
        why = str(stream_state.get("message") or "last tick reported ok=False")
        return _verdict(False, False,
                        f"last enforce tick FAILED: {why[:160]} ({boards_note}"
                        + (")" if not boards else "; the hold is NOT being enforced)"),
                        "fix the cause named in the message; the stream stamps "
                        "every tick so this clears on its own")
    return _verdict(True, False,
                    f"{boards_note}, last enforce {age:.0f}s ago "
                    f"(budget {limit:.0f}s)", "")
