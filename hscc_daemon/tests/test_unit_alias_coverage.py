"""t_be4b94ca addendum — per-unit /v1/models alias coverage (TDD-first).

The blind spot this pins: the existing alias machinery is PROXY-level
(`_proxy_expected_aliases` + `_proxy_models_healthy` read the MERGED
catalogue at http://localhost:4000/v1/models). When one uniform_pool unit
comes up with `--served-model-name` = base name only (operator 2026-10-22:
an unquoted dry-run command line copied by hand), the proxy's merged list
still contains the aliases because ANOTHER backend serves them — so the
proxy check reports healthy while every aliased request silently funnels to
that one node.

New behavior pinned here:
  * health.check_unit_serving_aliases — probe each unit's OWN
    http://<node>:<port>/v1/models and compare against the serving.json
    expected aliases (uniform_pool: BOTH role aliases must appear on every
    unit; otherwise the unit's own role alias).
  * health._unit_alias_check_fn seam + check_proxy wiring: findings name the
    unit and the missing aliases in the log + proxy state, and DO NOT flip
    proxy health (the proxy is genuinely healthy in this shape; flipping it
    would invite the wrong remedy — a proxy restart fixes nothing).
  * declaration_agreement fifth leg `unit_alias_coverage` — the same verdict
    on the doctor/verify surface, fail-closed (unreachable unit is
    unverified, never ok).

Fabricated fixtures only (10.0.0.x placeholder range, fake /v1/models
bodies); a unit serving base-name-only MUST trigger the named warning — the
proxy-level check alone does not count as coverage.
"""

import importlib.util
import json
import os
import sys

import pytest

from hscc_daemon import health, serving

HOST_ORCH = "10.0.0.244"
HOST_WK1 = "10.0.0.247"
HOST_WK2 = "10.0.0.248"
BASE = "local-lab/Qwen-Flash-NVFP4"


def _serving(units=None, uniform=True):
    return {
        "port": 8000,
        "uniform_pool": uniform,
        "units": units if units is not None else [
            {"id": "orch", "role": "orchestrator", "nodes": [HOST_ORCH],
             "port": 8000, "recipe": "~/r/orch.yaml", "model": BASE, "tp": 1},
            {"id": "wk1", "role": "worker", "nodes": [HOST_WK1],
             "port": 8001, "recipe": "~/r/wk1.yaml", "model": BASE,
             "keepalive": True, "tp": 1},
            {"id": "wk2", "role": "worker", "nodes": [HOST_WK2],
             "port": 8002, "recipe": "~/r/wk2.yaml", "model": BASE,
             "keepalive": True, "tp": 1},
        ],
    }


def _probe_by_url(table, default_err="connection refused"):
    """Fake /v1/models probe: table maps host substring -> served ids.
    A host missing from the table behaves like an unreachable unit."""
    def probe(url, timeout=5):
        for host, models in table.items():
            if host in url:
                if models is None:
                    return {"ok": False, "models": [], "error": default_err}
                return {"ok": True, "models": list(models), "error": ""}
        return {"ok": False, "models": [], "error": default_err}
    return probe


class TestUnitExpectedAliases:
    """The expectation itself — uniform pool means BOTH aliases per unit."""

    def test_uniform_pool_expects_both_role_aliases(self):
        unit = {"id": "wk1", "role": "worker", "nodes": [HOST_WK1], "port": 8001}
        assert health.unit_expected_aliases(_serving(), unit) == {
            "worker-model", "orchestrator-model"}

    def test_uniform_pool_orchestr_unit_also_expects_both(self):
        """Under a uniform pool even the orchestrator unit must advertise the
        worker alias — that is the whole point of the pool, and exactly what
        the 2026-10-22 launch lost."""
        unit = {"id": "orch", "role": "orchestrator", "nodes": [HOST_ORCH],
                "port": 8000}
        assert health.unit_expected_aliases(_serving(), unit) == {
            "worker-model", "orchestrator-model"}

    def test_non_uniform_expects_own_role_alias(self):
        wk = {"id": "wk1", "role": "worker", "nodes": [HOST_WK1], "port": 8001}
        oc = {"id": "orch", "role": "orchestrator", "nodes": [HOST_ORCH],
              "port": 8000}
        assert health.unit_expected_aliases(_serving(uniform=False), wk) == {"worker-model"}
        assert health.unit_expected_aliases(_serving(uniform=False), oc) == {"orchestrator-model"}


class TestCheckUnitServingAliases:
    """The per-unit probe + comparison (health.py, next to
    _proxy_models_probe — the per-backend granularity _fleet_unit_backends
    already uses for liveness)."""

    def test_base_name_only_unit_is_named_with_missing_aliases(self):
        """THE incident fixture: wk2 answers 200 with the BASE NAME ONLY
        (no aliases) while the other units serve the full set. The finding
        must name wk2 AND the missing aliases — a proxy-level pass must not
        cover for it."""
        probe = _probe_by_url({
            HOST_ORCH: [BASE, "worker-model", "orchestrator-model"],
            HOST_WK1: [BASE, "worker-model", "orchestrator-model"],
            HOST_WK2: [BASE],                       # <-- the unquoted launch
        })
        res = health.check_unit_serving_aliases(serving_data=_serving(),
                                                probe=probe)
        assert res["ok"] is False
        assert len(res["findings"]) == 1
        assert "wk2" in res["findings"][0]
        assert "worker-model" in res["findings"][0]
        assert "orchestrator-model" in res["findings"][0]
        assert HOST_WK2 in res["findings"][0]

    def test_all_units_serving_aliases_is_ok(self):
        probe = _probe_by_url({
            HOST_ORCH: [BASE, "worker-model", "orchestrator-model"],
            HOST_WK1: [BASE, "worker-model", "orchestrator-model"],
            HOST_WK2: [BASE, "worker-model", "orchestrator-model"],
        })
        res = health.check_unit_serving_aliases(serving_data=_serving(),
                                                probe=probe)
        assert res["ok"] is True
        assert res["findings"] == [] and res["unverified"] == []

    def test_unreachable_unit_is_unverified_never_ok(self):
        """Fail-closed doctrine: 'could not ask' is not 'serves the aliases'.
        A down unit is loading/absent — reported as unverified, and the
        verdict is None (not True) while nothing else proves a mismatch."""
        probe = _probe_by_url({
            HOST_ORCH: [BASE, "worker-model", "orchestrator-model"],
            HOST_WK1: [BASE, "worker-model", "orchestrator-model"],
            HOST_WK2: None,
        })
        res = health.check_unit_serving_aliases(serving_data=_serving(),
                                                probe=probe)
        assert res["ok"] is None
        assert res["findings"] == []
        assert any("wk2" in u for u in res["unverified"])

    def test_missing_alias_and_unverified_report_both(self):
        probe = _probe_by_url({
            HOST_ORCH: [BASE, "worker-model", "orchestrator-model"],
            HOST_WK1: [BASE, "worker-model"],       # missing orchestrator-model
            HOST_WK2: None,
        })
        res = health.check_unit_serving_aliases(serving_data=_serving(),
                                                probe=probe)
        assert res["ok"] is False, "a real mismatch outranks an unverified unit"
        assert len(res["findings"]) == 1 and "wk1" in res["findings"][0]
        assert len(res["unverified"]) == 1

    def test_non_uniform_only_own_role_alias_required(self):
        """A non-uniform worker legitimately does NOT serve the orchestrator
        alias — flagging it would warn on every healthy fleet."""
        probe = _probe_by_url({
            HOST_ORCH: [BASE, "orchestrator-model"],
            HOST_WK1: [BASE, "worker-model"],
        })
        res = health.check_unit_serving_aliases(
            serving_data=_serving(units=[
                {"id": "orch", "role": "orchestrator", "nodes": [HOST_ORCH],
                 "port": 8000, "model": BASE},
                {"id": "wk1", "role": "worker", "nodes": [HOST_WK1],
                 "port": 8001, "model": BASE},
            ], uniform=False), probe=probe)
        assert res["ok"] is True and res["findings"] == []

    def test_no_units_is_ok_trivially(self):
        res = health.check_unit_serving_aliases(
            serving_data={"port": 8000, "units": []},
            probe=lambda url, timeout=5: pytest.fail("must not probe"))
        assert res["ok"] is True and res["findings"] == []

    def test_default_probe_is_the_models_probe(self, monkeypatch):
        """The per-unit probe reuses _proxy_models_probe (GET + parse body,
        bounded timeout) — patching that one seam keeps every test (and the
        daemon's own tick) hermetic from sockets."""
        seen = []

        def stub(url, timeout=5):
            seen.append(url)
            return {"ok": True, "models": [BASE, "worker-model",
                                           "orchestrator-model"], "error": ""}
        monkeypatch.setattr(health, "_proxy_models_probe", stub)
        res = health.check_unit_serving_aliases(serving_data=_serving())
        assert res["ok"] is True
        assert len(seen) == 3
        assert all(u.startswith("http://10.0.0.") and u.endswith("/v1/models")
                   for u in seen)


class TestCheckProxyWiring:
    """check_proxy surfaces the finding WITHOUT flipping proxy health."""

    def _wire(self, monkeypatch, alias_result):
        logs = []
        monkeypatch.setattr(serving, "KEEPALIVE_NODES", {HOST_WK1, HOST_WK2})
        monkeypatch.setattr(serving, "load_serving", lambda *a, **k: _serving())
        # Proxy itself healthy: merged catalogue serves both aliases.
        monkeypatch.setattr(
            health, "_proxy_models_probe",
            lambda url, timeout=5: {"ok": True,
                                    "models": [BASE, "worker-model",
                                               "orchestrator-model"],
                                    "error": ""})
        monkeypatch.setattr(health, "_router_drift_probe",
                            lambda: {"status": "skip", "warn": False,
                                     "detail": "stubbed"})
        monkeypatch.setattr(
            health, "_proxy_backend_probe",
            lambda timeout=5: {"ok": True,
                               "backends": {f"{HOST_WK1}:8001",
                                            f"{HOST_WK2}:8002",
                                            f"{HOST_ORCH}:8000"},
                               "error": ""})
        monkeypatch.setattr(health, "http_check",
                            lambda url, timeout=5: {"ok": True, "message": ""})
        # The unit-level verdict, injected through the seam.
        monkeypatch.setattr(health, "_unit_alias_check_fn",
                            lambda serving_data=None: dict(alias_result))
        monkeypatch.setattr(health, "log", lambda msg, level="INFO": logs.append((msg, level)))
        return logs

    def test_finding_warns_state_names_it_and_proxy_stays_healthy(
            self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        logs = self._wire(monkeypatch, {
            "ok": False,
            "findings": [f"unit 'wk2' ({HOST_WK2}:8002) missing expected "
                         f"alias(es) ['orchestrator-model', 'worker-model']"],
            "unverified": [], "message": "1 finding"})
        assert health.check_proxy() is True, \
            "a per-unit alias gap is not a proxy fault — do not flip it"
        assert any("unit alias coverage" in m and "wk2" in m for m, _ in logs), logs
        st = state.read_state("proxy")
        assert st is not None
        assert st["ok"] is True
        assert st["unit_alias_warn"] is True

    def test_clean_coverage_writes_no_warn(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        self._wire(monkeypatch, {"ok": True, "findings": [], "unverified": [],
                                 "message": "all units serve the aliases"})
        assert health.check_proxy() is True
        st = state.read_state("proxy")
        assert not st.get("unit_alias_warn")

    def test_raising_check_never_takes_proxy_check_down(
            self, tmp_hfcc_dir, monkeypatch):
        self._wire(monkeypatch, {"ok": True, "findings": [], "unverified": [],
                                 "message": "unused"})
        def boom(serving_data=None):
            raise RuntimeError("probe stack overflow")
        monkeypatch.setattr(health, "_unit_alias_check_fn", boom)
        assert health.check_proxy() is True


# ── the doctor/verify surface: declaration_agreement fifth leg ──────────────

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))


def _leg_module():
    from hscc_daemon import declaration_agreement as da
    return da


class TestDeclarationFifthLeg:
    def _world(self, tmp_path, units=None, uniform=True):
        """Planted applied-state + serving fixture (no live ~/.hscc)."""
        da = _leg_module()
        applied = tmp_path / "applied_template.json"
        applied.parent.mkdir(parents=True, exist_ok=True)
        applied.write_text(json.dumps({"template": "4node-uniform",
                                       "uniform_pool": uniform}))
        return da, str(applied), _serving(units=units, uniform=uniform)

    def test_leg_named_in_legs_and_run_all(self, tmp_path):
        da, applied, srv = self._world(tmp_path)
        assert da.LEGS[-1] == "unit_alias_coverage"
        res = da.run_declaration_agreement(
            applied_state_path=applied, serving=srv,
            _http_get=lambda url, timeout=5: json.dumps(
                {"data": [{"id": BASE}, {"id": "worker-model"},
                          {"id": "orchestrator-model"}]}).encode())
        assert res["legs"][-1]["name"] == \
            "declaration_agreement.unit_alias_coverage"

    def test_base_name_only_unit_fails_the_leg_by_name(self, tmp_path):
        da, applied, srv = self._world(tmp_path)

        def http_get(url, timeout=5):
            if HOST_WK2 in url:
                return json.dumps({"data": [{"id": BASE}]}).encode()
            return json.dumps({"data": [{"id": BASE}, {"id": "worker-model"},
                                        {"id": "orchestrator-model"}]}).encode()

        leg = da.check_unit_alias_coverage(applied_state_path=applied,
                                           serving=srv, _http_get=http_get)
        assert leg["ok"] is False
        assert "wk2" in leg["detail"]
        assert "worker-model" in leg["detail"]
        assert leg.get("remediation")

    def test_unreachable_unit_makes_leg_unverified_not_ok(self, tmp_path):
        import urllib.error
        da, applied, srv = self._world(tmp_path)

        def http_get(url, timeout=5):
            if HOST_WK2 in url:
                raise urllib.error.URLError("connection refused")
            return json.dumps({"data": [{"id": BASE}, {"id": "worker-model"},
                                        {"id": "orchestrator-model"}]}).encode()

        leg = da.check_unit_alias_coverage(applied_state_path=applied,
                                           serving=srv, _http_get=http_get)
        assert leg["ok"] is None
        assert "wk2" in leg["detail"]

    def test_no_applied_state_is_unverified(self, tmp_path):
        """Fail-closed like every other leg: no applied state -> None."""
        da = _leg_module()
        leg = da.check_unit_alias_coverage(
            applied_state_path=str(tmp_path / "missing.json"),
            serving=_serving(),
            _http_get=lambda url, timeout=5: b'{"data": []}')
        assert leg["ok"] is None

    def test_full_agreement_still_aggregates_new_leg(self, tmp_path):
        da, applied, srv = self._world(tmp_path)

        def http_get(url, timeout=5):
            if HOST_WK2 in url:
                return json.dumps({"data": [{"id": BASE}]}).encode()
            return json.dumps({"data": [{"id": BASE}, {"id": "worker-model"},
                                        {"id": "orchestrator-model"}]}).encode()

        res = da.run_declaration_agreement(applied_state_path=applied,
                                           serving=srv, _http_get=http_get)
        assert res["ok"] is False, "the new leg must count in the aggregate"
