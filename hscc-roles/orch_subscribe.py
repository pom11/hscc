"""Kanban gateway-subscription policy for orchestrators (t_31c8b258).

A pane-backed orchestrator must not ALSO receive gateway ``notify+wake``
subscriptions. Its wake path is the tmux pane plus the hscc orch-waker; a
gateway subscription pointing at the same profile's gateway chat drives a
SECOND copy of the same identity. That is the 2026-11-23 incident: 307
``notify+wake`` subs targeted a gateway ``local`` session while the real
``hscc-orch`` lived in a pane, so the gateway kept waking a duplicate orch.

The durable switch is Hermes' own per-profile config gate
``kanban.auto_subscribe_on_create``, which ``kanban_create`` consults before
writing a subscription. Pane-backed-ness is read from Hermes' public
active-session registry (``<profile>/runtime/active_sessions.json``): a lease
with ``surface == "cli"`` is a live REPL, i.e. a pane. Tests inject that
snapshot, so no suite touches a real registry or tmux server.

Every unknown fails open: an unreadable registry is NOT proof of a pane, so
the gate is left alone (a missed wake for a paneless orchestrator is worse
than the duplicate this module exists to prevent). A missing/unparseable
profile config is likewise reported and skipped, never created here.
"""

from __future__ import annotations

import json
import os

DEFAULT_HERMES_HOME = "~/.hermes"
#: The active-session lease surface that means "an interactive REPL", i.e. the
#: pane path. Gateway/TUI/api leases are other surfaces and do not qualify.
PANE_SURFACE = "cli"
#: The Hermes config gate this policy owns. One key, under ``kanban``.
AUTO_SUBSCRIBE_KEY = "auto_subscribe_on_create"
REASON_PANE_BACKED = "pane-backed-orchestrator"
REASON_NO_LIVE_PANE = "no-live-pane"


def _hermes_root(hermes_home=None):
    """The Hermes install root (a profile-scoped ``HERMES_HOME`` is walked up)."""
    home = os.path.expanduser(
        str(hermes_home or os.environ.get("HERMES_HOME") or DEFAULT_HERMES_HOME))
    parent = os.path.dirname(home)
    if os.path.basename(parent) == "profiles":
        return os.path.dirname(parent)
    return home


def profile_home(profile, *, hermes_home=None, profile_dir=None):
    """The profile's own home. ``profile_dir`` wins (the generator's resolved
    root, which tests pin at a tmp tree)."""
    if profile_dir:
        return os.path.expanduser(str(profile_dir))
    return os.path.join(_hermes_root(hermes_home), "profiles", str(profile))


def read_active_sessions(profile, *, hermes_home=None, profile_dir=None):
    """Raw lease entries from Hermes' active-session registry.

    Read-only and fail-open: a missing/unreadable/corrupt registry is ``[]``
    ("cannot prove a pane"), never a guess and never a write. We deliberately
    do not use Hermes' snapshot API here because it prunes dead leases (a
    write); this policy is a consumer of the registry, not its owner.
    """
    path = os.path.join(
        profile_home(profile, hermes_home=hermes_home, profile_dir=profile_dir),
        "runtime", "active_sessions.json")
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return []
    entries = doc.get("entries") if isinstance(doc, dict) else None
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def live_pane_sessions(profile, *, registry=None, hermes_home=None,
                       profile_dir=None):
    """Session ids of the profile's live CLI (pane) leases.

    ``registry`` is the injectable snapshot: an iterable of lease dicts or a
    callable ``(profile_home) -> iterable``. Without it the registry file is
    read read-only. Duplicate ids are collapsed; non-CLI surfaces are ignored.
    """
    if callable(registry):
        try:
            entries = registry(
                profile_home(profile, hermes_home=hermes_home,
                             profile_dir=profile_dir)) or []
        except Exception:  # noqa: BLE001 — unreadable input is "no proof"
            entries = []
    elif registry is not None:
        entries = list(registry)
    else:
        entries = read_active_sessions(
            profile, hermes_home=hermes_home, profile_dir=profile_dir)
    sessions = []
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("surface") or "").strip().lower() != PANE_SURFACE:
            continue
        sid = entry.get("session_id") or entry.get("live_session_id")
        if sid is None:
            continue
        sid = str(sid)
        if sid and sid not in sessions:
            sessions.append(sid)
    return sessions


def subscription_decision(profile, target_session=None, *, registry=None,
                          hermes_home=None, profile_dir=None, pane_backed=None):
    """Should a gateway notify+wake subscription be created for ``profile``?

    ``allowed`` is False for a pane-backed orchestrator: the pane + orch-waker
    is the wake path, so a gateway sub would be a second driver. When
    ``target_session`` equals one of the live pane sessions the subscription
    is pointed at the pane itself, which is reported separately
    (``target_is_live_pane``) so a caller can name the drift exactly.

    ``pane_backed`` is the explicit override for a caller that just created or
    reused a pane and therefore does not need the registry's confirmation.
    """
    if pane_backed is None:
        panes = live_pane_sessions(
            profile, registry=registry, hermes_home=hermes_home,
            profile_dir=profile_dir)
        pane_backed = bool(panes)
    else:
        panes = []
    target = str(target_session) if target_session is not None else None
    return {
        "profile": str(profile),
        "allowed": not pane_backed,
        "reason": REASON_NO_LIVE_PANE if not pane_backed else REASON_PANE_BACKED,
        "pane_backed": bool(pane_backed),
        "pane_sessions": panes,
        "target_session": target,
        "target_is_live_pane": bool(target and target in panes),
    }


def apply_subscription_policy(profile, *, profile_dir=None, registry=None,
                              hermes_home=None, pane_backed=None,
                              dry_run=False):
    """Turn Hermes' auto-subscribe gate OFF when the orch is pane-backed.

    Idempotent and fail-soft: a paneless orchestrator is left exactly as the
    operator configured it, and a missing/unreadable config is reported
    without being created. Returns the decision dict plus the write result
    (``action`` / ``changed`` / ``config_path``).
    """
    decision = subscription_decision(
        profile, registry=registry, hermes_home=hermes_home,
        profile_dir=profile_dir, pane_backed=pane_backed)
    pdir = profile_home(profile, hermes_home=hermes_home,
                        profile_dir=profile_dir)
    decision["config_path"] = os.path.join(pdir, "config.yaml")
    if decision["allowed"]:
        decision.update(action="kept", changed=False)
        return decision
    try:
        import generator  # local import: this module stays import-light
        result = generator.set_kanban_auto_subscribe(
            pdir, False, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 — provisioning must not fail here
        decision.update(action="error", changed=False, error=str(exc))
        return decision
    decision.update(result)
    return decision
