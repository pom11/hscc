import json
import subprocess
from unittest import mock

from hscc_mcp import runner


def test_run_hscc_builds_correct_argv_and_parses_json():
    fake = subprocess.CompletedProcess(
        args=[], returncode=0, stdout='{"ok": true, "n": 3}', stderr=""
    )
    with mock.patch("subprocess.run", return_value=fake) as m:
        res = runner.run_hscc("hscc-projects", "create", "MyProj", "a desc")

    called_argv = m.call_args[0][0]
    # python interpreter, plugin hscc.py path, then the subcommand + args
    assert called_argv[1].endswith("hscc-projects/hscc.py")
    assert called_argv[2:] == ["create", "MyProj", "a desc"]
    assert res["ok"] is True
    assert res["exit_code"] == 0
    assert res["json"] == {"ok": True, "n": 3}
    assert res["stdout"] == '{"ok": true, "n": 3}'


def test_run_hscc_non_json_stdout_returns_raw_with_json_none():
    fake = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="WORKLOADS\n  solo  192.0.2.10", stderr=""
    )
    with mock.patch("subprocess.run", return_value=fake):
        res = runner.run_hscc("hscc-cluster", "cluster-status")
    assert res["ok"] is True
    assert res["json"] is None
    assert "WORKLOADS" in res["stdout"]


def test_run_hscc_nonzero_exit_marks_not_ok():
    fake = subprocess.CompletedProcess(
        args=[], returncode=2, stdout='{"error": "boom"}', stderr="trace"
    )
    with mock.patch("subprocess.run", return_value=fake):
        res = runner.run_hscc("hscc-projects", "delete", "X")
    assert res["ok"] is False
    assert res["exit_code"] == 2
    assert res["json"] == {"error": "boom"}
    assert res["stderr"] == "trace"


def test_run_hscc_timeout_returns_error_dict_not_raises():
    with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("c", 60)):
        res = runner.run_hscc("hscc-cluster", "cluster-status")
    assert res["ok"] is False
    assert res["json"] is None
    assert "timeout" in res["error"].lower()
