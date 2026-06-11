"""Generate Hermes profiles from role specs: SOUL composition + materialization."""
import os
import yaml
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
    """First sentence of the role identity, for the kanban decomposer roster."""
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

    Writes SOUL.md (composed), config.yaml (toolsets = full minus cluster,
    preload skills), and profile.yaml (decomposer-facing description). Returns
    True if any file was written/changed this call, else False.
    """
    pdir = os.path.join(rolelib.PROFILES_DIR, spec["name"])
    soul = compose_soul(spec, base_identity)
    config = {
        "toolsets": rolelib.role_toolsets(),
        "skills": {"preload": spec["preload_skills"]},
    }
    # Worker roles serve from the load-balanced worker proxy so their work runs
    # on worker GPUs, not the orchestrator. The orchestrator role keeps the root
    # config (its own gateway-node model) and is never repointed.
    if spec["name"] != "orchestrator":
        config["model"] = _worker_model_block()
    profile = {
        "description": _short_desc(spec),
        "description_auto": False,
    }
    changed = False
    changed |= _write_if_changed(os.path.join(pdir, "SOUL.md"), soul)
    changed |= _write_if_changed(
        os.path.join(pdir, "config.yaml"),
        yaml.safe_dump(config, default_flow_style=False, sort_keys=False),
    )
    changed |= _write_if_changed(
        os.path.join(pdir, "profile.yaml"),
        yaml.safe_dump(profile, default_flow_style=False, sort_keys=False),
    )
    return changed
