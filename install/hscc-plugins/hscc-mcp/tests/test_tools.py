from unittest import mock

from hscc_mcp import tools


def _ok(json_obj=None, stdout="ok"):
    return {"ok": True, "exit_code": 0, "stdout": stdout,
            "stderr": "", "json": json_obj, "error": None}


def test_cluster_status_calls_cluster_plugin():
    with mock.patch.object(tools, "run_hscc", return_value=_ok(stdout="WORKLOADS")) as m:
        out = tools.cluster_status()
    m.assert_called_once_with("hscc-cluster", "cluster-status")
    assert "WORKLOADS" in out


def test_fleet_activity_uses_json_flag_and_returns_parsed():
    payload = {"agents": [{"agent": "dev-002", "node": ".247"}]}
    with mock.patch.object(tools, "run_hscc", return_value=_ok(json_obj=payload)) as m:
        out = tools.fleet_activity()
    m.assert_called_once_with("hscc-agent-coordinator", "fleet-activity", "--json")
    assert out == payload


def test_projects_show_calls_projects_plugin():
    with mock.patch.object(tools, "run_hscc", return_value=_ok(stdout="proj")) as m:
        tools.projects_show()
    m.assert_called_once_with("hscc-projects", "show")


def test_task_status_passes_task_id():
    with mock.patch.object(tools, "run_hscc", return_value=_ok(json_obj={"s": 1})) as m:
        tools.task_status("t_abc")
    m.assert_called_once_with("hscc-agent-coordinator", "task-status", "t_abc")


def test_project_create_passes_name_and_desc():
    with mock.patch.object(tools, "run_hscc", return_value=_ok(json_obj={"id": "P1"})) as m:
        out = tools.project_create("MyProj", "the description")
    m.assert_called_once_with("hscc-projects", "create", "MyProj", "the description")
    assert out == {"id": "P1"}


def test_task_add_passes_roadmap_subproject_title_desc():
    with mock.patch.object(tools, "run_hscc", return_value=_ok(json_obj={"task": "t1"})) as m:
        tools.task_add("Roadmap A", "Sub B", "Do the thing", "details")
    m.assert_called_once_with(
        "hscc-projects", "add-task", "Roadmap A", "Sub B", "Do the thing", "details"
    )


def test_dispatch_task_passes_task_id_only():
    with mock.patch.object(tools, "run_hscc", return_value=_ok(json_obj={"blocked": True})) as m:
        tools.dispatch_task("t_xyz")
    m.assert_called_once_with("hscc-agent-coordinator", "dispatch-task", "t_xyz")


def test_release_task_refuses_without_confirm():
    with mock.patch.object(tools, "run_hscc") as m:
        out = tools.release_task("t_xyz")  # confirm defaults False
    m.assert_not_called()
    assert out["needs_confirmation"] is True
    assert "confirm" in out["error"].lower()


def test_release_task_runs_with_confirm_true():
    with mock.patch.object(tools, "run_hscc", return_value=_ok(json_obj={"released": True})) as m:
        out = tools.release_task("t_xyz", confirm=True)
    m.assert_called_once_with("hscc-agent-coordinator", "release-task", "t_xyz")
    assert out == {"released": True}


def test_cancel_task_refuses_without_confirm():
    with mock.patch.object(tools, "run_hscc") as m:
        out = tools.cancel_task("t_xyz")
    m.assert_not_called()
    assert out["needs_confirmation"] is True


def test_merge_worktree_refuses_without_confirm():
    with mock.patch.object(tools, "run_hscc") as m:
        out = tools.merge_worktree("t_xyz")
    m.assert_not_called()
    assert out["needs_confirmation"] is True


def test_merge_worktree_runs_with_confirm_true():
    with mock.patch.object(tools, "run_hscc", return_value=_ok(json_obj={"merged": True})) as m:
        tools.merge_worktree("t_xyz", confirm=True)
    m.assert_called_once_with("hscc-agent-coordinator", "merge-worktree", "t_xyz")


def test_green_check_is_not_gated():
    with mock.patch.object(tools, "run_hscc", return_value=_ok(json_obj={"green": True})) as m:
        tools.green_check("t_xyz")
    m.assert_called_once_with("hscc-agent-coordinator", "green-check", "t_xyz")


def test_remove_worktree_refuses_without_confirm():
    with mock.patch.object(tools, "run_hscc") as m:
        out = tools.remove_worktree("t_xyz")
    m.assert_not_called()
    assert out["needs_confirmation"] is True
