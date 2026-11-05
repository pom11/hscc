"""Tests for ensure_review_feature — idempotent, safe re-apply of the kanban
review feature into the Hermes runtime checkout."""

import os

import pytest

import ensure_review_feature as erf


def _make_runtime(tmp_path, *, marker=False, git=True):
    """Build a fake hermes runtime dir with tools/kanban_tools.py."""
    hermes = tmp_path / "hermes-agent"
    (hermes / "tools").mkdir(parents=True)
    body = "def _handle_submit_review(): pass\n" if marker else "def x(): pass\n"
    if marker:
        body += 'name="kanban_submit_review"\n'
    (hermes / "tools" / "kanban_tools.py").write_text(body)
    if git:
        (hermes / ".git").mkdir()
    return hermes


def test_already_present_is_noop(tmp_path, monkeypatch):
    hermes = _make_runtime(tmp_path, marker=True)
    calls = []
    monkeypatch.setattr(erf, "_git", lambda *a, **k: calls.append(a) or (True, "", ""))
    res = erf.ensure_review_feature(hermes_dir=str(hermes))
    assert res == {"status": "already_present", "ok": True}
    assert calls == []  # never touched git


def test_missing_runtime_skips(tmp_path):
    res = erf.ensure_review_feature(hermes_dir=str(tmp_path / "nope"))
    assert res["status"] == "skipped_missing" and res["ok"] is True
    # Honesty: the skip names the path it looked at, rather than a bare word.
    assert "nope" in res["detail"]


# --------------------------------------------------------------------------- #
# t_919ac971 — the runtime checkout must resolve through the ladder, not a
# raw $HERMES_HOME join (a profile-exported home made this step skip silently)
# --------------------------------------------------------------------------- #

def _fake_profile_home(tmp_path, *, at_root=True, marker=False):
    """Fabricate ``<root>/profiles/<name>`` + a runtime at the root.

    The live shape of every dispatched worker: HERMES_HOME points at the
    profile dir, ``hermes-agent`` sits one level above it. Nothing here touches
    the operator's real ``~/.hermes`` — the resolution is driven by the injected
    env/home below, not by the process env.
    """
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "backend-engineer"
    profile.mkdir(parents=True)
    if at_root:
        hermes = root / "hermes-agent"
        (hermes / "tools").mkdir(parents=True)
        body = ('def x(): pass\n' + ('name="kanban_submit_review"\n' if marker else ""))
        (hermes / "tools" / "kanban_tools.py").write_text(body)
        (hermes / ".git").mkdir()
        return str(profile), str(hermes)
    return str(profile), None


def test_profile_exported_home_does_not_skip_when_root_has_the_runtime(tmp_path, monkeypatch):
    """THE regression. HERMES_HOME=<root>/profiles/<name> and the real checkout
    at <root>/hermes-agent: the old import-time join resolved
    ``<profile>/hermes-agent`` — a path that cannot exist — so the step claimed
    ``skipped_missing``/ok and the dropped review feature stayed dropped while
    bootstrap printed a near-green row. It must find the root checkout instead."""
    profile, hermes = _fake_profile_home(tmp_path, at_root=True, marker=True)
    calls = []
    monkeypatch.setattr(erf, "_git", lambda *a, **k: calls.append(a) or (True, "", ""))
    res = erf.ensure_review_feature(env={"HERMES_HOME": profile}, home=str(tmp_path))
    assert res == {"status": "already_present", "ok": True}
    assert calls == []


def test_profile_exported_home_reaches_the_apply_path(tmp_path, monkeypatch):
    """Same layout, feature ABSENT: the step must actually run the cherry-pick.

    The skip was silent precisely because the apply path was never reached, so
    the regression is pinned at the apply too, not only at detection.
    """
    profile, hermes = _fake_profile_home(tmp_path, at_root=True, marker=False)
    seq = []

    def fake_git(args, cwd, **k):
        seq.append(args[0])
        assert cwd == hermes, "must operate on the ROOT checkout, not the profile dir"
        return True, ("abc123" if args[0] == "rev-parse" else ""), ""

    monkeypatch.setattr(erf, "_git", fake_git)
    res = erf.ensure_review_feature(env={"HERMES_HOME": profile}, home=str(tmp_path))
    assert res == {"status": "applied", "ok": True}
    assert "cherry-pick" in seq and "fetch" in seq


def test_nothing_exists_skips_and_names_every_candidate(tmp_path):
    """Nothing carries hermes-agent anywhere: skipped_missing/ok stays (there is
    genuinely no runtime to install into) but it must say where it looked."""
    res = erf.ensure_review_feature(
        env={"HERMES_HOME": str(tmp_path / ".hermes" / "profiles" / "worker")},
        home=str(tmp_path))
    assert res["status"] == "skipped_missing" and res["ok"] is True
    detail = res["detail"]
    assert str(tmp_path / ".hermes" / "profiles" / "worker" / "hermes-agent") in detail
    assert str(tmp_path / ".hermes" / "hermes-agent") in detail


def test_env_home_never_reads_the_operator_home(tmp_path, monkeypatch):
    """A test-fabricated env must fully seal resolution even if the worker env
    exports HERMES_HOME at the live profile (the suite's normal run mode)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "live-profile" / "profiles" / "x"))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    cands = erf.agent_dir_candidates({"HERMES_HOME": str(tmp_path / "h")},
                                     home=str(tmp_path / "h"))
    # Only fabricated roots — the injected `home` stands in for ~ , so the real
    # operator home (and the live exported HERMES_HOME) never appear.
    assert cands == [str(tmp_path / "h" / "hermes-agent"),
                     str(tmp_path / "h" / ".hermes" / "hermes-agent")]
    assert all(str(tmp_path) in c for c in cands)


def test_fallback_ladder_matches_the_shared_module(tmp_path):
    """Drift guard. ``_fallback_homes`` is the standalone-install copy of the
    ladder; if it drifts shorter than ``hermes_python.hermes_homes`` the fix
    regresses to the single-join bug for exactly the installs that use it."""
    hp = erf._load_hermes_python()
    if hp is None:  # pragma: no cover - hscc_daemon absent in a standalone install
        pytest.skip("hscc_daemon not importable — nothing to pin against")
    for env, home in [
        ({"HERMES_HOME": "/r/profiles/w"}, "/home/op"),
        ({"HERMES_HOME": "/srv/custom"}, "/home/op"),
        ({}, "/home/op"),
        ({"HERMES_HOME": "/r"}, "/r"),
    ]:
        assert erf._fallback_homes(env, home=home) == [
            os.path.join(h, "hermes-agent")
            for h in hp.hermes_homes(env, home=home)
        ], f"ladder drift for env={env} home={home}"


def test_module_binds_no_import_time_hermes_dir():
    """The bug's shape: a module-level ``HERMES_DIR`` joined from the env at
    IMPORT freezes the profile path before anything can resolve it. Keeping it
    gone is what makes the fix structural rather than a call-site patch."""
    assert not hasattr(erf, "HERMES_DIR"), (
        "resolve the checkout at call time through the ladder (t_919ac971)")



def test_not_git_reports(tmp_path):
    hermes = _make_runtime(tmp_path, marker=False, git=False)
    res = erf.ensure_review_feature(hermes_dir=str(hermes))
    assert res["status"] == "not_git" and res["ok"] is False


def test_unreadable_marker_is_not_claimed_present(tmp_path, monkeypatch):
    """If tools/kanban_tools.py can't be READ (missing/renamed/permission), we
    must NOT report already_present — we never looked. Previously the read
    error was swallowed and returned already_present:ok True, an environment
    fault rendered as a fact about the data. It must surface as a distinct
    non-ok status so bootstrap warns instead of silently skipping the
    install."""
    hermes = tmp_path / "hermes-agent"
    hermes.mkdir()
    # create a tools/ file we then make unreadable (or omit entirely)
    (hermes / "tools").mkdir(parents=True)
    # Simulate an unreadable marker file via monkeypatched _feature_state -> but
    # exercise the REAL read path: no kanban_tools.py at all -> OSError.
    called = []
    monkeypatch.setattr(erf, "_git",
                        lambda *a, **k: called.append(a) or (True, "", ""))
    res = erf.ensure_review_feature(hermes_dir=str(hermes))
    assert res["status"] == "unreadable" and res["ok"] is False
    assert called == []  # never touched git
    assert "kanban_tools.py" in res.get("detail", "")


def test_dirty_repo_refuses(tmp_path, monkeypatch):
    hermes = _make_runtime(tmp_path, marker=False)
    # status --porcelain returns uncommitted changes
    monkeypatch.setattr(erf, "_git",
                        lambda args, cwd, **k: (True, " M tools/kanban_tools.py", ""))
    res = erf.ensure_review_feature(hermes_dir=str(hermes))
    assert res["status"] == "dirty" and res["ok"] is False


def test_applies_when_absent_and_clean(tmp_path, monkeypatch):
    hermes = _make_runtime(tmp_path, marker=False)
    seq = []

    def fake_git(args, cwd, **k):
        seq.append(args[0])
        if args[0] == "status":
            return True, "", ""            # clean
        if args[0] == "fetch":
            return True, "", ""
        if args[0] == "rev-parse":
            return True, "abc123", ""
        if args[0] == "cherry-pick":
            return True, "", ""
        return True, "", ""

    monkeypatch.setattr(erf, "_git", fake_git)
    res = erf.ensure_review_feature(hermes_dir=str(hermes))
    assert res == {"status": "applied", "ok": True}
    assert "cherry-pick" in seq and "fetch" in seq


def test_conflict_aborts_cleanly(tmp_path, monkeypatch):
    hermes = _make_runtime(tmp_path, marker=False)
    seq = []

    def fake_git(args, cwd, **k):
        seq.append(tuple(args))
        if args[0] == "status":
            return True, "", ""
        if args[0] == "fetch":
            return True, "", ""
        if args[0] == "rev-parse":
            return True, "abc123", ""
        if args[:2] == ["cherry-pick", "--abort"]:
            return True, "", ""
        if args[0] == "cherry-pick":
            return False, "", "CONFLICT (content)"
        return True, "", ""

    monkeypatch.setattr(erf, "_git", fake_git)
    res = erf.ensure_review_feature(hermes_dir=str(hermes))
    assert res["status"] == "conflict" and res["ok"] is False
    # must have aborted to leave the runtime clean
    assert ("cherry-pick", "--abort") in seq


def test_fetch_failure_reports(tmp_path, monkeypatch):
    hermes = _make_runtime(tmp_path, marker=False)

    def fake_git(args, cwd, **k):
        if args[0] == "status":
            return True, "", ""
        if args[0] == "fetch":
            return False, "", "could not read from remote"
        return True, "", ""

    monkeypatch.setattr(erf, "_git", fake_git)
    res = erf.ensure_review_feature(hermes_dir=str(hermes))
    assert res["status"] == "fetch_failed" and res["ok"] is False


def test_option_smuggling_remote_rejected(tmp_path, monkeypatch):
    """An env-injected remote/branch starting with '-' is refused before any git call."""
    hermes = _make_runtime(tmp_path, marker=False)
    called = []
    monkeypatch.setattr(erf, "_git", lambda *a, **k: called.append(a) or (True, "", ""))
    res = erf.ensure_review_feature(hermes_dir=str(hermes), remote="--upload-pack=evil")
    assert res["status"] == "invalid_config" and res["ok"] is False
    res2 = erf.ensure_review_feature(hermes_dir=str(hermes), branch="-x;rm")
    assert res2["status"] == "invalid_config" and res2["ok"] is False
    assert called == []  # never reached git
