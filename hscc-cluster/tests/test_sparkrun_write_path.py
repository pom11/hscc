"""The sparkrun proxy.yaml WRITE path (spec §4.4) — bridge-level tests.

Every test here injects a fake runner (t_ac795424 doctrine: no real sparkrun,
no real proxy.yaml, no network). The point is not "sparkrun works" — it is that
hscc asks sparkrun to do the write, asks for the RIGHT region, and refuses to
write anything at all when sparkrun is unreachable.

Address doctrine: placeholders only (10.0.0.x / 100.64.0.1); nothing in here
names a real host.
"""

import json
import os
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

import sparkrun_bridge as sb


class FakeProc:
    """CompletedProcess-shaped stand-in for the venv subprocess."""

    def __init__(self, payload=None, returncode=0, stdout=None, stderr=""):
        self.returncode = returncode
        self.stdout = stdout if stdout is not None else json.dumps(
            {"ok": True, "before": {}, "after": payload or {}})
        self.stderr = stderr


def _runner(proc):
    calls = []

    def run(payload, mode, timeout=30):
        calls.append({"regions": json.loads(payload), "mode": mode})
        return proc
    run.calls = calls
    return run


# ── the contract this module owns ───────────────────────────────────────────


def test_private_write_path_contract():
    """The write path depends on sparkrun's PRIVATE ProxyConfig surface, and
    that dependency is deliberate, documented, and pinned here.

    sparkrun 0.4.0 has NO setter for the `gateways.*` section
    (`gateway_settings` is read-only: "sparkrun neither validates it nor WRITES
    it"), so `gateways.litellm.balancing` — the one region apply must write
    today — can only land through `ProxyConfig._write_atomic` under
    `ProxyConfig._write_lock()`. If sparkrun renames either, this test fails
    LOUDLY, and the remedy is the §4.4 public write API, not a hand-written
    yaml file.

    When this test starts failing, that IS the finding: file the smallest
    sparkrun-side addition (a `set_gateway_settings`/`unset_gateway_settings`
    pair mirroring `set_proxy`'s pending-merge discipline) instead of routing
    around it.
    """
    script = sb._PROXY_WRITE_SCRIPT
    for needed in ("from sparkrun.proxy.config import ProxyConfig",
                   "PC._write_lock()", "PC._write_atomic(", "PC._read(",
                   "PC.set_proxy(", "PC.save()"):
        assert needed in script, f"write script no longer uses {needed!r}"
    # And the real module still exposes them (this is the part that breaks if
    # sparkrun moves the code — a rename here would otherwise be silent).
    src = Path(sb.__file__).read_text()
    assert "sparkrun.proxy.config" in src


def test_the_bridge_never_writes_proxy_yaml_itself():
    """§4.4/§9: hscc never opens proxy.yaml for write and never touches the
    regenerated litellm_config.yaml. The only way this module can reach the
    file is by asking sparkrun's own module to do it.

    Checked on the SYNTAX TREE, not by grep: prose in this module names the
    forbidden files on purpose (that is how the boundary is documented), and a
    comment is not a write.
    """
    import ast

    tree = ast.parse(Path(sb.__file__).read_text())

    # No yaml at all — a module that can serialise a document can write one.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name.split(".")[0] != "yaml", "bridge imports yaml"
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "yaml"

    # The only write-ish surface allowed is the inline script handed to sparkrun
    # (a string constant). No OPEN CALL in executable code may request a write
    # mode, and no file-write helper may be called.
    write_helpers = {"safe_dump", "write_text", "write_bytes", "copyfile"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in write_helpers:
            pytest.fail(f"bridge calls a file-write helper: {node.attr}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "open":
            mode = ""
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value)
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = str(kw.value.value)
            assert not any(c in mode for c in "wax"), (
                f"bridge opens a file for writing (mode={mode!r})")


# ── what it asks sparkrun to write ──────────────────────────────────────────


def test_balancing_region_is_nested_not_top_level():
    """The 2026-10-19 addendum: sparkrun 0.4.0 reads `gateways.litellm.*`.
    A top-level `balancing` would be silently ignored by the live build, so the
    region shape is a correctness claim, not a style choice.
    """
    regions = {"gateways": {"litellm": {"balancing": "least-in-flight"}}}
    run = _runner(FakeProc(regions))
    out = sb.proxy_config_write(regions, _runner=run)
    assert out["ok"] is True
    assert run.calls[0]["regions"] == regions
    assert "balancing" not in run.calls[0]["regions"]


def test_changed_flag_comes_from_sparkrun_not_from_us():
    """`changed` is sparkrun's before/after comparison — apply's restart
    decision rests on it, so it must be derived from the real documents.
    """
    same = FakeProc(stdout=json.dumps(
        {"ok": True, "before": {"gateways": {"litellm": {"balancing": "x"}}},
         "after": {"gateways": {"litellm": {"balancing": "x"}}}}))
    out = sb.proxy_config_write({"gateways": {"litellm": {"balancing": "x"}}},
                               _runner=_runner(same))
    assert out["ok"] is True and out["changed"] is False

    diff = FakeProc(stdout=json.dumps(
        {"ok": True, "before": {},
         "after": {"gateways": {"litellm": {"balancing": "least-in-flight"}}}}))
    out2 = sb.proxy_config_write(
        {"gateways": {"litellm": {"balancing": "least-in-flight"}}},
        _runner=_runner(diff))
    assert out2["ok"] is True and out2["changed"] is True


def test_the_script_merges_inside_the_lock_rereading_first():
    """A discovery sweep writes proxy.yaml concurrently. The script must
    read-modify-write INSIDE sparkrun's lock (the ordering ProxyConfig.save
    uses), never merge a snapshot taken before the lock.
    """
    script = sb._PROXY_WRITE_SCRIPT
    lines = [ln.strip() for ln in script.splitlines()]
    lock_at = next(i for i, ln in enumerate(lines) if ln.startswith("with PC._write_lock"))
    reread_at = next(i for i, ln in enumerate(lines)
                     if ln.startswith("latest=PC._read("))
    write_at = next(i for i, ln in enumerate(lines)
                    if ln.startswith("PC._write_atomic("))
    assert lock_at < reread_at < write_at, (
        "read-modify-write must happen inside the writer lock")
    # The merge is DEEP, so writing balancing cannot erase another gateway's
    # namespace, an alias, or the bind host.
    assert "def _merge(dst,src)" in script and "_merge(latest.setdefault(" in script


# ── fail-closed ─────────────────────────────────────────────────────────────


def test_unreachable_sparkrun_writes_nothing_and_reports_failure():
    """No interpreter / spawn failure => ok=False and NO fallback write. The
    apply step must surface it, not hand-craft the file.
    """
    def boom(payload, mode, timeout=30):
        raise FileNotFoundError("sparkrun not on PATH")
    out = sb.proxy_config_write({"gateways": {"litellm": {"balancing": "x"}}},
                                _runner=boom)
    assert out["ok"] is False and out["changed"] is False
    assert "unreachable" in out["error"]


def test_nonzero_exit_is_a_failure_not_a_silent_success():
    run = _runner(FakeProc(returncode=1, stdout="", stderr="lock timeout"))
    out = sb.proxy_config_write({"gateways": {"litellm": {"balancing": "x"}}},
                               _runner=run)
    assert out["ok"] is False and "lock timeout" in out["error"]


def test_unparseable_output_is_a_failure():
    run = _runner(FakeProc(stdout="not json at all"))
    out = sb.proxy_config_write({"gateways": {"litellm": {"balancing": "x"}}},
                               _runner=run)
    assert out["ok"] is False and "parseable" in out["error"]


def test_empty_or_malformed_regions_never_reach_sparkrun():
    for bad in ({}, None, {"gateways": "not-a-mapping"}, {"": {"a": 1}}):
        out = sb.proxy_config_write(bad, _runner=_runner(FakeProc()))
        assert out["ok"] is False, f"{bad!r} was accepted"


def test_the_interpreter_is_sparkruns_own_not_this_process():
    """The health.py doctrine: drive sparkrun under sparkrun's venv python, so
    the file format, lock and atomic replace are sparkrun's code.
    """
    src = Path(sb.__file__).read_text()
    assert "_sparkrun_venv_python()" in src
    # and the real runner spawns argv directly — no shell string to drift in.
    assert "subprocess.run([venv_py, \"-c\", _PROXY_WRITE_SCRIPT" in src


def test_proxy_section_alone_uses_the_public_setter_path():
    """Where sparkrun HAS a setter (`set_proxy`), use it — the private
    `_write_atomic` route is only for the sections it has none for.
    """
    run = _runner(FakeProc({"proxy": {"port": 4000}}))
    out = sb.proxy_config_write({"proxy": {"port": 4000}}, _runner=run)
    assert out["ok"] is True
    assert run.calls[0]["mode"] == "pending"

    run2 = _runner(FakeProc())
    sb.proxy_config_write({"gateways": {"litellm": {"balancing": "x"}}},
                          _runner=run2)
    assert run2.calls[0]["mode"] == "direct"
