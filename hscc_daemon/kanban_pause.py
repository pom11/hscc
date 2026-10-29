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
  through ``hermes-inject`` (types into the orchestrator's own tmux REPL);
  ``HSCC_INJECT_BIN`` overrides the path, and a missing tool or pane is
  reported, never fatal.
* **enforce** — blocks any card that became ``ready``/``running`` on a paused
  board after the pause (an orchestrator creating new cards). Idempotent; safe
  to run from a loop or the fleet check.

State lives in ``~/.hscc/state/kanban_paused.json``. The hermes CLI runner and
the state path are injectable so the tests never touch a real board.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Callable, Optional

STATE_PATH = os.path.expanduser(
    os.environ.get("HSCC_KANBAN_PAUSE_STATE", "~/.hscc/state/kanban_paused.json"))
HERMES_CONFIG = os.path.expanduser("~/.hermes/config.yaml")
REASON_PREFIX = "PAUSED"
INJECT_BIN = os.path.expanduser(os.environ.get("HSCC_INJECT_BIN", "~/bin/hermes-inject"))

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


def _inject(profile: str, message: str, mode: str) -> dict:
    if not os.path.exists(INJECT_BIN):
        return {"ok": False, "error": f"{INJECT_BIN} not found"}
    try:
        r = subprocess.run([INJECT_BIN, profile, "--mode", mode, message],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": r.returncode == 0,
            "error": None if r.returncode == 0 else (r.stderr or r.stdout).strip()[-200:]}


def orchestrator_for(board: str) -> str:
    """Per-project orchestrator naming convention: board `<p>` -> `<p>-orch`."""
    return f"{board}-orch"
HOLD_STATUSES = ("running", "ready")


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
          now: Optional[int] = None, inject: Optional[Callable] = _inject) -> dict:
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
        if inject is not None:
            report[board]["orchestrator"] = inject(
                orchestrator_for(board),
                PAUSE_STEER.format(board=board, why=f": {reason}" if reason else ""),
                "interrupt")
    if paused_boards(state) and "_auto_decompose_was" not in state:
        state["_auto_decompose_was"] = _set_auto_decompose(False, config_path)
    save_state(state, state_path)
    return report


def resume(boards: list, *, run: Callable = _run_hermes,
           state_path: Optional[str] = None, config_path: Optional[str] = None,
           inject: Optional[Callable] = _inject) -> dict:
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
    """Hold cards that reached ready/running on a paused board after the pause."""
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
            if card["status"] in HOLD_STATUSES and card["id"] not in held:
                if _hold(board, card, text, run):
                    entry["cards"].append({"id": card["id"], "was": card["status"]})
                    added.append(card["id"])
        if added:
            report[board] = added
    if report:
        save_state(state, state_path)
    return report
