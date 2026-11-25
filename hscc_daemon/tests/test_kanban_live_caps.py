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


def test_load_hermes_config_resolves_the_agent_tree_then_closes_the_window(
        tmp_path, monkeypatch):
    """THE t_8eafef82 expectation change — read this before "fixing" it.

    The pre-fix version of this test asserted ``str(agent) in sys.path`` AFTER
    the call, i.e. it *pinned the leak*: ``_load_hermes_config`` put the agent
    tree at ``sys.path[0]`` and left it there for the life of the process. That
    assertion was the test-side half of the trap that made one planted fake
    ``hermes_cli`` poison a whole pytest run
    (docs/audits/orch-waker-import-leak_t_2be3b04e.md §3, §6), so it is
    deliberately INVERTED here, not relaxed:

      * the read must still come from the planted tree (``__file__`` inside it —
        the gate only serves that tree, so this also proves the window really
        opened: with no window the import could not have resolved at all);
      * ``sys.path`` must be exactly what it was afterwards, on a successful
        resolve;
      * and a repeat call must still leave it unchanged (the pre-fix code
        passed that one, which is why the leak survived it).
    """
    agent = tmp_path / "hermes-agent"
    with _HermesCliGate(agent):
        monkeypatch.setenv("HERMES_AGENT_PATH", str(agent))
        before = [p for p in sys.path
                  if os.path.realpath(p) != os.path.realpath(str(agent))]
        sys.path[:] = before
        cfg = kanban_live._load_hermes_config()
        assert (cfg.get("kanban") or {}).get("max_in_progress") == PLANTED_CAPS[0]
        # The window opened: the module the gate allowed us to import lives
        # inside the planted tree, and the gate serves nothing else.
        import hermes_cli.config as _planted
        assert os.path.realpath(_planted.__file__).startswith(
            os.path.realpath(str(agent)) + os.sep)
        # …and it closed: no residue, same entries, same order.
        assert str(agent) not in sys.path
        assert sys.path == before
        kanban_live._load_hermes_config()          # second call must not duplicate
        assert sys.path == before


def test_load_hermes_config_leaves_no_trace_when_the_import_fails(
        tmp_path, monkeypatch):
    """The restore is not on the happy path only — a raising import must also
    leave ``sys.path`` exactly as it found it (the ``finally`` is the point)."""
    agent = tmp_path / "hermes-agent"
    with _HermesCliGate(agent, block=True):
        monkeypatch.setenv("HERMES_AGENT_PATH", str(agent))
        before = list(sys.path)
        with pytest.raises(ModuleNotFoundError):
            kanban_live._load_hermes_config()
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


# ── the two monitor doors must not create $HSCC_HOME/daemon.log ─────────────
#
# t_4dcb34e6 §3 (docs/audits/latent-read-warns_t_4dcb34e6.md): ``collect`` reads the
# caps config, and a config it cannot read is reported by appending a WARN to
# ``$HSCC_HOME/daemon.log`` (``_note_caps_unreadable`` -> ``_caps_warn_route`` ->
# ``log``). ``hscc kanban monitor`` and ``hscc cards monitor`` both advertise a
# read-only view, so that write is the invariant t_88abe739 cares about — and it is
# LATENT: a sandbox that plants no broken Hermes config never reaches the branch,
# which is why the doctor door guard is blind to it. So the condition is PLANTED
# here, and the presence control below is what proves it fired (the #704 hole).
#
# Recipe: hscc-cluster/tests/test_doctor_tool.py::_seal_door_env, as worked by
# test_node_pauses.py::TestPausedDoorWritesNoFile and
# test_board_pin.py::TestReadDoorWritesNoLogFile.

_TASK_COLS = ("id", "title", "assignee", "status", "priority", "created_at",
              "completed_at", "last_heartbeat_at", "current_run_id")


class _DoorLib:
    """A kanban lib with one readable board, so ``collect`` runs a real pass.

    Not a stub of the caps read — only of hermes' lib loader, which is what the
    door itself calls. ``collect`` bails out BEFORE ``_read_caps`` when the loader
    returns None (``kanban_live.py:384-389``), so without this injected lib the
    guard would pass without ever touching the latent branch. No
    ``kanban_db_path``: ``_open_board_ro`` then falls back to
    ``board_pin.open_board``, which is the documented read door for the pin guard.
    """

    def __init__(self, slug="alpha"):
        import sqlite3          # module-local, as _alpha_board below does
        self.slug = slug
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
        CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT,
          status TEXT, priority INTEGER DEFAULT 0, created_at INTEGER,
          completed_at INTEGER, last_heartbeat_at INTEGER, current_run_id INTEGER);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT,
          status TEXT, started_at INTEGER, last_heartbeat_at INTEGER,
          ended_at INTEGER, outcome TEXT);
        CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
          run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
        """)
        conn.execute("INSERT INTO tasks (id,title,assignee,status,created_at) "
                     "VALUES ('t_rdy','waits','qa','ready',1700000000)")
        conn.commit()

        class _Ctx:
            def __enter__(self):
                return conn

            def __exit__(self, *a):
                return False

        self._ctx = _Ctx

    def list_boards(self):
        return [{"slug": self.slug}]

    def connect_closing(self, board=None):
        return self._ctx()


def _tree(root):
    """name -> bytes for every file under ``root`` ({} when absent)."""
    from pathlib import Path
    p = Path(root)
    if not p.exists():
        return {}
    return {str(f.relative_to(p)): f.read_bytes()
            for f in sorted(p.rglob("*")) if f.is_file()}


@pytest.fixture
def door_env(tmp_path, monkeypatch):
    """Sandbox HOME + the import-anchored sinks, and the pin kept unarmed.

    ``LOG_FILE``/``PID_FILE`` are bound at import (``daemon_ops.py:17-19``), so a
    test that only moved HOME would assert about a file it cannot create while
    quietly appending to the OPERATOR's real log. ``LEDGER_PATH`` and
    ``HERMES_HOME`` are import-anchored the same way: left alone, this door would
    read the operator's session store and append a real orchestrator-ledger sample
    from a test that is supposed to touch nothing.
    """
    from types import SimpleNamespace
    import hscc_daemon.daemon_ops as daemon_ops
    from hscc_daemon import board_pin

    home = tmp_path / "fakehome"
    hscc = home / ".hscc"
    hscc.mkdir(parents=True)
    hermes = home / ".hermes"          # no state.db: the token read finds nothing
    hermes.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(daemon_ops, "LOG_FILE", str(hscc / "daemon.log"))
    monkeypatch.setattr(daemon_ops, "PID_FILE", str(hscc / "daemon.pid"))
    monkeypatch.setattr(kanban_live, "HERMES_HOME", str(hermes))
    monkeypatch.setattr(kanban_live, "LEDGER_PATH",
                        str(tmp_path / "ledger" / "kmon_orch_tokens.json"))
    # No worker-shell pin: the board-pin route is PR #737's subject
    # (test_board_pin.TestReadDoorWritesNoLogFile) and must not be able to satisfy
    # or defeat a presence control that is about the CAPS route.
    monkeypatch.delenv(board_pin.PIN_ENV, raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    # Per-process dedupe + the caps budget: a stale one from another test would
    # let the WARN through silently, or swallow it before this door runs.
    monkeypatch.setattr(kanban_live, "_MUTED_ECHOED", set())
    monkeypatch.setattr(board_pin, "_MUTED_ECHOED", set())
    monkeypatch.setattr(board_pin, "_WARNED", set())
    kanban_live._CAPS_READ.update(reason="", warned=False)
    return SimpleNamespace(home=home, hscc=hscc, log=hscc / "daemon.log",
                           ledger=tmp_path / "ledger" / "kmon_orch_tokens.json")


@pytest.fixture
def unreadable_caps(monkeypatch, door_env):
    """PLANT the latent condition, both halves, and prove the read still runs.

    ``_load_hermes_config`` raising is the host state the audit describes (a
    Hermes tree that fails at import time). It is not enough on its own:
    ``collect`` calls ``autodown._load_kanban_db_or_default()`` first and returns
    early when hermes' lib is unreachable, so the caps read is never reached —
    the #704 hole in guard form. The loader therefore also gets a real lib, and
    ``registered_board_names`` keeps the board in the enumeration the door asks
    for (``registered_only=True``).
    """
    from types import SimpleNamespace
    from hscc_daemon import autodown

    boom = RuntimeError("config.yaml is unreadable: simulated disk fault")

    def _load_hermes_config():
        raise boom

    monkeypatch.setattr(kanban_live, "_load_hermes_config", _load_hermes_config)
    lib = _DoorLib()
    monkeypatch.setattr(autodown, "_load_kanban_db_or_default", lambda: lib)
    monkeypatch.setattr(autodown, "registered_board_names",
                        lambda registry_path=None: ({"alpha"}, None))
    return SimpleNamespace(lib=lib, reason=str(boom))


class TestMonitorDoorWritesNoLogFile:
    """The read doors advertise, guarded against the doors themselves."""

    def test_kanban_monitor_json_creates_no_log_file(self, door_env, unreadable_caps,
                                                     capsys):
        from hscc_daemon import kanban_cli
        import json as _json
        before = _tree(door_env.home)

        # The DOCUMENTED verb, not a hand-assembled ``_cmd_monitor(["--json"], …)``:
        # ``cmd_kanban`` strips ``--json`` from argv (kanban_cli.py:446-448) before
        # the handler's own parser sees it, so passing it down as ``rest`` dies in
        # "unknown argument" and the door never reads — a red that proves nothing
        # and, worse, a guard that would pass vacuously on the un-muted code too.
        rc = kanban_cli.cmd_kanban(["monitor", "--json"])
        captured = capsys.readouterr()

        assert rc == 0, captured.out
        payload = _json.loads(captured.out)
        # The monitor still renders, and it still reports blank caps — the payload
        # cannot tell "no cap configured" from "unreadable", which is exactly why
        # the WARN is the fact's only record and must go to stderr, not vanish.
        assert payload["caps"] == [None, None], payload["caps"]
        assert [b["name"] for b in payload["boards"]] == ["alpha"], payload["boards"]
        # Presence control: the latent branch really ran (the #704 hole).
        assert "fleet caps unreadable" in captured.err, captured.err
        assert unreadable_caps.reason in captured.err, captured.err
        assert "simulated disk fault" in captured.err, captured.err
        assert not door_env.ledger.exists(), (
            "the read touched the orchestrator ledger outside its own sandbox")
        assert not door_env.log.exists(), (
            "hscc kanban monitor created $HSCC_HOME/daemon.log during a read")
        assert _tree(door_env.home) == before, (
            f"a read wrote into the sandbox: "
            f"{sorted(set(_tree(door_env.home)) ^ set(before))}")
        (door_env.hscc / "canary.json").write_text("{}", encoding="utf-8")
        assert _tree(door_env.home) != before, (
            "the snapshot cannot see a write — the read-only claim proves nothing")

    def test_the_budget_and_the_reason_survive_the_read(self, door_env,
                                                        unreadable_caps, capsys):
        """The door spends no alarm and overwrites no finding.

        ``_note_caps_unreadable`` sets both slots *before* it emits, so a mute that
        handed back only the route would leave the process believing the caps
        warning had been delivered. Seeding a reason from an earlier real read is
        what makes restoring ``["reason"]`` load-bearing: a ``finally`` that reset
        the dict (or left the door's own reason in it) fails this, and the next
        render in the same process would then claim "the operator never set a cap"
        — the different fact ``kanban_live.py:140-142`` warns about.
        """
        from hscc_daemon import kanban_cli
        kanban_live._CAPS_READ.update(reason="earlier real read", warned=True)

        assert kanban_cli.cmd_kanban(["monitor", "--json"]) == 0
        capsys.readouterr()

        assert kanban_live._CAPS_READ == {"reason": "earlier real read",
                                          "warned": True}, kanban_live._CAPS_READ

    def test_the_caps_route_is_given_back_after_the_read(self, door_env,
                                                         unreadable_caps, capsys):
        """Post-window, the same WARN writes the file again: borrowed, not installed.

        Both halves of the ``finally`` are proved here. If the route were left
        swapped the file would stay empty; if only the route were restored and the
        ``warned`` budget left spent, the second read would stay silent and the
        operator's own log would never learn the caps are unreadable.
        """
        from hscc_daemon import kanban_cli
        assert kanban_cli.cmd_kanban(["monitor", "--json"]) == 0
        capsys.readouterr()
        assert not door_env.log.exists()

        kanban_live.collect(kanban_db=unreadable_caps.lib)     # outside a window

        assert door_env.log.exists(), (
            "the monitor never gave the caps WARN route back")
        text = door_env.log.read_text(encoding="utf-8")
        # Named, not merely "some line": a board-pin or unrelated WARN writing the
        # file must not be able to satisfy this.
        assert "fleet caps unreadable" in text, text
        assert unreadable_caps.reason in text, text

    def test_the_cards_monitor_collector_creates_no_log_file(self, door_env,
                                                             unreadable_caps,
                                                             capsys):
        """The second door: ``hscc cards monitor`` reads through the same collector.

        Driven at ``Monitor._default_collect`` — the seam ``cmd_cards`` reaches
        (cards_live.py:1056 <- hscc.py:2885) for both the snapshot and every
        ``--live`` tick. The presence control is asserted for THIS door, not
        inherited from the kanban one: each door owns its own mute.
        """
        from hscc_daemon import cards_live
        before = _tree(door_env.home)

        data = cards_live.Monitor()._default_collect()
        captured = capsys.readouterr()

        assert data["caps"] == (None, None), data["caps"]
        assert [b["name"] for b in data["boards"]] == ["alpha"], data["boards"]
        assert "fleet caps unreadable" in captured.err, captured.err
        assert not door_env.log.exists(), (
            "hscc cards monitor created $HSCC_HOME/daemon.log during a read")
        assert _tree(door_env.home) == before, (
            f"a read wrote into the sandbox: "
            f"{sorted(set(_tree(door_env.home)) ^ set(before))}")
