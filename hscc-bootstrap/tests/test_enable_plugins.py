import json

import yaml

import enable_plugins


def _write(p, data):
    p.write_text(yaml.safe_dump(data))
    return str(p)


# ── plugins.enabled ──────────────────────────────────────────────────────────

def test_adds_missing_plugins(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"model": {"default": "X"},
                   "plugins": {"enabled": ["hscc-cluster"]},
                   "toolsets": ["hermes-cli", "hscc-cluster", "sparkrun"]})
    res = enable_plugins.enable(path)
    assert res["plugins"] == ["hscc-commands", "sparkrun-hermes"]
    cfg = yaml.safe_load(open(path))
    assert cfg["plugins"]["enabled"] == [
        "hscc-cluster", "hscc-commands", "sparkrun-hermes"]
    assert cfg["model"]["default"] == "X"          # rest preserved


def test_creates_enabled_when_absent(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"toolsets": ["hermes-cli", "hscc-cluster", "sparkrun"]})
    res = enable_plugins.enable(path)
    assert set(res["plugins"]) == {
        "hscc-cluster", "hscc-commands", "sparkrun-hermes"}


# ── toolsets ─────────────────────────────────────────────────────────────────

def test_adds_missing_toolset_to_list(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": [
                      "hscc-cluster", "hscc-commands", "sparkrun-hermes"]},
                   "toolsets": ["hermes-cli", "hscc-cluster", "kanban"]})
    res = enable_plugins.enable(path)
    assert res["toolsets"] == ["sparkrun", "delegation"]
    cfg = yaml.safe_load(open(path))
    assert cfg["toolsets"] == [
        "hermes-cli", "hscc-cluster", "kanban", "sparkrun", "delegation"]


def test_toolsets_json_string_normalized_to_list(tmp_path):
    # config may store toolsets as a JSON-string; we normalize to a YAML list.
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": [
                      "hscc-cluster", "hscc-commands", "sparkrun-hermes"]},
                   "toolsets": json.dumps(["hermes-cli", "kanban"])})
    res = enable_plugins.enable(path)
    assert set(res["toolsets"]) == {"hscc-cluster", "sparkrun", "delegation"}
    cfg = yaml.safe_load(open(path))
    assert isinstance(cfg["toolsets"], list)
    assert all(t in cfg["toolsets"]
               for t in ("hscc-cluster", "sparkrun", "delegation"))


def test_toolsets_absent_seeds_default_plus_hscc(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": [
                      "hscc-cluster", "hscc-commands", "sparkrun-hermes"]}})
    res = enable_plugins.enable(path)
    cfg = yaml.safe_load(open(path))
    assert cfg["toolsets"] == [
        "hermes-cli", "hscc-cluster", "sparkrun", "delegation"]
    assert set(res["toolsets"]) == {"hscc-cluster", "sparkrun", "delegation"}


# ── idempotency + guards ─────────────────────────────────────────────────────

def _fully_wired_cfg():
    return {
        "plugins": {"enabled": ["hscc-cluster", "hscc-commands", "sparkrun-hermes"]},
        "toolsets": ["hermes-cli", "hscc-cluster", "kanban", "sparkrun",
                     "delegation"],
        "kanban": {"default_assignee": enable_plugins.DEFAULT_ASSIGNEE,
                   "max_in_progress": enable_plugins.MAX_IN_PROGRESS,
                   "max_in_progress_per_profile":
                       enable_plugins.MAX_IN_PROGRESS_PER_PROFILE,
                   "auto_review": {
                       "review_roles": [r.strip() for r in enable_plugins.REVIEW_ROLES if r.strip()],
                       "reviewer": enable_plugins.REVIEWER_PROFILE},
                   "failure_limit": enable_plugins.REJECT_ESCALATE_LIMIT},
        "delegation": {"base_url": enable_plugins.WORKER_PROXY_URL,
                       "model": enable_plugins.WORKER_MODEL,
                       "provider": "custom",
                       "api_key": enable_plugins.WORKER_PROXY_KEY,
                       "max_concurrent_children":
                           enable_plugins.MAX_CONCURRENT_CHILDREN},
        "compression": {"threshold": enable_plugins.COMPACT_THRESHOLD},
        "auxiliary": {"compression": {
            "base_url": enable_plugins.COMPACT_URL,
            "model": enable_plugins.COMPACT_MODEL,
            "provider": "custom",
            "api_key": enable_plugins.COMPACT_KEY,
            "timeout": enable_plugins.COMPACT_TIMEOUT}},
        "fallback_providers": [{
            "provider": "custom",
            "model": enable_plugins.FALLBACK_MODEL,
            "base_url": enable_plugins.FALLBACK_URL,
            "api_key": enable_plugins.FALLBACK_KEY}],
        "prompt_caching": {"cache_ttl": "1hr"},
        "dashboard": {"public_url": enable_plugins.DASHBOARD_PUBLIC_URL},
        "hooks": {
            "pre_tool_call": [{"matcher": "hscc-cluster", "command": "cluster-guard.py", "timeout": 10}],
            "post_tool_call": [{"matcher": "hscc-cluster", "command": "cluster-guard.py", "timeout": 5}],
            "on_session_start": [{"command": "cluster-guard.py", "timeout": 5}],
        },
    }


def test_fully_wired_is_noop(tmp_path):
    path = _write(tmp_path / "config.yaml", _fully_wired_cfg())
    before = open(path).read()
    res = enable_plugins.enable(path)
    assert res == {"plugins": [], "toolsets": [], "kanban": [], "delegation": [], "compaction": [], "fallback": [], "bitwarden": [], "prompt_caching": [], "dashboard": [], "hooks": []}
    assert open(path).read() == before              # no rewrite, no backup churn


def test_missing_config_noop(tmp_path):
    res = enable_plugins.enable(str(tmp_path / "nope.yaml"))
    assert res == {"plugins": [], "toolsets": [], "kanban": [], "delegation": [], "compaction": [], "fallback": [], "bitwarden": [], "prompt_caching": [], "dashboard": [], "hooks": []}


# ── fleet routing (kanban + delegation) ──────────────────────────────────────

def test_routing_filled_on_fresh_config(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": [
                      "hscc-cluster", "hscc-commands", "sparkrun-hermes"]},
                   "toolsets": ["hermes-cli", "hscc-cluster", "kanban", "sparkrun"]})
    res = enable_plugins.enable(path)
    assert "default_assignee" in res["kanban"]
    assert set(res["delegation"]) == {
        "base_url", "model", "provider", "api_key", "max_concurrent_children"}
    cfg = yaml.safe_load(open(path))
    assert cfg["kanban"]["default_assignee"] == enable_plugins.DEFAULT_ASSIGNEE
    assert cfg["kanban"]["max_in_progress"] == enable_plugins.MAX_IN_PROGRESS
    assert cfg["delegation"]["base_url"] == enable_plugins.WORKER_PROXY_URL
    assert cfg["delegation"]["max_concurrent_children"] == \
        enable_plugins.MAX_CONCURRENT_CHILDREN


def test_routing_preserves_operator_choices(tmp_path):
    # An operator-set default_assignee + a LARGER cap + a custom delegation
    # endpoint must all be kept.
    cfg = _fully_wired_cfg()
    cfg["kanban"]["default_assignee"] = "my-special-worker"
    cfg["kanban"]["max_in_progress"] = 99           # larger than default
    cfg["delegation"]["base_url"] = "http://my-proxy:9000/v1"
    path = _write(tmp_path / "config.yaml", cfg)
    res = enable_plugins.enable(path)
    assert res == {"plugins": [], "toolsets": [], "kanban": [], "delegation": [], "compaction": [], "fallback": [], "bitwarden": [], "prompt_caching": [], "dashboard": [], "hooks": []}
    out = yaml.safe_load(open(path))
    assert out["kanban"]["default_assignee"] == "my-special-worker"
    assert out["kanban"]["max_in_progress"] == 99   # not lowered
    assert out["delegation"]["base_url"] == "http://my-proxy:9000/v1"


def test_fallback_seeded_when_absent(tmp_path):
    """M5: a fresh config gets a worker-LB fallback provider."""
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": ["hscc-cluster"]}, "toolsets": ["kanban"]})
    res = enable_plugins.enable(path)
    assert res["fallback"] == ["fallback_providers"]
    fp = yaml.safe_load(open(path))["fallback_providers"]
    assert fp[0]["base_url"] == enable_plugins.FALLBACK_URL
    assert fp[0]["model"] == enable_plugins.FALLBACK_MODEL


def test_fallback_preserves_operator_chain(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": ["hscc-cluster"]}, "toolsets": ["kanban"],
                   "fallback_providers": [{"provider": "x", "model": "m"}]})
    enable_plugins.enable(path)
    fp = yaml.safe_load(open(path))["fallback_providers"]
    assert fp == [{"provider": "x", "model": "m"}]   # untouched


def test_compaction_aux_defaults_to_orchestrator(tmp_path):
    """H1: with no override, aux.compression points at the orchestrator :8000,
    NOT the worker proxy (avoids re-arming the compaction freeze)."""
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": ["hscc-cluster"]}, "toolsets": ["kanban"]})
    enable_plugins.enable(path)
    aux = yaml.safe_load(open(path)).get("auxiliary", {}).get("compression", {})
    # aux compaction targets the orchestrator (COMPACT_URL/COMPACT_BASE_URL),
    # NOT the worker proxy (WORKER_PROXY_URL) — they are different endpoints.
    assert aux.get("base_url") == enable_plugins.COMPACT_URL
    assert "10.0.0.244" in aux["base_url"]


def test_worker_proxy_default_is_lb_4000(tmp_path):
    """WORKER_PROXY_URL must point at the LiteLLM LB (:4000), NOT raw vLLM (:8000).
    Worker traffic goes through the LB to avoid dumping on a single GPU."""
    assert enable_plugins.WORKER_PROXY_URL == "http://localhost:4000/v1"
    assert "4000" in enable_plugins.WORKER_PROXY_URL
    assert "8000" not in enable_plugins.WORKER_PROXY_URL


def test_dashboard_default_targets_gateway(tmp_path):
    """DASHBOARD_PUBLIC_URL defaults to GATEWAY_HOST:3000 (the gateway Mac
    Studio), NOT ORCH_MODEL_HOST. The gateway runs Hermes + dashboard + LB;
    the orchestrator runs vLLM :8000 only."""
    assert enable_plugins.DASHBOARD_PUBLIC_URL == f"http://{enable_plugins.GATEWAY_HOST}:3000"
    assert enable_plugins.GATEWAY_HOST != enable_plugins.ORCH_MODEL_HOST


def test_compaction_model_defaults_to_orch_model(tmp_path):
    """When COMPACT_URL defaults to orch :8000, COMPACT_MODEL must default to
    the ORCH_MODEL (nvidia/Qwen3.6-35B-A3B-NVFP4), NOT the worker model
    (Qwen/Qwen3.6-27B-FP8). A mismatch causes every compression call to 404.
    Only when COMPACT_URL is overridden to a non-orch URL does COMPACT_MODEL
    fall back to WORKER_MODEL."""
    assert enable_plugins.COMPACT_URL == f"http://{enable_plugins.ORCH_MODEL_HOST}:8000/v1"
    assert enable_plugins.COMPACT_MODEL == enable_plugins.ORCH_MODEL
    assert enable_plugins.COMPACT_MODEL != enable_plugins.WORKER_MODEL


def test_compaction_model_switches_when_url_customized(monkeypatch):
    """If the operator overrides COMPACT_URL to a non-orch endpoint, the model
    should fall back to WORKER_MODEL (not ORCH_MODEL) since the operator is
    pointing at a different cluster."""
    monkeypatch.setenv("HSCC_COMPACT_URL", "http://other-host:8000/v1")
    import importlib
    import enable_plugins as ep
    importlib.reload(ep)
    assert ep.COMPACT_MODEL == ep.WORKER_MODEL


def test_compaction_url_customized_keeps_model_override(monkeypatch):
    """When the operator overrides COMPACT_URL, an explicit HSCC_COMPACT_MODEL
    env var is STILL respected (not overridden by WORKER_MODEL default)."""
    monkeypatch.setenv("HSCC_COMPACT_URL", "http://other-host:8000/v1")
    monkeypatch.setenv("HSCC_COMPACT_MODEL", "my/custom-model")
    import importlib
    import enable_plugins as ep
    importlib.reload(ep)
    assert ep.COMPACT_MODEL == "my/custom-model"


def test_auto_review_seeded_when_absent(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": ["hscc-cluster"]},
                   "toolsets": ["kanban"]})
    enable_plugins.enable(path)
    k = yaml.safe_load(open(path))["kanban"]
    assert k["auto_review"]["reviewer"] == enable_plugins.REVIEWER_PROFILE
    assert "worker" in k["auto_review"]["review_roles"]
    assert k["failure_limit"] == enable_plugins.REJECT_ESCALATE_LIMIT


def test_auto_review_preserves_operator_choice(tmp_path):
    cfg = _fully_wired_cfg()
    cfg["kanban"]["auto_review"] = {"review_roles": ["custom"], "reviewer": "my-reviewer"}
    cfg["kanban"]["failure_limit"] = 1          # stricter than default — keep
    path = _write(tmp_path / "config.yaml", cfg)
    enable_plugins.enable(path)
    k = yaml.safe_load(open(path))["kanban"]
    assert k["auto_review"]["reviewer"] == "my-reviewer"   # not overwritten
    assert k["failure_limit"] == 1                          # not raised


def test_caps_raised_when_too_low(tmp_path):
    cfg = _fully_wired_cfg()
    cfg["kanban"]["max_in_progress"] = 2            # below default -> raise
    cfg["kanban"]["max_in_progress_per_profile"] = 1
    path = _write(tmp_path / "config.yaml", cfg)
    res = enable_plugins.enable(path)
    assert "max_in_progress" in res["kanban"]
    out = yaml.safe_load(open(path))
    assert out["kanban"]["max_in_progress"] == enable_plugins.MAX_IN_PROGRESS


def test_bad_plugins_shape_does_not_clobber(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"plugins": "weird-string",
                   "toolsets": ["hermes-cli", "hscc-cluster", "sparkrun"]})
    res = enable_plugins.enable(path)
    assert res["plugins"] == []
    assert yaml.safe_load(open(path))["plugins"] == "weird-string"


def test_bad_toolsets_shape_does_not_clobber(tmp_path):
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": [
                      "hscc-cluster", "hscc-commands", "sparkrun-hermes"]},
                   "toolsets": {"weird": "dict"}})
    res = enable_plugins.enable(path)
    assert res["toolsets"] == []
    assert yaml.safe_load(open(path))["toolsets"] == {"weird": "dict"}


# ── compaction ───────────────────────────────────────────────────────────────

def test_compaction_threshold_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(enable_plugins, "COMPACT_URL", "")  # no aux endpoint
    cfg = _fully_wired_cfg()
    cfg["compression"]["threshold"] = 0.4   # below default -> raise
    path = _write(tmp_path / "config.yaml", cfg)
    res = enable_plugins.enable(path)
    assert "threshold" in res["compaction"]
    assert yaml.safe_load(open(path))["compression"]["threshold"] == \
        enable_plugins.COMPACT_THRESHOLD


def test_compaction_aux_wired_when_url_set(tmp_path, monkeypatch):
    monkeypatch.setattr(enable_plugins, "COMPACT_URL", "http://10.0.0.1:8000/v1")
    monkeypatch.setattr(enable_plugins, "COMPACT_MODEL", "orch-model")
    # start from a config WITHOUT aux.compression so the override gets filled
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": ["hscc-cluster"]}, "toolsets": ["kanban"]})
    res = enable_plugins.enable(path)
    aux = yaml.safe_load(open(path))["auxiliary"]["compression"]
    assert aux["base_url"] == "http://10.0.0.1:8000/v1"
    assert aux["model"] == "orch-model"
    assert "aux.base_url" in res["compaction"]


def test_compaction_threshold_not_lowered(tmp_path, monkeypatch):
    monkeypatch.setattr(enable_plugins, "COMPACT_URL", "")
    cfg = _fully_wired_cfg()
    cfg["compression"]["threshold"] = 0.95   # higher than default -> keep
    path = _write(tmp_path / "config.yaml", cfg)
    enable_plugins.enable(path)
    assert yaml.safe_load(open(path))["compression"]["threshold"] == 0.95


# ── dashboard ────────────────────────────────────────────────────────────────

def test_dashboard_public_url_filled_on_fresh(tmp_path, monkeypatch):
    """A fresh config gets dashboard.public_url from the env default."""
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": ["hscc-cluster"]}, "toolsets": ["kanban"]})
    res = enable_plugins.enable(path)
    assert "public_url" in res["dashboard"]
    cfg = yaml.safe_load(open(path))
    assert cfg["dashboard"]["public_url"] == enable_plugins.DASHBOARD_PUBLIC_URL


def test_dashboard_public_url_preserved_when_set(tmp_path, monkeypatch):
    """An operator-set public_url is NOT overwritten."""
    cfg = _fully_wired_cfg()
    cfg["dashboard"]["public_url"] = "http://custom-host:9999"
    path = _write(tmp_path / "config.yaml", cfg)
    res = enable_plugins.enable(path)
    assert res["dashboard"] == []
    out = yaml.safe_load(open(path))
    assert out["dashboard"]["public_url"] == "http://custom-host:9999"


def test_dashboard_skipped_when_env_blank(tmp_path, monkeypatch):
    """When DASHBOARD_PUBLIC_URL is empty, bootstrap does not touch dashboard."""
    monkeypatch.setattr(enable_plugins, "DASHBOARD_PUBLIC_URL", "")
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": ["hscc-cluster"]}, "toolsets": ["kanban"]})
    res = enable_plugins.enable(path)
    assert res["dashboard"] == []


# ── bitwarden ────────────────────────────────────────────────────────────────

def test_bitwarden_skipped_when_no_project_id(tmp_path):
    """Without HSCC_BITWARDEN_PROJECT_ID, bitwarden section is not created."""
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": ["hscc-cluster"]}, "toolsets": ["kanban"]})
    res = enable_plugins.enable(path)
    assert res["bitwarden"] == []
    cfg = yaml.safe_load(open(path))
    assert "bitwarden" not in cfg


def test_bitwarden_enabled_when_project_id_set(tmp_path, monkeypatch):
    """HSCC_BITWARDEN_PROJECT_ID enables BSM with the given project."""
    monkeypatch.setattr(enable_plugins, "BITWARDEN_PROJECT_ID", "abc-123")
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": ["hscc-cluster"]}, "toolsets": ["kanban"]})
    res = enable_plugins.enable(path)
    assert set(res["bitwarden"]) == {"enabled", "project_id"}
    cfg = yaml.safe_load(open(path))
    assert cfg["bitwarden"]["enabled"] is True
    assert cfg["bitwarden"]["project_id"] == "abc-123"


def test_bitwarden_server_url_optional(tmp_path, monkeypatch):
    """HSCC_BITWARDEN_SERVER_URL sets the server_url when provided."""
    monkeypatch.setattr(enable_plugins, "BITWARDEN_PROJECT_ID", "abc-123")
    monkeypatch.setattr(enable_plugins, "BITWARDEN_SERVER_URL",
                        "https://vault.bitwarden.eu")
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": ["hscc-cluster"]}, "toolsets": ["kanban"]})
    res = enable_plugins.enable(path)
    assert "server_url" in res["bitwarden"]
    cfg = yaml.safe_load(open(path))
    assert cfg["bitwarden"]["server_url"] == "https://vault.bitwarden.eu"


def test_bitwarden_idempotent_when_already_configured(tmp_path, monkeypatch):
    """If bitwarden is already enabled with the same project, nothing changes."""
    monkeypatch.setattr(enable_plugins, "BITWARDEN_PROJECT_ID", "abc-123")
    cfg = {"bitwarden": {"enabled": True, "project_id": "abc-123"}}
    path = _write(tmp_path / "config.yaml", cfg)
    res = enable_plugins.enable(path)
    assert res["bitwarden"] == []


def test_bitwarden_skipped_on_bad_shape(tmp_path, monkeypatch):
    """If bitwarden config is a non-dict, we don't clobber it."""
    monkeypatch.setattr(enable_plugins, "BITWARDEN_PROJECT_ID", "abc-123")
    path = _write(tmp_path / "config.yaml",
                  {"bitwarden": "weird-string"})
    res = enable_plugins.enable(path)
    assert res["bitwarden"] == []
    assert yaml.safe_load(open(path))["bitwarden"] == "weird-string"


# ── prompt caching ───────────────────────────────────────────────────────────

def test_prompt_caching_ttl_raised_from_default(tmp_path):
    """5m cache TTL is raised to 1hr for long-running autonomous tasks."""
    path = _write(tmp_path / "config.yaml",
                  {"prompt_caching": {"cache_ttl": "5m"}})
    res = enable_plugins.enable(path)
    assert "cache_ttl" in res["prompt_caching"]
    cfg = yaml.safe_load(open(path))
    assert cfg["prompt_caching"]["cache_ttl"] == "1hr"


def test_prompt_caching_absent_seeds_default(tmp_path):
    """When prompt_caching is absent, 1hr TTL is seeded."""
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": ["hscc-cluster"]}, "toolsets": ["kanban"]})
    res = enable_plugins.enable(path)
    assert "cache_ttl" in res["prompt_caching"]
    cfg = yaml.safe_load(open(path))
    assert cfg["prompt_caching"]["cache_ttl"] == "1hr"


def test_prompt_caching_preserves_higher_ttl(tmp_path):
    """An operator-set 2hr TTL is not lowered to 1hr."""
    path = _write(tmp_path / "config.yaml",
                  {"prompt_caching": {"cache_ttl": "2hr"}})
    res = enable_plugins.enable(path)
    assert res["prompt_caching"] == []
    assert yaml.safe_load(open(path))["prompt_caching"]["cache_ttl"] == "2hr"


def test_prompt_caching_preserves_day_ttl(tmp_path):
    """A 1d TTL is not lowered."""
    path = _write(tmp_path / "config.yaml",
                  {"prompt_caching": {"cache_ttl": "1d"}})
    res = enable_plugins.enable(path)
    assert res["prompt_caching"] == []
    assert yaml.safe_load(open(path))["prompt_caching"]["cache_ttl"] == "1d"


def test_prompt_caching_skipped_on_bad_shape(tmp_path):
    """If prompt_caching is a non-dict, we don't clobber it."""
    path = _write(tmp_path / "config.yaml",
                  {"prompt_caching": "weird-string"})
    res = enable_plugins.enable(path)
    assert res["prompt_caching"] == []
    assert yaml.safe_load(open(path))["prompt_caching"] == "weird-string"


def test_parse_cache_ttl_seconds():
    """Verify the TTL parser handles all supported formats."""
    p = enable_plugins._parse_cache_ttl_seconds
    assert p("5m") == 300
    assert p("1hr") == 3600
    assert p("2h") == 7200
    assert p("1d") == 86400
    assert p("3600") == 3600
    assert p("") == 0
    assert p("bad") == 0
    assert p(None) == 0


# ── hooks ────────────────────────────────────────────────────────────────────

def test_hooks_filled_on_fresh(tmp_path):
    """A fresh config gets cluster-guard hooks wired."""
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": ["hscc-cluster"]}, "toolsets": ["kanban"]})
    res = enable_plugins.enable(path)
    assert set(res["hooks"]) == {"pre_tool_call", "post_tool_call", "on_session_start"}
    cfg = yaml.safe_load(open(path))
    assert cfg["hooks"]["pre_tool_call"][0]["matcher"] == "hscc-cluster"
    assert "cluster-guard.py" in cfg["hooks"]["pre_tool_call"][0]["command"]


def test_hooks_preserves_operator_hooks(tmp_path):
    """Operator-added hooks are not overwritten."""
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": ["hscc-cluster"]}, "toolsets": ["kanban"]})
    enable_plugins.enable(path)
    # Re-run — should be noop since hooks are already present
    res = enable_plugins.enable(path)
    assert res["hooks"] == []


# ── hooks file (integration) ─────────────────────────────────────────────────

def test_hooks_file_installed(tmp_path, monkeypatch):
    """_ensure_hooks_file copies cluster-guard.py and sets permissions."""
    hooks_src = tmp_path / "hooks_src"
    hooks_src.mkdir()
    (hooks_src / "cluster-guard.py").write_text("# hook script\n")

    hooks_dst = tmp_path / "hooks_dst"
    monkeypatch.setattr(enable_plugins, "HOOKS_DIR", str(hooks_dst))
    monkeypatch.setattr(enable_plugins, "CLUSTER_GUARD_DST",
                        str(hooks_dst / "cluster-guard.py"))

    res = enable_plugins._ensure_hooks_file(str(hooks_src))
    assert res["installed"] is True
    assert (hooks_dst / "cluster-guard.py").is_file()


def test_hooks_file_missing_source(tmp_path, monkeypatch):
    """No crash when source cluster-guard.py doesn't exist."""
    hooks_src = tmp_path / "hooks_src"
    hooks_src.mkdir()  # dir exists but no cluster-guard.py

    hooks_dst = tmp_path / "hooks_dst"
    monkeypatch.setattr(enable_plugins, "HOOKS_DIR", str(hooks_dst))
    monkeypatch.setattr(enable_plugins, "CLUSTER_GUARD_DST",
                        str(hooks_dst / "cluster-guard.py"))

    res = enable_plugins._ensure_hooks_file(str(hooks_src))
    assert res["installed"] is False
    assert "not found" in res.get("reason", "")
