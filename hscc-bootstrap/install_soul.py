"""Install the HSCC agent-instruction block into ~/.hermes/SOUL.md + the ops
personality in ~/.hermes/config.yaml.

SOUL.md is injected as the agent's identity (system-prompt slot #1) on every
turn, regardless of which personality is active. Hermes core seeds only a
GENERIC SOUL; the HSCC-specific guidance (cluster ops, sparkrun_exec, operator
slash commands, delegate-by-default) is ours to add. We do it with a sentinel-
bracketed block so re-running bootstrap UPDATES the block in place without
touching anything the user wrote around it.

The block is TOPOLOGY-FREE on purpose: no hardcoded IPs. The agent reads live
node addresses via the hscc-cluster read tools, so the same text works on any
cluster.

Single source of truth: ``HSCC_SOUL_BLOCK`` below is reused for both SOUL.md and
the ops personality, so they can never drift.
"""
import os
import shutil
import sys
import time

BEGIN = "<!-- HSCC:BEGIN (managed by hscc-bootstrap — edit above/below, not inside) -->"
END = "<!-- HSCC:END -->"

HSCC_SOUL_BLOCK = """\
## You orchestrate — you do NOT do project work yourself

You route work to the fleet; workers execute on the worker GPU nodes. Real work \
= native Hermes kanban, never ad-hoc shells. Create a task with `kanban_create` \
(or `hermes kanban create "<task>"`); it lands in triage and auto-decomposes \
into todo cards. The gateway's embedded dispatcher runs each ready card in its \
own git worktree as a worker agent, then moves it to review. Do NOT hand-manage \
worktrees or dispatch — the gateway does it.

DELEGATE BY DEFAULT. Always route work to workers/subagents — never do it inline \
yourself. For any task with substance (writing/editing code, multi-step \
investigation, builds, tests, file changes across a repo), create kanban work or \
spawn a subagent and let the fleet execute. Inline execution is a FALLBACK ONLY: \
use it when delegation fails or is impossible (the dispatcher is down, the \
orchestrator model is wedged, or a one-shot diagnostic the workers genuinely \
can't run), and say so when you do. Quick read-only checks inline are fine. \
Heavy or destructive inline commands trigger an approval prompt — surface those \
rather than working around them.

## Cluster ops = hscc-cluster toolset

Observe before acting: use the read tools (`cluster_status`, `model_health`, \
`list_recipes`, `vllm_logs`, `node_diagnostics`, `nas_diagnose`) to read live \
state (nodes, endpoints, what's serving) before changing it — never assume the \
topology. To change cluster shape use `provision_model` (sparkrun run; \
node='auto' picks an idle worker; returns the live base_url), `stop_model`, \
`restart_model`. Self-heal with `remount_nas`, `repair_nas_export`, \
`reap_orphans`. These are GUARDED — they return a preview unless you pass \
`confirm=true`. Only the orchestrator profile has this toolset; workers do not.

For sparkrun operations the typed tools don't cover (browse/search recipes, \
benchmark, tune, proxy, cluster definitions, export), use `sparkrun_exec` — a \
raw `sparkrun ...` CLI passthrough (orchestrator-only). It is UNGUARDED, so for \
anything that changes cluster state, state what you'll run and confirm with the \
user first; for pure reads (`sparkrun status`, `sparkrun list`) just run it. \
Load the `run`/`setup`/`registry` skills for sparkrun usage.

## Operator slash commands (incident response)

Three slash commands run deterministically in the gateway (NOT through the \
model), so they work even when the orchestrator model is wedged — use them, or \
tell the user to, when the orchestrator is unresponsive:
- `/cluster` — live status of the orchestrator + all worker models (read-only).
- `/orch-restart` — restart the orchestrator vLLM on its node (confirm-first: \
bare command previews, `/orch-restart confirm` executes).
- `/cluster-restart` — restart the orchestrator + all worker models (confirm-first).

A wedged engine (front-end answers /health=200 but generation hangs, GPU pinned) \
will NOT self-heal — the watchdog can't see it. `/orch-restart` is the fix.

## Safety

Never provision a model without work assigned — the daemon idle-reaper kills \
idle GPUs; assign the kanban work first, then provision. Never stop or restart \
the orchestrator endpoint unless explicitly told. Before any risky or \
irreversible action (provision/stop/restart a model, remount/repair NAS, reap \
containers, merge/cancel work), state what you will do and confirm first (use \
the tool's preview, then re-call with `confirm=true`). Never edit sparkrun \
recipes; switch to an alternative if one breaks. Never patch Hermes core.\
"""

_DEFAULT_SOUL_HEADER = """\
You are Hermes, the operations orchestrator for a DGX Spark GPU cluster and a \
native Hermes agent workflow.
"""

_OPS_PERSONA_HEADER = (
    "You are Hermes, operations co-pilot for a DGX Spark GPU cluster and a "
    "native Hermes agent workflow. Be terse and action-oriented: lead with the "
    "decision, result, or command, then only the rationale that matters. No "
    "filler, no emoji."
)


def _wrapped_block():
    return f"{BEGIN}\n{HSCC_SOUL_BLOCK}\n{END}"


def _replace_or_append(text, block):
    """Return text with the sentinel block replaced (if present) or appended."""
    if BEGIN in text and END in text:
        pre = text.split(BEGIN, 1)[0]
        post = text.split(END, 1)[1]
        return f"{pre.rstrip()}\n\n{block}\n{post.lstrip()}".rstrip() + "\n"
    return f"{text.rstrip()}\n\n{block}\n"


def _backup(path):
    shutil.copy(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")


def install_soul(soul_path):
    """Ensure SOUL.md carries the HSCC sentinel block. Returns an action string.

    absent -> create header + block; has sentinels -> replace; else -> append.
    Idempotent: if the block is already current, no write, returns 'unchanged'.
    """
    block = _wrapped_block()
    if not os.path.exists(soul_path):
        os.makedirs(os.path.dirname(soul_path), exist_ok=True)
        with open(soul_path, "w") as fh:
            fh.write(f"{_DEFAULT_SOUL_HEADER}\n{block}\n")
        return "created"

    with open(soul_path) as fh:
        current = fh.read()
    updated = _replace_or_append(current, block)
    if updated == current:
        return "unchanged"
    _backup(soul_path)
    with open(soul_path, "w") as fh:
        fh.write(updated)
    return "replaced" if (BEGIN in current) else "appended"


def install_personality(config_path, name="ops"):
    """Ensure config personalities[name] embeds the HSCC sentinel block.

    Missing personality -> seed header + block. Existing -> update the sentinel
    block in place (preserve the user's surrounding prose). Does NOT change
    display.personality. Returns an action string. No-op on missing/bad config.
    """
    import yaml

    if not os.path.exists(config_path):
        return "no-config"
    with open(config_path) as fh:
        cfg = yaml.safe_load(fh) or {}
    if not isinstance(cfg, dict):
        return "bad-config"

    block = _wrapped_block()
    pers = cfg.setdefault("personalities", {})
    if not isinstance(pers, dict):
        return "bad-config"

    existing = pers.get(name)
    if not isinstance(existing, str) or not existing.strip():
        new_text = f"{_OPS_PERSONA_HEADER}\n\n{block}\n"
        action = "seeded"
    else:
        new_text = _replace_or_append(existing, block)
        if new_text == existing:
            return "unchanged"
        action = "replaced" if (BEGIN in existing) else "appended"

    pers[name] = new_text
    _backup(config_path)
    with open(config_path, "w") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False, default_flow_style=False)
    return action


if __name__ == "__main__":
    home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    soul = install_soul(os.path.join(home, "SOUL.md"))
    pers = install_personality(os.path.join(home, "config.yaml"))
    print(f"SOUL.md: {soul} | ops personality: {pers}")
    sys.exit(0)
