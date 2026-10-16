"""Fork-branch invariant for the runtime dep-bump flow.

A runtime dependency bump (the ``check-runtime-deps`` workflow plus
``scripts/dep_pr_watcher.py``) rewrites ``runtime-versions.json`` and asks a
worker to verify the bump live. For **sparkrun** that is not a plain version
swap: the cluster does not run upstream ``main``. It runs OUR FORK BRANCH,
installed editable at ``~/sparkrun`` (``~/.local/bin/sparkrun`` ->
``~/sparkrun/.venv-sparkrun-uv312/bin/sparkrun``).

A bump must therefore REBASE the fork branch onto the new upstream tag and
keep it checked out. ``git checkout main`` in ``~/sparkrun`` — or reinstalling
from the upstream repo — silently drops the fork's proxy work on the next
proxy restart: the LiteLLM ``router_settings`` passthrough and the
least-in-flight balancer. Nothing errors; routing just quietly reverts. That
is the failure these strings exist to prevent, so they live in code (imported
by both the CI generator and the cluster poller, and pinned by tests) instead
of being prose duplicated in two places.

The branch name is verified against the live checkout at implementation time
(2026-10-16) — ``feat/proxy-least-in-flight``, fork remote ``pom11/sparkrun``.
It supersedes the earlier ``feat/proxy-router-settings`` name.
"""

FORK_REMOTE = "pom11/sparkrun"
FORK_BRANCH = "feat/proxy-least-in-flight"
SPARKRUN_CHECKOUT = "~/sparkrun"
SPARKRUN_REMOTE_NAME = "fork"

#: Lines appended to a generated sparkrun bump PR body / verification card.
#: Kept a tuple (not one blob) so tests can pin each obligation separately.
FORK_RULE_LINES = (
    f"**sparkrun is deployed from our fork branch `{FORK_BRANCH}`** "
    f"(`{SPARKRUN_REMOTE_NAME}` = {FORK_REMOTE}), installed editable at "
    f"`{SPARKRUN_CHECKOUT}` — **not** from upstream `main`.",
    f"A sparkrun bump must `git -C {SPARKRUN_CHECKOUT} fetch origin <new-tag>` "
    f"and **rebase `{FORK_BRANCH}` onto that tag**, and must leave "
    f"`{FORK_BRANCH}` checked out afterwards.",
    f"**Never `git checkout main` in `{SPARKRUN_CHECKOUT}`, never reinstall "
    "from upstream.** That silently drops the proxy `router_settings` "
    "passthrough and the least-in-flight balancer on the next proxy restart "
    "— no error, routing just reverts to `simple-shuffle`.",
    f"Verify after the bump: "
    f"`git -C {SPARKRUN_CHECKOUT} rev-parse --abbrev-ref HEAD` == "
    f"`{FORK_BRANCH}`, and `router_settings` is still read in "
    "`src/sparkrun/proxy/config.py` plus emitted in "
    "`src/sparkrun/proxy/engine.py`.",
    "Then restart the proxy and confirm `router_settings` appears in "
    "`~/.cache/sparkrun/proxy/litellm_config.yaml` "
    "(`hscc verify` check `proxy_router_settings` must go quiet).",
)


def fork_rule_block(dep_names) -> list:
    """Return the fork rule lines, or [] when no sparkrun dep is in the bump.

    ``dep_names`` is the iterable of dependency names a bump touches
    (``runtime-versions.json`` keys). hermes-agent bumps are unaffected — the
    rule is sparkrun-only, and appending it unconditionally would train
    readers to skim it.
    """
    if not any(str(name) == "sparkrun" for name in (dep_names or ())):
        return []
    return list(FORK_RULE_LINES)
