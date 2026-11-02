"""Doctor coverage for the worker budget-hook row (t_59bfc480, ships 2.15.0).

Two layers, same shape as test_doctor_orch_waker.py:

1. The BRIDGE helpers inside doctor.py (plugin-dir resolution + loader + row
   builder): a deployed tree that lost the wiring, or a module that cannot be
   imported at all, must surface as an honest non-fatal ERROR row — never a
   silent vanishing of the check and never a pass. The verdict itself belongs to
   ``hscc-cluster/budget_watch.py::doctor_check`` (verdict-with-feature), so the
   green/red tests here drive the REAL check with fixture plugin trees rather
   than restating its rules.

2. The SEAM inside run_doctor(): the check's real target is the operator's
   DEPLOYED plugin tree (``~/.hermes/plugins`` / the live profile's ``plugins``
   dir) when this suite runs from a dispatched worker. Inside a pytest process
   the verdict must be injected (``_budget_hook_check``) or the row renders as a
   refusal; it must never read the operator's install. A canary file under the
   redirected HOME would make a leak visible, and the refusal assertion is the
   tripwire (same reasoning as the waker suite).

Nothing here touches budget_watch.py's logic — the row only routes its dict into
the five Check fields.
"""

import json
import os
import shutil
import sys

import doctor

CHECK_NAME = "worker budget hook ([budget] note)"

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
_REAL_BUDGET_WATCH = os.path.join(_REPO_ROOT, "hscc-cluster",
                                  "budget_watch.py")

#: The wiring budget_watch.doctor_check looks for in the DEPLOYED __init__.py.
WIRED_INIT = '''"""Fixture copy of the deployed hscc-cluster register()."""


def register(ctx):
    if hasattr(ctx, "register_hook"):
        try:
            from . import budget_watch
            ctx.register_hook("post_api_request", budget_watch.on_post_api_request)
            ctx.register_hook("transform_tool_result", budget_watch.on_transform_tool_result)
        except Exception:
            pass
'''

#: Same plugin minus the two budget register_hook lines (the regression this
#: row exists to catch: everything else still works, only the note is gone).
UNWIRED_INIT = '''"""Fixture copy of a stale deployed hscc-cluster register()."""


def register(ctx):
    if hasattr(ctx, "register_hook"):
        try:
            from . import workflow
            ctx.register_hook("pre_tool_call", workflow.on_pre_tool_call)
        except Exception:
            pass
'''


def _fleet_health():
    """Stub every other fleet seam so run_doctor() cannot reach anything real."""
    return {"_cluster_default_runner": lambda: '{"hosts": ["10.0.0.1"]}',
            "_status_runner": lambda cl: "",
            "_serving_raw": json.dumps({"units": []}),
            "_venv_python": "/home/u/.venvs/sparkrun/bin/python",
            "_lan_probe": lambda i, n: None}


def _home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "hermes-agent").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HSCC_DIR", str(tmp_path / ".hscc"))
    # The loader honours PLUGINS first; clear it so no inherited worker value
    # can point the row at the operator's live plugin tree.
    monkeypatch.delenv("PLUGINS", raising=False)
    return home


FAKE_CLUSTER = json.dumps([{"name": "hscc", "hosts": ["10.0.0.1"]}])


def _row(**verdict):
    base = {"name": CHECK_NAME, "ok": True, "fatal": False,
            "detail": "post_api_request + transform_tool_result wired in "
                      "/deployed/hscc-cluster (this module: same tree)",
            "fix": ""}
    base.update(verdict)
    return lambda: dict(base)


def _doctor(tmp_path, monkeypatch, **kw):
    home = _home(tmp_path, monkeypatch)
    return doctor.run_doctor(
        str(home), _cluster_runner=lambda: FAKE_CLUSTER, fleet_checks=True,
        _ssh_factory=lambda h: (lambda c, t: {"ok": False, "stdout": "",
                                              "code": 127, "stderr": "stub"}),
        hscc_dir="/nonexistent",
        **_fleet_health(), **kw)


def _deploy(tmp_path, *, init_src=WIRED_INIT, budget_watch=True):
    """Build a fixture DEPLOYED tree: <root>/plugins/hscc-cluster/{...}.

    ``budget_watch.py`` is the REAL repo file (copied), so the verdict exercised
    below is the shipped check, not a stub of it. Returns the plugin-container
    root to hand to the row as hermes_home's sibling.
    """
    plugins = tmp_path / "deployed" / "plugins"
    cluster = plugins / "hscc-cluster"
    cluster.mkdir(parents=True)
    (cluster / "__init__.py").write_text(init_src, encoding="utf-8")
    if budget_watch:
        shutil.copyfile(_REAL_BUDGET_WATCH, cluster / "budget_watch.py")
    return plugins


def _out_of_pytest(monkeypatch):
    """Force the row's human-CLI path. monkeypatch.delitem restores
    sys.modules["pytest"] at teardown, so no other test sees it missing."""
    monkeypatch.delitem(sys.modules, "pytest", raising=False)


# ── the seam inside run_doctor ─────────────────────────────────────────────

def test_run_doctor_reports_the_budget_row_when_injected(tmp_path, monkeypatch):
    res = _doctor(tmp_path, monkeypatch, _budget_hook_check=_row())
    names = {c["name"]: c for c in res["checks"]}
    assert CHECK_NAME in names, "the budget hook must have its OWN row"
    assert names[CHECK_NAME]["ok"] is True
    assert names[CHECK_NAME]["fatal"] is False


def test_run_doctor_routes_a_missing_hook_without_sinking_the_doctor(tmp_path,
                                                                     monkeypatch):
    """The hook is a nice-to-have: an unwired deploy is ok=False / fatal=False,
    printed as a warning but preflight still passes (same posture as the
    cron-watchers row — doctor is the Stage-1 install gate)."""
    res = _doctor(tmp_path, monkeypatch,
                  _budget_hook_check=_row(
                      ok=False,
                      detail="hscc-cluster is deployed but does not register "
                             "the budget hooks",
                      fix="Re-deploy hscc-cluster from the repo (bootstrap)."))
    row = {c["name"]: c for c in res["checks"]}[CHECK_NAME]
    assert row["ok"] is False and row["fatal"] is False
    assert "does not register" in row["detail"]
    assert res["ok"] is True


def test_run_doctor_inside_pytest_refuses_an_uninjected_check(tmp_path,
                                                             monkeypatch):
    """THE fence. fleet_checks=True with no _budget_hook_check must not read the
    operator's deployed plugin tree — the row comes back a refusal naming the
    seam. If the pytest branch ever regressed into the real check, the detail
    would carry deployed-tree text instead of 'refused'."""
    res = _doctor(tmp_path, monkeypatch)
    row = {c["name"]: c for c in res["checks"]}[CHECK_NAME]
    assert row["ok"] is False
    assert "refused" in row["detail"] and "pytest" in row["detail"]


def test_budget_row_is_in_the_check_list_exactly_once(tmp_path, monkeypatch):
    """The checklist is keyed by name; a duplicated row would double-count the
    verdict and confuse the ○/● rendering."""
    res = _doctor(tmp_path, monkeypatch, _budget_hook_check=_row())
    assert [c["name"] for c in res["checks"]].count(CHECK_NAME) == 1


# ── the row builder against fixture deploy trees (real check, no stub) ─────

def test_wired_deploy_tree_is_green(tmp_path, monkeypatch):
    """A deployed hscc-cluster that registers both hooks reads GREEN, quoting
    the deployed dir — the row's whole point: it sees the tree Hermes loads."""
    plugins = _deploy(tmp_path)
    _out_of_pytest(monkeypatch)
    row = doctor._budget_hook_check_row(None,
                                        hermes_home=str(tmp_path / "deployed"))
    assert row.ok is True and row.fatal is False
    assert row.name == CHECK_NAME
    assert "post_api_request + transform_tool_result wired" in row.detail
    assert str(plugins) in row.detail


def test_missing_register_hook_lines_are_red_naming_the_fix(tmp_path,
                                                            monkeypatch):
    """THE regression: a deployed hscc-cluster whose register() lost the two
    budget register_hook calls (stale payload / hand-patched dir). Must be RED
    and its fix must say what to do, never a bare 'no'."""
    _deploy(tmp_path, init_src=UNWIRED_INIT)
    _out_of_pytest(monkeypatch)
    row = doctor._budget_hook_check_row(None,
                                        hermes_home=str(tmp_path / "deployed"))
    assert row.ok is False
    assert "does not register the budget hooks" in row.detail
    assert "Re-deploy hscc-cluster" in row.fix
    assert row.fatal is False, "a missing note never aborts an install"


def test_import_failure_is_non_fatal_degraded(tmp_path, monkeypatch):
    """No deployable budget_watch.py at all (payload half-installed): the row
    says the wiring CANNOT be verified and stays non-fatal — an honest
    'unverifiable', never absence-of-output and never a pass. Same honesty rule
    as the orch-waker / notifier-mask rows."""
    _deploy(tmp_path, budget_watch=False)
    _out_of_pytest(monkeypatch)
    row = doctor._budget_hook_check_row(None,
                                        hermes_home=str(tmp_path / "deployed"))
    assert row.ok is False and row.fatal is False
    assert "not importable" in row.detail
    assert "re-run bootstrap" in row.fix


def test_broken_module_is_non_fatal_degraded(tmp_path, monkeypatch):
    """A budget_watch.py that raises on import (syntax error, half-copied file)
    must degrade exactly like a missing one — the loader skips it instead of
    letting the exception escape the check list."""
    plugins = _deploy(tmp_path)
    (plugins / "hscc-cluster" / "budget_watch.py").write_text(
        "raise RuntimeError('cannot load')\n", encoding="utf-8")
    _out_of_pytest(monkeypatch)
    row = doctor._budget_hook_check_row(None,
                                        hermes_home=str(tmp_path / "deployed"))
    assert row.ok is False and row.fatal is False
    assert "not importable" in row.detail


def test_row_builder_follows_the_injected_verdict_exactly(tmp_path, monkeypatch):
    """The row must copy the five Check fields and add nothing: doctor's
    renderer and exit logic key off them, so an invented field or a dropped one
    is a contract break at the bridge, not in the check."""
    seen = _row(ok=False, fatal=True, detail="d", fix="f", extra="nope")
    row = doctor._budget_hook_check_row(seen)
    assert {f for f in row.__dataclass_fields__} == {"name", "ok", "detail",
                                                     "fix", "fatal"}
    assert row.name == CHECK_NAME and row.fatal is True
    assert row.detail == "d" and row.fix == "f"


def test_row_builder_passes_the_resolved_plugin_dirs_to_the_check(
        tmp_path, monkeypatch):
    """The human-CLI path delegates to budget_watch.doctor_check(plugins_dirs=…)
    — proving both the delegation and that the tree the check inspects is the
    tree doctor resolved, with no real deployed tree read."""
    seen = []

    def fake_check(plugins_dirs=None):
        seen.append(list(plugins_dirs))
        return {"name": CHECK_NAME, "ok": True, "fatal": False,
                "detail": "from the real check", "fix": ""}

    class BW:
        doctor_check = staticmethod(fake_check)

    monkeypatch.setattr(doctor, "_load_budget_watch",
                        lambda hermes_home=None: BW())
    monkeypatch.setenv("PLUGINS", str(tmp_path / "env-plugins"))
    monkeypatch.setenv("HOME", str(tmp_path))
    _out_of_pytest(monkeypatch)
    row = doctor._budget_hook_check_row(
        None, hermes_home=str(tmp_path / ".hermes"))
    assert row.detail == "from the real check"
    assert seen == [[str(tmp_path / "env-plugins"),
                     os.path.join(str(tmp_path), ".hermes", "plugins")]], \
        "the check must inspect exactly the tree doctor resolved (PLUGINS env " \
        "first, then the home's plugins dir; the ~/.hermes/plugins tail " \
        "dedupes onto the home one under a redirected HOME)"


# ── plugin-root resolution (installer's own rule) ──────────────────────────

def test_plugin_dirs_honour_the_plugins_env_then_the_home(tmp_path, monkeypatch):
    """Same precedence the installer writes with (install_payload.py): PLUGINS
    env first, then the home's own plugins dir, then ~/.hermes/plugins. A
    `--home`/sandbox bootstrap must check THAT tree, not the operator's."""
    monkeypatch.setenv("PLUGINS", str(tmp_path / "env-plugins"))
    home = tmp_path / ".hermes"
    dirs = doctor._budget_plugin_dirs(str(home))
    assert dirs[0] == str(tmp_path / "env-plugins")
    assert os.path.join(str(home), "plugins") in dirs
    assert dirs[-1] == os.path.join(os.path.expanduser("~"), ".hermes",
                                    "plugins")


def test_profile_scoped_home_also_contributes_the_root_plugins(
        tmp_path, monkeypatch):
    """Workers run with HERMES_HOME=<root>/profiles/<name>, and
    install_payload_profiles writes the payload into BOTH the profile's and the
    root's plugins dir — so the check must look in both or a correct deploy
    reads red."""
    monkeypatch.delenv("PLUGINS", raising=False)
    home = tmp_path / ".hermes" / "profiles" / "backend-engineer"
    dirs = doctor._budget_plugin_dirs(str(home))
    assert str(home / "plugins") in dirs
    assert str(tmp_path / ".hermes" / "plugins") in dirs
