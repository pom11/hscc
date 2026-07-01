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
WORKER_MODEL = os.environ.get("HSCC_WORKER_MODEL", "Qwen/Qwen3.6-27B-FP8")
WORKER_PROXY_KEY = os.environ.get("HSCC_WORKER_PROXY_KEY", "sk-sparkrun")


def _worker_model_block():
    """The model block that points a worker role at the load-balanced proxy."""
    return {
        "default": WORKER_MODEL,
        "provider": "custom",
        "base_url": WORKER_PROXY_BASE_URL,
        "api_key": WORKER_PROXY_KEY,
    }


# Context-compaction (summarization) endpoint for worker roles. A long task
# self-compacts when its context fills; if that summarization runs on the same
# busy worker proxy doing the task, it competes for the saturated GPU and the
# worker WEDGES (the big summary prompt generates forever). Route compaction to
# the idle orchestrator (A3B, fast MoE) instead, with a hard timeout so a stuck
# summarize fails fast rather than hanging. Env-overridable.
COMPACT_BASE_URL = os.environ.get(
    "HSCC_COMPACT_URL", "http://10.0.0.244:8000/v1")
COMPACT_MODEL = os.environ.get("HSCC_COMPACT_MODEL", "orchestrator-model")
COMPACT_KEY = os.environ.get("HSCC_COMPACT_KEY", "sk-sparkrun")
COMPACT_TIMEOUT = int(os.environ.get("HSCC_COMPACT_TIMEOUT", "90"))
# Compact less often: at this fraction of context (default 0.8 = ~210K of 262K),
# so summarization is rare instead of firing at 40%.
COMPACT_THRESHOLD = float(os.environ.get("HSCC_COMPACT_THRESHOLD", "0.8"))


def _worker_compaction():
    """Route a worker role's context-compaction to the idle orchestrator."""
    return {
        "compression": {"threshold": COMPACT_THRESHOLD},
        "auxiliary": {"compression": {
            "provider": "custom",
            "model": COMPACT_MODEL,
            "base_url": COMPACT_BASE_URL,
            "api_key": COMPACT_KEY,
            "timeout": COMPACT_TIMEOUT,
        }},
    }

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


def compose_soul(spec, base_identity):
    """Compose a profile SOUL from base + role disposition + thin operational.

    Orchestrator gets an operational block that does NOT describe worktree
    execution (it is not a worker); all other roles get the worker block.
    """
    name = spec["name"]
    ops = _ORCH_OPS if name == "orchestrator" else _WORKER_OPS
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
    first = text.split(". ")[0].strip()
    return (first[:200] + ".") if first else f"The {spec['name']} role."


def _write_if_changed(path, content):
    """Write content only if it differs from what's on disk. Returns changed?"""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            if f.read() == content:
                return False
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
    # Worker roles serve from the load-balanced worker proxy so their work runs
    # on worker GPUs, not the orchestrator. The orchestrator role keeps the root
    # config (its own gateway-node model) and is never repointed.
    if name != "orchestrator":
        config["model"] = _worker_model_block()
        # Route context-compaction OFF the busy worker proxy to the idle
        # orchestrator, so a long task's self-summarization doesn't wedge the
        # worker. Merge the two keys into config.
        for k, v in _worker_compaction().items():
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