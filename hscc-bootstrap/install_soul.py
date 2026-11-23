"""Install the HSCC agent-instruction block into ~/.hermes/SOUL.md + the ops
personality in ~/.hermes/config.yaml.

SOUL.md is injected as the agent's identity (system-prompt slot #1) on every
turn, regardless of which personality is active. Hermes core seeds only a
GENERIC SOUL; the HSCC-specific guidance (work routing, the review gate,
operator slash commands, delegate-by-default) is ours to add. We do it with a
sentinel-bracketed block so re-running bootstrap UPDATES the block in place
without touching anything the user wrote around it.

This installer seeds the Hermes ROOT home, which is the operator's interactive
CLI, the gateway and the kanban dispatcher — NOT the main orchestrator. Cluster
control belongs to the main orchestrator profile alone (its own SOUL section is
``hscc-roles/orchestrators._MAIN_CLUSTER_CONTROL``); the root block therefore
states the main-only rule and carries no cluster tool catalog. The operator
slash commands stay: they run deterministically in the gateway, not through this
agent.

The block is TOPOLOGY-FREE on purpose: no hardcoded IPs. The agent reads live
topology through the hscc read surface, so the same text works on any cluster.

Single source of truth: ``HSCC_SOUL_BLOCK`` below is reused for both SOUL.md and
the ops personality, so they can never drift.
"""
import argparse
import os
import sys

import _theme

try:
    import backup_util
except ImportError:  # pragma: no cover - import-path robustness only
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import backup_util

BEGIN = "<!-- HSCC:BEGIN (managed by hscc-bootstrap — edit above/below, not inside) -->"
END = "<!-- HSCC:END -->"

# A second managed region for the identity preamble (line 1 of SOUL). Kept
# separate so it can carry the HSCC name + character while staying topology-free,
# and so a hand-edited hardcoded-IP line can't drift back in (M4).
HEAD_BEGIN = "<!-- HSCC:HEADER:BEGIN (managed — topology-free identity) -->"
HEAD_END = "<!-- HSCC:HEADER:END -->"

#: The one-line cluster rule every non-main SOUL carries (operator ruling R4,
#: 2026-11-23). Says who may act, never what a config contains.
CLUSTER_ACTIONS_RULE = (
    "cluster actions go through the main orchestrator (board card or orch message)")

HSCC_SOUL_BLOCK = """\
## You are the ROOT operator agent — not the main orchestrator

This is the Hermes ROOT home: the operator's interactive CLI, the gateway and \
the kanban dispatcher. The main orchestrator is the separate profile that \
bootstrap named, whatever it was called, and it is the ONLY profile on this \
fleet with cluster control.

Fleet rule: """ + CLUSTER_ACTIONS_RULE + """. Do not claim cluster authority \
from this home and do not run cluster control here; when a task needs the \
cluster changed, route it to the main orchestrator as a board card or an orch \
message.

## You route work — you do NOT do project work yourself

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

## Doc-driven work + review gate

For non-trivial work, write a short spec + implementation plan into \
`docs/superpowers/` FIRST (specs/ then plans/), then decompose it into kanban \
tasks. The plan's checklist is the acceptance contract — the reviewer checks the \
work against it. Each coder/role task is auto-paired with a `reviewer` task \
(kanban `auto_review`): the reviewer approves only if the diff is correct, the \
task's tests run green, AND the work matches the plan; otherwise it rejects with \
precise change requests. After 3 consecutive rejects the task escalates to the \
human with the full review history (the circuit breaker's `failure_limit`). \
Reviewer-approved work lands on the `integration` branch; promoting \
`integration` → `main` is ALWAYS the human's call — never auto-merge to main. \
A re-dispatched worker resumes idempotently: it reads its prior attempts + what \
already landed on its task branch and continues from the first unfinished \
checklist item rather than redoing completed work.

## Operator slash commands (incident response)

Three slash commands run deterministically in the gateway (NOT through the \
model), so they work even when the orchestrator model is wedged — tell the \
operator to run one when the orchestrator is unresponsive:
- `/cluster` — live status of the orchestrator + all worker models (read-only).
- `/orch-restart` — restart the orchestrator vLLM on its node (confirm-first: \
bare command previews, `/orch-restart confirm` executes).
- `/cluster-restart` — restart the orchestrator + all worker models \
(confirm-first).

A wedged engine (front-end answers /health=200 but generation hangs, GPU pinned) \
will NOT self-heal — the watchdog can't see it. `/orch-restart` is the fix.

## Working-dir discipline

All development happens under `~/dev/<repo>` — one checkout per project. NEVER \
create duplicate clones of a repo; if a repo already exists under `~/dev`, work \
in it. The HSCC plugin repo lives at `~/dev/hscc` and is installed into \
`~/.hermes/plugins` by `hscc-bootstrap` (edit in the repo, run bootstrap, the \
runtime updates) — never edit `~/.hermes/plugins` directly as if it were the \
source. Keep work on a feature branch; reviewed work lands on the integration \
branch; main is the human's call.

## Safety

Cluster changes are the main orchestrator's to run — provision/stop/restart a \
model, remount/repair NAS, reap containers. Never provision a model without \
work assigned: the daemon idle-reaper kills idle GPUs, so assign the kanban work \
first and route the provision request to the main orchestrator. Never stop or \
restart the orchestrator endpoint unless explicitly told. Before any risky or \
irreversible action, state what you will do and confirm first; never shell \
around a guard. Never edit sparkrun recipes; switch to an alternative if one \
breaks. Never patch Hermes core.\
"""

# Topology-free identity preamble (the HSCC name + character). No IPs — live
# topology is read from the hscc read surface. Managed via HEAD sentinels.
_SOUL_IDENTITY = """\
You are **HSCC** — the operator's agent in the Hermes ROOT home of a DGX Spark \
GPU cluster and a native Hermes agent fleet. You route work to specialized \
workers; the main orchestrator profile is the only one that controls the \
cluster. You are calm, precise, and decisive — an operator who reads live state \
before acting and says exactly what changed. You never hardcode the cluster's \
shape: it is read from live state.
"""

_OPS_PERSONA_HEADER = (
    "You are HSCC, the operator's agent in the Hermes ROOT home of a DGX Spark "
    "GPU cluster and a native Hermes agent fleet. The main orchestrator profile "
    "is the only one that controls the cluster; route cluster actions there. Be "
    "terse and action-oriented: lead with the decision, result, or command, then "
    "only the rationale that matters. No filler, no emoji. Read live cluster "
    "state before acting; never assume topology."
)


def _wrapped_header():
    return f"{HEAD_BEGIN}\n{_SOUL_IDENTITY.rstrip()}\n{HEAD_END}"


def _wrapped_block():
    return f"{BEGIN}\n{HSCC_SOUL_BLOCK}\n{END}"


def _replace_or_append(text, block):
    """Return text with the sentinel block replaced (if present) or appended."""
    if BEGIN in text and END in text:
        pre = text.split(BEGIN, 1)[0]
        post = text.split(END, 1)[1]
        return f"{pre.rstrip()}\n\n{block}\n{post.lstrip()}".rstrip() + "\n"
    return f"{text.rstrip()}\n\n{block}\n"


def _replace_header(text, header):
    """Replace the managed identity header at the top of SOUL.

    If HEAD sentinels exist, replace between them. Otherwise treat the first
    paragraph (up to the first blank line) as the legacy hardcoded preamble and
    replace it — this strips the old `...192.0.2.244...` line (M4). User
    content below the first blank line is preserved."""
    if HEAD_BEGIN in text and HEAD_END in text:
        post = text.split(HEAD_END, 1)[1]
        return f"{header}\n{post.lstrip()}".rstrip() + "\n"
    # No header sentinels yet: replace the leading paragraph (legacy preamble).
    parts = text.split("\n\n", 1)
    body = parts[1] if len(parts) == 2 else ""
    return f"{header}\n\n{body.lstrip()}".rstrip() + "\n"


def _backup(path):
    """Atomic snapshot of ``path`` as ``<name>.bak-<stamp>`` + retain limit.

    t_9462260b: the old ``shutil.copy(path, f"{path}.bak-{ts}")`` truncated the
    backup name before streaming it (an interrupted run zeroed the rollback
    point) and never pruned — the root home kept 5 SOUL.md.bak-* plus 26
    config.yaml.bak-*.
    """
    return backup_util.backup_file(path)


def install_soul(soul_path):
    """Ensure SOUL.md carries the HSCC sentinel block. Returns an action string.

    absent -> create header + block; has sentinels -> replace; else -> append.
    Idempotent: if the block is already current, no write, returns 'unchanged'.
    """
    block = _wrapped_block()
    header = _wrapped_header()
    if not os.path.exists(soul_path):
        os.makedirs(os.path.dirname(soul_path), exist_ok=True)
        with open(soul_path, "w") as fh:
            fh.write(f"{header}\n\n{block}\n")
        return "created"

    with open(soul_path) as fh:
        current = fh.read()
    updated = _replace_header(current, header)   # strip/refresh identity (M4)
    updated = _replace_or_append(updated, block)  # refresh guidance block
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
    try:
        with open(config_path) as fh:
            cfg = yaml.safe_load(fh) or {}
    except yaml.YAMLError:
        # Malformed YAML is a "bad config" — the documented contract (below) is
        # a no-op, so refuse to touch it further and DON'T crash the enclosing
        # bootstrap step. install_soul.py __main__ writes SOUL.md before calling
        # us; a crash here would leave SOUL updated but personality not, surfaced
        # only as a generic warn in bootstrap.sh:253.
        return "bad-config"
    if not isinstance(cfg, dict):
        return "bad-config"

    block = _wrapped_block()
    pers = cfg.setdefault("personalities", {})
    if not isinstance(pers, dict):
        return "bad-config"

    header = f"{HEAD_BEGIN}\n{_OPS_PERSONA_HEADER}\n{HEAD_END}"
    existing = pers.get(name)
    if not isinstance(existing, str) or not existing.strip():
        new_text = f"{header}\n\n{block}\n"
        action = "seeded"
    else:
        # Strip/refresh the identity preamble (M4: ops persona line 1 also
        # hardcoded IPs) then refresh the guidance block.
        new_text = _replace_header(existing, header)
        new_text = _replace_or_append(new_text, block)
        if new_text == existing:
            return "unchanged"
        action = "replaced" if (BEGIN in existing) else "appended"

    pers[name] = new_text
    _backup(config_path)
    with open(config_path, "w") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False, default_flow_style=False)
    return action


def _root_home(env=None, *, home=None):
    """The Hermes ROOT home this installer seeds.

    ``__main__`` is the INSTALLER entry (bootstrap.sh's only caller) and seeds
    the install's ``SOUL.md`` + ops personality. Hermes exports
    ``HERMES_HOME=<root>/profiles/<name>`` under a profile — its normal run
    mode — so a raw env join writes the caller's profile and leaves the ROOT
    home unseeded. Resolve through the profiles-grandparent rule, the same
    rung of the shared homes ladder as
    ``hscc_daemon.hermes_python.hermes_homes`` (and the rule F2 landed in
    ``install_review_skill.default_home`` via ``backup_util._hermes_root``).

    An explicit ``--home`` never goes through here: it is used exactly as
    given, so an operator can deliberately wire one profile.
    """
    env = os.environ if env is None else env
    raw = (env.get("HERMES_HOME") or "").strip()
    if not raw:
        raw = os.path.join(os.path.expanduser(str(home or "~")), ".hermes")
    return backup_util._hermes_root(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Install the HSCC agent-instruction block into a Hermes "
                    "home (the ROOT install by default; --home to target one "
                    "profile deliberately).")
    parser.add_argument("--home", default=None,
                        help="explicit Hermes home to seed, used as given "
                             "(default: $HERMES_HOME resolved to the Hermes "
                             "ROOT, else ~/.hermes)")
    args = parser.parse_args(argv)
    home = args.home if args.home is not None else _root_home()
    home = os.path.expanduser(home)
    soul = install_soul(os.path.join(home, "SOUL.md"))
    pers = install_personality(os.path.join(home, "config.yaml"))
    body = f"SOUL.md: {soul} | ops personality: {pers}"
    # bootstrap.sh captures this stdout into its own status line; a Rich panel
    # there became box-drawing noise pasted mid-sentence. TTY gets the panel.
    if sys.stdout.isatty():
        _theme.make_console().print(_theme.panel("bootstrap", _theme.escape(body)))
    else:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
