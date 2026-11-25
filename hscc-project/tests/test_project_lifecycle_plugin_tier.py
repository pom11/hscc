"""Step 6a seeds the new orchestrator home's plugins.enabled tier (t_d1f2faa7).

R5 of the driver ruling (docs/audits/2026-11-23-main-only-surface.md §6) says
bootstrap must write an explicit ``plugins.enabled`` into EVERY profile home *and
seed NEW profiles the same way*. ``flightdeck project new`` creates the per-project
orchestrator with ``hermes profile create`` and touched nothing else, so every
``<name>-orch`` it provisions was the key-less home that half of R5 is about.

Everything here runs through the module's injectable ``_run`` seam — no ``hermes``
CLI executes — except the two end-to-end cases, which run the real bootstrap writer
against a throwaway ``HERMES_HOME`` + ``HSCC_DIR`` (still no hermes, no cluster;
the sandbox rule that made the brew-keg delegation necessary, t_9a9aeab1).

D-A: the tier is ``hscc-roles/main_orch.py``'s answer. This module must not spell a
profile name or a plugin name, and the last test pins that.
"""
import json
import os
import subprocess

import pytest
import yaml

from flightdeck.core import project_lifecycle as lifecycle

MAIN = "ops"                       # deliberately not the resolver's default name
TIER_FLAG = "--plugins-tier-only"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A fake Hermes root + an HSCC_DIR storing a NON-default main name."""
    root = tmp_path / "hermes"
    (root / "profiles").mkdir(parents=True)
    hscc = tmp_path / "hscc"
    hscc.mkdir()
    (hscc / "main-orchestrator.json").write_text(
        json.dumps({"name": MAIN}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HSCC_DIR", str(hscc))
    monkeypatch.delenv("HSCC_BOOTSTRAP_DIR", raising=False)
    lifecycle.reset_hermes_python_cache()     # env changed; drop any memoised answer
    return {"root": root, "hscc": hscc}


class Runner:
    """A ``_run`` seam: answers ``hermes profile`` from canned state, records all.

    Any other argv (the bootstrap writer call) is answered from ``tier_result``,
    which may be a CompletedProcess or a callable(cmd) -> CompletedProcess.
    """

    def __init__(self, exists=(), create_fails=False, tier_result=None):
        self.exists = list(exists)
        self.create_fails = create_fails
        self.calls = []
        self.tier_result = tier_result
        self.profile_creates = []

    def _proc(self, cmd, rc=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

    def __call__(self, cmd, cwd):
        self.calls.append(list(cmd))
        if cmd[:2] == ["hermes", "profile"]:
            verb = cmd[2]
            if verb == "list":
                return self._proc(cmd, 0, "\n".join(self.exists) + "\n")
            if verb == "create":
                if self.create_fails:
                    return self._proc(cmd, 1, "", "profile create refused")
                self.exists.append(cmd[3])
                self.profile_creates.append(cmd[3])
                return self._proc(cmd, 0)
        result = self.tier_result
        proc = result(cmd) if callable(result) else result
        return proc if proc is not None else self._proc(cmd, 0)

    @property
    def tier_calls(self):
        return [c for c in self.calls if TIER_FLAG in c]


class RealWriterRunner(Runner):
    """Fake the ``hermes`` CLI, run the bootstrap writer for real.

    The seam is per-command on purpose: the card forbids a real ``hermes`` run,
    and nothing forbids proving the writer's own contract — flag name, argument
    order, exit code — against its real implementation. Everything it writes goes
    to the tmp home the fixture points ``HERMES_HOME`` at.
    """

    def __call__(self, cmd, cwd):
        if cmd[:2] == ["hermes", "profile"]:
            return super().__call__(cmd, cwd)      # parent records the call
        self.calls.append(list(cmd))
        # Step 6a now also asks the real installer for orchpad. Its pip step is
        # deliberately NOT run from this suite (the card's read-only rule), so
        # answer that one command with the installer's own report shape.
        if any(str(part).endswith("install_orchpad.py") for part in cmd):
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"ok": True, "status": "already"}),
                stderr="")
        return lifecycle._default_run(cmd, cwd)


# ── the seeding call itself ──────────────────────────────────────────────────

def test_create_seeds_the_new_home(sandbox):
    """The gap: after `hermes profile create`, the tier write must be attempted."""
    runner = Runner()
    report = {}
    assert lifecycle.ensure_profile("pickolo", _run=runner,
                                   tier_report=report) == "created"
    calls = runner.tier_calls
    assert len(calls) == 1, f"expected exactly one tier call, got {calls}"
    cmd = calls[0]
    assert cmd[1].endswith(os.path.join("hscc-bootstrap", "enable_plugins.py")), cmd
    assert cmd[2] == TIER_FLAG
    assert cmd[3] == "--home"
    assert cmd[4] == str(sandbox["root"] / "profiles" / "pickolo-orch")
    # the profile create really happened, and the seed follows it
    assert runner.profile_creates == ["pickolo-orch"]
    assert runner.calls.index(cmd) > runner.calls.index(
        ["hermes", "profile", "create", "pickolo-orch"])
    assert report["status"] == "seeded"


def test_existing_profile_is_still_seeded(sandbox):
    """A re-run repairs a home whose first seed never landed (idempotent)."""
    runner = Runner(exists=["pickolo-orch"])
    report = {}
    assert lifecycle.ensure_profile("pickolo", _run=runner,
                                   tier_report=report) == "skipped"
    assert runner.profile_creates == [], "an existing profile must not be re-created"
    assert len(runner.tier_calls) == 1
    assert report["status"] == "seeded"


def test_already_tiered_home_is_reported_not_rewritten(sandbox):
    runner = Runner(tier_result=subprocess.CompletedProcess(
        [], 0, stdout=f"plugins.enabled already tiered (no change): /x\n"))
    report = {}
    lifecycle.ensure_profile("pickolo", _run=runner, tier_report=report)
    assert report["status"] == "already-tiered"


# ── never fatal ──────────────────────────────────────────────────────────────

def test_failing_seed_still_creates_the_profile(sandbox):
    """Seeding is not allowed to become a new failure mode for step 6a."""
    runner = Runner(tier_result=subprocess.CompletedProcess(
        [], 1, stdout="", stderr="bootstrap writer exploded"))
    report = {}
    assert lifecycle.ensure_profile("pickolo", _run=runner,
                                   tier_report=report) == "created"
    assert report["status"].startswith("failed:rc=1")
    assert "bootstrap writer exploded" in report["status"]


def test_missing_bootstrap_writer_is_skipped_not_fatal(sandbox, monkeypatch):
    monkeypatch.setattr(lifecycle, "_bootstrap_enable_plugins_script", lambda *a: None)
    runner = Runner()
    report = {}
    assert lifecycle.ensure_profile("pickolo", _run=runner,
                                   tier_report=report) == "created"
    assert runner.tier_calls == []
    assert report["status"] == "skipped:no-bootstrap-writer"


def test_seeder_never_raises_even_when_its_internals_break(sandbox, monkeypatch):
    """Any exception inside the seeder degrades to a token (best-effort by contract)."""
    def _boom(*a, **k):
        raise RuntimeError("resolver blew up")
    monkeypatch.setattr(lifecycle, "_bootstrap_enable_plugins_script", _boom)
    report = {}
    assert lifecycle.ensure_profile("pickolo", _run=Runner(),
                                    tier_report=report) == "created"
    assert report["status"] == "failed:RuntimeError"


def test_real_create_failure_is_still_a_lifecycle_error(sandbox):
    """The one thing that must still raise is the create itself."""
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.ensure_profile("pickolo", _run=Runner(create_fails=True))


# ── home + writer resolution ─────────────────────────────────────────────────

def test_profile_scoped_hermes_home_resolves_up_to_the_root(tmp_path, monkeypatch):
    """A dispatched worker runs with HERMES_HOME=<root>/profiles/<name>.

    Without the grandparent step the tier would be written into the caller's own
    home instead of the new profile's — the ambient-state class of bug that keeps
    recurring here, so it is pinned rather than assumed.
    """
    root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "backend-engineer"))
    assert lifecycle._hermes_root_home() == str(root)
    assert lifecycle._orch_profile_home("pickolo-orch") == str(
        root / "profiles" / "pickolo-orch")


def test_plain_hermes_home_is_the_root_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    assert lifecycle._hermes_root_home() == str(tmp_path / "hermes")


def test_the_writer_resolves_in_this_checkout(sandbox):
    """The repo-sibling candidate must exist, or production silently no-ops."""
    script = lifecycle._bootstrap_enable_plugins_script()
    assert script and os.path.isfile(script), script
    assert script.endswith(os.path.join("hscc-bootstrap", "enable_plugins.py"))


def test_bootstrap_dir_override_wins(sandbox, tmp_path):
    other = tmp_path / "elsewhere" / "hscc-bootstrap"
    other.mkdir(parents=True)
    script = other / "enable_plugins.py"
    script.write_text("# not the writer\n", encoding="utf-8")
    os.environ.pop("HSCC_BOOTSTRAP_DIR", None)
    assert lifecycle._bootstrap_enable_plugins_script(
        {"HSCC_BOOTSTRAP_DIR": str(other)}) == str(script)


# ── end to end: the two halves really connect ────────────────────────────────

def test_end_to_end_seed_writes_the_hooks_only_tier(sandbox):
    """Real writer, real config.yaml, tmp home — no hermes, no cluster.

    The seam is faked only for the ``hermes`` CLI: this is the one place that can
    prove the flag name, the argument order and the exit-code contract line up
    with what bootstrap actually implements, which no stubbed assertion shows.
    """
    script = lifecycle._bootstrap_enable_plugins_script()
    if script is None:                          # pragma: no cover - repo always has it
        pytest.skip("no bootstrap writer in this tree")
    home = sandbox["root"] / "profiles" / "pickolo-orch"
    home.mkdir(parents=True)                    # what `hermes profile create` leaves
    report = {}
    assert lifecycle.ensure_profile(
        "pickolo", _run=RealWriterRunner(), tier_report=report) == "created"
    assert report["status"] == "seeded", report
    doc = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    # <main> is `ops`, so this home is NOT the main orchestrator's: hooks only.
    assert doc["plugins"]["enabled"] == ["hscc-cluster"]
    assert list(doc) == ["plugins"], "the tier-only write leaked other keys"


def test_end_to_end_seed_is_idempotent(sandbox):
    home = sandbox["root"] / "profiles" / "pickolo-orch"
    home.mkdir(parents=True)
    runner = RealWriterRunner()
    first, second = {}, {}
    lifecycle.ensure_profile("pickolo", _run=runner, tier_report=first)
    text = (home / "config.yaml").read_text(encoding="utf-8")
    assert lifecycle.ensure_profile("pickolo", _run=runner,
                                    tier_report=second) == "skipped"
    assert second["status"] == "already-tiered", second
    assert (home / "config.yaml").read_text(encoding="utf-8") == text


# ── D-A pin ──────────────────────────────────────────────────────────────────

def test_the_module_spells_no_profile_name_and_no_plugin_name():
    """No second resolver, no hard-coded tier (ruling D-A).

    Two halves, both structural rather than substring-grep-on-a-word: the module
    may not name a plugin or the resolver's default profile, and it may not reach
    the resolver itself — the tier is ``main_orch``'s answer and this file only
    ever asks the bootstrap writer for it.
    """
    src = open(lifecycle.__file__, encoding="utf-8").read()
    for banned in ('"hscc-cluster"', '"hscc-commands"', '"sparkrun-hermes"',
                   '"general"', "HOOKS_ONLY_PLUGINS", "CLUSTER_PLUGINS"):
        assert banned not in src, f"flightdeck must not spell {banned}"
    for resolver in ("main_orch", "plugins_for_home", "cluster_tools_allowed",
                     "ensure_plugins_tier_for_home", "generate_profile"):
        assert resolver not in src.replace(
            "hscc-roles/main_orch", "hscc-roles/<resolver>"), (
            f"flightdeck must not reach the resolver ({resolver}); it asks the "
            "bootstrap writer and nothing else")
    assert TIER_FLAG in src, "the narrow entry point is the whole contract"
