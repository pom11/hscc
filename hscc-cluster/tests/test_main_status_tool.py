"""Tests for the `main_status` agent tool (card t_c920b0ca).

The tool adds no policy and computes no identity: it asks the CLI's own verb
(`hscc main status --json`) and hands the payload over, plus the waker block
from the one function `hscc orch waker status` renders. So these tests are about
three claims, same shape as test_autodown_status_tool.py /
test_merge_queue_status_tool.py.

1. ONE DOOR. What the tool returns for the main-orchestrator fields is what
   `hscc main status --json` prints — key for key. A tool that re-derived a name,
   defaulted one or dropped one is the second source of truth the audit
   (docs/audits/2026-11-23-agent-surface.md §4/§5) exists to prevent.
2. THE RESOLVER OWNS THE NAME. The answer follows
   `hscc-roles/main_orch.py` (the same module t_9e3d868f's orch_message uses):
   an un-stored install follows its default, and a stored/renamed main is
   reported without an edit here. No profile name is baked into the module.
3. READ-ONLY. Nothing in this call may write `~/.hscc`, start/re-attach a tmux
   pane, rename the main orchestrator or edit its wake schedule. An agent that
   can spawn the operator's orchestrator is the lifecycle door this tool exists
   to withhold.

Hermetic, and it has to be: `hscc-cluster/tests` has no `~/.hscc` isolation of
its own, and the CLI verb this tool calls discovers live tmux panes when no fake
is injected. `_seal_state` redirects every `~/.hscc` / `~/.hermes` read
(expanduser at call time plus the waker's import-time constants) and pins
`STATE_DIR`; `_fake_repl` installs the pane/busy seam the CLI itself takes. The
guard-asserts below fail the suite if a seal silently stops applying, rather
than letting a green test decide from the operator's live orchestrator.
"""

import ast
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import main_status as mst
from hscc_daemon import main_cli, orch_waker
from hscc_daemon import state as daemon_state

#: Captured BEFORE any fixture runs: the seal patches `os.path.expanduser`, so
#: a guard asking it for "the live dir" afterwards would be handed the tmp dir.
_REAL_HSCC = os.path.expanduser("~/.hscc")
_REAL_HERMES = os.path.expanduser("~/.hermes")


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _snapshot(directory):
    """name -> bytes for every file under ``directory`` ({} when absent)."""
    root = Path(directory)
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


class _FakeRepl:
    """The two seams `main_cli.status` takes from `repl_inject`."""

    def __init__(self, panes=(), busy=False, busy_error=None):
        self.panes = list(panes)
        self.busy = busy
        self.busy_error = busy_error
        self.busy_calls = []

    def hermes_panes(self):
        return list(self.panes)

    def is_busy(self, pane):
        self.busy_calls.append(pane)
        if self.busy_error is not None:
            raise self.busy_error
        return self.busy


def _pane(profile, pane="%1", target="ops:0.0"):
    return {"profile": profile, "pane": pane, "target": target}


@pytest.fixture(autouse=True)
def _seal_state(tmp_path, monkeypatch):
    """Redirect every read a status touch performs to a per-test sandbox."""
    base = tmp_path / "hscc"
    state_dir = base / "state"
    base.mkdir(parents=True, exist_ok=True)
    real_expanduser = os.path.expanduser

    def _redirect(path):
        expanded = real_expanduser(path)
        for root, dest in ((_REAL_HSCC, base),
                           (_REAL_HERMES, tmp_path / "hermes")):
            if expanded == root:
                return str(dest)
            if expanded.startswith(root + os.sep):
                return str(dest / expanded[len(root) + 1:])
        return expanded

    # (a) call-time expanduser: every `~/.hscc/...` / `~/.hermes/...` resolves
    # inside the per-test dir, even from a helper that only knows the tilde form.
    monkeypatch.setattr(os.path, "expanduser", _redirect)
    # (b) the resolver's own env root + the profile tree it validates against.
    monkeypatch.setenv("HSCC_DIR", str(base))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    # (c) constants already baked at import time in the daemon modules.
    monkeypatch.setattr(daemon_state, "STATE_DIR", str(state_dir))
    monkeypatch.setattr(orch_waker, "DEFAULT_CONFIG_PATH",
                        str(base / "orch_waker.json"))
    monkeypatch.setattr(orch_waker, "DEFAULT_STATE_PATH",
                        str(state_dir / "orch_waker.json"))
    monkeypatch.setattr(orch_waker, "DEFAULT_AUDIT_PATH",
                        str(state_dir / "orch_waker.jsonl"))
    # (d) pane discovery/busy is a live tmux read in the real module. Every test
    # starts with an idle pane for the default profile; a test that wants another
    # shape re-installs its own fake.
    fake = _FakeRepl([_pane("general")])
    monkeypatch.setattr(main_cli, "load_repl_inject", lambda: fake)
    return SimpleNamespace(tmp=tmp_path, base=base, state_dir=state_dir,
                           repl=fake, monkeypatch=monkeypatch)


@pytest.fixture(autouse=True)
def _the_seal_is_actually_on(_seal_state):
    """Guard-assert the seal, so a broken fixture is red instead of a live read."""
    assert os.path.expanduser("~/.hscc") == str(_seal_state.base)
    assert daemon_state.STATE_DIR == str(_seal_state.state_dir)
    assert orch_waker.config_path().startswith(str(_seal_state.base) + os.sep)


def install_repl(monkeypatch, repl):
    monkeypatch.setattr(main_cli, "load_repl_inject", lambda: repl)
    return repl


def configure_main(base, name):
    return _write(base / "main-orchestrator.json", {"name": name})


def cli_json():
    """`hscc main status --json` — the operator's own bytes, in-process."""
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = main_cli.cmd_main(["status", "--json"])
    except SystemExit as exc:          # a verb must RETURN, never exit
        rc = exc.code if isinstance(exc.code, int) else 0
    return rc, buf.getvalue().strip()


def main_shape(out):
    """The main-orchestrator fields, i.e. the CLI payload without the extra block."""
    return {k: v for k, v in out.items() if k not in ("rc", "waker")}


# ── 1. one door: the main fields ARE the CLI's payload ──────────────────────
class TestSameStateAsTheCli:
    def test_the_main_payload_is_the_cli_bytes(self, _seal_state):
        """The whole design in one assertion: parse what the CLI printed and it
        must equal the tool's main fields, key for key, value for value."""
        rc, text = cli_json()
        out = mst.main_status({})
        assert rc == 0
        assert out["ok"] is True and out["rc"] == 0
        assert main_shape(out) == json.loads(text)

    def test_a_running_pane_is_live_shape_not_a_cached_copy(self, _seal_state):
        """Change the injected pane between calls: the answer must change too."""
        first = mst.main_status({})
        assert first["state"] == "idle" and first["pane"] == "%1"
        install_repl(_seal_state.monkeypatch,
                     _FakeRepl([_pane("general", "%9", "ops:1.0")], busy=True))
        second = mst.main_status({})
        assert second["state"] == "busy" and second["busy"] is True
        assert second["pane"] == "%9" and second["target"] == "ops:1.0"

    def test_the_envelope_shadows_no_field_of_the_payload(self, _seal_state):
        """The payload is spread in, so a name collision would silently win or
        lose a field. `_ENVELOPE` holds only what the tool adds."""
        rc, text = cli_json()
        payload = json.loads(text)
        assert not (set(payload) & set(mst._ENVELOPE)), (
            f"the CLI payload now carries "
            f"{sorted(set(payload) & set(mst._ENVELOPE))} — rename the envelope "
            "key or nest the payload; do not let one silently overwrite the other")

    def test_the_result_is_json_serialisable(self):
        out = mst.main_status({})
        assert isinstance(out, dict)
        assert isinstance(json.dumps(out, default=str), str)


# ── 2. the resolver owns the name; live shapes are honest ───────────────────
class TestIdentityAndLiveShape:
    def test_an_unstored_install_follows_the_resolver_default(self, _seal_state):
        """No state document: the tool reports what the resolver defaults to,
        and it must not be a literal copied into main_status.py."""
        out = mst.main_status({})
        assert out["name"] == "general"
        assert out["profile"] == "general" and out["board"] == "general"
        assert "general" not in inspect.getsource(mst)

    def test_a_stored_name_follows_the_resolver(self, _seal_state):
        configure_main(_seal_state.base, "ops")
        install_repl(_seal_state.monkeypatch,
                     _FakeRepl([_pane("ops", "%2", "s:0.1")]))
        out = mst.main_status({})
        assert (out["name"], out["profile"], out["board"]) == ("ops", "ops", "ops")
        assert out["pane"] == "%2" and out["running"] is True

    def test_a_rename_in_place_is_followed_without_an_edit_here(
            self, _seal_state):
        configure_main(_seal_state.base, "ops")
        install_repl(_seal_state.monkeypatch,
                     _FakeRepl([_pane("ops", "%2", "s:0.1")]))
        assert mst.main_status({})["profile"] == "ops"
        configure_main(_seal_state.base, "control")
        install_repl(_seal_state.monkeypatch,
                     _FakeRepl([_pane("control", "%3", "s:0.2")]))
        renamed = mst.main_status({})
        assert (renamed["name"], renamed["profile"]) == ("control", "control")
        assert renamed["pane"] == "%3"

    def test_no_pane_is_not_running(self, _seal_state):
        install_repl(_seal_state.monkeypatch, _FakeRepl([]))
        out = mst.main_status({})
        assert out["state"] == "not-running"
        assert out["running"] is False and out["pane"] is None

    def test_one_pane_is_idle_or_busy(self, _seal_state):
        idle = mst.main_status({})
        assert idle["state"] == "idle" and idle["busy"] is False
        install_repl(_seal_state.monkeypatch, _FakeRepl([_pane("general")], busy=True))
        busy = mst.main_status({})
        assert busy["state"] == "busy" and busy["busy"] is True

    def test_several_panes_are_refused_not_guessed(self, _seal_state):
        install_repl(_seal_state.monkeypatch, _FakeRepl([
            _pane("general", "%1", "s:0.0"),
            _pane("general", "%2", "s:0.1"),
        ]))
        out = mst.main_status({})
        assert out["state"] == "ambiguous"
        assert out["running"] is False and out["pane"] is None
        assert out["panes"] == ["s:0.0", "s:0.1"]

    def test_an_unreadable_pane_is_unknown_not_a_guess(self, _seal_state):
        install_repl(_seal_state.monkeypatch, _FakeRepl(
            [_pane("general")], busy_error=RuntimeError("cannot capture")))
        out = mst.main_status({})
        assert out["state"] == "unknown" and out["busy"] is None
        assert out["running"] is True


# ── 3. the waker half is the waker's own status ─────────────────────────────
class TestWakerState:
    def test_the_waker_block_comes_from_the_waker_status_function(self, _seal_state):
        _write(_seal_state.base / "orch_waker.json", {"enabled": False})
        _write(_seal_state.state_dir / "orch_waker.json", {
            "last": {"ops": "t_done"},
            "pending": {"ops": ["t_new"]},
            "last_tick": 1700000000.0,
            "ok": True,
            "message": "tick ok",
        })
        out = mst.main_status({})["waker"]
        assert out["ok"] is True
        assert out["enabled"] is False
        assert out["stream"]["last_tick"] == 1700000000.0
        assert out["boards"]["ops"] == {"last_id": "t_done", "pending": 1}

    def test_an_unavailable_waker_does_not_hide_the_main_answer(
            self, _seal_state, monkeypatch):
        monkeypatch.setattr(
            mst, "_waker_status",
            lambda: {"ok": False, "detail": "orch_waker status unavailable: boom"})
        out = mst.main_status({})
        assert out["name"] == "general" and out["state"] == "idle"
        assert out["waker"] == {"ok": False,
                                "detail": "orch_waker status unavailable: boom"}


# ── 4. read-only, across every door ─────────────────────────────────────────
class TestReadOnly:
    def test_a_status_read_writes_nothing(self, _seal_state):
        """Byte-for-byte: the whole sandbox is unchanged by the tool call."""
        configure_main(_seal_state.base, "ops")
        _write(_seal_state.base / "orch_waker.json", {"enabled": True})
        _write(_seal_state.state_dir / "orch_waker.json", {
            "last": {}, "pending": {}, "last_tick": 1700000000.0, "ok": True})
        install_repl(_seal_state.monkeypatch,
                     _FakeRepl([_pane("ops", "%2", "s:0.1")]))
        before = _snapshot(_seal_state.base)
        assert before, "the fixture must plant state, or this proves nothing"
        out = mst.main_status({})
        assert out["name"] == "ops" and out["waker"]["ok"] is True
        assert _snapshot(_seal_state.base) == before

    def test_the_cli_door_writes_nothing_either(self, _seal_state):
        """The parity comparison is only meaningful if the CLI door is inert."""
        configure_main(_seal_state.base, "ops")
        _write(_seal_state.base / "orch_waker.json", {"enabled": True})
        before = _snapshot(_seal_state.base)
        cli_json()
        assert _snapshot(_seal_state.base) == before

    def test_the_handler_has_no_path_to_a_lifecycle_mutation(self):
        """Structural pin: the module may ask the CLI's status verb and the
        waker's status function — never a start, rename or wakeup write."""
        src = inspect.getsource(mst)
        tree = ast.parse(src)
        touched = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        touched |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        banned = {
            # main lifecycle / identity writers
            "start", "wakeups", "store_name", "set_schedule", "set_enabled",
            # waker/daemon writers and builders a tool must not read directly
            "load_config", "save_config", "waker_tick", "run_tick",
            "cas_update", "write_state",
            # no transport, ever
            "subprocess", "run_tmux", "inject", "sparkrun", "ssh",
        }
        assert not (touched & banned), (
            f"{sorted(touched & banned)} in main_status.py means a second door "
            "to the main orchestrator's lifecycle — the handler must only read")
        assert "cmd_main" in touched, (
            "the handler no longer asks the CLI's own verb")
        assert "waker_status" in touched, (
            "the waker block no longer comes from the waker's status function")

    def test_no_main_lifecycle_tool_is_registered_anywhere(self):
        """`start`/`wakeups`/rename stay CLI/operator-only. Assert it at the
        registry, where a 'just add main_start' edit would land."""
        init = Path(inspect.getfile(mst)).parent / "__init__.py"
        src = init.read_text()
        for verb in ("main_start", "main_wakeups", "main_rename", "main_wake"):
            assert f'"{verb}"' not in src, (
                f"{verb} is a main-orchestrator lifecycle mutation — CLI only")
        assert '"main_status"' in src
        assert "(_READ_TOOLS + " in src, "_READ_TOOLS is not wired into register()"

    def test_the_plugin_manifest_declares_only_the_read(self):
        """`provides_tools` is what `hermes tools` advertises; the lifecycle
        twins must never be promised there."""
        import re
        path = Path(inspect.getfile(mst)).parent / "plugin.yaml"
        text = path.read_text()
        assert re.search(r"^\s+- main_status$", text, re.MULTILINE)
        for verb in ("main_start", "main_wakeups", "main_rename"):
            assert verb not in text


# ── 5. the contract of the surface itself ───────────────────────────────────
class TestTheSurfaceContract:
    def test_the_schema_accepts_no_arguments(self):
        """There is nothing to select and nothing to confirm. A `confirm` key in
        particular would advertise a lifecycle door on a read-only tool."""
        assert mst.MAIN_STATUS_SCHEMA == {
            "type": "object", "properties": {}, "additionalProperties": False}
        assert "confirm" not in mst.MAIN_STATUS_SCHEMA["properties"]

    def test_an_argument_the_schema_forbids_is_inert(self, _seal_state):
        plain = mst.main_status({})
        nosy = mst.main_status({"profile": "other", "confirm": True,
                                "wake": "now"})
        assert nosy == plain

    def test_an_unparseable_report_degrades_to_the_cli_text(self, monkeypatch):
        monkeypatch.setattr(mst, "_report_status",
                            lambda: (0, "main orchestrator: general"))
        out = mst.main_status({})
        assert out["ok"] is True and out["rc"] == 0
        assert "report" in out and "name" not in out

    def test_a_non_zero_verb_reports_a_failure(self, monkeypatch):
        """A refusal must not arrive as `state: not-running` — an agent would
        conclude the orchestrator is down when the read simply did not happen."""
        monkeypatch.setattr(mst, "_report_status",
                            lambda: (1, "Error: repl_inject not importable"))
        out = mst.main_status({})
        assert out["ok"] is False and out["rc"] == 1
        assert "name" not in out
        assert "repl_inject not importable" in out["report"]

    def test_the_cli_verb_this_tool_uses_is_the_documented_one(self):
        """`hscc main status --json` is the documented machine form
        (SUBCOMMAND_HELP), and the tool hands a fixed argv, so it cannot be
        steered to start or wakeups."""
        assert "--json" in main_cli.SUBCOMMAND_HELP["status"]
        src = inspect.getsource(mst)
        assert 'cmd_main(["status", "--json"])' in src
