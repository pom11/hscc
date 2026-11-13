"""Profile-root INTENT for the hscc-api HTTP server (t_c443c001).

WHY THIS EXISTS. Every writer this server reaches — ``create_profile``,
``delete_profile(name, yes=True)``, ``rename_profile``, ``write_profile_meta``,
the editor's ``config.yaml``/``profile.yaml`` merge, ``ensure_orchestrator``,
the compaction-threshold ensure — resolves a profiles root and writes it. Three
different resolvers do that in one process and nothing cross-checked them:
``hermes_cli.profiles.get_profile_dir``, the hscc ladder
``routes_profile._hermes_profiles_dir()`` and ``rolelib.PROFILES_DIR``. The
hazard is a RULE inside hermes, not an accident:
``hermes_constants._hermes_root_of`` returns the NATIVE home whenever
``HERMES_HOME`` resolves *under* ``~/.hermes``, and honours it as a custom root
only when it lies outside it. So a process that believes
``HERMES_HOME=<scratch inside ~/.hermes>`` sandboxed it writes the operator's
real ``~/.hermes/profiles/**`` and reports success — and because one of those
writers DELETES a profile directory, "reported success" can mean an operator
profile is gone. Same class as t_10ccaf80 / t_d39c720e / t_7edf58ce, which the
CLI already guarded (``rolelib.assert_profile_root`` + ``--hermes-home``); this
module is the same control over the HTTP transport.

WHY A CONFIGURED INTENT AND NOT A RESOLVER GUARD. ``HERMES_HOME=<root>/profiles/
<name>`` resolving back to the native home is *the mechanism hermes profile mode
runs on* — the same bytes that mean "throwaway sandbox" also mean "this hermes
is running under a profile", and only the caller knows which. So the knowledge
has to come from outside the resolver: an operator-set intent, stated once at
server start. That is ``HSCC_API_PROFILES_ROOT`` (D1). It is deliberately the
ONLY channel — no flag on an HTTP route, on ``serve`` argv, or on ``hscc api
start`` (D2), because ``serve`` argv is baked into a generated plist and a flag
there would be a second, less visible way to say the same thing.

THE VALUE IS THE PROFILES DIR (D3): ``<hermes-home>/profiles``, the directory
that directly contains ``<profile>/config.yaml`` — the same contract as
``rolelib.assert_profile_root(expected)``, so the CLI and the API state intent
the same way.

TWO GATES, ONE IMPLEMENTATION (D8). ``assert_boot_intent()`` runs before
resolve_config / load_token / bind, so a mis-rooted server never serves at all
(exit 2, nothing written). ``guard_write()`` runs as the first statement of
every mutating backing call, because the ladder resolves at CALL TIME on
purpose — a root that matched at boot can drift afterwards, and the choke point
is what keeps "nothing written" true under that drift.

WHAT A DIVERGENCE DOES (D9). If the request's *purpose* is to change a profile,
the answer is ``503 profile_root_mismatch`` and nothing is written. If the write
is *incidental* to another purpose (the compaction-threshold ensure on the
chat/session-guard path, documented FAIL-SAFE "never break a chat"), the answer
is skip-the-write + ERROR log and the request proceeds: a server-config state
must not turn into a failed operator chat.

Reads are out of scope on purpose (D11): a wrong-root read degrades to an honest
empty list (``routes_memory.py:73-114``, ``routes_profile.py:63-107``), it does
not corrupt a tree. They get the startup log line, not a guard.
"""

from __future__ import annotations

import os

INTENT_ENV = "HSCC_API_PROFILES_ROOT"

# A valid profile id (hermes_constants.PROFILE_ID_RE) that is not a real profile
# on any machine we run on. ``get_profile_dir`` is pure path arithmetic — it does
# not create or stat anything — so probing it costs nothing and writes nothing.
# Same probe name and rationale as ``rolelib._PROFILE_ROOT_PROBE``.
_ROOT_PROBE = "hscc-root-probe"

# The writer_roots() keys. These strings are CONTRACT: they are what a refusal
# message names (``DIVERGENT <key> -> <root>``) and what the tests assert, so a
# CLI refusal and an API refusal read alike.
KEY_HERMES = "hermes get_profile_dir"
KEY_LADDER = "routes_profile._hermes_profiles_dir"
KEY_ROLELIB = "rolelib.PROFILES_DIR"

_EXPLANATION = (
    "  A requested home that lives INSIDE the live hermes root resolves "
    "back to that live root (hermes_constants._hermes_root_of): the write "
    "would land in the operator's tree. Use a home outside it.")

# The intent captured ONCE at server start (D7). Handlers must never re-read the
# environment: a long-lived, multithreaded server that re-resolved intent per
# request would be reading a value that can change underneath a running turn.
_captured: str | None = None
_captured_set = False


class ProfileRootRefusal(RuntimeError):
    """The configured profiles-root intent is not honoured.

    Raised by :func:`assert_boot_intent` BEFORE anything binds or writes. It is
    deliberately NOT the plain ``RuntimeError`` the serve entrypoints already map
    to exit 1 (bind / token failures): an operator must be able to tell
    "misconfigured profile root" from "port busy" — both entry paths turn this
    into exit code 2.
    """


# --------------------------------------------------------------------------- #
# Intent (the operator's statement of which profiles dir this server may write)
# --------------------------------------------------------------------------- #

def intent() -> str | None:
    """The raw configured intent, expanded and stripped; ``None`` when unset.

    Empty or whitespace-only counts as UNSET (D6): a ``VAR=`` left in a plist's
    ``EnvironmentVariables`` must not brick the server. A non-empty RELATIVE
    value is not refused here — :func:`resolve_intent` refuses it, so the same
    rule applies whichever entry point reads it.
    """
    raw = os.environ.get(INTENT_ENV)
    if raw is None:
        return None
    raw = os.path.expanduser(raw).strip()
    return raw or None


def resolve_intent(raw: str) -> str:
    """Canonicalise a configured intent to a comparable profiles dir.

    ``realpath`` (not ``abspath``) so a symlinked home compares correctly, and a
    trailing separator is dropped so ``.../profiles/`` and ``.../profiles`` are
    the same intent (B2). A non-absolute value cannot assert identity with a
    real root, so it is a refusal (D6), naming the offending value.
    """
    value = os.path.expanduser(str(raw)).strip()
    if not os.path.isabs(value):
        raise ProfileRootRefusal(
            f"{INTENT_ENV} must be an absolute path, got {raw!r}.\n"
            "  The value is the PROFILES DIR — <hermes-home>/profiles, the "
            "directory that directly contains <profile>/config.yaml. Nothing "
            "was bound and nothing was written.")
    return os.path.realpath(value).rstrip(os.sep) or os.sep


def captured_intent() -> str | None:
    """The intent this process committed to at boot (``None`` when unset).

    Falls back to :func:`intent()` before any server has been created, so the
    guard works for a caller that drives a backing function directly.
    """
    return _captured if _captured_set else intent()


def capture(resolved: str | None) -> str | None:
    """Remember the canonical intent for the life of the process (D7).

    Called once by ``create_server`` with the value it also stores on the
    ``ApiContext``; every later ``guard_write()`` reads this, never the env.
    """
    global _captured, _captured_set
    _captured = resolved
    _captured_set = True
    return resolved


# --------------------------------------------------------------------------- #
# The roots the writers will actually use
# --------------------------------------------------------------------------- #

def _canonical(path) -> str:
    """Realpath of an expanded path — how two path *identities* compare."""
    return os.path.realpath(os.path.expanduser(str(path)))


def _native_profiles_dir() -> str | None:
    """Where hermes' own resolver would put a profile, or ``None``.

    This is the value that decides where ``create_profile`` / ``delete_profile``
    / ``generate_profile`` land, so it is the one worth trusting. ``None`` when
    ``hermes_cli`` is unimportable (a thin interpreter), where the fallback
    writers' roots are the whole story.
    """
    try:
        from hermes_cli.profiles import get_profile_dir
    except Exception:      # noqa: BLE001 - thin interpreter / broken install
        return None
    try:
        return os.path.dirname(os.path.normpath(str(get_profile_dir(_ROOT_PROBE))))
    except Exception:      # noqa: BLE001 - a resolver that cannot answer is not a match
        return None


def _ladder_profiles_dir() -> str | None:
    """The hscc ladder's answer (what the profile editor writes), or ``None``.

    Resolves at call time on purpose — see ``routes_profile.hermes_root_dir``.
    Importing it lazily keeps this module importable standalone (and avoids an
    import cycle with ``routes_profile``, which imports ``api_server``).
    """
    try:
        import routes_profile
    except Exception:      # noqa: BLE001
        return None
    try:
        return str(routes_profile._hermes_profiles_dir())
    except Exception:      # noqa: BLE001
        return None


def _rolelib():
    """The ``rolelib`` module, or ``None`` when hscc-roles is unimportable.

    It normally IS importable here — ``routes_bootstrap`` puts ``hscc-roles`` on
    ``sys.path`` at import — so this guard reuses the module that OWNS the
    root-equality rule instead of restating it (D5).
    """
    try:
        import rolelib
    except Exception:      # noqa: BLE001
        return None
    return rolelib


def writer_roots() -> "dict[str, str | None]":
    """Every profiles root a mutating handler in this process would write.

    ``{KEY_HERMES: …, KEY_LADDER: …, KEY_ROLELIB: …}``, each ``None`` when that
    resolver cannot answer. Pure path arithmetic: creates nothing, stats nothing
    for creation, writes nothing (the probe name and rationale mirror
    ``rolelib._PROFILE_ROOT_PROBE``).
    """
    roots = {KEY_HERMES: _native_profiles_dir(),
             KEY_LADDER: _ladder_profiles_dir()}
    rl = _rolelib()
    roots[KEY_ROLELIB] = None if rl is None else str(rl.PROFILES_DIR)
    return roots


# --------------------------------------------------------------------------- #
# The one comparison, used by both gates
# --------------------------------------------------------------------------- #

def divergence(resolved: str, purpose: str = "hscc-api write") -> "str | None":
    """Refusal message when ``resolved`` is NOT every writable root; else None.

    Fail CLOSED: while an intent was asked for, a resolver that cannot answer is
    not a match — if NO root resolves at all, that is a refusal, not a pass.

    When ``rolelib`` is importable its ``assert_profile_root`` — the fleet's
    single implementation of root-equality for the two roots it owns — decides
    those two; the ladder entry (which rolelib does not know) is compared here,
    so D4 holds over all three. The message always carries, in this order:
    requested root → the HOME/HERMES_HOME/HERMES_REAL_HOME env triple → one
    ``DIVERGENT <writer root> -> <root>`` line per divergence → the
    ``_hermes_root_of`` explanation.
    """
    roots = writer_roots()
    divergent = [(name, got) for name, got in roots.items()
                 if got is not None and _canonical(got) != resolved]

    rl = _rolelib()
    if rl is not None:
        # Honour rolelib's own verdict even when my comparison found nothing
        # (it can fail closed for reasons of its own; a guard that is only as
        # strict as its reimplementation is a guard that drifts).
        try:
            rl.assert_profile_root(resolved, purpose="hscc-api write")
        except Exception as exc:      # noqa: BLE001 - shape of refusal is ours
            if not any(name == KEY_ROLELIB for name, _ in divergent):
                divergent.append((KEY_ROLELIB, f"rolelib refused ({exc})"))

    if not divergent:
        if not any(got is not None for got in roots.values()):
            # Zero resolvable roots while an intent was asked for: refuse.
            return _message(resolved, [("<no writable root resolved>", "None")])
        return None
    return _message(resolved, divergent)


def _message(resolved: str, divergent) -> str:
    """The refusal text. Order is contract (tests assert substrings)."""
    env = ", ".join(f"{k}={os.environ.get(k)!r}"
                    for k in ("HOME", "HERMES_HOME", "HERMES_REAL_HOME"))
    lines = ["refusing to write: profile-root intent not honoured "
             "(hscc-api profiles root).",
             f"  requested profiles root: {resolved}",
             f"  env: {env}"]
    for name, got in divergent:
        lines.append(f"  DIVERGENT {name} -> {got}")
    lines.append(_EXPLANATION)
    lines.append(f"  Set {INTENT_ENV} to the profiles dir this server may "
                 "write, or leave it unset to serve the ambient home.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Gate 1 — boot (the primary gate: never bind, therefore never write)
# --------------------------------------------------------------------------- #

def assert_boot_intent() -> "str | None":
    """Assert the configured intent before anything binds. Return it, or None.

    No intent → ``None`` and nothing else happens: the operator's default path
    is byte-identical to today (the whole point of A1-A4). Intent set → it is
    canonicalised (a relative value refuses), compared against every writable
    root, and a divergence raises :class:`ProfileRootRefusal` — which both
    entrypoints turn into exit 2.
    """
    raw = intent()
    if raw is None:
        return None
    resolved = resolve_intent(raw)
    msg = divergence(resolved)
    if msg:
        raise ProfileRootRefusal(msg)
    return resolved


# --------------------------------------------------------------------------- #
# Gate 2 — the write choke point (keeps "nothing written" true under drift)
# --------------------------------------------------------------------------- #

def divergence_message() -> "str | None":
    """The divergence message for the captured intent, or ``None``.

    The non-raising form, for the FAIL-SAFE incidental writers (D9): those sites
    log this at ERROR and skip the write rather than turning a server-config
    state into a failed operator chat.
    """
    resolved = captured_intent()
    if resolved is None:
        return None
    return divergence(resolved)


def guard_write() -> None:
    """Refuse an intentional profile write when the roots do not match.

    First statement of every mutating ``_backing_*`` (and of the site-1 write
    block), inside the existing auth → confirm-409 → validation order. No intent
    → no-op. Divergence → ``ApiError(503, "profile_root_mismatch", msg)`` with
    nothing written — the caller has not touched the filesystem yet.
    """
    msg = divergence_message()
    if msg is None:
        return
    from api_server import ApiError
    raise ApiError(503, "profile_root_mismatch", msg,
                   "Profile root mismatch — the server refused to write "
                   "any profile. Nothing was changed.")


# --------------------------------------------------------------------------- #
# The startup line (adopted from option 1: a refused boot still leaves a trace)
# --------------------------------------------------------------------------- #

def startup_line() -> str:
    """`hscc-api: profiles root <realpath> (intent: none|<path>)`.

    The root printed is the LADDER answer — what the profile editor will
    actually write — because that is the root a wrong-read symptom gets
    diagnosed against. Printed on every boot, both entry paths, whether or not
    an intent is configured.
    """
    root = _ladder_profiles_dir() or "unknown"
    captured = captured_intent()
    return (f"hscc-api: profiles root {root} "
            f"(intent: {captured if captured else 'none'})")
