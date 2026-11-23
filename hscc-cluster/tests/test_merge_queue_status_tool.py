"""Tests for the `merge_queue_status` agent tool (card t_96134d32).

The tool adds no policy and computes no queue state: it asks the CLI's own
read-only verb (`cmd_merge_queue(["status", "--json"])`) and hands the payload
over. So these tests are about exactly two claims, same shape as
test_autodown_status_tool.py.

1. ONE DOOR. What the tool returns is what `hscc merge-queue status --json`
   prints — byte for byte, key for key. A tool that re-derived a field,
   defaulted one, or dropped one is the second source of truth the audit
   (docs/audits/2026-11-23-agent-surface.md) exists to prevent, and the only way
   to catch it is to compare against the CLI in the same process.
2. READ-ONLY. Nothing in this call may write `~/.hscc/merge-queue-state.json`,
   and nothing here may reach the six mutating verbs. An agent enqueueing a PR
   (which then force-pushes and squash-merges) is the self-merge door the audit
   closed (D8).

Hermetic, and it has to be: `hscc-cluster/tests` has no `~/.hscc` isolation of
its own (unlike `hscc_daemon/tests/conftest.py::_isolate_hscc`), and a status
read is NOT inert — it opens `~/.hscc/merge-queue-state.json` and reads the
flightdeck registry (via `load_projects`/`project_config`) to explain why a
project is off. `_seal_state` below points the ledger at a tmp dir (the whole
`hscc_daemon.merge_queue` path stack resolves `_hscc_root()` from `HSCC_HOME` at
CALL time, so one env var seals every ledger/lock path) and pins
`FLIGHTDECK_REGISTRY` at a planted per-test registry (or an absent path), so a
test that does not plant a signal cannot be decided by what the operator's real
registry or ledger happens to contain. `_the_seal_is_actually_on` guard-asserts
the seal took effect, because a seal that silently failed to apply would leave
every green test below reading — and deciding from — the operator's live queue.

Every registry row planted here points `repo` at a NON-EXISTENT path, so
`merge_queue.default_reason` short-circuits to "no repo" inside the pure
`_git_checkout` check and the `git rev-parse`/`git config` subprocess in
`_repo_origin` is never spawned: no test here touches a real repository or a
network, and no github origin is ever contacted (nolive).
"""

import ast
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import merge_queue_tools as mqt
from hscc_daemon import merge_queue as mq, merge_queue_cli

#: Captured BEFORE any fixture runs: `_seal_state` redirects `HSCC_HOME`, so a
#: guard that asked `mq._hscc_root()` for "the live dir" after the seal would be
#: handed the tmp dir back and could not tell a good seal from a leak.
_REAL_HSCC = os.path.expanduser("~/.hscc")

#: The six mutating verbs of the group, whose agent-tool twins must never exist
#: (audit D8: the queue force-pushes and squash-merges — a self-merge path).
MUTATING = ("add", "remove", "pause", "resume", "enable", "disable")


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


def _write_registry(path, rows):
    """A flightdeck registry from ``[{name, repo, merge_queue?}, ...]``."""
    body = {"projects": [
        {k: v for k, v in row.items() if v is not None} for row in rows]}
    try:
        import yaml
        path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    except ImportError:  # pragma: no cover - the daemon's deps carry yaml
        lines = ["projects:"]
        for row in rows:
            lines.append(f"- name: {row['name']}")
            lines.append(f"  repo: {row['repo']}")
            mqcfg = row.get("merge_queue")
            if mqcfg:
                lines.append("  merge_queue:")
                if "enabled" in mqcfg:
                    lines.append("    enabled: "
                                 + ("true" if mqcfg["enabled"] else "false"))
                if "gates" in mqcfg:
                    lines.append("    gates: [" + ", ".join(
                        f'"{g}"' for g in mqcfg["gates"]) + "]")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _seal_state(tmp_path, monkeypatch):
    """Redirect every read a status touch performs, and plant a fake registry.

    Mirrors the doctrine of ``hscc_daemon/tests/conftest.py::_isolate_hscc`` for
    the subset this call reaches: an autouse fixture so no test can forget, and
    per-test patches that run later and win. Returns a small namespace (the base
    dir, the planted-registry helper, and the absent-registry path the default
    seal pins ``FLIGHTDECK_REGISTRY`` to) rather than the bare ``tmp_path``, so
    tests read the paths they need off the fixture.
    """
    base = tmp_path / "hscc"
    base.mkdir(parents=True, exist_ok=True)
    reg_dir = tmp_path / "registries"
    reg_dir.mkdir(parents=True, exist_ok=True)

    # (a) the ledger. `_hscc_root()` reads HSCC_HOME at CALL time, so this seals
    # state_file()/load_state()/save_state()/_file_lock()/queue_root() at once.
    monkeypatch.setenv("HSCC_HOME", str(base))
    assert mq._hscc_root() == str(base), "HSCC_HOME seal did not take"

    # (b) the registry. Default to an ABSENT path so no test can accidentally
    # read the operator's real registry; `plant_registry` rebinds the env (and
    # returns the path) when a test wants real projects.
    absent = str(reg_dir / "registry-absent.yaml")
    monkeypatch.setenv("FLIGHTDECK_REGISTRY", absent)

    def plant_registry(rows):
        path = _write_registry(reg_dir / "registry.yaml", rows)
        monkeypatch.setenv("FLIGHTDECK_REGISTRY", str(path))
        return str(path)

    return SimpleNamespace(tmp=tmp_path, base=base,
                           reg_dir=reg_dir, absent_registry=absent,
                           plant_registry=plant_registry)


@pytest.fixture(autouse=True)
def _the_seal_is_actually_on(_seal_state):
    """Guard-assert the seal, so a broken seal is a red test and not a live read.

    Without this, a rename upstream (e.g. `_hscc_root` no longer reading
    HSCC_HOME, or `load_projects` ignoring FLIGHTDECK_REGISTRY) would silently
    re-point the read at the operator's real ledger/registry and every test here
    would still pass — while answering from the live queue instead of the
    fixture it claims to control.
    """
    assert not mq.state_file().startswith(_REAL_HSCC + os.sep), (
        f"state_file() resolves to the live state dir "
        f"({mq.state_file()}) — the HSCC_HOME seal is not applied")
    reg = os.environ.get("FLIGHTDECK_REGISTRY")
    inside = (reg == _seal_state.absent_registry
              or (reg or "").startswith(str(_seal_state.reg_dir) + os.sep))
    assert inside, (
        f"FLIGHTDECK_REGISTRY points outside the per-test dir ({reg!r})")


def cli_json():
    """`hscc merge-queue status --json` — the operator's own bytes, in-process."""
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    rc = None
    try:
        with redirect_stdout(buf):
            rc = merge_queue_cli.cmd_merge_queue(["status", "--json"])
    except SystemExit as exc:          # a verb must RETURN, never exit
        rc = exc.code if isinstance(exc.code, int) else 0
    return rc, buf.getvalue().strip()


def cli_argv():
    """The argv the CLI form of the same question uses (docs + group help)."""
    return ["merge-queue", "status", "--json"]


# ── 1. one door: the tool's payload IS the CLI's payload ────────────────────
class TestSameStateAsTheCli:
    def test_the_payload_is_the_cli_bytes(self, _seal_state):
        """The whole design in one assertion: parse what the CLI printed and it
        must equal what the tool returned, key for key, value for value.

        Only ``rc`` is stripped from the tool result — the CLI payload carries
        its own ``ok`` (which the verb sets True), and the tool passes it
        through rather than re-setting it, so the parity holds on ``ok`` too.
        """
        rc, text = cli_json()
        out = mqt.merge_queue_status({})
        assert rc == 0
        assert out["ok"] is True and out["rc"] == 0
        assert {k: v for k, v in out.items() if k != "rc"} == json.loads(text)

    def test_the_key_sets_match_under_a_rich_state(self, _seal_state):
        """Schema parity (not just value parity): with a planted registry +
        ledger the tool's `projects` key set must equal the CLI's exactly, so a
        tool that hand-copied a subset of projects could not pass."""
        _seal_state.plant_registry([
            {"name": "alpha", "repo": "/nonexistent/alpha",
             "merge_queue": {"enabled": True}},
            {"name": "beta", "repo": "/nonexistent/beta"},
        ])
        _write(Path(mq.state_file()), {"version": 1, "projects": {
            "alpha": {"enabled": None, "paused": False,
                      "queue": [{"pr": 12, "card": "t_x", "assignee": "w1"}],
                      "waiting": [{"pr": 9}], "in_flight": {"pr": 11},
                      "last": {"pr": 10, "result": "merged"}},
        }})
        rc, text = cli_json()
        out = mqt.merge_queue_status({})
        assert set(out["projects"]) == set(json.loads(text)["projects"])
        assert set(out["projects"]) == {"alpha", "beta"}

    def test_a_queued_project_reports_its_depth_and_entries(self, _seal_state):
        _seal_state.plant_registry([
            {"name": "alpha", "repo": "/nonexistent/alpha",
             "merge_queue": {"enabled": True}}])
        _write(Path(mq.state_file()), {"version": 1, "projects": {
            "alpha": {"enabled": None, "paused": False,
                      "queue": [{"pr": 12, "card": "t_x", "assignee": "w1"},
                                {"pr": 13, "card": "t_y", "assignee": "w2"}],
                      "waiting": [{"pr": 9}], "in_flight": {"pr": 11},
                      "last": {"pr": 10, "result": "merged"}},
        }})
        row = mqt.merge_queue_status({})["projects"]["alpha"]
        assert row["queue_len"] == 2
        assert row["waiting_len"] == 1
        assert [e["pr"] for e in row["queue"]] == [12, 13]
        assert row["in_flight"] == {"pr": 11}
        assert row["last"] == {"pr": 10, "result": "merged"}

    def test_an_empty_ledger_reads_no_projects_not_error(self, _seal_state):
        """No ledger file is the empty default (load_state -> _empty_state),
        reported as an ordinary answer, not a failure."""
        out = mqt.merge_queue_status({})
        assert out["ok"] is True and out["rc"] == 0
        assert out["projects"] == {}

    def test_a_project_off_by_default_reports_its_reason(self, _seal_state):
        """`default_reason` is the field an agent acts on ('why is this project
        not landing?'), so it must arrive intact, in the CLI's own wording — the
        nonexistent repo path yields 'no repo' without any git subprocess."""
        _seal_state.plant_registry([
            {"name": "gamma", "repo": "/nonexistent/gamma"}])
        row = mqt.merge_queue_status({})["projects"]["gamma"]
        assert row["enabled"] is False
        assert row["enabled_source"] == "default"
        assert row["default_reason"] == "no repo"
        assert row["default_enabled"] is False

    def test_a_registry_override_reports_enabled_and_its_source(self, _seal_state):
        """An explicit registry `enabled: true` wins over the (failed) default
        even with a no-repo path, and `enabled_source` says 'registry' — the
        tool must relay the CLI's resolution, not recompute it."""
        _seal_state.plant_registry([
            {"name": "alpha", "repo": "/nonexistent/alpha",
             "merge_queue": {"enabled": True}}])
        row = mqt.merge_queue_status({})["projects"]["alpha"]
        assert row["enabled"] is True
        assert row["enabled_source"] == "registry"

    def test_a_ledger_pause_override_reports_paused_and_cli_source(
            self, _seal_state):
        """A CLI `pause`/`enable` written into the ledger surfaces as
        enabled_source='cli' and paused=True — again the CLI's own resolution."""
        _seal_state.plant_registry([
            {"name": "alpha", "repo": "/nonexistent/alpha"}])
        _write(Path(mq.state_file()), {"version": 1, "projects": {
            "alpha": {"enabled": True, "paused": True, "queue": [],
                      "waiting": [], "in_flight": None, "last": None},
        }})
        row = mqt.merge_queue_status({})["projects"]["alpha"]
        assert row["enabled"] is True
        assert row["enabled_source"] == "cli"
        assert row["paused"] is True

    def test_a_state_only_project_still_appears(self, _seal_state):
        """status lists every known project: registry order first, then ledger-
        only names. A project in the ledger but not the registry must still
        show (the CLI adds it; the tool relays it)."""
        _write(Path(mq.state_file()), {"version": 1, "projects": {
            "orphan": {"enabled": None, "paused": False, "queue": [],
                       "waiting": [], "in_flight": None,
                       "last": {"pr": 5, "result": "conflict"}},
        }})
        row = mqt.merge_queue_status({})["projects"]["orphan"]
        assert row["last"] == {"pr": 5, "result": "conflict"}

    def test_the_envelope_shadows_no_field_of_the_payload(self, _seal_state):
        """The payload is spread into the envelope, so a name collision would
        silently win or lose a field. It must not be possible by accident.

        `_ENVELOPE` holds only the keys the tool ADDS (rc, report); the CLI
        payload's own ``ok`` is passed through and is not in the envelope.
        """
        rc, text = cli_json()
        payload = json.loads(text)
        assert not (set(payload) & set(mqt._ENVELOPE)), (
            f"the CLI payload now carries "
            f"{sorted(set(payload) & set(mqt._ENVELOPE))} — rename the envelope "
            "key or nest the payload; do not let one silently overwrite the "
            "other")

    def test_the_result_is_json_serialisable(self):
        """register() wraps every handler in `_stringify`, which json.dumps-es a
        dict for the tool-role message. A non-serialisable value in the payload
        would only surface as an HTTP 400 on the wire."""
        out = mqt.merge_queue_status({})
        assert isinstance(out, dict)
        assert isinstance(json.dumps(out, default=str), str)


# ── 2. read-only, across every door ─────────────────────────────────────────
class TestReadOnly:
    def test_a_status_read_writes_nothing(self, _seal_state):
        """Byte-for-byte: the whole state dir is unchanged by the tool call."""
        _write(Path(mq.state_file()), {"version": 1, "projects": {
            "alpha": {"enabled": None, "paused": False,
                      "queue": [{"pr": 12}], "waiting": [], "in_flight": None,
                      "last": None},
        }})
        before = _snapshot(_seal_state.base)
        assert before, "the fixture must plant state, or this proves nothing"
        out = mqt.merge_queue_status({})
        assert out["projects"]["alpha"]["queue_len"] == 1
        assert _snapshot(_seal_state.base) == before

    def test_the_cli_door_writes_nothing_either(self, _seal_state):
        """The parity comparison is only meaningful if the CLI door is equally
        inert — otherwise 'tool == CLI' could be equality of two mutations."""
        _write(Path(mq.state_file()), {"version": 1, "projects": {
            "alpha": {"enabled": None, "paused": False, "queue": [],
                      "waiting": [], "in_flight": None, "last": None},
        }})
        before = _snapshot(_seal_state.base)
        cli_json()
        assert _snapshot(_seal_state.base) == before

    def test_a_write_verb_would_change_the_state_this_door_does_not(
            self, _seal_state):
        """Prove the read is distinguished from the write by MORE than naming:
        enqueue (a mutating verb) DOES touch the ledger under the same seal, so
        `test_a_status_read_writes_nothing` is discriminating, not vacuous."""
        _seal_state.plant_registry([
            {"name": "alpha", "repo": "/nonexistent/alpha"}])
        before = _snapshot(_seal_state.base)
        mq.add_to_queue("alpha", 99)
        after = _snapshot(_seal_state.base)
        assert after != before, (
            "add_to_queue wrote nothing — the seal is pointed at a dir the "
            "mutating verb does not use, and the read-only tests prove nothing")
        # The tool door, by contrast, still writes nothing on the same ledger.
        assert _snapshot(_seal_state.base) == after
        mqt.merge_queue_status({})
        assert _snapshot(_seal_state.base) == after

    def test_the_handler_has_no_path_to_a_mutation(self):
        """Structural pin: this module must not reach any mutating verb, the
        ledger writer, the builders, or a transport. Only the CLI's status verb
        may appear."""
        src = inspect.getsource(mqt)
        tree = ast.parse(src)
        touched = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        touched |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        banned = {
            # the six mutating verbs + the writer they all funnel through
            "add_to_queue", "remove_from_queue", "set_paused", "set_enabled",
            "save_state",
            # the builders a tool must not read directly (the verb is the door)
            "load_state", "project_config", "load_projects", "view",
            # no transport, ever
            "subprocess", "run_cmd", "sparkrun", "ssh",
        }
        assert not (touched & banned), (
            f"{sorted(touched & banned)} in merge_queue_tools.py means a second "
            "door to the state (or to a mutation) — the handler must only ask "
            "the CLI verb")
        assert "cmd_merge_queue" in touched, (
            "the handler no longer asks the CLI's own verb, so it is reading "
            "the ledger by hand again")

    def test_no_mutating_merge_queue_tool_is_registered_anywhere(self):
        """The audit closed the write door for agents (D8). Assert it at the
        registry, where a 'just add add_to_queue' edit would land."""
        init = Path(inspect.getfile(mqt)).parent / "__init__.py"
        src = init.read_text()
        for verb in MUTATING:
            assert f'"merge_queue_{verb}"' not in src, (
                f"merge_queue_{verb} is a merge-queue mutation (force-push + "
                "squash-merge) — CLI only, per audit D8")
        assert '"merge_queue_status"' in src
        # `_READ_TOOLS` opens the concatenation the register() loop iterates.
        assert "(_READ_TOOLS + " in src, "_READ_TOOLS is not wired into register()"

    def test_the_plugin_manifest_declares_only_the_read(self):
        """`provides_tools` is what `hermes tools` advertises; under-declaring
        hides the tool from the fleet, over-declaring makes verify demand a tool
        the registry cannot deploy."""
        import re
        path = Path(inspect.getfile(mqt)).parent / "plugin.yaml"
        text = path.read_text()
        assert re.search(r"^\s+- merge_queue_status$", text, re.MULTILINE)
        for verb in MUTATING:
            assert f"merge_queue_{verb}" not in text


# ── 3. the contract of the surface itself ───────────────────────────────────
class TestTheSurfaceContract:
    def test_the_schema_accepts_no_arguments(self):
        """There is nothing to select and nothing to confirm. A `confirm` key in
        particular would advertise a mutation door on a read-only tool."""
        assert mqt.MERGE_QUEUE_STATUS_SCHEMA == {
            "type": "object", "properties": {}, "additionalProperties": False}
        assert "confirm" not in mqt.MERGE_QUEUE_STATUS_SCHEMA["properties"]

    def test_an_argument_the_schema_forbids_is_inert(self, _seal_state):
        """A model that invents `project`/`confirm` must not change the answer
        and must not gain a mutation: the verb takes no steerable input here."""
        _write(Path(mq.state_file()), {"version": 1, "projects": {
            "alpha": {"enabled": None, "paused": False, "queue": [],
                      "waiting": [], "in_flight": None, "last": None},
        }})
        before = _snapshot(_seal_state.base)
        plain = mqt.merge_queue_status({})
        nosy = mqt.merge_queue_status({"project": "alpha", "confirm": True,
                                       "pr": 999})
        assert nosy == plain
        assert _snapshot(_seal_state.base) == before

    def test_an_unparseable_report_degrades_to_the_cli_text(self, monkeypatch):
        """The verb's --json is the contract; if it ever stops being parseable,
        the agent gets the operator's text rather than a crash or an empty dict
        that would read as 'the queue is empty'."""
        monkeypatch.setattr(mqt, "_report_status",
                            lambda: (0, "merge queue: no projects configured"))
        out = mqt.merge_queue_status({})
        assert out["ok"] is True and out["rc"] == 0
        assert "report" in out and "projects" not in out

    def test_a_non_zero_verb_reports_a_failure(self, monkeypatch):
        """A refusal must not arrive as a successful empty queue — an agent that
        read an empty `projects` from a failed call would conclude nothing is
        waiting to land when the read simply did not happen."""
        monkeypatch.setattr(mqt, "_report_status",
                            lambda: (1, "Error: cannot read the ledger"))
        out = mqt.merge_queue_status({})
        assert out["ok"] is False and out["rc"] == 1
        assert "projects" not in out
        assert "cannot read the ledger" in out["report"]

    def test_the_cli_verb_this_tool_uses_is_the_documented_one(self):
        """`hscc merge-queue status --json` is the documented machine form
        (SUBCOMMAND_HELP), and the tool hands a fixed argv, so it cannot be
        steered to a mutating subcommand."""
        assert "--json" in merge_queue_cli.SUBCOMMAND_HELP["status"]
        # The dispatcher takes only argv (a list the tool builds itself).
        params = list(inspect.signature(
            merge_queue_cli.cmd_merge_queue).parameters)
        assert params == ["argv"], params

    def test_the_argv_it_mirrors_is_the_operator_command(self):
        """Documentation pin: the argv a human types for the same answer."""
        assert cli_argv() == ["merge-queue", "status", "--json"]
        assert "hscc merge-queue status [--json]" in \
            merge_queue_cli.SUBCOMMAND_HELP["status"]
