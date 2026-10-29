"""Doctor skill findings (card t_bc611809): duplicate names + reviewer readiness.

Fabricated tmp homes only — same discipline as test_doctor.py's hermetic
fixtures. ``doctor._skill_findings`` is the pure scan both checks share;
``--skills`` is the deterministic fast mode the orchestrator re-runs for
acceptance (no sparkrun, no fleet probes).
"""

import json
import os
import sys
from io import StringIO

import pytest

import doctor


def mk_skill(root, rel, content):
    d = os.path.join(str(root), rel)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(content)
    return d


def mk_profile(home, name, skills=None):
    pdir = os.path.join(str(home), "profiles", name)
    skills_dir = os.path.join(pdir, "skills")
    os.makedirs(skills_dir, exist_ok=True)
    for rel, content in (skills or {}).items():
        mk_skill(skills_dir, rel, content)
    return pdir


def _capture_main(argv):
    old = sys.stdout
    try:
        sys.stdout = StringIO()
        rc = doctor.main(argv)
        return rc, sys.stdout.getvalue()
    finally:
        sys.stdout = old


@pytest.fixture
def clean_home(tmp_path):
    """Root home + reviewer profiles where sdlc-review resolves exactly once."""
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    mk_skill(home / "skills", "devops/sdlc-review", "root copy\n")
    mk_profile(home, "acme-orch", {"devops/sdlc-review": "orch copy\n"})
    mk_profile(home, "reviewer", {"devops/sdlc-review": "rev copy\n"})
    mk_profile(home, "backend-engineer", {"devops/sdlc-review": "be copy\n"})
    return home


class TestSkillFindings:
    def test_clean_tree_is_green(self, clean_home):
        dups, rev = doctor._skill_findings(str(clean_home))
        assert dups == [] and rev == []

    def test_missing_in_orch_is_red_with_paths(self, clean_home):
        mk_profile(clean_home, "broken-orch")  # no sdlc-review at all
        dups, rev = doctor._skill_findings(str(clean_home))
        assert dups == []
        assert len(rev) == 1 and "broken-orch" in rev[0]
        assert "missing sdlc-review" in rev[0]
        # The check renders it.
        chk = doctor._reviewer_readiness_ok(str(clean_home))
        assert chk.ok is False and chk.fatal is False
        assert "broken-orch" in chk.detail

    def test_non_reviewer_missing_is_not_nagged(self, clean_home):
        mk_profile(clean_home, "quiet-worker")  # missing, but not reviewer-capable
        dups, rev = doctor._skill_findings(str(clean_home))
        assert rev == [] and dups == []

    def test_collision_is_red_with_all_paths(self, clean_home):
        mk_profile(clean_home, "dupe-orch", {
            "sdlc-review": "stale june\n", "devops/sdlc-review": "september\n"})
        dups, rev = doctor._skill_findings(str(clean_home))
        flat = os.path.join(str(clean_home), "profiles", "dupe-orch", "skills")
        assert any("dupe-orch: sdlc-review" in d and
                   os.path.join(flat, "sdlc-review") in d and
                   os.path.join(flat, "devops", "sdlc-review") in d for d in dups)
        assert any("dupe-orch" in r and "collides" in r and
                   os.path.join(flat, "sdlc-review") in r for r in rev)
        chk = doctor._skills_duplicates_ok(str(clean_home))
        assert chk.ok is False and "sdlc-review" in chk.detail

    def test_identical_duplicates_stay_green(self, clean_home):
        # Byte-identical copies in one root whose RANKS DIFFER (flat depth-1
        # vs category depth-2) RESOLVE for hermes (same-root ranking) — doctor
        # must not cry wolf, and readiness counts them ok.
        mk_profile(clean_home, "dup-orch", {
            "sdlc-review": "SAME\n", "devops/sdlc-review": "SAME\n"})
        _, rev = doctor._skill_findings(str(clean_home))
        assert rev == []
        # BUT the duplicate-name finding still lists them (two packages share
        # the name — a landmine the moment either copy is edited).
        dups, _ = doctor._skill_findings(str(clean_home))
        assert any("dup-orch" in d for d in dups)

    def test_equal_rank_identical_copies_read_red(self, clean_home):
        # B2 (round-2 review): the SAME content in two category dirs (equal
        # rank) is NOT collapsed by the resolver's `rank[0] != rank[1]` gate —
        # a spawn to that profile dies with 'Unknown skill(s)'. Readiness must
        # be RED (was GREEN in the first cut) and say collision, listing both
        # paths, with the equal-rank explanation.
        mk_profile(clean_home, "tie-orch", {
            "catA/sdlc-review": "SAME\n", "catB/sdlc-review": "SAME\n"})
        dups, rev = doctor._skill_findings(str(clean_home))
        assert any("tie-orch" in d for d in dups)
        assert len(rev) == 1 and "tie-orch" in rev[0] and "collides" in rev[0]
        assert "equal rank" in rev[0]
        chk = doctor._reviewer_readiness_ok(str(clean_home))
        assert chk.ok is False and "tie-orch" in chk.detail

    def test_disabled_skill_reads_red(self, clean_home):
        # S2: present but listed in skills.disabled -> a preload spawn counts
        # it missing; readiness must be RED, not ok.
        pdir = mk_profile(clean_home, "off-orch", {"devops/sdlc-review": "x\n"})
        with open(os.path.join(pdir, "config.yaml"), "w", encoding="utf-8") as f:
            f.write("skills:\n  disabled: [sdlc-review]\n")
        _, rev = doctor._skill_findings(str(clean_home))
        assert len(rev) == 1 and "off-orch" in rev[0]
        assert "skills.disabled" in rev[0]

    def test_opted_out_home_annotated_never_silent(self, clean_home):
        # B3: a reviewer-capable home carrying the canonical
        # <home>/.no-bundled-skills marker and missing the name is reported
        # like any other broken home, annotated with the marker.
        pdir = mk_profile(clean_home, "opt-orch")  # zero sdlc-review
        with open(os.path.join(pdir, ".no-bundled-skills"), "w", encoding="utf-8") as f:
            f.write("opted out\n")
        _, rev = doctor._skill_findings(str(clean_home))
        assert len(rev) == 1 and "opt-orch" in rev[0]
        assert "missing sdlc-review" in rev[0]
        assert ".no-bundled-skills" in rev[0]

    def test_findings_survive_broken_trees(self, tmp_path):
        # A skills tree missing everything must not crash preflight: the
        # checks RETURN (duplicates ok, readiness RED-but-reported), never
        # raise. The card requires doctor to read a zero-sdlc profile RED.
        home = tmp_path / ".hermes"
        (home / "skills").mkdir(parents=True)
        (home / "profiles").mkdir(parents=True)
        ok1 = doctor._skills_duplicates_ok(str(home))
        ok2 = doctor._reviewer_readiness_ok(str(home))
        assert ok1.ok is True
        assert ok2.ok is False and "default (root)" in ok2.detail
        assert ok2.fatal is False

    def test_root_home_counts_as_reviewer(self, tmp_path):
        home = tmp_path / ".hermes"
        (home / "skills").mkdir(parents=True)  # root home missing the skill
        _, rev = doctor._skill_findings(str(home))
        assert any("default (root)" in r and "missing sdlc-review" in r for r in rev)

    def test_profile_scoped_home_walks_up_to_root(self, clean_home):
        # doctor running as a worker (HERMES_HOME=<root>/profiles/<name>)
        # must still audit the WHOLE fleet, not just its own profile.
        _, rev = doctor._skill_findings(str(clean_home / "profiles" / "acme-orch"))
        assert rev == []  # fleet is clean from here too


class TestDoctorIntegration:
    def test_run_doctor_includes_both_skill_checks(self, tmp_path):
        (tmp_path / "skills").mkdir()
        (tmp_path / "profiles").mkdir()
        res = doctor.run_doctor(hermes_home=str(tmp_path),
                                _cluster_runner=lambda: "[]")
        names = {c["name"] for c in res["checks"]}
        assert {"skill names (duplicates)",
                "reviewer readiness (sdlc-review)"} <= names
        # Never fatal — a colliding operator tree must not hard-stop bootstrap.
        for c in res["checks"]:
            if c["name"].startswith(("skill names", "reviewer readiness")):
                assert c["fatal"] is False

    def test_skill_failure_does_not_flip_overall_ok(self, tmp_path):
        home = tmp_path / ".hermes"
        (home / "skills").mkdir(parents=True)
        (home / "profiles").mkdir()
        mk_profile(home, "broken-orch")
        res = doctor.run_doctor(hermes_home=str(home),
                                _cluster_runner=lambda: "[]")
        bad = [c for c in res["checks"] if c["name"] == "reviewer readiness (sdlc-review)"]
        assert bad and bad[0]["ok"] is False
        assert "broken-orch" in bad[0]["detail"]
        assert "reviewer readiness (sdlc-review)" not in res["fatal_failures"]

    def test_cli_skills_mode_json_and_rc(self, clean_home, tmp_path):
        rc, out = _capture_main(["--skills", "--home", str(clean_home)])
        assert rc == 0, out
        payload = json.loads(out)
        assert payload["skill_duplicates"]["ok"] is True
        assert payload["reviewer_readiness"]["ok"] is True

    def test_cli_skills_mode_red_on_collision(self, clean_home):
        mk_profile(clean_home, "dupe-orch", {
            "sdlc-review": "stale\n", "devops/sdlc-review": "sept\n"})
        rc, out = _capture_main(["--skills", "--home", str(clean_home)])
        assert rc == 1
        payload = json.loads(out)
        assert payload["skill_duplicates"]["ok"] is False
        assert payload["reviewer_readiness"]["ok"] is False

    def test_cli_skills_mode_never_touches_real_home(self, tmp_path, monkeypatch):
        # --home wins: a tmp tree must be scanned even with HERMES_HOME ambient
        # (the operator repro runs against whatever --home says, never ~/.hermes).
        monkeypatch.setenv("HERMES_HOME", "/nonexistent-operator-home")
        home = tmp_path / ".hermes"
        mk_skill(home / "skills", "devops/sdlc-review", "root copy\n")
        (home / "profiles").mkdir()
        rc, out = _capture_main(["--skills", "--home", str(home)])
        payload = json.loads(out)
        assert payload["home"] == str(home)
        assert rc == 0

    def test_install_then_doctor_goes_green(self, clean_home, tmp_path):
        """Acceptance loop: fabricated RED profile -> guarded first install ->
        doctor reads GREEN (and the real resolver mirror agrees)."""
        import install_review_skill as irs
        repo_root = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        mk_profile(clean_home, "fresh-orch")  # reviewer-capable, zero sdlc-review
        rc, out = _capture_main(["--skills", "--home", str(clean_home)])
        assert rc == 1 and "fresh-orch" in out
        irs.install_review_skill(home=str(clean_home), repo_root=repo_root,
                                 stamp="test")
        rc2, out2 = _capture_main(["--skills", "--home", str(clean_home)])
        assert rc2 == 0, out2
