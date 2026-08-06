"""Tests for the apply-path guards (card t_7733c4cc):

  Part 1 — recreate on serve-command change: a unit whose rendered serve
  command drifted from the RUNNING container's actual command (e.g. a new
  --served-model-name alias) is recreated, never silently "ensured up". A unit
  whose command is unchanged stays ensured-up (no unnecessary recreation).

  Part 2 — never double-provision a span member: a node that is already a
  member of a tp span is never given its own SOLO unit.

All tests are dry-run / render + fixtures — NO live provisioning, NO real
docker/ssh. The live backend functions (_running_container_cmd,
_existing_span_member_ips) are monkeypatched to return controlled values.
"""

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR / "tests"))

import cluster_template
import template_intent as ti

# A realistic rendered vLLM serve command as it appears in the container's
# /tmp/sparkrun_serve.sh (what `_running_container_cmd` returns after shlex).
def _served_argv(model, alias_name, port="8000", tp=1):
    argv = ["vllm", "serve", model, "--host", "0.0.0.0", "--port", port,
            "--trust-remote-code", "--max-model-len", "262144",
            "--enable-prefix-caching"]
    if tp > 1:
        argv += ["-tp", str(tp)]
    argv += ["--served-model-name", model, alias_name]
    if tp > 1:
        argv += ["--nnodes", "2", "--node-rank", "1", "--headless"]
    return argv


def _unit(role, recipe, model, node, port, tp=1, family=None):
    return ti.ResolvedUnit(role, family, recipe, model, [node], port, tp, 1)


def _plan(orch, workers=()):
    fams = [ti.ResolvedFamily(name=f[0], proxy_port=4000, units=f[1])
            for f in workers] if workers else []
    return ti.ResolvedPlan(template="t", orchestrator=orch, families=fams)


def _monkey_subprocess(monkeypatch):
    """Return (calls, setter): records every subprocess.run argv and monkeypatches
    subprocess.run to return success. Existing branch asserts on calls."""
    from unittest.mock import MagicMock
    calls = []
    seen = {"set": False}

    def mock_run(argv, **kw):
        calls.append(argv)
        return MagicMock(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("subprocess.run", mock_run)
    # _running_container_cmd also uses subprocess.run(["ssh", ...]) — default to
    # "no container" (None) unless a test overrides by name.
    monkeypatch.setattr(cluster_template, "_running_container_cmd",
                        lambda node: None, raising=False)
    return calls


def _sparkrun_runs(calls):
    return [c for c in calls if c[0] == "sparkrun" and c[1] == "run"]


def _stops(calls):
    return [c for c in calls if "stop" in c]


# ── Part 1: recreate on serve-command change ───────────────────────────────

class TestRecreateOnServeCommandChange:
    def test_command_changed_is_recreated(self, monkeypatch):
        """A unit whose --served-model-name alias changed vs the running
        container's actual command → RECREATED (stop + fresh launch), and the
        action is surfaced in result['recreated'] + the note. Never a silent
        'ensured up'."""
        calls = _monkey_subprocess(monkeypatch)
        # Running container advertises the OLD/warmup alias → drifted.
        monkeypatch.setattr(
            cluster_template, "_running_container_cmd",
            lambda node: _served_argv("orch", "warmup-model"))
        monkeypatch.setattr(cluster_template, "_existing_span_member_ips",
                            lambda: set())
        monkeypatch.setattr(cluster_template, "_running_recipes_via_sparkrun",
                            lambda: {"10.0.0.1": "~/recipes/orch.yaml"})

        orch = _unit("orchestrator", "~/recipes/orch.yaml", "orch",
                     "10.0.0.1", 8000, tp=1)
        result = cluster_template._provision_models(_plan(orch), do_launch=True)

        # It stopped the node (recreate) and relaunched with the new alias.
        assert result["recreated"], "drifted unit must be recreated, got none"
        assert len(_stops(calls)) >= 1
        runs = _sparkrun_runs(calls)
        assert len(runs) == 1
        assert "--served-model-name" in runs[0]
        assert runs[0][runs[0].index("--served-model-name") + 1] == \
            "orch orchestrator-model"
        assert "recreated" in result["note"]
        # And the note must NOT claim a clean ensure-only ("ensured up" without
        # a recreate qualifier would be the silent-drift bug we're killing).
        assert "ensured up" in result["note"]
        assert "recreated" in result["note"]

    def test_command_unchanged_stays_ensured_up(self, monkeypatch):
        """A unit whose serve command is UNCHANGED from the running container →
        NO stop, NO recreate; it just ensures-up (no unnecessary churn)."""
        calls = _monkey_subprocess(monkeypatch)
        monkeypatch.setattr(
            cluster_template, "_running_container_cmd",
            lambda node: _served_argv("orch", "orchestrator-model"))
        monkeypatch.setattr(cluster_template, "_existing_span_member_ips",
                            lambda: set())
        monkeypatch.setattr(cluster_template, "_running_recipes_via_sparkrun",
                            lambda: {"10.0.0.1": "~/recipes/orch.yaml"})

        orch = _unit("orchestrator", "~/recipes/orch.yaml", "orch",
                     "10.0.0.1", 8000, tp=1)
        result = cluster_template._provision_models(_plan(orch), do_launch=True)

        assert "recreated" not in result
        assert "refused" not in result
        assert _stops(calls) == []  # matching command → no stop/recreate
        runs = _sparkrun_runs(calls)
        assert len(runs) == 1  # still one ensure call (no-op) per unit
        assert "ensured up" in result["note"]

    def test_non_vllm_running_cmd_not_forced_recreate(self, monkeypatch):
        """A running command we cannot recognize as a vLLM serve (no `serve`
        subcommand — e.g. a non-vLLM runtime) must NOT be force-recreated. The
        guard treats it as uninspectable to avoid a false-positive recreation
        loop, while real vLLM flag drift is always caught."""
        calls = _monkey_subprocess(monkeypatch)
        monkeypatch.setattr(
            cluster_template, "_running_container_cmd",
            lambda node: ["llama-server", "-m", "/models/q4.gguf", "--port", "8000"])
        monkeypatch.setattr(cluster_template, "_existing_span_member_ips",
                            lambda: set())
        monkeypatch.setattr(cluster_template, "_running_recipes_via_sparkrun",
                            lambda: {"10.0.0.1": "~/recipes/orch.yaml"})

        orch = _unit("orchestrator", "~/recipes/orch.yaml", "orch",
                     "10.0.0.1", 8000, tp=1)
        result = cluster_template._provision_models(_plan(orch), do_launch=True)

        assert "recreated" not in result  # not force-recreated
        assert _stops(calls) == []
        assert "ensured up" in result["note"]

    def test_tp_span_command_changed_recreates_whole_span(self, monkeypatch):
        """A tp=2 span whose serve command changed is recreated across BOTH
        nodes (stop both, relaunch with comma-hosts + --tp)."""
        calls = _monkey_subprocess(monkeypatch)
        monkeypatch.setattr(
            cluster_template, "_running_container_cmd",
            lambda node: _served_argv("deepseek", "worker-model", tp=2))
        monkeypatch.setattr(cluster_template, "_existing_span_member_ips",
                            lambda: set())
        monkeypatch.setattr(cluster_template, "_running_recipes_via_sparkrun",
                            lambda: {})

        orch = _unit("orchestrator", "~/recipes/orch.yaml", "orch",
                     "10.0.0.1", 9000, tp=1)
        span = ti.ResolvedUnit("worker", "reasoning", "~/recipes/deepseek.yaml",
                               "deepseek", ["10.0.0.2", "10.0.0.3"], 8000, 2, 1)
        fam = ti.ResolvedFamily(name="reasoning", proxy_port=None, units=[span])
        plan = ti.ResolvedPlan(template="t", orchestrator=orch, families=[fam])

        # Wanted alias for this span is worker-model == running → same model, so
        # to force a drift we make the running command's model differ instead.
        monkeypatch.setattr(
            cluster_template, "_running_container_cmd",
            lambda node: _served_argv("stale-model", "worker-model", tp=2))

        result = cluster_template._provision_models(plan, do_launch=True)

        assert result["recreated"], "drifted span must be recreated"
        stopped_nodes = {s for s in result["stopped"]}
        assert {"10.0.0.2", "10.0.0.3"} <= stopped_nodes  # both peers stopped
        # fresh launch spans both nodes with --tp 2
        span_runs = [c for c in _sparkrun_runs(calls) if "10.0.0.2,10.0.0.3" in str(c)]
        assert len(span_runs) == 1
        assert "--tp" in span_runs[0]
        assert span_runs[0][span_runs[0].index("--tp") + 1] == "2"


# ── Part 2: never double-provision a span member ───────────────────────────

class TestNeverSoloProvisionSpanMember:
    def test_solo_unit_on_span_member_is_refused(self, monkeypatch):
        """A SOLO unit whose node is already a tp-span member → refused loudly
        and never launched (no double-provision). apply reports it."""
        calls = _monkey_subprocess(monkeypatch)
        # 10.0.0.2 is already a span member in the applied serving.json.
        monkeypatch.setattr(cluster_template, "_existing_span_member_ips",
                            lambda: {"10.0.0.2"})
        monkeypatch.setattr(cluster_template, "_running_container_cmd",
                            lambda node: _served_argv("worker-model", "worker-model"))

        orch = _unit("orchestrator", "~/recipes/orch.yaml", "orch",
                     "10.0.0.1", 9000, tp=1)
        solo = _unit("worker", "~/recipes/worker.yaml", "worker-model",
                     "10.0.0.2", 8000, tp=1, family="reasoning")
        plan = _plan(orch, [("reasoning", [solo])])
        result = cluster_template._provision_models(plan, do_launch=True)

        assert result["refused"]
        assert result["refused"][0]["node"] == "10.0.0.2"
        assert "tensor-parallel span" in result["refused"][0]["reason"]
        # No sparkrun run launched for the refused node.
        runs = _sparkrun_runs(calls)
        assert not any("10.0.0.2" in str(c) for c in runs)
        assert result["status"] == "warn"
        assert "refused" in result["note"]

    def test_span_unit_on_span_member_is_allowed(self, monkeypatch):
        """A tp>1 SPAN unit may still reclaim a span-member node (the legit
        re-apply / recreate path) — the guard only blocks SOLO units."""
        calls = _monkey_subprocess(monkeypatch)
        monkeypatch.setattr(cluster_template, "_existing_span_member_ips",
                            lambda: {"10.0.0.2", "10.0.0.3"})
        monkeypatch.setattr(cluster_template, "_running_container_cmd",
                            lambda node: None)  # not running → fresh launch
        monkeypatch.setattr(cluster_template, "_running_recipes_via_sparkrun",
                            lambda: {})

        orch = _unit("orchestrator", "~/recipes/orch.yaml", "orch",
                     "10.0.0.1", 9000, tp=1)
        span = ti.ResolvedUnit("worker", "reasoning", "~/recipes/deepseek.yaml",
                               "deepseek", ["10.0.0.2", "10.0.0.3"], 8000, 2, 1)
        fam = ti.ResolvedFamily(name="reasoning", proxy_port=None, units=[span])
        plan = ti.ResolvedPlan(template="t", orchestrator=orch, families=[fam])
        result = cluster_template._provision_models(plan, do_launch=True)

        assert "refused" not in result
        assert len(_sparkrun_runs(calls)) == 2  # orch + span
        assert any("10.0.0.2,10.0.0.3" in str(c) for c in _sparkrun_runs(calls))

    def test_resolver_never_assigns_solo_on_span_member(self, monkeypatch):
        """The plan RESOLVER excludes existing span members from SOLO assignment,
        relocating them to genuinely-free nodes instead of emitting a plan that
        would double-provision one GPU."""
        existing = {"10.0.0.2"}
        plan = ti.resolve(
            ti.ClusterTemplate.from_dict({
                "name": "t", "orchestrator": "o.yaml",
                "families": [{"name": "c", "models": ["m.yaml"], "workers": "remaining"}],
            }),
            _topo_like(["10.0.0.1"], ["10.0.0.2", "10.0.0.3", "10.0.0.4"]),
            _coster=_coster_like(),
            existing_span_members=existing,
        )
        solo_nodes = [u.nodes[0] for fam in plan.families for u in fam.units if u.tp <= 1]
        assert "10.0.0.2" not in solo_nodes  # span member never solo-assigned
        assert set(solo_nodes) == {"10.0.0.3", "10.0.0.4"}

    def test_resolver_raises_when_all_candidates_are_span_members(self, monkeypatch):
        """If every candidate worker is already a span member, resolve() raises
        TemplateIntentError instead of silently emitting an orphan solo plan."""
        import pytest
        existing = {"10.0.0.2", "10.0.0.3"}
        with pytest.raises(ti.TemplateIntentError):
            ti.resolve(
                ti.ClusterTemplate.from_dict({
                    "name": "t", "orchestrator": "o.yaml",
                    "families": [{"name": "c", "models": ["m.yaml"], "workers": "remaining"}],
                }),
                _topo_like(["10.0.0.1"], ["10.0.0.2", "10.0.0.3"]),
                _coster=_coster_like(),
                existing_span_members=existing,
            )

    def test_reapply_same_dualdsv4_spans_still_resolves(self, monkeypatch):
        """The legit path is NOT broken: re-applying a dual-dsv4-like template
        where the orchestrator + reasoning are ALREADY spans (the alias-refresh
        case) still resolves to the SAME spans — the spanning branches may
        reclaim existing span members. Only SOLO assignment is restricted."""
        existing = {"10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"}
        plan = ti.resolve(
            ti.ClusterTemplate.from_dict({
                "name": "t", "orchestrator": {"recipe": "o.yaml", "tp": 2},
                "families": [{"name": "reasoning",
                              "models": [{"recipe": "m.yaml", "tp": 2}],
                              "workers": "remaining"}],
            }),
            _topo_like(["10.0.0.1"], ["10.0.0.2", "10.0.0.3", "10.0.0.4"]),
            _coster=_coster_like(),
            existing_span_members=existing,
        )
        # Orchestrator span + reasoning span, exactly as before the re-apply.
        assert plan.orchestrator.nodes == ["10.0.0.1", "10.0.0.2"]
        fam_units = [u for fam in plan.families for u in fam.units]
        assert len(fam_units) == 1
        assert fam_units[0].nodes == ["10.0.0.3", "10.0.0.4"]
        assert fam_units[0].tp == 2


# ── small helper topologies (fixtures, no live cluster) ─────────────────────

def _topo_like(orch_ips, worker_ips):
    from dataclasses import dataclass

    @dataclass
    class FakeNode:
        ip: str
        vram_free_gb: float = 120.0

    @dataclass
    class FakeTopo:
        orchestrator: FakeNode
        workers: list

    return FakeTopo(FakeNode(orch_ips[0]),
                    [FakeNode(ip) for ip in worker_ips])


def _coster_like(per_gpu=30.0, fits=True):
    import recipe_cost as rc
    return lambda r: rc.RecipeCost(r, per_gpu_total_gb=per_gpu, fits=fits)
