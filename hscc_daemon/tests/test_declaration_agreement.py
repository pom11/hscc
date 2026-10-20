"""Tests for hscc_daemon.declaration_agreement — the four spec §6 legs.

Fakes only (t_ac795424 doctrine): every applied state, config, proxy.yaml,
litellm config and HTTP surface is planted under tmp_path or injected. Nothing
here reads ~/.hscc, ~/.config/sparkrun, ~/.cache/sparkrun, the fleet, or the
network. Address doctrine: placeholders only (10.0.0.x / 100.64.0.1).

The fail-closed rule (spec §6 + provider_routing): a leg that cannot be read
is ok=None — never True, and never False either (a check that could not run is
not evidence something is broken). Every leg is tested in all three verdicts
plus its not-applicable case.
"""

import json
import os

import pytest

from hscc_daemon import declaration_agreement as da


# ── fixtures / helpers ───────────────────────────────────────────────────────

def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(path)


def _applied(path, *, uniform=True, declarations=None):
    state = {"template": "4node-uniform", "uniform_pool": uniform}
    if declarations is not None:
        state["declarations"] = declarations
    return _write(path / "applied_template.json", json.dumps(state))


def _decl(endpoints=None, proxy=None, caps=None):
    d = {}
    if endpoints is not None:
        d["endpoints"] = endpoints
    if proxy is not None:
        d["proxy"] = proxy
    if caps is not None:
        d["caps"] = caps
    return d


WRITTEN_DELEG = {"delegation": {
    "target": "family-coding", "url": "http://localhost:4000/v1",
    "model": "worker-model", "probe": "ok"}}

WRITTEN_PROFILE = {"profiles.writer": {
    "target": "family-coding", "url": "http://localhost:4000/v1",
    "model": "worker-model", "probe": "ok"}}


def _root_config(path, base_url="http://localhost:4000/v1",
                 delegation_base="http://localhost:4000/v1",
                 delegation_model="worker-model", custom_same=True):
    custom_url = base_url if custom_same else "http://localhost:4001/v1"
    return _write(
        path / "config.yaml",
        "model:\n"
        f"  base_url: {base_url}\n"
        "  default: orchestrator-model\n"
        "providers:\n"
        "- name: custom\n"
        f"  base_url: {custom_url}\n"
        "  model:\n"
        "    default: orchestrator-model\n"
        "delegation:\n"
        f"  base_url: {delegation_base}\n"
        f"  model: {delegation_model}\n")


def _profile_config(root, name, base_url="http://localhost:4000/v1",
                    default="worker-model"):
    return _write(root / "profiles" / name / "config.yaml",
                  "model:\n"
                  f"  base_url: {base_url}\n"
                  f"  default: {default}\n")


def _proxy_yaml(path, *, nested_rs=None, top_rs=None, balancing=None):
    lines = ["aliases: {}\n"]
    if nested_rs is not None or balancing is not None:
        lines.append("gateways:\n  litellm:\n")
        if nested_rs is not None:
            lines.append("    router_settings:\n")
            for k, v in nested_rs.items():
                lines.append(f"      {k}: {v}\n")
        if balancing is not None:
            lines.append(f"    balancing: {balancing}\n")
    if top_rs is not None:
        lines.append("router_settings:\n")
        for k, v in top_rs.items():
            lines.append(f"  {k}: {v}\n")
    return _write(path / "proxy.yaml", "".join(lines))


def _litellm_yaml(path, *, callbacks=("sparkrun_balancer.handler",),
                  rs="simple-shuffle"):
    text = ("model_list: []\n"
            "router_settings:\n"
            f"  routing_strategy: {rs}\n")
    if callbacks:
        text += "litellm_settings:\n  callbacks:\n"
        for c in callbacks:
            text += f"  - {c}\n"
    return _write(path / "litellm_config.yaml", text)


def _fake_http(responses):
    """responses: {url_substring: bytes or Exception}. Callable with .calls."""
    class _Get:
        def __init__(self):
            self.calls = []

        def __call__(self, url, timeout=5):
            self.calls.append(url)
            for sub, payload in responses.items():
                if sub in url:
                    if isinstance(payload, Exception):
                        raise payload
                    return payload
            raise ConnectionRefusedError(f"no fake for {url}")
    return _Get()


# ── shared projection: endpoint_expectations ────────────────────────────────

class TestEndpointExpectations:
    def test_absent_declarations_is_nothing_declared(self, tmp_path):
        p = _applied(tmp_path, uniform=False)
        state, err = da.load_applied_state(p)
        assert err is None
        mode, uniform, chat, written, skips = da.endpoint_expectations(state)
        assert mode is None and uniform is False
        assert chat is None and written == {} and skips == []

    def test_default_only_uniform_record_reads_uniform_flag(self, tmp_path):
        p = _applied(tmp_path, uniform=True)   # declarations key ABSENT (§7)
        state, err = da.load_applied_state(p)
        mode, uniform, chat, written, skips = da.endpoint_expectations(state)
        assert mode is None and uniform is True   # doctor reads uniform_pool

    def test_probe_refused_and_omitted_are_skips_not_written(self, tmp_path):
        decl = _decl(endpoints={
            "declared": {"chat": "family-coding", "delegation": "family-coding"},
            "mode": "proxy-alias",
            "resolved": {
                "delegation": {"target": "family-coding", "url": None,
                               "model": None, "probe": "refused",
                               "note": "no proxy for target"},
                "profiles.writer": {"target": "family-coding", "url": None,
                                    "model": None, "probe": "omitted"},
            }})
        p = _applied(tmp_path, declarations=decl)
        state, _ = da.load_applied_state(p)
        mode, uniform, chat, written, skips = da.endpoint_expectations(state)
        assert mode == "proxy-alias"
        assert chat == "family-coding"
        assert written == {}
        assert dict(skips)["delegation"].startswith("resolve refused")
        assert "omitted" in dict(skips)["profiles.writer"]

    def test_unreadable_state_is_unverified_not_a_pass(self, tmp_path):
        p = _write(tmp_path / "applied_template.json", "{ broken")
        state, err = da.load_applied_state(p)
        assert state is None and err is not None
        assert "unverified" in err

    def test_missing_state_is_unverified(self, tmp_path):
        state, err = da.load_applied_state(str(tmp_path / "gone.json"))
        assert state is None and err is not None
        assert "no applied-template state" in err


# ── leg 1: hermes <-> endpoints ──────────────────────────────────────────────

class TestHermesEndpointsLeg:
    def _world(self, tmp_path, resolved=None, root_cfg=None,
               profiles=("writer",)):
        decl = _decl(endpoints={
            "declared": {"chat": "family-coding", "delegation": "family-coding"},
            "mode": "proxy-alias",
            "resolved": resolved if resolved is not None else
                        dict(WRITTEN_DELEG, **WRITTEN_PROFILE)})
        p = _applied(tmp_path, declarations=decl)
        cfg = root_cfg or _root_config(tmp_path)
        for name in profiles:
            _profile_config(tmp_path, name)
        return p, cfg

    def test_agreement_ok(self, tmp_path):
        p, cfg = self._world(tmp_path)
        res = da.check_hermes_endpoints(
            applied_state_path=p, config=cfg,
            profiles_dir=str(tmp_path / "profiles"))
        assert res["ok"] is True
        assert "delegation" in res["detail"] and "profiles.writer" in res["detail"]

    def test_url_disagreement_is_problem(self, tmp_path):
        # root delegation points at 100.64.0.1 (placeholder tailnet host)
        # while the declaration wrote localhost — provider_routing owns the
        # fleet-node half; here the positive half catches the URL mismatch.
        cfg = _root_config(tmp_path, delegation_base="http://100.64.0.1:4000/v1")
        p, _ = self._world(tmp_path, root_cfg=cfg)
        res = da.check_hermes_endpoints(
            applied_state_path=p, config=cfg,
            profiles_dir=str(tmp_path / "profiles"))
        assert res["ok"] is False
        assert "delegation" in res["detail"]

    def test_model_id_disagreement_is_problem(self, tmp_path):
        p, cfg = self._world(tmp_path)
        # The live profile carries a stale id while the applied record says
        # apply probe-confirmed `worker-model` at the declared target.
        _profile_config(tmp_path, "writer", default="stale-old-model")
        res = da.check_hermes_endpoints(
            applied_state_path=p, config=cfg,
            profiles_dir=str(tmp_path / "profiles"))
        assert res["ok"] is False
        assert "stale-old-model" in res["detail"]

    def test_model_custom_url_split_brain_is_problem(self, tmp_path):
        """§1.2: model.base_url and custom.base_url are LITERALLY the same
        value — a split is the t_ac795424 regression class."""
        cfg = _root_config(tmp_path, custom_same=False)
        p, _ = self._world(tmp_path, root_cfg=cfg)
        res = da.check_hermes_endpoints(
            applied_state_path=p, config=cfg,
            profiles_dir=str(tmp_path / "profiles"))
        assert res["ok"] is False
        assert "literally" in res["detail"]

    def test_profile_unreadable_is_unverified_never_ok(self, tmp_path):
        p, cfg = self._world(tmp_path)
        # delete the writer profile: apply said it WROTE that file; it is gone.
        os.remove(tmp_path / "profiles" / "writer" / "config.yaml")
        res = da.check_hermes_endpoints(
            applied_state_path=p, config=cfg,
            profiles_dir=str(tmp_path / "profiles"))
        assert res["ok"] is None and "unreadable" in res["detail"]

    def test_root_unreadable_is_unverified(self, tmp_path):
        p, cfg = self._world(tmp_path)
        res = da.check_hermes_endpoints(
            applied_state_path=p, config=str(tmp_path / "nope.yaml"),
            profiles_dir=str(tmp_path / "profiles"))
        assert res["ok"] is None

    def test_no_applied_state_is_unverified(self, tmp_path):
        res = da.check_hermes_endpoints(
            applied_state_path=str(tmp_path / "missing.json"))
        assert res["ok"] is None and "no applied-template state" in res["detail"]

    def test_pre_v4_nonuniform_record_is_not_applicable(self, tmp_path):
        p = _applied(tmp_path, uniform=False)   # declarations absent
        res = da.check_hermes_endpoints(applied_state_path=p)
        assert res["ok"] is True and "not applicable" in res["detail"]

    def test_skipped_consumers_are_named_not_silent(self, tmp_path):
        decl = _decl(endpoints={
            "declared": {"chat": "family-coding", "delegation": "family-coding"},
            "mode": "proxy-alias",
            "resolved": dict(WRITTEN_DELEG,
                             **{"profiles.writer": {
                                 "target": "family-coding", "url": None,
                                 "model": None, "probe": "refused",
                                 "note": "probe unreachable"}})})
        p = _applied(tmp_path, declarations=decl)
        cfg = _root_config(tmp_path)
        res = da.check_hermes_endpoints(
            applied_state_path=p, config=cfg,
            profiles_dir=str(tmp_path / "profiles"))
        assert res["ok"] is True
        assert "profiles.writer: not checked" in res["detail"]


# ── leg 2: proxy <-> declaration (nested router_settings per addendum) ───────

class TestProxyDeclarationLeg:
    def test_declared_balancing_matches_nested_read(self, tmp_path):
        decl = _decl(proxy={"declared": {"families": {"coding": {
                        "routing": "least-in-flight"}}},
                        "balancing": "least-in-flight",
                        "balancing_source": "declared", "written": True,
                        "restart_required": False, "inert": []})
        p = _applied(tmp_path, declarations=decl)
        px = _proxy_yaml(tmp_path, balancing="least-in-flight",
                         nested_rs={"routing_strategy": "simple-shuffle"})
        res = da.check_proxy_declaration(applied_state_path=p, proxy_config=px)
        assert res["ok"] is True
        assert "gateways.litellm.router_settings" in res["detail"]

    def test_top_level_router_settings_named_as_rollback_key(self, tmp_path):
        """Addendum item 1: the top-level key is the 0.3.10 fallback — the
        detail must name WHICH key sparkrun actually read (nested-first)."""
        decl = _decl(proxy={"declared": {}, "balancing": "least-in-flight",
                            "written": True, "restart_required": False,
                            "inert": []})
        p = _applied(tmp_path, declarations=decl)
        px = _proxy_yaml(tmp_path, balancing="least-in-flight",
                         top_rs={"routing_strategy": "simple-shuffle"})
        res = da.check_proxy_declaration(applied_state_path=p, proxy_config=px)
        assert res["ok"] is True
        assert "router_settings" in res["detail"]
        assert "router_settings'" in res["detail"]  # names the top-level path

    def test_balancing_disagreement_is_problem(self, tmp_path):
        decl = _decl(proxy={"declared": {}, "balancing": "least-in-flight",
                            "written": True, "restart_required": False,
                            "inert": []})
        p = _applied(tmp_path, declarations=decl)
        px = _proxy_yaml(tmp_path, balancing="simple-shuffle")
        res = da.check_proxy_declaration(applied_state_path=p, proxy_config=px)
        assert res["ok"] is False
        assert "simple-shuffle" in res["detail"]
        assert "cutover" in res["remediation"]

    def test_uniform_pool_default_is_the_expectation(self, tmp_path):
        """No proxy declaration recorded but uniform_pool -> the §2.2 blessed
        default (least-in-flight) is what doctor expects."""
        p = _applied(tmp_path, uniform=True)
        px = _proxy_yaml(tmp_path, balancing="least-in-flight")
        res = da.check_proxy_declaration(applied_state_path=p, proxy_config=px)
        assert res["ok"] is True and "blessed default" in res["detail"]
        px2 = _proxy_yaml(tmp_path, balancing=None)  # operator removed it
        res2 = da.check_proxy_declaration(applied_state_path=p, proxy_config=px2)
        assert res2["ok"] is False

    def test_written_false_record_is_unverified_not_silent_pass(self, tmp_path):
        decl = _decl(proxy={"declared": {}, "balancing": "least-in-flight",
                            "written": False, "restart_required": False,
                            "inert": []})
        p = _applied(tmp_path, declarations=decl)
        px = _proxy_yaml(tmp_path, balancing="least-in-flight")
        res = da.check_proxy_declaration(applied_state_path=p, proxy_config=px)
        assert res["ok"] is None and "NOT written" in res["detail"]

    def test_missing_proxy_yaml_with_expectation_is_unverified(self, tmp_path):
        decl = _decl(proxy={"declared": {}, "balancing": "least-in-flight",
                            "written": True, "restart_required": False,
                            "inert": []})
        p = _applied(tmp_path, declarations=decl)
        res = da.check_proxy_declaration(
            applied_state_path=p, proxy_config=str(tmp_path / "absent.yaml"))
        assert res["ok"] is None and "unverified" in res["detail"]

    def test_inert_knobs_expected_absent(self, tmp_path):
        decl = _decl(proxy={"declared": {}, "balancing": "least-in-flight",
                            "written": True, "restart_required": False,
                            "inert": [{"family": "coding",
                                       "knobs": {"max_parallel_requests": 2},
                                       "status": "INERT"}]})
        p = _applied(tmp_path, declarations=decl)
        px = _proxy_yaml(tmp_path, balancing="least-in-flight")
        res = da.check_proxy_declaration(applied_state_path=p, proxy_config=px)
        assert res["ok"] is True and "INERT-PENDING-SPARKRUN" in res["detail"]
        # proxy.yaml carrying a model_list region anyway => drift named
        px2 = _write(tmp_path / "proxy2.yaml",
                     "gateways:\n  litellm:\n    balancing: least-in-flight\n"
                     "model_list:\n- x\n")
        res2 = da.check_proxy_declaration(applied_state_path=p,
                                          proxy_config=str(px2))
        assert res2["ok"] is False and "model_list" in res2["detail"]

    def test_restart_pending_is_reported_not_fixed(self, tmp_path):
        decl = _decl(proxy={"declared": {}, "balancing": "least-in-flight",
                            "written": True, "restart_required": True,
                            "inert": []})
        p = _applied(tmp_path, declarations=decl)
        px = _proxy_yaml(tmp_path, balancing="least-in-flight")
        res = da.check_proxy_declaration(applied_state_path=p, proxy_config=px)
        assert res["ok"] is True
        assert "cutover" in res["detail"]      # names the unit, never restarts
        assert "never restarts" in res["detail"]

    def test_not_applicable_nonuniform_undeclared(self, tmp_path):
        p = _applied(tmp_path, uniform=False)
        res = da.check_proxy_declaration(
            applied_state_path=p, proxy_config=str(tmp_path / "absent.yaml"))
        assert res["ok"] is True and "not applicable" in res["detail"]


# ── leg 3: balancer pair (WARN doctrine, ONE implementation) ─────────────────

class TestBalancerPairLeg:
    def _world(self, tmp_path, *, callbacks=("sparkrun_balancer.handler",),
               module=True, balancing="least-in-flight"):
        px = _proxy_yaml(tmp_path, balancing=balancing,
                         nested_rs={"routing_strategy": "simple-shuffle"})
        lc = _litellm_yaml(tmp_path, callbacks=callbacks)
        mod = tmp_path / "sparkrun_balancer.py"
        if module:
            mod.write_text("handler = object()\n")
        return px, lc, str(mod)

    def test_pair_live_is_ok(self, tmp_path):
        px, lc, mod = self._world(tmp_path)
        res = da.check_balancer_pair(proxy_config=px, litellm_config=lc,
                                     balancer_module=mod)
        assert res["ok"] is True and "pair live" in res["detail"]

    def test_missing_callback_is_warn_never_fail(self, tmp_path):
        px, lc, mod = self._world(tmp_path, callbacks=())
        res = da.check_balancer_pair(proxy_config=px, litellm_config=lc,
                                     balancer_module=mod)
        assert res["ok"] is True          # WARN, per operator doctrine
        assert res["detail"].startswith("WARN:")

    def test_missing_module_is_warn_naming_remediation(self, tmp_path):
        px, lc, mod = self._world(tmp_path, module=False)
        res = da.check_balancer_pair(proxy_config=px, litellm_config=lc,
                                     balancer_module=mod)
        assert res["ok"] is True and res["detail"].startswith("WARN:")
        assert "sparkrun_balancer.py absent" in res["detail"]
        assert "never auto-repaired" in res["remediation"]
        assert "cutover" in res["remediation"]

    def test_no_proxy_yaml_is_not_applicable(self, tmp_path):
        res = da.check_balancer_pair(
            proxy_config=str(tmp_path / "absent.yaml"),
            litellm_config=str(tmp_path / "lit.yaml"))
        assert res["ok"] is True and "not applicable" in res["detail"]

    def test_unreadable_litellm_config_is_unverified(self, tmp_path):
        px = _proxy_yaml(tmp_path, balancing="least-in-flight")
        lc = _write(tmp_path / "litellm_config.yaml", "litellm_settings: [oops")
        res = da.check_balancer_pair(proxy_config=px, litellm_config=lc)
        assert res["ok"] is None

    def test_balancer_not_requested_no_warn(self, tmp_path):
        px = _proxy_yaml(tmp_path, balancing=None,
                         nested_rs={"routing_strategy": "simple-shuffle"})
        lc = _litellm_yaml(tmp_path, callbacks=())
        res = da.check_balancer_pair(proxy_config=px, litellm_config=lc)
        assert res["ok"] is True and "requests no balancer" in res["detail"]


# ── leg 4: live model info ───────────────────────────────────────────────────

class TestLiveModelInfoLeg:
    def _applied_with_writes(self, tmp_path, resolved=None):
        decl = _decl(endpoints={
            "declared": {"chat": "family-coding", "delegation": "family-coding"},
            "mode": "proxy-alias",
            "resolved": resolved or dict(WRITTEN_DELEG, **WRITTEN_PROFILE)})
        return _applied(tmp_path, declarations=decl)

    def test_coverage_ok_one_fetch_per_distinct_url(self, tmp_path):
        p = self._applied_with_writes(tmp_path)
        body = json.dumps({"data": [{"model_name": "worker-model"},
                                    {"model_name": "orchestrator-model"}]}).encode()
        get = _fake_http({"model/info": body})
        res = da.check_live_model_info(applied_state_path=p, _http_get=get)
        assert res["ok"] is True
        assert len(get.calls) == 1                     # deduped by URL
        assert get.calls[0].endswith("/v1/model/info")
        assert "/v1/models" not in "".join(get.calls)  # spec names model/info

    def test_missing_alias_is_problem_naming_load_instrument(self, tmp_path):
        p = self._applied_with_writes(tmp_path)
        body = json.dumps({"data": [{"model_name": "other-model"}]}).encode()
        res = da.check_live_model_info(
            applied_state_path=p, _http_get=_fake_http({"model/info": body}))
        assert res["ok"] is False
        assert "worker-model" in res["detail"]
        assert "hscc cluster monitor --live" in res["remediation"]

    def test_unreachable_proxy_is_unverified_not_false(self, tmp_path):
        p = self._applied_with_writes(tmp_path)
        res = da.check_live_model_info(applied_state_path=p,
                                       _http_get=_fake_http({}))
        assert res["ok"] is None and "unreachable" in res["detail"]

    def test_no_writes_is_not_applicable(self, tmp_path):
        p = self._applied_with_writes(
            tmp_path, resolved={"delegation": {
                "target": "family-coding", "url": None, "model": None,
                "probe": "refused", "note": "x"}})
        res = da.check_live_model_info(applied_state_path=p,
                                       _http_get=_fake_http({}))
        assert res["ok"] is True and "not applicable" in res["detail"]

    def test_no_applied_state_unverified(self, tmp_path):
        res = da.check_live_model_info(
            applied_state_path=str(tmp_path / "missing.json"))
        assert res["ok"] is None


# ── the four legs as one call ────────────────────────────────────────────────

class TestRunDeclarationAgreement:
    def test_four_legs_named_as_spec(self, tmp_path):
        p = _applied(tmp_path, uniform=False)
        res = da.run_declaration_agreement(applied_state_path=p)
        assert [l["name"] for l in res["legs"]] == [
            "declaration_agreement.hermes_endpoints",
            "declaration_agreement.proxy_declaration",
            "declaration_agreement.balancer_pair",
            "declaration_agreement.live_model_info"]
        assert res["ok"] is True    # all legs resolve to not-applicable

    def test_single_leg_selection(self, tmp_path):
        p = _applied(tmp_path, uniform=False)
        res = da.run_declaration_agreement("proxy_declaration",
                                           applied_state_path=p)
        assert len(res["legs"]) == 1

    def test_unknown_leg_is_unverified(self):
        res = da.run_declaration_agreement("bogus")
        assert res["ok"] is None

    def test_fail_closed_aggregation(self, tmp_path):
        """One leg False + one leg None => overall False (False beats None);
        one leg None alone => None; never does a None leg hide a False one."""
        p = _applied(tmp_path, uniform=True)
        px = _proxy_yaml(tmp_path, balancing="simple-shuffle")  # drift -> leg2 False
        cfg = _root_config(tmp_path)   # hermetic: never the operator's real one
        res = da.run_declaration_agreement(applied_state_path=p, config=cfg,
                                           profiles_dir=str(tmp_path / "profiles"),
                                           proxy_config=px,
                                           litellm_config=str(tmp_path / "x.yaml"),
                                           balancer_module=str(tmp_path / "y.py"))
        oks = {l["name"].split(".")[-1]: l["ok"] for l in res["legs"]}
        assert oks["proxy_declaration"] is False
        assert res["ok"] is False

    def test_false_beats_none_when_both_present(self, tmp_path):
        """The fail-closed ORDER, not just the set: with a leg that DISAGREES
        and another leg that is UNREADABLE in the same run, the overall verdict
        is False — an unverified leg must never soften a known disagreement
        into 'unknown' (an operator triaging 'unknown' waits; 'False' acts)."""
        decl = _decl(endpoints={
            "declared": {"delegation": "family-coding"},
            "mode": "proxy-alias",
            "resolved": dict(WRITTEN_DELEG)})   # leg 4 will try to fetch it
        p = _applied(tmp_path, declarations=decl)
        px = _proxy_yaml(tmp_path, balancing="simple-shuffle")  # leg 2 False
        cfg = _root_config(tmp_path)
        res = da.run_declaration_agreement(
            applied_state_path=p, config=cfg,
            profiles_dir=str(tmp_path / "profiles"),
            proxy_config=px,
            litellm_config=str(tmp_path / "x.yaml"),
            balancer_module=str(tmp_path / "y.py"),
            _http_get=_fake_http({}))          # leg 4 unreachable -> None
        oks = {l["name"].split(".")[-1]: l["ok"] for l in res["legs"]}
        assert oks["proxy_declaration"] is False
        assert oks["live_model_info"] is None
        assert res["ok"] is False

    def test_next_step_names_load_instrument_on_failure(self, tmp_path):
        p = _applied(tmp_path, uniform=True)
        px = _proxy_yaml(tmp_path, balancing="simple-shuffle")
        cfg = _root_config(tmp_path)
        res = da.run_declaration_agreement(applied_state_path=p, config=cfg,
                                           profiles_dir=str(tmp_path / "profiles"),
                                           proxy_config=px,
                                           litellm_config=str(tmp_path / "x.yaml"),
                                           balancer_module=str(tmp_path / "y.py"))
        assert "hscc cluster monitor --live" in res["next_step"]


# ── verify wrapper registration (C3 acceptance: FOUR legs as ONE check) ─────

class TestVerifyRegistration:
    def test_registered_in_run_all(self):
        from hscc_daemon import verify
        import inspect
        src = inspect.getsource(verify.run_all)
        assert "check_declaration_agreement" in src

    def test_wrapper_shape_and_leg_names(self, tmp_path):
        from hscc_daemon.verify import check_declaration_agreement
        p = _applied(tmp_path, uniform=False)
        res = check_declaration_agreement(
            applied_state_path=p,
            proxy_config=str(tmp_path / "absent.yaml"),
            litellm_config=str(tmp_path / "absent2.yaml"),
            balancer_module=str(tmp_path / "absent3.py"))
        assert res["name"] == "declaration_agreement"
        assert res["ok"] is True

    def test_wrapper_surfaces_next_step_on_failure(self, tmp_path):
        from hscc_daemon.verify import check_declaration_agreement
        p = _applied(tmp_path, uniform=True)
        px = _proxy_yaml(tmp_path, balancing="none-of-the-above")
        res = check_declaration_agreement(
            applied_state_path=p, proxy_config=px,
            litellm_config=str(tmp_path / "x.yaml"),
            balancer_module=str(tmp_path / "y.py"))
        assert res["ok"] is False
        assert "hscc cluster monitor --live" in res["next_step"]
