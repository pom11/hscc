"""Generate Hermes profiles from role specs: SOUL composition + materialization.

Uses the Hermes 0.17 native profile API where available:
- create_profile() to scaffold the profile directory
- write_profile_meta() for profile.yaml (routing_description → description)

Bundled-skill seeding is intentionally skipped (create_profile is called with
no_skills=True): the hand-written path never seeded skill files either, and the
config.yaml skills.preload list is what drives preloading. HSCC-specific
config.yaml (model block, compaction, toolsets) is still written manually
because the native API has no concept of cluster topology.
"""
import os
import yaml

# Allowed non-stdlib import: hermes_cli.profiles (Hermes 0.17 native API)
try:
    from hermes_cli.profiles import (
        create_profile,
        get_profile_dir,
        write_profile_meta,
    )
    USE_NATIVE_API = True
except ImportError:
    USE_NATIVE_API = False

import rolelib

# Worker role profiles must serve from the WORKER pool, not inherit the root
# config (which points at the orchestrator node). We point them at the sparkrun
# LiteLLM proxy, which load-balances every worker endpoint serving the worker
# model behind one OpenAI-compatible URL. Without this, every role task runs on
# the orchestrator GPU. Overridable via env so bootstrap can set the real
# proxy/host without editing code.
WORKER_PROXY_BASE_URL = os.environ.get(
    "HSCC_WORKER_PROXY_URL", "http://localhost:4000/v1")
WORKER_MODEL = os.environ.get("HSCC_WORKER_MODEL", "worker-model")
WORKER_PROXY_KEY = os.environ.get("HSCC_WORKER_PROXY_KEY", "sk-sparkrun")

# Strong-tier roles (model_tier: strong) route to the orchestrator GPU directly.
# Only architect + orchestrator use strong by default — .244 already runs
# orchestration + worker-compaction, so saturating it would hurt the whole fleet.
# Reviewers, coders, and QA stay on the fast worker proxy (:4000).
STRONG_URL = os.environ.get(
    "HSCC_STRONG_URL", "http://10.0.0.244:8000/v1")
# Default to the stable logical alias "orchestrator-model", resolved at the
# serving layer: the endpoint advertises that alias via --served-model-name, so
# the alias IS served as long as the serving layer's alias→id mapping is
# correct. Override per deployment with HSCC_STRONG_MODEL to force a concrete id.
STRONG_MODEL = os.environ.get(
    "HSCC_STRONG_MODEL", "orchestrator-model")
STRONG_KEY = os.environ.get("HSCC_STRONG_KEY", "sk-sparkrun")


def _worker_model_block():
    """The model block that points a worker role at the load-balanced proxy."""
    return {
        "default": WORKER_MODEL,
        "provider": "custom",
        "base_url": WORKER_PROXY_BASE_URL,
        "api_key": WORKER_PROXY_KEY,
    }


def _strong_model_block():
    """The model block that points a strong-tier role at the orchestrator GPU."""
    return {
        "default": STRONG_MODEL,
        "provider": "custom",
        "base_url": STRONG_URL,
        "api_key": STRONG_KEY,
    }


# Context-compaction (summarization) endpoint for worker roles. A long task
# self-compacts when its context fills; if that summarization runs on the same
# busy worker proxy doing the task, it competes for the saturated GPU and the
# worker WEDGES (the big summary prompt generates forever). Route compaction to
# the idle orchestrator (A3B, fast MoE) instead, with a hard timeout so a stuck
# summarize fails fast rather than hanging. Env-overridable.
COMPACT_BASE_URL = os.environ.get(
    "HSCC_COMPACT_URL", "http://10.0.0.244:8000/v1")
# The compaction model ID defaults to STRONG_MODEL, which is the stable logical
# alias "orchestrator-model", resolved at the serving layer — one orchestrator
# knob for both strong-tier and compaction. The endpoint advertises the alias
# via --served-model-name, so the alias IS served as long as the serving layer's
# alias→id mapping is correct. A placeholder id that the endpoint does not serve
# 404s the summary call, and the auxiliary-client fallback chain then reaches
# OpenRouter, whose credential read throws under the multiplexing secret-scope
# guard → "context length exceeded: max compression attempts reached" with the
# turn wedged. Override per deployment with HSCC_COMPACT_MODEL.
COMPACT_MODEL = os.environ.get("HSCC_COMPACT_MODEL", STRONG_MODEL)
COMPACT_KEY = os.environ.get("HSCC_COMPACT_KEY", "sk-sparkrun")
COMPACT_TIMEOUT = int(os.environ.get("HSCC_COMPACT_TIMEOUT", "90"))
# Compact less often: at this fraction of context (default 0.8 = ~210K of 262K),
# so summarization is rare instead of firing at 40%.
COMPACT_THRESHOLD = float(os.environ.get("HSCC_COMPACT_THRESHOLD", "0.8"))
# The compaction TOKEN CAP (v1.14.0, t_a8e9b7ff): native compaction fires EARLY
# at this many active tokens in the window — headroom for the compression call
# itself — instead of at the 196608 ratio floor where it wedges. This is the
# SINGLE source of truth, shared with the API-side ensure
# (hscc-api/routes_orchestrator.py imports it from here); do NOT duplicate the
# literal elsewhere. A lower operator value is always preserved (compaction
# can only fire earlier — see _compression_block).
SESSION_COMPACTION_THRESHOLD_TOKENS = 100000


def _compression_block(existing=None):
    """Build the ``compression`` block for a generated profile, MERGED over an
    existing block so operator values survive a regeneration.

    Mirrors the well-behaved intent of
    ``hscc-bootstrap/enable_plugins.py:_ensure_compaction`` (\"operator choices
    survive\"), applied to the generator's own compression block:

      * ``threshold`` (ratio) is only raised toward COMPACT_THRESHOLD, never
        lowered — an operator who set a smaller ratio keeps it.
      * ``threshold_tokens`` (token cap) is emitted at the shared
        SESSION_COMPACTION_THRESHOLD_TOKENS unless an existing value already
        ``<=`` that constant is present — a lower cap is deliberate and must
        never be raised (same rule as the API-side ``_ensure_compaction``).
      * every OTHER key in an existing compression block is preserved verbatim
        (the old generator replaced the whole block and dropped them).

    Returns a fresh dict; ``existing`` is never mutated.
    """
    comp = dict(existing) if isinstance(existing, dict) else {}
    cur_thr = comp.get("threshold")
    if not isinstance(cur_thr, (int, float)) or cur_thr < COMPACT_THRESHOLD:
        comp["threshold"] = COMPACT_THRESHOLD
    cur_tok = comp.get("threshold_tokens")
    if not (isinstance(cur_tok, (int, float))
            and not isinstance(cur_tok, bool)
            and cur_tok <= SESSION_COMPACTION_THRESHOLD_TOKENS):
        comp["threshold_tokens"] = SESSION_COMPACTION_THRESHOLD_TOKENS
    return comp


def _read_existing_config(pdir):
    """Read an existing on-disk profile config.yaml, if any.

    Returns ``(compression, auxiliary)`` blocks (or ``None`` for each when
    absent/unreadable) so the generator can MERGE into them instead of
    clobbering operator values. Never touches anything but the given profile
    dir; a missing or unparseable file simply yields ``None`` blocks.
    """
    path = os.path.join(pdir, "config.yaml")
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    comp = data.get("compression") if isinstance(data.get("compression"), dict) else None
    aux = data.get("auxiliary") if isinstance(data.get("auxiliary"), dict) else None
    return comp, aux


def _worker_compaction(existing_compression=None):
    """Route a worker role's context-compaction to the idle orchestrator.

    The compression block is merged over any existing block (see
    ``_compression_block``) so an operator's ``threshold_tokens`` survives a
    regeneration. The auxiliary block is fresh — a worker's summarization must
    run on the idle orchestrator, not the busy worker proxy.
    """
    out = {"compression": _compression_block(existing_compression)}
    out["auxiliary"] = {"compression": {
        "provider": "custom",
        "model": COMPACT_MODEL,
        "base_url": COMPACT_BASE_URL,
        "api_key": COMPACT_KEY,
        "timeout": COMPACT_TIMEOUT,
    }}
    return out

_WORKER_OPS = (
    "## Operational\n\n"
    "You run as the **{name}** role on a worker GPU node of the cluster, "
    "executing a single kanban task in your own git worktree. The task "
    "lifecycle (claim, heartbeat, complete or block-for-review) is provided to "
    "you at runtime — follow it exactly.\n"
)

_ORCH_OPS = (
    "## Operational\n\n"
    "You run as the **{name}** on the gateway node. You route work through the "
    "native kanban board and hold sole authority over the physical cluster.\n"
)

_PROJECT_ORCH_OPS = (
    "## Operational\n\n"
    "You run as the **{name}** on the gateway node. You route that project's "
    "work through its kanban board and hold sole authority over that board, "
    "but you do NOT hold authority over the physical cluster (only the "
    "cluster-wide `orchestrator` role does).\n"
)


def _is_orchestrator(name):
    """The orchestrator family: the cluster-wide `orchestrator` plus any
    per-project `<project>-orch` profile. They are NOT kanban workers."""
    return name == "orchestrator" or name.endswith("-orch")


def _is_project_orchestrator(name):
    """A PER-PROJECT orchestrator (`<project>-orch`), not the cluster-wide one.

    The cluster-wide `orchestrator` deliberately keeps the root config and is
    never repointed. Per-project orchestrators are separate profiles that must
    carry their own model block to be invocable directly.
    """
    return name != "orchestrator" and name.endswith("-orch")


def compose_soul(spec, base_identity):
    """Compose a profile SOUL from base + role disposition + thin operational.

    Orchestrators get an operational block that does NOT describe worktree
    execution (they are not workers); all other roles get the worker block.
    The cluster-wide `orchestrator` is the only one with authority over the
    physical cluster; per-project `<project>-orch` scopes that authority to its
    own board.
    """
    name = spec["name"]
    if name == "orchestrator":
        ops = _ORCH_OPS
    elif _is_orchestrator(name):
        ops = _PROJECT_ORCH_OPS
    else:
        ops = _WORKER_OPS
    return (
        f"{base_identity.rstrip()}\n\n"
        f"## Role: {name}\n\n"
        f"{spec['identity'].rstrip()}\n\n"
        f"{ops.format(name=name)}"
    )


def _short_desc(spec):
    """Routing description for the kanban decomposer roster.

    Uses the operator-written discriminative routing_description from the
    role spec.  The decomposer LLM matches tasks against these descriptions
    to assign them correctly.
    """
    return spec.get("routing_description", _short_desc_identity(spec))


def _short_desc_identity(spec):
    """Fallback: first sentence of the role identity (legacy)."""
    text = " ".join(spec["identity"].split())
    first = text.split(". ")[0].rstrip(".").strip()
    return (first[:200] + ".") if first else f"The {spec['name']} role."


def _write_if_changed(path, content):
    """Write content only if it differs from what's on disk. Returns changed?"""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                if f.read() == content:
                    return False
        except (UnicodeDecodeError, OSError):
            # Existing file is not valid UTF-8 or unreadable — treat as different.
            pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return True


def generate_profile(spec, base_identity):
    """Materialize a Hermes profile dir for a role spec. Idempotent.

    Uses the Hermes 0.17 native profile API where available:
    - create_profile() scaffolds the profile directory (idempotent —
      raises FileExistsError if it already exists, which we catch)
    - write_profile_meta() writes profile.yaml with routing_description
      (always safe — only overwrites the fields we pass)

    HSCC-specific config.yaml (model block, compaction, toolsets) is still
    written manually because the native API has no concept of cluster
    topology or the worker proxy.

    Returns True if any file was written/changed this call, else False.
    """
    name = spec["name"]

    # 1. Scaffold profile dir via native API (idempotent — creates if absent).
    # The native fns take a pathlib.Path, so keep the Path handle (pdir_path)
    # and derive the str form (pdir) only for os.path.join.
    pdir_path = None
    if USE_NATIVE_API:
        try:
            pdir_path = create_profile(name, no_alias=True, no_skills=True)
            changed = True  # newly created
        except FileExistsError:
            # Already exists — idempotent, resolve dir and continue
            pdir_path = get_profile_dir(name)
            changed = False
        pdir = str(pdir_path)
    else:
        # Fallback: manual path resolution
        pdir = os.path.join(rolelib.PROFILES_DIR, name)
        changed = False

    # 2. Compose and write SOUL.md (HSCC-specific composition — always manual)
    soul = compose_soul(spec, base_identity)
    changed |= _write_if_changed(os.path.join(pdir, "SOUL.md"), soul)

    # 3. Write config.yaml (HSCC-specific: model block + compaction + toolsets)
    config = {
        "toolsets": rolelib.role_toolsets(),
        "skills": {"preload": spec["preload_skills"]},
    }
    # Read the profile's EXISTING on-disk compaction blocks (if any) so a
    # regeneration MERGES into them instead of clobbering operator values —
    # in particular, an operator-set ``compression.threshold_tokens`` must
    # survive (see _compression_block). Fresh profiles yield None and start
    # from the generated defaults.
    existing_compression, _existing_aux = _read_existing_config(pdir)
    # Worker roles serve from the load-balanced worker proxy so their work runs
    # on worker GPUs, not the orchestrator. The orchestrator role keeps the root
    # config (its own gateway-node model) and is never repointed; per-project
    # orchestrators (<project>-orch) likewise keep the gateway-node model.
    if _is_project_orchestrator(name):
        # Per-project orchestrators (<project>-orch) need an EXPLICIT model
        # block. They were previously lumped in with the cluster-wide
        # `orchestrator` and left to "inherit the root config" — but the root
        # config's model block carries no api_key, so `hermes -p <p>-orch chat`
        # dies with "No inference provider configured" and the profile cannot
        # run at all outside the gateway. This block is the SAME model/endpoint
        # the root config points at (orchestrator-model on the orchestrator
        # GPU), just with the api_key that a direct invocation requires.
        #
        # Deliberately NOT given worker-proxy compaction routing: an
        # orchestrator is not a kanban worker and must not be repointed at the
        # worker proxy. It DOES, however, get the compaction compression block
        # (threshold + threshold_tokens) so bootstrap no longer nulls the
        # token cap the API-side ensure set (the 2026-08-27 incident: 11 idle
        # project orche went null for ~84 min and wedged at the ratio floor).
        config["model"] = _strong_model_block()
        config["compression"] = _compression_block(existing_compression)
    elif not _is_orchestrator(name):
        model_tier = spec.get("model_tier", "fast")
        model_endpoint = spec.get("model_endpoint")
        model_name = spec.get("model_name")

        if model_endpoint:
            # Per-role override: use the specified endpoint.
            override_block = {
                "default": model_name if model_name else (
                    STRONG_MODEL if model_tier == "strong" else WORKER_MODEL
                ),
                "provider": "custom",
                "base_url": model_endpoint,
                "api_key": WORKER_PROXY_KEY,
            }
            config["model"] = override_block
        elif model_tier == "strong":
            config["model"] = _strong_model_block()
        else:
            config["model"] = _worker_model_block()
        # Route context-compaction OFF the busy worker proxy to the idle
        # orchestrator, so a long task's self-summarization doesn't wedge the
        # worker. This applies regardless of model_tier — even strong-tier roles
        # should not compete with their own model for compaction.
        for k, v in _worker_compaction(existing_compression).items():
            config[k] = v
    changed |= _write_if_changed(
        os.path.join(pdir, "config.yaml"),
        yaml.safe_dump(config, default_flow_style=False, sort_keys=False),
    )

    # 4. Write profile.yaml via native API (routing_description → description)
    routing_desc = _short_desc(spec)  # routing_description from spec (WS2)
    profile_yaml = os.path.join(pdir, "profile.yaml")
    if USE_NATIVE_API:
        # Write via the native API (takes a Path). Track a real change by
        # comparing profile.yaml before/after so a no-op re-run reports
        # changed=False (idempotent return value).
        before = ""
        if os.path.isfile(profile_yaml):
            with open(profile_yaml) as f:
                before = f.read()
        write_profile_meta(pdir_path, description=routing_desc,
                           description_auto=False)
        with open(profile_yaml) as f:
            after = f.read()
        changed |= (before != after)
    else:
        # Fallback: write manually (legacy path)
        profile = {"description": routing_desc, "description_auto": False}
        changed |= _write_if_changed(
            profile_yaml,
            yaml.safe_dump(profile, default_flow_style=False, sort_keys=False),
        )

    return changed