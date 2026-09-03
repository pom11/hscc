"""HSCC HTTP API — Phase C2: orchestrator chat (job-based).

The conversational endpoint: talk to a project's orchestrator DIRECTLY,
bypassing Telegram. Unlike the deterministic structured ops (A2-A4), this is
the one endpoint where an operator can say "go build X" and the orchestrator
decomposes it and dispatches real work onto its board. Because it can cause an
orchestrator to dispatch real work, it is a *mutation* and therefore REQUIRES
``"confirm": true`` (409 otherwise) — the same gate as every other mutating
endpoint.

Project → orchestrator resolution reuses C1's ``hscc-roles/orchestrators.py``
resolver (``resolve_orchestrator``), which maps project → ``<project>-orch`` /
``<project>`` session / board / repo and the catch-all ``general`` →
``general-orch`` / ``general`` / ``default``. We load that sibling plugin via
the same sys.path pattern the API uses everywhere (routes_cluster loads the
cluster engine, routes_project loads hscc-project). C1 shipped on an unmerged
branch, so ``orchestrators.py`` is vendored into ``hscc-roles/`` verbatim.

Transport (documented decision — see module docstring of the backing fn): we
shell Hermes directly, ``hermes -p <profile> chat -Q --continue <session>
-q <prompt>``, passing argv as a LIST (never string-interpolating the user's
prompt into a shell command) with a hard timeout. ``chat --continue`` resolves
the named session by title from the profile's state.db and persists the
exchange into it — the Telegram-topic analog. The localhost:4000 proxy is
litellm, a stateless OpenAI-compatible inference relay with no Hermes session
store / profile / memory; it cannot bind a named session, so it cannot satisfy
the session-continuity requirement.

Why job-based (Phase 1 — do NOT fake streaming):
``hermes chat -Q`` emits the reply ONCE, complete, when the underlying
``run_conversation`` returns (see cli.py:17858-17896 — quiet mode sets
``stream_delta_callback = None`` and ``print(response)`` once). There is NO
supported incremental/output-streaming interface on the ``hermes chat`` CLI
(no ``--stream`` / SSE / ndjson flag; the CLI's streaming is interactive
display chrome, gated on ``display.streaming`` which is off, and routes
through ANSI ``_cprint`` box drawing — not a machine-readable token stream).
So token streaming is NOT available, and faking it would mislead the operator.

The honest fix is a JOB API (option b):
  * ``POST /v1/orchestrator/chat`` VALIDATES + RESOLVES the project, spawns a
    background thread that actually invokes hermes, and returns IMMEDIATELY
    (202) with a ``job_id`` + ``status`` — no more dead-wait on the phone.
  * ``GET /v1/orchestrator/chat/{id}`` polls the job: ``queued`` / ``running``
    / ``done`` (with the reply) / a terminal error state, plus honest
    ``elapsed`` seconds.
This ALSO fixes a real failure mode: a dropped connection no longer loses a
90 s answer the server already computed — the background thread finishes and
the phone (or a backgrounded app) can pick it up by job_id later.

Timeout semantics (t_023d4c4c): because a job holds NO connection open, a short
"reply latency" timeout is pointless — the honest model is "report elapsed and
let the operator decide". The worker therefore runs the orchestrator under a
generous WEDGE-backstop :data:`_DEFAULT_TIMEOUT` (600s default, configurable as
``chat_timeout`` seconds in ``~/.hscc/api.json`` — see ``_chat_timeout``) that
only fires when the ``hermes chat`` process is genuinely hung, not merely slow.
Every terminal job (``done`` or a failure state) reports ``elapsed`` FROZEN at
its termination instant (``finished_at - submitted_at``), so status, message
and elapsed always agree (the original 180s-vs-2972.7s contradiction).

Busy-reporting (t_5ed5dfa8 Bug 1): when kanban workers are running on the SAME
orchestrator profile, the chat can wedge in the Hermes agent layer (shared
named session + ``journal_mode=delete`` state.db contention → compaction storm,
silent 600s timeout). We don't hide that behind a higher timeout; at submit the
handler counts ``profile``'s running board tasks (``_backing_busy_tasks``) and,
when non-zero, returns an honest ``notice`` ("<profile> is busy with N kanban
tasks...") on the POST AND on GET polls of a LIVE job. The notice is DROPPED
once the job reaches a terminal state (t_a8e9b7ff): a finished job must never
carry "busy right now ... poll this job" — the outcome is described by
status/reply/error. The verified job-status state machine
(queued/running/done/error) is unchanged.

Session-bloat guard (t_a8e9b7ff Bug 1): each project's chat uses ONE long-lived
named session that grows forever until compaction stops being able to recover
and the chat wedges (the 14.2M-token / 278-message ``hscc`` session timed out
at 600s while a fresh session answered in 1.777s). ``_guard_session_bloat``
runs at chat POST time — before the job continues the named session — reads the
real signals on the session row (``input_tokens``, ``compression_failure_error``,
``compression_fallback_streak``, ``compression_ineffective_count``) and, when
the session is bloated, ROTATES it: retitles the old session to
``<project>-retired-<ts>`` (non-destructive, kept on disk) and creates a fresh
session titled ``<project>`` (via Hermes' own SessionDB, so ``--continue
<project>`` resolves to the fresh row). Per-project continuity is preserved —
profile ``<project>-orch``, session ``<project>``, board ``<project>`` are all
untouched as identities; only the session's accumulated context is reset.

Preamble stripping (t_5ed5dfa8 Bug 2): ``hermes chat -Q`` emits a few one-line
startup notices to stdout before the reply (the cwd-restore notice, the tirith
security warning, and defensively the resume banner). ``_backing_invoke`` strips
them so ``reply`` carries only the model's answer — anchored on the exact known
shapes so a legitimate reply that merely looks like a warning is never stripped.
"""
from __future__ import annotations

import base64
import itertools
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from api_server import ApiError, ROUTES

# --------------------------------------------------------------------------- #
# C1 resolver loading (same sys.path pattern as every other sibling plugin)
# --------------------------------------------------------------------------- #

_ROLES_DIR = None
for _candidate in (
    Path(__file__).resolve().parent.parent / "hscc-roles",  # live checkout
):
    if _candidate.is_dir():
        _ROLES_DIR = _candidate
        break
if _ROLES_DIR is not None and str(_ROLES_DIR) not in sys.path:
    sys.path.insert(0, str(_ROLES_DIR))

from orchestrators import (                          # noqa: E402
    OrchestratorError,        # base error (e.g. project has no board)
    UnknownProjectError,      # project neither in registry nor 'general'
    resolve_orchestrator,     # project -> {profile, session, board, repo}
)

# The compaction token cap is the SINGLE source of truth held in the roles
# generator (hscc-roles/generator.py), which the bootstrap producer uses to
# mint profiles. The API-side ensure imports it from there so the two sides
# can never drift apart — a diverged literal is exactly the bug that let
# bootstrap silently null threshold_tokens on every profile (2026-08-27).
from generator import SESSION_COMPACTION_THRESHOLD_TOKENS  # noqa: E402

# Registry path is shared with routes_project (config-driven).
from routes_project import _registry_path             # noqa: E402

# Default: how long we wait for the orchestrator to reply before giving up.
#
# WHY 600s and not 180s (t_023d4c4c): the orchestrator answers a trivial prompt
# in ~17s when the fleet is IDLE, but >165s while even three kanban workers are
# running — and "busy" is this cluster's NORMAL state, not the exception. The
# old 180s sat barely above loaded latency, so normal busy-cluster chats timed
# out (the reported 2972.7s / 180s contradiction came from poll-side elapsed
# drift, fixed separately — see _job_dict). A job does NOT hold a connection
# open (the POST returns in ms and the phone polls by job_id), so a short
# "reply latency" budget serves no purpose; the honest model is "report elapsed
# and let the operator decide". This value is therefore a pure WEDGE-backstop:
# it should only fire when the underlying `hermes chat` process is genuinely
# hung (deadlocked / wedged), never merely slow. 600s = 10 min, ~3.5x the
# measured 165s loaded case — room for "busy", yet still bounds a truly stuck
# process so an orphan can't burn GPU forever. Tune via ``chat_timeout`` (float
# seconds) in ``~/.hscc/api.json`` — see ``_chat_timeout``.
_DEFAULT_TIMEOUT = 600.0   # seconds (configurable backstop for a WEDGED process)


def _chat_timeout(ctx) -> float:
    """Resolve the orchestrator-chat timeout for this server's config.

    Precedence (lowest -> highest): :data:`_DEFAULT_TIMEOUT` -> ``chat_timeout``
    in ``~/.hscc/api.json`` -> ``chat_timeout`` on the resolved config dict.
    Mirrors the ``registry`` precedence in :func:`routes_project._registry_path`.
    A non-numeric / non-positive value is a user CONFIG error: raise (matching
    api_server's hard-error-on-malformed-config stance) rather than silently
    guess.
    """
    raw = None
    from_config = getattr(ctx, "config", None)
    if from_config and from_config.get("chat_timeout") is not None:
        raw = from_config["chat_timeout"]
    else:
        hscc_dir = getattr(ctx, "hscc_dir", None) or "~/.hscc"
        cfg_path = Path(hscc_dir).expanduser() / "api.json"
        if cfg_path.exists():
            try:
                import json
                data = json.loads(cfg_path.read_text())
                if isinstance(data, dict) and data.get("chat_timeout") is not None:
                    raw = data["chat_timeout"]
            except (OSError, ValueError):
                pass  # fall through to the default
        else:
            raw = None
    if raw is None:
        return _DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"invalid 'chat_timeout' {raw!r} in api config — expected seconds"
        )
    if value <= 0:
        raise RuntimeError(
            f"invalid 'chat_timeout' {value!r} — must be a positive number of seconds"
        )
    return value


# --------------------------------------------------------------------------- #
# Backing-call seam (monkeypatch these in tests — no test spawns a real agent)
# --------------------------------------------------------------------------- #

def _backing_resolve(project, registry_path):
    """Resolve project -> orchestrator identity via C1's resolver.

    ``project`` may be None -> the ``general`` orchestrator. Raises
    :class:`UnknownProjectError` / :class:`OrchestratorError` for unresolvable
    inputs (the handler maps them to 400).
    """
    return resolve_orchestrator(project, path=registry_path)


def _stderr_tail(err: str, max_chars: int = 300) -> str:
    """A bounded, human-readable tail of the failing process's REAL stderr.

    We never paste unbounded stderr (a stack trace can be huge and may carry
    an internal path we don't want to hand the client raw), but dropping it
    entirely is exactly the disaster this card fixes (every real failure
    reported as a fabricated "session not ready"). Take the last few
    non-empty lines, collapse to a single quoted line, cap the length. Falls
    back to a plain description when there is nothing to show.
    """
    if not err:
        return "stderr tail: (no stderr captured)"
    lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
    tail = " | ".join(lines[-3:])
    if len(tail) > max_chars:
        tail = tail[-max_chars:] + "…"
    return f'stderr tail: "{tail}"'


def _backing_invoke(profile, session, prompt, timeout=_DEFAULT_TIMEOUT,
                    image_data=None, image_mime=None, cancel_evt=None,
                    on_spawn=None):
    """Send a prompt to an orchestrator and return its reply.

    The transport: shell Hermes headlessly as the orchestrator profile, in the
    profile's NAMED session, quiet mode so the reply is the only thing on
    stdout:

        hermes -p <profile> chat -Q --continue <session> -q <prompt>

    argv is passed as a LIST — the user's prompt is a plain element, never
    interpolated into a shell string (no shell-injection). ``--continue
    <session>`` resolves the session by title from the profile's state.db and
    persists this exchange into it, preserving the thread (the Telegram-topic
    analog). Quiet mode (``-Q``) keeps status/banner lines on stderr so stdout
    is machine-readable.

    When ``image_data`` (raw bytes) + ``image_mime`` are supplied (t_a779c06f),
    the bytes are written to a temp file and passed as ``--image <path>`` so the
    orchestrator can SEE the attached screenshot/photo. The temp file is always
    removed afterwards (try/finally) — even on failure — so a background job
    never leaks a copy of the image on disk.

    Cancellation (t_68432c2d): the invocation uses ``subprocess.Popen`` (NOT a
    blocking ``subprocess.run``) so the underlying process can be stopped
    mid-flight. Two cooperating hooks:

      * ``cancel_evt`` — a ``threading.Event``. When set, :func:`_backing_invoke`
        terminates the running process (SIGTERM, then SIGKILL after a grace)
        and raises :class:`_OrchestratorCancelled`. The job/relay maps that to
        the terminal ``stopped`` state.
      * ``on_spawn`` — an optional callback ``on_spawn(proc)`` invoked the
        moment the Popen is created, so the caller (the job) can RETAIN the
        handle for an immediate out-of-band ``proc.terminate()``/``proc.kill()``
        (the stop path this card's tests cover). When ``cancel_evt`` is None
        (the non-cancellable fallback), the invoke still polls nothing and runs
        to completion exactly as before.

    Returns ``(reply_text, profile, session)``. Raises:
      * ``_OrchestratorTimeout`` when the reply exceeds ``timeout``;
      * ``_OrchestratorCancelled`` when ``cancel_evt`` fired mid-flight;
      * ``_OrchestratorUnavailable`` when the profile/session cannot be
        reached (e.g. no matching session yet, or hermes not installed).
      * ``_OrchestratorInvocationError`` on any other failed invocation
        (nonzero exit / unparsable output).
    """
    argv = ["hermes", "-p", profile, "chat", "-Q", "--continue", session,
            "-q", prompt]
    tmp_path = None
    try:
        if image_data is not None:
            # Write the decoded attachment to a temp file so `--image` has a
            # real path for hermes to read. Keep the suffix aligned with the
            # MIME so the model / downstream decode sees a correct extension.
            tmp_path = tempfile.mkstemp(
                prefix="hscc-chat-", suffix=_image_suffix(image_mime)
            )[1]
            with open(tmp_path, "wb") as f:
                f.write(image_data)
            argv += ["--image", tmp_path]

        # Popen (not blocking subprocess.run) so a stop can retain + kill the
        # handle. Command stays a LIST — no shell interpolation of the prompt.
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except (FileNotFoundError, OSError) as exc:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise _OrchestratorUnavailable(
            f"cannot invoke `hermes`: {exc!r}"
        )

    # Hand the live Popen to the caller so a stop can terminate/kill it
    # directly (the retained-handle this card exists to provide).
    if on_spawn is not None:
        try:
            on_spawn(proc)
        except Exception:   # never let a retention callback break the invoke
            pass

    try:
        out, err = _run_proc(proc, timeout, cancel_evt)
    except _OrchestratorCancelled:
        _terminate_proc(proc)
        raise
    except _OrchestratorTimeout:
        _terminate_proc(proc)
        raise
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass   # best-effort cleanup; never mask the real outcome

    err = (err or "").strip()
    # A clean "no such session yet" failure — the orchestrator's named session
    # must exist before it can be continued (created by provisioning / first
    # Telegram topic). Surface it honestly rather than synthesising a reply.
    # This is the ONLY stderr signal that means "session not ready": a missing
    # session is reported verbatim and is checkable. It is checked FIRST so
    # that no OTHER nonzero exit can be misreported as a missing session.
    if "Session not found" in err:
        raise _OrchestratorUnavailable(
            f"orchestrator session {session!r} not ready "
            f"(create it first, then re-send)"
        )

    if proc.returncode != 0:
        # A cancellation (not a failure) if the stop event fired — the process
        # was terminated externally (SIGTERM/SIGKILL), so this nonzero exit is
        # the operator's doing, never an invocation error.
        if cancel_evt is not None and cancel_evt.is_set():
            raise _OrchestratorCancelled("chat turn cancelled by operator")
        # A nonzero exit that is NOT a missing-session signal. This is where
        # every real failure lands — model unreachable, an internal hermes
        # error, a crash. It must NAME ITSELF with the actual stderr tail so
        # the operator (and reviewer) see what truly happened, instead of the
        # fabricated "session 'hscc' not ready" that sent both chasing a
        # non-existent bug while the session existed. Selecting a session is
        # impossible here (the process already failed), so this is an
        # invocation error, not an unavailable session.
        raise _OrchestratorInvocationError(
            f"orchestrator {profile!r} invocation failed (exit "
            f"{proc.returncode}): {_stderr_tail(err)}"
        )

    reply = (out or "").strip()
    # ``-Q`` still lets a couple of harmless one-line notices through to stdout
    # BEFORE the reply (observed empirically, t_bc242def Phase 1) — e.g. the
    # cwd-restore line ``↪ restored workspace dir: <path>``. Strip those so the
    # answer we parse is never polluted by a notice.
    reply = "\n".join(
        ln for ln in reply.splitlines() if not _is_notice_line(ln)
    ).strip()
    if not reply:
        raise _OrchestratorInvocationError(
            f"orchestrator {profile!r} returned an empty reply"
        )
    return reply, profile, session


def _run_proc(proc, timeout: float, cancel_evt=None):
    """Notify-compatible run of ``proc`` that polls the cancel event.

    Replaces the previous blocking ``subprocess.run`` wait with a Popen wait
    loop that checks ``cancel_evt`` on a short interval, so a stop can interrupt
    promptly instead of waiting out the whole timeout. On the timeout it
    terminates the process and raises :class:`_OrchestratorTimeout`; on cancel
    it terminates and raises :class:`_OrchestratorCancelled`.

    Returns ``(out, err)`` — the drained stdout/stderr strings — so the caller
    never inspects ``proc.stdout``/``proc.stderr`` (which remain file wrappers
    until closed). This mirrors what the old ``subprocess.run(capture_output=…)``
    produced.
    """
    start = time.time()
    poll = 0.25
    while True:
        if cancel_evt is not None and cancel_evt.is_set():
            _terminate_proc(proc)
            raise _OrchestratorCancelled("chat turn cancelled by operator")
        remaining = timeout - (time.time() - start)
        if remaining <= 0:
            _terminate_proc(proc)
            raise _OrchestratorTimeout(f"orchestrator did not reply within {timeout:.0f}s")
        try:
            proc.wait(timeout=min(poll, remaining))
            break                                  # exited cleanly/internally
        except subprocess.TimeoutExpired:
            continue
    out, err = proc.communicate()   # drain remaining buffered stdout/stderr
    if out is None:
        out = ""
    if err is None:
        err = ""
    return out, err


def _terminate_proc(proc) -> None:
    """Best-effort terminate (SIGTERM) then kill (SIGKILL) a running process.

    Core of the stop path: a cancellable turn must be able to end the running
    ``hermes chat`` immediately, not wait it out. SIGTERM first (a graceful
    shutdown hermes can catch), then SIGKILL after a short grace for anything
    that ignores SIGTERM. Idempotent and never raises — terminating an already
    exited process is a no-op.
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass


def _backing_busy_tasks(profile: str) -> int:
    """Count running kanban tasks assigned to ``profile`` across ALL boards.

    This is the contention signal for Bug 1 (t_5ed5dfa8). Every HSCC chat runs
    ``hermes -p <profile> chat -Q --continue <session>`` against the profile's
    named session — the SAME session and profile through which the project's
    kanban workers are also running. When ``profile`` has ``n`` running batch
    tasks, the interactive chat competes for the same agent layer / the same
    per-profile ``state.db`` (which is pinned to ``journal_mode=delete`` by the
    WAL-reset-vulnerability guard — see the warning in agent.log), so it can
    wedge in the agent layer (a shared-session context-compaction storm on a
    busy profile → the silent 600s timeout the card measured). This count is
    how the API reports that contention honestly instead of timing out silently.

    Reuses Hermes' OWN ``list_boards()`` enumeration — the same seam
    flightdeck/core/kanban.py::list_boards and hscc_daemon.autodown use — and
    scans each board's ``tasks`` for ``status='running'`` rows whose assignee
    is ``profile``. Fail-SAFE: any board we cannot enumerate/read is counted as
    BUSY (+1) rather than silently skipped — behaving as idle on an
    unresolvable signal is a step back toward the silent timeout.
    """
    try:
        from hermes_cli import kanban_db
        import sqlite3
        boards = kanban_db.list_boards()
    except Exception:
        # Cannot verify the profile is idle — fail safe toward busy (>=1).
        return 1
    count = 0
    for board in boards:
        db_path = board.get("db_path")
        if not db_path:
            count += 1   # no DB path to inspect — fail safe
            continue
        try:
            conn = sqlite3.connect(str(db_path), timeout=1.0)
        except Exception:
            count += 1   # fail safe: cannot verify idle
            continue
        try:
            rows = conn.execute(
                "SELECT 1 FROM tasks "
                "WHERE status='running' AND assignee=? LIMIT 1",
                (profile,),
            ).fetchone()
            if rows is not None:
                count += 1
        except Exception:
            count += 1   # fail safe: unreadable board => not provably idle
        finally:
            conn.close()
    return count


def _busy_notice(profile: str, count: int) -> str:
    """An honest, operator-facing notice that the orchestrator is busy.

    Bug 1 fix (t_5ed5dfa8): name the contention instead of a silent 600s
    timeout. The insight the card proved — a queued message the operator
    understands beats a timeout they cannot explain. ``count`` is the number of
    running kanban tasks on ``profile`` (>=1); if it is 1 (only this chat's own
    profile has no other work) the notice reads naturally singular.
    """
    n = int(count)
    task_word = "task" if n == 1 else "tasks"
    return (
        f"{profile} is busy right now (with {n} kanban {task_word} running). "
        f"Your question is queued behind that work and may take a while; "
        f"poll this job for the answer."
    )


# --------------------------------------------------------------------------- #
# Session-bloat guard (t_a8e9b7ff Bug 1): rotate a chat session approaching the
# context ceiling BEFORE it wedges, preserving per-project continuity.
# --------------------------------------------------------------------------- #

# The orchestrator model's context window (the "context ceiling" the card
# means). Measured from the live strong-tier endpoint: ``orchestrator-model``
# resolves to 262144 tokens (same value the hermes-agent compressor resolves;
# the hscc-roles generator's COMPACT_THRESHOLD comment cites "~210K of 262K").
# Reported on ``session_health`` for operator transparency; it is NOT a rotation
# trigger (see below — rotation fires only on positive compaction-failure
# evidence, never on raw size).
_ORCH_CONTEXT_WINDOW = 262144

# The proactive context-health mechanism (operator decision, t_a8e9b7ff): make
# Hermes' NATIVE compaction fire EARLY so there is always ~2x headroom for the
# compression call itself. Hermes triggers compaction at the lower of the
# ratio-based threshold and this ABSOLUTE token cap
# (agent/agent_init.py:1946-1953 reads ``compression.threshold_tokens``, then
# ``context_compressor._apply_threshold_tokens_cap`` clamps it DOWNWARD only —
# ``min(cap, context_length)``, and never fires LATER than this cap). Without
# the cap, the ratio path floors at 0.75 x 262144 = 196608 — the exact
# active-token value the wedged ``hscc`` run died at, where the compression
# call (196K + summarizer overhead) had no headroom and retried with growing
# inputs (196609 -> 196804) until the 600s timeout. Setting the cap to 100000
# makes compaction fire at ~100K active — comfortably inside the window with
# headroom to spare — so continuity is PRESERVED (same session id, same history
# via the summary) instead of wedging. ``_ensure_compaction_threshold`` writes
# this key onto every resolved ``<project>-orch`` profile, idempotently.
# SESSION_COMPACTION_THRESHOLD_TOKENS is imported from the roles generator
# (hscc-roles/generator.py), the single source of truth shared with the
# bootstrap producer, so the two sides cannot drift (see import above).


def _session_guard_config(ctx) -> tuple:
    """Resolve the session-guard parameters for this server's config.

    Returns ``(enabled, context_window)``. Precedence mirrors the
    ``chat_timeout`` / ``registry`` pattern: ``session_guard`` dict on the
    resolved config object, then ``session_guard`` in ``~/.hscc/api.json``, then
    the module defaults. ``context_window`` is REPORTED on ``session_health``
    (operator transparency) but does not drive rotation — rotation is decided
    solely on positive compaction-failure evidence. Malformed values are a hard
    config error (matching the API's hard-error-on-malformed-config stance),
    never a silent guess.
    """
    # defaults
    enabled = True
    context_window = _ORCH_CONTEXT_WINDOW

    raw = None
    from_config = getattr(ctx, "config", None)
    if from_config and isinstance(from_config.get("session_guard"), dict):
        raw = from_config["session_guard"]
    else:
        hscc_dir = getattr(ctx, "hscc_dir", None) or "~/.hscc"
        cfg_path = Path(hscc_dir).expanduser() / "api.json"
        if cfg_path.exists():
            try:
                import json
                data = json.loads(cfg_path.read_text())
                if isinstance(data, dict) and isinstance(data.get("session_guard"), dict):
                    raw = data["session_guard"]
            except (OSError, ValueError):
                raw = None
    if isinstance(raw, dict):
        if raw.get("enabled") is not None:
            enabled = bool(raw["enabled"])
        if raw.get("context_window") is not None:
            try:
                context_window = int(raw["context_window"])
            except (TypeError, ValueError):
                raise RuntimeError(
                    f"invalid session_guard.context_window {raw['context_window']!r} — "
                    "expected an integer number of tokens"
                )
            if context_window <= 0:
                raise RuntimeError(
                    "invalid session_guard.context_window — must be > 0"
                )
    return enabled, context_window


def _open_profile_session_db(profile: str, read_only: bool = False):
    """Open the orchestrator profile's ``state.db`` via Hermes' own SessionDB.

    Returns a ``SessionDB`` bound to ``<profile>``'s state.db (the same DB the
    ``hermes -p <profile> chat --continue`` invocation reads and writes), or
    ``None`` when the profile is unresolvable / has no state.db (fail-safe: the
    caller then skips the guard rather than guessing). We resolve the profile
    directory through ``hermes_cli.profiles`` exactly like the session-search
    tool does, so reads always target the RIGHT profile — never the API process's
    own default profile.

    ``read_only=True`` opens the DB in read-only mode (no write lock) — the
    safe choice for surfacing health (``_session_health``), so a busy profile's
    state.db is never contended for a mere status read. The rotation path
    (``_guard_session_bloat``) passes ``read_only=False`` (the default) because
    it must retitle + create.
    """
    try:
        from hermes_cli import profiles as profiles_mod
        from hermes_state import SessionDB
        from pathlib import Path as _Path
        canon = profiles_mod.normalize_profile_name(profile)
        profiles_mod.validate_profile_name(canon)
        if not profiles_mod.profile_exists(canon):
            return None
        db_path = _Path(profiles_mod.get_profile_dir(canon)) / "state.db"
        if not db_path.exists():
            return None
        return SessionDB(db_path=db_path, read_only=read_only)
    except Exception:
        return None


def _ensure_compaction_threshold(profile: str) -> dict:
    """ENSURE the profile's ``compression.threshold_tokens`` makes native
    compaction fire EARLY (the PRIMARY context-health mechanism, t_a8e9b7ff).

    The operator's decision: compaction is the goal, rotation only the last
    resort. Compaction's trigger is moved EARLIER declaratively — by writing
    ``compression.threshold_tokens = 100000`` (see
    :data:`SESSION_COMPACTION_THRESHOLD_TOKENS`) onto the resolved ``<project>-orch``
    profile's ``config.yaml`` — so Hermes' next chat turn compacts at ~100K
    active in the 262K window (headroom for the compression call itself)
    instead of at the 196608 ratio floor where it wedges. Continuity is fully
    preserved: same session id, same history via the summary — no rotation.

    Idempotent + non-destructive: if the profile already has
    ``compression.threshold_tokens`` set to a value <= the constant (an
    operator-set or previously-ensured value), we do NOT clobber it — a lower
    cap is strictly better, and our own uniform 100000 is a harmless no-op on
    any window (``_apply_threshold_tokens_cap`` takes ``min(cap,
    context_length)``, so it can only LOWER the trigger, never raise it).

    We write the profile's OWN ``config.yaml`` directly (via an atomic YAML
    read-modify-write through the same ``utils.atomic_yaml_write`` the CLI's
    ``config set`` uses), keyed on the profile's resolved home — we never touch
    the global ``HERMES_HOME`` env in this multithreaded server, so concurrent
    chats to different projects can't race each other's config. The write goes
    through the same file ``hermes -p <profile> config set ...`` writes, which
    ``agent_init`` (``load_config`` -> ``_compression_cfg.get(\"threshold_tokens\")``)
    reads on the next ``hermes -p <profile> chat`` invocation.

    Returns a dict describing what was done (``profile``, ``threshold_tokens``,
    ``previous``, ``set``), or ``None`` when the profile is unresolvable /
    already correctly configured / the guard couldn't run (FAIL-SAFE: any
    error leaves the profile untouched and the chat proceeds as before).
    """
    import yaml
    try:
        from hermes_cli import profiles as profiles_mod
        from utils import atomic_yaml_write
    except Exception:
        return None

    try:
        canon = profiles_mod.normalize_profile_name(profile)
        profiles_mod.validate_profile_name(canon)
        if not profiles_mod.profile_exists(canon):
            return None
        cfg_path = profiles_mod.get_profile_dir(canon) / "config.yaml"
    except Exception:
        return None

    try:
        cur = {}
        if cfg_path.exists():
            with open(cfg_path, encoding="utf-8") as f:
                cur = yaml.safe_load(f) or {}
        if not isinstance(cur, dict):
            cur = {}
        comp = cur.get("compression")
        prev = None
        if isinstance(comp, dict):
            prev = comp.get("threshold_tokens")
        # No-op when an operator value already <= the constant is present: a
        # lower cap is strictly better (compaction can only fire earlier) and
        # we must never clobber it.
        if isinstance(prev, (int, float)) and not isinstance(prev, bool) \
                and prev <= SESSION_COMPACTION_THRESHOLD_TOKENS:
            return None
        if not isinstance(comp, dict):
            comp = {}
        comp["threshold_tokens"] = SESSION_COMPACTION_THRESHOLD_TOKENS
        cur["compression"] = comp
        atomic_yaml_write(cfg_path, cur, sort_keys=False)
        return {
            "profile": profile,
            "threshold_tokens": SESSION_COMPACTION_THRESHOLD_TOKENS,
            "previous": prev,
            "set": True,
        }
    except Exception as exc:  # noqa: BLE001 — fail-safe: never break a chat
        logging.getLogger("hscc-api").error(
            "could not ensure compression.threshold_tokens on %s (chat "
            "proceeds on existing config): %r", profile, exc,
        )
        return None


def _ensure_role_profiles() -> dict:
    """ENSURE ``compression.threshold_tokens`` on every ROLE profile (t_c03fd5ae).

    v1.14.0 shipped :func:`_ensure_compaction_threshold` covering only the
    resolved ``<project>-orch`` profiles. But kanban workers and subagents run
    under ROLE profiles (coder, reviewer, worker, ios-engineer, qa,
    backend-engineer, ...) and wedge at the same 196608 ratio floor. The
    operator set all 26 role profiles to 100000 BY HAND; nothing in code held
    them there, so a newly created or reset role profile silently regressed to
    the wedge. This sweep holds every role profile at
    :data:`SESSION_COMPACTION_THRESHOLD_TOKENS` declaratively, so NEW profiles
    are covered on the next run.

    A role profile is ANY profile under the profile root that is not a
    ``<project>-orch`` orchestrator (name does not end with ``-orch``). We
    discover the set via ``hermes_cli.profiles.list_profiles()`` — the same
    enumeration the CLI uses — so the current 26 names are never hardcoded and
    new profiles are picked up automatically. ``default`` counts as a role
    profile (it is not an orchestrator and wedges the same way).

    Each profile runs through the SAME :func:`_ensure_compaction_threshold`,
    so the idempotence rule is identical: a value already <= the constant
    (operator-set or previously ensured) is never clobbered. FAIL-SAFE: any
    error here — on the enumeration or on an individual profile — is caught and
    logged, never propagated; a profile we cannot reach is skipped and the rest
    still get ensured. The orch profiles are skipped by this sweep because they
    are already covered by the per-project path; their behaviour is untouched.

    Returns a summary dict: ``role_profiles`` (total considered), ``set``
    (names the threshold was written to), ``unchanged`` (already ensured /
    preserved), ``orchestrators`` (skipped orch profiles). Returns ``None`` when
    the enumeration itself fails (fail-safe — nothing is written blind).
    """
    import logging
    _log = logging.getLogger("hscc-api")
    try:
        from hermes_cli import profiles as profiles_mod
    except Exception:
        return None

    # Discover every profile, then keep only role profiles (non-orchestrators).
    # The point of discovery is that NEW profiles are covered — never hardcode
    # the current roster.
    try:
        infos = profiles_mod.list_profiles()
    except Exception as exc:  # noqa: BLE001 — fail-safe
        _log.error(
            "could not enumerate profiles for compaction-threshold ensure: %r",
            exc)
        return None

    set_, unchanged, orchestrators = [], [], []
    for info in infos:
        name = getattr(info, "name", None)
        if not isinstance(name, str) or not name:
            continue
        if name.endswith("-orch"):
            # Orchestrator: already covered by the per-project ensure path
            # (_ensure_compaction_threshold on the resolved <project>-orch).
            # This sweep deliberately does not touch them — extension, not
            # rewrite — so their behaviour stays byte-identical.
            orchestrators.append(name)
            continue
        try:
            res = _ensure_compaction_threshold(name)
        except Exception as exc:  # noqa: BLE001 — fail-safe per profile
            _log.error(
                "compaction-threshold ensure failed for role profile %s: %r",
                name, exc)
            continue
        if res is not None:
            set_.append(name)
        else:
            unchanged.append(name)

    return {
        "role_profiles": len(set_) + len(unchanged),
        "set": sorted(set_),
        "unchanged": sorted(unchanged),
        "orchestrators": sorted(orchestrators),
    }


def _session_bloat_verdict(session_row) -> tuple:
    """Decide whether a session must be ROTATED (last resort) — from POSITIVE
    compaction-failure evidence only.

    Returns ``(bloated, reason)``. Since the operator made native compaction
    the PRIMARY mechanism (see :data:`SESSION_COMPACTION_THRESHOLD_TOKENS` and
    :func:`_ensure_compaction_threshold`), rotation is demoted to a TRUE last
    resort that fires ONLY when we have positive evidence the session's
    compaction has already failed:

      * ``compression_failure_error`` set (the compressor threw — e.g. \"model
        context length exceeded\");
      * ``compression_fallback_streak >= 1`` (the compressor fell back to a
        less-effective path);
      * ``compression_ineffective_count >= 1`` (compaction ran but did not
        actually shrink the context).

    Any one of these means the session already crossed the point where another
    compaction round can recover it — continuing it is exactly the 600s wedge
    the card measured. Rotate.

    A session is NEVER rotated merely for being LARGE — ``input_tokens`` is a
    CUMULATIVE counter (``input_tokens = input_tokens + ?`` per turn in the CLI
    path, never reset by compaction), so raw size says nothing about current
    health, and with the 100K cap in place a large-but-healthy session is
    normal and must not be rotated (operator decision, t_a8e9b7ff). The ``reason``
    feeds the rotation payload / project-detail surfacing.
    """
    def _int(row, key):
        try:
            return int(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    failure_error = (session_row.get("compression_failure_error") or "").strip()
    fallback_streak = _int(session_row, "compression_fallback_streak")
    ineffective_count = _int(session_row, "compression_ineffective_count")
    if failure_error or fallback_streak >= 1 or ineffective_count >= 1:
        return True, "context compression is failing"
    return False, ""


def _rotate_session(db, profile: str, session: str) -> dict:
    """Retire the bloated session and create a fresh one titled ``session``.

    Non-destructive and reversible, exactly mirroring the operator's manual
    recovery: (1) retitle the existing ``<session>`` row to
    ``<session>-retired-<timestamp>`` (kept on disk, all messages intact);
    (2) create a fresh session via Hermes' own ``SessionDB.create_session`` and
    title it ``<session>``, so ``--continue <session>`` — the transport the
    chat job uses — resolves to the clean row. The CREATE MUST happen before
    the job's ``--continue`` (the card's ordering note: ``--continue`` does not
    create a session and the API returns ``orchestrator_unavailable`` if none
    matches) — the caller guarantees this because rotation completes before the
    job spawns its background invoke.

    ``profile`` / ``session`` are the orchestrator identity (``<project>-orch`` /
    ``<project>``). Returns a dict describing the rotation (old title, new
    session id, reason) for surfacing to the operator. Raises on failure so the
    caller can fail-safely skip; every error here leaves the ORIGINAL session
    in place (the retitle is the only mutation and it happens first, so a crash
    after retitle-but-before-create leaves NO session titled ``<project>`` — an
    ``orchestrator_unavailable`` the operator can recover from exactly as the
    card's manual flow does).
    """
    import time as _time
    import uuid as _uuid

    old_id = db.resolve_session_by_title(session)
    retired_title = f"{session}-retired-{_time.strftime('%Y%m%d-%H%M%S')}"
    if old_id and db.get_session_title(old_id) == session:
        db.set_session_title(old_id, retired_title)

    new_id = f"{_time.strftime('%Y%m%d')}_rot_{_uuid.uuid4().hex[:6]}"
    db.create_session(
        new_id,
        source="cli",
        model="orchestrator-model",
        profile_name=profile,
    )
    db.set_session_title(new_id, session)
    return {
        "retired_session": old_id,
        "retired_title": retired_title if old_id else None,
        "session": new_id,          # now the live session titled ``session``
        "created_at": new_id,
    }


def _ensure_session_exists(profile: str, session: str) -> dict | None:
    """Ensure a session titled ``session`` exists on ``profile`` — CREATE on
    first use (t_fc53b13d), idempotent, never clobbers an existing session.

    A brand-new project has NO chat session yet, so ``hermes chat -Q --continue
    <session>`` — the transport ``_backing_invoke`` uses — fails with
    ``orchestrator_unavailable`` (\"create it first, then re-send\") and the
    project can never be chatted with. This makes the chat path CREATE the
    project's session on first use: we open the profile's state.db via Hermes'
    own SessionDB (``_open_profile_session_db``) and, when no session is titled
    ``<session>``, create one (``create_session`` + ``set_session_title``) — the
    SAME machinery ``_rotate_session`` uses to hand a bloated chat a fresh row —
    so the SUBSEQUENT ``--continue <session>`` in the job resolves to it.

    Idempotent: if a session titled ``session`` already exists, we do nothing
    and return ``None`` (never clobber an existing session / its history).
    Returns a dict describing the creation (``created_session``, ``profile``,
    ``title``) for operator surfacing, or ``None`` when the session already
    existed OR could not be created (fail-safe — an ensure that can't verify
    must not break the chat; the job then goes on to fail honestly with
    ``orchestrator_unavailable`` exactly as before this fix).
    """
    db = _open_profile_session_db(profile)
    if db is None:
        # Cannot open the profile's state.db — do not guess. Return None so the
        # chat proceeds; the downstream ``--continue`` fails honestly if the
        # session truly doesn't exist yet.
        return None
    import time as _time
    import uuid as _uuid
    try:
        if db.resolve_session_by_title(session):
            return None   # already exists — never clobber
        new_id = f"{_time.strftime('%Y%m%d')}_first_{_uuid.uuid4().hex[:6]}"
        db.create_session(new_id, source="cli", model="orchestrator-model",
                          profile_name=profile)
        db.set_session_title(new_id, session)
        return {
            "created_session": new_id,
            "profile": profile,
            "title": session,
        }
    except Exception:
        # Fail-safe: an ensure that errors must not break the chat or corrupt a
        # profile. Downstream ``--continue`` will fail honestly if needed.
        return None
    finally:
        db.close()


def _guard_session_bloat(ctx, profile: str, session: str):
    """The session context-health guard, run at chat POST time.

    Two layers, per the operator decision (t_a8e9b7ff):

      1. ENSURE (primary, non-destructive): call
         :func:`_ensure_compaction_threshold` so the profile's native
         compaction triggers EARLY at ``compression.threshold_tokens = 100000``.
         This is the actual fix — it prevents the wedge by compacting long
         before the context runs out of headroom, preserving session continuity
         (no rotation, same session id, history via the summary). Idempotent;
         never clobbers an operator value already <= the constant.

      2. ROTATE (last resort, only on positive failure evidence): read the
         real compaction-failure signals on the session row and, when the
         verdict says compaction has ALREADY failed (``compression_failure_error``
         / ``compression_fallback_streak`` / ``compression_ineffective_count``),
         retire + recreate the session so the chat never wedges. NEVER rotates
         on raw size alone.

    Returns a dict describing a rotation (when one happened), or ``None`` when
    the session is healthy / the guard couldn't run. FAIL-SAFE: any error here
    results in NO rotation and the chat proceeds on the existing session
    exactly as before this guard existed — a guard that can't verify must not
    invent health, but it must also never break a working chat or corrupt a
    profile.
    """
    enabled, _context_window = _session_guard_config(ctx)
    if not enabled:
        return None

    # Layer 1 — ensure native compaction fires early (the real fix). Best-effort
    # and non-destructive: failure leaves the chat on the existing config.
    try:
        _ensure_compaction_threshold(profile)
    except Exception:  # noqa: BLE001 — fail-safe, never break the chat
        logging.getLogger("hscc-api").exception(
            "compaction-threshold ensure raised for %s (continuing)", profile)

    # Extension (t_c03fd5ae): also sweep the ROLE profiles (kanban workers /
    # subagents wedge identically). Runs on the same trigger as the orch ensure,
    # idempotently — after the first sweep every profile is already at the
    # constant, so this degrades to cheap no-op reads. Best-effort and
    # non-destructive: failure never breaks the chat and never touches the
    # orch profile path above (which stays byte-identical).
    try:
        _ensure_role_profiles()
    except Exception:  # noqa: BLE001 — fail-safe, never break the chat
        logging.getLogger("hscc-api").exception(
            "role-profile compaction-threshold sweep raised (continuing)")

    # Layer 2 — last-resort rotation, ONLY on positive compaction-failure
    # evidence (never on size alone).
    db = _open_profile_session_db(profile)
    if db is None:
        # Cannot inspect the profile's sessions — do not guess, do not rotate.
        return None
    try:
        try:
            session_row = db.get_session_by_title(session)
        except Exception:
            session_row = None
        if not session_row:
            return None   # no titled session to guard (chat will 503 honestly)
        bloated, reason = _session_bloat_verdict(session_row)
        if not bloated:
            return None
        return _do_rotation(db, profile, session, reason)
    finally:
        db.close()


def _do_rotation(db, profile: str, session: str, reason: str):
    """Retire + recreate a bloated session, FAIL-SAFE to no-rotation on error.

    Wraps the actual mutation so ANY failure (e.g. ``database is locked`` while
    a kanban worker is mid-turn on the profile, or an unexpected SessionDB
    error) fails SAFE: log it, return ``None``, and leave the chat to proceed on
    the existing session exactly as before this guard existed — never corrupt a
    profile, never wedge the POST on a retry. ``reason`` is the bloat verdict
    (see :func:`_session_bloat_verdict`) carried on the returned rotation dict.
    """
    try:
        rotation = _rotate_session(db, profile, session)
    except Exception as exc:  # noqa: BLE001 — must never break the chat
        logging.getLogger("hscc-api").error(
            "session-bloat rotation failed for %s/%s (guard disabled for this "
            "run; chat proceeds on existing session): %r",
            profile, session, exc,
        )
        return None
    rotation["reason"] = reason
    rotation["profile"] = profile
    rotation["title"] = session
    return rotation


def _session_health(ctx, profile: str, session: str):
    """Read-only session-health report (NO rotation) — for operator visibility.

    Surfaces the same real signals ``_guard_session_bloat`` inspects, plus the
    rotation verdict, WITHOUT mutating anything, so the operator can see a
    project's chat session health before it breaks (the card's surfacing
    requirement). Returns ``None`` when the profile/session is unresolvable or
    the guard is disabled — an honest "cannot report" that implies nothing.

    ``compaction_at_risk`` is the "alert" the operator asked for (t_a8e9b7ff):
    it is True exactly when there is POSITIVE evidence compaction is not firing
    (``compression_failure_error`` / fallback streak / ineffective count). It is
    intentionally NOT tied to raw ``input_tokens`` — that column is a CUMULATIVE
    counter (never reset by compaction), so "large" does not mean "compaction
    failed"; a large-but-healthy session is normal and must NOT be flagged.
    ``threshold_tokens`` reports the ensured compaction cap so the operator can
    see the proactive mechanism's configured value.

    Payload keys: ``profile``, ``session`` (title), ``messages``,
    ``input_tokens``, ``compression_failure_error``, ``compression_fallback_streak``,
    ``compression_ineffective_count``, ``context_window``, ``threshold_tokens``,
    ``compaction_at_risk``, ``bloated``, ``reason``.
    """
    enabled, context_window = _session_guard_config(ctx)
    if not enabled:
        return None
    db = _open_profile_session_db(profile, read_only=True)
    if db is None:
        return None
    try:
        try:
            session_row = db.get_session_by_title(session)
        except Exception:
            session_row = None
        if not session_row:
            return None
        bloated, reason = _session_bloat_verdict(session_row)
        return {
            "profile": profile,
            "session": session,
            "messages": int(session_row.get("message_count") or 0),
            "input_tokens": int(session_row.get("input_tokens") or 0),
            "compression_failure_error":
                (session_row.get("compression_failure_error") or "").strip() or None,
            "compression_fallback_streak":
                int(session_row.get("compression_fallback_streak") or 0),
            "compression_ineffective_count":
                int(session_row.get("compression_ineffective_count") or 0),
            "context_window": context_window,
            "threshold_tokens": SESSION_COMPACTION_THRESHOLD_TOKENS,
            "compaction_at_risk": bloated,
            "bloated": bloated,
            "reason": reason,
        }
    finally:
        db.close()


def _is_notice_line(line: str) -> bool:
    """True when a stdout line is a Hermes preamble notice, not part of the reply.

    ``hermes chat -Q`` can print a handful of harmless one-liners to stdout
    BEFORE the reply (observed). Known shapes:
      * the cwd-restore notice  ``↪ restored workspace dir: <path>``
        (t_bc242def Phase 1);
      * the security preamble   ``⚠ tirith security scanner enabled but not
        available — command scanning will use pattern matching only``
        (cli.py:7018-7021 — emitted via ``_cprint`` => stdout when no
        interactive prompt_toolkit app is running, t_5ed5dfa8 Bug 2);
      * the resume banner       ``↻ Resumed session <id> "<title>" (N user
        messages, M total messages)`` (cli_agent_setup_mixin.py:312-315 —
        ordinarily stderr, kept here defensively in case a config change
        lands it on stdout).

    Matched defensively (substring) against the KNOWN preamble shapes only, so
    future drift still lands on the conservative side. Crucially, no legitimate
    reply line that merely "looks like a warning" is ever stripped — only these
    exact Hermes startup lines are removed, never a model answer that happens
    to mention a scanner or a session.
    """
    s = line.strip()
    if "restored workspace dir:" in s:
        return True
    if "tirith security scanner enabled but not available" in s:
        return True
    # The resume banner is distinctive (a ↻ prefix plus the bracketed
    # user/total message counts) — no model reply naturally matches it.
    if s.startswith("↻ Resumed session ") and " total messages" in s:
        return True
    return False


# --------------------------------------------------------------------------- #
# Orchestrator-invocation error types (mapped to clean HTTP errors, no leaks)
# --------------------------------------------------------------------------- #

class _OrchestratorError(Exception):
    """Base for transport-level orchestrator failures (never leaked verbatim)."""


class _OrchestratorTimeout(_OrchestratorError):
    """The orchestrator did not reply within the timeout."""


class _OrchestratorUnavailable(_OrchestratorError):
    """The orchestrator profile/session could not be reached."""


class _OrchestratorInvocationError(_OrchestratorError):
    """A failed invocation with no clean session message."""


class _OrchestratorCancelled(_OrchestratorError):
    """The chat turn was explicitly cancelled (stopped) by the operator.

    Raised by :func:`_backing_invoke` when its ``cancel_evt`` fires mid-flight.
    ``_run_job`` maps this to the terminal ``stopped`` job state (never an error
    — a cancellation is an operator action, not a failure).
    """


# --------------------------------------------------------------------------- #
# Job store (in-memory, thread-safe). Lives as long as the server process, so
# a dropped POST connection does NOT lose a reply the background thread later
# completes — the phone can pick it up by job_id via GET.
# --------------------------------------------------------------------------- #

_jobs_lock = threading.Lock()
_jobs = {}                       # job_id -> _ChatJob
_job_ids = itertools.count(1)

# How long a TERMINAL job (done / timeout / unavailable / error) is retained
# after it finished before it becomes eligible for eviction. The whole value of
# the job API is that a job outlives its submitting connection and can be
# picked up LATER by job_id (a dropped/backgrounded phone), so eviction must
# NOT be aggressive enough to 404 a result an operator was promised. 60 min
# past ``finished_at`` is a generous grace: a real operator polling an in-flight
# chat reads it far sooner, and a result left unread for an hour is fair to reap.
_JOB_RETENTION_SECONDS = 3600.0

# Hard bound on the store: no matter how few terminal jobs have aged out of the
# retention window, never let the dict grow past this many entries. Belt to the
# retention-window's suspenders — keeps a pathological burst from ballooning
# memory even between _new_job opportunism. When over it we drop the oldest
# terminal jobs first (a live queued/running job is never evicted).
_JOBS_MAX = 10000


class _ChatJob:
    """One asynchronous orchestrator chat invocation.

    Status transitions: ``queued`` -> ``running`` -> ``done``, or to a
    terminal error state (``timeout`` / ``unavailable`` / ``error``). ``reply``
    and the identity fields are set only on ``done``; ``error`` carries a clean
    code+message on the failure states. ``elapsed`` is honest wall-clock from
    submission.
    """

    # terminal error status -> (public error code, speak-safe headline)
    _ERROR_MAP = {
        "timeout": ("orchestrator_timeout",
                    "The orchestrator did not reply in time."),
        "unavailable": ("orchestrator_unavailable",
                        "The orchestrator is not available right now."),
        "error": ("orchestrator_error",
                  "The orchestrator call failed."),
        "stopped": ("orchestrator_stopped",
                    "The chat turn was stopped by the operator."),
    }

    def __init__(self, job_id, project, profile, session, prompt,
                 timeout=_DEFAULT_TIMEOUT, notice=None,
                 image_data=None, image_mime=None):
        self.job_id = job_id
        self.project = project
        self.profile = profile
        self.session = session
        self.prompt = prompt
        self.timeout = timeout
        self.notice = notice   # optional honest busy-notice (Bug 1, t_5ed5dfa8)
        # Optional decoded image attachment (t_a779c06f). Neither is set for a
        # plain chat; when present, _run_job forwards both to _backing_invoke so
        # it can pass `--image <file>` to `hermes chat`.
        self.image_data = image_data
        self.image_mime = image_mime
        self.submitted_at = time.time()
        self.finished_at: float | None = None
        self.lock = threading.Lock()
        self.status = "queued"
        # Cancellation (t_68432c2d): set by the stop handler. ``_run_job`` polls
        # it via ``cancel_evt`` (forwarded into ``_backing_invoke``) and the stop
        # handler also holds the retained ``proc`` handle for an immediate
        # ``terminate()``/``kill()``.
        self.cancel_evt = threading.Event()
        self.proc = None                 # the live subprocess.Popen, when running
        self.stop_notified = False       # "turn stopped" transcript once (E)
        # done-state payload
        self.reply: str | None = None
        self.speak: str | None = None
        # error-state payload
        self.error: dict | None = None


def _reap_jobs():
    """Evict terminal jobs that have outlived their retention, under the lock.

    Safe policy (never over-aggressive — a legit late poll must not 404):
      1. Drop every terminal job (finished_at set) whose ``finished_at`` is
         older than :data:`_JOB_RETENTION_SECONDS` past now.
      2. As a hard bound, if the store is still over :data:`_JOBS_MAX`, drop
         the oldest terminal jobs until it is back under — never a live job
         (queued/running has no finished_at).

    The job_id :data:`_job_ids` counter is deliberately NOT touched: it is
    monotonic forever, so a reaped id is never re-issued to a later client that
    might already have seen it. Must be called with ``_jobs_lock`` held.
    """
    now = time.time()
    stale = [
        jid for jid, job in _jobs.items()
        if job.finished_at is not None
        and now - job.finished_at >= _JOB_RETENTION_SECONDS
    ]
    for jid in stale:
        del _jobs[jid]

    # Hard bound: if the store is still huge (pathological burst that flooded
    # in faster than retention ages it), trim the OLDEST terminal jobs first.
    over = len(_jobs) - _JOBS_MAX
    if over > 0:
        terminal_sorted = sorted(
            (job for job in _jobs.values() if job.finished_at is not None),
            key=lambda j: j.finished_at,
        )
        for job in terminal_sorted[:over]:
            del _jobs[job.job_id]


def _new_job(project, profile, session, prompt, timeout=_DEFAULT_TIMEOUT,
             notice=None, image_data=None, image_mime=None) -> _ChatJob:
    """Create (and store) a queued job under the store lock.

    Also runs the opportunistic :func:`_reap_jobs` so every submission pays
    down any terminal jobs that have aged past retention — this is what bounds
    the otherwise-unbounded ``_jobs`` dict (the t_2bb97a26 slow leak: nothing
    ever removed terminal jobs). ``notice`` (optional) is an honest busy-notice
    carried on the job (Bug 1, t_5ed5dfa8) so a later poll still explains why
    the orchestrator was slow.

    ``image_data`` (raw bytes) + ``image_mime`` are the optional decoded image
    attachment carried on the job (t_a779c06f) and forwarded to the transport
    when the job runs. Both are ``None`` for the common plain-text chat path.
    """
    with _jobs_lock:
        _reap_jobs()
        job_id = f"chat-{next(_job_ids)}"
        job = _ChatJob(job_id, project, profile, session, prompt, timeout=timeout,
                       notice=notice, image_data=image_data,
                       image_mime=image_mime)
        _jobs[job_id] = job
    return job


def _job_dict(job: _ChatJob) -> dict:
    """Snapshot a job as a JSON-serialisable dict (safe to hand the client).

    ``elapsed`` is honest wall-clock from submission. For ANY terminal state
    (``done`` or a failure state) it is FROZEN at ``finished_at - submitted_at``
    — the exact instant the job terminated — so the reported elapsed ALWAYS
    agrees with the status and error message: a job marked ``timeout`` at 180s
    reports ~180s, never the minutes you spent polling it afterward (that drift
    was the t_023d4c4c contradiction: status/error said 180s while elapsed said
    2972.7s). Only a live (``queued``/``running``) job shows a growing
    ``time.time() - submitted_at``.

    A ``notice`` set at submission (Bug 1, t_5ed5dfa8 — the orchestrator was
    busy with running kanban tasks) is emitted ONLY while the job is still
    LIVE (``queued``/``running``). It is DROPPED the instant the job reaches a
    terminal state (``done`` / ``timeout`` / ``unavailable`` / ``error``): the
    card (t_a8e9b7ff) proved the old "rides on every state forever" behavior
    LIED — a job that finished in 1.777s still carried "busy right now ... may
    take a while; poll this job for the answer." A notice that cries wolf is
    worse than none, so on a terminal job the outcome is fully described by
    ``status`` / ``reply`` / ``error`` and the stale busy/poll text is
    suppressed. The notice never tells you to poll a job that is already
    finished.
    """
    with job.lock:
        base = {
            "job_id": job.job_id,
            "project": job.project,
            "status": job.status,
        }
        # Busy notice only on a LIVE (non-terminal) job. A terminal job's
        # outcome is already fully described by status/reply/error; surfacing
        # "busy ... poll this job" there is the lying notice t_a8e9b7ff bans
        # (a job that already finished must never tell the operator to poll).
        if job.notice is not None and job.finished_at is None:
            base["notice"] = job.notice
        if job.finished_at is not None:
            # Terminal: elapsed frozen at the termination moment, so status,
            # error message and elapsed tell ONE coherent story.
            base["elapsed"] = round(job.finished_at - job.submitted_at, 3)
        else:
            # Live: elapsed grows as the operator polls.
            base["elapsed"] = round(time.time() - job.submitted_at, 3)
        if job.status == "done":
            base["reply"] = job.reply
            base["profile"] = job.profile
            base["session"] = job.session
            base["speak"] = job.speak
        if job.error is not None:
            base["error"] = job.error
        return base


def _run_job(job: _ChatJob):
    """Background worker: invoke the orchestrator and record the outcome.

    Runs in a daemon thread spawned by ``handle_orchestrator_chat``. Any
    transport failure maps to a terminal job error state with a clean
    code+message — never a raw exception, never a leaked detail.
    """
    with job.lock:
        job.status = "running"
    try:
        reply, profile, session = _backing_invoke(
            job.profile, job.session, job.prompt, timeout=job.timeout,
            image_data=job.image_data, image_mime=job.image_mime,
            cancel_evt=job.cancel_evt, on_spawn=_retain_proc(job),
        )
    except _OrchestratorCancelled:
        # An operator-initiated stop, not a failure. Land the job in the
        # terminal ``stopped`` state (never an error) — see _finish_cancelled.
        _finish_cancelled(job, "chat turn cancelled by operator")
        return
    except _OrchestratorTimeout as exc:
        _finish_job_error(job, "timeout", str(exc))
        return
    except _OrchestratorUnavailable as exc:
        _finish_job_error(job, "unavailable", str(exc))
        return
    except _OrchestratorInvocationError as exc:
        _finish_job_error(job, "error", str(exc))
        return
    except Exception:  # never surface raw internals to the client
        _finish_job_error(job, "error",
                          "an unexpected orchestrator failure occurred — "
                          "check ~/.hscc/api.log for details")
        return

    with job.lock:
        job.status = "done"
        job.reply = reply
        job.speak = f"{profile} says: {_shorten(reply)}"
        job.finished_at = time.time()


def _retain_proc(job: _ChatJob):
    """Return an ``on_spawn`` callback that stores the live Popen on the job.

    The moment ``_backing_invoke`` creates the subprocess it reports it here,
    so ``_run_job``'s worker thread and the CLI thread both see ``job.proc``.
    The stop handler reads this retained handle under the job lock to call
    ``terminate()``/``kill()`` immediately — the out-of-band kill the audit
    (t_68432c2d) requires, independent of the poll loop.
    """
    def _cb(proc):
        with job.lock:
            job.proc = proc
    return _cb


def _finish_cancelled(job: _ChatJob, message: str):
    """Land a job in the terminal ``stopped`` (cancelled) state.

    Maps to the ``orchestrator_stopped`` code — a cancellation is an operator
    action, never an error. Safe to call from both the worker thread (when
    ``_backing_invoke`` raises :class:`_OrchestratorCancelled`) and the stop
    handler (which acknowledges promptly). Idempotent under the job lock.
    """
    code, headline = _ChatJob._ERROR_MAP["stopped"]
    with job.lock:
        job.status = "stopped"
        job.error = {"code": code, "message": message,
                     "speak": headline}
        job.finished_at = time.time()


def cancel_job(job: _ChatJob, message: str = "chat turn cancelled by operator"):
    """Cancel a live job: request stop AND land the terminal ``stopped`` state.

    Central stop primitive shared by the REST stop route and the WS ``stop``
    kind (t_68432c2d). Three cooperating actions, all idempotent:

      1. Set ``job.cancel_evt`` — the worker's ``_backing_invoke`` poll loop
         sees it, terminates the process and raises ``_OrchestratorCancelled``;
      2. Terminate/kill the retained ``job.proc`` Popen directly — the
         out-of-band kill that works even if the worker thread is blocked;
      3. Land the ``stopped`` terminal state immediately, so the response /
         transcript reflects the operator action without waiting for the
         worker thread to observe the event.

    Returns the job (so the caller can snapshot it) or ``None`` if the job has
    already reached a terminal state (nothing left to cancel).
    """
    with job.lock:
        if job.finished_at is not None:
            return None      # already terminal; nothing to cancel
        terminal = job.status in ("done",)
    if terminal:
        return None
    job.cancel_evt.set()
    with job.lock:
        proc = job.proc
    if proc is not None:
        _terminate_proc(proc)
    _finish_cancelled(job, message)
    return job


def _finish_job_error(job: _ChatJob, status: str, message: str):
    """Land a job in a terminal error state with a clean, client-safe shape."""
    code, headline = _ChatJob._ERROR_MAP[status]
    with job.lock:
        job.status = status
        job.error = {"code": code, "message": message,
                     "speak": headline}
        job.finished_at = time.time()


# --------------------------------------------------------------------------- #
# Shared POST validation (confirm + prompt), used by handle_orchestrator_chat
# --------------------------------------------------------------------------- #

def _parse_body(body: bytes) -> dict:
    """Decode the request body as JSON; 400 on empty or malformed."""
    if not body:
        return {}
    import json
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "bad_request", "request body must be JSON")
    if not isinstance(data, dict):
        raise ApiError(400, "bad_request", "request body must be a JSON object")
    return data


def _require_confirm(data: dict) -> None:
    """Require ``confirm: true``; raise 409 otherwise (the mutation gate)."""
    if data.get("confirm") is True:
        return
    raise ApiError(
        409, "confirm_required",
        "this action is destructive and requires \"confirm\": true in the "
        "request body to send a message to an orchestrator",
        "Confirmation required to message the orchestrator.",
    )


def _validate_prompt(data: dict) -> str:
    """Pull + validate the required ``prompt``; 400 when missing/blank."""
    prompt = data.get("prompt")
    if prompt is None or not str(prompt).strip():
        raise ApiError(
            400, "bad_request", "missing required field 'prompt'",
            "Prompt is required.",
        )
    return str(prompt).strip()


# --------------------------------------------------------------------------- #
# Image attachment (t_a779c06f) — optional base64-in-JSON support
# --------------------------------------------------------------------------- #

# Decoded-size cap for an attached image. The operator may attach a
# screenshot/photo to a chat message; we bound it so a misbehaving client can't
# push an unbounded blob (and so the file hermes reads into context stays sane).
_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MiB decoded cap


def _validate_image(data: dict):
    """Pull + validate the optional image attachment from a chat POST body.

    The client may attach an image to a chat message as ``image_data`` (base64)
    + ``image_mime``. Both must come together. Returns
    ``(raw_bytes, normalized_mime)`` on success, or ``None`` when no image was
    supplied. Raises ``ApiError(400, bad_request)`` on a malformed,
    non-image-typed, or oversized attachment.
    """
    if "image_data" not in data and "image_mime" not in data:
        return None            # no attachment — the common (plain chat) path
    if "image_data" not in data or "image_mime" not in data:
        raise ApiError(400, "bad_request",
                       "'image_data' and 'image_mime' must be supplied together",
                       "Image attachment is incomplete.")
    b64 = data.get("image_data")
    mime = data.get("image_mime")
    if not isinstance(b64, str) or not isinstance(mime, str):
        raise ApiError(400, "bad_request",
                       "'image_data' must be a base64 string and 'image_mime' "
                       "a string",
                       "Image attachment is malformed.")
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        raise ApiError(400, "bad_request", "'image_data' is not valid base64",
                       "Image attachment could not be decoded.")
    if not raw:
        raise ApiError(400, "bad_request", "the image is empty",
                       "Image attachment is empty.")
    if len(raw) > _MAX_IMAGE_BYTES:
        raise ApiError(400, "bad_request",
                       f"image exceeds the {_MAX_IMAGE_BYTES // (1024 * 1024)} "
                       "MiB decoded limit",
                       "Image attachment is too large.")
    mime = mime.lower()
    if not mime.startswith("image/"):
        raise ApiError(400, "bad_request",
                       "'image_mime' must be an image/* type",
                       "Image attachment has an unsupported type.")
    return raw, mime


_IMAGE_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}


def _image_suffix(mime: str | None) -> str:
    """Map an image MIME to a temp-file suffix. Defaults to ``.png`` so an
    unusual image/* type (or a missing MIME) still gets a sensible, decodable
    extension."""
    return _IMAGE_SUFFIXES.get((mime or "").lower(), ".png")


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

def handle_orchestrator_chat(server, ctx, query, body):
    """POST /v1/orchestrator/chat — start an async orchestrator chat JOB.

    Body: ``{"project": "<name>"|null, "prompt": "...", "confirm": true}``.
      * ``project`` absent/null -> the ``general`` orchestrator;
      * unknown project -> 400;
      * ``prompt`` required -> 400 if missing/empty;
      * ``confirm: true`` REQUIRED -> 409 otherwise.

    The request VALIDATES + RESOLVES synchronously (so a bad project/prompt
    returns a clean 4xx immediately), then spawns a background thread that
    actually invokes the orchestrator, and returns **202 Accepted** with a
    ``job_id`` — NOT the reply. The phone polls
    ``GET /v1/orchestrator/chat/{id}`` for ``queued``/``running``/``done`` +
    honest ``elapsed`` and the reply when finished. This kills the 90 s dead
    wait: the POST returns in milliseconds, and a dropped connection no longer
    loses an answer the server is still computing.
    """
    data = _parse_body(body)
    _require_confirm(data)
    prompt = _validate_prompt(data)
    image = _validate_image(data)   # None, or (raw_bytes, normalized_mime)
    project = data.get("project")
    registry_path = _registry_path(ctx)

    # Resolve project -> orchestrator identity (general when absent/null).
    try:
        resolved = _backing_resolve(project, registry_path)
    except UnknownProjectError as exc:
        raise ApiError(
            400, "unknown_project", str(exc),
            f"Unknown project {project!r}.",
        )
    except OrchestratorError as exc:
        # e.g. a registry project with no board — cannot route an orchestrator.
        raise ApiError(
            400, "bad_request", str(exc),
            f"Project {project!r} cannot resolve to an orchestrator.",
        )

    profile = resolved["profile"]
    session = resolved["session"]

    # Create the project's chat session on first use (t_fc53b13d): a brand-new
    # project has NO session yet, so without this the job's
    # ``hermes chat -Q --continue <session>`` would fail with
    # ``orchestrator_unavailable`` ("create it first, then re-send") and the
    # project could never be chatted with. This CREATEs it (idempotently, via
    # Hermes' own SessionDB — never clobbers an existing session's history)
    # BEFORE the guard and the background job, so the subsequent
    # ``--continue <session>`` resolves to the fresh row. Returns a ``created``
    # dict only when it actually created the session; ``None`` when one already
    # existed or the ensure couldn't run (fail-safe).
    created = _ensure_session_exists(profile, session)

    # Session context-health guard (t_a8e9b7ff Bug 1): before the job continues
    # the named ``<session>``, (1) ENSURE the profile's native compaction fires
    # early (``compression.threshold_tokens = 100000`` — the PRIMARY mechanism,
    # proactive, non-destructive) and (2) as a last resort, when the session row
    # shows POSITIVE compaction-failure evidence, ROTATE it (retire the old row
    # to ``<session>-retired-<ts>`` — non-destructive — and create a fresh
    # ``<session>``) so this chat never wedges on a context compaction can no
    # longer recover. Rotation (if any) completes HERE, synchronously, BEFORE
    # the background job runs ``hermes chat --continue <session>``, satisfying
    # the card's ordering requirement (``--continue`` does not create a session;
    # the fresh one must already exist). The resolved ``session`` name is
    # unchanged — the fresh row now owns the ``<project>`` title — so
    # per-project continuity (``<project>-orch`` profile / ``<project>``
    # session / ``<project>`` board) is fully preserved.
    rotation = _guard_session_bloat(ctx, profile, session)

    # Bug 1 honest-reporting (t_5ed5dfa8): detect when the resolved orchestrator
    # profile is busy with running kanban work and say so up front, instead of a
    # silent 600s timeout. A chat on a busy profile queues behind its batch
    # workers (shared named session + journal_mode=delete state.db contention →
    # possible agent-layer compaction wedge) so the operator should understand
    # WHY it may be slow. The notice is surfaced at submit and on live polls
    # (see _job_dict); once the job is TERMINAL it is dropped (t_a8e9b7ff) —
    # a finished job never carries stale "busy / poll" text. Fail-safe: a busy
    # profile is reported busy; an unverifiable one is treated as busy.
    busy_count = _backing_busy_tasks(profile)
    busy_notice = _busy_notice(profile, busy_count) if busy_count > 0 else None

    # Create the job and run the real invocation on a daemon background thread
    # (the server is ThreadingHTTPServer with daemon_threads, so this thread
    # lives as long as the process — the reply is NOT lost when the POST
    # connection closes). The timeout (configurable, default 600s) is captured
    # per-job at submission so every job reports elapsed at ITS termination.
    job = _new_job(project, profile, session, prompt, timeout=_chat_timeout(ctx),
                   notice=busy_notice,
                   image_data=(image[0] if image else None),
                   image_mime=(image[1] if image else None))
    worker = threading.Thread(target=_run_job, args=(job,), daemon=True)
    worker.start()

    payload = {
        "job_id": job.job_id,
        "project": project,
        "status": "queued",
        "elapsed": 0.0,
        "profile": profile,
        "session": session,
        "speak": f"Chat request accepted (job {job.job_id}).",
    }
    if busy_notice is not None:
        payload["notice"] = busy_notice
    if created is not None:
        # Surface the create-on-first-use so the operator sees a brand-new
        # project's chat session was created and the chat now continues on it
        # (idempotently; proving the cold-start path works).
        payload["session_created"] = {
            "profile": created["profile"],
            "title": created["title"],
            "session": created["created_session"],
        }
    if rotation is not None:
        # Surface the rotation so the operator sees the session was retired +
        # recreated (the chat continues on the fresh session, same name).
        payload["session_rotation"] = {
            "profile": rotation["profile"],
            "title": rotation["title"],
            "retired_session": rotation["retired_session"],
            "retired_title": rotation["retired_title"],
            "session": rotation["session"],
            "reason": rotation["reason"],
        }
    return 202, payload


def handle_orchestrator_chat_job(server, ctx, query, body):
    """GET /v1/orchestrator/chat/{id} — poll an async chat job.

    Read-only (the orchestrator was already messaged by the POST), so no
    ``confirm`` is required — only bearer auth (enforced by the dispatcher).

    Response:
      * ``queued``/``running`` -> ``{job_id, project, status, elapsed}``;
      * ``done`` -> plus ``reply``, ``profile``, ``session``, ``speak``;
      * terminal error state (``timeout``/``unavailable``/``error``) -> plus an
        ``error`` object ``{code, message, speak}`` mirroring the API's error
        shape so the client can surface each real condition distinctly.
    Unknown job id -> 404.
    """
    job_id = query.get("id")
    with _jobs_lock:
        job = _jobs.get(job_id)  # type: ignore[arg-type]
    if job is None:
        raise ApiError(404, "not_found", f"unknown chat job {job_id!r}",
                       "That chat job does not exist (it may have expired).")
    return 200, _job_dict(job)


def _in_flight_job(project) -> _ChatJob | None:
    """Return the live job for a project, if any (``None`` otherwise).

    The WS relay path has no ``job_id`` at hand — a ``stop`` frame carries only
    the ``project`` — so the stop handler finds the running turn by project
    here. A job counts as in-flight while it is ``queued`` or ``running`` (not
    yet terminal). Scans the job store; there is at most one live job per
    project in practice, but if several somehow coexist the oldest is returned.
    """
    best = None
    with _jobs_lock:
        for job in _jobs.values():
            if job.project != project:
                continue
            if job.finished_at is not None:
                continue
            if best is None or job.submitted_at < best.submitted_at:
                best = job
    return best


def handle_orchestrator_chat_stop(server, ctx, query, body):
    """POST /v1/orchestrator/chat/{id}/stop — cancel a running chat job.

    Body: ``{\"confirm\": true}`` — REQUIRED (409 otherwise), mirroring the
    confirm gate every mutating endpoint enforces (the analogous
    ``POST /v1/cluster/stop`` is confirm-gated too; stopping a turn is a
    destructive action on a running process and gets the same guard).

    Behavior (t_68432c2d):
      * finds the job by id; unknown -> 404;
      * if the job is already terminal (``done``/``timeout``/``unavailable``/
        ``error``/``stopped``) -> 200 with the CURRENT state and ``already``
        flag, so a double-tap stop (or a stop racing completion) is a clean
        no-op, not an error;
      * otherwise cancels it: sets the cancel event, terminates/kills the
        retained Popen, lands the terminal ``stopped`` state, and (if the
        project has a live store) appends a ``turn stopped`` transcript notice
        (requirement E).

    Response (fresh snapshot after cancellation):
      ``{job_id, project, status: \"stopped\", elapsed, error:
        {code: \"orchestrator_stopped\", ...}, stopped: true}``.
    """
    data = _parse_body(body)
    _require_confirm(data)
    job_id = query.get("id")
    with _jobs_lock:
        job = _jobs.get(job_id)  # type: ignore[arg-type]
    if job is None:
        raise ApiError(404, "not_found", f"unknown chat job {job_id!r}",
                       "That chat job does not exist (it may have expired).")

    cancelled = cancel_job(job, "chat turn cancelled by operator")
    payload = _job_dict(job)
    payload["job_id"] = job.job_id
    if cancelled is None:
        # Already terminal — report its real current state, flag the no-op.
        payload["already"] = True
    else:
        payload["stopped"] = True
        # Requirement E: surface a "turn stopped" notice in the project's live
        # transcript so a connected client sees the interruption, not a hang.
        _notify_stopped(job.project)
    return 200, payload


def _notify_stopped(project) -> None:
    """Append a one-time ``turn stopped`` notice to the project's chat store.

    Requirement E of t_68432c2d. Deliberately late-bound into ``routes_ws``
    (importing it at module top would be circular — routes_ws already imports
    this module). Best-effort: a project with no live store (nobody subscribed)
    simply gets no notice. The idempotency (one notice per stop burst) is owned
    inside routes_ws, not here.
    """
    try:
        from . import routes_ws
        routes_ws.notify_turn_stopped(project)
    except Exception:
        pass   # never break the stop response over a best-effort notice


def _shorten(text: str, limit: int = 120) -> str:
    """Trim a long reply for the one-line ``speak`` summary."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("POST", re.compile(r"^/v1/orchestrator/chat$"),
               handle_orchestrator_chat))
ROUTES.append(("GET", re.compile(r"^/v1/orchestrator/chat/(?P<id>[^/]+)$"),
               handle_orchestrator_chat_job))
ROUTES.append(("POST", re.compile(r"^/v1/orchestrator/chat/(?P<id>[^/]+)/stop$"),
               handle_orchestrator_chat_stop))
