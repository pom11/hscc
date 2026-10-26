"""Tests for the Hermes-runtime delegation + lifecycle atomicity (t_9a9aeab1).

The bug: `hscc` installed via brew runs inside an isolated keg venv that
carries NO hermes_cli / hermes_state / httpx (Formula/hscc.rb installs only
PyYAML/rich/psutil/markdown-it-py/mdurl/pygments). `flightdeck project new
--apply` died inside ensure_session -> _open_profile_session_db with
ModuleNotFoundError and left an orphan stub <name>-orch profile behind.

These tests pin the two halves of the fix WITHOUT touching the operator's
real ~/.hermes:

  (a) an interpreter that cannot host the Hermes runtime DELEGATES every
      runtime touch to the hermes venv interpreter (one JSON-line op per
      call), and reports HermesRuntimeUnavailable — an environment fault —
      when no interpreter with the runtime can be resolved;
  (b) a profile CREATED by create_project is rolled back when the session
      step fails afterwards (no orphan stub), an already-existing profile is
      never deleted, and a failed rollback is reported with the exact
      cleanup command;
  (c) the retry path is idempotent: re-running after a rollback recreates the
      profile, ensures the session once, and registers once.

Everything external is faked the way the neighbouring lifecycle tests fake
it: the ``_run`` seam answers ``hermes profile`` commands from canned state;
the remote-op transport is exercised through the ``runner`` seam (argv
capture) and, for the wire protocol itself, against a FABRICATED agent tree
+ fabricated venv python planted under tmp_path (the same fixture style as
the libexec-resolution tests).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from flightdeck.core import project_lifecycle as lifecycle
from flightdeck.core.project_lifecycle import (
    HermesRuntimeUnavailable,
    LifecycleError,
    RemoteRuntimeError,
)

# The probe command resolve_hermes_python() runs against each candidate.
PROBE_CODE = "import hermes_state, hermes_cli.profiles"


@pytest.fixture(autouse=True)
def _no_interpreter_cache(monkeypatch):
    """Interpreter resolution is memoised in production; tests must not leak
    a resolution into the next test (env varies per test)."""
    lifecycle.reset_hermes_python_cache()
    yield
    lifecycle.reset_hermes_python_cache()


@pytest.fixture(autouse=True)
def _no_real_hermes_home(monkeypatch, tmp_path):
    """Never let a test resolve the OPERATOR's hermes tree: HERMES_HOME and
    HOME point at tmp unless a test opts in explicitly."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HSCC_HERMES_PYTHON", raising=False)
    monkeypatch.delenv("HERMES_AGENT_PATH", raising=False)


# --------------------------------------------------------------------------- #
# (a1) resolution: which interpreter gets chosen
# --------------------------------------------------------------------------- #

class FakeProbe:
    """Stand in for subprocess.run inside project_lifecycle: only the import
    probe runs there during resolution. ``ok`` lists candidate paths that can
    host the runtime; the probe's returncode decides."""

    def __init__(self, ok_paths=()):
        self.ok = set(ok_paths)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        rc = 0 if cmd[0] in self.ok else 1
        return subprocess.CompletedProcess(cmd, rc, "", "")


def test_resolve_prefers_hscc_hermes_python_override(monkeypatch, tmp_path):
    override = tmp_path / "operator" / "bin" / "python"
    override.parent.mkdir(parents=True)
    override.write_text("")
    override.chmod(0o755)
    monkeypatch.setenv("HSCC_HERMES_PYTHON", str(override))
    probe = FakeProbe(ok_paths={str(override)})
    monkeypatch.setattr(lifecycle.subprocess, "run", probe)
    assert lifecycle.resolve_hermes_python() == str(override)
    # The override is probed FIRST — an override that cannot host the runtime
    # must be rejected, not trusted blindly.
    assert probe.calls[0][0] == str(override)


def test_resolve_rejects_override_that_cannot_import_runtime(monkeypatch, tmp_path):
    override = tmp_path / "broken" / "python"
    override.parent.mkdir(parents=True)
    override.write_text("")
    override.chmod(0o755)
    hermes_py = (tmp_path / "home" / ".hermes" / "hermes-agent" / "venv"
                 / "bin" / "python3")
    hermes_py.parent.mkdir(parents=True)
    hermes_py.write_text("")
    hermes_py.chmod(0o755)
    monkeypatch.setenv("HSCC_HERMES_PYTHON", str(override))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    probe = FakeProbe(ok_paths={str(hermes_py)})
    monkeypatch.setattr(lifecycle.subprocess, "run", probe)
    assert lifecycle.resolve_hermes_python() == str(hermes_py)


def test_resolve_honours_env_hermes_home_layout(monkeypatch, tmp_path):
    """A non-default HERMES_HOME is consulted before ~/.hermes (matches
    api_launchd.recommend_api_python's HERMES_HOME-awareness)."""
    env_py = tmp_path / "custom-home" / "hermes-agent" / "venv" / "bin" / "python3"
    env_py.parent.mkdir(parents=True)
    env_py.write_text("")
    env_py.chmod(0o755)
    home_py = tmp_path / "home" / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
    home_py.parent.mkdir(parents=True)
    home_py.write_text("")
    home_py.chmod(0o755)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "custom-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    probe = FakeProbe(ok_paths={str(env_py), str(home_py)})
    monkeypatch.setattr(lifecycle.subprocess, "run", probe)
    assert lifecycle.resolve_hermes_python() == str(env_py)


def test_resolve_returns_none_when_nothing_can_host_runtime(monkeypatch, tmp_path):
    probe = FakeProbe(ok_paths=())  # everything fails the import probe
    monkeypatch.setattr(lifecycle.subprocess, "run", probe)
    assert lifecycle.resolve_hermes_python() is None


# --------------------------------------------------------------------------- #
# (a2) delegation chosen when THIS interpreter lacks the runtime
# --------------------------------------------------------------------------- #

@pytest.fixture
def brew_venv_simkey(monkeypatch):
    """Simulate the brew keg interpreter: it cannot import the Hermes runtime,
    and the hermes venv interpreter resolves to a fabricated path."""
    monkeypatch.setattr(
        lifecycle, "_in_process_runtime",
        lambda: (None, None, "No module named 'hermes_cli'"))
    monkeypatch.setattr(
        lifecycle, "resolve_hermes_python",
        lambda: "/fabricated/hermes-agent/venv/bin/python")


def _op_argv(runner_calls):
    """Decode (python, -c, script, argv-json) captured by a runner seam."""
    cmd = runner_calls[-1]
    return cmd[0], cmd[2], json.loads(cmd[3])


def test_open_delegates_to_hermes_venv_when_runtime_missing(brew_venv_simkey, monkeypatch):
    calls = []

    def fake_proc(cmd, rc=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

    def runner(cmd, cwd):
        calls.append(list(cmd))
        # First call is the open_check the opener makes to mirror the
        # in-process None semantics; answer success.
        return fake_proc(cmd, 0, json.dumps({"ok": True, "result": True}))

    monkeypatch.setattr(lifecycle, "_remote_default_run", runner)
    db = lifecycle._open_profile_session_db("probe-orch")
    assert isinstance(db, lifecycle.RemoteSessionHandle)
    assert db.read_only is False
    python, script, args = _op_argv(calls)
    assert python == "/fabricated/hermes-agent/venv/bin/python"
    assert args == {"op": "open_check", "profile": "probe-orch",
                    "read_only": False, "create_db": False}
    assert "from hermes_cli import profiles" in script  # runs HERMES' packages


def test_remote_handle_calls_are_ops_under_the_hermes_venv(brew_venv_simkey, monkeypatch):
    calls = []

    def runner(cmd, cwd):
        calls.append(list(cmd))
        args = json.loads(cmd[3])
        if args["op"] == "resolve_by_title":
            return subprocess.CompletedProcess(cmd, 0,
                                               json.dumps({"ok": True, "result": "20261026_first_abc123"}),
                                               "")
        return subprocess.CompletedProcess(cmd, 0,
                                           json.dumps({"ok": True, "result": True}), "")

    monkeypatch.setattr(lifecycle, "_remote_default_run", runner)
    db = lifecycle.RemoteSessionHandle("probe-orch", read_only=True)
    assert db.resolve_session_by_title("probe") == "20261026_first_abc123"
    _python, _script, args = _op_argv(calls)
    assert args == {"op": "resolve_by_title", "profile": "probe-orch",
                    "read_only": True, "title": "probe"}
    db.close()
    with pytest.raises(RuntimeError):
        db.resolve_session_by_title("probe")


def test_ensure_session_delegates_atomic_create_plus_title(brew_venv_simkey, monkeypatch):
    """The whole create+title lands in ONE remote op (no untitled-row leak),
    and it carries create_db=True — a profile created moments ago legitimately
    has no state.db yet (hermes itself creates it on the profile's first run)."""
    calls = []

    def runner(cmd, cwd):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(
            cmd, 0,
            json.dumps({"ok": True,
                        "result": {"status": "created",
                                   "session": "20261026_first_abc123",
                                   "title": "probe"}}),
            "")

    monkeypatch.setattr(lifecycle, "_remote_default_run", runner)
    out = lifecycle.ensure_session("probe", "probe-orch")
    assert out == {"status": "created", "session": "20261026_first_abc123",
                   "title": "probe"}
    assert len(calls) == 1  # ONE op — create+title are atomic
    _python, _script, args = _op_argv(calls)
    assert args == {"op": "ensure_session", "profile": "probe-orch",
                    "title": "probe", "create_db": True}


def test_no_runtime_is_environment_fault_not_missing_data(brew_venv_simkey, monkeypatch):
    """With the runtime unimportable AND no interpreter that can host it, the
    honest environment fault surfaces — never 'profile has no state.db'."""
    monkeypatch.setattr(lifecycle, "resolve_hermes_python", lambda: None)
    with pytest.raises(HermesRuntimeUnavailable) as ei:
        lifecycle._open_profile_session_db("probe-orch")
    msg = str(ei.value)
    assert "cannot import the Hermes runtime" in msg
    assert "No module named 'hermes_cli'" in msg  # names the missing module
    assert "probe-orch" not in msg  # never dressed up as profile data


def test_no_runtime_environment_fault_survives_ensure_session(brew_venv_simkey, monkeypatch):
    monkeypatch.setattr(lifecycle, "resolve_hermes_python", lambda: None)
    with pytest.raises(HermesRuntimeUnavailable):
        lifecycle.ensure_session("probe", "probe-orch")


def test_no_db_remote_is_data_fact_none_not_env_fault(brew_venv_simkey, monkeypatch):
    """Remote no_db classification maps to None — the same data fact the
    in-process opener returns when the profile has no state.db."""
    def runner(cmd, cwd):
        return subprocess.CompletedProcess(
            cmd, 0,
            json.dumps({"ok": False, "code": "no_db",
                        "error": "profile 'probe-orch' has no state.db"}),
            "")
    monkeypatch.setattr(lifecycle, "_remote_default_run", runner)
    assert lifecycle._open_profile_session_db("probe-orch") is None


def test_remote_look_failure_raises_never_silent(brew_venv_simkey, monkeypatch):
    """A failed LOOK is never a false negative (e2a3251 rule): op_error/
    transport failures raise through the handle."""
    state = {"n": 0}

    def runner(cmd, cwd):
        state["n"] += 1
        if state["n"] == 1:  # open_check succeeds
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"ok": True, "result": True}), "")
        return subprocess.CompletedProcess(cmd, 0, "", "boom")

    monkeypatch.setattr(lifecycle, "_remote_default_run", runner)
    db = lifecycle._open_profile_session_db("probe-orch", read_only=True)
    with pytest.raises(RemoteRuntimeError) as ei:
        db.list_sessions_rich(source="telegram", limit=5000)
    assert ei.value.code == "transport"


def test_remote_no_runtime_becomes_env_fault(brew_venv_simkey, monkeypatch):
    def runner(cmd, cwd):
        return subprocess.CompletedProcess(
            cmd, 0,
            json.dumps({"ok": False, "code": "no_runtime",
                        "error": "No module named 'hermes_state'"}), "")
    monkeypatch.setattr(lifecycle, "_remote_default_run", runner)
    with pytest.raises(HermesRuntimeUnavailable):
        lifecycle._remote_runtime_op("open_check", {"profile": "p"})


def test_in_process_path_unchanged_when_runtime_importable(monkeypatch):
    """A hermes-venv interpreter keeps the ORIGINAL in-process semantics: the
    opener is resolved through hermes_cli.profiles directly, no subprocess."""
    opened = {}

    class Mod:
        @staticmethod
        def normalize_profile_name(n):
            return n

        @staticmethod
        def validate_profile_name(n):
            return None

        @staticmethod
        def profile_exists(n):
            return True

        @staticmethod
        def get_profile_dir(n):
            return Path("/fake/profiles") / n

    class DB:
        def __init__(self, db_path=None, read_only=False):
            opened["db_path"] = db_path
            opened["read_only"] = read_only

        def close(self):
            opened["closed"] = True

    monkeypatch.setattr(lifecycle, "_in_process_runtime",
                        lambda: (Mod, DB, None))
    real_exists = Path.exists

    def fake_exists(self):
        return real_exists(self) or self.name == "state.db"

    monkeypatch.setattr(Path, "exists", fake_exists)
    db = lifecycle._open_profile_session_db("proj-orch", read_only=True)
    assert isinstance(db, DB)
    assert opened["read_only"] is True
    assert str(opened["db_path"]).endswith("proj-orch/state.db")


# --------------------------------------------------------------------------- #
# The wire protocol, executed for real against a FABRICATED agent tree
# --------------------------------------------------------------------------- #

@pytest.fixture
def fabricated_hermes(monkeypatch, tmp_path):
    """A fake Hermes install under tmp_path:

      agent/                 the "source tree" (HERMES_AGENT_PATH)
        hermes_cli/          profiles module with in-memory state
        hermes_state.py      SessionDB stand-in (create/title/append)
      home/.hermes/hermes-agent/venv/bin/python3
                             a #!-wrapper that re-execs the test interpreter
                             with -S -E (no site, no PYTHONPATH — so ONLY the
                             fabricated tree is importable) and honours the
                             ``-c code args`` convention.

    resolve_hermes_python() finds it through the real candidate scan and the
    real import probe — the whole delegation chain runs, end to end, with no
    operator ~/.hermes in sight. The parent side is simkeyed (via
    _in_process_runtime) as a brew-keg interpreter: it cannot import the
    runtime, so every op goes through the planted child.
    """
    agent = tmp_path / "agent"
    (agent / "hermes_cli").mkdir(parents=True)
    (agent / "hermes_cli" / "__init__.py").write_text("")
    (agent / "hermes_cli" / "profiles.py").write_text(
        "import json, os, pathlib\n"
        "def _state_file():\n"
        "    return os.path.join(os.environ['FAKE_HERMES_HOME'], 'state.json')\n"
        "def _load():\n"
        "    p = _state_file()\n"
        "    return json.loads(open(p).read()) if os.path.exists(p) else {}\n"
        "def normalize_profile_name(n):\n"
        "    return str(n).strip().lower()\n"
        "def validate_profile_name(n):\n"
        "    n = str(n)\n"
        "    if not n or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-.' for c in n):\n"
        "        raise ValueError('invalid profile name %r' % n)\n"
        "def profile_exists(n):\n"
        "    return normalize_profile_name(n) in _load()\n"
        "def get_profile_dir(n):\n"
        "    return pathlib.Path(os.environ['FAKE_HERMES_HOME']) / 'profiles' / normalize_profile_name(n)\n")
    (agent / "hermes_state.py").write_text(
        "import json, os, pathlib\n"
        "class SessionDB:\n"
        "    def __init__(self, db_path=None, read_only=False):\n"
        "        self.path = pathlib.Path(db_path)\n"
        "        self.read_only = read_only\n"
        "        self._data = json.loads(self.path.read_text()) if self.path.exists() else {'sessions': {}, 'titles': {}}\n"
        "    def resolve_session_by_title(self, title):\n"
        "        return self._data['titles'].get(title)\n"
        "    def create_session(self, sid, source='cli', **kw):\n"
        "        self._data['sessions'][sid] = {'source': source}\n"
        "        return sid\n"
        "    def set_session_title(self, sid, title):\n"
        "        self._data['titles'][title] = sid\n"
        "        return True\n"
        "    def append_message(self, sid, role, content, **kw):\n"
        "        self._data.setdefault('messages', {}).setdefault(sid, []).append([role, content])\n"
        "        return 1\n"
        "    def get_session(self, sid):\n"
        "        return self._data['sessions'].get(sid)\n"
        "    def list_sessions_rich(self, source=None, limit=20):\n"
        "        rows = [{'id': s} for s in self._data['sessions']]\n"
        "        if source:\n"
        "            rows = [r for r in rows if self._data['sessions'][r['id']]['source'] == source]\n"
        "        return rows[:limit]\n"
        "    def message_count(self, sid=None):\n"
        "        if sid is None:\n"
        "            return sum(len(v) for v in self._data.get('messages', {}).values())\n"
        "        return len(self._data.get('messages', {}).get(sid, []))\n"
        "    def get_messages(self, sid):\n"
        "        return [{'role': r, 'content': c} for r, c in self._data.get('messages', {}).get(sid, [])]\n"
        "    def get_session_title(self, sid):\n"
        "        for t, s in self._data['titles'].items():\n"
        "            if s == sid:\n"
        "                return t\n"
        "        return None\n"
        "    def close(self):\n"
        "        self.path.parent.mkdir(parents=True, exist_ok=True)\n"
        "        self.path.write_text(json.dumps(self._data))\n")

    # The planted "venv python": re-exec the test interpreter fully isolated
    # (-S -E: no site-packages, no PYTHONPATH — the REAL hermes_cli of the
    # test runner's own venv must NOT leak into the child), then run the
    # given -c code with the fabricated agent tree first on sys.path. The
    # isolation is via os.execv (the shebang cannot carry two flags on mac).
    fake_home = tmp_path / "fake-hermes-home"
    venv_bin = tmp_path / "home" / ".hermes" / "hermes-agent" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python3"
    py.write_text(
        "#!" + sys.executable + "\n"
        "import os, sys\n"
        "if '--fabricated-child' not in sys.argv:\n"
        "    os.execv(sys.executable, [sys.executable, '-S', '-E', __file__,\n"
        "                              '--fabricated-child'] + sys.argv[1:])\n"
        "argv = [a for a in sys.argv[1:] if a != '--fabricated-child']\n"
        "tree = os.environ.get('HERMES_AGENT_PATH', '')\n"
        "if tree and os.path.isdir(tree) and tree not in sys.path:\n"
        "    sys.path.insert(0, tree)\n"
        "if len(argv) >= 2 and argv[0] == '-c':\n"
        "    sys.argv = ['<deleg-child>'] + argv[2:]\n"
        "    exec(compile(argv[1], '<deleg-child>', 'exec'),\n"
        "         {'__name__': '__main__', '__file__': '<deleg-child>'})\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit('fabricated venv python: only -c is supported')\n")
    py.chmod(0o755)

    monkeypatch.setenv("HERMES_AGENT_PATH", str(agent))
    monkeypatch.setenv("FAKE_HERMES_HOME", str(fake_home))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home" / ".hermes"))
    (fake_home).mkdir(parents=True, exist_ok=True)
    (fake_home / "state.json").write_text("{}")
    # Simulate the brew keg on the PARENT side: this interpreter cannot host
    # the runtime, so the opener takes the delegation branch for real.
    monkeypatch.setattr(
        lifecycle, "_in_process_runtime",
        lambda: (None, None, "No module named 'hermes_cli'"))
    return {"agent": agent, "home": fake_home, "python": str(py),
            "profiles_file": fake_home / "state.json"}


def _make_profile(fabricated, name, with_db=True):
    """Create the fabricated profile dir + registry state (not via hermes)."""
    import json as _json
    state = _json.loads(fabricated["profiles_file"].read_text())
    state[name] = {"created": True}
    fabricated["profiles_file"].write_text(_json.dumps(state))
    pdir = fabricated["home"] / "profiles" / name
    pdir.mkdir(parents=True, exist_ok=True)
    if with_db:
        (pdir / "state.db").write_text('{"sessions": {}, "titles": {}}')
    return pdir


def test_fabricated_resolution_finds_the_planted_venv(fabricated_hermes):
    assert lifecycle.resolve_hermes_python() == fabricated_hermes["python"]


def test_remote_ops_run_end_to_end_under_fabricated_venv(fabricated_hermes):
    """The real protocol: parent serialises an op, the planted interpreter
    executes it against the fabricated tree, JSON comes back."""
    _make_profile(fabricated_hermes, "probe-orch")
    assert lifecycle._remote_runtime_op(
        "profile_exists", {"profile": "probe-orch"}) is True
    assert lifecycle._remote_runtime_op(
        "profile_exists", {"profile": "absent-orch"}) is False

    db = lifecycle._open_profile_session_db("probe-orch", read_only=True)
    assert isinstance(db, lifecycle.RemoteSessionHandle)
    assert db.resolve_session_by_title("nothing") is None
    db.append_message("s1", "user", "hello")
    assert db.message_count("s1") == 1
    assert db.get_session_title("s1") is None
    db.close()


def test_ensure_session_over_real_delegation_creates_then_exists(fabricated_hermes):
    """Idempotence (requirement c) over the REAL delegation transport: the
    first call creates + titles, the second reports exists, one row only."""
    pdir = _make_profile(fabricated_hermes, "probe-orch")
    first = lifecycle.ensure_session("probe", "probe-orch")
    assert first["status"] == "created"
    second = lifecycle.ensure_session("probe", "probe-orch")
    assert second == {"status": "exists"}
    db_file = pdir / "state.db"
    data = json.loads(db_file.read_text())
    assert data["titles"] == {"probe": first["session"]}
    assert list(data["sessions"]) == [first["session"]]  # exactly one row


def test_ensure_session_creates_state_db_for_fresh_profile(fabricated_hermes):
    """A freshly created orchestrator profile legitimately has NO state.db
    (hermes creates it on first run); ensure_session must still provision the
    session instead of reporting 'profile has no state.db'."""
    pdir = _make_profile(fabricated_hermes, "fresh-orch", with_db=False)
    out = lifecycle.ensure_session("freshproj", "fresh-orch")
    assert out["status"] == "created"
    assert (pdir / "state.db").exists()


def test_open_read_paths_still_report_no_db_for_fresh_profile(fabricated_hermes):
    """The flip side: a READ path (no create_db) still returns None when the
    db is absent — ensure-only semantics, never a read that creates files."""
    _make_profile(fabricated_hermes, "dbless-orch", with_db=False)
    assert lifecycle._open_profile_session_db("dbless-orch",
                                              read_only=True) is None


# --------------------------------------------------------------------------- #
# (b) atomicity: the stub profile never survives a failed session step
# --------------------------------------------------------------------------- #

# reuse the fakes from the neighbouring lifecycle suite
sys.path.insert(0, str(Path(__file__).parent))
from test_project_lifecycle import (  # noqa: E402
    FakeHermes,
    FakeKanban,
    FakeRunner,
    FakeSessionDB,
    _db_provider,
)


def _args(tmp_path, name="probe"):
    return {
        "name": name,
        "repo": str(tmp_path / "repo"),
        "registry_path": str(tmp_path / "registry.yaml"),
    }


def test_created_profile_rolled_back_when_session_step_fails(tmp_path):
    """The operator's repro: session step fails AFTER the stub profile was
    created — the profile must not survive as an orphan."""
    hermes = FakeHermes(exists=())
    results = lifecycle.create_project(
        **_args(tmp_path),
        _run=FakeRunner(hermes),
        _session_db=lambda profile: None,  # session step fails: no state.db
        _kanban=FakeKanban(),
    )
    steps = {s["id"]: s for s in results["steps"]}
    assert steps["session"]["status"] == "failed"
    assert steps["profile"]["status"] == "rolled_back"
    assert hermes.profile_delete_calls == ["probe-orch"]  # Hermes' own verb
    assert hermes._exists == []  # nothing left behind
    assert results["ok"] is False


def test_existing_profile_never_deleted_on_session_failure(tmp_path):
    hermes = FakeHermes(exists=("probe-orch",))
    results = lifecycle.create_project(
        **_args(tmp_path),
        _run=FakeRunner(hermes),
        _session_db=lambda profile: None,
        _kanban=FakeKanban(),
    )
    steps = {s["id"]: s for s in results["steps"]}
    assert steps["session"]["status"] == "failed"
    assert steps["profile"]["status"] == "skipped"
    assert hermes.profile_delete_calls == []  # not ours to roll back
    assert hermes._exists == ["probe-orch"]


def test_rollback_failure_is_reported_with_exact_command(tmp_path, monkeypatch):
    """If even the rollback fails, the report says so and names the exact
    cleanup command — never silently."""
    hermes = FakeHermes(exists=())
    monkeypatch.setattr(lifecycle, "delete_profile",
                        lambda name, _run=None: False)
    results = lifecycle.create_project(
        **_args(tmp_path),
        _run=FakeRunner(hermes),
        _session_db=lambda profile: None,
        _kanban=FakeKanban(),
    )
    steps = {s["id"]: s for s in results["steps"]}
    assert steps["profile"]["status"] == "ok"  # still there...
    cleanup = steps["profile-cleanup"]
    assert cleanup["status"] == "failed"
    assert "hermes profile delete probe-orch --yes" in cleanup["detail"]


def test_env_fault_triggers_rollback_and_names_the_fix(tmp_path, monkeypatch):
    """An unresolvable Hermes runtime during the session step: environment
    fault is REPORTED (not hidden), the stub profile is rolled back, and the
    detail names HSCC_HERMES_PYTHON."""
    hermes = FakeHermes(exists=())
    monkeypatch.setattr(lifecycle, "_in_process_runtime",
                        lambda: (None, None, "No module named 'httpx'"))
    monkeypatch.setattr(lifecycle, "resolve_hermes_python", lambda: None)
    results = lifecycle.create_project(
        **_args(tmp_path),
        _run=FakeRunner(hermes),
        _kanban=FakeKanban(),
    )
    steps = {s["id"]: s for s in results["steps"]}
    assert steps["session"]["status"] == "failed"
    assert "HSCC_HERMES_PYTHON" in steps["session"]["detail"]
    assert "No module named 'httpx'" in steps["session"]["detail"]
    assert steps["profile"]["status"] == "rolled_back"
    assert hermes.profile_delete_calls == ["probe-orch"]


def test_registry_write_failure_is_a_reported_step_not_a_crash(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("registry disk on fire")

    monkeypatch.setattr(lifecycle, "ensure_registry", boom)
    results = lifecycle.create_project(
        **_args(tmp_path),
        _run=FakeRunner(FakeHermes(exists=())),
        _session_db=_db_provider(FakeSessionDB()),
        _kanban=FakeKanban(),
    )
    steps = {s["id"]: s for s in results["steps"]}
    assert steps["registry"]["status"] == "failed"
    assert "registry disk on fire" in steps["registry"]["detail"]
    assert results["ok"] is False
    assert results["retry"]  # the retry command is offered


# --------------------------------------------------------------------------- #
# (c) retry idempotence
# --------------------------------------------------------------------------- #

def test_rerun_after_rollback_is_fully_idempotent(tmp_path):
    """First run: profile created, session fails, profile rolled back.
    Second run (state fixed): profile recreated, session ensured ONCE,
    registry written — no duplicate profile, no duplicate session."""
    hermes = FakeHermes(exists=())
    shared_db = FakeSessionDB()
    first = lifecycle.create_project(
        **_args(tmp_path),
        _run=FakeRunner(hermes),
        _session_db=lambda profile: None,
        _kanban=FakeKanban(),
    )
    assert first["ok"] is False
    assert hermes.profile_create_calls == ["probe-orch"]
    assert hermes.profile_delete_calls == ["probe-orch"]

    second = lifecycle.create_project(
        **_args(tmp_path),
        _run=FakeRunner(hermes),
        _session_db=_db_provider(shared_db),
        _kanban=FakeKanban(),
    )
    assert second["ok"] is True, second["steps"]
    # The rolled-back profile is gone from FakeHermes state, so the retry
    # recreates it — exactly once — and it is NOT then deleted.
    assert hermes.profile_create_calls == ["probe-orch", "probe-orch"]
    assert hermes.profile_delete_calls == ["probe-orch"]
    assert len(shared_db._ids) == 1  # session created once, not twice

    # Third run on converged state: every mutating step is a no-op.
    third = lifecycle.create_project(
        **_args(tmp_path),
        _run=FakeRunner(hermes),
        _session_db=_db_provider(shared_db),
        _kanban=FakeKanban(),
    )
    assert third["ok"] is True
    assert hermes.profile_create_calls == ["probe-orch", "probe-orch"]
    assert len(shared_db._ids) == 1


def test_delete_profile_uses_hermes_own_verb(tmp_path):
    hermes = FakeHermes(exists=("probe-orch",))
    ok = lifecycle.delete_profile("probe", _run=FakeRunner(hermes))
    assert ok is True
    assert hermes.profile_delete_calls == ["probe-orch"]


def test_delete_profile_reports_failure(tmp_path):
    hermes = FakeHermes(exists=("probe-orch",))

    def failing(cmd, cwd):
        if cmd[:3] == ["hermes", "profile", "delete"]:
            return subprocess.CompletedProcess(cmd, 1, "", "gateway is live")
        return FakeRunner(hermes)(cmd, cwd)

    assert lifecycle.delete_profile("probe", _run=failing) is False
