"""Tests for the `autodown_status` agent tool (card t_c036808c).

The tool adds no policy and computes no state: it asks the CLI's own read-only
verb and hands the payload over. So these tests are about exactly two claims.

1. ONE DOOR. What the tool returns is what `hscc autodown status --json`
   prints — byte for byte, key for key. A tool that re-derived a field,
   defaulted one, or dropped one is the second source of truth the audit
   (docs/audits/2026-11-23-agent-surface.md §4) exists to prevent, and the only
   way to catch it is to compare against the CLI in the same process.
2. READ-ONLY. Nothing in this call may write `~/.hscc/autodown.json` or the
   watchdog block, and nothing here may reach the four mutating verbs. Arming
   idle teardown from an agent turn is the failure the audit closed
   ("🔒 for the tool — fleet-power policy is daemon/operator scope").

Hermetic, and it has to be: `hscc-cluster/tests` has no `~/.hscc` isolation of
its own (unlike `hscc_daemon/tests/conftest.py::_isolate_hscc`), and a status
read is NOT inert — it opens `~/.hscc/autodown.json`, the watchdog block and the
Hermes cron file, evaluates the kanban and PR/CI interlocks, and, when the
record says `down`, probes the orchestrator's `/health` endpoint over the
network. `_seal_state` below redirects the files and default-stubs the ambient
probes to the answer a fresh host gives (nothing active, nothing probed), and
`_the_seal_is_actually_on` guard-asserts that it took effect, because a seal
that silently failed to apply would leave every green test below reading — and
deciding from — the operator's live cluster state.
"""

import ast
import inspect
import json
import os
import sys
from pathlib import Path

import pytest

import autodown_tools as adt
from hscc_daemon import autodown, autodown_cli, lifecycle

ORCH = "192.0.2.10"
W1 = "192.0.2.11"

#: Captured BEFORE any fixture runs: `_seal_state` patches `os.path.expanduser`,
#: so a guard that asked `os.path.expanduser("~/.hscc")` for "the live dir" would
#: be handed the tmp dir back and would fail on a perfectly good seal (measured:
#: 21 setup errors, all of them this assert, on a run where the seal worked).
_REAL_HSCC = os.path.expanduser("~/.hscc")
_REAL_HERMES = os.path.expanduser("~/.hermes")

#: The four mutating verbs of the group, whose agent-tool twins must never exist.
MUTATING = ("enable", "disable", "wake", "cancel")


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


@pytest.fixture(autouse=True)
def _seal_state(tmp_path, monkeypatch):
    """Redirect every read a status touch performs, and default-stub its probes.

    Mirrors (a) + (b) of ``hscc_daemon/tests/conftest.py::_isolate_hscc`` for the
    subset this call actually reaches; the doctrine — an autouse fixture so no
    test can forget, and per-test patches that run later and win — is the same.
    """
    base = tmp_path / "hscc"
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

    # (a) call-time expanduser: any `~/.hscc/...` or `~/.hermes/...` a helper
    # resolves at runtime lands under the per-test dir.
    monkeypatch.setattr(os.path, "expanduser", _redirect)

    # (b) the constants already baked at import time.
    def p(name):
        return str(base / name)

    for mod, attr, value in (
            (autodown, "AUTODOWN_FILE", p("autodown.json")),
            (autodown, "AUTODOWN_LOCK", p("autodown.lock")),
            (autodown, "INTERLOCK_STATE_FILE", p("autodown_interlock.json")),
            (autodown, "HTTP_ACTIVITY_STATE", p("activity.json")),
            (autodown, "AGENTS_FILE", p("agents.json")),
            # Hermes' cron file is a status input (the active-cron split).
            (autodown, "CRON_JOBS_FILE", str(tmp_path / "hermes" / "cron" / "jobs.json")),
            (lifecycle, "WATCHDOG_BLOCK_FILE", p("watchdog-block.json")),
            (lifecycle, "BRIDGE_FILE", p("bridge.json")),
    ):
        monkeypatch.setattr(mod, attr, value)
        # Self-verify the seal HERE, not only in the guard below: a patch that
        # silently did not stick (an attribute renamed upstream, a descriptor, a
        # second module object for the same file) must name the entry that
        # failed instead of surfacing 21 identical guard failures.
        if getattr(mod, attr) != value:
            raise AssertionError(
                f"seal did not stick: {mod.__name__}.{attr} reads "
                f"{getattr(mod, attr)!r} after being set to {value!r}")

    # (c) the ambient probes. Defaults reproduce a fresh host: nothing active,
    # nothing evaluated, layer not probed — so a test that does not plant a
    # signal cannot accidentally be decided by what the operator's cluster or
    # boards happen to be doing.
    monkeypatch.setattr(autodown, "_serving_actually_up", lambda *a, **k: False)
    monkeypatch.setattr(autodown, "_has_active_work", lambda *a, **k: False)
    monkeypatch.setattr(autodown, "_has_active_pr_ci", lambda *a, **k: False)
    monkeypatch.setattr(autodown, "_KANBAN_LOAD",
                        {"ok": None, "reason": "", "warned": False})
    monkeypatch.setattr(autodown, "_PRCI_LOAD",
                        {"blocking": None, "ok": None, "reason": "", "warned": False})
    return tmp_path


@pytest.fixture(autouse=True)
def _the_seal_is_actually_on(_seal_state):
    """Guard-assert the seal, so a broken seal is a red test and not a live read.

    Without this, a rename in ``autodown.py`` would silently re-point
    ``load_config`` at the operator's real `~/.hscc/autodown.json` and every
    test here would still pass — while reading armed/disabled from the live
    cluster instead of from the fixture it claims to control.
    """
    live = os.path.join(_REAL_HSCC, "autodown.json")
    ident = (f"module id here={id(autodown)} "
             f"id in sys.modules={id(sys.modules.get('hscc_daemon.autodown'))}")
    assert not autodown.AUTODOWN_FILE.startswith(_REAL_HSCC + os.sep), (
        f"AUTODOWN_FILE resolves to the live state dir ({live}) — the seal is "
        f"not applied, so nothing below is hermetic ({ident})")
    assert not autodown.CRON_JOBS_FILE.startswith(_REAL_HERMES + os.sep), (
        f"CRON_JOBS_FILE still points inside the live HERMES_HOME "
        f"({autodown.CRON_JOBS_FILE!r}; {ident})")


def cli_json():
    """`hscc autodown status --json` — the operator's own bytes, in-process."""
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    rc = None
    try:
        with redirect_stdout(buf):
            rc = autodown_cli._cmd_status([], True, None)
    except SystemExit as exc:          # a verb must RETURN, never exit
        rc = exc.code if isinstance(exc.code, int) else 0
    return rc, buf.getvalue().strip()


def cli_argv():
    """The argv the CLI form of the same question uses (docs + the group help)."""
    return ["autodown", "status", "--json"]


# ── 1. one door: the tool's payload IS the CLI's payload ────────────────────
class TestSameStateAsTheCli:
    def test_the_payload_is_the_cli_bytes(self):
        """The whole design in one assertion: parse what the CLI printed and it
        must equal what the tool returned, key for key, value for value."""
        rc, text = cli_json()
        out = adt.autodown_status({})
        assert rc == 0
        assert out["ok"] is True and out["rc"] == 0
        assert {k: v for k, v in out.items() if k not in ("ok", "rc")} \
            == json.loads(text)

    def test_an_armed_config_reports_enabled_with_its_window(self, _seal_state):
        _write(Path(autodown.AUTODOWN_FILE),
               {"enabled": True, "idle_minutes": 45, "state": "up",
                "last_activity_iso": "2026-11-23T04:00:00+00:00"})
        out = adt.autodown_status({})
        assert out["enabled"] is True
        assert out["idle_minutes"] == 45
        assert out["state"] == "up"
        assert out["state_source"] == "autodown.json"
        assert out["last_activity_iso"] == "2026-11-23T04:00:00+00:00"

    def test_no_config_file_reads_disabled_not_error(self):
        """An absent autodown.json is the disabled default (fail-closed, §8),
        and the tool must report that as an ordinary answer, not a failure."""
        out = adt.autodown_status({})
        assert out["ok"] is True
        assert out["enabled"] is False
        assert out["state"] == "up"
        assert out["last_activity_iso"] is None

    def test_a_blocked_teardown_names_the_interlock(self, monkeypatch):
        """`blocked_by` is the field an agent acts on ('why has this fleet not
        scaled down?'), so it must arrive intact, in the CLI's own wording."""
        monkeypatch.setattr(autodown, "_has_active_work", lambda *a, **k: True)
        monkeypatch.setattr(autodown, "kanban_blocking_board", lambda: "hscc")
        monkeypatch.setattr(
            autodown, "list_stale_tasks",
            lambda **kw: {"tasks": [{"id": "t_abc", "title": "hold the fleet"}]})
        out = adt.autodown_status({})
        assert "kanban work on board 'hscc'" in out["blocked_by"]
        assert "t_abc" in out["blocked_by"]
        assert out["kanban_ok"] is None or out["kanban_ok"] is True

    def test_an_unreachable_prci_source_reports_the_fail_safe(self, monkeypatch):
        monkeypatch.setattr(autodown, "_has_active_pr_ci", lambda *a, **k: True)
        monkeypatch.setattr(autodown, "prci_blocking_signal",
                            lambda: autodown._PRCI_UNREACHABLE)
        out = adt.autodown_status({})
        assert "PR/CI source unreachable" in out["blocked_by"]
        assert out["prci_active"] is True
        assert out["prci_signal"] == autodown._PRCI_UNREACHABLE

    def test_a_down_record_the_probe_contradicts_arrives_reconciled(
            self, _seal_state, monkeypatch):
        """The §8 reconcile is the reason a status read is worth having: the
        tool must show `state: up` + `state_recorded: down` + the note, exactly
        as the CLI prints them, and must NOT invent a `down_since`."""
        _write(Path(autodown.AUTODOWN_FILE),
               {"enabled": True, "state": "down",
                "down_since": "2026-11-05T00:00:00+00:00"})
        monkeypatch.setattr(autodown, "_serving_actually_up",
                            lambda *a, **k: True)
        rc, text = cli_json()
        out = adt.autodown_status({})
        assert out["state"] == "up"
        assert out["state_recorded"] == "down"
        assert out["state_source"] == "live probe"
        assert out["fleet_up"] is True
        assert out["down_since"] is None
        assert "read-only; the file was not changed" in out["state_note"]
        assert {k: v for k, v in out.items() if k not in ("ok", "rc")} \
            == json.loads(text)

    def test_no_field_is_dropped_or_invented(self, _seal_state, monkeypatch):
        """A rich payload through both doors: the key SETS must be identical.

        The parity tests above compare values; this one compares the schema
        itself, so a tool that hand-copied a subset of the fields could not
        pass by getting the copied ones right.
        """
        _write(Path(autodown.AUTODOWN_FILE), {
            "enabled": True, "idle_minutes": 0, "state": "error",
            "last_activity_iso": "2026-11-23T04:00:00+00:00",
            "wake_source": "telegram", "reason": "wake failed: timeout",
            "force_armed": True, "force_armed_overrides": ["nightly-eval"]})
        _write(Path(lifecycle.WATCHDOG_BLOCK_FILE),
               {"blocked": True, "intentional": "autodown: idle teardown"})
        monkeypatch.setitem(autodown._PRCI_LOAD, "ok", False)
        monkeypatch.setitem(autodown._PRCI_LOAD, "reason", "gh auth expired")
        rc, text = cli_json()
        out = adt.autodown_status({})
        payload = {k: v for k, v in out.items() if k not in ("ok", "rc")}
        assert set(payload) == set(json.loads(text))
        assert payload["force_armed"] is True
        assert payload["force_armed_overrides"] == ["nightly-eval"]
        assert payload["watchdog_blocked"] is True
        assert payload["watchdog_intentional"] == "autodown: idle teardown"
        assert payload["prci_ok"] is False
        assert payload["reason"] == "wake failed: timeout"

    def test_the_envelope_shadows_no_field_of_the_payload(self):
        """The payload is spread into the envelope, so a name collision would
        silently win or lose a field. It must not be possible by accident."""
        rc, text = cli_json()
        payload = json.loads(text)
        assert not (set(payload) & set(adt._ENVELOPE)), (
            f"the CLI payload now carries {sorted(set(payload) & set(adt._ENVELOPE))} "
            "— rename the envelope key or nest the payload; do not let one "
            "silently overwrite the other")

    def test_the_result_is_json_serialisable(self):
        """register() wraps every handler in `_stringify`, which json.dumps-es a
        dict for the tool-role message. A non-serialisable value in the payload
        would only surface as an HTTP 400 on the wire."""
        out = adt.autodown_status({})
        assert isinstance(out, dict)
        assert isinstance(json.dumps(out, default=str), str)


# ── 2. read-only, across every door ─────────────────────────────────────────
class TestReadOnly:
    def test_a_status_read_writes_nothing(self, _seal_state):
        """Byte-for-byte: the whole state dir is unchanged by the tool call."""
        _write(Path(autodown.AUTODOWN_FILE),
               {"enabled": True, "idle_minutes": 10, "state": "up"})
        before = _snapshot(_seal_state / "hscc")
        assert before, "the fixture must plant state, or this proves nothing"
        assert adt.autodown_status({})["enabled"] is True
        assert _snapshot(_seal_state / "hscc") == before

    def test_the_cli_door_writes_nothing_either(self, _seal_state):
        """The comparison above is only meaningful if the CLI door is equally
        inert — otherwise 'tool == CLI' could be equality of two mutations."""
        _write(Path(autodown.AUTODOWN_FILE), {"enabled": True, "state": "up"})
        before = _snapshot(_seal_state / "hscc")
        cli_json()
        assert _snapshot(_seal_state / "hscc") == before

    def test_the_status_probe_never_writes_the_record_it_reconciles(
            self, _seal_state, monkeypatch):
        """The reconcile is the one path where a 'read' has a tempting write
        (the file says down, the layer is up). §8 forbids it here; assert it."""
        path = _write(Path(autodown.AUTODOWN_FILE),
                      {"enabled": True, "state": "down",
                       "down_since": "2026-11-05T00:00:00+00:00"})
        before = path.read_bytes()
        monkeypatch.setattr(autodown, "_serving_actually_up",
                            lambda *a, **k: True)
        out = adt.autodown_status({})
        assert out["state"] == "up" and out["state_recorded"] == "down"
        assert path.read_bytes() == before

    def test_the_handler_has_no_path_to_a_mutation(self):
        """Structural pin: this module must not reach any mutating verb, the
        idle timer, or a transport. Only the CLI's read verb may appear."""
        src = inspect.getsource(adt)
        tree = ast.parse(src)
        touched = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        touched |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        banned = {"save_config", "record_activity", "autoup", "teardown",
                  "_clear_intentional_block", "run_cmd", "subprocess",
                  "sparkrun", "ssh", "status_snapshot", "load_watchdog_block"}
        assert not (touched & banned), (
            f"{sorted(touched & banned)} in autodown_tools.py means a second "
            "door to the state (or to a mutation) — the handler must only ask "
            "the CLI verb")
        assert "_cmd_status" in touched, (
            "the handler no longer asks the CLI's own verb, so it is reading "
            "the state by hand again")

    def test_no_mutating_autodown_tool_is_registered_anywhere(self):
        """The audit closed the write door for agents. Assert it at the
        registry, where a 'just add enable' edit would land."""
        init = Path(inspect.getfile(adt)).parent / "__init__.py"
        src = init.read_text()
        for verb in MUTATING:
            assert f'"autodown_{verb}"' not in src, (
                f"autodown_{verb} is a fleet-power mutation — CLI + "
                "confirm-gated route only (audit §2.4)")
        assert '"autodown_status"' in src
        # `_READ_TOOLS` opens the concatenation the register() loop iterates:
        # `in (_READ_TOOLS + _OPS_TOOLS + …)`. A table that exists but is not in
        # that expression registers nothing (test_plugin_manifest_drift.py has
        # the generic version of this pin).
        assert "(_READ_TOOLS + " in src, "_READ_TOOLS is not wired into register()"

    def test_the_plugin_manifest_declares_only_the_read(self):
        """`provides_tools` is what `hermes tools` advertises; under-declaring
        hides the tool from the fleet, over-declaring makes verify demand a tool
        the registry cannot deploy."""
        import re
        path = Path(inspect.getfile(adt)).parent / "plugin.yaml"
        text = path.read_text()
        assert re.search(r"^\s+- autodown_status$", text, re.MULTILINE)
        for verb in MUTATING:
            assert f"autodown_{verb}" not in text


# ── 3. the contract of the surface itself ───────────────────────────────────
class TestTheSurfaceContract:
    def test_the_schema_accepts_no_arguments(self):
        """There is nothing to select and nothing to confirm. A `confirm` key in
        particular would advertise a mutation door on a read-only tool."""
        assert adt.AUTODOWN_STATUS_SCHEMA == {
            "type": "object", "properties": {}, "additionalProperties": False}
        assert "confirm" not in adt.AUTODOWN_STATUS_SCHEMA["properties"]

    def test_a_argument_the_schema_forbids_is_inert(self, _seal_state):
        """A model that invents `host`/`confirm` must not change the answer and
        must not gain a mutation: the verb takes no positional arguments."""
        _write(Path(autodown.AUTODOWN_FILE), {"enabled": False, "state": "up"})
        before = _snapshot(_seal_state / "hscc")
        plain = adt.autodown_status({})
        nosy = adt.autodown_status({"host": W1, "confirm": True,
                                    "idle_minutes": 999})
        assert nosy == plain
        assert _snapshot(_seal_state / "hscc") == before

    def test_an_unparseable_report_degrades_to_the_cli_text(self, monkeypatch):
        """The verb's --json is the contract; if it ever stops being parseable,
        the agent gets the operator's text rather than a crash or an empty dict
        that would read as 'disabled'."""
        monkeypatch.setattr(adt, "_report_status",
                            lambda: (0, "autodown: DISABLED (not json)"))
        out = adt.autodown_status({})
        assert out["ok"] is True and out["rc"] == 0
        assert "report" in out and "enabled" not in out

    def test_a_non_zero_verb_reports_a_failure(self, monkeypatch):
        """A refusal must not arrive as a successful empty status — an agent
        that read `enabled: false` from a failed call would conclude the fleet
        is safe to power down."""
        monkeypatch.setattr(adt, "_report_status",
                            lambda: (1, "Error: cannot read the board"))
        out = adt.autodown_status({})
        assert out["ok"] is False and out["rc"] == 1
        assert "enabled" not in out
        assert "cannot read the board" in out["report"]

    def test_the_cli_verb_this_tool_uses_is_the_documented_one(self):
        """`hscc autodown status --json` is the documented machine form
        (SUBCOMMAND_HELP), and the verb exposes no other input besides the
        unused positional rest — so this tool cannot be steered."""
        assert "--json" in autodown_cli.SUBCOMMAND_HELP["status"]
        params = list(inspect.signature(autodown_cli._cmd_status).parameters)
        assert params == ["rest", "json_mode", "theme_name"], params

    def test_the_argv_it_mirrors_is_the_operator_command(self):
        """Documentation pin: the argv a human types for the same answer."""
        assert cli_argv() == ["autodown", "status", "--json"]
        assert "hscc autodown status [--json]" in \
            autodown_cli.SUBCOMMAND_HELP["status"]
