"""GAP-D-4 — ``_read_caps`` must not depend on the kanban loader having run.

The bug (audit D; measured under the brew keg venv, where ``hscc kanban
monitor`` actually runs and ``hermes_cli`` is NOT installed):

    OLD (origin/main)  FIRST-CALL caps  : (None, None)
    OLD (origin/main)  AFTER-LOADER caps: (12, 3)
    NEW (fixed)        FIRST-CALL caps  : (12, 3)

``_read_caps`` did a bare ``from hermes_cli.config import load_config`` inside
``except Exception``, so before ``autodown._load_kanban_db_or_default()`` put the
Hermes tree on ``sys.path``, the import raised ModuleNotFoundError and the broad
handler returned ``(None, None)`` — indistinguishable from "the operator
configured no caps".

Hermetic and non-vacuous, on three fronts:

* ``_HermesCliGate`` makes the planted tree the ONLY thing ``import hermes_cli``
  can resolve to (a ``sys.meta_path`` finder at position 0 — needed because this
  venv installs hermes editable through a ``.pth`` finder that outranks
  ``sys.path``), and ``block=True`` makes the import fail outright even with a
  perfectly good ``sys.path``. The blocker is what proves the assertions exercise
  the resolver instead of an accidental leftover on ``sys.path``.
* the planted caps (17/5) deliberately differ from this machine's real config
  (12/3), so reading the operator's config could not satisfy them.
* ``test_negative_control_the_gate_really_hides_hermes_cli`` asserts the blocker
  fires; without it, a suite that never reached the chain would pass vacuously.
"""

import os
import sys
from importlib.machinery import PathFinder

import pytest

from hscc_daemon import kanban_live

# Deliberately NOT this machine's real values (12/3) — see module docstring.
PLANTED_CAPS = (17, 5)


class _HermesCliGate:
    """Control what ``import hermes_cli`` can resolve to, for one test.

    ``serve``: the planted tree is importable ONLY through ``sys.path`` — the
    finder resolves with ``PathFinder`` against ``sys.path`` and refuses
    anything that would land outside the planted tree. That reproduces the keg
    venv faithfully (there ``hermes_cli`` exists only under the agent tree, so
    only a resolver insert can reach it) instead of the test venv, where an
    editable install is importable no matter what ``sys.path`` holds. Without
    this the pre-fix code passes by accident and the suite is vacuous — measured
    with a mutation probe against the original ``_read_caps`` body.
    ``block``: every ``hermes_cli`` import raises ModuleNotFoundError, which is
    how a genuine "Hermes is not reachable from this interpreter" failure is
    simulated even on a machine that has hermes installed.
    """

    def __init__(self, agent_dir=None, block=False, caps=PLANTED_CAPS, config_body=None):
        self.agent_dir = str(agent_dir) if agent_dir else None
        self.block = block
        if agent_dir is not None:
            cli = agent_dir / "hermes_cli"
            cli.mkdir(parents=True, exist_ok=True)
            (cli / "__init__.py").write_text("# planted\n")
            (cli / "config.py").write_text(
                config_body if config_body is not None else
                "def load_config():\n"
                f"    return {{'kanban': {{'max_in_progress': {caps[0]}, "
                f"'max_in_progress_per_profile': {caps[1]}}}}}\n")
        self._saved_modules = {}
        self._saved_path: list = list(sys.path)

    def __enter__(self):
        for name in [n for n in sys.modules if n == "hermes_cli"
                     or n.startswith("hermes_cli.")]:
            self._saved_modules[name] = sys.modules.pop(name)
        self._saved_path = list(sys.path)
        if self.agent_dir:
            # The planted tree must NOT already be on sys.path: reaching it is
            # the thing under test.
            sys.path[:] = [p for p in sys.path
                           if os.path.realpath(p) != os.path.realpath(self.agent_dir)]
        sys.meta_path.insert(0, self)
        return self

    def __exit__(self, *a):
        if self in sys.meta_path:
            sys.meta_path.remove(self)
        for name in [n for n in sys.modules if n == "hermes_cli"
                     or n.startswith("hermes_cli.")]:
            del sys.modules[name]
        sys.modules.update(self._saved_modules)
        sys.path[:] = self._saved_path
        return False

    def find_spec(self, name, path=None, target=None):
        if not (name == "hermes_cli" or name.startswith("hermes_cli.")):
            return None
        if self.block:
            raise ModuleNotFoundError(f"No module named {name!r} (hidden by test gate)")
        if name == "hermes_cli":
            # Top level: resolve through sys.path ONLY, exactly as a real
            # interpreter with hermes installed nowhere else would.
            spec = PathFinder.find_spec(name, path=sys.path)
        else:
            # Submodule: PathFinder wants the PARENT's __path__, which is itself
            # inside the planted tree because the parent was gated too — so this
            # stays sys.path-derived and cannot leak to the editable install.
            parent = sys.modules.get(name.rsplit(".", 1)[0])
            spec = PathFinder.find_spec(
                name, path=list(getattr(parent, "__path__", []) or []))
        origin = str(getattr(spec, "origin", "") or "") if spec else ""
        root = os.path.realpath(self.agent_dir or "") + os.sep
        if origin.startswith(root):
            return spec
        raise ModuleNotFoundError(
            f"No module named {name!r} (hidden by test gate: resolved to {origin!r})")


@pytest.fixture(autouse=True)
def _reset_caps_state():
    """``_CAPS_READ`` is process state; every test starts and ends clean."""
    kanban_live._CAPS_READ.update(reason="", warned=False)
    yield
    kanban_live._CAPS_READ.update(reason="", warned=False)


@pytest.fixture
def warn_lines(monkeypatch):
    """Capture the warn-once log instead of writing the daemon log."""
    lines = []
    monkeypatch.setattr(kanban_live, "log",
                        lambda msg, level="INFO", **kw: lines.append((level, msg)))
    return lines


# ── the acceptance case: caps read FIRST, loader never called ──────────────

def test_read_caps_is_correct_with_no_prior_loader_call(tmp_path, monkeypatch):
    """THE acceptance test: ``_read_caps()`` FIRST ⇒ the real caps.

    No ``autodown._load_kanban_db_or_default()`` anywhere in this test, and the
    gate means the old code's bare import could not have resolved. Pre-fix this
    returned ``(None, None)``.
    """
    agent = tmp_path / "hermes-agent"
    with _HermesCliGate(agent):
        monkeypatch.setenv("HERMES_AGENT_PATH", str(agent))
        assert kanban_live._read_caps() == PLANTED_CAPS
        # and it stays correct on repeat calls (deterministic, not order-dependent)
        assert kanban_live._read_caps() == PLANTED_CAPS
    assert kanban_live._CAPS_READ["reason"] == ""


def test_negative_control_the_gate_really_hides_hermes_cli(tmp_path, monkeypatch):
    """Non-vacuity: with the blocker in place the import genuinely fails.

    If this ever stops raising, the gate is not blocking anything and every
    "caps came from the planted tree" assertion above would be green for the
    wrong reason (the editable install leaking through ``sys.path``).
    """
    with _HermesCliGate(block=True):
        for name in [n for n in sys.modules if n == "hermes_cli"
                     or n.startswith("hermes_cli.")]:
            del sys.modules[name]
        with pytest.raises(ModuleNotFoundError):
            import hermes_cli.config  # noqa: F401
        # …and the resolver cannot rescue it either: that is the honest failure
        # mode the monitor now reports instead of swallowing.
        monkeypatch.setenv("HERMES_AGENT_PATH", str(tmp_path / "hermes-agent"))
        with pytest.raises(ImportError):
            kanban_live._load_hermes_config()


def test_collect_caps_do_not_depend_on_call_order(tmp_path, monkeypatch, _alpha_board):
    """The latent-fragility case from the card: caps read BEFORE the loader,
    then a collect() that runs the loader — both must agree."""
    agent = tmp_path / "hermes-agent"
    with _HermesCliGate(agent):
        monkeypatch.setenv("HERMES_AGENT_PATH", str(agent))
        first = kanban_live._read_caps()                      # loader never ran
        data = kanban_live.collect(kanban_db=_alpha_board, now=1_800_000_000)
        assert first == PLANTED_CAPS
        assert data["caps"] == PLANTED_CAPS
        # and still right afterwards
        assert kanban_live._read_caps() == PLANTED_CAPS


# ── a genuine read failure is now distinguishable from "no caps configured" ─

def test_genuine_read_failure_is_recorded_and_warned_once(monkeypatch, warn_lines):
    """Blank caps are no longer silent: the reason is recorded and logged ONCE
    (the monitor calls this every 5 s, so per-call logging would spam the log)."""
    with _HermesCliGate(block=True):
        monkeypatch.setenv("HERMES_AGENT_PATH", "/tmp/does-not-matter")
        for _ in range(3):
            assert kanban_live._read_caps() == (None, None)
    assert "hidden by test gate" in kanban_live._CAPS_READ["reason"]
    warns = [m for lvl, m in warn_lines if lvl == "WARN"]
    assert len(warns) == 1, f"expected one WARN for 3 calls, got {warns}"
    assert "fleet caps unreadable" in warns[0]


def test_unset_caps_are_not_reported_as_a_failure(tmp_path, monkeypatch, warn_lines):
    """``reason`` stays empty when the config read fine but declares no caps —
    'not configured' and 'could not read' must not collapse into one signal."""
    agent = tmp_path / "hermes-agent"
    with _HermesCliGate(agent, config_body="def load_config():\n    return {}\n"):
        monkeypatch.setenv("HERMES_AGENT_PATH", str(agent))
        assert kanban_live._read_caps() == (None, None)
    assert kanban_live._CAPS_READ["reason"] == ""
    assert warn_lines == []


def test_a_successful_read_clears_the_previous_failure(tmp_path, monkeypatch, warn_lines):
    """Mirrors autodown._note_prci_clear: after a recovery the recorded state
    must not keep claiming an unevaluable read."""
    agent = tmp_path / "hermes-agent"
    with _HermesCliGate(agent, block=True):
        assert kanban_live._read_caps() == (None, None)
        assert kanban_live._CAPS_READ["warned"] is True
    with _HermesCliGate(agent):
        monkeypatch.setenv("HERMES_AGENT_PATH", str(agent))
        assert kanban_live._read_caps() == PLANTED_CAPS
    assert kanban_live._CAPS_READ == {"reason": "", "warned": False}


def test_hermes_fail_open_fallback_is_flagged_not_silent(tmp_path, monkeypatch, warn_lines):
    """hermes serves a ``FailedConfigRead`` (dict subclass carrying
    ``read_error``) when config.yaml exists but cannot be read/parsed. Those
    caps are defaults or the last-known-good copy, so the monitor must say so
    while still rendering whatever values came back."""
    body = (
        "class _Fallback(dict):\n"
        "    def __init__(self, d, err):\n"
        "        super().__init__(d)\n"
        "        self.read_error = err\n"
        "def load_config():\n"
        "    return _Fallback({'kanban': {'max_in_progress': 17,"
        " 'max_in_progress_per_profile': 5}}, OSError('bad yaml'))\n")
    agent = tmp_path / "hermes-agent"
    with _HermesCliGate(agent, config_body=body):
        monkeypatch.setenv("HERMES_AGENT_PATH", str(agent))
        assert kanban_live._read_caps() == PLANTED_CAPS
    assert "fail-open" in kanban_live._CAPS_READ["reason"]
    assert "bad yaml" in kanban_live._CAPS_READ["reason"]
    assert len([m for lvl, m in warn_lines if lvl == "WARN"]) == 1


def test_malformed_cap_values_do_not_bring_the_monitor_down(tmp_path, monkeypatch):
    """A non-numeric cap is still None, not an exception — unchanged behaviour,
    pinned so the rewrite cannot regress it."""
    agent = tmp_path / "hermes-agent"
    with _HermesCliGate(agent, config_body=(
            "def load_config():\n"
            "    return {'kanban': {'max_in_progress': 'twelve'}}\n")):
        monkeypatch.setenv("HERMES_AGENT_PATH", str(agent))
        assert kanban_live._read_caps() == (None, None)
    assert kanban_live._CAPS_READ["reason"] == ""


def test_load_hermes_config_resolves_the_agent_tree_once(tmp_path, monkeypatch):
    """The resolver honours HERMES_AGENT_PATH (the same override autodown uses)
    and does not re-insert a path that is already present."""
    agent = tmp_path / "hermes-agent"
    with _HermesCliGate(agent):
        monkeypatch.setenv("HERMES_AGENT_PATH", str(agent))
        sys.path[:] = [p for p in sys.path
                       if os.path.realpath(p) != os.path.realpath(str(agent))]
        cfg = kanban_live._load_hermes_config()
        assert (cfg.get("kanban") or {}).get("max_in_progress") == PLANTED_CAPS[0]
        assert str(agent) in sys.path
        before = list(sys.path)
        kanban_live._load_hermes_config()          # second call must not duplicate
        assert sys.path == before


# ── helper for the collect-order test (same shape as test_kanban_live._FakeKb) ─

@pytest.fixture
def _alpha_board():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
      priority INTEGER DEFAULT 0, created_at INTEGER, completed_at INTEGER,
      last_heartbeat_at INTEGER, current_run_id INTEGER);
    CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT,
      status TEXT, started_at INTEGER, last_heartbeat_at INTEGER);
    CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
      run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
    """)

    class _Ctx:
        def __enter__(self):
            return conn

        def __exit__(self, *a):
            return False

    class _Lib:
        def list_boards(self):
            return [{"slug": "alpha"}]

        def connect_closing(self, board=None):
            return _Ctx()

    return _Lib()
