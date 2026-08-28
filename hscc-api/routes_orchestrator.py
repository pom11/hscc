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

import itertools
import logging
import re
import subprocess
import sys
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


def _backing_invoke(profile, session, prompt, timeout=_DEFAULT_TIMEOUT):
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

    Returns ``(reply_text, profile, session)``. Raises:
      * ``_OrchestratorTimeout`` when the reply exceeds ``timeout``;
      * ``_OrchestratorUnavailable`` when the profile/session cannot be
        reached (e.g. no matching session yet, or hermes not installed).
      * ``_OrchestratorInvocationError`` on any other failed invocation
        (nonzero exit / unparsable output).
    """
    argv = ["hermes", "-p", profile, "chat", "-Q", "--continue", session,
            "-q", prompt]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise _OrchestratorTimeout(
            f"orchestrator {profile!r} did not reply within {timeout:.0f}s"
        )
    except (FileNotFoundError, OSError) as exc:
        raise _OrchestratorUnavailable(
            f"cannot invoke `hermes`: {exc!r}"
        )

    err = (proc.stderr or "").strip()
    # A clean "no such session yet" failure — the orchestrator's named session
    # must exist before it can be continued (created by provisioning / first
    # Telegram topic). Surface it honestly rather than synthesising a reply.
    if proc.returncode != 0 or "Session not found" in err:
        raise _OrchestratorUnavailable(
            f"orchestrator session {session!r} not ready "
            f"(create it first, then re-send)".strip()
        )

    reply = (proc.stdout or "").strip()
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
    }

    def __init__(self, job_id, project, profile, session, prompt,
                 timeout=_DEFAULT_TIMEOUT, notice=None):
        self.job_id = job_id
        self.project = project
        self.profile = profile
        self.session = session
        self.prompt = prompt
        self.timeout = timeout
        self.notice = notice   # optional honest busy-notice (Bug 1, t_5ed5dfa8)
        self.submitted_at = time.time()
        self.finished_at: float | None = None
        self.lock = threading.Lock()
        self.status = "queued"
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
             notice=None) -> _ChatJob:
    """Create (and store) a queued job under the store lock.

    Also runs the opportunistic :func:`_reap_jobs` so every submission pays
    down any terminal jobs that have aged past retention — this is what bounds
    the otherwise-unbounded ``_jobs`` dict (the t_2bb97a26 slow leak: nothing
    ever removed terminal jobs). ``notice`` (optional) is an honest busy-notice
    carried on the job (Bug 1, t_5ed5dfa8) so a later poll still explains why
    the orchestrator was slow.
    """
    with _jobs_lock:
        _reap_jobs()
        job_id = f"chat-{next(_job_ids)}"
        job = _ChatJob(job_id, project, profile, session, prompt, timeout=timeout,
                       notice=notice)
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
        )
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
                   notice=busy_notice)
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
