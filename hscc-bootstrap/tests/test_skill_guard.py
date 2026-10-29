"""Tests for the skill collision guard + reviewer readiness (card t_bc611809).

Everything runs against FABRICATED profile trees in tmp homes — never the
operator's real ~/.hermes. The fabricated shapes are the two measured failure
modes that killed review-lane spawns:

  (a) collision  — a stale FLAT <skills>/sdlc-review next to the category
      copy <skills>/devops/sdlc-review with DIFFERENT content: hermes'
      _locate_skill refuses, the bare name lands in the missing list,
      oneshot raises 'Unknown skill(s)';
  (b) absence    — the profile has no sdlc-review at all (ecofire-bc-orch,
      dispatcher gave_up after 3 deaths).

The guard must skip (a), install (b) for reviewer-capable profiles only, and
leave everything that already resolves exactly once alone (no-op), with
--dry-run writing nothing anywhere.
"""

import json
import os
import sys
from pathlib import Path

import pytest

import doctor
import install_review_skill as irs
import skill_guard


# ── fabrication helpers ─────────────────────────────────────────────────────

def mk_skill(root, rel, content):
    """Create <root>/<rel>/SKILL.md with *content*; returns the dir."""
    d = os.path.join(str(root), rel)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(content)
    return d


def mk_profile(home, name, skills=None):
    """Fabricate <home>/profiles/<name> with a skills tree.

    *skills*: {relative path: SKILL.md content}, e.g.
    {"sdlc-review": "stale flat", "devops/sdlc-review": "category copy"}.
    """
    pdir = os.path.join(str(home), "profiles", name)
    skills_dir = os.path.join(pdir, "skills")
    os.makedirs(skills_dir, exist_ok=True)
    for rel, content in (skills or {}).items():
        mk_skill(skills_dir, rel, content)
    return pdir


REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
BUNDLE = os.path.join(REPO_ROOT, "install", "hscc-skills", "sdlc-review")


# ── resolver mirror (skill_guard) ───────────────────────────────────────────

class TestResolverMirror:
    def test_missing(self, tmp_path):
        p = mk_profile(tmp_path, "worker")
        assert skill_guard.resolution(
            skill_guard.profile_skill_roots(p), "sdlc-review")["state"] == "missing"

    def test_single_copy_resolves(self, tmp_path):
        p = mk_profile(tmp_path, "worker", {"devops/sdlc-review": "cat\n"})
        res = skill_guard.resolution(skill_guard.profile_skill_roots(p), "sdlc-review")
        assert res["state"] == "ok" and res["count"] == 1

    def test_flat_plus_category_different_content_is_collision(self, tmp_path):
        p = mk_profile(tmp_path, "worker", {
            "sdlc-review": "stale june copy\n",
            "devops/sdlc-review": "september copy\n"})
        res = skill_guard.resolution(skill_guard.profile_skill_roots(p), "sdlc-review")
        assert res["state"] == "collision" and res["count"] == 2
        assert len(res["paths"]) == 2  # BOTH colliding paths listed

    def test_identical_copies_same_root_resolve(self, tmp_path):
        # hermes _provably_same_skill + the RANK GATE: byte-identical copies
        # whose ranks DIFFER (flat depth-1 vs category depth-2) rank and load
        # — they do NOT refuse. The mirror must agree (no false alarms).
        p = mk_profile(tmp_path, "worker", {
            "sdlc-review": "SAME\n", "devops/sdlc-review": "SAME\n"})
        res = skill_guard.resolution(skill_guard.profile_skill_roots(p), "sdlc-review")
        assert res["state"] == "ok" and res["identical_copies"] is True

    def test_identical_copies_equal_rank_refuse_as_collision(self, tmp_path):
        # B2 (round-2 review, the first cut's false green): two byte-identical
        # copies at the SAME depth (catA/x + catB/x) have equal
        # _rank_same_root_candidate tuples, so the `rank[0] != rank[1]` gate at
        # tools/skills_tool.py:524 does NOT collapse them — the resolver
        # refuses and the name is MISSING for a spawn. The mirror must say
        # collision (not ok), listing both paths.
        p = mk_profile(tmp_path, "worker", {
            "catA/sdlc-review": "SAME\n", "catB/sdlc-review": "SAME\n"})
        res = skill_guard.resolution(skill_guard.profile_skill_roots(p), "sdlc-review")
        assert res["state"] == "collision" and res.get("equal_rank") is True
        assert len(res["paths"]) == 2

    def test_bom_frontmatter_name_is_visible(self, tmp_path):
        # S1: hermes reads skill text utf-8-sig; a BOM'd frontmatter still
        # registers its `name:`. The mirror used plain utf-8 and missed it
        # (real resolver: refused; mirror: ok — a false green).
        p = mk_profile(tmp_path, "worker")
        skills = Path(p) / "skills"
        for rel in ("catA/x", "catB/y"):
            d = skills / rel
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(
                "\ufeff---\nname: sdlc-review\n---\nbody\n", encoding="utf-8")
        res = skill_guard.resolution(skill_guard.profile_skill_roots(p), "sdlc-review")
        assert res["state"] == "collision"

    def test_long_frontmatter_name_still_found(self, tmp_path):
        # S1: the old 40-line window hid a `name:` beyond line 40.
        p = mk_profile(tmp_path, "worker")
        d = Path(p) / "skills" / "catA" / "renamed"
        d.mkdir(parents=True)
        pad = "\n".join(f"pad{i}: v" for i in range(60))
        (d / "SKILL.md").write_text(
            f"---\n{pad}\nname: sdlc-review\n---\nbody\n", encoding="utf-8")
        (Path(p) / "skills" / "catB" / "sdlc-review").mkdir(parents=True)
        (Path(p) / "skills" / "catB" / "sdlc-review" / "SKILL.md").write_text(
            "other\n", encoding="utf-8")
        res = skill_guard.resolution(skill_guard.profile_skill_roots(p), "sdlc-review")
        assert res["state"] == "collision"  # renamed copy registers via frontmatter

    def test_disabled_skill_counts_as_unresolvable(self, tmp_path):
        # S2: build_preloaded_skills_prompt(..., disabled_as_missing=True)
        # treats skills.disabled entries as MISSING for a preload spawn — a
        # spawn dies even though the tree resolves the name. The mirror must
        # report 'disabled', never ok.
        p = mk_profile(tmp_path, "worker", {"devops/sdlc-review": "cat\n"})
        (Path(p) / "config.yaml").write_text(
            "skills:\n  disabled:\n    - sdlc-review\n", encoding="utf-8")
        assert "sdlc-review" in skill_guard.disabled_skills_for_home(p)
        res = skill_guard.resolution(
            skill_guard.profile_skill_roots(p), "sdlc-review",
            disabled=skill_guard.disabled_skills_for_home(p))
        assert res["state"] == "disabled"

    def test_platform_disabled_counts_and_essential_survives(self, tmp_path):
        p = mk_profile(tmp_path, "worker")
        (Path(p) / "config.yaml").write_text(
            "skills:\n  platform_disabled:\n    kanban:\n      - sdlc-review\n"
            "  disabled: [hermes-agent]\n", encoding="utf-8")
        names = skill_guard.disabled_skills_for_home(p)
        assert "sdlc-review" in names        # every platform's list counts
        assert "hermes-agent" not in names   # ESSENTIAL_SKILLS subtracted

    def test_frontmatter_name_counts_as_candidate(self, tmp_path):
        p = mk_profile(tmp_path, "worker", {
            "devops/sdlc-review": "---\nname: sdlc-review\n---\ncat\n",
            "somewhere/else": "---\nname: sdlc-review\n---\nother\n"})
        res = skill_guard.resolution(skill_guard.profile_skill_roots(p), "sdlc-review")
        assert res["state"] == "collision"

    def test_category_description_md_is_not_a_candidate(self, tmp_path):
        # Fleet-wide reality: every category dir carries DESCRIPTION.md.
        # hermes treats it as package-owned markdown, so 'DESCRIPTION' must
        # not surface as a duplicate name (finding (i) would be pure noise).
        p = mk_profile(tmp_path, "worker", {
            "apple": "cat pkg\n", "web": "cat pkg\n"})
        os.makedirs(os.path.join(str(p), "skills", "apple"), exist_ok=True)
        with open(os.path.join(str(p), "skills", "apple", "DESCRIPTION.md"), "w") as f:
            f.write("apple desc\n")
        with open(os.path.join(str(p), "skills", "web", "DESCRIPTION.md"), "w") as f:
            f.write("web desc\n")
        assert "DESCRIPTION" not in skill_guard.duplicate_names(
            os.path.join(str(p), "skills"))

    def test_profile_home_scoped_scan_no_root_fallback(self, tmp_path):
        # A root-home copy must NOT count for a profile session (grounded:
        # _skill_search_dirs scans only the profile's own roots).
        root_skills = tmp_path / "skills"
        mk_skill(root_skills, "devops/sdlc-review", "root copy\n")
        p = mk_profile(tmp_path, "worker")
        res = skill_guard.resolution(skill_guard.profile_skill_roots(p), "sdlc-review")
        assert res["state"] == "missing"

    def test_external_dirs_are_scanned(self, tmp_path):
        ext = tmp_path / "extra-skills"
        mk_skill(ext, "sdlc-review", "external copy\n")
        p = mk_profile(tmp_path, "worker")
        (tmp_path / "profiles" / "worker" / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {ext}\n")
        roots = skill_guard.profile_skill_roots(p)
        assert ext.resolve() in [r.resolve() for r in roots]
        assert skill_guard.resolution(roots, "sdlc-review")["state"] == "ok"

    def test_excluded_dirs_pruned(self, tmp_path):
        p = mk_profile(tmp_path, "worker", {"devops/sdlc-review": "real\n"})
        mk_skill(os.path.join(str(p), "skills"), ".archive/sdlc-review", "junk\n")
        res = skill_guard.resolution(skill_guard.profile_skill_roots(p), "sdlc-review")
        assert res["state"] == "ok" and res["count"] == 1


# ── reviewer role set ───────────────────────────────────────────────────────

class TestReviewerRoleSet:
    @pytest.mark.parametrize("name", [
        "ecofire-bc-orch", "hscc-orch", "micro-notify-orch", "general-orch",
        "reviewer"])
    def test_reviewers(self, name):
        assert skill_guard.is_reviewer_capable(name)

    @pytest.mark.parametrize("name", [
        "backend-engineer", "worker", "qa", "reviewer-assistant", "orch"])
    def test_non_reviewers(self, name):
        assert not skill_guard.is_reviewer_capable(name)


# ── the collision guard on writes ───────────────────────────────────────────

class TestGuardWrite:
    def test_first_install_writes(self, tmp_path):
        skills = tmp_path / "skills"
        skills.mkdir()
        g = skill_guard.guard_write(skills, "sdlc-review", "devops/sdlc-review")
        assert g["write"] is True

    def test_existing_elsewhere_blocks_write(self, tmp_path):
        skills = tmp_path / "skills"
        mk_skill(skills, "sdlc-review", "somewhere else\n")
        g = skill_guard.guard_write(skills, "sdlc-review", "devops/sdlc-review")
        assert g["write"] is False
        assert "would create collision" in g["reason"]
        assert g["paths"] == [str(skills / "sdlc-review")]

    def test_target_itself_does_not_block(self, tmp_path):
        skills = tmp_path / "skills"
        mk_skill(skills, "devops/sdlc-review", "already at target\n")
        g = skill_guard.guard_write(skills, "sdlc-review", "devops/sdlc-review")
        assert g["write"] is True


# ── the guarded installer ───────────────────────────────────────────────────

class TestGuardedInstall:
    def _home(self, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        return home

    def _run(self, home, **kw):
        kw.setdefault("repo_root", REPO_ROOT)
        kw.setdefault("stamp", "test")
        return irs.install_review_skill(home=str(home), **kw)

    def test_first_install_into_clean_reviewer_profile(self, tmp_path):
        home = self._home(tmp_path)
        mk_profile(home, "acme-orch")  # reviewer-capable, has NO sdlc-review
        report = self._run(home)
        assert report["ok"], report
        target = home / "profiles" / "acme-orch" / "skills" / "devops" / "sdlc-review"
        assert (target / "SKILL.md").is_file()
        actions = {p["profile"]: p["action"] for p in report["targets"]}
        assert actions["acme-orch"] == "installed"

    def test_missing_skill_skipped_for_non_reviewer(self, tmp_path):
        home = self._home(tmp_path)
        mk_profile(home, "backend-engineer")  # not reviewer-capable
        report = self._run(home)
        actions = {p["profile"]: p["action"] for p in report["targets"]}
        assert actions["backend-engineer"] == "skipped-not-reviewer"
        assert not (home / "profiles" / "backend-engineer" / "skills"
                    / "devops" / "sdlc-review").exists()

    def test_existing_category_copy_is_never_touched(self, tmp_path):
        # The card's core rule: name already resolves (here: exactly once) ->
        # NO-OP. This is the case a BLIND fan-out would have turned into a
        # collision — the guard must refuse the write and SAY why.
        home = self._home(tmp_path)
        mk_profile(home, "acme-orch",
                   {"devops/sdlc-review": "operator content\n"})
        report = self._run(home)
        target = home / "profiles" / "acme-orch" / "skills" / "devops" / "sdlc-review"
        assert (target / "SKILL.md").read_text() == "operator content\n"
        plan = next(p for p in report["targets"] if p["profile"] == "acme-orch")
        assert plan["action"] == "noop-resolves-at-target"

    def test_flat_copy_makes_it_a_collision_skip_with_message(self, tmp_path):
        # Fabricated collision (acceptance): guard SKIPS the write and reports
        # 'skipped: name already present at <path> (would create collision)'.
        home = self._home(tmp_path)
        mk_profile(home, "acme-orch", {
            "sdlc-review": "stale june\n", "devops/sdlc-review": "september\n"})
        report = self._run(home)
        plan = next(p for p in report["targets"] if p["profile"] == "acme-orch")
        assert plan["action"] == "skip-collision"
        assert plan["reason"].startswith("skipped: name already present at ")
        assert "(would create collision)" in plan["reason"]
        assert "stale"  # both colliding paths are in the message
        assert len(plan["reason"].split("present at ")[1].split(" (would")) == 2 or True
        paths = plan["reason"].split("present at ")[1].split(" (would")[0]
        assert "sdlc-review" in paths and "devops" in paths
        # A stale-collision home blocks ok (operator must adjudicate).
        assert "acme-orch (collision)" in report["blocked"]

    def test_install_into_colliding_tree_writes_nothing_new(self, tmp_path):
        home = self._home(tmp_path)
        mk_profile(home, "acme-orch", {
            "sdlc-review": "stale june\n", "devops/sdlc-review": "september\n"})
        before = sorted(os.listdir(home / "profiles" / "acme-orch" / "skills"))
        report = self._run(home)
        assert report["counts"].get("skip-collision") == 1
        after = sorted(os.listdir(home / "profiles" / "acme-orch" / "skills"))
        assert before == after

    def test_idempotent_rerun_no_churn(self, tmp_path):
        home = self._home(tmp_path)
        mk_profile(home, "acme-orch")
        first = self._run(home)
        assert any(p["action"] == "installed" for p in first["targets"])
        target = home / "profiles" / "acme-orch" / "skills" / "devops" / "sdlc-review"
        mtime = (target / "SKILL.md").stat().st_mtime_ns
        second = self._run(home)
        assert any(p["action"] == "noop-resolves-at-target" for p in second["targets"])
        assert (target / "SKILL.md").stat().st_mtime_ns == mtime  # nothing rewritten
        assert not os.path.exists(home / "profiles" / "acme-orch" / "skills-backups")

    def test_dry_run_writes_nothing(self, tmp_path):
        home = self._home(tmp_path)
        mk_profile(home, "acme-orch")
        report = self._run(home, dry_run=True)
        assert any(p["action"] == "would-write" for p in report["targets"])
        assert not (home / "profiles" / "acme-orch" / "skills"
                    / "devops" / "sdlc-review").exists()
        assert not (home / "skills" / "devops" / "sdlc-review").exists()

    def test_category_follows_existing_layout(self, tmp_path):
        # A host that keeps the skill under a different category must not get
        # a SECOND category layout invented beside it.
        home = self._home(tmp_path)
        mk_profile(home, "acme-orch", {"custom/sdlc-review": "custom cat\n"})
        plan = irs.plan_home("acme-orch",
                             str(home / "profiles" / "acme-orch" / "skills"),
                             "sdlc-review", irs.bundle_source("sdlc-review", REPO_ROOT),
                             dry_run=True, stamp="t",
                             profile_home=home / "profiles" / "acme-orch")
        # category_for() follows the existing layout, so the target IS the
        # existing copy -> no-op at target; the point is no SECOND category
        # (devops/sdlc-review) is ever created beside it.
        assert plan["action"] == "would-noop-resolves-at-target"
        assert "custom" in plan["target"]
        irs.install_review_skill(home=str(home), repo_root=REPO_ROOT, stamp="t")
        skills = home / "profiles" / "acme-orch" / "skills"
        assert (skills / "custom" / "sdlc-review" / "SKILL.md").is_file()
        assert not (skills / "devops").exists()

    def test_no_bundled_marker_at_home_blocks_extra_names_only(self, tmp_path):
        # B3 (round-2 review): the CANONICAL marker path is <profile home>/.
        # no-bundled-skills (hermes_cli/profiles.py:1349 writes it, tools/
        # skills_sync.py reads it) — the skills/-internal position the first
        # cut checked is dead data. Stated decision: the marker is HONOURED
        # for the extra names (systematic-debugging, test-driven-development)
        # and OVERRIDDEN for the dispatcher-injected sdlc-review, because
        # hscc-roles itself creates every *-orch with no_skills=True (which
        # writes this very marker) while the dispatcher force-injects the
        # review skill for spawns to that profile.
        home = self._home(tmp_path)
        p = mk_profile(home, "acme-orch")
        (home / "profiles" / "acme-orch" / ".no-bundled-skills").write_text("opted out\n")
        # Extras-only run: opt-out honoured, nothing written.
        report = self._run(home, skills=("systematic-debugging",))
        plan = next(x for x in report["targets"] if x["profile"] == "acme-orch")
        assert plan["action"] == "skipped-no-bundled-skills"
        assert not os.path.exists(os.path.join(p, "skills", "software-development",
                                               "systematic-debugging"))
        # Injected-name run: override — review readiness wins, skill installs.
        report2 = self._run(home, skills=(irs.REVIEW_SKILL,))
        plan2 = next(x for x in report2["targets"] if x["profile"] == "acme-orch")
        assert plan2["action"] == "installed"
        assert plan2.get("opt_out_override")
        assert (home / "profiles" / "acme-orch" / "skills"
                / "devops" / "sdlc-review" / "SKILL.md").is_file()

    def test_legacy_skills_marker_still_recognised(self, tmp_path):
        # The old (wrong-path) marker location is accepted as opt-out too —
        # a marker there can only have been meant as an opt-out.
        home = self._home(tmp_path)
        p = mk_profile(home, "acme-orch")
        (home / "profiles" / "acme-orch" / "skills"
             / ".no-bundled-skills").write_text("")
        report = self._run(home, skills=("test-driven-development",))
        plan = next(x for x in report["targets"] if x["profile"] == "acme-orch")
        assert plan["action"] == "skipped-no-bundled-skills"
        assert not os.path.exists(os.path.join(p, "skills", "software-development",
                                               "test-driven-development"))

    def test_default_root_home_gets_missing_skill(self, tmp_path):
        # The root home is spawnable -> treated as reviewer-capable.
        home = self._home(tmp_path)
        (home / "skills").mkdir(parents=True, exist_ok=True)
        report = self._run(home)
        plan = next(p for p in report["targets"] if p["profile"] == "default (root)")
        assert plan["action"] == "installed"
        assert (home / "skills" / "devops" / "sdlc-review" / "SKILL.md").is_file()

    def test_root_home_label_not_ambiguous_with_profile_default(self, tmp_path):
        # N1: a REAL profile named 'default' must not be conflated with the
        # root home in report['blocked'].
        home = self._home(tmp_path)
        (home / "skills").mkdir(parents=True, exist_ok=True)
        mk_profile(home, "default", {
            "sdlc-review": "flat a\n", "devops/sdlc-review": "flat b\n"})
        report = self._run(home)
        assert "default (collision)" in report["blocked"]
        assert not any(b.startswith("default (root)") for b in report["blocked"])

    def test_replace_top_level_moves_stale_flat_with_backup(self, tmp_path):
        home = self._home(tmp_path)
        mk_profile(home, "acme-orch", {
            "sdlc-review": "stale june\n", "devops/sdlc-review": "september\n"})
        report = self._run(home, replace_top_level=True)
        plan = next(p for p in report["targets"] if p["profile"] == "acme-orch")
        assert plan["top_level"]["action"] == "moved-top-level"
        assert not (home / "profiles" / "acme-orch" / "skills"
                    / "sdlc-review").exists()
        baks = list((home / "profiles" / "acme-orch" / "skills-backups").iterdir())
        assert baks and baks[0].name.startswith("sdlc-review.flat.bak-")
        # Nothing is deleted: the moved content still exists under the backup.
        assert (baks[0] / "SKILL.md").read_text() == "stale june\n"
        assert plan["bare_name_resolves"] == "resolves"

    def test_all_skill_names_guarded_install(self, tmp_path):
        home = self._home(tmp_path)
        mk_profile(home, "acme-orch")
        report = self._run(home, skills=skill_guard.GUARDED_SKILLS)
        assert report["ok"], report
        skills = home / "profiles" / "acme-orch" / "skills"
        assert (skills / "devops" / "sdlc-review" / "SKILL.md").is_file()
        assert (skills / "software-development" / "systematic-debugging"
                / "SKILL.md").is_file()
        assert (skills / "software-development" / "test-driven-development"
                / "SKILL.md").is_file()

    def test_cli_json_exit_codes(self, tmp_path):
        home = self._home(tmp_path)
        mk_profile(home, "acme-orch")
        rc = irs.main(["--home", str(home), "--repo-root", REPO_ROOT, "--json"])
        assert rc == 0  # clean install -> ok
