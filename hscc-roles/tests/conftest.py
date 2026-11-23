"""Leg-level isolation for the ``hscc-roles`` suite.

Two per-test fixtures (below) re-point machine inputs a case must not read, and one
process-level seal (the block above them) re-points the input every case inherits.
The split is deliberate and measured — see ``HOME-SEAL`` for why the process-level
one cannot be a fixture.
"""
import atexit
import os
import shutil
import sys
import tempfile

import pytest

# Make the plugin modules importable as top-level (mirrors hscc-cluster tests).
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)


# ━━━ HOME-SEAL: the leg runs with ``~`` pointed at an empty dir ━━━━━━━━━━━━━
# (t_d24a05ea)
#
# WHY. ``orchestrators._registry_path`` resolved ``~/.flightdeck/registry.yaml``,
# the operator's workstation HAS that file and the CI runner does not, and the case
# asserted a field the live file happened to agree with: green locally, red only on
# CI, and advisory-red at that (``pytest-legs.yml``: ``HSCC_TESTS_ENFORCE`` unset ->
# ``::warning a test leg is red``, job green). It hid across two merges. The
# resolver is fixed (PR #593); the POSTURE that hid it is what this block closes.
#
# MEASURED at ``db9a878d`` (roster env, one pytest process per leg, as
# ``run_tests.sh`` runs it): this leg cannot see the difference TODAY —
# ``358 passed`` under the operator's HOME and ``358 passed`` under an empty HOME,
# 0 skips both ways (the 10 skips CI reports are the two ``hermes_gap``
# capabilities, unrelated). What the same measurement shows the code DOES answer
# differently: ``main_orch._registry_projects()`` returns the operator's 18 projects
# here and ``[]`` on CI; ``autonomy.is_on()`` answers True here (the operator's
# ``~/.hscc/autonomy`` is ``on``) and False there; ``generator.STRONG_URL`` comes
# from the operator's ``~/.hscc/serving.json`` here and from the documented fallback
# there. Every one of those is hidden only because each case injects its own seam.
# So the seal costs nothing (no item lost, no skip added, no assertion weakened) and
# buys the thing this card is for: the NEXT default-path read reddens HERE, on the
# workstation, instead of only on CI.
#
# WHY HERE AND NOT IN AN AUTOUSE FIXTURE (the shape this card was offered). An
# autouse fixture runs after collection, so it cannot seal what is resolved at
# IMPORT: ``autonomy.HSCC_DIR``/``AUTONOMY_FILE`` (``autonomy.py:11``, which reads
# no env var at all), ``rolelib.HERMES_HOME``/``PROFILES_DIR`` (``rolelib.py:73``),
# and ``generator.STRONG_URL``/``COMPACT_BASE_URL`` (``generator.py:96,134`` reading
# ``~/.hscc/serving.json`` through ``_live_orchestrator_endpoint()`` at import, with
# no env seam). Import-time resolution is exactly the mechanism that hid
# t_4b70e8d4, so a fixture-shaped seal would have left the class open. Pinned by
# ``test_home_seal.py`` — probe P3 (the seal moved below the imports) reddens it.
#
# WHY THESE KEYS AND NOT MORE. Each names a location the leg resolves and an
# operator value overrides:
#   ``HOME``/``USERPROFILE``  the ``~``-derived defaults above (Windows spells the
#                             same input with ``USERPROFILE``; leaving it unsealed
#                             would leave ``Path.home()`` on the operator's tree).
#   ``HSCC_REGISTRY``         the registry override ``_registry_path`` reads per
#                             call (#593); an exported value outranks the default.
#   ``HERMES_HOME``           ``rolelib.py:73``'s ``~/.hermes`` fallback. A
#                             dispatched worker exports it (it is the profile dir),
#                             so unsealed this leg's fallback writer resolves the
#                             OPERATOR'S LIVE ``~/.hermes/profiles`` — a write
#                             target, the t_d39c720e class. Scrubbing it here is
#                             leg-local on purpose: a roster-wide scrub has to be
#                             measured against every leg's hermes-gated cases first
#                             (carded; ``run_tests.sh`` gets only ``HSCC_REGISTRY``).
#
# THE TRAP THIS GUARDS. A sandbox that lives INSIDE the live tree is not a sandbox:
# ``hermes_constants._hermes_root_of`` answers the native root whenever ``HERMES_HOME``
# resolves under ``~/.hermes``, so a seal directory taken from a ``TMPDIR`` that sits
# under ``~/.hermes/profiles/<p>/cache/scratch`` (which is where this fleet's scratch
# lives, and where a dispatched worker's ``TMPDIR`` points) would seal ``HOME`` and
# still hand the leg the operator's real ``~/.hermes``. Measured and documented at
# rolelib.py:75-95 (t_d39c720e). So the candidate root is checked against the login
# home and refused, not trusted.

#: Location pins this leg must not inherit. Measured, block above.
SEALED_ENV_KEYS = ("HSCC_REGISTRY", "HERMES_HOME")

#: The login home BEFORE the seal. Kept so the guard can name what moved, and so
#: the containment check below has a pre-seal value to compare against.
PRE_SEAL_HOME = os.path.realpath(os.path.expanduser("~"))


def _make_sealed_home() -> str:
    """A fresh empty home for this leg process, outside the operator's tree.

    ``/tmp`` first, then ``tempfile.gettempdir()``: the second honour ``TMPDIR``,
    which on this fleet points inside ``~/.hermes/...`` and would seal nothing (see
    the trap above). Refuses rather than silently sealing into the live tree — a
    seal that does not seal has to stop the leg, not green it.
    """
    roots = []
    if os.path.isdir("/tmp"):
        roots.append("/tmp")
    roots.append(tempfile.gettempdir())
    refused = []
    for root in roots:
        try:
            path = os.path.realpath(tempfile.mkdtemp(prefix="hscc-roles-leg-home-", dir=root))
        except OSError as exc:  # unreadable/unwritable TMPDIR candidate
            refused.append(f"{root}: {type(exc).__name__}: {exc}")
            continue
        if path == PRE_SEAL_HOME or path.startswith(PRE_SEAL_HOME + os.sep):
            shutil.rmtree(path, ignore_errors=True)
            refused.append(f"{root}: inside the login home {PRE_SEAL_HOME} — not a sandbox")
            continue
        return path
    raise RuntimeError(
        "cannot seal HOME for this leg: no temp root outside the login home "
        f"{PRE_SEAL_HOME}. Refused: {'; '.join(refused) or 'no candidates'} — "
        "a leg that reads the operator's home while believing it is sealed is "
        "worse than a leg that stops (t_d24a05ea)."
    )


#: The sealed ``HOME`` every case of this leg resolves ``~`` against.
SEALED_HOME = _make_sealed_home()
atexit.register(shutil.rmtree, SEALED_HOME, ignore_errors=True)

os.environ["HOME"] = SEALED_HOME
os.environ["USERPROFILE"] = SEALED_HOME
for _key in SEALED_ENV_KEYS:
    os.environ.pop(_key, None)

#: The environment as it stood AFTER the seal, for the guard to assert. Recorded
#: rather than re-read, so a case that patches one of these later cannot make the
#: seal's own contract order-dependent.
SEAL_BASELINE = {
    "home": SEALED_HOME,
    "pre_seal_home": PRE_SEAL_HOME,
    "env": {"HOME": os.environ.get("HOME"), "USERPROFILE": os.environ.get("USERPROFILE"),
            **{k: os.environ.get(k) for k in SEALED_ENV_KEYS}},
}


def _record_import_time_resolution():
    """Freeze what the plugin modules resolve at IMPORT, under the seal.

    These three modules resolve a ``~``-derived default while being imported, so the
    values below are the posture's answer, not a later case's: ``autonomy.py:11``
    (``HSCC_DIR``, which reads no env var at all), ``rolelib.py:73``
    (``HERMES_HOME`` -> ``PROFILES_DIR``, a WRITE target) and ``generator.py:96,134``
    (``STRONG_URL``/``COMPACT_BASE_URL``, from ``_live_orchestrator_endpoint()``
    reading ``~/.hscc/serving.json`` with no env seam). Importing them here, after
    the seal and before pytest imports a test module, is also what guarantees the
    seal is the first thing that runs — if the seal ever moves below these imports,
    the values below come back as the operator's and ``test_home_seal.py`` reddens.
    """
    import autonomy
    import generator
    import rolelib
    SEAL_BASELINE["resolved"] = {
        "autonomy.HSCC_DIR": autonomy.HSCC_DIR,
        "autonomy.AUTONOMY_FILE": autonomy.AUTONOMY_FILE,
        "rolelib.HERMES_HOME": rolelib.HERMES_HOME,
        "rolelib.PROFILES_DIR": rolelib.PROFILES_DIR,
        "rolelib.REASONING_POLICY_PATH": rolelib.REASONING_POLICY_PATH,
        "generator.STRONG_URL": generator.STRONG_URL,
        "generator.COMPACT_BASE_URL": generator.COMPACT_BASE_URL,
    }


_record_import_time_resolution()


@pytest.fixture(scope="session")
def leg_seal():
    """The facts of the HOME seal as they stood when it ran (t_d24a05ea)."""
    return dict(SEAL_BASELINE)


@pytest.fixture(autouse=True)
def _no_real_reasoning_policy(tmp_path, monkeypatch):
    """Never read the machine's applied reasoning policy from a test: the
    generator would otherwise emit agent/delegation blocks depending on what
    the last real `hscc template apply` recorded."""
    import rolelib
    monkeypatch.setattr(rolelib, "REASONING_POLICY_PATH",
                        str(tmp_path / "no-reasoning-policy.json"))


@pytest.fixture(autouse=True)
def _isolated_main_orchestrator_state(tmp_path, monkeypatch):
    """Never read the machine's stored main-orchestrator name from a test.

    The resolver falls back to ``general`` when nothing is stored — deterministic
    only when ``HSCC_DIR`` points at a throwaway root, never the operator's
    ``~/.hscc``.
    """
    monkeypatch.setenv("HSCC_DIR", str(tmp_path / "hscc-state"))
