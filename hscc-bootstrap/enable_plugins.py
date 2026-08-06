"""Idempotently wire HSCC into ~/.hermes/config.yaml.

Eight things must be true for the HSCC fleet to actually work:
  1. each plugin name is in ``plugins.enabled`` (Hermes only loads enabled plugins);
  2. each plugin's toolset is in the top-level ``toolsets`` (a tool is gated by its
     toolset — without it the tool registers but no agent can call it);
  3. kanban routing sends catch-all work to a WORKER, not the orchestrator
     (``kanban.default_assignee`` + concurrency caps);
  4. orchestrator subagents run on the WORKER pool, not the gateway GPU
     (``delegation.base_url`` → the load-balanced worker proxy);
  5. Bitwarden secrets manager is enabled when HSCC_BITWARDEN_PROJECT_ID is set,
     so API keys live in BSM instead of plaintext .env / config.yaml;
  6. prompt caching TTL is set to 1hr so long-running autonomous tasks (kanban
     workers, cron jobs) benefit from cached prompt prefixes across restarts;
  7. cluster-guard hook is installed and wired so provision/restart operations
     are capacity-gated and all cluster ops are audited;
  8. dashboard public_url is configured for network access.

Bootstrap calls this so a fresh install is fully wired, not half-wired and
silently dumping all work onto the orchestrator. Safe to re-run: only fills
missing/default values, never clobbers a value the operator deliberately set,
preserves the rest of the config, backs up once before writing.

Model aliases: the defaults below name STABLE LOGICAL ALIASES
(``orchestrator-model``, ``worker-model``), NOT concrete model ids. The aliases
are resolved to a real served model id at the serving layer, so swapping a
model or template changes ONE thing (the alias mapping) instead of ~35 copied
concrete ids scattered across the config. An operator who wants a concrete id
regardless can still force one via the ``HSCC_*_MODEL`` env overrides — those
bypass the alias and are written verbatim. Bootstrap only emits the alias; the
serving layer is what resolves alias → concrete id.
"""
import os
import sys

HSCC_PLUGINS = ["hscc-cluster", "hscc-commands", "sparkrun-hermes"]

# Cluster-guard hook — lives in hscc-bootstrap/hooks/cluster-guard.py in the
# repo; installed to ~/.hermes/hooks/cluster-guard.py at bootstrap time.
HOOKS_DIR = os.path.expanduser("~/.hermes/hooks")
CLUSTER_GUARD_DST = os.path.join(HOOKS_DIR, "cluster-guard.py")
CLUSTER_GUARD_COMMAND = f"python3 {CLUSTER_GUARD_DST}"
# Toolsets the orchestrator needs:
#   hscc-cluster — cluster ops (orchestrator-only)
#   sparkrun     — sparkrun_exec passthrough
#   delegation   — the delegate_task tool, so the orchestrator can actually spawn
#                  subagents (they run on the worker pool via delegation.base_url).
#                  Without this the orchestrator has no way to delegate and does
#                  everything inline.
# hscc-commands registers slash COMMANDS (not a toolset), so it is absent here.
HSCC_TOOLSETS = ["hscc-cluster", "sparkrun", "delegation"]

# Fleet-routing defaults. The worker proxy (sparkrun LiteLLM LB) load-balances
# every worker GPU behind one URL; env-overridable to match the generator.
WORKER_PROXY_URL = os.environ.get("HSCC_WORKER_PROXY_URL", "http://localhost:4000/v1")
WORKER_MODEL = os.environ.get("HSCC_WORKER_MODEL", "worker-model")
WORKER_PROXY_KEY = os.environ.get("HSCC_WORKER_PROXY_KEY", "sk-sparkrun")
DEFAULT_ASSIGNEE = os.environ.get("HSCC_DEFAULT_ASSIGNEE", "worker")
# Concurrency: board-wide cap + per-profile cap. Defaults sized so the single
# `worker` catch-all role can run many tasks in parallel (the proxy spreads them).
MAX_IN_PROGRESS = int(os.environ.get("HSCC_MAX_IN_PROGRESS", "30"))
MAX_IN_PROGRESS_PER_PROFILE = int(os.environ.get("HSCC_MAX_IN_PROGRESS_PER_PROFILE", "10"))
# WS4 review flow: auto-pair a reviewer task behind each coder/role task, and
# escalate after N consecutive rejects (the circuit breaker's failure_limit).
REVIEW_ROLES = os.environ.get(
    "HSCC_REVIEW_ROLES",
    "worker,coder,backend-engineer,frontend-engineer,ml-engineer,data-engineer,devops-engineer").split(",")
REVIEWER_PROFILE = os.environ.get("HSCC_REVIEWER_PROFILE", "reviewer")
REJECT_ESCALATE_LIMIT = int(os.environ.get("HSCC_REJECT_ESCALATE_LIMIT", "3"))
# Subagent parallelism per delegate batch — sized for the worker pool (the proxy
# load-balances across worker GPUs). Spawn depth stays flat (1) by default;
# nesting (2+) multiplies cost and is opt-in.
MAX_CONCURRENT_CHILDREN = int(os.environ.get("HSCC_MAX_CONCURRENT_CHILDREN", "9"))
# Gateway LAN IPs — env-overridable; defaults match the live HSCC topology.
# ORCH_MODEL_HOST: the orchestrator vLLM node (runs models :8000)
# GATEWAY_HOST: the gateway Mac Studio (runs Hermes + dashboard :3000 + LB :4000)
ORCH_MODEL_HOST = os.environ.get("HSCC_ORCH_MODEL_HOST", "10.0.0.244")
GATEWAY_HOST = os.environ.get("HSCC_GATEWAY_HOST", "10.0.0.245")

# Orchestrator model alias: the vLLM model served on ORCH_MODEL_HOST:8000/v1.
# Used by compaction + the text auxiliaries when their URL defaults to the
# orchestrator endpoint. Defaults to the STABLE ALIAS ``orchestrator-model``
# (resolved to a concrete served id at the serving layer), so swapping the
# orchestrator's served model changes ONE thing — the alias mapping — not
# every copied concrete id. MUST resolve to a model actually served there: a
# stale id 404s every call and the auxiliary-client fallback chain then
# reaches a cloud provider, whose credential read throws under the
# multiplexing secret-scope guard. Env-overridable via HSCC_ORCH_MODEL to
# force a concrete id and short-circuit the alias resolution.
ORCH_MODEL = os.environ.get("HSCC_ORCH_MODEL", "orchestrator-model")

# Context-compaction: compact rarely (high threshold) and run the summarization
# on a dedicated endpoint, not the main model — otherwise a big summary prompt
# competes with real work and wedges the orchestrator (H1). Default the summary
# endpoint to the orchestrator's vLLM (fast MoE A3B) so compaction never runs on
# the busy worker proxy; override with HSCC_COMPACT_URL/_MODEL.
COMPACT_THRESHOLD = float(os.environ.get("HSCC_COMPACT_THRESHOLD", "0.8"))
# When COMPACT_URL defaults to the orch endpoint (:8000), the model MUST be the
# orchestrator-model alias — the worker-model alias resolves to a model not
# registered on the orchestrator, so every compaction call would 404. The
# derivation below reads both aliases end-to-end: orch URL → orchestrator-model,
# any other URL → worker-model. The COMPACT_MODEL env override (HSCC_COMPACT_MODEL)
# still forces a concrete id at runtime.
_DEFAULT_COMPACT_URL = f"http://{ORCH_MODEL_HOST}:8000/v1"
_COMPACT_URL_RAW = os.environ.get("HSCC_COMPACT_URL", _DEFAULT_COMPACT_URL)
_COMPACT_MODEL_DEFAULT = ORCH_MODEL if _COMPACT_URL_RAW == _DEFAULT_COMPACT_URL else WORKER_MODEL
COMPACT_URL = _COMPACT_URL_RAW
COMPACT_MODEL = os.environ.get("HSCC_COMPACT_MODEL", _COMPACT_MODEL_DEFAULT)
COMPACT_KEY = os.environ.get("HSCC_COMPACT_KEY", "sk-sparkrun")
COMPACT_TIMEOUT = int(os.environ.get("HSCC_COMPACT_TIMEOUT", "90"))

# M5: a wedged orchestrator (200-but-hangs) should degrade to the worker LB
# instead of hanging. Seed one fallback provider = the worker proxy, using the
# worker-model alias (resolved to a concrete id at the serving layer).
FALLBACK_MODEL = os.environ.get("HSCC_FALLBACK_MODEL", WORKER_MODEL)
FALLBACK_URL = os.environ.get("HSCC_FALLBACK_URL", WORKER_PROXY_URL)
FALLBACK_KEY = os.environ.get("HSCC_FALLBACK_KEY", "sk-sparkrun")

# Bitwarden Secrets Manager integration. When HSCC_BITWARDEN_PROJECT_ID is set,
# bootstrap enables BSM so API keys live in Bitwarden instead of plaintext .env
# or config.yaml. HSCC_BITWARDEN_SERVER_URL is optional (defaults to US Cloud).
BITWARDEN_PROJECT_ID = os.environ.get("HSCC_BITWARDEN_PROJECT_ID", "")
BITWARDEN_SERVER_URL = os.environ.get("HSCC_BITWARDEN_SERVER_URL", "")

# Cross-session prompt caching. Kanban workers and cron jobs run for hours;
# a 1hr cache TTL keeps the prompt prefix cached across session restarts,
# slashing token costs for long autonomous tasks. Default 3600s (1hr).
PROMPT_CACHE_TTL_SECONDS = int(os.environ.get("HSCC_PROMPT_CACHE_TTL_SECONDS", "3600"))

# Dashboard: public_url for network access. Defaults to the gateway node
# (orchestrator) on port 3000. Env-overridable. Leave blank to skip.
DASHBOARD_PUBLIC_URL = os.environ.get(
    "HSCC_DASHBOARD_PUBLIC_URL",
    f"http://{GATEWAY_HOST}:3000")


def _ensure_plugins_enabled(cfg, plugins):
    """Append missing plugin names to plugins.enabled. Returns names added.

    Returns [] without mutating on a bad/unexpected config shape.
    """
    plug = cfg.setdefault("plugins", {})
    if not isinstance(plug, dict):
        return []
    enabled = plug.setdefault("enabled", [])
    if not isinstance(enabled, list):
        return []
    added = [p for p in plugins if p not in enabled]
    enabled.extend(added)
    return added


def _ensure_toolsets(cfg, toolsets):
    """Append missing toolset names to the top-level toolsets. Returns added.

    ``toolsets`` in config may be a JSON-string or a YAML list (Hermes'
    _normalize_toolsets accepts both). We normalize to a list and write it back
    as a list. Returns [] without mutating on a bad shape.
    """
    import json

    raw = cfg.get("toolsets")
    if isinstance(raw, str):
        try:
            current = json.loads(raw)
        except (ValueError, TypeError):
            return []
    elif isinstance(raw, list):
        current = raw
    elif raw is None:
        current = ["hermes-cli"]  # Hermes' own default
    else:
        return []
    if not isinstance(current, list):
        return []

    added = [t for t in toolsets if t not in current]
    current.extend(added)
    cfg["toolsets"] = current  # normalize to a list regardless
    return added


def _ensure_kanban_routing(cfg):
    """Route catch-all kanban work to a worker + size concurrency. Returns the
    keys changed.

    Only fills an EMPTY/absent ``default_assignee`` (so an operator who set a
    specific assignee keeps it). Concurrency caps follow the same pattern as
    ``failure_limit`` in this function: a lower operator value is deliberate
    (STRICTER — limits concurrency or escalates sooner), so we preserve it.
    Only fill when absent or invalid (not an int).
    """
    k = cfg.setdefault("kanban", {})
    if not isinstance(k, dict):
        return []
    changed = []
    if not (k.get("default_assignee") or "").strip():
        k["default_assignee"] = DEFAULT_ASSIGNEE
        changed.append("default_assignee")
    for key, want in (("max_in_progress", MAX_IN_PROGRESS),
                      ("max_in_progress_per_profile", MAX_IN_PROGRESS_PER_PROFILE)):
        cur = k.get(key)
        # Mirror the failure_limit pattern: a lower operator value is
        # deliberate — STRICTER concurrency. Only fill when absent / not an int.
        if not isinstance(cur, int):
            k[key] = want
            changed.append(key)
    # WS4: auto_review pairing — only fill when absent (operator override kept).
    # If the key exists but is not a dict, preserve it (operator intent) and warn.
    if "auto_review" not in k:
        k["auto_review"] = {
            "review_roles": [r.strip() for r in REVIEW_ROLES if r.strip()],
            "reviewer": REVIEWER_PROFILE,
        }
        changed.append("auto_review")
    elif not isinstance(k.get("auto_review"), dict):
        print(f"[WARN] HSCC could not wire kanban.auto_review: expected dict, got {type(k.get('auto_review')).__name__}", file=sys.stderr)
    # WS4: escalate after N consecutive rejects. Only FILL when absent/invalid —
    # a lower operator limit is deliberately STRICTER (escalates sooner), so it
    # must be preserved, not raised.
    cur = k.get("failure_limit")
    if not isinstance(cur, int) or cur < 1:
        k["failure_limit"] = REJECT_ESCALATE_LIMIT
        changed.append("failure_limit")
    return changed


def _ensure_delegation(cfg):
    """Point orchestrator subagents at the worker proxy. Returns keys changed.

    Only fills EMPTY fields, so an operator-chosen delegation endpoint is kept.
    """
    d = cfg.setdefault("delegation", {})
    if not isinstance(d, dict):
        return []
    changed = []
    for key, want in (("base_url", WORKER_PROXY_URL), ("model", WORKER_MODEL),
                      ("provider", "custom"), ("api_key", WORKER_PROXY_KEY)):
        if not (d.get(key) or "").strip():
            d[key] = want
            changed.append(key)
    # Subagent parallelism: only RAISE toward the default (never lower a
    # deliberately higher value). Spawn depth is left to Hermes' default (flat) —
    # nesting is opt-in and not set here.
    cur = d.get("max_concurrent_children")
    if not isinstance(cur, int) or cur < MAX_CONCURRENT_CHILDREN:
        d["max_concurrent_children"] = MAX_CONCURRENT_CHILDREN
        changed.append("max_concurrent_children")
    return changed


def _ensure_compaction(cfg):
    """Set context-compaction to compact rarely + summarize off the main model.

    Raises compression.threshold toward the default (never lowers). Points
    auxiliary.compression at HSCC_COMPACT_URL if provided (else leaves it to
    Hermes' default). Only fills empty aux fields — operator choices survive.
    """
    changed = []
    comp = cfg.setdefault("compression", {})
    if isinstance(comp, dict):
        cur = comp.get("threshold")
        if not isinstance(cur, (int, float)) or cur < COMPACT_THRESHOLD:
            comp["threshold"] = COMPACT_THRESHOLD
            changed.append("threshold")
    # Only wire the aux endpoint if an operator gave one (no safe generic default
    # for the summarization host — it's cluster-specific).
    if COMPACT_URL:
        aux = cfg.setdefault("auxiliary", {}).setdefault("compression", {})
        if isinstance(aux, dict):
            for key, want in (("base_url", COMPACT_URL), ("model", COMPACT_MODEL),
                              ("provider", "custom"), ("api_key", COMPACT_KEY)):
                if want and not (aux.get(key) or "").strip():
                    aux[key] = want
                    changed.append(f"aux.{key}")
            # Only fill timeout when absent or non-int — a higher operator value
            # is deliberate and preserved (no lowering above COMPACT_TIMEOUT).
            if not isinstance(aux.get("timeout"), int):
                aux["timeout"] = COMPACT_TIMEOUT
                changed.append("aux.timeout")
    return changed


# Text-only auxiliary LLM tasks that run in the orchestration path. Left at the
# Hermes default (provider: auto), each falls through the auxiliary-client chain
# to a cloud provider (OpenAI/OpenRouter); under multiplexing that credential
# read runs with no active secret scope and THROWS, silently breaking the task
# (e.g. kanban_decomposer could not decompose any card). Route them to the local
# orchestrator model like compaction. vision + web_extract are intentionally
# excluded — they need capability-specific providers, not a text model.
_LOCAL_TEXT_AUX_TASKS = (
    "kanban_decomposer", "triage_specifier", "profile_describer",
    "curator", "title_generation", "skills_hub", "approval", "mcp",
)


def _ensure_text_auxiliaries(cfg):
    """Point the text orchestration auxiliaries at the local orchestrator model.

    Mirrors _ensure_compaction: only fills EMPTY fields (operator choices and any
    pre-set provider survive) and only when a local endpoint is available.
    Prevents the multiplexing secret-scope throw from an auto→cloud fall-through.
    """
    changed = []
    if not COMPACT_URL:
        return changed
    aux_root = cfg.setdefault("auxiliary", {})
    if not isinstance(aux_root, dict):
        return changed
    for task in _LOCAL_TEXT_AUX_TASKS:
        aux = aux_root.setdefault(task, {})
        if not isinstance(aux, dict):
            continue
        # Only claim a task still at the default auto/empty provider — never
        # override an operator who deliberately set a provider for it.
        prov = (aux.get("provider") or "").strip()
        if prov not in ("", "auto"):
            continue
        # provider must become 'custom': 'auto' is the very value that triggers
        # the cloud fall-through, so it is replaced (not fill-empty-preserved).
        if prov != "custom":
            aux["provider"] = "custom"
            changed.append(f"aux.{task}.provider")
        for key, want in (("base_url", COMPACT_URL),
                          ("model", COMPACT_MODEL), ("api_key", COMPACT_KEY)):
            if want and not (aux.get(key) or "").strip():
                aux[key] = want
                changed.append(f"aux.{task}.{key}")
    return changed


def _ensure_fallback(cfg):
    """Seed a fallback provider = the worker LB (M5), so a wedged orchestrator
    degrades to the worker tier instead of hanging. Only fills an EMPTY
    fallback_providers — an operator-defined chain is preserved.
    """
    cur = cfg.get("fallback_providers")
    if cur is not None and not isinstance(cur, list):
        # Key present but wrong shape — preserve operator value and warn.
        print(f"[WARN] HSCC could not wire fallback_providers: expected list, got {type(cur).__name__}", file=sys.stderr)
        return []
    if isinstance(cur, list) and cur:
        return []          # operator already has a chain — keep it
    cfg["fallback_providers"] = [{
        "provider": "custom",
        "model": FALLBACK_MODEL,
        "base_url": FALLBACK_URL,
        "api_key": FALLBACK_KEY,
    }]
    return ["fallback_providers"]


def _parse_cache_ttl_seconds(ttl_str: str | None) -> int:
    """Parse a cache TTL string (e.g. '5m', '1hr', '3600') into seconds.

    Supports: plain integers (seconds), 'm' suffix (minutes), 'hr'/'h' suffix
    (hours), 'd' suffix (days). Returns 0 on parse failure.
    """
    if not ttl_str or not isinstance(ttl_str, str):
        return 0
    ttl_str = ttl_str.strip().lower()
    if ttl_str.endswith("d"):
        try:
            return int(ttl_str[:-1]) * 86400
        except ValueError:
            return 0
    if ttl_str.endswith(("hr", "h")):
        try:
            return int(ttl_str[:-2 if ttl_str.endswith("hr") else -1]) * 3600
        except ValueError:
            return 0
    if ttl_str.endswith("m"):
        try:
            return int(ttl_str[:-1]) * 60
        except ValueError:
            return 0
    try:
        return int(ttl_str)
    except ValueError:
        return 0


def _ensure_bitwarden(cfg):
    """Enable Bitwarden Secrets Manager when HSCC_BITWARDEN_PROJECT_ID is set.

    Writes ``bitwarden.enabled: true`` + ``project_id``. Optionally sets
    ``server_url`` for EU Cloud or self-hosted instances. Leaves
    ``access_token_env`` and other defaults alone (Hermes handles them).

    Only activates when BITWARDEN_PROJECT_ID is non-empty. Returns keys changed.
    """
    if not BITWARDEN_PROJECT_ID:
        return []

    bw = cfg.setdefault("bitwarden", {})
    if not isinstance(bw, dict):
        return []

    changed = []
    # Enable BSM + set the project ID (the minimum required config).
    # Only set enabled=true when the key is ABSENT — an explicit false is an
    # operator deliberate disable, never overridden.
    if "enabled" not in bw:
        bw["enabled"] = True
        changed.append("enabled")
    if bw.get("project_id", "") != BITWARDEN_PROJECT_ID:
        bw["project_id"] = BITWARDEN_PROJECT_ID
        changed.append("project_id")
    # Optional: set server URL for non-default regions.
    if BITWARDEN_SERVER_URL:
        cur_url = (bw.get("server_url") or "").strip()
        if cur_url != BITWARDEN_SERVER_URL:
            bw["server_url"] = BITWARDEN_SERVER_URL
            changed.append("server_url")
    return changed


def _ensure_prompt_caching(cfg):
    """Set prompt caching TTL for long-running autonomous tasks.

    HSCC workers run for hours (up to max_runtime_seconds per kanban task);
    a 1hr cache TTL keeps the system prompt + early context cached across
    session restarts, avoiding redundant token billing for cached prefixes.

    Only raises the TTL — an operator-set higher value is preserved. Returns
    keys changed.
    """
    pc = cfg.setdefault("prompt_caching", {})
    if not isinstance(pc, dict):
        return []

    changed = []
    current_ttl = _parse_cache_ttl_seconds(pc.get("cache_ttl", "") or "")
    if current_ttl < PROMPT_CACHE_TTL_SECONDS:
        # Format: seconds → human-readable string for config.yaml.
        if PROMPT_CACHE_TTL_SECONDS >= 86400:
            pc["cache_ttl"] = f"{PROMPT_CACHE_TTL_SECONDS // 86400}d"
        elif PROMPT_CACHE_TTL_SECONDS >= 3600:
            pc["cache_ttl"] = f"{PROMPT_CACHE_TTL_SECONDS // 3600}hr"
        elif PROMPT_CACHE_TTL_SECONDS >= 60:
            pc["cache_ttl"] = f"{PROMPT_CACHE_TTL_SECONDS // 60}m"
        else:
            pc["cache_ttl"] = str(PROMPT_CACHE_TTL_SECONDS)
        changed.append("cache_ttl")
    return changed


def _ensure_multiplex(cfg):
    """Enable gateway multiplexing for per-profile session isolation.

    Hermes 0.17 gateway loader (gateway/config.py::load_gateway_config) only
    reads a TOP-LEVEL ``multiplex_profiles`` key from config.yaml — it never
    looks inside the nested ``gateway.multiplex_profiles`` that we historically
    wrote here. The nested fallback only works for the legacy ``gateway.json``.
    To make multiplexing actually activate we write BOTH forms: the top-level
    key (for the 0.17 loader) and the nested ``gateway.multiplex_profiles`` (for
    forward compatibility and for any code that still reads the nested form).

    Fill-only semantics with a cross-form disable guard: if the operator set
    EITHER form to explicit ``false``, we bail and write nothing — a
    deliberate disable is never overridden through the other form. Otherwise
    each absent form is filled. Filling the top-level key when only the
    nested one is present is the whole point: every install bootstrapped
    before this fix has nested ``true`` and no top-level key — the exact
    state where multiplexing silently stays off.

    When ``gateway`` exists but is not a dict (operator damage), we skip the
    nested write — never clobber the value with a dict — and only fill the
    top-level key.
    Returns keys changed.
    """
    gw_raw = cfg.get("gateway")
    nested_raw = gw_raw.get("multiplex_profiles") if isinstance(gw_raw, dict) else None
    if cfg.get("multiplex_profiles") is False or nested_raw is False:
        return []

    changed = []
    if "multiplex_profiles" not in cfg:
        cfg["multiplex_profiles"] = True
        changed.append("multiplex_profiles")

    if "gateway" not in cfg:
        cfg["gateway"] = {}
    gw = cfg["gateway"]
    if isinstance(gw, dict) and "multiplex_profiles" not in gw:
        gw["multiplex_profiles"] = True
        changed.append("gateway.multiplex_profiles")

    return changed


def _ensure_dashboard(cfg):
    """Ensure the dashboard block has a public_url for network access.

    Only fills ``dashboard.public_url`` when it is empty and DASHBOARD_PUBLIC_URL
    is configured. Does NOT touch basic_auth (operator manages credentials).
    Returns keys changed.
    """
    d = cfg.setdefault("dashboard", {})
    if not isinstance(d, dict):
        return []
    changed = []
    # public_url: only fill when empty — operator overrides are preserved.
    if DASHBOARD_PUBLIC_URL and not (d.get("public_url") or "").strip():
        d["public_url"] = DASHBOARD_PUBLIC_URL
        changed.append("public_url")
    return changed


def _probe_compaction_endpoint():
    """Sanity-probe the compaction endpoint at bootstrap.

    Hit <COMPACT_URL>/v1/models and assert that COMPACT_MODEL is present.
    Print a warning (non-fatal) if the model is missing so the user knows
    compression is silently dead (abort_on_summary_failure=False means workers
    limp on with degraded summaries).

    Returns True if the model is present in the endpoint's model list, False
    otherwise. Silently skips on connection errors (no live orch in test env).
    """
    import urllib.request
    import json
    import sys

    if not COMPACT_URL:
        return True  # no aux endpoint configured — nothing to probe

    models_url = COMPACT_URL.rstrip("/") + "/v1/models"
    try:
        req = urllib.request.Request(models_url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        listed = {m["id"] for m in data.get("data", [])}
        # vLLM sometimes returns fully-qualified IDs; check substring match too.
        present = COMPACT_MODEL in listed or any(
            COMPACT_MODEL.split("/")[-1] in mid for mid in listed
        )
        if not present:
            # Log a warning-like message so it surfaces in gateway logs.
            print(
                f"[WARN] HSCC compaction model mismatch: endpoint {models_url} "
                f"lists {len(listed)} model(s) — '{COMPACT_MODEL}' not found. "
                f"Compression will fail with 404. Fix: set HSCC_COMPACT_MODEL "
                f"or update the endpoint.",
                file=sys.stderr,
            )
        return present
    except Exception as exc:
        # Silent skip in tests (no live orch); only warn in real runs.
        if "pytest" not in sys.modules and "unittest" not in sys.modules:
            print(f"[WARN] HSCC compaction probe failed: {exc}", file=sys.stderr)
        return False


def _ensure_hooks_file(hooks_source):
    """Copy cluster-guard.py from the repo's hooks/ dir to ~/.hermes/hooks/.

    Args:
        hooks_source: path to hscc-bootstrap/hooks/ in the repo.

    Returns {"installed": True/False, "backed_up": str|None}.
    """
    import shutil
    from datetime import datetime
    from pathlib import Path

    src = Path(hooks_source) / "cluster-guard.py"
    if not src.is_file():
        return {"installed": False, "reason": "source cluster-guard.py not found"}

    dst = Path(CLUSTER_GUARD_DST)
    dst.parent.mkdir(parents=True, exist_ok=True)

    backed_up = None
    if dst.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = dst.parent / f"{dst.name}.bak-{stamp}"
        shutil.copy2(dst, bak)
        backed_up = str(bak)

    shutil.copy2(src, dst)
    os.chmod(dst, 0o755)
    return {"installed": True, "backed_up": backed_up}


def _ensure_hooks(cfg):
    """Wire cluster-guard shell hooks into config.yaml hooks section.

    Adds three hook entries (pre_tool_call, post_tool_call, on_session_start)
    only when a cluster-guard entry is not already present for that hook type.
    Preserves any operator-added hooks.  Returns keys changed.
    """
    hooks = cfg.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return []

    changed = []

    # Build the desired hook entries
    pre_hook = {
        "matcher": "hscc-cluster",
        "command": CLUSTER_GUARD_COMMAND,
        "timeout": 10,
    }
    post_hook = {
        "matcher": "hscc-cluster",
        "command": CLUSTER_GUARD_COMMAND,
        "timeout": 5,
    }
    session_hook = {
        "command": CLUSTER_GUARD_COMMAND,
        "timeout": 5,
    }

    for hook_type, hook_entry in [
        ("pre_tool_call", pre_hook),
        ("post_tool_call", post_hook),
        ("on_session_start", session_hook),
    ]:
        entries = hooks.get(hook_type)
        if entries is None:
            # Key absent — create a fresh list and append cluster-guard.
            entries = []
            hooks[hook_type] = entries
        elif not isinstance(entries, list):
            # Key present but wrong shape — preserve operator value and warn.
            print(f"[WARN] HSCC could not wire hooks.{hook_type}: expected list, got {type(entries).__name__}", file=sys.stderr)
            continue

        # Check if cluster-guard is already wired for this hook type
        has_guard = any(
            isinstance(e, dict) and e.get("command", "").endswith("cluster-guard.py")
            for e in entries
        )
        if not has_guard:
            entries.append(hook_entry)
            changed.append(hook_type)

    return changed


def enable(config_path, plugins=HSCC_PLUGINS, toolsets=HSCC_TOOLSETS,
           hooks_source=None):
    """Ensure HSCC plugins + toolsets + fleet routing + hooks are wired.

    Returns {"plugins": [...], "toolsets": [...], "kanban": [...], "delegation":
    [...], "compaction": [...], "fallback": [...], "bitwarden": [...],
    "prompt_caching": [...], "dashboard": [...], "multiplex": [...], "hooks": [...]} of what
    changed. Writes (with one backup) only if something changed. No-op + no
    backup if already wired or if the config is missing/malformed.

    Args:
        hooks_source: path to hscc-bootstrap/hooks/ in the repo.  Auto-detected
            from this module's location when None.
    """
    import yaml

    # Auto-detect hooks source: ../hscc-bootstrap/hooks/ relative to this file
    if hooks_source is None:
        hooks_source = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "hooks"
        )

    empty = {"plugins": [], "toolsets": [], "kanban": [], "delegation": [],
             "compaction": [], "text_aux": [], "fallback": [], "bitwarden": [],
             "prompt_caching": [], "dashboard": [], "multiplex": [], "hooks": []}
    if not os.path.exists(config_path):
        return empty
    with open(config_path) as fh:
        cfg = yaml.safe_load(fh) or {}
    if not isinstance(cfg, dict):
        return empty

    added_plugins = _ensure_plugins_enabled(cfg, plugins)
    added_toolsets = _ensure_toolsets(cfg, toolsets)
    changed_kanban = _ensure_kanban_routing(cfg)
    changed_delegation = _ensure_delegation(cfg)
    changed_compaction = _ensure_compaction(cfg)
    changed_text_aux = _ensure_text_auxiliaries(cfg)
    changed_fallback = _ensure_fallback(cfg)
    changed_bitwarden = _ensure_bitwarden(cfg)
    changed_prompt_caching = _ensure_prompt_caching(cfg)
    changed_dashboard = _ensure_dashboard(cfg)
    changed_multiplex = _ensure_multiplex(cfg)
    changed_hooks = _ensure_hooks(cfg)

    # Sanity-probe the compaction endpoint (prints a warning to gateway logs
    # if the model is missing — non-fatal but alerts the operator).
    _probe_compaction_endpoint()

    # Install the hook script to disk (idempotent, backup-then-overwrite)
    hooks_file_result = _ensure_hooks_file(hooks_source)

    if (added_plugins or added_toolsets or changed_kanban or changed_delegation
            or changed_compaction or changed_text_aux or changed_fallback
            or changed_bitwarden or changed_prompt_caching or changed_dashboard
            or changed_multiplex or changed_hooks):
        import shutil
        import time
        shutil.copy(config_path,
                    f"{config_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        with open(config_path, "w") as fh:
            yaml.safe_dump(cfg, fh, sort_keys=False, default_flow_style=False)

    return {"plugins": added_plugins, "toolsets": added_toolsets,
            "kanban": changed_kanban, "delegation": changed_delegation,
            "compaction": changed_compaction, "text_aux": changed_text_aux,
            "fallback": changed_fallback,
            "bitwarden": changed_bitwarden,
            "prompt_caching": changed_prompt_caching,
            "dashboard": changed_dashboard,
            "multiplex": changed_multiplex,
            "hooks": changed_hooks}


if __name__ == "__main__":
    path = os.path.expanduser("~/.hermes/config.yaml")
    res = enable(path)
    parts = [f"{k}: {', '.join(v)}" for k, v in res.items() if v]
    print(" | ".join(parts) if parts else "already wired (no changes)")
    sys.exit(0)