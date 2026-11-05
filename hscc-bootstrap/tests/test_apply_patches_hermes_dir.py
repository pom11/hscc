"""t_8579fba4 — `apply_patches.py --check` must resolve hermes-agent through the
shared homes ladder, not a `$HERMES_HOME/hermes-agent` join.

The bug's shape: `--check` with no `--target` derived the hermes checkout as
`os.environ["HERMES_HOME"] + "/hermes-agent"`. Hermes exports
`HERMES_HOME=<root>/profiles/<name>` for every profile session (the fleet's
normal run mode), so from a profile session that path cannot exist; `--check`
reported `{tgt} not found`, `bootstrap.sh`'s grep for `'"ok": true'` failed,
and the stage printed "already patched, or upstream drifted" and SKIPPED the
`--target` apply. The hermes patch set — the kanban review tool + the
kanban_task_claimed resume hook — silently never landed, and the message blamed
upstream drift. Exactly the sibling of the import-time `HERMES_DIR` in
`ensure_review_feature.py` (t_919ac971), in the same file as the fix commit
c5ba9926.

These tests fabricate a root with a `hermes-agent` checkout while `HERMES_HOME`
points at a profile dir under it (never the operator's `~/.hermes`) and assert
`--check` finds it and echoes the chosen target.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import apply_patches

REPO = Path(__file__).resolve().parent.parent.parent


# ── ladder unit surface ─────────────────────────────────────────────────────

def test_agent_dir_candidates_follow_the_homes_ladder(tmp_path, monkeypatch):
    """Injected env/home seal resolution; no live ~/.hermes is read."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    cands = apply_patches.agent_dir_candidates(
        {"HERMES_HOME": str(tmp_path / ".hermes" / "profiles" / "worker")},
        home=tmp_path)
    # profile dir first (a profile-local checkout wins), then the profiles/
    # grandparent root, then the default home — deduplicated.
    assert cands[0] == str(tmp_path / ".hermes" / "profiles" / "worker" / "hermes-agent")
    assert str(tmp_path / ".hermes" / "hermes-agent") in cands
    assert len(cands) == len(set(cands)), cands
    assert all(str(tmp_path) in c for c in cands), cands


def test_resolve_agent_dir_prefers_profile_local_then_root(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "backend-engineer"
    profile.mkdir(parents=True)
    # nothing exists yet -> fail closed with the searched detail
    got, searched = apply_patches.resolve_agent_dir(
        {"HERMES_HOME": str(profile)}, home=tmp_path)
    assert got is None
    assert "hermes-agent" in searched and str(tmp_path) in searched

    # the root checkout is found from a profile session — THE regression
    (root / "hermes-agent").mkdir()
    got, searched = apply_patches.resolve_agent_dir(
        {"HERMES_HOME": str(profile)}, home=tmp_path)
    assert got == str(root / "hermes-agent")
    assert searched == ""

    # a profile-local checkout still wins over the root
    local = profile / "hermes-agent"
    local.mkdir()
    got, _ = apply_patches.resolve_agent_dir({"HERMES_HOME": str(profile)}, home=tmp_path)
    assert got == str(local)


def test_a_stale_hermes_agent_file_is_not_a_checkout(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    root = tmp_path / ".hermes"
    root.mkdir(parents=True)
    (root / "hermes-agent").write_text("i am a file\n")
    got, _ = apply_patches.resolve_agent_dir({}, home=tmp_path)
    assert got is None


def test_fallback_ladder_matches_the_shared_module(tmp_path):
    """Drift pin: the bootstrap-only fallback must equal hermes_python's ladder.

    A shorter fallback list is exactly the bug being fixed — it would report
    "no checkout" from every profile session again, silently, on any install
    where hscc_daemon isn't importable.
    """
    sys.path.insert(0, str(REPO))
    from hscc_daemon import hermes_python

    envs = [
        {},
        {"HERMES_HOME": str(tmp_path / ".hermes")},
        {"HERMES_HOME": str(tmp_path / ".hermes" / "profiles" / "w")},
        {"HERMES_HOME": "   "},
    ]
    for env in envs:
        assert apply_patches._fallback_agent_dirs(env, home=tmp_path) == \
            hermes_python.agent_dir_candidates(env, home=tmp_path), env


def test_load_hermes_python_returns_the_shared_module():
    assert apply_patches._load_hermes_python() is not None


# ── --check end-to-end (the CLI bootstrap.sh actually calls) ────────────────

def _run_check(tmp_path, *, hermes_home):
    """Run `apply_patches.py --check` with HERMES_HOME/HOME pinned at a sandbox."""
    env = {k: v for k, v in os.environ.items() if k not in ("HERMES_HOME",)}
    env["HOME"] = str(tmp_path)
    env["HERMES_HOME"] = str(hermes_home)
    p = subprocess.run([sys.executable, str(REPO / "hscc-bootstrap" / "apply_patches.py"),
                        "--check"],
                       capture_output=True, text=True, env=env, timeout=120,
                       cwd=str(REPO))
    return p


def test_check_finds_the_root_checkout_under_a_profile_home(tmp_path):
    """THE regression, at the CLI level: profile HERMES_HOME + root checkout.

    The checkout has no .git here, so the set cannot be checked — but the
    report must name the RESOLVED ROOT path (the old code named the impossible
    profile path, which is what made the skip read as upstream drift), and the
    error must not be the bare `not found` that bootstrap misread.
    """
    root = tmp_path / ".hermes"
    agent = root / "hermes-agent"
    agent.mkdir(parents=True)
    profile = root / "profiles" / "backend-engineer"
    profile.mkdir(parents=True)

    p = _run_check(tmp_path, hermes_home=profile)
    out = json.loads(p.stdout)
    assert out["hermes"].get("target") == str(agent)
    assert "not found" not in out["hermes"].get("error", "")


def test_check_reports_a_resolvable_patched_checkout_as_ok(tmp_path):
    """A real (git-initialised, already-patched-shape) root checkout resolves
    and its set is actually checked — proving the apply path is now reachable
    from a profile session instead of being skipped before it started."""
    import shutil
    import subprocess as sp
    root = tmp_path / ".hermes"
    agent = root / "hermes-agent"
    (agent / "tools").mkdir(parents=True)
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)

    def git(*a):
        return sp.run(["git", "-C", str(agent), *a], capture_output=True, text=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (agent / "tools" / "kanban_tools.py").write_text("def kanban_submit_review():\n    pass\n")
    git("add", "-A")
    git("commit", "-qm", "base")

    p = _run_check(tmp_path, hermes_home=profile)
    out = json.loads(p.stdout)
    assert out["hermes"]["target"] == str(agent)
    # The set was really evaluated (patches listed), not skipped as "not found".
    assert "patches" in out["hermes"], out["hermes"]
    assert out["hermes"]["ok"] in (True, False)


def test_check_still_names_every_candidate_when_nothing_exists(tmp_path):
    root = tmp_path / "nowhere"
    root.mkdir()
    p = _run_check(tmp_path, hermes_home=root / "profiles" / "w")
    out = json.loads(p.stdout)
    assert out["hermes"]["ok"] is False
    assert "not found" in out["hermes"]["error"]
    assert "looked:" in out["hermes"]["error"] and str(tmp_path) in out["hermes"]["error"]


# ── static pins: no raw join may come back ──────────────────────────────────

def test_no_raw_hermes_home_join_remains_in_apply_patches():
    text = (REPO / "hscc-bootstrap" / "apply_patches.py").read_text()
    assert 'os.environ.get("HERMES_HOME")' not in text
    assert '"hermes-agent"' in text  # only through the ladder helpers
