"""Dep-bump guard tests: fork-branch rule text + proxy.yaml/litellm drift check.

Covers t_39d7c915:
  * hscc_daemon.dep_bump_guard — the canonical fork-branch rule (branch name,
    the never-checkout-main prohibition, the post-bump verification).
  * hscc_daemon.proxy_router — the pure-read drift comparator: matching /
    missing / divergent / no-proxy-config / least-busy-trap / balancer-missing
    cases, WARN-never-fails semantics, and pure-read (input hashes unchanged).
  * verify.check_proxy_router_settings — check shape + run_all registration.
  * health.check_proxy — the proxy daemon stream carries the drift verdict on
    BOTH state writes (healthy + unhealthy), and drift never changes the
    health verdict itself.
All fixtures are fake proxy.yaml / litellm_config.yaml files under tmp paths;
nothing here reads ~/.config/sparkrun or ~/.cache/sparkrun.
"""

import hashlib
import json

from hscc_daemon import proxy_router


# ── fake-file helpers ───────────────────────────────────────────────────────

PROXY_OK = """\
aliases: {}
router_settings:
  routing_strategy: simple-shuffle
gateways:
  litellm:
    balancing: least-in-flight
"""

LITELLM_OK = """\
model_list:
- model_name: worker-model
  litellm_params:
    api_base: http://10.0.0.1:8000/v1
litellm_settings:
  drop_params: true
  callbacks:
  - sparkrun_balancer.handler
router_settings:
  routing_strategy: simple-shuffle
"""


def _w(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def _drift(tmp_path, proxy=PROXY_OK, litellm=LITELLM_OK):
    proxy_path = _w(tmp_path, "proxy.yaml", proxy) if proxy is not None else None
    lit_path = _w(tmp_path, "litellm_config.yaml", litellm) if litellm is not None else None
    return proxy_router.router_drift(proxy_config=proxy_path, litellm_config=lit_path)


# ── dep_bump_guard ──────────────────────────────────────────────────────────

class TestDepBumpGuard:
    def test_rule_names_the_live_fork_branch(self):
        from hscc_daemon import dep_bump_guard as g
        assert g.FORK_BRANCH == "feat/proxy-least-in-flight"
        assert g.FORK_REMOTE == "pom11/sparkrun"
        text = "\n".join(g.FORK_RULE_LINES)
        # The failure this guard exists to prevent, stated in the rule itself:
        assert "feat/proxy-least-in-flight" in text
        assert "checkout main" in text and "never" in text.lower()
        assert "rebase" in text
        assert "rev-parse --abbrev-ref HEAD" in text
        assert "router_settings" in text
        # The SUPERSEDED name must not survive anywhere in the canonical text.
        assert "feat/proxy-router-settings" not in text

    def test_rule_only_for_sparkrun(self):
        from hscc_daemon import dep_bump_guard as g
        assert g.fork_rule_block(["hermes-agent"]) == []
        assert g.fork_rule_block([]) == []
        assert len(g.fork_rule_block(["sparkrun"])) == len(g.FORK_RULE_LINES)
        assert g.fork_rule_block(["hermes-agent", "sparkrun"]) == list(g.FORK_RULE_LINES)


# ── proxy_router.router_drift — the four required fake cases + extras ──────

class TestRouterDrift:
    def test_matching(self, tmp_path):
        res = _drift(tmp_path)
        assert res["status"] == "ok" and res["warn"] is False
        assert "router_settings" in res["detail"]

    def test_matching_carries_both_file_names(self, tmp_path):
        # Divergence text must name BOTH files (card requirement).
        res = _drift(tmp_path, litellm=LITELLM_OK.replace(
            "routing_strategy: simple-shuffle", "routing_strategy: least-busy"))
        assert res["warn"] is True
        assert "proxy.yaml" in res["detail"]
        assert "litellm_config.yaml" in res["detail"]

    def test_missing_litellm_config_warns_with_both_paths(self, tmp_path):
        proxy_path = _w(tmp_path, "proxy.yaml", PROXY_OK)
        res = proxy_router.router_drift(
            proxy_config=proxy_path,
            litellm_config=str(tmp_path / "absent" / "litellm_config.yaml"))
        assert res["status"] == "warn" and res["warn"] is True
        assert res["detail"].startswith("WARN:")
        assert proxy_path in res["detail"]

    def test_divergent_router_settings_warns(self, tmp_path):
        res = _drift(tmp_path, litellm=LITELLM_OK.replace(
            "routing_strategy: simple-shuffle", "routing_strategy: lowest-latency"))
        assert res["status"] == "warn" and "diverged" in res["detail"]
        # drift is a WARN, never a hard failure, at every layer:
        assert res["status"] != "fail"

    def test_no_proxy_config_skips(self, tmp_path):
        res = proxy_router.router_drift(
            proxy_config=str(tmp_path / "nope" / "proxy.yaml"),
            litellm_config=_w(tmp_path, "litellm_config.yaml", LITELLM_OK))
        assert res["status"] == "skip" and res["warn"] is False
        assert "not applicable" in res["detail"]

    def test_least_busy_strategy_flagged(self, tmp_path):
        res = _drift(tmp_path,
                     proxy=PROXY_OK.replace("simple-shuffle", "least-busy"),
                     litellm=LITELLM_OK.replace("simple-shuffle", "least-busy"))
        # Even when config and emitted file AGREE, least-busy is a trap.
        assert res["warn"] is True
        assert "least-busy" in res["detail"] and "no-op" in res["detail"]
        assert "least-in-flight" in res["detail"]  # points at the working knob

    def test_balancer_requested_but_absent_warns(self, tmp_path):
        res = _drift(tmp_path, litellm=LITELLM_OK.replace(
            "  callbacks:\n  - sparkrun_balancer.handler\n", ""))
        assert res["warn"] is True
        assert "sparkrun_balancer.handler" in res["detail"]

    def test_unimplemented_balancing_warns(self, tmp_path):
        res = _drift(tmp_path, proxy=PROXY_OK.replace(
            "least-in-flight", "round-robin"))
        assert res["warn"] is True
        assert "not implemented" in res["detail"]

    def test_non_mapping_router_settings_warns(self, tmp_path):
        # The documented footgun: bare `router_settings: least-busy`.
        res = _drift(tmp_path, proxy="router_settings: least-busy\n",
                     litellm=LITELLM_OK)
        assert res["warn"] is True
        assert "not a mapping" in res["detail"]

    def test_nothing_configured_nothing_emitted_skips(self, tmp_path):
        res = _drift(tmp_path, proxy="aliases: {}\n",
                     litellm="model_list: []\nlitellm_settings:\n  drop_params: true\n")
        assert res["status"] == "skip" and res["warn"] is False

    def test_unreadable_inputs_unverified_never_fail(self, tmp_path):
        proxy_path = _w(tmp_path, "proxy.yaml", PROXY_OK)
        bad = _w(tmp_path, "litellm_config.yaml", "model_list: [unclosed")
        res = proxy_router.router_drift(proxy_config=proxy_path, litellm_config=bad)
        assert res["status"] == "unverified" and res["warn"] is False

    def test_pure_read_inputs_unchanged(self, tmp_path):
        proxy_path = _w(tmp_path, "proxy.yaml", PROXY_OK)
        lit_path = _w(tmp_path, "litellm_config.yaml", LITELLM_OK)
        before = (hashlib.sha256(open(proxy_path, "rb").read()).hexdigest(),
                  hashlib.sha256(open(lit_path, "rb").read()).hexdigest())
        proxy_router.router_drift(proxy_config=proxy_path, litellm_config=lit_path)
        proxy_router.router_drift(proxy_config=proxy_path, litellm_config=lit_path)
        after = (hashlib.sha256(open(proxy_path, "rb").read()).hexdigest(),
                 hashlib.sha256(open(lit_path, "rb").read()).hexdigest())
        assert before == after

    def test_default_paths_are_sparkruns_own_locations(self):
        assert proxy_router.PROXY_CONFIG_PATH == "~/.config/sparkrun/proxy.yaml"
        assert proxy_router.LITELLM_CONFIG_PATH == "~/.cache/sparkrun/proxy/litellm_config.yaml"


# ── verify.check_proxy_router_settings ─────────────────────────────────────

class TestVerifyCheck:
    def _check(self, tmp_path, proxy=PROXY_OK, litellm=LITELLM_OK):
        from hscc_daemon.verify import check_proxy_router_settings
        return check_proxy_router_settings(
            proxy_config=_w(tmp_path, "proxy.yaml", proxy),
            litellm_config=_w(tmp_path, "litellm_config.yaml", litellm))

    def test_ok_shape_on_match(self, tmp_path):
        res = self._check(tmp_path)
        assert res["name"] == "proxy_router_settings"
        assert res["ok"] is True and not res["detail"].startswith("WARN")

    def test_drift_is_warn_not_fail(self, tmp_path):
        """Operator rule: divergence must NEVER make the check red."""
        res = self._check(tmp_path, litellm=LITELLM_OK.replace(
            "routing_strategy: simple-shuffle", "routing_strategy: least-busy"))
        assert res["ok"] is True            # not False — WARN, per operator
        assert res["detail"].startswith("WARN:")

    def test_unreadable_is_none_not_false(self, tmp_path):
        from hscc_daemon.verify import check_proxy_router_settings
        res = check_proxy_router_settings(
            proxy_config=_w(tmp_path, "proxy.yaml", PROXY_OK),
            litellm_config=str(tmp_path / "gone" / "litellm_config.yaml"))
        # absent litellm config while proxy.yaml configures settings: WARN-ok
        assert res["ok"] is True and res["detail"].startswith("WARN:")
        # unreadable (corrupt) input: unverified, still never False
        bad = _w(tmp_path, "bad.yaml", "model_list: [unclosed")
        res2 = check_proxy_router_settings(
            proxy_config=_w(tmp_path, "proxy.yaml", PROXY_OK),
            litellm_config=str(bad))
        assert res2["ok"] is None

    def test_registered_in_run_all(self):
        from hscc_daemon import verify
        import inspect
        src = inspect.getsource(verify.run_all)
        assert "check_proxy_router_settings" in src


# ── health.check_proxy — the proxy daemon stream carries the verdict ───────

class TestProxyStreamCarriesDrift:
    """The drift verdict must land in the proxy STATE FILE on both paths
    (healthy + unhealthy) WITHOUT ever changing the health verdict itself."""

    _SERVING_NO_UNITS = {"version": 2, "units": []}

    def _setup(self, tmp_hfcc_dir, monkeypatch, drift):
        from hscc_daemon import health, serving
        from hscc_daemon import state as state_mod
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(serving, "KEEPALIVE_NODES", {"10.0.0.1"})
        monkeypatch.setattr(serving, "load_serving", lambda *a, **k: self._SERVING_NO_UNITS)
        monkeypatch.setattr(health, "_proxy_models_probe",
                            lambda url, timeout=5: {
                                "ok": True, "models": ["worker-model", "orchestrator-model"],
                                "error": None})
        monkeypatch.setattr(health, "_proxy_backend_probe",
                            lambda url=None, timeout=5: {"ok": False, "backends": set(),
                                                         "error": "stubbed"})
        monkeypatch.setattr(health, "_router_drift_probe", lambda: dict(drift))
        return health, state_dir

    def _entry(self, state_dir):
        return json.loads((state_dir / "proxy.json").read_text())

    def test_healthy_stream_records_drift_warn(self, tmp_hfcc_dir, monkeypatch):
        health, state_dir = self._setup(
            tmp_hfcc_dir, monkeypatch,
            {"status": "warn", "warn": True, "detail": "WARN: diverged"})
        assert health.check_proxy() is True          # health verdict unchanged
        entry = self._entry(state_dir)
        assert entry["ok"] is True
        assert entry["router_drift_warn"] is True    # but the stream speaks
        assert "WARN: diverged" in entry["router_drift"]

    def test_healthy_stream_records_no_drift(self, tmp_hfcc_dir, monkeypatch):
        health, state_dir = self._setup(
            tmp_hfcc_dir, monkeypatch,
            {"status": "ok", "warn": False, "detail": "carries router_settings"})
        assert health.check_proxy() is True
        entry = self._entry(state_dir)
        assert entry["router_drift_warn"] is False
        assert "carries router_settings" in entry["router_drift"]

    def test_unhealthy_stream_records_drift_too(self, tmp_hfcc_dir, monkeypatch):
        """Ordering pin: drift must be computed BEFORE the healthy/unhealthy
        branch so the unhealthy write carries it too (a unit test of the
        drift function alone cannot catch the call site moving). Mirrors
        TestCheckProxy._unhealthy_setup: reachable alias missing -> repair
        path -> run_cmd refuses (rc 1 shape)."""
        health, state_dir = self._setup(
            tmp_hfcc_dir, monkeypatch,
            {"status": "warn", "warn": True, "detail": "WARN: diverged"})
        monkeypatch.setattr(health, "_proxy_models_probe",
                            lambda url, timeout=5: {
                                "ok": True, "models": ["some-model"], "error": ""})
        from hscc_daemon import serving
        monkeypatch.setattr(serving, "load_serving", lambda *a, **k: {
            "version": 2, "units": [
                {"id": "w1", "role": "worker", "keepalive": True,
                 "nodes": ["10.0.0.1"], "port": 8001}]})
        monkeypatch.setattr(health, "run_cmd", lambda args, **k: {"ok": False})
        assert health.check_proxy() is False
        entry = self._entry(state_dir)
        assert entry["router_drift_warn"] is True

    def test_raising_probe_never_breaks_proxy_check(self, tmp_hfcc_dir, monkeypatch):
        def _boom():
            raise RuntimeError("probe exploded")
        health, state_dir = self._setup(tmp_hfcc_dir, monkeypatch, None)
        monkeypatch.setattr(health, "_router_drift_probe", _boom)
        assert health.check_proxy() is True
        entry = self._entry(state_dir)
        assert "probe raised" in entry["router_drift"]
        assert entry["router_drift_warn"] is False   # unverified never warns
