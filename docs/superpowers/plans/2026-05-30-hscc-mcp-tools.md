# HSCC MCP Toolset + Native Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose HSCC operations as first-class typed MCP tools so the orchestrator naturally dispatches work to the fleet, and replace the bypassable custom route-guard with default Hermes' native dangerous-command approval.

**Architecture:** A stdio MCP server (`hscc-mcp/`) run by the hermes venv python. Each tool is a thin wrapper that shells out to the existing `hscc-*/hscc.py` CLI plugins and returns their JSON — a typed facade, zero rewrite of working plugins. Risky tools (release/cancel/merge) gate on a required `confirm` parameter. Phase 1 removes the guard hook and switches to native approval (config-only); Phase 2 builds and wires the MCP server.

**Tech Stack:** Python 3.11 (`~/.hermes/hermes-agent/venv`), `mcp` 1.26.0 (`mcp.server.fastmcp.FastMCP`), pytest 9.0.2, stdlib `subprocess`/`json`/`pathlib`.

> **Commit policy (user override):** The user's standing rule is NEVER commit unless explicitly asked. Each task below ends with a commit step per methodology, but the executor MUST get explicit user approval before running any `git commit`. If approval is withheld, leave changes staged and continue.

> **Repo:** all paths under `~/.hermes/plugins` (the `pom11/hscc` git repo). Dual-layout rule: every file created under `hscc-mcp/` is mirrored to `install/hscc-plugins/hscc-mcp/` (Task 8).

---

## File Structure

- `hscc-mcp/runner.py` — `run_hscc()` shell-out helper. Pure, mockable. One responsibility: invoke a plugin CLI and return structured result.
- `hscc-mcp/tools.py` — plain python tool functions (read / write / risky-with-confirm). Calls `run_hscc`. Unit-tested by monkeypatching the runner.
- `hscc-mcp/server.py` — FastMCP assembly: registers each `tools.py` function as an MCP tool, runs stdio. Thin integration layer (not unit-tested).
- `hscc-mcp/tests/test_runner.py` — runner unit tests (argv construction, JSON parse, error handling).
- `hscc-mcp/tests/test_tools.py` — tool unit tests (correct CLI mapping, confirm-gate refusal).
- `hscc-mcp/__init__.py`, `hscc-mcp/tests/__init__.py` — package markers.

Config edits (no new files): `~/.hermes/config.yaml`, `~/.hermes/SOUL.md`.

---

## Phase 1 — Config-only relief (remove guard, native approval)

### Task 1: Remove the route-guard hook and switch to native approval

**Files:**
- Modify: `~/.hermes/config.yaml` (`hooks.pre_tool_call`, `approvals.mode`)
- Modify: `~/.hermes/SOUL.md` (routing prose → light nudge)
- Move: `~/.hermes/hooks/route-guard.py` → `~/.hermes/hooks/route-guard.py.disabled-20260530`

- [ ] **Step 1: Back up the guard hook (move, not delete)**

Run:
```bash
mv ~/.hermes/hooks/route-guard.py ~/.hermes/hooks/route-guard.py.disabled-20260530
```
Expected: file moved; `ls ~/.hermes/hooks/route-guard.py` → "No such file".

- [ ] **Step 2: Remove the `hooks.pre_tool_call` route-guard entry from config**

In `~/.hermes/config.yaml`, the current block is:
```yaml
hooks:
  pre_tool_call:
  - matcher: terminal
    command: python3 ~/.hermes/hooks/route-guard.py
    timeout: 5
```
Replace it with:
```yaml
hooks:
  pre_tool_call: []
```

- [ ] **Step 3: Switch approvals to smart mode**

In `~/.hermes/config.yaml`, under `approvals:`, change:
```yaml
  mode: manual
```
to:
```yaml
  mode: smart
```

- [ ] **Step 4: Revert SOUL.md routing prose to a light nudge**

In `~/.hermes/SOUL.md`, the strict "EXECUTION ROUTING — MANDATORY" section forbids inline work and references the guard. Replace the body of that section (keep the heading) with:
```markdown
## Execution routing

To run project work (coding, data, builds, remote-host ops), prefer the `hscc_*`
tools — they dispatch it to the fleet (worker nodes), which is where real work
belongs. Quick read-only checks inline are fine. Heavy or destructive inline
commands will trigger an approval prompt to the user; surface those rather than
working around them.
```
Leave the "Brevity contract" and "Safety" sections unchanged.

- [ ] **Step 5: Validate config YAML**

Run:
```bash
~/.hermes/hermes-agent/venv/bin/python -c "import yaml; yaml.safe_load(open('~/.hermes/config.yaml')); print('YAML OK')"
```
Expected: `YAML OK`

- [ ] **Step 6: Restart gateway and confirm (fleet must be idle first)**

Run:
```bash
python3 ~/.hermes/plugins/hscc-agent-coordinator/hscc.py fleet-activity --json
```
Expected: confirm 0 active agents. THEN (state intent to user, get OK — restart kills any running agents):
```bash
~/.local/bin/hermes gateway restart && sleep 6 && launchctl list | grep hermes.gateway
```
Expected: `✓ Service restarted` and a PID line.

- [ ] **Step 7: Verify guard is gone (the old false-positive now runs)**

After restart, via a Telegram message to Hermes ask it to run `gh repo list 2>/dev/null | head -1 || echo none`. Expected: it executes (no "BLOCKED by HSCC route-guard"). If `gh` unauthenticated, `none` is fine — the point is no guard block.

- [ ] **Step 8: Commit (await user approval per commit policy)**

```bash
cd ~/.hermes/plugins
git add ../config.yaml ../SOUL.md 2>/dev/null || true
git commit -m "chore: retire route-guard hook in favor of native approval (smart mode)"
```
Note: config.yaml/SOUL.md live in `~/.hermes/`, outside the plugins repo. If they are not tracked there, skip the git step for them and only record the change in the spec — do NOT force-add files outside the repo.

---

## Phase 2 — Build and wire the HSCC MCP server

### Task 2: Package scaffold + `run_hscc` shell-out helper (TDD)

**Files:**
- Create: `hscc-mcp/__init__.py`
- Create: `hscc-mcp/tests/__init__.py`
- Create: `hscc-mcp/runner.py`
- Test: `hscc-mcp/tests/test_runner.py`

- [ ] **Step 1: Create package markers**

Run:
```bash
mkdir -p ~/.hermes/plugins/hscc-mcp/tests
touch ~/.hermes/plugins/hscc-mcp/__init__.py ~/.hermes/plugins/hscc-mcp/tests/__init__.py
```

- [ ] **Step 2: Write the failing test**

Create `hscc-mcp/tests/test_runner.py`:
```python
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
```

- [ ] **Step 3: Run test to verify it fails**

Run:
```bash
cd ~/.hermes/plugins/hscc-mcp && ~/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_runner.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'hscc_mcp'` / `runner`.

Note: the dir is `hscc-mcp` (hyphen) but the import is `hscc_mcp` (underscore). Resolve by running pytest from a `conftest.py` that adds the package, OR rename import. Use **Step 3b** below.

- [ ] **Step 3b: Add conftest to expose the hyphenated dir as importable `hscc_mcp`**

Create `hscc-mcp/tests/conftest.py`:
```python
import importlib.util
import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent  # .../hscc-mcp


def _load(mod_name, filename):
    spec = importlib.util.spec_from_file_location(
        f"hscc_mcp.{mod_name}", _PKG_DIR / filename
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[f"hscc_mcp.{mod_name}"] = module
    return module


# Register a synthetic package so `from hscc_mcp import runner` works despite the
# hyphenated directory name (not a valid Python identifier).
if "hscc_mcp" not in sys.modules:
    import types
    pkg = types.ModuleType("hscc_mcp")
    pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["hscc_mcp"] = pkg

_load("runner", "runner.py")
```

- [ ] **Step 4: Write minimal implementation**

Create `hscc-mcp/runner.py`:
```python
"""Shell-out helper: invoke an HSCC CLI plugin and return a structured result."""
import json
import subprocess
import sys
from pathlib import Path

# hscc-mcp/ -> parent is plugins/, where the hscc-* plugins live.
PLUGINS_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TIMEOUT = 60


def _plugin_script(plugin: str) -> str:
    return str(PLUGINS_DIR / plugin / "hscc.py")


def run_hscc(plugin: str, *args: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run ``python <plugin>/hscc.py <args...>`` and return a structured dict.

    Returns keys: ok(bool), exit_code(int|None), stdout(str), stderr(str),
    json(parsed|None), error(str|None).
    """
    argv = [sys.executable, _plugin_script(plugin), *[str(a) for a in args]]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "exit_code": None, "stdout": "", "stderr": "",
            "json": None, "error": f"timeout after {timeout}s running {plugin} {args}",
        }
    except FileNotFoundError as exc:
        return {
            "ok": False, "exit_code": None, "stdout": "", "stderr": "",
            "json": None, "error": f"plugin not found: {exc}",
        }

    parsed = None
    stdout = proc.stdout or ""
    try:
        parsed = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        parsed = None

    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": proc.stderr or "",
        "json": parsed,
        "error": None,
    }
```

- [ ] **Step 5: Run test to verify it passes**

Run:
```bash
cd ~/.hermes/plugins/hscc-mcp && ~/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_runner.py -v
```
Expected: 4 passed.

- [ ] **Step 6: Commit (await user approval)**

```bash
cd ~/.hermes/plugins && git add hscc-mcp/__init__.py hscc-mcp/runner.py hscc-mcp/tests/
git commit -m "feat(hscc-mcp): add run_hscc shell-out helper with tests"
```

---

### Task 3: Read-only tools (TDD)

**Files:**
- Create: `hscc-mcp/tools.py`
- Test: `hscc-mcp/tests/test_tools.py`

- [ ] **Step 1: Write the failing test**

Create `hscc-mcp/tests/test_tools.py`:
```python
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
```

- [ ] **Step 2: Add `tools` loading to conftest**

In `hscc-mcp/tests/conftest.py`, after the `_load("runner", "runner.py")` line add:
```python
_load("tools", "tools.py")
```

- [ ] **Step 3: Run test to verify it fails**

Run:
```bash
cd ~/.hermes/plugins/hscc-mcp && ~/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_tools.py -v
```
Expected: FAIL — no module `tools` / attribute errors.

- [ ] **Step 4: Write minimal implementation**

Create `hscc-mcp/tools.py`:
```python
"""HSCC tool functions — typed facades over the CLI plugins.

Each returns either parsed JSON (when the plugin emits JSON) or raw stdout text.
Risky tools require ``confirm=True`` (added in a later task).
"""
from hscc_mcp.runner import run_hscc

COORD = "hscc-agent-coordinator"
PROJECTS = "hscc-projects"
CLUSTER = "hscc-cluster"


def _result(res: dict):
    """Prefer parsed JSON; fall back to raw stdout; surface errors."""
    if res.get("error"):
        return {"error": res["error"]}
    if res.get("json") is not None:
        return res["json"]
    return res.get("stdout", "")


def cluster_status():
    return _result(run_hscc(CLUSTER, "cluster-status"))


def fleet_activity():
    return _result(run_hscc(COORD, "fleet-activity", "--json"))


def projects_show():
    return _result(run_hscc(PROJECTS, "show"))


def task_status(task_id: str):
    return _result(run_hscc(COORD, "task-status", task_id))
```

- [ ] **Step 5: Run test to verify it passes**

Run:
```bash
cd ~/.hermes/plugins/hscc-mcp && ~/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_tools.py -v
```
Expected: 4 passed.

- [ ] **Step 6: Commit (await user approval)**

```bash
cd ~/.hermes/plugins && git add hscc-mcp/tools.py hscc-mcp/tests/test_tools.py hscc-mcp/tests/conftest.py
git commit -m "feat(hscc-mcp): add read-only tools (cluster/fleet/projects/task status)"
```

---

### Task 4: Low-risk write tools (TDD)

**Files:**
- Modify: `hscc-mcp/tools.py`
- Modify: `hscc-mcp/tests/test_tools.py`

- [ ] **Step 1: Write the failing test (append to test_tools.py)**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd ~/.hermes/plugins/hscc-mcp && ~/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_tools.py -k "create or task_add or dispatch" -v
```
Expected: FAIL — attributes not defined.

- [ ] **Step 3: Write minimal implementation (append to tools.py)**

```python
def project_create(name: str, description: str = ""):
    return _result(run_hscc(PROJECTS, "create", name, description))


def task_add(roadmap: str, subproject: str, title: str, description: str = ""):
    return _result(run_hscc(PROJECTS, "add-task", roadmap, subproject, title, description))


def dispatch_task(task_id: str):
    """Pre-create the worktree + a BLOCKED kanban card. Nothing runs until release."""
    return _result(run_hscc(COORD, "dispatch-task", task_id))
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
cd ~/.hermes/plugins/hscc-mcp && ~/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_tools.py -v
```
Expected: all passed.

- [ ] **Step 5: Commit (await user approval)**

```bash
cd ~/.hermes/plugins && git add hscc-mcp/tools.py hscc-mcp/tests/test_tools.py
git commit -m "feat(hscc-mcp): add low-risk write tools (project/task/dispatch)"
```

---

### Task 5: Risky gated tools with `confirm` (TDD)

**Files:**
- Modify: `hscc-mcp/tools.py`
- Modify: `hscc-mcp/tests/test_tools.py`

- [ ] **Step 1: Write the failing test (append to test_tools.py)**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd ~/.hermes/plugins/hscc-mcp && ~/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_tools.py -k "release or cancel or merge or green or remove" -v
```
Expected: FAIL — attributes not defined.

- [ ] **Step 3: Write minimal implementation (append to tools.py)**

```python
def _require_confirm(action: str, confirm: bool):
    if not confirm:
        return {
            "needs_confirmation": True,
            "error": (
                f"Refused: '{action}' is a live fleet operation. Ask the user to "
                f"approve, then call again with confirm=true."
            ),
        }
    return None


def release_task(task_id: str, confirm: bool = False):
    """Unblock a dispatched task → gateway spawns a live worker. GATED."""
    gate = _require_confirm("release_task", confirm)
    if gate:
        return gate
    return _result(run_hscc(COORD, "release-task", task_id))


def cancel_task(task_id: str, confirm: bool = False):
    """Cancel a live task/worker. GATED."""
    gate = _require_confirm("cancel_task", confirm)
    if gate:
        return gate
    return _result(run_hscc(COORD, "cancel-task", task_id))


def merge_worktree(task_id: str, confirm: bool = False):
    """Merge a task's worktree branch into the project default branch. GATED."""
    gate = _require_confirm("merge_worktree", confirm)
    if gate:
        return gate
    return _result(run_hscc(COORD, "merge-worktree", task_id))


def remove_worktree(task_id: str, confirm: bool = False):
    """Remove a task's worktree. GATED."""
    gate = _require_confirm("remove_worktree", confirm)
    if gate:
        return gate
    return _result(run_hscc(COORD, "remove-worktree", task_id))


def green_check(task_id: str):
    """Read-only readiness check before merge. Not gated."""
    return _result(run_hscc(COORD, "green-check", task_id))
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
cd ~/.hermes/plugins/hscc-mcp && ~/.hermes/hermes-agent/venv/bin/python -m pytest tests/ -v
```
Expected: all passed (runner + tools).

- [ ] **Step 5: Commit (await user approval)**

```bash
cd ~/.hermes/plugins && git add hscc-mcp/tools.py hscc-mcp/tests/test_tools.py
git commit -m "feat(hscc-mcp): add confirm-gated risky tools (release/cancel/merge/remove)"
```

---

### Task 6: FastMCP server assembly + entrypoint

**Files:**
- Create: `hscc-mcp/server.py`

- [ ] **Step 1: Write the server**

Create `hscc-mcp/server.py`:
```python
"""HSCC MCP server — registers HSCC operations as typed MCP tools over stdio.

Run by Hermes via config ``mcp_servers.hscc`` with the hermes venv python.
Tools are thin facades over the hscc-* CLI plugins (see tools.py / runner.py).
"""
import importlib.util
import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent  # .../hscc-mcp


def _load(mod_name, filename):
    spec = importlib.util.spec_from_file_location(
        f"hscc_mcp.{mod_name}", _PKG_DIR / filename
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[f"hscc_mcp.{mod_name}"] = module
    return module


# Synthetic package so intra-package imports resolve despite the hyphen dir name.
if "hscc_mcp" not in sys.modules:
    import types
    _pkg = types.ModuleType("hscc_mcp")
    _pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["hscc_mcp"] = _pkg

_load("runner", "runner.py")
tools = _load("tools", "tools.py")

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("hscc")


@mcp.tool()
def hscc_cluster_status() -> object:
    """DGX Spark cluster status: running workloads and idle hosts."""
    return tools.cluster_status()


@mcp.tool()
def hscc_fleet_activity() -> object:
    """Per-agent live fleet activity (agent -> task -> kanban -> node)."""
    return tools.fleet_activity()


@mcp.tool()
def hscc_projects_show() -> object:
    """Active project roadmaps and tasks."""
    return tools.projects_show()


@mcp.tool()
def hscc_task_status(task_id: str) -> object:
    """Status of a dispatched task by id."""
    return tools.task_status(task_id)


@mcp.tool()
def hscc_project_create(name: str, description: str = "") -> object:
    """Create an HSCC project (auto-provisions a git repo + kanban board)."""
    return tools.project_create(name, description)


@mcp.tool()
def hscc_task_add(roadmap: str, subproject: str, title: str, description: str = "") -> object:
    """Add a task under a roadmap/subproject."""
    return tools.task_add(roadmap, subproject, title, description)


@mcp.tool()
def hscc_dispatch_task(task_id: str) -> object:
    """Pre-create a git worktree + a BLOCKED kanban card. Nothing runs until release."""
    return tools.dispatch_task(task_id)


@mcp.tool()
def hscc_release_task(task_id: str, confirm: bool = False) -> object:
    """Unblock a dispatched task so a worker runs it on its node. Ask the user
    first, then call with confirm=true."""
    return tools.release_task(task_id, confirm)


@mcp.tool()
def hscc_cancel_task(task_id: str, confirm: bool = False) -> object:
    """Cancel a live task/worker. Ask the user first, then confirm=true."""
    return tools.cancel_task(task_id, confirm)


@mcp.tool()
def hscc_green_check(task_id: str) -> object:
    """Read-only readiness check before merging a task's worktree."""
    return tools.green_check(task_id)


@mcp.tool()
def hscc_merge_worktree(task_id: str, confirm: bool = False) -> object:
    """Merge a task's worktree branch into the project default branch. Ask the
    user first, then confirm=true."""
    return tools.merge_worktree(task_id, confirm)


@mcp.tool()
def hscc_remove_worktree(task_id: str, confirm: bool = False) -> object:
    """Remove a task's worktree. Ask the user first, then confirm=true."""
    return tools.remove_worktree(task_id, confirm)


if __name__ == "__main__":
    mcp.run()
```

- [ ] **Step 2: Smoke-test the server imports and registers tools**

Run:
```bash
~/.hermes/hermes-agent/venv/bin/python -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('srv', '~/.hermes/plugins/hscc-mcp/server.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
names = [t.name for t in __import__('asyncio').get_event_loop().run_until_complete(m.mcp.list_tools())]
print(sorted(names))
"
```
Expected: a sorted list containing `hscc_cluster_status`, `hscc_dispatch_task`, `hscc_release_task`, `hscc_merge_worktree`, etc. (12 tools). If the asyncio one-liner is awkward, instead just assert import succeeds:
```bash
~/.hermes/hermes-agent/venv/bin/python -c "import importlib.util; s=importlib.util.spec_from_file_location('srv','~/.hermes/plugins/hscc-mcp/server.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print('server import OK, mcp name:', m.mcp.name)"
```
Expected: `server import OK, mcp name: hscc`

- [ ] **Step 3: Live read-only smoke test against the real cluster**

Run (this actually shells out to the real plugin — read-only, safe):
```bash
~/.hermes/hermes-agent/venv/bin/python -c "
import importlib.util,sys
s=importlib.util.spec_from_file_location('srv','~/.hermes/plugins/hscc-mcp/server.py')
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
import json; print(json.dumps(m.tools.fleet_activity())[:300])
"
```
Expected: JSON beginning with the fleet-activity payload (same shape as the CLI).

- [ ] **Step 4: Commit (await user approval)**

```bash
cd ~/.hermes/plugins && git add hscc-mcp/server.py
git commit -m "feat(hscc-mcp): FastMCP server exposing 12 HSCC tools over stdio"
```

---

### Task 7: Wire `mcp_servers.hscc` into Hermes config + restart

**Files:**
- Modify: `~/.hermes/config.yaml` (add top-level `mcp_servers`)

- [ ] **Step 1: Add the MCP server entry**

In `~/.hermes/config.yaml`, add a top-level block (there is no existing `mcp_servers:` key — `mcp_servers` is distinct from the `auxiliary.mcp` and `delegation.inherit_mcp_toolsets` keys):
```yaml
mcp_servers:
  hscc:
    command: ~/.hermes/hermes-agent/venv/bin/python
    args:
    - ~/.hermes/plugins/hscc-mcp/server.py
    timeout: 60
```

- [ ] **Step 2: Validate YAML**

Run:
```bash
~/.hermes/hermes-agent/venv/bin/python -c "import yaml; c=yaml.safe_load(open('~/.hermes/config.yaml')); print('hscc' in c.get('mcp_servers',{}))"
```
Expected: `True`

- [ ] **Step 3: Restart gateway (confirm fleet idle + user OK first)**

Run:
```bash
python3 ~/.hermes/plugins/hscc-agent-coordinator/hscc.py fleet-activity --json
```
Confirm 0 active. Then (state intent, get user OK):
```bash
~/.local/bin/hermes gateway restart && sleep 8 && launchctl list | grep hermes.gateway
```
Expected: `✓ Service restarted` + PID.

- [ ] **Step 4: Verify the MCP server is registered**

Run:
```bash
~/.local/bin/hermes mcp list 2>/dev/null | grep -i hscc || grep -i "mcp.*hscc\|hscc.*tool" ~/.hermes/logs/gateway.log | tail -5
```
Expected: `hscc` appears in the MCP server list, or gateway log shows the hscc tools loaded.

- [ ] **Step 5: Commit (await user approval — config outside repo, see Task 1 note)**

Record the config change; only `git add` it if `~/.hermes/config.yaml` is tracked in the plugins repo (it is not by default — skip force-add).

---

### Task 8: Sync to install template (dual-layout rule)

**Files:**
- Create (mirror): `install/hscc-plugins/hscc-mcp/` (all of `hscc-mcp/` except `tests/` caches)

- [ ] **Step 1: Copy the package into the template tree**

Run:
```bash
mkdir -p ~/.hermes/plugins/install/hscc-plugins/hscc-mcp
cp -R ~/.hermes/plugins/hscc-mcp/__init__.py \
      ~/.hermes/plugins/hscc-mcp/runner.py \
      ~/.hermes/plugins/hscc-mcp/tools.py \
      ~/.hermes/plugins/hscc-mcp/server.py \
      ~/.hermes/plugins/install/hscc-plugins/hscc-mcp/
mkdir -p ~/.hermes/plugins/install/hscc-plugins/hscc-mcp/tests
cp -R ~/.hermes/plugins/hscc-mcp/tests/__init__.py \
      ~/.hermes/plugins/hscc-mcp/tests/conftest.py \
      ~/.hermes/plugins/hscc-mcp/tests/test_runner.py \
      ~/.hermes/plugins/hscc-mcp/tests/test_tools.py \
      ~/.hermes/plugins/install/hscc-plugins/hscc-mcp/tests/
```

- [ ] **Step 2: Verify the two trees match**

Run:
```bash
diff -rq ~/.hermes/plugins/hscc-mcp ~/.hermes/plugins/install/hscc-plugins/hscc-mcp \
  --exclude='__pycache__' --exclude='.pytest_cache'
```
Expected: no output (identical).

- [ ] **Step 3: Commit (await user approval)**

```bash
cd ~/.hermes/plugins && git add install/hscc-plugins/hscc-mcp
git commit -m "chore(hscc-mcp): sync active plugin to install template"
```

---

### Task 9: End-to-end verification (live, read-only + gated)

- [ ] **Step 1: Tools visible to Hermes**

Via Telegram, ask Hermes: "list your hscc tools". Expected: it reports the `hscc_*` tools (or calls `hscc_fleet_activity` directly).

- [ ] **Step 2: Read tool parity**

Ask Hermes to run `hscc_fleet_activity`. Expected: same content as `hscc_activity` quick command / the CLI.

- [ ] **Step 3: Confirm-gate works**

Ask Hermes to release some task id WITHOUT you approving. Expected: the tool returns `needs_confirmation` and Hermes asks you first, rather than releasing.

- [ ] **Step 4: Guard-removal regression**

Ask Hermes to run a benign piped read (`gh repo list | head` or `ls ~ | grep hermes`). Expected: runs, no `BLOCKED by HSCC route-guard`.

- [ ] **Step 5: Orchestrator vLLM untouched**

Run:
```bash
curl -sf http://192.0.2.10:8000/v1/models >/dev/null && echo ".244 vLLM OK"
```
Expected: `.244 vLLM OK`.

---

## Self-Review

**Spec coverage:**
- Native approval / guard removal → Task 1. ✔
- HSCC MCP server location + runtime + dual-layout → Tasks 2–8. ✔
- Read tools (cluster/fleet/projects/task_status) → Task 3. ✔
- Write tools (create/add-task/dispatch) → Task 4. ✔
- Risky gated tools (release/cancel/merge/remove) + green_check → Task 5. ✔
- FastMCP assembly + stdio → Task 6. ✔
- `mcp_servers.hscc` wiring + restart → Task 7. ✔
- Verification list → Task 9. ✔
- Phased rollout (Phase 1 config-only, Phase 2 server) → section split. ✔

**Placeholder scan:** No TBD/TODO; all code shown in full; all commands have expected output. ✔

**Type/name consistency:** Function names match across tools.py, tests, and server.py wrappers: `cluster_status`, `fleet_activity`, `projects_show`, `task_status`, `project_create`, `task_add`, `dispatch_task`, `release_task`, `cancel_task`, `merge_worktree`, `remove_worktree`, `green_check`. CLI subcommands verified against the live plugins (`hscc-projects`: create/add-task/show; `hscc-agent-coordinator`: fleet-activity/task-status/dispatch-task/release-task/cancel-task/merge-worktree/remove-worktree/green-check; `hscc-cluster`: cluster-status). ✔

**Known nuance:** the hyphenated `hscc-mcp` dir is not a valid Python package identifier; both tests (conftest) and server.py install a synthetic `hscc_mcp` module so `from hscc_mcp import runner/tools` resolves. This is intentional and tested.
