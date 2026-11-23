"""The leg must not be able to see an operator home (t_d24a05ea).

The class this pins is the one that hid t_4b70e8d4 / PR #593: production code
resolved a ``~``-derived default, the workstation HAS the file, the CI runner does
not, and the case asserted a field the live file happened to agree with — so the
case was green here and red only on CI, and the legs are advisory, so the red was a
warning and the job was green. The resolver was fixed; these cases pin the POSTURE,
so the next default-path read of an operator file reddens on the workstation.

What makes each of these a pin rather than a report is that the value it asserts is
the one the OPERATOR's machine would NOT give. Measured at ``db9a878d`` on this
workstation, unsealed: ``main_orch._registry_projects()`` -> 18 project rows,
``autonomy.is_on()`` -> ``True``, ``generator._live_orchestrator_endpoint()`` ->
the operator's serving endpoint, ``rolelib.PROFILES_DIR`` -> the operator's live
``~/.hermes/profiles``. Delete the seal in ``conftest.py`` and this file goes red
here (probe P1), while staying green on CI — which is the correct asymmetry: the
seal exists to make the CI-only failure visible where it is invisible today.

No skip in this file, and nothing here may become one: a case that skipped when the
seal was missing would silence its own watcher.
"""
import os

import autonomy
import generator
import main_orch
import orchestrators


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


# ── the seal itself ──────────────────────────────────────────────────────────

def test_the_leg_resolves_home_to_the_sealed_dir_not_the_login_home(leg_seal):
    home = leg_seal["home"]
    assert os.environ["HOME"] == home, (
        "HOME is not the sealed root — the leg is reading someone's real tree"
    )
    assert os.environ["USERPROFILE"] == home, (
        "USERPROFILE left unsealed: Path.home()/expanduser on Windows would still "
        "answer the login home, so the seal would be platform-shaped"
    )
    assert os.path.realpath(os.path.expanduser("~")) == home
    assert home != leg_seal["pre_seal_home"], "the seal did not move HOME off the login home"
    # rolelib.py:75-95 (t_d39c720e): hermes resolves a home that sits under
    # ~/.hermes back to the NATIVE root, so a seal dir taken from a TMPDIR inside
    # the live tree seals nothing. Refusing it is part of the seal.
    assert not _under(home, leg_seal["pre_seal_home"]), (
        f"the sealed home {home} lives inside the login home "
        f"{leg_seal['pre_seal_home']} — hermes would resolve it back to the live root"
    )


def test_no_operator_location_pin_reached_the_leg(leg_seal):
    """The two env pins that outrank a ``~`` default, absent at seal time.

    Recorded at seal time rather than re-read, because ``rolelib.apply_hermes_home``
    legitimately writes ``HERMES_HOME`` into the process during other cases — that
    is the contract of that function and must not make this claim order-dependent.
    """
    assert leg_seal["env"]["HSCC_REGISTRY"] is None, (
        "an ambient HSCC_REGISTRY outranks the documented registry default "
        "(orchestrators._registry_path reads it per call) — the leg would answer "
        "the provisioning registry and believe it answered the default"
    )
    assert leg_seal["env"]["HERMES_HOME"] is None, (
        "an ambient HERMES_HOME outranks rolelib.py:73's ~/.hermes fallback, and "
        "what it points at is a WRITE target (the t_d39c720e class)"
    )


# ── what the seal buys: the registry default, the #593 shape ─────────────────

def test_the_registry_default_resolves_under_the_seal_and_is_absent(leg_seal, monkeypatch):
    monkeypatch.delenv("HSCC_REGISTRY", raising=False)
    resolved = orchestrators._registry_path(None)
    assert resolved == os.path.join(leg_seal["home"], ".flightdeck", "registry.yaml"), (
        "the documented default did not resolve under the seal: "
        f"{resolved!r} (HSCC_REGISTRY: {os.environ.get('HSCC_REGISTRY')!r})"
    )
    assert not os.path.isfile(resolved), (
        "a default-path read found a real registry — the leg is seeing an operator "
        f"tree ({resolved}) and any assertion through it can pass for the wrong "
        "reason, which is exactly how t_4b70e8d4 hid"
    )


def test_the_registry_reader_sees_no_projects_without_a_path_or_env(leg_seal, monkeypatch):
    """No-arg resolution must answer empty here, the operator's 18 rows unsealed.

    This is the assertion t_4b70e8d4 lacked: the case that read the registry through
    the default path asserted a field the live operator registry happened to agree
    with, so it passed locally and raised UnknownProjectError on CI.
    """
    monkeypatch.delenv("HSCC_REGISTRY", raising=False)
    assert main_orch._registry_projects() == []
    assert orchestrators._load_projects(None) == []


# ── the import-time half: what a fixture-shaped seal could not cover ────────

def test_the_strong_tier_endpoint_is_the_documented_fallback_not_an_operators_serving_config(leg_seal):
    """``~/.hscc/serving.json`` is read AT IMPORT by generator, with no env seam.

    On this workstation that file exists and is a uniform-pool plan, so unsealed
    ``STRONG_URL`` equals the worker proxy — the drift test_generator.py:400-410
    records as "failed depending on whose machine ran the suite". Under the seal the
    read finds nothing and both constants keep the documented fallback.
    """
    assert generator._live_orchestrator_endpoint() is None
    resolved = leg_seal["resolved"]
    assert resolved["generator.STRONG_URL"] == "http://127.0.0.1:8000/v1"
    assert resolved["generator.COMPACT_BASE_URL"] == "http://127.0.0.1:8000/v1"


def test_the_import_time_module_roots_are_all_under_the_seal(leg_seal):
    """The frozen constants, not the live attributes (other cases patch those)."""
    home = leg_seal["home"]
    resolved = leg_seal["resolved"]
    for key in ("autonomy.HSCC_DIR", "autonomy.AUTONOMY_FILE",
                "rolelib.HERMES_HOME", "rolelib.PROFILES_DIR",
                "rolelib.REASONING_POLICY_PATH"):
        assert _under(resolved[key], home), (
            f"{key} was resolved at import to {resolved[key]!r}, outside the sealed "
            f"home {home!r} — the seal ran after the imports, or not at all"
        )


def test_the_profile_root_the_fallback_writer_writes_is_under_the_seal(leg_seal):
    """rolelib.PROFILES_DIR is where the fallback writer writes profiles.

    Unsealed it is the operator's live ``~/.hermes/profiles`` (``HERMES_HOME`` is
    exported to every dispatched worker, so the ambient value wins over the
    ``~/.hermes`` default). A leg whose default write target is the operator's tree
    is the t_d39c720e incident, one missing monkeypatch away.
    """
    assert _under(leg_seal["resolved"]["rolelib.PROFILES_DIR"], leg_seal["home"])
    assert leg_seal["resolved"]["rolelib.PROFILES_DIR"] == os.path.join(
        leg_seal["resolved"]["rolelib.HERMES_HOME"], "profiles")


# ── the two per-test seams still work under the seal ────────────────────────

def test_the_autonomy_flag_read_is_not_the_operators(leg_seal):
    """``~/.hscc/autonomy`` — the flag file has no env override at all.

    On this host the operator's flag is ``on``, so an unsealed ``is_on()`` answers
    True and on CI False: a live behavioural delta the leg only survives because
    test_autonomy.py re-points the constant per case.
    """
    assert _under(leg_seal["resolved"]["autonomy.AUTONOMY_FILE"], leg_seal["home"])
    assert autonomy.is_on() is False


def test_the_reasoning_policy_default_is_under_the_seal_and_absent(leg_seal, monkeypatch):
    """The `HSCC_REASONING_POLICY` -> ``~/.hscc/state/…`` default, sealed by both
    mechanisms: the process seal moves the default, the autouse fixture re-points
    the module attribute per case (so this asserts the snapshot, not the attribute).
    """
    monkeypatch.delenv("HSCC_REASONING_POLICY", raising=False)
    assert leg_seal["resolved"]["rolelib.REASONING_POLICY_PATH"] == os.path.join(
        leg_seal["home"], ".hscc", "state", "reasoning_policy.json")
    assert not os.path.isfile(leg_seal["resolved"]["rolelib.REASONING_POLICY_PATH"])
