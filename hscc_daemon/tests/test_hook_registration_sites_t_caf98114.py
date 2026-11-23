"""Hook-site drift: the six hscc-cluster hooks must register on EVERY home (R6).

Folded into card t_caf98114 by operator ruling ("LEFT-4 IS FOLDED INTO THIS
CARD"). The audit left it open (``docs/audits/2026-11-23-main-only-surface.md``
§9):

    LEFT-4 — no test pins "``hscc-cluster/plugin.yaml:54-62`` hook list == the
    ``register_hook`` sites" *plus* "each hook's firing process" — the drift test
    (``hscc_daemon/tests/test_plugin_manifest_drift.py``) covers the declaration
    half only.

Reading the shipped tests before writing this one moved the goalposts, honestly,
and this file owns only what is genuinely unowned — the three other tests named
below are deliberately NOT duplicated here:

* ``test_plugin_manifest_drift.py::test_manifest_matches_registered_hooks``
  already pins ``set(provides_hooks) == set(register_hook names)`` in both
  directions (t_720a674f), so the *equation* exists. What it cannot see is a
  coordinated removal: delete a hook from BOTH the manifest and ``register()``
  and it stays green. :data:`REQUIRED_HOOKS` is the literal that makes that red.
* ``hscc-cluster/budget_watch.py::doctor_check`` reads the DEPLOYED tree for the
  two budget-hook calls (``budget_watch.py:431-432``). That is a per-install
  wiring check; this file is the repo-side pin — different question, same list.
* the row added by t_caf98114 (``doctor_main_only``) refuses to call hook
  presence drift at all (binding ruling R2/D-C). This test is the other half of
  that ruling: it is what keeps the sentence "hooks fire on a non-main home"
  true while doctor stops checking it.

What this pins, all four being shapes a green suite keeps green today:

1. the six hook names, as a literal table (:data:`REQUIRED_HOOKS`);
2. **each hook's firing process** (:data:`FIRES_IN`) — the hermes call site that
   raises it and the home that must therefore load the plugin;
3. the ONLY gate permitted around a registration is the capability probe
   ``hasattr(ctx, "register_hook")`` — a profile/identity gate around a hook
   goes red (that is the failure mode the main-only work tempts: the D-B
   tightening wants tools gated by profile, and the tempting edit wraps the
   whole ``register()`` body);
4. no early ``return`` may precede the last registration, and ``register()``
   must not resolve a profile or the main identity at all.

Parses only — never imports ``hscc-cluster`` (a hyphenated dir is not an
importable package name, and importing it would pull hermes into the leg), never
touches ``~/.hscc``, the fleet or the network.
"""

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "hscc-cluster"
INIT = PLUGIN / "__init__.py"

#: The six hooks, verbatim. Every one of them serves a worker or a dispatcher
#: acting for a worker, which is why the audit ruled (and binding ruling R2/D-C
#: repeats) that ``hscc-cluster`` stays enabled in EVERY home: Hermes registers
#: tools and hooks in the same ``register()`` call, so a home that stops loading
#: the plugin loses these silently — "silently dead, not degraded" (§3.1).
REQUIRED_HOOKS = (
    "kanban_task_claimed",
    "kanban_task_blocked",
    "kanban_task_completed",
    "pre_tool_call",
    "post_api_request",
    "transform_tool_result",
)

#: The one conditional a registration may sit under: a capability probe against
#: an older hermes that has no ``register_hook`` on the context. Anything else —
#: a profile check, an identity comparison, a toolset lookup — is a gate, and a
#: gate here silently un-hooks whichever homes it excludes.
ALLOWED_GATE = "hasattr(ctx, 'register_hook')"

#: LEFT-4's second half: which process raises each hook, and which home must
#: therefore load the plugin. File:line evidence is the audit's (§3.2), taken
#: against hermes at the time of the main-only audit.
FIRES_IN = {
    "kanban_task_claimed": {
        "raises_in": "the kanban dispatcher, inside the board's dispatch lock "
                     "(hermes_cli/kanban_db.py:2202)",
        "homes": ("root home (the dispatcher)",),
        "loses_if_absent": "the idempotent-resume note on re-dispatch (WS4)",
    },
    "kanban_task_blocked": {
        "raises_in": "the worker's own block_task call (kanban_db.py:3211)",
        "homes": ("worker profile home", "project-orch profile home"),
        "loses_if_absent": "blocked-task surfacing to ops — the majority of "
                           "blocks are worker-initiated",
    },
    "kanban_task_completed": {
        "raises_in": "the worker's own complete_task call (kanban_db.py:2743)",
        "homes": ("worker profile home", "project-orch profile home"),
        "loses_if_absent": "completion records and the auto-unblock they drive",
    },
    "pre_tool_call": {
        "raises_in": "every agent turn, in that agent's process",
        "homes": ("worker profile home", "project-orch profile home"),
        "loses_if_absent": "the tool-event ledger's profile_name stamp — the "
                           "per-profile observability the dashboards read",
    },
    "post_api_request": {
        "raises_in": "every API call, in the agent process (worker-only by "
                     "construction, budget_watch.py:8-16)",
        "homes": ("worker profile home",),
        "loses_if_absent": "the [budget] note the worker SOUL promises "
                           "(t_6c4f38f2) — a documented-promise regression",
    },
    "transform_tool_result": {
        "raises_in": "every tool result, in the agent process",
        "homes": ("worker profile home",),
        "loses_if_absent": "the one [budget] line the note is rendered from",
    },
}

#: Words that must not appear anywhere in ``register()``: the shape of an
#: identity gate. The main-only design (audit D-B) puts profile gating one layer
#: BELOW the plugin — which tools ``register()`` exposes — never around hooks.
FORBIDDEN_IN_REGISTER = ("profile_name", "main_name", "main_orch", "is_main",
                         "main_profile", "toolsets", "get_plugin_manager")


def _source() -> str:
    if not INIT.is_file():
        pytest.skip("repo layout only: the deployed plugins dir makes no "
                    "promise about hscc-cluster/__init__.py")
    return INIT.read_text(encoding="utf-8")


def _register_fn() -> ast.FunctionDef:
    tree = ast.parse(_source())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "register":
            return node
    raise AssertionError("hscc-cluster/__init__.py has no top-level register()")


def _hook_calls(fn) -> list:
    """Every ``ctx.register_hook("name", handler)`` in *fn*, with its node."""
    out = []
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "register_hook"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "ctx"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            raise AssertionError(
                f"register_hook at line {node.lineno} needs a literal name — a "
                "computed hook name is invisible to the manifest drift test")
        out.append({"name": node.args[0].value,
                    "handler": ast.unparse(node.args[1]) if len(node.args) > 1
                    else "",
                    "line": node.lineno,
                    "node": node})
    return out


def _gating_tests(fn, call_node) -> list:
    """The tests of every ``If`` enclosing *call_node*, outermost first."""
    parents = {}
    for node in ast.walk(fn):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    gates = []
    node = call_node
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.If):
            gates.append(ast.unparse(node.test))
    return list(reversed(gates))


# ── 1. the six names ────────────────────────────────────────────────────────

def test_all_six_hooks_are_registered():
    """A hook dropped from ``register()`` goes red even if plugin.yaml is edited
    in the same commit (which is exactly how the existing equation test stays
    green)."""
    names = tuple(c["name"] for c in _hook_calls(_register_fn()))
    assert set(names) == set(REQUIRED_HOOKS), (
        f"register() hooks {sorted(set(names))} != the six that must fire "
        f"{sorted(REQUIRED_HOOKS)}; missing "
        f"{sorted(set(REQUIRED_HOOKS) - set(names))}")
    assert len(names) == len(set(names)), "a hook registered twice"


def test_firing_process_table_covers_every_hook():
    """LEFT-4's "plus each hook's firing process": the record exists, is
    complete, and no hook is allowed to be main-orchestrator-only."""
    assert set(FIRES_IN) == set(REQUIRED_HOOKS)
    for hook, facts in FIRES_IN.items():
        assert facts["raises_in"], hook
        assert facts["homes"], hook
        assert facts["loses_if_absent"], hook
        for home in facts["homes"]:
            assert "main" not in home or "main orchestrator" in home, \
                f"{hook}: a hook may not be declared main-only (R2/D-C)"
    # Five of the six fire in a WORKER process; only kanban_task_claimed is the
    # dispatcher's lane. A future edit that drops a worker home has to pass this.
    worker_lane = {h for h, f in FIRES_IN.items()
                   if any("worker" in home for home in f["homes"])}
    assert worker_lane == set(REQUIRED_HOOKS) - {"kanban_task_claimed"}


# ── 2-4. nothing may make a registration conditional on who is asking ───────

def test_the_only_gate_around_a_registration_is_the_capability_probe():
    """The R6 bite: wrap a hook in ``if <profile is main>:`` and this is red.

    Every ``register_hook`` call today sits under ``if hasattr(ctx,
    "register_hook"):`` plus a ``try/except`` — a capability probe and a
    fail-open, neither of which can exclude a home by identity. Any other
    enclosing ``If`` (and any ``For``/``comprehension``, which would make the
    registration conditional on data) is reported with the line it guards.
    """
    fn = _register_fn()
    bad = []
    for call in _hook_calls(fn):
        for test in _gating_tests(fn, call["node"]):
            if test.replace('"', "'") != ALLOWED_GATE:
                bad.append(f"line {call['line']} ({call['name']}) if {test}")
    assert not bad, (
        "hook registration must not be conditional on anything but the "
        "capability probe "
        f"{ALLOWED_GATE!r}; a gate here un-hooks real homes silently "
        f"(audit §3.1): {bad}")


def test_no_early_return_can_skip_the_hook_block():
    """An identity gate that works by leaving ``register()`` early is the same
    bug in a different shape — and it is invisible to the gate scan above."""
    fn = _register_fn()
    last_hook = max(c["line"] for c in _hook_calls(fn))
    early = [n.lineno for n in ast.walk(fn)
             if isinstance(n, ast.Return) and n.lineno < last_hook
             and n.value is not None]
    assert not early, (
        f"register() returns before line {last_hook} (lines {early}); an early "
        "return is how a profile gate would skip the hook block")


def test_register_resolves_no_identity():
    """The D-B rule as an AST fact: gating belongs to which TOOLS are exposed,
    never to whether the plugin loads or its hooks fire."""
    fn = _register_fn()
    body = ast.unparse(fn)
    offenders = [w for w in FORBIDDEN_IN_REGISTER if w in body]
    assert not offenders, (
        f"register() mentions {offenders}: hook registration may not depend on "
        "the profile or the main identity (binding ruling R2/D-C). The main-only "
        "gate lives on the TOOL lists register() iterates, one layer below the "
        "plugin (audit §3.1, §6 rule 1).")


def test_hooks_still_come_from_the_two_owner_modules():
    """``workflow`` owns the kanban/tool hooks, ``budget_watch`` the two budget
    ones. A hook moving to a third module is fine — but it must be a decision
    taken here, because this table is what the doctor row's wording cites."""
    owners = {"kanban_task_claimed": "workflow",
              "kanban_task_blocked": "workflow",
              "kanban_task_completed": "workflow",
              "pre_tool_call": "workflow",
              "post_api_request": "budget_watch",
              "transform_tool_result": "budget_watch"}
    for call in _hook_calls(_register_fn()):
        assert call["handler"].startswith(owners[call["name"]] + "."), (
            f"{call['name']} handler {call['handler']!r} is no longer "
            f"{owners[call['name']]}.<hook> — update this table and the audit's "
            "§3.2 row with it")
        assert re.match(r"^\w+\.on_\w+$", call["handler"]), call["handler"]


def test_hook_registration_is_unreachable_by_config_only():
    """The claim this card's doctor row depends on: the ONLY thing that stops
    these hooks firing is the plugin not being enabled — there is no second
    switch inside ``register()`` for a model to reason about."""
    src = _source()
    # The manifest declares the hooks (so `hermes plugins doctor` sees them)…
    manifest = (PLUGIN / "plugin.yaml").read_text(encoding="utf-8")
    declared = re.findall(r"^\s+-\s+(\w+)\s*$",
                          manifest.split("provides_hooks:")[-1].split(
                              "\n\n")[0], re.M)
    assert set(declared) == set(REQUIRED_HOOKS)
    # Every registration site in the whole module is inside register(), and
    # there is exactly one site per hook — so "the plugin is enabled" is the only
    # remaining switch, which is what doctor's row is allowed to assume.
    sites = _hook_calls(_register_fn())
    assert {c["name"] for c in sites} == set(REQUIRED_HOOKS)
    assert _source().count("ctx.register_hook(") == len(sites) == 6, (
        "a register_hook site outside register() would be a second, untracked "
        "switch")
    for hook in REQUIRED_HOOKS:
        assert _source().count(f'register_hook("{hook}"') == 1, (
            f"{hook} must be registered from exactly one site")
