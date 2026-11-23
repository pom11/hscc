"""The main-only cluster-surface doctor row (card t_caf98114).

Two legs, per the doctor-row test layout:

1. the FEATURE side (:mod:`doctor_main_only`) — every posture pinned against
   fabricated profile trees the module reads for real, so the verdict is pinned
   against what an install actually looks like, not against a fixture's guess;
2. the ROW side (``doctor.py`` + the registry) — routing through the seam, the
   in-suite refusal, the unverifiable case, and the context the row declares.

The fixtures name the main profile ``acme-orch`` and store it in
``main-orchestrator.json``, because the check must get the identity from
``hscc-roles/main_orch.main_name()`` and the suite would go red the moment anyone
hardcoded an identity here (binding ruling R2/D-A, pinned by
:func:`test_module_source_carries_no_hardcoded_identity`).

Nothing in here reads the operator's install: every read is rooted at ``tmp_path``,
and :func:`test_no_read_escapes_the_asked_about_root` is the canary that proves it.
"""

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BOOT_DIR = os.path.dirname(_HERE)
_REPO_ROOT = os.path.dirname(_BOOT_DIR)
for _p in (_BOOT_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import doctor  # noqa: E402
import doctor_main_only as dmo  # noqa: E402
import doctor_rows  # noqa: E402

MAIN = "acme-orch"
WORKER = "backend-engineer"
CLEAN = "clean-worker"
ALL_THREE = ["hscc-cluster", "hscc-commands", "sparkrun-hermes"]

#: The skills whose presence on a non-main profile the row calls drift, and
#: which the installer must therefore declare. The row reads this list out of
#: ``hscc-skills/hscc.py``; this constant is the pin that keeps the fallback in
#: doctor_main_only from drifting.
CLUSTER_SKILLS = ("hscc", "hscc-cluster", "hscc-model-onboard")


# ── fabrication helpers ─────────────────────────────────────────────────────

def mk_profile(home, name, *, enabled=None, skills=(), plugin_dirs=(),
               yaml_extra=""):
    """Fabricate ``<home>/profiles/<name>`` with a config + skills tree.

    ``enabled=None`` writes no ``plugins`` key at all (the audit's measured 0/52
    posture); ``plugin_dirs`` creates on-disk payload dirs, which are NOT drift.
    """
    d = os.path.join(str(home), "profiles", name)
    os.makedirs(os.path.join(d, "skills"), exist_ok=True)
    with open(os.path.join(d, "config.yaml"), "w", encoding="utf-8") as f:
        if enabled is not None:
            f.write("plugins:\n  enabled: [%s]\n" % ", ".join(enabled))
        f.write(yaml_extra)
    for s in skills:
        sk = os.path.join(d, "skills", s)
        os.makedirs(sk, exist_ok=True)
        with open(os.path.join(sk, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: %s\n---\nbody\n" % s)
    for p in plugin_dirs:
        os.makedirs(os.path.join(d, "plugins", p), exist_ok=True)
    return d


def mk_root(home, *, enabled=None, skills=()):
    """The root home: the operator's own ``hermes`` (never examined by the row)."""
    os.makedirs(os.path.join(str(home), "skills"), exist_ok=True)
    with open(os.path.join(str(home), "config.yaml"), "w",
              encoding="utf-8") as f:
        if enabled is not None:
            f.write("plugins:\n  enabled: [%s]\n" % ", ".join(enabled))
    for s in skills:
        os.makedirs(os.path.join(str(home), "skills", s), exist_ok=True)
    return home


def store_main(hscc_dir, name):
    """The resolver's stored identity — the only place a main name is written."""
    os.makedirs(str(hscc_dir), exist_ok=True)
    with open(os.path.join(str(hscc_dir), "main-orchestrator.json"), "w",
              encoding="utf-8") as f:
        json.dump({"name": name}, f)
    return hscc_dir


def posture(tmp_path, *, worker_enabled=None, worker_skills=(),
            main_enabled=None, main_skills=(), root_enabled=None,
            root_skills=(), no_main_home=False):
    """Build a three-home fleet and return ``(hermes_home, hscc_dir)``."""
    home = tmp_path / ".hermes"
    hscc = tmp_path / ".hscc"
    mk_root(home, enabled=root_enabled, skills=root_skills)
    if not no_main_home:
        mk_profile(home, MAIN, enabled=main_enabled, skills=main_skills)
    mk_profile(home, WORKER, enabled=worker_enabled, skills=worker_skills)
    mk_profile(home, CLEAN, enabled=["hscc-cluster"], skills=("caveman",))
    store_main(hscc, MAIN)
    return home, hscc


def clean_posture(tmp_path):
    """The posture the fix produces: main has it, the workers have the hooks."""
    return posture(tmp_path, main_enabled=list(ALL_THREE),
                   main_skills=CLUSTER_SKILLS + ("caveman",),
                   worker_enabled=["hscc-cluster"], worker_skills=("caveman",),
                   root_enabled=list(ALL_THREE))


# ── feature side: the two flags ────────────────────────────────────────────

def test_both_flags_fire_with_exact_profile_surface_and_names(tmp_path):
    """The card's required posture: a worker seeded with a stray skill AND a
    per-profile plugins block — both flags fire, names exact."""
    home, hscc = posture(
        tmp_path, worker_enabled=list(ALL_THREE),
        worker_skills=("hscc-cluster", "caveman"),
        main_enabled=list(ALL_THREE), main_skills=CLUSTER_SKILLS,
        root_enabled=list(ALL_THREE))
    res = dmo.doctor_check(str(home), str(hscc))
    assert res["ok"] is False and res["fatal"] is False
    findings = dmo.home_findings(str(home), main_name=MAIN,
                                 tool_plugins=["hscc-commands",
                                               "sparkrun-hermes"],
                                 skills=list(CLUSTER_SKILLS))
    assert [(f["profile"], f["surface"], tuple(f["names"]))
            for f in findings] == [
        (WORKER, "plugins.enabled", ("hscc-commands", "sparkrun-hermes")),
        (WORKER, "skills", ("hscc-cluster",)),
    ], "exactly the two seeded surfaces, nothing else, in sweep order"
    # The verdict wording names who, what and which — an operator must be able
    # to act from the row alone.
    assert WORKER in res["detail"]
    assert "hscc-commands" in res["detail"] and "hscc-cluster" in res["detail"]
    assert "plugins.enabled" in res["detail"] and "skills" in res["detail"]
    assert MAIN in res["detail"], "the detail states which home is the owner"
    assert "hscc-bootstrap" not in res["detail"]  # detail is a finding, not a fix
    for want in (WORKER, "plugins.enabled", "skills"):
        assert want in res["detail"]
    assert "t_e98c6ad4" in res["fix"], "the fix names the bootstrap re-run"
    assert "skills-backups/" in res["fix"] and ".bak-" in res["fix"], \
        "the fix names the dated backup dir the moved copies live in"
    assert "hscc-cluster']" in res["fix"], "the fix says what to keep enabled"


def test_clean_posture_is_silent(tmp_path):
    """The fixed install produces no findings — the row's green is the shape
    bootstrap writes, not an absence of checking."""
    home, hscc = clean_posture(tmp_path)
    res = dmo.doctor_check(str(home), str(hscc))
    assert res["ok"] is True and res["fatal"] is False
    assert res["fix"] == ""
    assert MAIN in res["detail"]


def test_root_home_is_never_reported(tmp_path):
    """The root home keeps all three plugins and the bundled skills (driver
    ruling 3 + audit §6 "unchanged"). Scanning it would call the operator's own
    install drift; this pins the exemption."""
    home, hscc = posture(
        tmp_path, main_enabled=list(ALL_THREE), main_skills=CLUSTER_SKILLS,
        root_enabled=list(ALL_THREE), root_skills=list(CLUSTER_SKILLS))
    res = dmo.doctor_check(str(home), str(hscc))
    assert res["ok"] is True, res["detail"]
    findings = dmo.home_findings(str(home), main_name=MAIN,
                                 tool_plugins=list(dmo._FALLBACK_TOOL_PLUGINS),
                                 skills=list(CLUSTER_SKILLS))
    assert [f["profile"] for f in findings] == []


def test_hscc_cluster_enabled_everywhere_is_not_drift(tmp_path):
    """Binding ruling R2/D-C: the hook plugin stays enabled in every home.

    Two directions, both must bite: the declared list must exclude it, and a
    worker enabled ONLY for it must stay clean.
    """
    home, hscc = posture(tmp_path, worker_enabled=["hscc-cluster"],
                         main_enabled=list(ALL_THREE),
                         main_skills=CLUSTER_SKILLS)
    assert dmo.doctor_check(str(home), str(hscc))["ok"] is True
    assert "hscc-cluster" not in dmo.cluster_tool_plugins()
    # Mutation witness: put the hook plugin back into the tool list and the very
    # same clean posture goes red — the exclusion is load-bearing, not inert.
    dirty = dmo.doctor_check(str(home), str(hscc),
                             tool_plugins=["hscc-cluster", "hscc-commands",
                                           "sparkrun-hermes"])
    assert dirty["ok"] is False and WORKER in dirty["detail"]


def test_on_disk_plugin_dir_is_not_drift(tmp_path):
    """``install_payload_profiles`` deploys the payload everywhere and the hooks
    need it there; only ENABLEMENT loads a plugin (audit §5.2)."""
    home = tmp_path / ".hermes"
    hscc = tmp_path / ".hscc"
    mk_root(home)
    mk_profile(home, MAIN, enabled=list(ALL_THREE), skills=CLUSTER_SKILLS)
    mk_profile(home, WORKER, enabled=["hscc-cluster"],
               plugin_dirs=("hscc-cluster", "hscc-commands", "sparkrun-hermes"))
    store_main(hscc, MAIN)
    res = dmo.doctor_check(str(home), str(hscc))
    assert res["ok"] is True, res["detail"]


def test_cosmetic_toolsets_entry_is_not_drift(tmp_path):
    """Audit §6 rule 2: a top-level ``toolsets:`` naming a cluster toolset is
    "cosmetic, not a control" — flagging it would drown the real findings."""
    home, hscc = posture(
        tmp_path, main_enabled=list(ALL_THREE), main_skills=CLUSTER_SKILLS,
        worker_skills=(),
        worker_enabled=None)
    with open(os.path.join(str(home), "profiles", WORKER, "config.yaml"),
              "a", encoding="utf-8") as f:
        f.write("toolsets: [hermes-cli, kanban, hscc-cluster, sparkrun]\n")
    assert dmo.doctor_check(str(home), str(hscc))["ok"] is True


def test_skills_flag_ignores_generic_bundled_skills(tmp_path):
    """``BUNDLED_SKILLS`` as a whole is NOT the predicate — the generic half
    belongs on every profile, so a worker full of it must stay clean."""
    home, hscc = posture(
        tmp_path, main_enabled=list(ALL_THREE), main_skills=CLUSTER_SKILLS,
        worker_enabled=["hscc-cluster"],
        worker_skills=("brainstorming", "caveman", "sdlc-review",
                       "systematic-debugging", "devops"))
    assert dmo.doctor_check(str(home), str(hscc))["ok"] is True


# ── feature side: the <main> half ──────────────────────────────────────────

def test_main_missing_surface_is_reported(tmp_path):
    """Audit §8 for this card: "``<main>`` loads all three". The audit's own
    headline was that <main> had NO cluster tools — this is that finding."""
    home, hscc = posture(
        tmp_path, main_enabled=["hscc-cluster"],   # the two tool plugins absent
        main_skills=("hscc", "hscc-cluster"),      # one skill absent
        worker_enabled=["hscc-cluster"])
    findings = dmo.home_findings(str(home), main_name=MAIN,
                                 tool_plugins=list(dmo._FALLBACK_TOOL_PLUGINS),
                                 skills=list(CLUSTER_SKILLS))
    assert [(f["profile"], f["surface"], tuple(f["names"]))
            for f in findings] == [
        (MAIN, "plugins.enabled", ("hscc-commands", "sparkrun-hermes")),
        (MAIN, "skills", ("hscc-model-onboard",)),
    ]
    res = dmo.doctor_check(str(home), str(hscc))
    assert res["ok"] is False and res["fatal"] is False
    assert "hscc-skills install-skills" in res["fix"], \
        "the missing-skills fix is the installer, not a hand-copy"


def test_missing_main_home_is_a_finding(tmp_path):
    """A stored identity with no profile home is the loudest drift there is."""
    home, hscc = posture(tmp_path, no_main_home=True,
                         worker_enabled=["hscc-cluster"])
    findings = dmo.home_findings(str(home), main_name=MAIN,
                                 tool_plugins=list(dmo._FALLBACK_TOOL_PLUGINS),
                                 skills=list(CLUSTER_SKILLS))
    assert [(f["surface"], tuple(f["names"])) for f in findings] == [
        ("profile", (MAIN,))]
    res = dmo.doctor_check(str(home), str(hscc))
    assert res["ok"] is False and MAIN in res["detail"]
    assert "create the <main> profile home" in res["fix"]


def test_identity_is_resolved_not_hardcoded(tmp_path):
    """R2/D-A, behaviourally: moving the stored identity moves WHICH home is
    exempt, and the exempt one stops being reported. A hardcoded name cannot do
    that, and neither can a check that guessed from the profile's own tools."""
    home = tmp_path / ".hermes"
    hscc = tmp_path / ".hscc"
    mk_root(home)
    seeded = (list(ALL_THREE), CLUSTER_SKILLS)
    for name in ("alpha-orch", "beta-orch"):
        mk_profile(home, name, enabled=seeded[0], skills=seeded[1])
    store_main(hscc, "alpha-orch")
    flagged = lambda: sorted({f["profile"] for f in dmo.home_findings(
        str(home), main_name=dmo.main_profile_name(str(hscc)),
        tool_plugins=list(dmo._FALLBACK_TOOL_PLUGINS),
        skills=list(CLUSTER_SKILLS))})
    assert flagged() == ["beta-orch"]
    store_main(hscc, "beta-orch")
    assert flagged() == ["alpha-orch"]
    assert dmo.main_profile_name(str(hscc)) == "beta-orch"


def test_dot_dirs_are_not_profiles(tmp_path):
    """The 53-vs-54 denominator correction (operator re-measure, 2026-11-23).

    ``~/.hermes/profiles/.deleted`` is a recycle dir (dated Oct 26, holding two
    ``probe-*`` markers), not a profile — a ``find -maxdepth 1 -type d``-style
    glob counts it, and the audit's §5.1 denominator went 53 → 54 that way. A
    sweep that treated it as a home would ask for plugins on a directory no
    process can select as ``HERMES_HOME`` and report the same false finding on
    every future run, which is the failure mode this pins out. The exclusion is
    ``skill_guard.discover_profiles``' own rule (dot-dir or not-a-dir → not a
    profile), inherited by delegating rather than globbing again.
    """
    home, hscc = clean_posture(tmp_path)
    # A recycle dir full of drift: every tool plugin enabled, all three cluster
    # skills present. If it counted as a home, two findings would fire.
    mk_profile(home, ".deleted", enabled=list(ALL_THREE),
               skills=CLUSTER_SKILLS)
    os.makedirs(os.path.join(str(home), "profiles", ".deleted",
                             "probe-20261026-a"), exist_ok=True)
    assert dmo.doctor_check(str(home), str(hscc))["ok"] is True
    found = [name for name, _ in dmo.discover_profiles(str(home))]
    assert found == sorted([MAIN, WORKER, CLEAN]), \
        "the denominator is the real profile dirs, not every dir under profiles/"
    # A dot-file too, since a glob over entries sees those as well.
    open(os.path.join(str(home), "profiles", ".keep"), "w").close()
    assert dmo.doctor_check(str(home), str(hscc))["ok"] is True


def test_a_home_with_no_config_yet_is_not_invented(tmp_path):
    """A profile dir with no ``config.yaml`` declares nothing, so it must not be
    reported for the plugins half — but its skills tree is still its own."""
    home, hscc = clean_posture(tmp_path)
    mk_profile(home, "brand-new", enabled=None, skills=("hscc-cluster",))
    res = dmo.doctor_check(str(home), str(hscc))
    findings = dmo.home_findings(str(home), main_name=MAIN,
                                 tool_plugins=list(dmo._FALLBACK_TOOL_PLUGINS),
                                 skills=list(CLUSTER_SKILLS))
    assert [(f["profile"], f["surface"]) for f in findings] == [
        ("brand-new", "skills")]
    assert "brand-new" in res["detail"]


def test_module_source_carries_no_hardcoded_identity():
    """R2/D-A as a text pin: the retired fallback identity never appears in the
    module, so the resolver stays the only owner of it."""
    src = open(os.path.join(_BOOT_DIR, "doctor_main_only.py"),
               encoding="utf-8").read()
    for literal in ("general", "general-orch", "GENERAL_PROFILE",
                    "GENERAL_BOARD", "LEGACY_"):
        assert literal not in src, f"doctor_main_only.py hardcodes {literal}"
    assert "main_name(" in src, "the read must go through the resolver"


# ── feature side: honest failure ───────────────────────────────────────────

def test_unreadable_skill_declaration_reports_instead_of_passing(tmp_path,
                                                                 monkeypatch):
    """The skills half must never go quiet because the declaration vanished."""
    home, hscc = clean_posture(tmp_path)
    monkeypatch.setattr(dmo, "_SKILLS_INSTALLER",
                        os.path.join(str(tmp_path), "nope", "hscc.py"))
    monkeypatch.setattr(dmo, "_installer_candidates", lambda: [])
    res = dmo.doctor_check(str(home), str(hscc))
    assert res["ok"] is True, "no plugins.enabled finding, so not a red"
    assert "unverifiable" in res["detail"]
    # And when there IS a finding, the caveat rides along in the fix.
    mk_profile(home, WORKER, enabled=list(ALL_THREE))
    dirty = dmo.doctor_check(str(home), str(hscc))
    assert dirty["ok"] is False
    assert "unverifiable" in dirty["fix"]
    assert "hscc-skills/hscc.py" in dirty["fix"]


def test_unresolvable_identity_is_an_error_not_a_guess(tmp_path, monkeypatch):
    """If the resolver is unreachable the row cannot tell <main> from a worker,
    so it must say so rather than exempt the wrong home."""
    home, hscc = posture(tmp_path, worker_enabled=list(ALL_THREE))
    monkeypatch.setattr(dmo, "_load_main_orch",
                        lambda: (_ for _ in ()).throw(ImportError("gone")))
    res = dmo.doctor_check(str(home), str(hscc))
    assert res["ok"] is False and res["fatal"] is False
    assert "could not be resolved" in res["detail"]
    assert "hscc-roles" in res["fix"]


def test_refusal_names_the_seam_and_reads_nothing():
    """The pytest fence: with no injected seam the check refuses."""
    res = dmo.doctor_check()
    assert res["ok"] is False and res["fatal"] is False
    assert "_main_only_surface_check" in res["detail"]
    assert res["name"] == dmo.CHECK_NAME


def test_no_read_escapes_the_asked_about_root(tmp_path, monkeypatch):
    """Canary: a red artefact in a second root must not move the asked-about
    root, and the sweep must not touch the operator's tree."""
    asked, hscc = clean_posture(tmp_path)
    other = tmp_path / "other" / ".hermes"
    mk_profile(other, MAIN, enabled=list(ALL_THREE), skills=CLUSTER_SKILLS)
    mk_profile(other, WORKER, enabled=list(ALL_THREE),
               skills=("hscc-cluster",))
    seen = []
    real_enabled = dmo.enabled_plugins
    real_skills = dmo._skill_dirs

    def spy_enabled(path):
        seen.append(os.path.abspath(str(path)))
        return real_enabled(path)

    def spy_skills(path):
        seen.append(os.path.abspath(str(path)))
        return real_skills(path)

    monkeypatch.setattr(dmo, "enabled_plugins", spy_enabled)
    monkeypatch.setattr(dmo, "_skill_dirs", spy_skills)
    before = _snapshot(asked)
    assert dmo.doctor_check(str(asked), str(hscc))["ok"] is True
    assert seen, "the spies prove the sweep ran"
    root = os.path.abspath(str(asked))
    for path in seen:
        assert path.startswith(root + os.sep), f"sweep escaped {root}: {path}"
    assert _snapshot(asked) == before, "the row must not write what it reads"


def _snapshot(home):
    out = {}
    for dirpath, _dirs, files in os.walk(str(home)):
        for name in files:
            p = os.path.join(dirpath, name)
            with open(p, "rb") as f:
                out[p] = (os.path.getmtime(p), len(f.read()))
    return out


# ── the declarations are read, not duplicated ──────────────────────────────

def test_tool_plugins_come_from_the_installer():
    """``enable_plugins.HSCC_PLUGINS`` is the SOT; the fallback is a pin, not an
    alternative list."""
    import enable_plugins
    declared = tuple(p for p in enable_plugins.HSCC_PLUGINS
                     if p != dmo.HOOK_ONLY_PLUGIN)
    assert dmo.cluster_tool_plugins() == declared
    assert dmo._FALLBACK_TOOL_PLUGINS == declared
    assert list(ALL_THREE[1:]) == list(declared)


def test_cluster_skills_come_from_the_skills_installer():
    parsed = dmo._parse_tuple(os.path.join(_REPO_ROOT, "hscc-skills",
                                           "hscc.py"), "CLUSTER_SKILLS")
    assert parsed == CLUSTER_SKILLS == dmo._FALLBACK_CLUSTER_SKILLS
    assert dmo.cluster_skills() == parsed


def test_cluster_skills_are_a_strict_subset_of_bundled():
    """The set is a SUBSET on purpose. If someone widens it to all of
    ``BUNDLED_SKILLS`` the row starts calling generic skills drift, so this
    guards the boundary in both directions."""
    src = open(os.path.join(_REPO_ROOT, "hscc-skills", "hscc.py"),
               encoding="utf-8").read()
    import re
    bundled = re.findall(r"[\"']([^\"']+)[\"']",
                         re.search(r"BUNDLED_SKILLS\s*=\s*\[(.*?)\n\]", src,
                                   re.M | re.S).group(1))
    assert set(CLUSTER_SKILLS) <= set(bundled)
    assert set(CLUSTER_SKILLS) != set(bundled)
    for generic in ("brainstorming", "caveman", "sdlc-review", "devops"):
        assert generic not in CLUSTER_SKILLS


def test_check_name_fallback_matches_the_feature_module():
    """doctor.py prints this when the module cannot be imported; the two must
    be one string or the row's lookups break silently."""
    assert doctor._MAIN_ONLY_SURFACE_CHECK_NAME == dmo.CHECK_NAME


# ── row side: registry wiring ──────────────────────────────────────────────

def _run(tmp_path, monkeypatch, **seams):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.delenv("PLUGINS", raising=False)
    monkeypatch.setenv("HSCC_DIR", str(tmp_path / ".hscc"))
    return doctor.run_doctor(str(tmp_path / ".hermes"), hscc_dir=str(
        tmp_path / ".hscc"), _cluster_runner=lambda *a, **k: "",
        fleet_checks=False, **seams)


def _row(res):
    return {c["name"]: c for c in res["checks"]}[dmo.CHECK_NAME]


def test_row_renders_through_the_seam(tmp_path, monkeypatch):
    seam = lambda: {"name": dmo.CHECK_NAME, "ok": False, "fatal": False,
                    "detail": "swapped main-only row", "fix": "do the thing"}
    row = _row(_run(tmp_path, monkeypatch, _main_only_surface_check=seam))
    assert row["ok"] is False and row["detail"] == "swapped main-only row"
    assert row["fix"] == "do the thing"


def test_row_refuses_inside_pytest_without_a_seam(tmp_path, monkeypatch):
    row = _row(_run(tmp_path, monkeypatch))
    assert row["ok"] is False and row["fatal"] is False
    assert "_main_only_surface_check" in row["detail"]


def test_row_is_unverifiable_when_the_module_is_missing(monkeypatch):
    """The UNVERIFIABLE path lives in the loader branch, which the registry only
    reaches OUTSIDE a pytest process (the refusal wins inside one). Driven
    through ``build_row`` with a stand-in module whose ``sys.modules`` has no
    ``pytest``, exactly as ``test_doctor_row_registry.py`` does it — un-fencing a
    whole ``run_doctor()`` call would put every other row's live transport in
    scope for the sake of one row's branch."""
    row = _build_row_outside_pytest(monkeypatch, loader=lambda: None)
    assert row.ok is False and row.fatal is False
    assert "doctor_main_only.py not importable" in row.detail
    assert "re-run bootstrap" in row.fix
    assert row.name == doctor._MAIN_ONLY_SURFACE_CHECK_NAME


def test_row_receives_both_roots_it_declares(monkeypatch):
    """The registry must hand this row BOTH contexts: ``hermes_home`` for the
    profile trees and ``hscc_dir`` for the identity document. Drop either from
    ``row_kwargs`` and this goes red (``render_rows`` filters by declaration)."""
    seen = {}

    class _Fake:
        @staticmethod
        def doctor_check(hermes_home=None, hscc_dir=None):
            seen["hermes_home"] = hermes_home
            seen["hscc_dir"] = hscc_dir
            return {"name": dmo.CHECK_NAME, "ok": True, "fatal": False,
                    "detail": "fake sweep", "fix": ""}

    row = _build_row_outside_pytest(monkeypatch, loader=lambda: _Fake,
                                    hermes_home="/asked/.hermes",
                                    hscc_dir="/asked/.hscc")
    assert row.detail == "fake sweep" and row.ok is True
    assert seen == {"hermes_home": "/asked/.hermes",
                    "hscc_dir": "/asked/.hscc"}


class _SysShim:
    """``doctor_rows.build_row`` reads ``doctor_module.sys.modules`` for the
    pytest fence. This is the shim that says "not a pytest process" so a row's
    loader branch can be exercised from inside the suite."""
    modules = {}


class _DoctorLike:
    """Only what :func:`doctor_rows.build_row` resolves on the doctor module."""
    Check = doctor.Check
    sys = _SysShim()


def _build_row_outside_pytest(monkeypatch, *, loader, **context):
    spec = {s.key: s for s in doctor.FEATURE_CHECK_ROWS}["main_only_surface"]
    monkeypatch.setattr(_DoctorLike, "_load_doctor_main_only", loader,
                        raising=False)
    monkeypatch.setattr(_DoctorLike, "_refused_main_only_check",
                        doctor._refused_main_only_check, raising=False)
    monkeypatch.setattr(_DoctorLike, "_MAIN_ONLY_SURFACE_CHECK_NAME",
                        doctor._MAIN_ONLY_SURFACE_CHECK_NAME, raising=False)
    return doctor_rows.build_row(_DoctorLike, spec, None, **context)


def test_row_declares_its_context_and_no_row_fn():
    by_key = {s.key: s for s in doctor.FEATURE_CHECK_ROWS}
    spec = by_key["main_only_surface"]
    assert spec.row_fn == "", "a new row uses the generic builder"
    assert spec.row_kwargs == ("hermes_home", "hscc_dir")
    assert spec.seam_kwarg == "_main_only_surface_check"
    assert spec.refusal == "_refused_main_only_check"
    assert spec.loader == "_load_doctor_main_only"


def test_refusal_delegates_to_the_feature_module(tmp_path, monkeypatch):
    """One refusal wording, not two."""
    monkeypatch.setattr(doctor, "_load_doctor_main_only", lambda: dmo)
    assert doctor._refused_main_only_check() == dmo.refused()
    monkeypatch.setattr(doctor, "_load_doctor_main_only", lambda: None)
    literal = doctor._refused_main_only_check()
    assert literal["name"] == doctor._MAIN_ONLY_SURFACE_CHECK_NAME
    assert "_main_only_surface_check" in literal["detail"]


def test_profile_scoped_home_still_sees_its_siblings(tmp_path):
    """The fleet sweep walks a profile-scoped ``HERMES_HOME`` up to the root,
    like ``doctor._skills_home`` — otherwise a worker run reports the fleet
    clean while its neighbours are dirty."""
    home, hscc = posture(tmp_path, main_enabled=list(ALL_THREE),
                         main_skills=CLUSTER_SKILLS,
                         worker_enabled=list(ALL_THREE))
    profile_home = os.path.join(str(home), "profiles", MAIN)
    res = dmo.doctor_check(profile_home, str(hscc))
    assert res["ok"] is False
    assert WORKER in res["detail"], "the sibling profile was still swept"


def test_sweep_survives_an_unparsable_config(tmp_path):
    """A broken config is not this row's claim, and must not raise it either."""
    home, hscc = posture(tmp_path, main_enabled=list(ALL_THREE),
                         main_skills=CLUSTER_SKILLS,
                         worker_enabled=["hscc-cluster"])
    with open(os.path.join(str(home), "profiles", WORKER, "config.yaml"),
              "w", encoding="utf-8") as f:
        f.write("plugins:\n  enabled: [hscc-cluster\n   : : :\n")
    assert dmo.doctor_check(str(home), str(hscc))["ok"] is True


def test_non_list_enabled_block_is_not_a_crash(tmp_path):
    home, hscc = posture(tmp_path, main_enabled=list(ALL_THREE),
                         main_skills=CLUSTER_SKILLS,
                         worker_enabled=["hscc-cluster"])
    with open(os.path.join(str(home), "profiles", WORKER, "config.yaml"),
              "w", encoding="utf-8") as f:
        f.write("plugins:\n  enabled: hscc-commands\n")
    assert dmo.enabled_plugins(os.path.join(str(home), "profiles", WORKER,
                                            "config.yaml")) == []
