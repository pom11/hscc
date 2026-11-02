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


def _isolate(tmp_path, monkeypatch):
    """Pin the two env vars that decide where the row looks, for tests that hand
    the row an explicit ``hermes_home``.

    ``_budget_plugin_dirs`` reads ``PLUGINS`` and falls back to ``~/.hermes/plugins``,
    so without this the row's candidate list is whatever the DISPATCHING host
    happens to have deployed — the exact hermeticity bug t_d7f2ccd1 is about.
    ``_home()`` does the same for the run_doctor() tests.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PLUGINS", raising=False)


def _live_deploy(tmp_path, monkeypatch, *, init_src=WIRED_INIT):
    """Seed a WIRED (or unwired) deploy at the shape a no-home caller resolves:
    ``$HOME/.hermes/plugins/hscc-cluster`` — the OPERATOR's tree in a real run.

    Returns the plugin-container root. The canary tests below put this in place
    WHILE also naming a different ``hermes_home``: a row that resolves the live
    tree at all then reads green over the fixture's red verdict, which is how the
    leak becomes visible even under a redirected HOME.
    """
    _isolate(tmp_path, monkeypatch)
    plugins = tmp_path / ".hermes" / "plugins"
    cluster = plugins / "hscc-cluster"
    cluster.mkdir(parents=True)
    (cluster / "__init__.py").write_text(init_src, encoding="utf-8")
    shutil.copyfile(_REAL_BUDGET_WATCH, cluster / "budget_watch.py")
    return plugins


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
    _isolate(tmp_path, monkeypatch)
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
    and its fix must say what to do, never a bare 'no'.

    A WIRED deploy at the live shape is seeded alongside the fixture (see
    :func:`_live_deploy`): if the row ever resolves the host's tree instead of
    the ``hermes_home`` it was handed, the live wiring answers for the fixture
    and this reads green — the leak t_d7f2ccd1 caught did exactly that."""
    _live_deploy(tmp_path, monkeypatch)          # the trap: wired, but not the tree asked about
    _deploy(tmp_path, init_src=UNWIRED_INIT)     # the tree the row was told to check
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
    as the orch-waker / notifier-mask rows.

    Same live-shaped trap as above: an import failure in the named tree must not
    be papered over by a healthy module in the operator's deploy."""
    _live_deploy(tmp_path, monkeypatch)
    _deploy(tmp_path, budget_watch=False)
    _out_of_pytest(monkeypatch)
    row = doctor._budget_hook_check_row(None,
                                        hermes_home=str(tmp_path / "deployed"))
    assert row.ok is False and row.fatal is False
    assert "not importable" in row.detail
    assert "re-run bootstrap" in row.fix
    # The detail must name the roots it searched: "not importable" alone cannot
    # be acted on — a stale deploy and a wrong --home look identical otherwise.
    assert "looked in:" in row.detail
    assert str(tmp_path / "deployed" / "plugins") in row.detail


def test_broken_module_is_non_fatal_degraded(tmp_path, monkeypatch):
    """A budget_watch.py that raises on import (syntax error, half-copied file)
    must degrade exactly like a missing one — the loader skips it instead of
    letting the exception escape the check list.

    And "skips it" must not mean "falls through to the operator's copy": with a
    WIRED live deploy seeded, a fall-through reads ok=True (the t_d7f2ccd1
    symptom) instead of the honest unverifiable row."""
    _live_deploy(tmp_path, monkeypatch)
    plugins = _deploy(tmp_path)
    (plugins / "hscc-cluster" / "budget_watch.py").write_text(
        "raise RuntimeError('cannot load')\n", encoding="utf-8")
    _out_of_pytest(monkeypatch)
    row = doctor._budget_hook_check_row(None,
                                        hermes_home=str(tmp_path / "deployed"))
    assert row.ok is False and row.fatal is False
    assert "not importable" in row.detail


# ── hermeticity: the row follows the home it was handed, not the host ──────

def test_named_home_wins_over_a_wired_live_deploy(tmp_path, monkeypatch):
    """The hermeticity proof t_d7f2ccd1 asks for, both directions at once.

    A fully WIRED deploy sits at ``$HOME/.hermes/plugins`` (what a no-home caller
    resolves — the operator's install in a real run) while the named ``hermes_home``
    is UNWIRED. The row must report the named home RED; if it consulted the live
    tree at all, that wiring would rescue the verdict (measured at main: ok=True
    with 'wired in ~/.hermes/plugins' in the detail)."""
    live = _live_deploy(tmp_path, monkeypatch)   # wired
    _deploy(tmp_path, init_src=UNWIRED_INIT)     # named home — unwired
    _out_of_pytest(monkeypatch)
    row = doctor._budget_hook_check_row(None,
                                        hermes_home=str(tmp_path / "deployed"))
    assert row.ok is False, "the live tree's wiring must not answer for the named home"
    assert str(live) not in row.detail, \
        f"the row read the live deploy, not the home it was handed: {row.detail}"


def test_wired_named_home_is_green_while_the_live_deploy_is_unwired(
        tmp_path, monkeypatch):
    """The mirror image: a correct named deploy must read GREEN even when the
    host's own deploy is broken, and its detail must name the tree it actually
    inspected. Without this the fix could pass by always preferring the host."""
    live = _live_deploy(tmp_path, monkeypatch, init_src=UNWIRED_INIT)
    plugins = _deploy(tmp_path)                  # named home — wired, the good one
    _out_of_pytest(monkeypatch)
    row = doctor._budget_hook_check_row(None,
                                        hermes_home=str(tmp_path / "deployed"))
    assert row.ok is True and row.fatal is False
    assert str(plugins) in row.detail
    assert str(live) not in row.detail


def test_no_named_home_still_falls_back_to_the_default_root(
        tmp_path, monkeypatch):
    """The other half of the contract: a HUMAN CLI run (no --home) must still
    find the operator's deploy at ``~/.hermes/plugins``. Otherwise the fix above
    would only have moved the bug."""
    live = _live_deploy(tmp_path, monkeypatch)
    _out_of_pytest(monkeypatch)
    row = doctor._budget_hook_check_row(None)    # no hermes_home — CLI path
    assert row.ok is True, f"the default root must still be checked: {row.detail}"
    assert str(live) in row.detail



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
    env first, then the home's own plugins dir. A `--home`/sandbox bootstrap must
    check THAT tree, not the operator's.

    Exactly-two-roots is the point (t_d7f2ccd1): this asserted a third,
    ``~/.hermes/plugins`` tail while a home was named — i.e. it pinned the leak
    this card fixes, and contradicted its own docstring in the process. The
    host's default root belongs to the no-home CLI path only (below)."""
    monkeypatch.setenv("PLUGINS", str(tmp_path / "env-plugins"))
    home = tmp_path / ".hermes"
    dirs = doctor._budget_plugin_dirs(str(home))
    assert dirs[0] == str(tmp_path / "env-plugins")
    assert os.path.join(str(home), "plugins") in dirs
    assert dirs == [str(tmp_path / "env-plugins"),
                    os.path.join(str(home), "plugins")], \
        "a named home must resolve to its OWN roots and nothing else"
    assert os.path.join(os.path.expanduser("~"), ".hermes", "plugins") not in dirs


def test_plugin_dirs_add_the_default_root_only_without_a_named_home(
        tmp_path, monkeypatch):
    """The other side of the same rule: a human CLI run (``doctor`` with no
    ``--home``, so no home reaches the resolver) still targets ``~/.hermes/plugins``
    — the fix must not have blinded the real-world path. PLUGINS keeps priority."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PLUGINS", raising=False)
    default = os.path.join(str(tmp_path), ".hermes", "plugins")
    assert doctor._budget_plugin_dirs(None) == [default]
    assert doctor._budget_plugin_dirs("") == [default], \
        "an empty home is 'not named', not the cwd"
    monkeypatch.setenv("PLUGINS", str(tmp_path / "env-plugins"))
    assert doctor._budget_plugin_dirs(None) == [str(tmp_path / "env-plugins"),
                                                default]


def test_profile_scoped_home_contributes_only_the_profile_plugins_dir(
        tmp_path, monkeypatch):
    """THE DECISION (t_ba14f77f): for a PROFILE-scoped home, the profile's own
    ``plugins`` dir is the ONLY authoritative tree.

    Chosen over "both dirs stay candidates, per-dir verdict" because of what
    Hermes actually loads: the user-plugin scan is ``get_hermes_home() /
    "plugins"`` (hermes_cli/plugins_discovery.py:184) and a profile-scoped
    ``HERMES_HOME`` resolves to the profile dir — no root fallback (the only
    code that scans both is the dashboard's tab discovery,
    ``web_server_dashboard.py:489-492``, which is not the runtime that fires
    ``register_hook``). The root copy ``install_payload_profiles`` writes stays
    on disk and remains authoritative for the ROOT-scoped home; it just may not
    answer for a profile. The resolver used to add it for every
    ``<root>/profiles/<name>`` home, and with ``doctor_check``'s
    any-wired-is-green that let a wired root rescue a dead profile deploy
    (canary: ``test_wired_root_deploy_does_not_rescue_an_unwired_profile_deploy``).
    """
    monkeypatch.delenv("PLUGINS", raising=False)
    home = tmp_path / ".hermes" / "profiles" / "backend-engineer"
    dirs = doctor._budget_plugin_dirs(str(home))
    assert dirs == [str(home / "plugins")], \
        "a profile-scoped home must resolve to its OWN plugins dir and nothing " \
        "else — the root's answer belongs to the root-scoped home"


def test_root_scoped_home_still_checks_the_root_plugins_dir(
        tmp_path, monkeypatch):
    """The other half of the decision: dropping the root dir from the PROFILE
    branch must not blind the root-scoped home. ``HERMES_HOME=~/.hermes``
    (the human/default mode) still resolves ``<root>/plugins`` — reached via the
    ordinary ``home/plugins`` branch, not a profiles rule — so the root deploy
    ``install_payload_profiles`` writes is still verified for the home that
    actually loads it."""
    monkeypatch.delenv("PLUGINS", raising=False)
    root = tmp_path / ".hermes"
    dirs = doctor._budget_plugin_dirs(str(root))
    assert dirs == [str(root / "plugins")]


# ── which deploy is authoritative for a PROFILE-scoped home (t_ba14f77f) ───

def _profile_deploy(tmp_path, *, init_src, name="backend-engineer"):
    """Seed a deploy at the tree a profile-scoped worker ACTUALLY loads:
    ``<root>/profiles/<name>/plugins/hscc-cluster``.

    This is not a hypothetical shape. Hermes' user-plugin scan is
    ``get_hermes_home() / "plugins"`` (hermes_cli/plugins_discovery.py:184) and
    ``get_hermes_home()`` IS the profile dir when HERMES_HOME is profile-scoped,
    with no fallback to the root's plugins dir. Measured on this host, the real
    CLI under ``HERMES_HOME=~/.hermes/profiles/backend-engineer`` parses exactly
    one ``hscc-cluster`` manifest, from the profile dir. ``install_payload_profiles``
    writes the root copy too, but the runtime never reads it for a profile run.
    """
    plugins = tmp_path / ".hermes" / "profiles" / name / "plugins"
    cluster = plugins / "hscc-cluster"
    cluster.mkdir(parents=True)
    (cluster / "__init__.py").write_text(init_src, encoding="utf-8")
    shutil.copyfile(_REAL_BUDGET_WATCH, cluster / "budget_watch.py")
    return plugins


def test_wired_root_deploy_does_not_rescue_an_unwired_profile_deploy(
        tmp_path, monkeypatch):
    """THE canary this card was filed for (t_ba14f77f).

    A worker runs with ``HERMES_HOME=<root>/profiles/<name>``, so Hermes loads
    ``<profile>/plugins/hscc-cluster``. Seed that copy UNWIRED (the register()
    lost the two budget hooks) and the root's copy WIRED, then ask the row about
    the profile home. It must be RED and name the profile dir.

    Measured red at the base commit: the row returned ok=True with
    ``'wired in <root>/.hermes/plugins'`` in the detail — the root copy, which
    the worker never loads, answered for the profile's dead hook. That is the
    exact failure class the row exists to catch: the worker silently loses its
    ``[budget]`` note while doctor reads all-green.
    """
    _isolate(tmp_path, monkeypatch)
    root_plugins = _live_deploy(tmp_path, monkeypatch)          # root: WIRED
    profile_plugins = _profile_deploy(tmp_path, init_src=UNWIRED_INIT)  # dead
    _out_of_pytest(monkeypatch)
    home = tmp_path / ".hermes" / "profiles" / "backend-engineer"
    monkeypatch.setenv("HERMES_HOME", str(home))
    row = doctor._budget_hook_check_row(None, hermes_home=str(home))
    assert row.ok is False, (
        "the root tree's wiring must not answer for the profile deploy the "
        f"worker actually loads: {row.detail}")
    assert str(root_plugins) not in row.detail, \
        f"the row consulted the root tree: {row.detail}"
    assert str(profile_plugins) in row.detail, \
        f"the row must name the tree it found broken: {row.detail}"


# ── a home that NEVER carried the payload: red, and distinguishable (t_5720ef53) ──

def test_home_that_never_carried_the_payload_reads_red_naming_the_absent_payload(
        tmp_path, monkeypatch):
    """THE decision this card was filed for (t_5720ef53): a home that has NEVER
    carried the ``hscc-cluster`` payload reads RED — not skipped, not a warning.

    Measured on this host before anything was changed: 47 of 49 profile dirs
    have no ``plugins/`` dir at all, and the red set is NOT confined to other
    projects' profiles. Cross-referenced against `hermes kanban --board hscc
    assignees`, the homes that read red include ones the dispatcher has really
    run cards in (ios-engineer done=139, worker done=41, devops-engineer
    done=25, reviewer done=19, coder done=7). So "never deployed here" is a real
    deploy gap, not noise, and red is the TRUE verdict: Hermes discovers user
    plugins at ``get_hermes_home()/plugins`` only
    (``hermes_cli/plugins_discovery.py:184``), so such a home loads no hscc
    plugin at all — no budget hook and no hscc tools.

    A WIRED live deploy sits at ``$HOME/.hermes/plugins`` as the trap: the
    strict rule from t_ba14f77f must still hold here, so the root's health may
    not rescue a home that has nothing — that would re-open the exact leak the
    parent card closed, just through a different branch.
    """
    live = _live_deploy(tmp_path, monkeypatch)                 # wired root — trap
    _out_of_pytest(monkeypatch)
    home = tmp_path / ".hermes" / "profiles" / "ios-engineer"  # no plugins/ at all
    row = doctor._budget_hook_check_row(None, hermes_home=str(home))
    assert row.ok is False, \
        f"a never-deployed home must not read green: {row.detail}"
    assert row.fatal is False, "an absent payload never aborts an install"
    assert row.name == CHECK_NAME
    assert "no hscc-cluster payload in this home" in row.detail, \
        f"the row must say the payload is ABSENT: {row.detail}"
    assert str(home / "plugins") in row.detail, \
        f"the row must name the tree it looked for: {row.detail}"
    assert "not importable" not in row.detail, \
        "a never-deployed home must not be reported as a broken import: " \
        f"{row.detail}"
    assert "Deploy the payload here" in row.fix
    assert str(live) not in row.detail, \
        f"the root deploy answered for a profile home again: {row.detail}"


def test_never_deployed_home_is_distinguishable_from_a_lost_wiring(
        tmp_path, monkeypatch):
    """The signal-to-noise half of the decision (t_5720ef53).

    The row exists to catch ONE thing: a deploy that silently LOST the
    ``[budget]`` wiring while everything else keeps working. A home that never
    received the payload is a different problem with a different fix, and before
    this card both printed as "budget_watch.py not importable ... cannot be
    verified" — so an operator reading red across 47 profiles could not tell a
    lost hook from an undeployed home, and the one red that mattered hid in the
    noise.

    Both stay RED (the honest verdict); what must differ is the wording. One
    test, both trees side by side, because the point IS the contrast: any
    wording change that re-converges the two fails here.
    """
    _live_deploy(tmp_path, monkeypatch)          # trap: healthy root, never consulted
    _deploy(tmp_path, budget_watch=False)        # payload HERE, half-installed
    _out_of_pytest(monkeypatch)

    never = doctor._budget_hook_check_row(
        None, hermes_home=str(tmp_path / ".hermes" / "profiles" / "coder"))
    lost = doctor._budget_hook_check_row(
        None, hermes_home=str(tmp_path / "deployed"))

    assert never.ok is False and lost.ok is False
    assert "no hscc-cluster payload in this home" in never.detail
    assert "not importable" in lost.detail, \
        f"the lost-wiring class must keep its own wording: {lost.detail}"
    assert "cannot be verified" in lost.detail
    assert "cannot be verified" not in never.detail, \
        f"an undeployed home is not an unverifiable wiring: {never.detail}"
    assert "never received the payload" not in lost.detail, \
        f"a half-installed payload must not be called absent: {lost.detail}"
    assert never.detail != lost.detail


def test_payload_dir_presence_is_what_separates_the_two_red_classes(tmp_path):
    """``_home_has_hscc_cluster_payload`` is the seam the row branches on, so it
    is pinned directly: a dir-exists test over the RESOLVED roots, nothing
    stronger.

    It must not grow into an import test or a wiring test — the row calls it
    precisely when the import already failed, and the wiring verdict belongs to
    ``budget_watch.doctor_check`` (verdict-with-feature). An unrelated plugin
    dir must not count as "the payload is here".
    """
    assert doctor._home_has_hscc_cluster_payload([]) is False
    assert doctor._home_has_hscc_cluster_payload(None) is False
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    assert doctor._home_has_hscc_cluster_payload([str(plugins)]) is False
    (plugins / "hscc-roles").mkdir()            # a sibling plugin, not our payload
    assert doctor._home_has_hscc_cluster_payload([str(plugins)]) is False
    (plugins / "hscc-cluster").mkdir()
    assert doctor._home_has_hscc_cluster_payload([str(plugins)]) is True
    # ANY root votes, matching doctor_check's scan over the same list: a later
    # candidate is enough, so the row never calls a multi-root home "empty".
    assert doctor._home_has_hscc_cluster_payload(
        [str(tmp_path / "nowhere"), str(plugins)]) is True
