"""Spec provenance + coverage census (t_00d9581d).

The bug class, measured rather than inferred: ``rolelib.ROLES_DIR`` is
``dirname(rolelib.__file__)/roles`` — whichever checkout the CLI imported from —
while ``generate`` writes the profile into ``PROFILES_DIR``, which lives OUTSIDE
that tree. The fleet's own ops block (``generator.py``, "Missing specialist?
Create the profile") sends operators to mint from the DEPLOYED plugin copy
(``~/.hermes/plugins/hscc-roles/hscc.py create …``), which is not a git working
tree. So a minted spec can exist, mint a live profile, and then be deleted by the
next re-sync from the repo while the profile survives — which is exactly what
happened to ``marketing`` (t_f2630ca3): ~6 hours of a spec that was never tracked.

What these tests pin, and why each would have caught that:

  * ``roles_repo`` accepts ONLY a tree whose git toplevel is the plugin dir's
    parent — so the deployed copy can never be mistaken for the repo (the false
    all-clear that would make every other test here pointless);
  * ``tracked_spec_names`` answers ``None`` — "cannot tell" — rather than an empty
    set, so a non-repo never reads as "nothing is tracked" (every spec doomed) or
    as "all is fine";
  * ``create`` prints the exact ``git add``/commit lines, because the documented
    workflow writes the spec before anyone commits it;
  * ``list`` fails (rc 1) on a worker-signature profile with no spec — the pytest
    guard's own condition, now available on a deployed host — and warns (rc 0) on
    a spec that exists but is untracked, which is the doomed-soon state no existing
    check could see.

The backstop stays: ``test_generator.py::test_every_hscc_owned_worker_profile_has_role_spec``
is untouched by this file and must keep biting on its own.
"""
import os
import re
import subprocess

import pytest

import author
import hscc
import rolelib
import role_soul_check as rsc


# ── helpers ──────────────────────────────────────────────────────────────────

def _norm(text):
    """Collapse Rich's wrapping so assertions survive a narrow console."""
    return re.sub(r"\s+", " ", text)


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout


def _worker_soul(name):
    """A SOUL in the shape generate emits: the dir-name '## Role:' header plus the
    worker signature role_soul_check (and the pytest guard) classify on."""
    return (f"# Soul\n\n## Role: {name}\n\nIdentity text.\n\n"
            f"- You work in {rsc.WORKER_MARKER}.\n")


def _spec_text(name):
    return f"name: {name}\nidentity: I own {name} work.\nrouting_description: x\n"


def _write_profile(profiles_dir, name):
    pdir = profiles_dir / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "SOUL.md").write_text(_worker_soul(name), encoding="utf-8")
    return pdir


@pytest.fixture
def repo(tmp_path):
    """A stand-in for the tracked hscc checkout: a git repo whose toplevel holds
    ``hscc-roles/roles`` — the exact shape ``roles_repo`` must accept."""
    root = tmp_path / "repo"
    roles = root / "hscc-roles" / "roles"
    roles.mkdir(parents=True)
    (root / "README.md").write_text("# stand-in hscc repo\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "hscc-tests")
    return root, roles


# ── roles_repo: which tree is the tracked repo? ──────────────────────────────

def test_roles_repo_accepts_a_checkout_of_the_repo(repo):
    top, rel = rolelib.roles_repo(str(repo[1]))
    assert top == os.path.realpath(str(repo[0]))
    assert rel == "hscc-roles/roles"


def test_roles_repo_rejects_a_copy_inside_an_unrelated_repo(tmp_path):
    """The deployed-plugin shape: `.../plugins/hscc-roles/roles` sitting inside
    SOME git tree (a dotfiles repo over ~/.hermes). Its toplevel is not the plugin
    dir's parent, so it must NOT be accepted — accepting it would report a doomed
    spec as safely committed."""
    root = tmp_path / "dotfiles"
    roles = root / "plugins" / "hscc-roles" / "roles"
    roles.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "hscc-tests")
    assert rolelib.roles_repo(str(roles)) == (None, None)
    assert rolelib.tracked_spec_names(str(roles)) is None


def test_roles_repo_is_none_outside_any_repo(tmp_path):
    roles = tmp_path / "scratch" / "hscc-roles" / "roles"
    roles.mkdir(parents=True)
    assert rolelib.roles_repo(str(roles)) == (None, None)


def test_roles_repo_is_none_for_a_missing_dir(tmp_path):
    assert rolelib.roles_repo(str(tmp_path / "nope")) == (None, None)


# ── tracked_spec_names: the git census of roles/ ─────────────────────────────

def test_tracked_spec_names_reports_only_committed_specs(repo):
    root, roles = repo
    (roles / "coder.yaml").write_text(_spec_text("coder"), encoding="utf-8")
    (roles / "writer.yaml").write_text(_spec_text("writer"), encoding="utf-8")
    _git(root, "add", "hscc-roles/roles/coder.yaml")
    _git(root, "commit", "-q", "-m", "add coder spec")
    assert rolelib.tracked_spec_names(str(roles)) == {"coder.yaml"}


def test_tracked_spec_names_is_none_not_empty_outside_a_repo(tmp_path):
    """`None` = "cannot tell". An empty set would read as "every spec is doomed";
    a full set would read as "all is fine" — both are the false verdicts."""
    roles = tmp_path / "hscc-roles" / "roles"
    roles.mkdir(parents=True)
    (roles / "coder.yaml").write_text(_spec_text("coder"), encoding="utf-8")
    assert rolelib.tracked_spec_names(str(roles)) is None


# ── spec_provenance: the per-spec verdict ────────────────────────────────────

def test_provenance_untracked_names_the_exact_commit(repo):
    root, roles = repo
    path = roles / "seo.yaml"
    path.write_text(_spec_text("seo"), encoding="utf-8")
    prov = rolelib.spec_provenance(str(path), roles_dir=str(roles))
    assert prov["state"] == "untracked"
    assert prov["git_known"] is True
    assert prov["commands"] == [
        f"git -C {os.path.realpath(str(root))} add hscc-roles/roles/seo.yaml",
        'git -C ' + os.path.realpath(str(root))
        + ' commit -m "hscc-roles: add seo role spec"',
    ]


def test_provenance_tracked_once_committed(repo):
    root, roles = repo
    path = roles / "seo.yaml"
    path.write_text(_spec_text("seo"), encoding="utf-8")
    _git(root, "add", "hscc-roles/roles/seo.yaml")
    _git(root, "commit", "-q", "-m", "add seo spec")
    prov = rolelib.spec_provenance(str(path), roles_dir=str(roles))
    assert prov["state"] == "tracked"
    assert prov["commands"] == []
    assert prov["git_known"] is True


def test_provenance_outside_repo_is_never_an_all_clear(tmp_path):
    roles = tmp_path / "deployed" / "hscc-roles" / "roles"
    roles.mkdir(parents=True)
    path = roles / "seo.yaml"
    path.write_text(_spec_text("seo"), encoding="utf-8")
    prov = rolelib.spec_provenance(str(path), roles_dir=str(roles))
    assert prov["state"] == "outside-repo"
    assert prov["git_known"] is False
    assert prov["commands"] == []      # committing THERE would not help


def test_provenance_reports_unknown_when_git_will_not_answer(repo, monkeypatch):
    """A git that answers `rev-parse` but not `ls-files` must land in "unknown"
    (warned about), NOT in "tracked" — silence is the failure being fixed."""
    root, roles = repo
    path = roles / "seo.yaml"
    path.write_text(_spec_text("seo"), encoding="utf-8")

    def fake_git(cwd, *args):
        if args and args[0] == "rev-parse":
            return str(root)
        return None

    monkeypatch.setattr(rolelib, "_git", fake_git)
    prov = rolelib.spec_provenance(str(path), roles_dir=str(roles))
    assert prov["state"] == "unknown"
    assert prov["git_known"] is False
    assert any("add hscc-roles/roles/seo.yaml" in c for c in prov["commands"])


def test_provenance_never_raises_when_git_is_absent(repo, monkeypatch):
    _root, roles = repo
    path = roles / "seo.yaml"
    path.write_text(_spec_text("seo"), encoding="utf-8")

    def boom(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(rolelib.subprocess, "run", boom)
    assert rolelib.roles_repo(str(roles)) == (None, None)
    assert rolelib.spec_provenance(str(path), roles_dir=str(roles))["state"] \
        == "outside-repo"


# ── cmd_create: report the follow-up commit ───────────────────────────────────

def test_cmd_create_prints_the_commit_for_an_untracked_spec(repo, monkeypatch, capsys):
    _root, roles = repo
    monkeypatch.setattr(rolelib, "ROLES_DIR", str(roles))
    assert hscc.cmd_create(["seo", "Drives organic search."]) == 0
    out = _norm(capsys.readouterr().out)
    assert "NOT tracked by git" in out
    assert "git -C" in out
    assert "add hscc-roles/roles/seo.yaml" in out
    assert "hscc-roles: add seo role spec" in out


def test_cmd_create_says_so_when_minted_outside_the_repo(tmp_path, monkeypatch,
                                                         capsys):
    """The marketing case end to end: `create` run against a non-repo tree must
    name the loss and tell the operator to move the spec into the repo."""
    roles = tmp_path / "deployed" / "hscc-roles" / "roles"
    monkeypatch.setattr(rolelib, "ROLES_DIR", str(roles))
    assert hscc.cmd_create(["seo", "Drives organic search."]) == 0
    out = _norm(capsys.readouterr().out)
    assert "OUTSIDE the tracked hscc repo" in out
    assert "hscc-roles/roles/" in out
    # the spec really was written — the warning is the whole point, not a refusal
    assert os.path.isfile(os.path.join(str(roles), "seo.yaml"))


def test_cmd_create_report_renders_the_tracked_state():
    """The all-clear branch exists and is only reachable with git's agreement."""
    line = hscc._create_provenance_line(
        {"state": "tracked", "repo": "/r", "rel": "hscc-roles/roles",
         "name": "seo", "commands": [], "git_known": True,
         "detail": "hscc-roles/roles/seo.yaml is tracked by git in /r"})
    assert "tracked by git" in line
    line = hscc._create_provenance_line(
        {"state": "outside-repo", "repo": None, "rel": None, "name": "seo",
         "commands": [], "git_known": False, "detail": "not in the repo"})
    assert "OUTSIDE the tracked hscc repo" in line


# ── cmd_list: the census, on a deployed host ─────────────────────────────────

def test_cmd_list_fails_on_a_worker_profile_with_no_spec(tmp_path, monkeypatch,
                                                         capsys):
    roles = tmp_path / "hscc-roles" / "roles"
    roles.mkdir(parents=True)
    (roles / "coder.yaml").write_text(_spec_text("coder"), encoding="utf-8")
    profiles = tmp_path / "profiles"
    _write_profile(profiles, "coder")
    _write_profile(profiles, "ghostrole")          # the marketing shape
    monkeypatch.setattr(rolelib, "ROLES_DIR", str(roles))
    monkeypatch.setattr(rolelib, "PROFILES_DIR", str(profiles))

    assert hscc.cmd_list() == 1
    out = _norm(capsys.readouterr().out)
    assert "orphan" in out.lower()
    assert "ghostrole" in out
    assert "roles/ghostrole.yaml" in out


def test_cmd_list_reports_coverage_ok_when_every_profile_has_a_spec(
        repo, tmp_path, monkeypatch, capsys):
    root, roles = repo
    (roles / "coder.yaml").write_text(_spec_text("coder"), encoding="utf-8")
    _git(root, "add", "hscc-roles/roles/coder.yaml")
    _git(root, "commit", "-q", "-m", "add coder spec")
    profiles = tmp_path / "profiles"
    _write_profile(profiles, "coder")
    monkeypatch.setattr(rolelib, "ROLES_DIR", str(roles))
    monkeypatch.setattr(rolelib, "PROFILES_DIR", str(profiles))

    assert hscc.cmd_list() == 0
    out = _norm(capsys.readouterr().out)
    assert "coverage ok" in out
    assert "orphan" not in out.lower()


def test_cmd_list_warns_about_an_untracked_spec_without_failing(
        repo, tmp_path, monkeypatch, capsys):
    """The doomed-soon state: spec present (so `has_spec` is true and the pytest
    guard is green) but not in `git ls-files`. Warn, do not fail — the fleet's ops
    block mints specs ahead of the commit card on purpose."""
    root, roles = repo
    (roles / "coder.yaml").write_text(_spec_text("coder"), encoding="utf-8")
    _git(root, "add", "hscc-roles/roles/coder.yaml")
    _git(root, "commit", "-q", "-m", "add coder spec")
    (roles / "seo.yaml").write_text(_spec_text("seo"), encoding="utf-8")
    profiles = tmp_path / "profiles"
    _write_profile(profiles, "coder")
    _write_profile(profiles, "seo")
    monkeypatch.setattr(rolelib, "ROLES_DIR", str(roles))
    monkeypatch.setattr(rolelib, "PROFILES_DIR", str(profiles))

    assert hscc.cmd_list() == 0
    out = _norm(capsys.readouterr().out)
    assert "untracked" in out
    assert "seo" in out
    assert "ls-files" in out


def test_cmd_list_flags_provenance_unknown_outside_a_repo(tmp_path, monkeypatch,
                                                          capsys):
    roles = tmp_path / "deployed" / "hscc-roles" / "roles"
    roles.mkdir(parents=True)
    (roles / "coder.yaml").write_text(_spec_text("coder"), encoding="utf-8")
    profiles = tmp_path / "profiles"
    _write_profile(profiles, "coder")
    monkeypatch.setattr(rolelib, "ROLES_DIR", str(roles))
    monkeypatch.setattr(rolelib, "PROFILES_DIR", str(profiles))

    assert hscc.cmd_list() == 0
    out = _norm(capsys.readouterr().out)
    assert "provenance unknown" in out
    assert "coverage ok" not in out


def test_cmd_list_calls_a_vacuous_scan_vacuous(tmp_path, monkeypatch, capsys):
    """No profiles dir is NOT health — the reason has to be printed with the zero."""
    roles = tmp_path / "hscc-roles" / "roles"
    roles.mkdir(parents=True)
    (roles / "coder.yaml").write_text(_spec_text("coder"), encoding="utf-8")
    monkeypatch.setattr(rolelib, "ROLES_DIR", str(roles))
    monkeypatch.setattr(rolelib, "PROFILES_DIR", str(tmp_path / "no-profiles"))

    assert hscc.cmd_list() == 0
    out = _norm(capsys.readouterr().out)
    assert "VACUOUS" in out
    assert "not an all-clear" in out.lower()


def test_census_reuses_the_role_soul_check_predicate(monkeypatch, tmp_path):
    """One predicate, not two. The orphan half of the census must come from
    ``role_soul_check.scan`` rows, so the census and the doctor can never disagree
    about what "has a spec" means. A hand-rolled second implementation here would
    silently drift from the one the doctor reports."""
    calls = []

    def fake_scan(profiles_dir=None, **kw):
        calls.append(profiles_dir)
        return {"scanned": 2, "worker_scanned": 2, "nonworker": 0,
                "vacuous": False, "detail": "",
                "rows": [{"profile": "coder", "worker": True, "has_spec": True},
                         {"profile": "ghostrole", "worker": True,
                          "has_spec": False}]}

    monkeypatch.setattr(rsc, "scan", fake_scan)
    roles = tmp_path / "hscc-roles" / "roles"
    roles.mkdir(parents=True)
    monkeypatch.setattr(rolelib, "ROLES_DIR", str(roles))
    monkeypatch.setattr(rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))

    census = rsc.spec_coverage()
    assert calls == [None] or len(calls) == 1        # scan is THE source
    assert census["orphans"] == ["ghostrole"]

    monkeypatch.setattr(hscc.role_soul_check, "spec_coverage",
                        lambda *a, **k: dict(census))
    assert hscc._report_spec_coverage() == 1


def test_spec_coverage_orphan_shape_matches_the_doctor(tmp_path, monkeypatch):
    """A profile the doctor reports ``has_spec: false`` is the SAME profile the
    census reports as an orphan — the two surfaces must not disagree on a fixture."""
    roles = tmp_path / "hscc-roles" / "roles"
    roles.mkdir(parents=True)
    (roles / "coder.yaml").write_text(_spec_text("coder"), encoding="utf-8")
    profiles = tmp_path / "profiles"
    _write_profile(profiles, "coder")
    _write_profile(profiles, "ghostrole")
    monkeypatch.setattr(rolelib, "ROLES_DIR", str(roles))
    monkeypatch.setattr(rolelib, "PROFILES_DIR", str(profiles))

    doctor_orphans = [r["profile"] for r in rsc.scan()["rows"]
                      if r["worker"] and not r["has_spec"]]
    assert doctor_orphans == ["ghostrole"]
    assert rsc.spec_coverage()["orphans"] == doctor_orphans


def test_author_still_returns_the_path_unchanged(repo, monkeypatch):
    """`create_role`'s contract is untouched (t_f2630ca3 minted a spec by calling
    it directly); the provenance report is a CLI concern, not a write-path one."""
    _root, roles = repo
    monkeypatch.setattr(rolelib, "ROLES_DIR", str(roles))
    path = author.create_role("seo", "Drives organic search.")
    assert path == os.path.join(str(roles), "seo.yaml")
    assert rolelib.load_spec(path)["name"] == "seo"
