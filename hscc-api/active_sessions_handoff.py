"""Consumed session-ownership handoff (t_7cc2e7d5).

Wires the HSCC serve bridge to the upstream consented authorized-controller
handoff seam — fork branch ``feat/session-ownership-handoff``, commits
``ed7c04b338`` + ``77f046f089`` (pom11/hermes-agent). When the operator's
interactive CLI holds a project's named session, an app→session send is
currently refused SESSION_NOT_OWNED and only a "session busy" notice surfaces.
This coordinator drives the CONSENTED handoff so the app can genuinely DRIVE
the session 1:1:

    request -> emit "waiting for approval on your computer"
            -> poll for the owner's consent
            -> complete (take the lease)
            -> drive the turn (existing send path)
            -> release (hand the session back to the operator's CLI)

The upstream seam is RPC-based on the gateway (``session.request_handoff`` /
``session.handoff_consent`` / ``session.complete_handoff``), but the HSCC drive
acts as the CONTROLLER and calls the hermes PRIMITIVES directly against the
project profile's active-session registry — the same read/write seam the
upstream RPCs themselves use (``hermes_cli/active_sessions.py``), scoped to the
project's profile home. This is exactly the "dashboard-pool same-process peer"
path the upstream seam documents for a controller that shares the registry.

The consent itself is always the OWNER's act: ``grant_active_session_handoff``
is only callable by a process that proves same-writer identity (pid +
``live_session_id``), which a controller can never do. The controller requests,
waits, and completes — it can never self-approve.

Availability: the primitives only exist on a hermes deployment that has the
seam. The live operator's branch (``dep-bump-2026.9.24``) does NOT expose them
at runtime, so we detect via ``hasattr`` and return a cooperative sentinel that
makes the caller fall back to the existing ``session_busy`` notice — NO
regression when the seam is absent (this is the task's sanctioned no-deploy
path).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("hscc-api.handoff")

# Controller identity this bridge uses for every handoff (profile-tied, stable,
# and namespaced so it cannot collide with another controller).
CONTROLLER_ID = "hscc-app"

# How long we wait for the operator to consent before giving up (upstream TTL
# is 300s; we cap our own poll beneath it so we never race the expiry mid-turn).
_GRANT_POLL_TIMEOUT = 280.0
_GRANT_POLL_INTERVAL = 0.25

# Sentinel returned when the running hermes does not expose the handoff seam
# (pre-deploy). The caller maps it to its existing ``session_busy`` fallback.
SEAM_UNAVAILABLE = "SEAM_UNAVAILABLE"


def _import_primitives() -> Optional[Any]:
    """Import (and return) the upstream handoff primitives, or None if absent.

    The primitives live in ``hermes_cli.active_sessions``. They are only
    present on a hermes that has the seam landed (fork commit 77f046f089). On
    the live pre-deploy branch they do not exist → None, so the caller falls
    back. Any import failure is treated as seam-unavailable (fail closed, like
    the upstream fail-closed design).
    """
    try:
        from hermes_cli import active_sessions as as_mod
    except Exception:  # noqa: BLE001 — seam absent on pre-deploy hermes
        return None
    if not all(hasattr(as_mod, name) for name in (
            "request_active_session_handoff",
            "complete_active_session_handoff",
            "active_session_registry_snapshot",
    )):
        return None
    return as_mod


def seam_available() -> bool:
    """True when the running hermes exposes the handoff primitives (post-deploy)."""
    return _import_primitives() is not None


def _profile_home(profile: str) -> Optional[Path]:
    """Resolve a profile's home dir (the registry home for its active sessions).

    Mirrors routes_orchestrator's own resolution (``get_profile_dir``) so the
    handoff targets the SAME profile the CLI chat path uses. Any failure → None.
    """
    try:
        from hermes_cli import profiles as profiles_mod
        canon = profiles_mod.normalize_profile_name(profile)
        if not profiles_mod.profile_exists(canon):
            return None
        return Path(profiles_mod.get_profile_dir(canon))
    except Exception:  # noqa: BLE001 — fail-safe
        return None


class HandoffError(Exception):
    """A handoff could not proceed (surfaced to the caller for feedback)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class HandoffCoordinator:
    """Drives a consented session handoff for ONE app send on ONE project.

    Not tied to a driver instance — it is a pure, seam-abstracted coordinator
    the WS send path (``routes_ws._handle_client_send``) invokes. All state is
    local to a single ``orchestrate`` call, so concurrent sends to the same
    session serialize where it matters (the upstream primitives' own file lock).

    The seam calls are lazy + injectable so tests run hermetically against an
    isolated profile home (or a real fork-branch hermes via PYTHONPATH), never
    the operator's live registry.
    """

    def __init__(
        self,
        *,
        controller: str = CONTROLLER_ID,
        grant_timeout: float = _GRANT_POLL_TIMEOUT,
        grant_poll_interval: float = _GRANT_POLL_INTERVAL,
        session_id: Optional[str] = None,
        profile: Optional[str] = None,
        surface: str = "tui",
        snapshot_fn: Optional[Callable[..., list]] = None,
        primitives_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.controller = controller
        self.grant_timeout = grant_timeout
        self.grant_poll_interval = grant_poll_interval
        self.session_id = session_id
        self.profile = profile
        self.surface = surface
        # Injectable for hermetic tests: ``snapshot_fn(registry_home) -> list[dict]``
        # and ``primitives_factory() -> the active_sessions module (or None)``.
        self._snapshot_fn = snapshot_fn
        self._primitives_factory = primitives_factory

    # -- injectables (overridable by tests) -------------------------------- #

    def _primitives(self) -> Optional[Any]:
        if self._primitives_factory is not None:
            return self._primitives_factory()
        return _import_primitives()

    def _snapshot(self, registry_home: Optional[Path]) -> list[dict]:
        if self._snapshot_fn is not None:
            return self._snapshot_fn(registry_home)
        mod = self._primitives()
        if mod is None:
            return []
        return mod.active_session_registry_snapshot(
            registry_home, strict=False)

    # -- registry home ----------------------------------------------------- #

    def registry_home(self, profile: Optional[str] = None) -> Optional[Path]:
        profile = profile or self.profile
        if not profile:
            return None
        return _profile_home(profile)

    # -- step 1: request --------------------------------------------------- #

    def request(self, session_id: Optional[str] = None,
                registry_home: Optional[Path] = None) -> dict:
        """Request the handoff; return ``{"granted": bool}``.

        Raises :class:`HandoffError` when the seam is unavailable or there is no
        live owner to ask (the caller then falls back to ``session_busy``).
        """
        mod = self._primitives()
        if mod is None:
            raise HandoffError(SEAM_UNAVAILABLE,
                               "session handoff not available on this hermes")
        sid = session_id or self.session_id
        if not sid:
            raise HandoffError("no_session",
                               "no session id to hand off")
        pending, refusal = mod.request_active_session_handoff(
            sid, controller=self.controller, registry_home=registry_home)
        if refusal is not None:
            reason = getattr(refusal, "reason", None)
            raise HandoffError(reason or "handoff_refused", str(refusal))
        return {"granted": bool((pending or {}).get("granted"))}

    # -- step 2: poll for consent (capture the granted nonce) -------------- #

    def poll_for_nonce(self, session_id: Optional[str] = None,
                       registry_home: Optional[Path] = None,
                       timeout: Optional[float] = None) -> str:
        """Wait for the owner to consent; return the one-shot nonce.

        The owner's consent (``grant_active_session_handoff`` on ITS client)
        writes ``handoff.granted == True`` + the nonce onto the SAME registry
        entry this controller requested on. We poll that entry read-only until
        the nonce is granted or ``timeout`` (default :data:`_GRANT_POLL_TIMEOUT`)
        elapses. Returns the nonce, or raises ``HandoffError("grant_timeout")``
        (the operator did not approve in time — surface a denied/timeout
        notice).
        """
        sid = session_id or self.session_id
        timeout = self.grant_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for entry in self._snapshot(registry_home):
                if str(entry.get("session_id") or "") != sid:
                    continue
                h = entry.get("handoff")
                if isinstance(h, dict) and \
                        str(h.get("controller") or "") == self.controller and \
                        h.get("granted") and h.get("nonce"):
                    return str(h["nonce"])
                # The entry changed hands or the ask was cleared (denied /
                # expired / re-requested by someone else): bail early so we do
                # not wait the full TTL against a moot ask.
                if not isinstance(h, dict) or \
                        str(h.get("controller") or "") != self.controller:
                    raise HandoffError("handoff_denied",
                                       "The handoff request was not approved.")
            time.sleep(self.grant_poll_interval)
        raise HandoffError("grant_timeout",
                           "Timed out waiting for the owner to approve.")

    # -- step 3: complete (take the lease) -------------------------------- #

    def complete(self, nonce: str, session_id: Optional[str] = None,
                 registry_home: Optional[Path] = None) -> Any:
        """Present the nonce; return an ``ActiveSessionLease`` (or raise)."""
        mod = self._primitives()
        if mod is None:
            raise HandoffError(SEAM_UNAVAILABLE,
                               "session handoff not available on this hermes")
        sid = session_id or self.session_id
        if not sid or not nonce:
            raise HandoffError("no_handoff",
                               "cannot complete a handoff without session id + nonce")
        lease, refusal = mod.complete_active_session_handoff(
            sid, controller=self.controller, nonce=nonce, surface=self.surface,
            registry_home=registry_home)
        if lease is not None:
            return lease
        reason = getattr(refusal, "reason", None)
        raise HandoffError(reason or "handoff_complete_refused", str(refusal))

    # -- step 5: hand back ------------------------------------------------- #

    def release(self, lease, *, session_id: Optional[str] = None) -> None:
        """Release the controller's lease (hand the session back to the owner)."""
        try:
            if lease is not None:
                lease.release()
        except Exception as exc:  # noqa: BLE001 — best-effort hand-back
            log.warning("handoff: failed to release lease on %s: %r",
                        session_id or self.session_id, exc)

    # -- full orchestration (request → consent → complete → drive → release) #

    def orchestrate(
        self,
        text: str,
        *,
        drive: Callable[[], Any],
        on_pending: Optional[Callable[[], None]] = None,
        on_denied: Optional[Callable[[str], None]] = None,
        session_id: Optional[str] = None,
        registry_home: Optional[Path] = None,
    ) -> dict:
        """Run the full consented-handoff turn for one app send.

        Steps (each can short-circuit to a fallback):
          1. request — seam absent → raise SEAM_UNAVAILABLE (caller falls back
             to ``session_busy``); no live owner → HandoffError (caller
             surfaces busy/refused).
          2. If already ``granted``, skip the wait; else call ``on_pending``
             (emits "waiting for approval on your computer") and poll for the
             nonce. ``on_denied`` is called on timeout/denial so the caller can
             surface a denied notice (default no-op).
          3. complete → take the lease.
          4. call ``drive()`` (the turn) — the reply streams via the existing
             store / driver path and folds back 1:1.
          5. release → hand the session back to the operator's CLI.

        Returns a status dict ``{"outcome": "completed"|"pending_emitted"|"denied"}``.

        ``on_pending``/``on_denied`` are injected so the WS caller appends the
        system frames and tests assert them — the coordinator itself is pure.
        """
        outcome: Optional[dict] = None
        try:
            granted = self.request(session_id=session_id,
                                   registry_home=registry_home)["granted"]
        except HandoffError as exc:
            if exc.code == SEAM_UNAVAILABLE:
                raise  # caller falls back to session_busy
            outcome = {"outcome": "denied", "reason": exc.code,
                       "message": exc.message}
            if on_denied is not None:
                on_denied(exc.message)
            return outcome

        if not granted:
            if on_pending is not None:
                on_pending()
            # Wait for the owner to consent. A timeout / denial is a no-op
            # for the send itself (nothing was driven) — surface it and stop.
            try:
                nonce = self.poll_for_nonce(session_id=session_id,
                                            registry_home=registry_home)
            except HandoffError as exc:
                outcome = {"outcome": "denied", "reason": exc.code,
                           "message": exc.message}
                if on_denied is not None:
                    on_denied(exc.message)
                return outcome
        else:
            # Owner already granted a nonce in an earlier round-trip: the ask
            # is moot, so complete immediately. We still need the nonce — read
            # it from the registry rather than waiting a full poll cycle (the
            # request already confirmed it is granted + unexpired).
            try:
                nonce = self.poll_for_nonce(session_id=session_id,
                                            registry_home=registry_home,
                                            timeout=0.0)
            except HandoffError as exc:
                outcome = {"outcome": "denied", "reason": exc.code,
                           "message": exc.message}
                if on_denied is not None:
                    on_denied(exc.message)
                return outcome

        # Take the lease, drive the turn, hand back.
        lease = None
        try:
            lease = self.complete(nonce, session_id=session_id,
                                  registry_home=registry_home)
        except HandoffError as exc:
            outcome = {"outcome": "denied", "reason": exc.code,
                       "message": exc.message}
            if on_denied is not None:
                on_denied(exc.message)
            return outcome
        try:
            drive()
            return {"outcome": "completed"}
        finally:
            self.release(lease, session_id=session_id)
