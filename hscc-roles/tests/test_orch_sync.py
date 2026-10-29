"""orch_sync: bring live <project>-orch profiles in line with the template.

Offline only — every profile tree and skills tree is built under tmp_path.
"""

import json
import os

import pytest

import generator
import orch_sync

BASE = "# Base identity\n\nShared fleet values.\n"
ROLE = ("## Role: demo-orch\n\nYou are the orchestrator for the **demo** project.\n\n"
        "Hard rules on top of the fleet rules above:\n- never push.\n")
OLD_OPS = "## Operational\n\nYou run as the **demo-orch** on the gateway node.\n"


def _profile(root, name, soul, skills=()):
    pdir = root / "profiles" / name
    pdir.mkdir(parents=True)
    (pdir / "SOUL.md").write_text(soul)
    for rel in skills:
        d = pdir / "skills" / rel
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {os.path.basename(rel)}\n---\nprofile copy\n")
    return pdir


def _source(root):
    src = root / "shared-skills"
    for rel in ("devops/kanban-orchestrator", "sdlc-review", "devops/sdlc-review",
                "writing-plans", "brainstorming", "systematic-debugging",
                "verification-before-completion", "executing-plans"):
        d = src / rel
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {os.path.basename(rel)}\n---\nshared {rel}\n")
        (d / "references").mkdir()
        (d / "references" / "notes.md").write_text("ref")
    return src


def test_compose_keeps_role_section_and_renders_work_rules():
    soul = f"OLD BASE\n\n{ROLE}\n{OLD_OPS}"
    out = orch_sync.compose(soul, "demo-orch", BASE)
    assert out.startswith(BASE.rstrip() + "\n\n## Role: demo-orch")
    assert "Hard rules on top of the fleet rules above:\n- never push." in out
    assert "OLD BASE" not in out
    assert "## How you manage work" in out
    assert "Sub-atomic cards" in out and "delegate_task" in out
    assert "**demo-orch**" in out
    assert out.endswith(generator._PROJECT_ORCH_OPS.format(name="demo-orch"))


def test_compose_refuses_a_soul_without_markers():
    assert orch_sync.compose("just text\n", "demo-orch", BASE) is None
    assert orch_sync.compose("x\n## Role: demo-orch\nno ops\n", "demo-orch", BASE) is None


def test_project_orch_ops_formats_cleanly():
    text = generator._PROJECT_ORCH_OPS.format(name="x-orch")
    assert "{" not in text and "}" not in text
    assert "hscc.py create" in text


def test_sync_updates_soul_with_backup_and_is_idempotent(tmp_path):
    pdir = _profile(tmp_path, "demo-orch", f"OLD BASE\n\n{ROLE}\n{OLD_OPS}")
    src = _source(tmp_path)
    r = orch_sync.sync_profile(str(pdir), BASE, str(src), stamp="T1")
    assert r["soul"] == "updated"
    assert (pdir / "SOUL.md.bak-T1").read_text().startswith("OLD BASE")
    again = orch_sync.sync_profile(str(pdir), BASE, str(src), stamp="T2")
    assert again["soul"] == "unchanged" and again["skills_added"] == []
    assert not (pdir / "SOUL.md.bak-T2").exists()


def test_sync_adds_only_missing_skills_by_name_never_duplicates(tmp_path):
    # profile already has sdlc-review at top level: the shared devops/ copy
    # must NOT be added (two SKILL.md with one name = 'Unknown skill(s)').
    pdir = _profile(tmp_path, "demo-orch", f"{BASE}\n{ROLE}\n{OLD_OPS}",
                    skills=("sdlc-review", "project/demo-knowledge"))
    src = _source(tmp_path)
    r = orch_sync.sync_profile(str(pdir), BASE, str(src), stamp="T")
    assert "sdlc-review" not in r["skills_added"]
    assert set(r["skills_added"]) == set(orch_sync.ORCH_SKILL_PACK) - {"sdlc-review"}
    assert (pdir / "skills" / "devops" / "kanban-orchestrator" / "references" / "notes.md").exists()
    assert not (pdir / "skills" / "devops" / "sdlc-review").exists()
    assert (pdir / "skills" / "sdlc-review" / "SKILL.md").read_text().endswith("profile copy\n")
    assert (pdir / "skills" / "project" / "demo-knowledge" / "SKILL.md").exists()
    names = [os.path.basename(root) for root, _d, f in os.walk(pdir / "skills") if "SKILL.md" in f]
    assert len(names) == len(set(names))


def test_find_skill_prefers_category_copy(tmp_path):
    src = _source(tmp_path)
    assert orch_sync.find_skill(str(src), "sdlc-review") == os.path.join("devops", "sdlc-review")
    assert orch_sync.find_skill(str(src), "writing-plans") == "writing-plans"
    assert orch_sync.find_skill(str(src), "nope") is None


def test_missing_shared_skill_is_reported_not_fatal(tmp_path):
    pdir = _profile(tmp_path, "demo-orch", f"{BASE}\n{ROLE}\n{OLD_OPS}")
    empty = tmp_path / "empty"
    empty.mkdir()
    r = orch_sync.sync_profile(str(pdir), BASE, str(empty), stamp="T")
    assert r["skills_added"] == []
    assert set(r["skills_missing_source"]) == set(orch_sync.ORCH_SKILL_PACK)


def test_unrecognized_soul_left_alone(tmp_path):
    pdir = _profile(tmp_path, "odd-orch", "hand-written soul\n")
    r = orch_sync.sync_profile(str(pdir), BASE, str(_source(tmp_path)), stamp="T")
    assert r["soul"] == "unrecognized (left alone)"
    assert (pdir / "SOUL.md").read_text() == "hand-written soul\n"


def test_dry_run_writes_nothing(tmp_path):
    soul = f"OLD BASE\n\n{ROLE}\n{OLD_OPS}"
    pdir = _profile(tmp_path, "demo-orch", soul)
    r = orch_sync.sync_profile(str(pdir), BASE, str(_source(tmp_path)), dry_run=True)
    assert r["soul"] == "updated" and r["skills_added"]
    assert (pdir / "SOUL.md").read_text() == soul
    assert not (pdir / "skills").exists()
    assert not list(pdir.glob("SOUL.md.bak-*"))


def test_orchestrator_dirs_only_project_orchs(tmp_path):
    for n in ("a-orch", "b-orch", "orchestrator", "backend-engineer"):
        (tmp_path / "profiles" / n).mkdir(parents=True)
    root = str(tmp_path / "profiles")
    assert [os.path.basename(p) for p in orch_sync.orchestrator_dirs(root)] == ["a-orch", "b-orch"]
    assert [os.path.basename(p) for p in orch_sync.orchestrator_dirs(root, ["b-orch"])] == ["b-orch"]


def test_cli_orch_sync_json(tmp_path, monkeypatch, capsys):
    import hscc
    import rolelib
    _profile(tmp_path, "demo-orch", f"OLD BASE\n\n{ROLE}\n{OLD_OPS}")
    src = _source(tmp_path)
    monkeypatch.setattr(rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.setattr(hscc, "_base_identity", lambda: BASE)
    assert hscc.cmd_orch_sync(["--dry-run", "--json", "--skills-source", str(src)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["profiles"][0]["soul"] == "updated"
    assert hscc.cmd_orch_sync(["--bogus"]) == 1
