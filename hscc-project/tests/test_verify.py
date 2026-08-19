"""Tests for flightdeck.core.verify + flightdeck.commands.verify.

Every test injects a fake runner (and fake clocks), so no test touches the
real shell, git, or the network. State files go to a pytest tmp_path, never
~/.flightdeck.

Three states, never two: PASS / FAIL / NO_VERIFY. A project with no verify
command is reported distinctly -- never skipped silently, never counted as
passing.
"""

import argparse
import json

import pytest

import yaml

from flightdeck.commands import verify as verify_cmd
from flightdeck.core import registry, verify
from flightdeck.core.verify import FAIL, NO_VERIFY, PASS


# --------------------------------------------------------------------------- #
# A fake shell runner
# --------------------------------------------------------------------------- #

def _ok_run(cmd, cwd):
    """Runner stub: ANY command succeeds, instantly."""
    return _Proc(0, "", "")


class _Proc:
    """Minimal process-like object: returncode / stdout / stderr."""

    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fail_run(cmd, cwd):
    """Runner stub: ANY command fails with a stderr hint."""
    return _Proc(1, "oops", "boom: broken thing")


def _record_run(calls):
    """Return a runner that records (cmd, cwd) and always succeeds."""
    def run(cmd, cwd):
        calls.append((cmd, cwd))
        return _Proc(0, "", "")
    return run


def _ns(**kw):
    """Build an argparse.Namespace with defaults for a verify cmd."""
    defaults = dict(
        project=None, all=False, json=False, registry=None,
        run=None, state=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _write_registry(tmp_path, rows):
    """Write a registry yaml with the given project rows; return its path."""
    p = tmp_path / "registry.yaml"
    p.write_text(
        yaml.safe_dump({"projects": rows}, sort_keys=False), encoding="utf-8"
    )
    return str(p)


def _proj(name="hscc", verify_cmd: str | None = "true", repo="~/dev/hscc"):
    return registry.Project(name=name, repo=repo, verify=verify_cmd)


# --------------------------------------------------------------------------- #
# core.verify -- run_verify
# --------------------------------------------------------------------------- #

def test_run_verify_pass():
    """Exit 0 is PASS; duration is measured from the injected clock."""
    res = verify.run_verify(_proj(), _run=_ok_run)
    assert res.status == PASS
    assert res.error is None


def test_run_verify_duration_is_elapsed_time():
    """The duration is clock-after minus clock-before, an actual elapsed."""
    seq = iter([1.0, 3.5])

    def clock():
        return next(seq)

    res = verify.run_verify(_proj(), _run=_ok_run, _clock=clock)
    assert res.duration_s == 2.5


def test_run_verify_fail_records_stderr_hint():
    """Non-zero exit is FAIL and carries the stderr hint."""
    res = verify.run_verify(_proj(), _run=_fail_run)
    assert res.status == FAIL
    assert res.error == "boom: broken thing"


def test_run_verify_fail_falls_back_to_stdout_hint():
    """If stderr is empty but stdout has text, that text is the hint."""
    runner = lambda cmd, cwd: _Proc(3, "whoops lines", "")
    res = verify.run_verify(_proj(), _run=runner)
    assert res.status == FAIL
    assert res.error == "whoops lines"


def test_run_verify_no_verify_when_cmd_absent(tmp_path):
    """No verify command -> NO_VERIFY, distinct from pass and fail."""
    res = verify.run_verify(_proj(verify_cmd=None), _run=_ok_run)
    assert res.status == NO_VERIFY
    assert res.duration_s == 0.0


def test_run_verify_no_verify_never_calls_runner():
    """A no-verify project must not execute anything -- nothing runs."""
    calls = []
    verify.run_verify(_proj(verify_cmd=None), _run=_record_run(calls))
    assert calls == []


def test_run_verify_dispatch_respects_injected_runner():
    """The injected runner is what executes the command, never the shell."""
    calls = []
    res = verify.run_verify(
        _proj(verify_cmd="cd ~/dev/hscc && make test"), _run=_record_run(calls)
    )
    # _proj builds a Project directly (no _row_to_project), so repo is the
    # raw string "~/dev/hscc", which is what the runner receives.
    assert calls == [("cd ~/dev/hscc && make test", "~/dev/hscc")]
    assert res.status == PASS


# --------------------------------------------------------------------------- #
# core.verify -- state file
# --------------------------------------------------------------------------- #

def test_record_result_persists_pass_with_timestamp(tmp_path):
    """A PASS is recorded to the state file with the injected timestamp."""
    state = tmp_path / "state.yaml"
    verify.record_result(
        "hscc", verify.VerifyResult(PASS, 1.5), _now=lambda: 1000.0,
        path=str(state),
    )

    doc = yaml.safe_load(state.read_text(encoding="utf-8"))
    assert doc["verify"]["hscc"] == {"status": PASS, "timestamp": 1000.0, "duration_s": 1.5}


def test_record_result_persists_fail_with_timestamp(tmp_path):
    """A FAIL is recorded too -- a broken project is not hidden."""
    state = tmp_path / "state.yaml"
    verify.record_result(
        "hscc", verify.VerifyResult(FAIL, 0.2, "boom"), _now=lambda: 2000.0,
        path=str(state),
    )

    doc = yaml.safe_load(state.read_text(encoding="utf-8"))
    assert doc["verify"]["hscc"]["status"] == FAIL
    assert doc["verify"]["hscc"]["timestamp"] == 2000.0
    assert doc["verify"]["hscc"]["duration_s"] == 0.2


def test_state_file_round_trips(tmp_path):
    """load_state after record_result returns exactly what was written."""
    state = tmp_path / "state.yaml"
    verify.record_result(
        "a", verify.VerifyResult(PASS, 0.5), _now=lambda: 1.0, path=str(state),
    )
    verify.record_result(
        "b", verify.VerifyResult(FAIL, 2.0), _now=lambda: 2.0, path=str(state),
    )

    doc = verify.load_state(str(state))
    assert doc["verify"]["a"] == {"status": PASS, "timestamp": 1.0, "duration_s": 0.5}
    assert doc["verify"]["b"] == {"status": FAIL, "timestamp": 2.0, "duration_s": 2.0}


def test_record_result_does_not_clobber_sibling_sections(tmp_path):
    """A later command's section survives; we only touch the one key."""
    state = tmp_path / "state.yaml"
    verify.record_result("a", verify.VerifyResult(PASS, 0.1), _now=lambda: 1.0, path=str(state))

    # simulate a sibling section written by another caller
    doc = verify.load_state(str(state))
    doc["standup"] = {"config": True}
    verify.save_state(doc, str(state))

    verify.record_result("b", verify.VerifyResult(PASS, 0.2), _now=lambda: 3.0, path=str(state))
    reloaded = verify.load_state(str(state))
    assert reloaded["standup"] == {"config": True}
    assert "b" in reloaded["verify"]


def test_load_state_missing_file_is_empty(tmp_path):
    assert verify.load_state(str(tmp_path / "nope.yaml")) == {}


def test_load_state_defaults_to_home_but_we_never_write_absent(tmp_path, monkeypatch):
    """With no path we target ~/.flightdeck; tests never write there.

    This guards that the default resolves to the documented location. It does
    NOT create the file -- assert the module's default constant instead.
    """
    assert verify.DEFAULT_STATE == "~/.flightdeck/state.yaml"


def test_load_state_unparseable_file_is_empty(tmp_path):
    """A corrupt state file degrades to empty, never raises."""
    state = tmp_path / "state.yaml"
    # Genuinely invalid yaml: an unclosed flow sequence raises a ScannerError.
    state.write_text("verify: [unclosed", encoding="utf-8")
    assert verify.load_state(str(state)) == {}


# --------------------------------------------------------------------------- #
# command layer -- flightdeck verify <project>
# --------------------------------------------------------------------------- #

def test_single_pass_reports_and_records(tmp_path, capsys):
    """Single PASS prints PASS and persists the record to the state file."""
    reg = _write_registry(tmp_path, [{"name": "hscc", "repo": "~/dev/hscc", "verify": "true"}])
    state = tmp_path / "state.yaml"
    args = _ns(project="hscc", registry=reg, state=str(state), run=_ok_run)

    code = verify_cmd.run(args, reg)

    assert code == 0
    out = capsys.readouterr().out
    assert "hscc: PASS" in out
    doc = verify.load_state(str(state))
    assert doc["verify"]["hscc"]["status"] == PASS


def test_single_fail_reports_and_records_with_error(tmp_path, capsys):
    reg = _write_registry(tmp_path, [{"name": "hscc", "repo": "~/dev/hscc", "verify": "true"}])
    state = tmp_path / "state.yaml"
    args = _ns(project="hscc", registry=reg, state=str(state), run=_fail_run)

    code = verify_cmd.run(args, reg)

    assert code == 1
    out = capsys.readouterr().out
    assert "hscc: FAIL" in out
    assert "boom: broken thing" in out
    doc = verify.load_state(str(state))
    assert doc["verify"]["hscc"]["status"] == FAIL


def test_single_no_verify_is_distinct_not_pass(tmp_path, capsys):
    """A project with no verify is reported as 'no verify configured', not pass."""
    reg = _write_registry(tmp_path, [{"name": "bare", "repo": "~/dev/bare"}])
    state = tmp_path / "state.yaml"
    args = _ns(project="bare", registry=reg, state=str(state), run=_ok_run)

    code = verify_cmd.run(args, reg)

    assert code == 0  # "no verify" is not a failure, but must not read as PASS
    out = capsys.readouterr().out
    assert "bare: no verify configured" in out
    assert "PASS" not in out
    doc = verify.load_state(str(state))
    assert doc["verify"]["bare"]["status"] == NO_VERIFY


def test_single_unknown_project_errors(tmp_path, capsys):
    reg = _write_registry(tmp_path, [])
    state = tmp_path / "state.yaml"
    args = _ns(project="ghost", registry=reg, state=str(state), run=_ok_run)

    code = verify_cmd.run(args, reg)

    assert code == 2
    assert "ghost" in capsys.readouterr().err


def test_single_without_project_arg_requires_all(tmp_path, capsys):
    reg = _write_registry(tmp_path, [])
    args = _ns(project=None, registry=reg, run=_ok_run)

    code = verify_cmd.run(args, reg)

    assert code == 2
    assert "--all" in capsys.readouterr().err


def test_single_detects_project_from_cwd(tmp_path, capsys):
    """No project arg + cwd inside a registered repo -> uses that project.

    The detection note prints to stderr so the machine-readable stdout is
    unchanged, while the operator sees that the project was inferred.
    """
    repo = registry._expand("~/dev/hscc")
    # cwd is inside the repo (a subdir), NOT the real ~/dev/hscc, so no test
    # touches the real disk for a repo path we only compare as strings.
    cwd = repo + "/sub/dir"
    reg = _write_registry(tmp_path, [{"name": "hscc", "repo": "~/dev/hscc", "verify": "true"}])
    state = tmp_path / "state.yaml"
    args = _ns(project=None, registry=reg, state=str(state), run=_ok_run, cwd=cwd)

    code = verify_cmd.run(args, reg)

    assert code == 0
    captured = capsys.readouterr()
    assert "hscc: PASS" in captured.out
    assert "detected from cwd" in captured.err


def test_single_no_project_no_cwd_match_still_errors(tmp_path, capsys):
    """No project arg + cwd outside every repo -> unchanged error (no detect)."""
    reg = _write_registry(tmp_path, [{"name": "hscc", "repo": "~/dev/hscc"}])
    args = _ns(
        project=None, registry=reg, run=_ok_run,
        cwd=str(tmp_path / "elsewhere" / "deep"),
    )

    code = verify_cmd.run(args, reg)

    assert code == 2
    captured = capsys.readouterr()
    assert "--all" in captured.err
    assert "detected from cwd" not in captured.err


def test_single_explicit_arg_wins_over_cwd(tmp_path, capsys):
    """An explicit project arg beats cwd detection, with no detection note."""
    repo = registry._expand("~/dev/hscc")
    cwd = repo + "/sub"
    reg = _write_registry(
        tmp_path,
        [
            {"name": "hscc", "repo": "~/dev/hscc", "verify": "true"},
            {"name": "other", "repo": "~/dev/other", "verify": "true"},
        ],
    )
    state = tmp_path / "state.yaml"
    # cwd matches hscc, but the user explicitly typed "other".
    args = _ns(project="other", registry=reg, state=str(state), run=_ok_run, cwd=cwd)

    code = verify_cmd.run(args, reg)

    assert code == 0
    captured = capsys.readouterr()
    assert "other: PASS" in captured.out
    assert "detected from cwd" not in captured.err
    assert "hscc" not in captured.out


def test_single_json_output(tmp_path, capsys):
    reg = _write_registry(tmp_path, [{"name": "hscc", "repo": "~/dev/hscc", "verify": "true"}])
    state = tmp_path / "state.yaml"
    args = _ns(project="hscc", registry=reg, state=str(state), run=_ok_run, json=True)

    code = verify_cmd.run(args, reg)

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["project"] == "hscc"
    assert payload["status"] == PASS
    assert payload["duration_s"] >= 0.0


# --------------------------------------------------------------------------- #
# command layer -- flightdeck verify --all
# --------------------------------------------------------------------------- #

def test_all_summarises_mixed_pass_fail(tmp_path, capsys):
    """--all runs every project and summarises; any FAIL -> exit 1."""
    rows = [
        {"name": "good", "repo": "~/dev/good", "verify": "true"},
        {"name": "broken", "repo": "~/dev/broken", "verify": "true"},
        {"name": "plain", "repo": "~/dev/plain"},  # no verify
    ]
    reg = _write_registry(tmp_path, rows)
    state = tmp_path / "state.yaml"

    # fail only the "broken" project by name
    def run(cmd, cwd):
        if cwd == registry._expand("~/dev/broken") or "broken" in cwd:
            return _Proc(1, "", "eh")
        return _Proc(0, "", "")

    args = _ns(all=True, project=None, registry=reg, state=str(state), run=run)

    code = verify_cmd.run(args, reg)

    assert code == 1  # a FAIL anywhere fails the whole command
    out = capsys.readouterr().out
    assert "good" in out and "PASS" in out
    assert "broken" in out and "FAIL" in out
    assert "no verify configured" in out
    assert "1 passed, 1 failed, 1 no verify configured" in out

    # every project recorded, including the no-verify one (distinct)
    doc = verify.load_state(str(state))
    assert doc["verify"]["good"]["status"] == PASS
    assert doc["verify"]["broken"]["status"] == FAIL
    assert doc["verify"]["plain"]["status"] == NO_VERIFY


def test_all_all_pass_exits_zero(tmp_path, capsys):
    rows = [
        {"name": "a", "repo": "~/dev/a", "verify": "true"},
        {"name": "b", "repo": "~/dev/b", "verify": "true"},
    ]
    reg = _write_registry(tmp_path, rows)
    args = _ns(all=True, project=None, registry=reg, state=str(tmp_path / "s.yaml"), run=_ok_run)

    code = verify_cmd.run(args, reg)

    assert code == 0
    out = capsys.readouterr().out
    assert "2 passed, 0 failed, 0 no verify configured" in out


def test_all_no_verify_is_not_counted_as_pass(tmp_path, capsys):
    """A registry of only no-verify projects reports 0 passed, not all passed."""
    rows = [{"name": "a", "repo": "~/dev/a"}, {"name": "b", "repo": "~/dev/b"}]
    reg = _write_registry(tmp_path, rows)
    args = _ns(all=True, project=None, registry=reg, state=str(tmp_path / "s.yaml"), run=_ok_run)

    code = verify_cmd.run(args, reg)

    assert code == 0  # nothing failing, but nothing passed either
    out = capsys.readouterr().out
    assert "0 passed, 0 failed, 2 no verify configured" in out
    assert out.count("no verify configured") == 3  # two rows + summary


def test_all_empty_registry(tmp_path, capsys):
    reg = _write_registry(tmp_path, [])
    args = _ns(all=True, project=None, registry=reg, state=str(tmp_path / "s.yaml"), run=_ok_run)

    code = verify_cmd.run(args, reg)

    assert code == 0
    assert "no projects" in capsys.readouterr().out


def test_all_json_output(tmp_path, capsys):
    rows = [
        {"name": "broken", "repo": "~/dev/broken", "verify": "true"},
        {"name": "good", "repo": "~/dev/good", "verify": "true"},
    ]
    reg = _write_registry(tmp_path, rows)
    state = tmp_path / "state.yaml"

    def run(cmd, cwd):
        return _Proc(1, "", "eh") if "broken" in cwd else _Proc(0, "", "")

    args = _ns(all=True, registry=reg, state=str(state), run=run, json=True)

    code = verify_cmd.run(args, reg)

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    statuses = {o["project"]: o["status"] for o in payload}
    assert statuses == {"broken": FAIL, "good": PASS}


# --------------------------------------------------------------------------- #
# build_subparser wiring
# --------------------------------------------------------------------------- #

def test_build_subparser_and_run_hook_present():
    """The module must expose the two-discovery hooks for cli.py."""
    assert hasattr(verify_cmd, "build_subparser")
    assert hasattr(verify_cmd, "run")


def test_subparser_accepts_project_and_all(capsys):
    """The subparser wires up --all and the positional project argument."""
    import argparse

    parser = argparse.ArgumentParser(prog="flightdeck")
    sub = parser.add_subparsers(dest="command")
    verify_cmd.build_subparser(sub)
    ns = parser.parse_args(["verify", "--all"])
    assert ns.command == "verify"
    assert ns.all is True
    ns2 = parser.parse_args(["verify", "hscc"])
    assert ns2.project == "hscc"
    assert ns2.all is False


def test_discovery_loads_verify_module():
    """cli._discover_commands finds verify via its run/build_subparser hooks."""
    from flightdeck.cli import _discover_commands

    mods = _discover_commands()
    assert "verify" in mods
