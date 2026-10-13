"""repl_inject — type into the operator's RUNNING Hermes REPL, as they would.

Ported from the operator's proven tool ~/bin/hermes-inject (findings doc
"tmux-as-source-of-truth", 2026-10-12). The iOS/API write path used to type
into a SECOND REPL that ``hermes serve`` hosts on the same session (serve
/api/pty), so the terminal REPL — and any /goal loop inside it — never saw
the message. This module types into the very pane on the operator's screen:
same process, same history, seen by a running goal, no second driver and no
turn-lease fight.

Mechanism (identical to the proven tool):
  * discovery: ``tmux list-panes -a`` -> for each pane tty, ``ps -t`` ->
    keep panes whose command is a ``hermes ... chat`` process (gateway/serve
    processes excluded), extract the profile from ``-p/--profile``.
  * resolution: EXACTLY ONE pane for the profile, or refuse. Never guess —
    a wrong injection target is worse than no injection (the operator's rule:
    never inject a REPL you did not pick).
  * busy detection: the REPL prompt shows ``msg=interrupt`` while a turn is
    running; a plain message then INTERRUPTS it. Auto mode sends ``/queue``
    instead so a running goal is never cut off mid-work.
  * delivery: tmux bracketed paste (load-buffer + ``paste-buffer -p -d``) so
    a multi-line message arrives as ONE message, then Enter.

All tmux/ps calls go through module-level seams (``run_tmux`` / ``run_ps``)
so unit tests never touch a real tmux server or the operator's panes.
"""

from __future__ import annotations

import re
import subprocess
import time
import uuid

BUSY_MARK = "msg=interrupt"          # shown in the prompt line while a turn runs
REPLY_BOX = "☤ Hermes"               # header of the assistant's reply box

# modes: auto -> plain when idle, /queue when busy. Explicit overrides.
MODES = ("auto", "queue", "steer", "interrupt")
_PREFIX = {"queue": "/queue ", "steer": "/steer ", "interrupt": "", "plain": ""}


class InjectError(Exception):
    """Refusal or transport failure. ``kind``: no_panes | ambiguous | tmux | empty."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


# --------------------------------------------------------------------------- #
# Subprocess seams (tests replace these; production shells out exactly like
# the reference tool).
# --------------------------------------------------------------------------- #

def _under_pytest() -> bool:
    """True inside a pytest run. The ORIGINAL subprocess seams refuse to
    execute there: tests that forget to stub them must not list — let alone
    inject into — the operator's REAL panes (the operator's rule: no test may
    touch a live session). Stubbing repl_inject.run_tmux/run_ps replaces the
    module attribute, so the replaced function runs normally (identity check).
    Same guard style as doctor's run_doctor fleet-check skip."""
    import os
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def run_tmux(*args: str, input_text: str | None = None) -> str:
    """Run tmux with argv; return stdout. Raise InjectError on non-zero rc.

    Hermeticity guard: under pytest the ORIGINAL seam raises instead of
    executing (a test must stub it explicitly) — no suite run can ever
    discover or type into a live operator pane.
    """
    if _under_pytest() and run_tmux is _ORIG_RUN_TMUX:
        raise InjectError(
            "tmux", "real tmux execution is disabled under pytest "
                    "(hermeticity guard) — stub repl_inject.run_tmux")
    p = subprocess.run(["tmux", *args], input=input_text,
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise InjectError("tmux",
                          f"tmux {' '.join(args[:2])} failed: {p.stderr.strip()}")
    return p.stdout


def run_ps(tty: str) -> str:
    """Commands running on ``tty`` (one per line), via ps.

    Same pytest guard as :func:`run_tmux`.
    """
    if _under_pytest() and run_ps is _ORIG_RUN_PS:
        raise InjectError(
            "tmux", "real ps execution is disabled under pytest "
                    "(hermeticity guard) — stub repl_inject.run_ps")
    return subprocess.run(["ps", "-t", tty.replace("/dev/", ""), "-o", "command="],
                          capture_output=True, text=True).stdout


# Captured after definition: identity check for the hermeticity guards above.
_ORIG_RUN_TMUX = run_tmux
_ORIG_RUN_PS = run_ps


# --------------------------------------------------------------------------- #
# Pane discovery + busy detection (reference-identical parsing)
# --------------------------------------------------------------------------- #

_PROFILE_RE = re.compile(r"(?:^|\s)(?:-p|--profile)[ =](\S+)")


def hermes_panes() -> list[dict]:
    """Every tmux pane running a Hermes REPL chat, with its profile.

    Excludes gateway/serve processes (their command lines can also carry
    'chat'); a pane running ``hermes -p X chat`` is the operator's REPL.
    """
    try:
        out = run_tmux("list-panes", "-a", "-F",
                       "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}"
                       "\t#{pane_tty}")
    except InjectError:
        return []
    panes = []
    for line in out.splitlines():
        try:
            pane_id, target, tty = line.split("\t")
        except ValueError:
            continue
        ps = run_ps(tty)
        for cmd in ps.splitlines():
            if "hermes" not in cmd or " chat" not in f" {cmd}":
                continue
            if "gateway" in cmd:
                continue
            m = _PROFILE_RE.search(cmd)
            panes.append({"pane": pane_id, "target": target, "tty": tty,
                          "profile": m.group(1) if m else "default", "cmd": cmd})
            break
    return panes


def screen(pane: str, lines: int = 60) -> str:
    return run_tmux("capture-pane", "-p", "-t", pane, "-S", f"-{lines}")


def is_busy(pane: str) -> bool:
    """True while the pane's last lines show the busy prompt marker."""
    tail = [l for l in screen(pane, 12).splitlines() if l.strip()]
    return any(BUSY_MARK in l for l in tail[-4:])


def last_reply(text: str) -> str:
    """The most recent assistant reply box's body, best effort."""
    lines = text.splitlines()
    starts = [i for i, l in enumerate(lines) if REPLY_BOX in l]
    if not starts:
        return ""
    body = []
    for l in lines[starts[-1] + 1:]:
        if l.lstrip().startswith("╰"):
            break
        body.append(l.strip("│ ").rstrip())
    return "\n".join(body).strip()


def resolve_pane(profile: str, panes: list[dict] | None = None,
                 pane_override: str | None = None) -> dict:
    """Exactly-one pane for ``profile``, or InjectError. Never guess."""
    if panes is None:
        panes = hermes_panes()
    if pane_override:
        match = [p for p in panes if pane_override in (p["pane"], p["target"])]
    else:
        match = [p for p in panes if p["profile"] == profile]
    if len(match) != 1:
        found = ", ".join(f"{p['profile']}@{p['target']}" for p in panes) or "none"
        kind = "no_panes" if not match else "ambiguous"
        why = "no" if not match else f"{len(match)}"
        raise InjectError(
            kind,
            f"{why} tmux pane running a Hermes REPL for profile {profile!r} "
            f"(found: {found}).")
    return match[0]


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #

def paste(pane: str, text: str) -> None:
    """Bracketed-paste ``text`` into ``pane`` (multi-line stays ONE message)."""
    buf = f"hscc-inject-{uuid.uuid4().hex[:8]}"
    run_tmux("load-buffer", "-b", buf, "-", input_text=text)
    # -p: bracketed paste; -d: delete the buffer so nothing lingers in tmux.
    run_tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", pane)


def deliver(pane: dict, text: str, mode: str = "auto",
            sleep_fn=time.sleep) -> dict:
    """Deliver to an ALREADY-RESOLVED pane dict (no re-discovery).

    Splits resolve from delivery so callers that resolved the pane earlier
    (e.g. the WS endpoint, which discovers synchronously to decide the
    routing path) deliver without a second round of pane discovery and
    without re-checking ambiguity. Raises InjectError on empty text, bad
    mode, or tmux failure.

    Receipt: {pane, target, profile, busy, mode, sent_text}.
    """
    if mode not in MODES:
        raise InjectError("empty", f"unknown mode {mode!r} (want one of {MODES})")
    if not text or not text.strip():
        raise InjectError("empty", "empty message")
    text = text.rstrip("\n")
    busy = is_busy(pane["pane"])
    eff = ("queue" if busy else "plain") if mode == "auto" else mode
    prefix = _PREFIX[eff]
    if prefix:
        run_tmux("send-keys", "-t", pane["pane"], "-l", prefix)
    paste(pane["pane"], text)
    sleep_fn(0.5)
    run_tmux("send-keys", "-t", pane["pane"], "Enter")
    return {"pane": pane["pane"], "target": pane["target"],
            "profile": pane["profile"], "busy": busy, "mode": eff,
            "sent_text": (prefix + text) if prefix else text}


def inject(profile: str, text: str, mode: str = "auto",
           pane_override: str | None = None,
           sleep_fn=time.sleep) -> dict:
    """Deliver ``text`` to the profile's REPL pane. Returns a receipt dict.

    ``mode``: auto (plain if idle, /queue if busy — NEVER interrupts a
    running turn), or explicit queue/steer/interrupt. Raises InjectError on
    no/ambiguous pane, empty text, or tmux failure.
    """
    pane = resolve_pane(profile, pane_override=pane_override)
    return deliver(pane, text, mode=mode, sleep_fn=sleep_fn)


def wait_for_reply(pane: str, before: str, timeout: float = 900.0,
                   poll: float = 3.0, sleep_fn=time.sleep,
                   busy_fn=None, screen_fn=None) -> str | None:
    """Best-effort reply capture: screen changed AND no longer busy.

    Returns the reply text, or None on timeout. (Reference tool semantics:
    --wait is best effort; screen(screen) is the truth.)
    """
    busy_fn = busy_fn or is_busy
    screen_fn = screen_fn or screen
    deadline = time.time() + timeout
    sleep_fn(3)
    while time.time() < deadline:
        now = screen_fn(pane, 200)
        if now != before and not busy_fn(pane):
            return last_reply(now)
        sleep_fn(poll)
    return None
