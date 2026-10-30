"""Tests for the per-model ``max_num_seqs`` template key.

What is pinned here, mirroring exactly how ``gpu_memory_utilization`` is
carried (the pattern both follow):

1. VALIDATION at parse time — an int >= 1 only; bools, floats, strings and
   values < 1 are rejected offline by ``hscc template validate`` rather than
   refused mid-provision by the fleet.
2. ROUND-TRIP — ``to_dict()`` omits the key when absent, so a template that
   never mentions it reproduces its pre-change dict byte-identically (the
   goldens pin this).
3. RENDER — a resolved unit whose model set the cap emits the argv pair
   ``-o max_num_seqs=N`` in the serve command (sparkrun's repeatable
   ``-o key=value`` override — there is no dedicated flag; verified against
   the live ``sparkrun run --help``), positioned after ``--gpu-mem``.
4. DRIFT — a changed value must report as "serve command drift NOT applied"
   naming ``-o max_num_seqs``, never be normalized away, exactly like a
   changed ``--gpu-mem``.

All tests use fakes — no sparkrun exec, no cluster calls (the suite-wide
sparkrun fence in conftest.py makes any real call a hard failure).
"""

import json
from dataclasses import dataclass

import pytest

import template_intent as ti
import recipe_cost as rc
import cluster_template as ct


# ── fake topology (same shape as test_template_intent's) ─────────────────────

@dataclass
class FakeNode:
    ip: str
    vram_free_gb: float = 120.0


@dataclass
class FakeTopo:
    orchestrator: FakeNode
    workers: list


def _topo(n_workers=2, vram=120.0):
    return FakeTopo(orchestrator=FakeNode("10.0.0.1"),
                    workers=[FakeNode(f"10.0.0.{2+i}", vram) for i in range(n_workers)])


def _coster(per_gpu=30.0, fits=True):
    return lambda recipe: rc.RecipeCost(recipe, per_gpu_total_gb=per_gpu,
                                        fits=fits, tensor_parallel=1)


def _model_dict(**extra):
    d = {"recipe": "r.yaml"}
    d.update(extra)
    return d


# ── 1. validation ────────────────────────────────────────────────────────────

class TestMaxNumSeqsValidation:
    """`max_num_seqs: <int >= 1>` or nothing. Bad values fail at PARSE."""

    def test_absent_means_recipe_default(self):
        m = ti.ModelIntent.from_dict(_model_dict())
        assert m.max_num_seqs is None
        assert "max_num_seqs" not in m.to_dict()

    def test_valid_values_parse(self):
        for good in (1, 5, 64):
            m = ti.ModelIntent.from_dict(_model_dict(max_num_seqs=good))
            assert m.max_num_seqs == good

    def test_rejects_below_one(self):
        for bad in (0, -1, -100):
            with pytest.raises(ti.TemplateIntentError) as e:
                ti.ModelIntent.from_dict(_model_dict(max_num_seqs=bad))
            assert "max_num_seqs" in str(e.value)

    def test_rejects_non_int(self):
        # 5.0 is arithmetically fine but is NOT what the key means: a float in
        # YAML almost always means a misread schema, so refuse it.
        for bad in ("5", 5.0, "lots", [5], {"n": 5}):
            with pytest.raises(ti.TemplateIntentError) as e:
                ti.ModelIntent.from_dict(_model_dict(max_num_seqs=bad))
            assert "max_num_seqs" in str(e.value)

    def test_rejects_bool(self):
        # bool is an int subclass in Python — `max_num_seqs: true` would
        # otherwise parse as 1, which is never what anyone meant.
        for bad in (True, False):
            with pytest.raises(ti.TemplateIntentError) as e:
                ti.ModelIntent.from_dict(_model_dict(max_num_seqs=bad))
            assert "max_num_seqs" in str(e.value)

    def test_error_names_the_key_like_gpu_mem_does(self):
        # Structural validation surfaces the parse error text; both validators
        # must name their own key so the operator knows which line to fix.
        with pytest.raises(ti.TemplateIntentError) as e:
            ti.ModelIntent.from_dict(_model_dict(max_num_seqs=0))
        assert str(e.value).startswith("max_num_seqs")


# ── 2. round-trip ────────────────────────────────────────────────────────────

class TestMaxNumSeqsRoundTrip:
    def test_present_round_trips(self):
        m = ti.ModelIntent.from_dict(_model_dict(max_num_seqs=5))
        assert m.to_dict() == {"recipe": "r.yaml", "max_num_seqs": 5}

    def test_absent_omitted_so_pre_change_dicts_are_byte_identical(self):
        m = ti.ModelIntent.from_dict(_model_dict(tp=2, gpu_memory_utilization=0.4))
        assert "max_num_seqs" not in m.to_dict()
        assert m.to_dict() == {"recipe": "r.yaml", "tp": 2,
                               "gpu_memory_utilization": 0.4}

    def test_full_template_round_trip(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "x", "version": 4, "orchestrator": _model_dict(max_num_seqs=5),
            "families": [{"name": "coding",
                          "models": [_model_dict(max_num_seqs=3)]}]})
        d = t.to_dict()
        assert d["orchestrator"]["max_num_seqs"] == 5
        assert d["families"][0]["models"][0]["max_num_seqs"] == 3
        # and it re-parses to the same thing
        assert ti.ClusterTemplate.from_dict(d).to_dict() == d

    def test_is_a_known_model_key(self):
        # Unknown keys are a hard structural error (typo protection), so the
        # new key has to be registered or every template using it is rejected.
        assert "max_num_seqs" in ct._KNOWN_MODEL_KEYS


# ── 3. resolve() carry-through ───────────────────────────────────────────────

class TestMaxNumSeqsResolve:
    """Every placement branch must carry the intent onto the ResolvedUnit."""

    def _tpl(self, **orch_extra):
        return ti.ClusterTemplate.from_dict({
            "name": "t", "version": 3,
            "orchestrator": _model_dict(**orch_extra),
            "families": [{"name": "coding",
                          "models": [_model_dict(max_num_seqs=5)],
                          "workers": 2}]})

    def test_tp1_replication_carries_it(self):
        plan = ti.resolve(self._tpl(max_num_seqs=7), _topo(2), _coster=_coster())
        assert plan.orchestrator.max_num_seqs == 7
        for u in plan.families[0].units:
            assert u.max_num_seqs == 5

    def test_explicit_nodes_branch_carries_it(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "t", "version": 3,
            "orchestrator": _model_dict(max_num_seqs=7),
            "families": [{"name": "coding", "nodes": ["10.0.0.2"],
                          "models": [_model_dict(max_num_seqs=5)]}]})
        plan = ti.resolve(t, _topo(2), _coster=_coster())
        assert plan.orchestrator.max_num_seqs == 7
        assert plan.families[0].units[0].max_num_seqs == 5

    def test_tp_span_branch_carries_it(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "t", "version": 3,
            "orchestrator": "o.yaml",
            "families": [{"name": "coding",
                          "models": [{"recipe": "m.yaml", "tp": 2,
                                      "max_num_seqs": 4}],
                          "workers": 2}]})
        plan = ti.resolve(t, _topo(2), _coster=_coster())
        for u in plan.families[0].units:
            assert u.max_num_seqs == 4

    def test_absent_stays_none_on_the_plan(self):
        t = ti.ClusterTemplate.from_dict({
            "name": "t", "version": 3, "orchestrator": "o.yaml",
            "families": [{"name": "coding", "models": ["m.yaml"],
                          "workers": 2}]})
        plan = ti.resolve(t, _topo(2), _coster=_coster())
        assert plan.orchestrator.max_num_seqs is None
        assert all(u.max_num_seqs is None for u in plan.all_units)

    def test_resolved_unit_defaults_to_none(self):
        # The 8-positional-arg constructor form (used across test_apply.py)
        # must keep working untouched — new kw is last, defaults None.
        u = ti.ResolvedUnit("worker", "coding", "m.yaml", "M",
                            ["10.0.0.2"], 8000, 1, 1)
        assert u.max_num_seqs is None


# ── 4. serve-command render ──────────────────────────────────────────────────

def _render(gpu_mem=None, max_num_seqs=None):
    return ct._render_serve_cmd("hscc", "10.0.0.2", 8000, "/r/model.yaml",
                                "worker-model", 1, "org/model",
                                gpu_mem, max_num_seqs)


class TestMaxNumSeqsRender:
    def test_absent_by_default_so_recipe_default_wins(self):
        assert "-o" not in _render()

    def test_emitted_as_option_pair(self):
        cmd = _render(max_num_seqs=5)
        i = cmd.index("-o")
        assert cmd[i + 1] == "max_num_seqs=5"

    def test_positioned_like_gpu_mem_after_it(self):
        cmd = _render(gpu_mem=0.45, max_num_seqs=5)
        assert cmd.index("-o") > cmd.index("--gpu-mem")
        # the pair is exactly the two tokens `-o`, `max_num_seqs=5`
        assert cmd[cmd.index("-o") + 1].startswith("max_num_seqs=")

    def test_cap_alone_still_renders(self):
        cmd = _render(max_num_seqs=1)
        assert "--gpu-mem" not in cmd
        assert cmd[cmd.index("-o") + 1] == "max_num_seqs=1"


class TestMaxNumSeqsDrift:
    """A changed cap is REAL drift — named, never normalized away."""

    def test_same_cap_is_equivalent(self):
        assert ct.serve_cmds_equivalent(_render(max_num_seqs=5),
                                        _render(max_num_seqs=5)) is True

    def test_changed_cap_reads_as_drift_naming_the_knob(self):
        old, new = _render(max_num_seqs=8), _render(max_num_seqs=5)
        assert ct.serve_cmds_equivalent(old, new) is False
        diff = ct._diff_serve_cmds(old, new)
        assert "-o max_num_seqs" in diff
        assert "'8'" in diff and "'5'" in diff

    def test_adding_the_cap_reads_as_drift(self):
        # A unit that previously ran on the recipe default and is now pinned
        # genuinely needs relaunching — this must NOT be normalized away
        # (same contract as --gpu-mem's adding-it test).
        assert ct.serve_cmds_equivalent(_render(max_num_seqs=None),
                                        _render(max_num_seqs=5)) is False
        assert "-o max_num_seqs" in ct._diff_serve_cmds(
            _render(max_num_seqs=None), _render(max_num_seqs=5))

    def test_dropping_the_cap_reads_as_drift(self):
        assert ct.serve_cmds_equivalent(_render(max_num_seqs=5),
                                        _render(max_num_seqs=None)) is False

    def test_option_pair_does_not_pollute_positionals(self):
        # Without the -o normalization the pair would land in `positional`
        # next to the recipe path and the diff would lose the knob name.
        flags = ct._serve_cmd_flags(_render(max_num_seqs=5))
        assert flags["-o max_num_seqs"] == "5"
        assert not any("max_num_seqs" in p for p in flags.get("positional", []))

    def test_repeatable_option_each_pair_kept(self):
        # `-o` is repeatable in sparkrun; comparison must not collapse pairs.
        a = _render(max_num_seqs=5) + ["-o", "seed=1"]
        b = _render(max_num_seqs=5) + ["-o", "seed=2"]
        assert ct.serve_cmds_equivalent(a, b) is False
        assert "-o seed" in ct._diff_serve_cmds(a, b)

    def test_long_form_option_normalizes_the_same(self):
        # `--option key=value` is the same sparkrun option spelled long-form;
        # a recorded command using it must compare equal to the -o rendering.
        long_form = ["--option" if t == "-o" else t
                     for t in _render(max_num_seqs=5)]
        assert ct.serve_cmds_equivalent(long_form,
                                        _render(max_num_seqs=5)) is True


# ── 5. end-to-end: provision reports drift until --force-recreate ────────────

class TestProvisionReportsCapDrift:
    """apply (recreate=False) on a unit already running the same recipe with
    the OLD cap must warn 'serve command drift NOT applied' naming the knob,
    leave the unit alone, and keep its old record — until --force-recreate."""

    def _invoke(self, monkeypatch, tmp_path, *, recorded_cap, cap,
                recreate=False):
        import subprocess
        from unittest.mock import MagicMock
        calls = []

        def mock_run(argv, **kw):
            calls.append(list(argv))
            if "status" in argv:
                return MagicMock(returncode=0, stderr="",
                                 stdout="Job: ~/recipes/deepseek.yaml\n10.0.0.2\n")
            return MagicMock(returncode=0, stderr="", stdout="")
        monkeypatch.setattr(subprocess, "run", mock_run)
        # FAKES ONLY: the --ensure-skip gate probes each span node's
        # /v1/models. Report NOT serving so the provision path proceeds to the
        # drift comparison / launch (the same shape the pre-existing drift
        # tests get from an unreachable fake IP), without any network I/O.
        monkeypatch.setattr(ct, "_unit_models_probe",
                            lambda url, timeout=2: {"ok": False, "models": [],
                                                    "error": "fake"})

        serving_path = tmp_path / "serving.json"
        monkeypatch.setattr(ct, "SERVING_JSON", serving_path)
        rec_cmd = ct._render_serve_cmd(
            "hscc", "10.0.0.2", 8000, "~/recipes/deepseek.yaml",
            "worker-model", 1, "deepseek", None, recorded_cap)
        serving_path.write_text(json.dumps({"version": 2, "units": [
            {"id": "orch"},
            {"id": "family-coding-deepseek-2-8000", "serve_cmd": rec_cmd}]}))

        orch = ti.ResolvedUnit("orchestrator", None, "~/recipes/orch.yaml",
                               "orch", ["10.0.0.1"], 8000, 1, 1)
        unit = ti.ResolvedUnit("worker", "coding", "~/recipes/deepseek.yaml",
                               "deepseek", ["10.0.0.2"], 8000, 1, 1,
                               max_num_seqs=cap)
        plan = ti.ResolvedPlan(
            template="test", orchestrator=orch,
            families=[ti.ResolvedFamily(name="coding", proxy_port=4000,
                                        units=[unit])])
        result = ct._provision_models(plan, do_launch=True, recreate=recreate)
        return calls, result

    @staticmethod
    def _runs(calls):
        return [c for c in calls if c[0] == "sparkrun" and c[1] == "run"
                and "deepseek.yaml" in str(c)]

    def test_changed_cap_warns_drift_without_recreating(self, monkeypatch, tmp_path):
        calls, result = self._invoke(monkeypatch, tmp_path,
                                     recorded_cap=8, cap=5)
        assert result["status"] == "warn"
        w = result["warnings"][0]
        assert "serve command drift NOT applied" in w
        assert "-o max_num_seqs" in w
        assert "--force-recreate" in w
        # the running unit was NOT stopped — the cap is NOT applied
        assert [c for c in calls if c[:2] == ["sparkrun", "stop"]] == []
        # and its record still says what is actually running (cap 8)
        persisted = json.loads((tmp_path / "serving.json").read_text())
        rec = [u for u in persisted["units"]
               if u["id"] == "family-coding-deepseek-2-8000"][0]
        assert ct._serve_cmd_flags(rec["serve_cmd"])["-o max_num_seqs"] == "8"

    def test_force_recreate_applies_the_cap(self, monkeypatch, tmp_path):
        calls, result = self._invoke(monkeypatch, tmp_path,
                                     recorded_cap=8, cap=5, recreate=True)
        assert result["status"] == "ok"
        assert result["warnings"] == []
        run = self._runs(calls)[0]
        assert run[run.index("-o") + 1] == "max_num_seqs=5"
        persisted = json.loads((tmp_path / "serving.json").read_text())
        rec = [u for u in persisted["units"]
               if u["id"] == "family-coding-deepseek-2-8000"][0]
        assert ct._serve_cmd_flags(rec["serve_cmd"])["-o max_num_seqs"] == "5"

    def test_unchanged_cap_stays_silent(self, monkeypatch, tmp_path):
        """The important half of the drift contract: an unchanged fleet is
        SILENCE — no drift warning when the cap matches the record."""
        calls, result = self._invoke(monkeypatch, tmp_path,
                                     recorded_cap=5, cap=5)
        assert result["warnings"] == []
        assert result["status"] == "ok"
        assert [c for c in calls if c[:2] == ["sparkrun", "stop"]] == []


# ── 6. the shipped template ──────────────────────────────────────────────────

class TestFlashNextAllShipsCap:
    """4node/flash-next-all caps BOTH models at 5 (the per-node rule; the
    recipe default of 8 exceeds it)."""

    def test_both_models_cap_at_five(self):
        tpl = ct._load_intent("4node-flash-next-all")
        assert tpl.orchestrator.max_num_seqs == 5
        assert tpl.families, "template lost its families"
        for fam in tpl.families:
            for m in fam.models:
                assert m.max_num_seqs == 5


# ── 7. serving.json carry-through (the wake path's only source) ──────────────

class TestServingJsonCarriesCap:
    """The cap must survive into serving.json, or the wake path loses it.

    serving.json is the ONLY record the daemon's restart path reads — the
    template is never re-read by ``hscc cluster up``, autodown's wake, health's
    gentle relaunch, or auto-heal. Every one of them rebuilds the launch argv
    from these unit entries via ``hscc_daemon/serving._unit_run_cmd``, so a cap
    that is applied but not recorded here silently comes back at the RECIPE
    default after an autodown/reboot/heal cycle. (t_35dc5346.)
    """

    def _plan(self, orch_cap=None, worker_cap=None):
        d = {
            "name": "cap-plan", "version": 3,
            "orchestrator": _model_dict(**({"max_num_seqs": orch_cap}
                                           if orch_cap is not None else {})),
            "families": [{"name": "coding",
                          "models": [_model_dict(**(
                              {"max_num_seqs": worker_cap}
                              if worker_cap is not None else {}))],
                          "workers": 2}],
        }
        return ti.resolve(ti.ClusterTemplate.from_dict(d), _topo(2),
                          _coster=_coster())

    def _worker(self, sj):
        return [u for u in sj["units"] if u["role"] == "worker"][0]

    def test_worker_unit_carries_the_cap(self):
        sj = ti.to_serving_json(self._plan(worker_cap=5))
        assert self._worker(sj)["max_num_seqs"] == 5

    def test_orchestrator_unit_carries_the_cap(self):
        sj = ti.to_serving_json(self._plan(orch_cap=7))
        orch = [u for u in sj["units"] if u["role"] == "orchestrator"][0]
        assert orch["max_num_seqs"] == 7

    def test_both_caps_survive_one_resolve(self):
        # The apply path is resolve() → to_serving_json(); the cap rides from
        # the ModelIntent through ResolvedUnit into the record in one hop.
        sj = ti.to_serving_json(self._plan(orch_cap=7, worker_cap=5))
        orch = [u for u in sj["units"] if u["role"] == "orchestrator"][0]
        assert orch["max_num_seqs"] == 7
        assert self._worker(sj)["max_num_seqs"] == 5

    def test_absent_cap_omitted_not_nulled(self):
        """Omit-if-absent (the ``uniform_pool`` rule), NOT an explicit null:
        a template that never mentions the key must keep its serving.json
        byte-identical to the pre-change form, which is what the _v3_* and
        _capture_apply_golden goldens pin. An ``"max_num_seqs": null`` here
        would be a visible diff to every existing record."""
        sj = ti.to_serving_json(self._plan())
        for u in sj["units"]:
            assert "max_num_seqs" not in u, (
                f"cap key leaked into a no-cap unit {u['id']!r}")

    def test_every_worker_of_a_replicated_family_carries_it(self):
        # workers: 2 replicates the model; each resolved unit keeps the cap so
        # neither node comes back over the operator's per-node limit.
        sj = ti.to_serving_json(self._plan(worker_cap=5))
        workers = [u for u in sj["units"] if u["role"] == "worker"]
        assert len(workers) == 2
        assert all(w["max_num_seqs"] == 5 for w in workers)

    def test_key_order_appended_last_after_family(self):
        # Position matters for byte-identity of everything BEFORE it: the cap
        # is appended after the pre-existing keys so no existing unit's
        # serialization shifts.
        sj = ti.to_serving_json(self._plan(worker_cap=5))
        assert list(self._worker(sj).keys())[-1] == "max_num_seqs"

    def test_the_record_is_what_the_wake_builder_reads(self):
        """The contract this leg owns, stated as data: the cap must be a plain
        int on the unit dict under exactly the key the daemon's wake builder
        looks for (``hscc_daemon/serving.py:_max_num_seqs_from``). The
        end-to-end read-back (record → `-o max_num_seqs=N` on the start
        command) is pinned from the OTHER side in
        hscc_daemon/tests/test_serving_extra.py::TestFleetUpMaxNumSeqs, so the
        two legs are pinned against each other without this suite importing the
        daemon package (which is not importable in every leg — see
        test_apply.py's "without hscc_daemon importable" case)."""
        sj = ti.to_serving_json(self._plan(worker_cap=5))
        w = self._worker(sj)
        assert w["max_num_seqs"] == 5
        assert type(w["max_num_seqs"]) is int


