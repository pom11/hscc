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
    processes excluded), extract the profile from ``-p/--profile``. A pane's
    first chat-looking line may be a WRAPPER (a launcher such as the one that
    starts ``kronos-orch``, or ``hscc project chat <p>``) with the real
    ``hermes -p X chat`` child underneath it, so every line of the pane is
    scanned and the one that NAMES a profile wins (see
    :func:`pane_chat_command`).
  * resolution: EXACTLY ONE pane for the profile, or refuse. Never guess —
    a wrong injection target is worse than no injection (the operator's rule:
    never inject a REPL you did not pick).
  * modes: ``plain`` (no verb prefix), ``queue`` (``/queue ``), ``steer``
    (``/steer ``) and ``auto`` = ``queue`` while busy else ``plain``. The
    busy prompt shows ``msg=interrupt``, i.e. a plain message sent during a
    running turn INTERRUPTS it; ``auto`` therefore queues so a running goal is
    never cut off mid-work. Legacy ``interrupt`` == ``plain``.
  * planning: :func:`plan` / :func:`plan_pane` answer "which pane, busy or
    idle, which verb" and touch nothing — the dry-run path (and the
    ``hscc orch inject --dry-run`` verb built on it) renders that, never a
    ``send-keys``.
  * delivery: tmux bracketed paste (load-buffer + ``paste-buffer -p -d``) so
    a multi-line message arrives as ONE message, then Enter.

THE ONE PANE MATCHER LIVES HERE. :func:`pane_chat_command` /
:func:`pane_chat_profile` are public for the WHOLE repo, not just this
module: any consumer that asks "is this command line a Hermes REPL chat, and
under which profile?" calls them (today:
hscc-project/flightdeck/core/tmux_chat.py — the project-chat launch door,
doctor's REPL-visibility panel and the bootstrap REPL report). A consumer that
copies the predicate drifts from it, and GAP-C-3 is the measured proof: the
copy had grown a per-line ``default`` rule that contradicted the
prefer-profiled-sibling rule below. When a copy and this rule disagree, THIS
is the one that wins — so call it instead of re-deriving it.

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

# Modes (operator naming contract, same as ~/bin/hermes-inject):
#   plain   -> no verb prefix (the reference tool's `interrupt`; a plain
#              message sent while a turn runs is what INTERRUPTS it)
#   queue   -> "/queue "   (never cuts off a running turn)
#   steer   -> "/steer "
#   auto    -> queue when the pane is busy, plain when it is idle
# `interrupt` is kept as a legacy alias of `plain` so callers written against
# the old vocabulary keep working; the resolved mode in any receipt is always
# the canonical name, never `interrupt`.
MODES = ("auto", "plain", "queue", "steer")
LEGACY_MODES = ("interrupt",)            # accepted, normalised to `plain`

# The ONE verb table in the repo: resolved mode -> what gets typed first.
VERB_PREFIX = {"plain": "", "queue": "/queue ", "steer": "/steer "}


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

#: The ONE profile-flag pattern in the repo — a chat command line's profile is
#: whatever ``-p/--profile`` names (space or ``=`` form). Exported so a consumer
#: that reads the flag off a *launch* argv (tmux_chat.target_profile) reuses
#: this instead of copying the regex: two copies of a pattern drift (GAP-C-3).
PROFILE_FLAG_RE = re.compile(r"(?:^|\s)(?:-p|--profile)[ =](\S+)")
_CHAT_MARK = " chat"
_GATEWAY_MARK = "gateway"            # never a REPL target (see pane_chat_command)
DEFAULT_PROFILE = "default"          # the reference tool's no-flag fallback


def normalize_mode(mode: str) -> str:
    """Canonical mode name, or InjectError.

    ``interrupt`` (the reference tool's old name for a no-verb send) is
    accepted as an alias of ``plain``; everything else must already be one of
    :data:`MODES`. ``auto`` stays ``auto`` here — it is resolved against the
    pane's busy state by :func:`resolve_mode`.
    """
    if mode == "interrupt":
        return "plain"
    if mode not in MODES:
        raise InjectError(
            "empty", f"unknown mode {mode!r} (want one of {MODES}"
                     f", or the legacy alias {LEGACY_MODES[0]!r})")
    return mode


def resolve_mode(mode: str, busy: bool) -> str:
    """The concrete mode to deliver with, given the pane's busy state.

    ``auto`` -> ``queue`` while busy (never cut off a running turn), ``plain``
    when idle. An explicit mode is honoured as asked — including ``plain``
    while busy, which is the one combination that interrupts, so callers that
    report to a human mark it (see :func:`plan_pane`).
    """
    mode = normalize_mode(mode)
    if mode == "auto":
        return "queue" if busy else "plain"
    return mode


def pane_chat_command(cmds) -> tuple[str, str] | None:
    """The ``(profile, command)`` of a pane's Hermes REPL, or None.

    THE canonical pane matcher — the ONE predicate in the repo that decides
    "is this command line a Hermes REPL chat, and under which profile".
    ``cmds`` are the ``ps -t <tty>`` command lines for that pane. A pane can
    hold BOTH a wrapper (a launcher script, or ``hscc project chat <p>``) AND
    the real ``hermes -p <profile> chat`` child, and the wrapper is frequently
    the FIRST line — the 2026-10-29 lesson from ~/bin/hermes-inject: taking
    the first chat-looking line labelled such a pane ``default`` (no ``-p`` on
    a launcher) and an inject by profile name then refused on the one pane
    that was actually right.

    Rule: scan every line; prefer the chat line that NAMES a profile; fall
    back to ``default`` only when NONE of them does. Gateway/serve command
    lines are never a REPL target.

    PUBLIC on purpose, and public to the whole repo: tmux_chat (the project
    chat launch door, doctor's REPL-visibility panel, the bootstrap REPL
    report) calls THIS function instead of re-deriving the rule. GAP-C-3 was
    exactly that re-derivation drifting — a per-line ``default`` in tmux_chat
    contradicted the prefer-profiled-sibling rule right here, so the launch
    door could attribute a wrapper pane to ``default`` while injection here
    attributed the same pane to its real profile. When the two disagree,
    THIS is the one that wins: every consumer resolves panes through it.
    """
    fallback: tuple[str, str] | None = None
    for cmd in cmds:
        if "hermes" not in cmd or _CHAT_MARK not in f" {cmd}":
            continue
        if _GATEWAY_MARK in cmd:
            continue
        m = PROFILE_FLAG_RE.search(cmd)
        if m:
            return m.group(1), cmd
        if fallback is None:
            fallback = (DEFAULT_PROFILE, cmd)
    return fallback


def pane_chat_profile(cmds) -> str | None:
    """The profile a set of command lines resolves to, or None.

    One-liner over :func:`pane_chat_command` for consumers that only need the
    profile (the pane attribution), not the line that justified it. Same rule,
    so the launch door and the injection path can never disagree about which
    profile a pane belongs to.
    """
    hit = pane_chat_command(cmds)
    return hit[0] if hit else None


def profile_flag(cmdline: str) -> str | None:
    """The profile a LAUNCH command line names via ``-p/--profile``.

    Not a pane matcher — this makes no chat/gateway judgement; it reads the
    flag off an argv we are about to exec (tmux_chat.target_profile). It lives
    here so the flag pattern itself has exactly one compiled copy: two copies
    of a pattern drift exactly like two copies of a predicate did (GAP-C-3).
    """
    m = PROFILE_FLAG_RE.search(cmdline)
    return m.group(1) if m else None


def hermes_panes() -> list[dict]:
    """Every tmux pane running a Hermes REPL chat, with its profile.

    Excludes gateway/serve processes (their command lines can also carry
    'chat'); a pane running ``hermes -p X chat`` is the operator's REPL. The
    profile comes from the pane line that actually names one, so a pane whose
    first process is a launcher wrapper still resolves to its real profile
    (:func:`_pane_chat_command`).
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
        hit = pane_chat_command(run_ps(tty).splitlines())
        if hit:
            panes.append({"pane": pane_id, "target": target, "tty": tty,
                          "profile": hit[0], "cmd": hit[1]})
    return panes


def screen(pane: str, lines: int = 60) -> str:
    return run_tmux("capture-pane", "-p", "-t", pane, "-S", f"-{lines}")


def busy_from_screen(text: str) -> bool:
    """True when the last non-empty lines of a captured screen carry the
    busy prompt marker. One rule, shared by :func:`is_busy` and
    :func:`plan_pane` so the two can never disagree about what busy means."""
    tail = [l for l in text.splitlines() if l.strip()]
    return any(BUSY_MARK in l for l in tail[-4:])


def is_busy(pane: str) -> bool:
    """True while the pane's last lines show the busy prompt marker."""
    return busy_from_screen(screen(pane, 12))


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
# Planning — the dry-run / preview path (sends nothing)
# --------------------------------------------------------------------------- #

def plan_pane(pane: dict, mode: str = "auto", busy_fn=None) -> dict:
    """Decide how a message WOULD go to an already-resolved pane. Sends nothing.

    One shared decision for the dry-run path and the real delivery: the pane's
    busy state, the resolved mode and the verb prefix are computed here and
    nowhere else, so a preview can never disagree with what delivery does.
    Raises InjectError on a bad mode (pane resolution errors come from
    :func:`resolve_pane` / :func:`plan`).

    Plan: {pane, target, profile, busy, mode, verb, interrupts}.
    """
    busy_fn = busy_fn or is_busy
    busy = bool(busy_fn(pane["pane"]))
    eff = resolve_mode(mode, busy)
    return {"pane": pane["pane"], "target": pane["target"],
            "profile": pane["profile"], "busy": busy, "mode": eff,
            "verb": VERB_PREFIX[eff],
            # The one combination that cuts off a running turn: an explicit
            # no-verb send while busy. A human-readable target must say so.
            "interrupts": eff == "plain" and busy}


def plan(profile: str, mode: str = "auto",
         pane_override: str | None = None, busy_fn=None) -> dict:
    """Resolve + plan for ``profile`` without touching the pane.

    Raises InjectError exactly as :func:`inject` does for a bad mode, no pane
    or an ambiguous pane — never sends, so it is what ``--dry-run`` renders and
    what a CLI verb can print as a receipt without duplicating pane logic. A
    bad mode is refused BEFORE any pane work, matching :func:`deliver`.
    """
    normalize_mode(mode)      # refuse up front, before touching any pane
    pane = resolve_pane(profile, pane_override=pane_override)
    return plan_pane(pane, mode=mode, busy_fn=busy_fn)


def format_plan(p: dict) -> str:
    """The reference tool's one-line target rendering of a plan."""
    line = (f"target: {p['profile']} @ {p['target']} "
            f"({'busy' if p['busy'] else 'idle'}) -> {p['mode']}")
    if p.get("interrupts"):
        line += " (interrupts the running turn)"
    return line


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
            sleep_fn=time.sleep, dry_run: bool = False,
            busy_fn=None) -> dict:
    """Deliver to an ALREADY-RESOLVED pane dict (no re-discovery).

    Splits resolve from delivery so callers that resolved the pane earlier
    (e.g. the WS endpoint, which discovers synchronously to decide the
    routing path) deliver without a second round of pane discovery and
    without re-checking ambiguity. Raises InjectError on empty text, bad
    mode, or tmux failure.

    ``dry_run=True`` decides and returns but sends NOTHING: no ``send-keys``,
    no buffer, no Enter. Input validation still applies, so a dry run of an
    empty message or a bad mode refuses exactly as a real send would.

    Receipt: {pane, target, profile, busy, mode, verb, interrupts, sent_text}.
    A dry-run receipt adds {dry_run: True, text, chars}.
    """
    normalize_mode(mode)      # bad mode refuses BEFORE any pane work
    if not text or not text.strip():
        raise InjectError("empty", "empty message")
    text = text.rstrip("\n")
    decision = plan_pane(pane, mode=mode, busy_fn=busy_fn)
    if dry_run:
        return {**decision, "dry_run": True, "text": text, "chars": len(text)}
    prefix = decision["verb"]
    if prefix:
        run_tmux("send-keys", "-t", pane["pane"], "-l", prefix)
    paste(pane["pane"], text)
    sleep_fn(0.5)
    run_tmux("send-keys", "-t", pane["pane"], "Enter")
    return {**decision, "sent_text": (prefix + text) if prefix else text}


def inject(profile: str, text: str, mode: str = "auto",
           pane_override: str | None = None,
           sleep_fn=time.sleep, dry_run: bool = False) -> dict:
    """Deliver ``text`` to the profile's REPL pane. Returns a receipt dict.

    ``mode``: auto (plain if idle, /queue if busy — NEVER interrupts a
    running turn), or explicit plain/queue/steer (``interrupt`` accepted as the
    legacy name of ``plain``). ``dry_run=True`` resolves the pane, reports the
    target / busy state / chosen verb and sends nothing. Raises InjectError on
    no/ambiguous pane, empty text, bad mode, or tmux failure.
    """
    pane = resolve_pane(profile, pane_override=pane_override)
    return deliver(pane, text, mode=mode, sleep_fn=sleep_fn, dry_run=dry_run)


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
