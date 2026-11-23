"""Tests for the ``doctor`` agent tool (card t_7f570861).

The tool computes nothing: it runs the operator's own doctor command
(``python3 hscc-bootstrap/doctor.py --json`` — the line ``bootstrap.sh`` Stage 1
execs) and relays its payload. So, as in ``test_merge_queue_status_tool.py`` /
``test_autodown_status_tool.py``, these tests are about two claims plus the
surface contract, with one addition specific to this door:

1. **ONE DOOR.** What the tool returns is what the door printed — same keys,
   same rows, same bytes. Any re-derivation is the second source of truth the
   audit exists to prevent. Because the door is a real subprocess (its flat
   sibling imports only resolve when it runs as a script), fidelity is proven by
   capturing *that very spawn* and requiring equality with the tool's result —
   not by spawning twice and hoping the host state stood still between calls.
2. **READ-ONLY.** The door has exactly one mutating mode (``--fix`` →
   ``run_doctor_fix`` → ``enable_plugins.enable`` → a rewrite of the operator's
   ``config.yaml``). It must be unreachable from here, and nothing on the read
   path may write anything. Pinned four ways: the fixed argv, the structural
   AST ban, the registry, and the manifest.
3. **RC IS A VERDICT, NOT A READ STATUS** (this tool only). ``main()`` prints
   the JSON payload and *then* exits 1 on a FATAL row, so a host with a real
   problem has a good payload on a non-zero rc. A handler that checked rc first
   would drop the findings on exactly the host that needs them.

Hermetic (nolive): the door runs in a process this file cannot patch, so the
seal is done with the only thing that reaches a child — its environment.
``_seal_door_env`` points ``HOME``/``HERMES_HOME`` at a sandbox, prepends a fake
``bin`` whose ``sparkrun``/``curl`` record that they were the ones invoked, and
plants ``~/.hscc`` state naming only TEST-NET-1 (192.0.2.x) hosts and closed
loopback ports, so no probe can reach the fleet and none needs to.
``_the_seal_actually_took`` guard-asserts both halves: a seal that silently did
not apply would have every green test below reading — and deciding from — the
operator's live ``~/.hscc``, ``~/.hermes`` and fleet.
"""

import ast
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import doctor_tools as dt

#: Captured before any fixture runs, so the guard can tell a working seal from a
#: leak (after ``_seal_door_env`` the process's own ``expanduser`` answers the
#: sandbox, and comparing it against itself would pass anywhere).
_REAL_HOME = os.path.expanduser("~")
_REAL_HSCC = os.path.expanduser("~/.hscc")

#: The door's other modes. ``--fix`` is the mutation; ``--skills``/``--home``
#: change *what is read* — a tool whose caller can pick the tree is a tool that
#: can be pointed at another host's data.
_OTHER_MODES = ("--fix", "--skills", "--home")

FAKE_BIN_FILES = {
    # Records the call, answers as "no cluster configured" (so the door's TCP
    # probes get no host to probe — nolive and fast). ``cluster list`` -> ``[]``,
    # ``cluster default`` -> the literal ``null`` the doctor's own comments say
    # sparkrun prints on a host with no cluster (and already handles), and
    # ``status`` -> ``{}`` because the door does NOT survive a ``null`` status:
    # doctor.py:1274 does ``data.get("groups")`` on whatever ``json.loads`` gave
    # back, and ``json.loads("null")`` is ``None`` — an uncaught AttributeError
    # that kills the whole checklist. Reported as a door finding on t_7f570861;
    # until it is fixed the fixture answers with an empty status dict, which is
    # the shape the door can actually reason about.
    "sparkrun": (
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.path.join(os.environ['FAKE_BIN_LOG']), 'a') as fh:\n"
        "    fh.write('sparkrun ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "a = sys.argv[1:]\n"
        "if a[:2] == ['cluster', 'list']:\n"
        "    print('[]')\n"
        "elif a[:2] == ['cluster', 'default']:\n"
        "    print('null')\n"
        "elif a[:1] == ['status']:\n"
        "    print('{}')\n"
        "else:\n"
        "    print('{}')\n"
    ),
    "curl": (
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "with open(os.path.join(os.environ['FAKE_BIN_LOG']), 'a') as fh:\n"
        "    fh.write('curl ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "sys.exit(22)\n"
    ),
    "ssh": (
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "with open(os.path.join(os.environ['FAKE_BIN_LOG']), 'a') as fh:\n"
        "    fh.write('ssh ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "sys.stderr.write('nolive: ssh refused\\n')\n"
        "sys.exit(255)\n"
    ),
}


def _payload(rows, ok=True, fatal=()):
    """A door payload in the doctor's own shape."""
    return {"ok": ok, "checks": [dict(r) for r in rows],
            "fatal_failures": list(fatal)}


def _row(name, ok=True, detail="fine", fix="", fatal=False):
    return {"name": name, "ok": ok, "detail": detail, "fix": fix, "fatal": fatal}


@pytest.fixture(autouse=True)
def _seal_door_env(tmp_path, monkeypatch):
    """Every read the door performs goes to a sandbox, and nothing can reach the fleet.

    The child cannot be patched, so the seal is env-only: ``HOME`` (and with it
    ``expanduser('~')`` and ``~/.hscc``), ``HERMES_HOME``, and a ``PATH`` whose
    first entry is a fake bin holding ``sparkrun``/``curl``/``ssh`` stubs that
    log every invocation. Returns a namespace with the paths a test needs.
    """
    home = tmp_path / "fakehome"
    hermes = home / ".hermes"
    (hermes / "plugins").mkdir(parents=True)
    hscc = home / ".hscc"
    hscc.mkdir()
    (hscc / "cluster.json").write_text(json.dumps(
        {"clusters": [{"name": "sandbox", "hosts": ["192.0.2.1"]}]}),
        encoding="utf-8")
    (hscc / "serving.json").write_text(json.dumps(
        {"units": [{"name": "sandbox-unit", "host": "127.0.0.1", "port": 1,
                   "model": "sandbox-model"}]}), encoding="utf-8")
    (hermes / "config.yaml").write_text(
        "model:\n  default: worker-model\n"
        "  base_url: http://127.0.0.1:1/v1\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "fake-bin.log"
    log.write_text("", encoding="utf-8")
    for name, body in FAKE_BIN_FILES.items():
        p = bin_dir / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setenv("FAKE_BIN_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # The door probes `uv python find`, and uv maintains a cache under $HOME —
    # so a run writes `$HOME/.cache/uv/*` even though HSCC itself writes nothing
    # (that is the operator's own `doctor.py` behaving the same way today). Seal
    # it outside the snapshot so "the read writes nothing" is an exact claim
    # about HSCC state rather than a claim about uv.
    (tmp_path / "xdg-cache").mkdir()
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "xdg-cache" / "uv"))
    # Nothing in the door should need a real profile root or hscc dir.
    for var in ("HSCC_DIR", "HSCC_HOME", "HERMES_REAL_HOME"):
        monkeypatch.delenv(var, raising=False)
    return SimpleNamespace(home=home, hermes=hermes, hscc=hscc, bin=bin_dir,
                           log=log, cache=tmp_path / "xdg-cache")


def _the_seal_actually_took():
    """Fail loudly if the sandbox is not what the child will read."""
    assert os.path.expanduser("~") != _REAL_HOME, (
        "HOME was not redirected — the door would read the operator's live "
        f"{_REAL_HOME}")
    assert os.path.expanduser("~/.hscc") != _REAL_HSCC, (
        "the door's ~/.hscc still resolves to the operator's live state dir "
        f"({_REAL_HSCC})")


def _run_door_for_real(door, argv, timeout):
    """The real spawn, captured — used where fidelity to the door is the claim."""
    proc = subprocess.run([sys.executable, door, *argv],
                          cwd=os.path.dirname(door), capture_output=True,
                          text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


# ── 1. one door ─────────────────────────────────────────────────────────────
class TestOneDoor:
    def test_the_result_is_the_door_payload_and_nothing_else(
            self, _seal_door_env, monkeypatch):
        """Byte-for-byte fidelity against THE SAME SPAWN.

        Comparing against a second door run would be a coin flip on any volatile
        row (free disk GB changes between calls), so the real spawn is captured
        and the tool's result must equal its payload plus the envelope — no
        re-derived field, no default, no dropped key.
        """
        _the_seal_actually_took()
        captured = {}

        def spy(door, argv, timeout):
            rc, out, err = _run_door_for_real(door, argv, timeout)
            captured.update(rc=rc, out=out, err=err)
            return rc, out, err

        monkeypatch.setattr(dt, "_run_door", spy)
        out = dt.doctor_findings({})
        assert captured, "the door never ran"
        assert out == {**json.loads(captured["out"]), "rc": captured["rc"]}, (
            "the tool changed the payload: "
            f"{sorted(set(out) ^ set(json.loads(captured['out'])))}")

    def test_the_door_is_the_shipped_script_and_the_operator_command(
            self, _seal_door_env):
        """The door is the file ``bootstrap.sh`` Stage 1 execs, not a copy of it.

        If ``doctor.py`` ever moves, this tool must go red rather than keep
        answering from wherever it last found a doctor.
        """
        door = dt.door_source()
        assert door and door.endswith(os.path.join(*dt.DOOR_REL))
        assert Path(door).is_file()
        bootstrap = Path(door).parent / "bootstrap.sh"
        assert 'doctor.py' in bootstrap.read_text(encoding="utf-8"), (
            "bootstrap.sh no longer execs doctor.py — check what the real door "
            "is before trusting this tool's parity claim")
        # The machine flag the door keys its payload off, and the one mutating
        # mode it has (which this tool withholds).
        source = Path(door).read_text(encoding="utf-8")
        assert '"--json" in argv' in source
        assert "def run_doctor_fix" in source

    def test_the_findings_are_addressable_by_check_name(self, monkeypatch):
        """The card's contract: check name -> ok/problem + one-line cause.

        Each row keeps the door's five fields untouched, and the list stays the
        door's list — no sort, no filter, no re-naming into a tool-shaped dict.
        """
        rows = [_row("python", True, "3.12.11"),
                _row("gateway", False, "not running", "hscc api start"),
                _row("hermes", False, "/x/hermes-agent missing", "Install Hermes",
                     fatal=True)]
        monkeypatch.setattr(dt, "_report_findings",
                            lambda: (1, json.dumps(_payload(
                                rows, ok=False, fatal=["hermes"])), ""))
        out = dt.doctor_findings({})
        assert [c["name"] for c in out["checks"]] == \
               ["python", "gateway", "hermes"]
        by_name = {c["name"]: c for c in out["checks"]}
        assert by_name["gateway"]["ok"] is False
        assert by_name["gateway"]["detail"] == "not running"
        assert by_name["hermes"]["fatal"] is True
        assert out["fatal_failures"] == ["hermes"]
        assert out["rc"] == 1

    def test_a_fatal_verdict_still_returns_the_findings_not_a_read_error(
            self, monkeypatch):
        """The rc-is-a-verdict rule (the one place this tool differs from its
        siblings): ``main()`` prints the payload and exits 1 on a FATAL row, so
        rc 1 with a parseable payload must relay the findings, not swallow them.
        """
        payload = _payload([_row("hermes", False, "missing", fatal=True)],
                           ok=False, fatal=["hermes"])
        monkeypatch.setattr(dt, "_report_findings",
                            lambda: (1, json.dumps(payload), ""))
        out = dt.doctor_findings({})
        assert "checks" in out and len(out["checks"]) == 1
        assert out["ok"] is False and out["rc"] == 1
        assert "error" not in out, (
            "a broken host was reported as a read failure — the findings are the "
            "answer here, and dropping them loses the whole point of the tool")

    def test_a_clean_verdict_is_reported_clean(self, monkeypatch):
        monkeypatch.setattr(dt, "_report_findings",
                            lambda: (0, json.dumps(_payload(
                                [_row("python", True, "3.12.11")], ok=True)), ""))
        out = dt.doctor_findings({})
        assert out["ok"] is True and out["rc"] == 0

    def test_the_envelope_shadows_no_field_of_the_payload(self, monkeypatch):
        """The payload is spread into the result, so an envelope name that also
        exists in the payload would silently win or lose a field."""
        payload = _payload([_row("python")])
        monkeypatch.setattr(dt, "_report_findings",
                            lambda: (0, json.dumps(payload), ""))
        out = dt.doctor_findings({})
        assert not (set(payload) & set(dt._ENVELOPE)), (
            f"{sorted(set(payload) & set(dt._ENVELOPE))} exists in both — the "
            "door's value would be overwritten by the tool's own")
        assert set(out) == set(payload) | {"rc"}

    def test_a_second_call_asks_the_door_again_rather_than_replaying(
            self, monkeypatch, tmp_path):
        """No stale copies: the tool caches nothing, so a changed host answers
        differently on the next call. Counted, not assumed."""
        counter = tmp_path / "calls"
        counter.write_text("0", encoding="utf-8")

        def counting(door, argv, timeout):
            n = int(counter.read_text()) + 1
            counter.write_text(str(n), encoding="utf-8")
            return 0, json.dumps(_payload([_row("python", True, f"run {n}")])), ""

        monkeypatch.setattr(dt, "_run_door", counting)
        first = dt.doctor_findings({})
        second = dt.doctor_findings({})
        assert counter.read_text() == "2", "the door is not being re-run per call"
        assert first["checks"][0]["detail"] == "run 1"
        assert second["checks"][0]["detail"] == "run 2", (
            "the tool replayed a cached payload — a doctor result is a snapshot "
            "of one moment and must not outlive it")

    def test_the_sandbox_is_what_the_door_actually_read(self, _seal_door_env,
                                                        monkeypatch):
        """Guard-assert the seal reached the CHILD (a seal only the parent sees
        would leave every test here reading the operator's real trees)."""
        _the_seal_actually_took()
        monkeypatch.setattr(dt, "_run_door", _run_door_for_real)
        out = dt.doctor_findings({})
        assert out.get("checks"), f"no payload from the door: {out}"
        hermes = [c for c in out["checks"] if c["name"] == "hermes"]
        assert hermes, [c["name"] for c in out["checks"]]
        assert str(_seal_door_env.home) in hermes[0]["detail"], (
            f"the door resolved a home that is not the sandbox: {hermes[0]}")
        assert _REAL_HOME not in json.dumps(out), (
            "the operator's real home appears in the findings — the seal leaked")


# ── 2. read-only ────────────────────────────────────────────────────────────
class TestReadOnly:
    def test_the_argv_is_exactly_the_machine_flag(self):
        """No ``--fix`` (the mutation), no ``--skills``/``--home`` (a caller
        picking which tree to read), nothing else the door might act on."""
        assert dt.DOOR_ARGV == ("--json",)
        for mode in _OTHER_MODES:
            assert not any(mode in tok for tok in dt.DOOR_ARGV), (
                f"{mode} would let the caller {('repair the config' if mode == '--fix' else 'choose what is read')}"
            )

    def test_no_caller_argument_reaches_the_door(self, monkeypatch):
        seen = []

        def spy(door, argv, timeout):
            seen.append(list(argv))
            return 0, json.dumps(_payload([_row("python")])), ""

        monkeypatch.setattr(dt, "_run_door", spy)
        plain = dt.doctor_findings({})
        nosy = dt.doctor_findings({"fix": True, "argv": ["--fix"],
                                   "home": "/", "confirm": True})
        assert seen == [["--json"], ["--json"]], seen
        assert nosy == plain

    def test_the_read_writes_nothing_anywhere(self, _seal_door_env, monkeypatch):
        """Snapshot the sandbox tree around a real door call: the doctor's writes
        live behind ``--fix``, so a read that touched HSCC state here would show
        up as a changed snapshot.

        The snapshot is then proven to bite (the same control the sibling
        read-only suites use — an assertion that nothing changed is worthless if
        nothing could have changed), and the uv cache seal is checked, because
        without it this test would be quietly re-reading uv's cache as if it were
        HSCC state.
        """
        _the_seal_actually_took()
        before = _snapshot(_seal_door_env.home)
        monkeypatch.setattr(dt, "_run_door", _run_door_for_real)
        result = dt.doctor_findings({})
        after = _snapshot(_seal_door_env.home)
        assert after == before, (
            f"a read wrote into the sandbox: "
            f"{sorted(set(after) ^ set(before))}")
        assert not (_seal_door_env.home / ".cache").exists(), (
            "uv wrote its cache into $HOME after all — the cache seal is not "
            "holding, so the snapshot above is not the claim it says it is")
        # Control: the same snapshot DOES notice a write, so the assert above is
        # not vacuous.
        (_seal_door_env.home / ".hscc" / "canary.json").write_text("{}",
                                                                   encoding="utf-8")
        assert _snapshot(_seal_door_env.home) != before, (
            "the snapshot cannot see a write — the read-only assertion proves "
            "nothing")
        assert "checks" in result, (
            "the control ran against a door call that never produced findings "
            f"(rc={result.get('rc')}: {result.get('report')!r})")

    def test_the_handler_has_no_path_to_a_mutation(self):
        """Structural pin: no reference to the repair side, its writer, or a
        transport. ``subprocess`` IS allowed here — it is the door to the CLI,
        and the ban list is about the repair and the fleet, not about spawning
        the operator's own script."""
        tree = ast.parse(inspect.getsource(dt))
        touched = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        touched |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        banned = {
            "run_doctor_fix", "enable_plugins", "enable", "_write_yaml",
            "backup_file", "save_config", "record_activity",
            "sparkrun", "ssh", "docker", "run_cmd", "popen", "system",
            "write_text", "remove", "unlink", "mkdir", "chmod",
        }
        assert not (touched & banned), (
            f"{sorted(touched & banned)} in doctor_tools.py means the tool has a "
            "path to the repair side (or to a transport) — findings only")
        assert "subprocess" in touched and "run" in touched, (
            "the door is no longer the CLI command line, so this tool is "
            "re-implementing the doctor rather than running it")

    def test_no_mutating_flag_appears_in_code_outside_prose(self):
        """The argv is a literal; nothing else in the module may carry a flag the
        door would act on. Docstrings are prose (they name ``--fix`` to explain
        the refusal), so only executable string constants are scanned."""
        tree = ast.parse(inspect.getsource(dt))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                first = node.body[0] if node.body else None
                if isinstance(first, ast.Expr) and isinstance(first.value,
                                                               ast.Constant) \
                        and isinstance(first.value.value, str):
                    docstrings.add(id(first.value))
        flags = {c.value for c in ast.walk(tree)
                 if isinstance(c, ast.Constant) and isinstance(c.value, str)
                 and id(c) not in docstrings and c.value.startswith("--")}
        assert flags == {"--json"}, (
            f"{sorted(flags)} — the door may be invoked with --json only")

    def test_no_repair_doctor_tool_is_registered_anywhere(self):
        """Audit §12.2 D11 opened the read; the repair is not on the agent
        surface. Asserted at the registry, where a 'just add doctor_fix' edit
        would land."""
        init = Path(inspect.getfile(dt)).parent / "__init__.py"
        src = init.read_text(encoding="utf-8")
        assert '"doctor"' in src
        for name in ("doctor_fix", "doctor_apply", "doctor_repair",
                     "doctor_enable", "doctor_write"):
            assert f'"{name}"' not in src, (
                f"{name} would be the doctor's config-rewriting side — CLI + "
                "`--fix` only")

    def test_the_tool_is_in_the_read_table_only(self):
        init = Path(inspect.getfile(dt)).parent / "__init__.py"
        src = init.read_text(encoding="utf-8")
        ops = src.split("_OPS_TOOLS", 1)[1].split("]", 1)[0] if "_OPS_TOOLS" in src else ""
        assert '"doctor"' not in ops, (
            "doctor is a read — _OPS_TOOLS is the guarded/confirm mutation table")
        assert "(_READ_TOOLS + " in src, "_READ_TOOLS is not wired into register()"

    def test_the_plugin_manifest_declares_only_the_read(self):
        text = (Path(inspect.getfile(dt)).parent / "plugin.yaml").read_text(
            encoding="utf-8")
        assert "\n  - doctor\n" in text
        for name in ("doctor_fix", "doctor_apply", "doctor_repair"):
            assert name not in text


# ── 3. the surface contract ─────────────────────────────────────────────────
class TestTheSurfaceContract:
    def test_the_schema_accepts_no_arguments(self):
        """Nothing to select, nothing to confirm, nothing to repair. A ``fix`` or
        ``confirm`` key would advertise the mutation this tool exists to
        withhold."""
        assert dt.DOCTOR_SCHEMA == {"type": "object", "properties": {},
                                    "additionalProperties": False}
        for key in ("fix", "confirm", "home", "argv", "skills"):
            assert key not in dt.DOCTOR_SCHEMA["properties"]

    def test_a_missing_door_reports_nothing_checked_and_not_a_green(
            self, monkeypatch):
        """A tool that cannot find the doctor must not report a healthy host.
        No ``checks`` key at all — an empty list would read as 'every row
        green'."""
        monkeypatch.setattr(dt, "door_source", lambda root=None, home=None: None)
        out = dt.doctor_findings({})
        assert out["ok"] is False
        assert out["error"] == "doctor-unavailable"
        assert "checks" not in out
        assert os.path.join(*dt.DOOR_REL) in out["report"], (
            "the refusal must name what it looked for")

    def test_an_unparseable_door_is_a_read_failure_with_its_stderr(
            self, monkeypatch):
        """The door crashed (or its --json broke). stdout is empty, the traceback
        is on stderr, and that is the only explanation the agent gets."""
        monkeypatch.setattr(dt, "_report_findings",
                            lambda: (1, "", "Traceback (most recent call last): ..."))
        out = dt.doctor_findings({})
        assert out["ok"] is False and out["rc"] == 1
        assert out["error"] == "doctor-no-payload"
        assert "Traceback" in out["report"]
        assert "checks" not in out

    def test_a_hung_door_reports_nothing_answered(self, monkeypatch):
        """Timeout is not a verdict: rc None, ok False, and no payload shape that
        an agent could mistake for 'all checks passed'."""
        def hang(door, argv, timeout):
            raise subprocess.TimeoutExpired(cmd=[door], timeout=timeout)

        monkeypatch.setattr(dt, "_run_door", hang)
        out = dt.doctor_findings({})
        assert out["ok"] is False and out["rc"] is None
        assert out["error"] == "doctor-no-payload"
        assert "timed out" in out["report"]

    def test_a_non_object_payload_is_not_treated_as_findings(self, monkeypatch):
        monkeypatch.setattr(dt, "_report_findings", lambda: (0, "[1, 2, 3]", ""))
        out = dt.doctor_findings({})
        assert out["ok"] is False and out["error"] == "doctor-no-payload"

    def test_a_payload_without_ok_is_not_smoothed_to_ok(self, monkeypatch):
        """``ok`` is the doctor's verdict. Defaulting it to True would turn a
        door that reported a fatal row (but forgot the key) into a clean bill."""
        monkeypatch.setattr(dt, "_report_findings",
                            lambda: (0, json.dumps({"checks": []}), ""))
        out = dt.doctor_findings({})
        assert out["ok"] is False

    def test_the_door_timeout_is_bounded(self):
        """An unbounded tool call would hang the agent turn. The door bounds its
        own probes; this bounds the whole run."""
        assert isinstance(dt.DOOR_TIMEOUT_SECONDS, int)
        assert 10 <= dt.DOOR_TIMEOUT_SECONDS <= 300

    def test_the_candidate_paths_cover_both_deployments(self, tmp_path):
        """Repo checkout (the plugin dir's own sibling) and deployed plugins
        (``~/.hermes/plugins``)."""
        root = tmp_path / "plugins"
        (root / "hscc-bootstrap").mkdir(parents=True)
        door = root / "hscc-bootstrap" / "doctor.py"
        door.write_text("# door\n", encoding="utf-8")
        assert dt.door_source(root=str(root)) == str(door)
        home = tmp_path / "home"
        deployed = home / ".hermes" / "plugins" / "hscc-bootstrap"
        deployed.mkdir(parents=True)
        (deployed / "doctor.py").write_text("# door\n", encoding="utf-8")
        assert dt.door_source(root=str(tmp_path / "nowhere"),
                              home=str(home)) == str(deployed / "doctor.py")
        assert dt.door_source(root=str(tmp_path / "nowhere"),
                              home=str(tmp_path / "empty")) is None

    def test_the_default_candidate_is_the_plugin_dirs_own_sibling(self):
        """The order of the ladder is load-bearing, and this is the pin that
        proves it.

        Candidate #1 must be ``<this plugin dir>/../hscc-bootstrap/doctor.py`` —
        the copy that sits next to the code being tested. If it is computed a
        level too high (or resolves to the home-directory fallback first), a dev
        checkout silently answers from the DEPLOYED plugin copy in
        ``~/.hermes/plugins``: a doctor that is a different version than the one
        being changed, which is the stale copy this tool exists not to have. The
        first real version of this module did exactly that.
        """
        here = os.path.dirname(os.path.abspath(dt.__file__))
        expected = os.path.join(os.path.dirname(here), *dt.DOOR_REL)
        first = next(iter(dt._candidate_paths()))
        assert first == expected, (
            f"the door resolves to {first}, not the sibling copy {expected} — "
            "the plugin root is one level up from the plugin dir, not two")
        assert not first.startswith(os.path.expanduser("~")), (
            "the deployed copy must not outrank the sibling of the code under "
            "test")
        assert dt.door_source() == expected, (
            "in this layout the tool is reading a different doctor than the "
            "repo's")


def _snapshot(directory):
    """name -> bytes for every file under ``directory`` ({} when absent)."""
    root = Path(directory)
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}
