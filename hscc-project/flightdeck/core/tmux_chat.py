"""tmux_chat — every interactive Hermes chat the hscc CLI opens is tmux-aware.

Why (operator requirement, 2026-10-13): the iOS app types into the operator's
terminal REPL by finding the project's ``hermes -p <profile> chat`` pane in
tmux (``hscc-api/repl_inject``). A REPL started OUTSIDE tmux is invisible to
that path, so iPhone messages silently go to a hidden serve-hosted session
instead of the chat on the operator's screen. The fix has to be at the door
where the chat is OPENED, not only in the error path: every interactive chat
launch goes through :func:`launch_interactive_chat`, which guarantees one of:

  A. already inside tmux (``$TMUX`` set) -> open the chat right here, exactly
     as before (execvp in place, real TTY);
  B. not inside tmux, tmux available -> open the chat in the named session
     ``hscc-<project>`` (create detached, then attach; attach an existing one
     after verifying its REPL belongs to this project's profile);
  C. cannot use tmux (not installed / server fails to start / an existing
     ``hscc-<project>`` session runs a DIFFERENT profile's REPL) -> STOP and
     ask an explicit Y/N naming exactly what is missing and the consequence
     ("Without tmux, messages from the iOS app will NOT reach this chat...").
     Default N; non-interactive (no TTY) -> refuse with that message. A
     silent non-tmux fallback never happens.

Hermeticity (same identity-guard style as hscc-api/repl_inject): the real
subprocess/exec/prompt seams raise under pytest unless a test replaced the
module attribute, so no suite run can exec a chat, touch a live tmux server,
or block on input(). Entry points inject ``exec_seam``/``env`` like the
existing chat tests already inject ``exec_seam``.

Pane matching (GAP-C-3): deciding "is this ``ps`` line a Hermes REPL chat,
and under which profile" is NOT this module's job — the ONE matcher is
``repl_inject.pane_chat_command`` / ``pane_chat_profile`` in the sibling
hscc-api plugin dir, and every question here (``chat_cmd_profile``,
``_session_profiles``, ``_live_profile_panes``) is a thin delegation to it.
The former local copy had already drifted (its per-line ``default``
contradicted the canonical prefer-profiled-sibling rule), which is exactly
why the rule is imported, never re-derived. If hscc-api is not deployed
beside hscc-project the matcher cannot answer, and that surfaces as a NAMED
branch-C refusal / UNVERIFIED row — never as a private fallback predicate.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

CONSEQUENCE = ("Without tmux, messages from the iOS app will NOT reach this "
               "chat (they go to a hidden background session).")

_INVALID_TMUX_NAME = re.compile(r"[^A-Za-z0-9_-]")


class TmuxChatRefused(Exception):
    """Refusal of branch C (operator answered N, or no TTY to ask)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class PaneMatcherMissing(RuntimeError):
    """The ONE canonical pane matcher (hscc-api/repl_inject) cannot be loaded.

    Raised instead of silently falling back to a private predicate: a world
    where the rule is unanswerable must be reported (branch-C reason /
    UNVERIFIED), never guessed at with a copy that can drift (GAP-C-3).
    """


_MATCHER_UNAVAILABLE = (
    "pane matcher unavailable — hscc-api/repl_inject.py is not importable "
    "beside hscc-project (the hscc-api plugin must be deployed next to "
    "hscc-project; it owns the one REPL matcher)")

_MATCHER = None  # cached after the first successful resolution

#: The canonical matcher module must expose all of these (a module that does
#: not is not the matcher — never accepted, see _pane_matcher).
_MATCHER_API = ("pane_chat_command", "pane_chat_profile", "profile_flag",
                "PROFILE_FLAG_RE")


def _is_matcher(mod) -> bool:
    return all(hasattr(mod, name) for name in _MATCHER_API)


def _load_matcher_from_sibling_dir():
    """Load <repo_or_plugins_root>/hscc-api/repl_inject.py BY PATH, or None.

    importlib with a private module name (no sys.path insert, no sharing the
    name ``repl_inject`` in sys.modules): the resolution is deterministic —
    the matcher IS the sibling file — and it cannot be poisoned by, or
    poison, an unrelated module cached under that name.
    """
    src = Path(__file__).resolve().parents[3] / "hscc-api" / "repl_inject.py"
    if not src.is_file():
        return None
    spec = importlib.util.spec_from_file_location(
        "_hscc_canonical_pane_matcher", src)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pane_matcher():
    """The canonical matcher module ``repl_inject`` — THE owner of the rule.

    Resolution order: (1) an already-importable ``repl_inject`` that carries
    the full matcher API (the in-process module the iOS/CLI inject path uses,
    when this process can see it); (2) the sibling ``hscc-api/repl_inject.py``
    loaded by path — ``parents[3]`` of this file is the repo root in a
    checkout and the plugins dir when installed, and hscc-api sits beside
    hscc-project in BOTH layouts (same shape as flightdeck/core/
    repl_delivery._repl_inject and hscc-bootstrap/tmux_repl_report's
    by-path load of THIS module). A module lacking the matcher API is
    rejected, not trusted; if neither route yields the matcher, raise
    PaneMatcherMissing instead of falling back to a private predicate
    (install_payload deploys hscc-project and hscc-api as one payload, so a
    deployed tree always resolves it).
    """
    global _MATCHER
    if _MATCHER is not None:
        return _MATCHER
    try:
        import repl_inject  # noqa: F401
        if _is_matcher(repl_inject):
            _MATCHER = repl_inject
            return _MATCHER
    except ImportError:
        pass
    mod = _load_matcher_from_sibling_dir()
    if mod is not None and _is_matcher(mod):
        _MATCHER = mod
        return _MATCHER
    raise PaneMatcherMissing(_MATCHER_UNAVAILABLE)


# --------------------------------------------------------------------------- #
# Seams (tests replace these; production shells out exactly like the CLI does
# today). Identity guards: the ORIGINAL seam refuses to run under pytest.
# --------------------------------------------------------------------------- #

def _under_pytest() -> bool:
    import os as _os
    return bool(_os.environ.get("PYTEST_CURRENT_TEST"))


def _guard(name: str) -> None:
    if _under_pytest():
        raise RuntimeError(
            f"real {name} execution is disabled under pytest "
            f"(hermeticity guard) — stub tmux_chat.{name}")


def run_tmux(*args: str) -> subprocess.CompletedProcess:
    """Run tmux with argv; return the CompletedProcess (never raises on rc)."""
    if run_tmux is _ORIG_RUN_TMUX:
        _guard("run_tmux")
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def run_ps(tty: str) -> str:
    """Commands running on ``tty`` (one per line)."""
    if run_ps is _ORIG_RUN_PS:
        _guard("run_ps")
    return subprocess.run(["ps", "-t", tty.replace("/dev/", ""), "-o", "command="],
                          capture_output=True, text=True).stdout


def run_ps_all() -> str:
    """``tty`` + command line for EVERY process (ps -axo tty=,command=).

    Used to find REPL chats running OUTSIDE tmux (a tty no pane owns).
    Same pytest identity guard as :func:`run_ps`.
    """
    if run_ps_all is _ORIG_RUN_PS_ALL:
        _guard("run_ps_all")
    return subprocess.run(["ps", "-axo", "tty=,command="],
                          capture_output=True, text=True).stdout


def exec_chat(file: str, argv: list) -> None:
    """Replace the process with the chat (os.execvp contract)."""
    if exec_chat is _ORIG_EXEC_CHAT:
        _guard("exec_chat")
    os.execvp(file, list(argv))


def read_line() -> str:
    """One line from the operator's terminal (guarded: a test must stub it —
    no suite run may ever block on the real stdin)."""
    if read_line is _ORIG_READ_LINE:
        _guard("read_line")
    return input()


def prompt_yes_no(question: str) -> bool:
    """Ask the operator interactively; empty answer = N (default refuse)."""
    print(question, end="", flush=True)
    try:
        return read_line().strip().lower() in ("y", "yes")
    except EOFError:
        return False


_ORIG_RUN_TMUX = run_tmux
_ORIG_RUN_PS = run_ps
_ORIG_RUN_PS_ALL = run_ps_all
_ORIG_EXEC_CHAT = exec_chat
_ORIG_READ_LINE = read_line


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def session_name(project: str) -> str:
    """The named tmux session for a project's chat (tmux forbids . and :)."""
    return "hscc-" + _INVALID_TMUX_NAME.sub("-", project)


def target_profile(argv: list) -> str | None:
    """The ``-p/--profile`` the chat will run under (foreign-session check).

    Delegates the flag pattern itself to the canonical matcher module
    (:func:`repl_inject.profile_flag`) — one compiled copy of the pattern,
    never a second copy that can drift (GAP-C-3)."""
    return _pane_matcher().profile_flag(" ".join(argv))


def chat_cmd_profile(cmdline: str) -> str | None:
    """Profile of ONE running Hermes REPL chat command line, else None.

    Thin delegation to the canonical matcher (:func:`repl_inject.
    pane_chat_command`) applied to a single raw ``ps`` line — this module
    owns no predicate of its own. The former local copy contradicted the
    canonical rule for multi-line panes; pane-level questions must ask
    :func:`pane_profile` with the pane's whole line set instead, so the
    prefer-profiled-sibling rule decides exactly once, in repl_inject.
    """
    hit = _pane_matcher().pane_chat_command([cmdline])
    return hit[0] if hit else None


def pane_profile(cmds) -> str | None:
    """Profile of a pane/tty from ALL its ``ps`` command lines, else None.

    THE canonical matcher (:func:`repl_inject.pane_chat_profile`) verbatim:
    the line that NAMES a profile wins; ``default`` only when no line does.
    This is what makes the launch door, doctor and the iOS inject path agree
    on which profile a pane runs (the GAP-C-3 reconciliation).
    """
    return _pane_matcher().pane_chat_profile(cmds)


def pane_ttys() -> set:
    """ttys owned by tmux panes (normalised, no /dev/ prefix). Empty when tmux
    reports none; raises via the guarded seam when tmux cannot be consulted."""
    r = run_tmux("list-panes", "-a", "-F", "#{pane_tty}")
    if r.returncode != 0:
        return set()
    return {t.replace("/dev/", "") for t in r.stdout.split() if t != "no tty"}


def chat_processes() -> list:
    """[(tty, profile)] for every running Hermes REPL chat (tmux or not).

    Headless ``chat -q``/``-Q`` invocations (kanban workers, server-side
    one-shots) have NO controlling tty — ps prints it as ``?``/``??``. An
    interactive REPL always has a real tty (that is how repl_inject finds
    panes in the first place), so ttyless rows are excluded: a worker is
    not the operator's chat and must never be reported as one outside tmux.

    ``ps`` prints one row PER PROCESS, so a pane's wrapper and its
    ``hermes -p X chat`` child arrive as two rows of the SAME tty. They are
    grouped and resolved as ONE pane through the canonical matcher
    (:func:`pane_profile`) — the same whole-line-set resolution repl_inject
    applies with ``ps -t``. The old per-line rule double-reported such a tty
    (once mislabelled ``default`` by the wrapper row); the canonical rule
    reports it exactly once, under its real profile (GAP-C-3).
    """
    by_tty: dict = {}
    order = []
    for line in run_ps_all().splitlines():
        tty, _, cmd = line.strip().partition(" ")
        if not tty or tty.startswith("?") or not cmd:
            continue
        if tty not in by_tty:
            by_tty[tty] = []
            order.append(tty)
        by_tty[tty].append(cmd)
    rows = []
    for tty in order:
        prof = pane_profile(by_tty[tty])
        if prof:
            rows.append((tty, prof))
    return rows


def _session_profiles(session: str) -> set:
    """Profiles of Hermes REPL chats currently running in ``session``.

    One pane resolution per tty through the canonical matcher — a pane
    contributes the profile the matcher resolved for it (wrapper-safe),
    never a per-line guess (GAP-C-3)."""
    r = run_tmux("list-panes", "-t", f"={session}", "-F", "#{pane_tty}")
    if r.returncode != 0:
        return set()
    found = set()
    for tty in r.stdout.split():
        prof = pane_profile(run_ps(tty).splitlines())
        if prof:
            found.add(prof)
    return found


def _live_profile_panes(profile: str | None) -> list:
    """Panes in ANY tmux session already running ``profile``'s REPL chat.

    Each entry is ``(session_name, pane_id)``. The operator's REPLs often live
    in an ad-hoc session (e.g. ``0``), not in ``hscc-<project>``; opening a
    second chat there would give the iOS path two panes for one profile, and
    repl_inject refuses ambiguity — so the live pane must be reused first.

    The per-pane resolution is the SAME call repl_inject uses for its own pane
    discovery (:func:`pane_profile`), so "which panes run profile X" has one
    answer repo-wide — including a wrapper pane, which the old per-line copy
    classified as ``default`` and therefore nearly double-opened (GAP-C-3).
    """
    if not profile:
        return []
    r = run_tmux("list-panes", "-a", "-F",
                 "#{session_name}\t#{pane_id}\t#{pane_tty}")
    if r.returncode != 0:
        return []
    found = []
    for line in r.stdout.splitlines():
        try:
            session, pane_id, tty = line.split("\t")
        except ValueError:
            continue
        if pane_profile(run_ps(tty).splitlines()) == profile:
            found.append((session, pane_id))
    return found


def _ensure_tmux_session(project: str, argv: list, which) -> str | None:
    """Guarantee the named session hosts THIS chat; return a branch-C reason
    instead, if it cannot.

    ``None`` means: session ``hscc-<project>`` now holds (or already holds) a
    REPL chat for this chat's profile — the caller just attaches. Existing
    chats are REUSED (attaching to the live REPL is the whole point of
    one-REPL-as-source-of-truth; a second chat on the same session would be
    refused by hermes' session lease anyway):

      * session missing              -> create it detached running the chat
        (detached first so a broken server is a NAMED reason, never a dead
        exec);
      * session holds this profile's chat -> reuse, attach only;
      * session holds ANOTHER profile's chat -> reason (never merge projects);
      * session exists, no chat in it  -> open the chat as a new window.
    """
    if not which("tmux"):
        return "tmux is not installed (brew install tmux)"
    session = session_name(project)
    try:
        want = target_profile(argv)
    except PaneMatcherMissing as exc:
        return str(exc)
    if run_tmux("has-session", "-t", f"={session}").returncode == 0:
        try:
            profiles = _session_profiles(session)
        except PaneMatcherMissing as exc:
            return str(exc)
        if profiles and want not in profiles:
            other = ", ".join(sorted(profiles))
            return (f"a session {session} exists but its REPL is for another "
                    f"profile ('{other}', this chat is '{want}')")
        if not profiles:
            # Named session without a chat (plain shell, or the chat exited):
            # open the chat as a NEW WINDOW inside it — one command always
            # opens the chat; silently attaching to a chat-less session is a
            # no-op the operator would misread as success.
            r = run_tmux("new-window", "-t", f"={session}", "-P", "-d",
                         "--", *argv)
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "").strip()
                return f"tmux server failed to start: {err}"
        return None
    r = run_tmux("new-session", "-d", "-s", session, "--", *argv)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        return f"tmux server failed to start: {err}"
    return None


# --------------------------------------------------------------------------- #
# The one entry point every interactive chat launch uses
# --------------------------------------------------------------------------- #

def launch_interactive_chat(project: str, argv: list, *,
                            exec_seam=None, env=None, which=shutil.which,
                            is_tty=None, ask=None) -> int:
    """Open an interactive hermes chat tmux-aware (branches A/B/C, see module
    docstring). Returns non-zero ONLY on refusal (branch C answered N or no
    TTY); every accepted path replaces the process and never returns.

    ``exec_seam`` keeps the os.execvp (file, argv) contract so existing
    command tests can capture the launch instead of dying.
    """
    env = os.environ if env is None else env
    exec_fn = exec_seam or exec_chat
    tty = sys.stdin.isatty() if is_tty is None else is_tty
    ask_fn = ask or prompt_yes_no

    # A. Already in tmux: this pane is exactly where the chat should open.
    if env.get("TMUX"):
        exec_fn("hermes", list(argv))
        return 0

    # B/C. Decide whether tmux can host it.
    reason = None
    live = []
    if which("tmux"):
        try:
            live = _live_profile_panes(target_profile(argv))
        except PaneMatcherMissing as exc:
            # The pane rule itself is unanswerable (hscc-api not deployed
            # beside hscc-project). Never fall back to a private predicate:
            # the operator hears the real reason and decides (branch C).
            reason = str(exc)
        if reason is None and len(live) == 1:
            # Reuse the operator's existing REPL wherever it lives; never
            # start a second chat for the same profile.
            session, pane_id = live[0]
            run_tmux("select-window", "-t", pane_id)
            run_tmux("select-pane", "-t", pane_id)
            print(f"project chat: this REPL is already open in tmux session "
                  f"'{session}' — attaching to it.")
            exec_fn("tmux", ["tmux", "attach-session", "-t", f"={session}"])
            return 0
        if len(live) > 1:
            where = ", ".join(f"{s}:{p}" for s, p in live)
            reason = (f"{len(live)} tmux panes already run this profile's REPL "
                      f"({where}); close the extras")
    if reason is None:
        reason = _ensure_tmux_session(project, argv, which)
    if reason is None:
        session = session_name(project)
        # The chat opens in the named session, not in this terminal — say so,
        # or a first-time operator watches their shell "do nothing" and
        # assumes the command failed.
        print(f"project chat: opening in tmux session '{session}' — "
              f"attach from anywhere with:  tmux attach -t {session}")
        exec_fn("tmux", ["tmux", "attach-session", "-t", f"={session}"])
        return 0

    # C. tmux unavailable for a NAMED reason: ask, never silently fall back.
    ask_text = (f"project chat: cannot open this chat in tmux — {reason}\n"
                f"{CONSEQUENCE} Open anyway? [y/N] ")
    if not tty:
        print(f"project chat: refused (not a terminal to ask) — {reason}\n"
              f"{CONSEQUENCE} Not opening.", file=sys.stderr)
        return 1
    if not ask_fn(ask_text):
        # No canned advice here: `reason` already names the right fix per
        # case (brew install / close the foreign REPL / fix the server).
        print("project chat: refused — nothing opened.")
        return 1
    exec_fn("hermes", list(argv))
    return 0
