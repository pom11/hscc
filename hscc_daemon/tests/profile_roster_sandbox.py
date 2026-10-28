"""Deterministic profile-roster gate — no Hermes import, no path guessing.

t_1c545ebb: ``profile_roster._hermes_gate()`` resolves Hermes' predicates
lazily, which means the *fallback* branch decides real-vs-stub whenever
``hermes_constants`` is not importable. That is fine for the deployed daemon,
but as a test seam it is ambient: whether a given assertion exercises the
mirror or Hermes' own predicate would depend on ``sys.path`` at the moment the
test ran. The two branches must be pinned by the SAME cases, so the seam is
separated: tests inject the predicates explicitly and each branch is exercised
regardless of the interpreter it runs under.
"""

from __future__ import annotations

from hscc_daemon import profile_roster as pr

__all__ = ["roster_with"]


def _never_standalone(home):  # nothing opts out unless a test says so
    return False


def roster_with(profiles_dir, *, id_re, has_identity, is_deleted,
                is_standalone=_never_standalone):
    """``list_profiles_on_disk`` with Hermes' predicates supplied explicitly.

    ``id_re`` / ``has_identity`` / ``is_deleted`` / ``is_standalone`` mirror
    ``hermes_constants.PROFILE_ID_RE`` / ``named_profile_has_identity`` /
    ``named_profile_is_deleted`` / ``hermes_cli.profiles.profile_is_standalone``.
    """
    original = pr._hermes_gate
    pr._hermes_gate = lambda: (id_re, has_identity, is_deleted, is_standalone)
    try:
        return pr.list_profiles_on_disk(profiles_dir)
    finally:
        pr._hermes_gate = original


def roster_fallback(profiles_dir):
    """``list_profiles_on_disk`` forced down the no-Hermes fallback branch."""
    original = pr._hermes_gate
    pr._hermes_gate = lambda: None
    try:
        return pr.list_profiles_on_disk(profiles_dir)
    finally:
        pr._hermes_gate = original
