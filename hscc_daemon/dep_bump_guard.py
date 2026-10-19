"""Fork-branch invariant for the runtime dep-bump flow.

A runtime dependency bump (the ``check-runtime-deps`` workflow plus
``scripts/dep_pr_watcher.py``) rewrites ``runtime-versions.json`` and asks a
worker to verify the bump live. For **sparkrun** that is not a plain version
swap: the cluster does not run upstream ``main``. It runs OUR FORK BRANCH,
installed editable at ``~/dev/sparkrun-live`` (``~/.local/bin/sparkrun`` ->
``~/sparkrun/.venv-sparkrun-040/bin/sparkrun``).

A bump must therefore REBASE the fork branch onto the new upstream tag and
keep it checked out. ``git checkout main`` in the live checkout — or
reinstalling from the upstream repo — silently drops whatever proxy work only
the fork branch carries on the next proxy restart. Nothing errors; routing
just quietly reverts. That is the failure these strings exist to prevent, so
they live in code (imported by both the CI generator and the cluster poller,
and pinned by tests) instead of being prose duplicated in two places.

Verified at implementation time (2026-10-19): the deployed branch is
``feat/proxy-least-in-flight-040`` (fork remote ``pom11/sparkrun``), cut off
upstream tag ``v0.4.0`` — upstream v0.4.0 absorbed the ``router_settings``
gateway handling and the least-in-flight balancer, so the fork diff at this
tag is minimal, but the rebase rule is tag-independent and stays mandatory.
The ``-040`` branch supersedes ``feat/proxy-least-in-flight`` (the v0.3.10
deployment, kept as the rollback line) and the earlier
``feat/proxy-router-settings`` name.
"""

FORK_REMOTE = "pom11/sparkrun"
FORK_BRANCH = "feat/proxy-least-in-flight-040"
SPARKRUN_CHECKOUT = "~/dev/sparkrun-live"
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
    "from upstream.** That silently drops whatever proxy work only the fork "
    "branch carries (at v0.4.0 that is at minimum the fork's test/runbook "
    "lineage; upstream may also move or rename the balancer again) on the "
    "next proxy restart — no error.",
    f"Verify after the bump: "
    f"`git -C {SPARKRUN_CHECKOUT} rev-parse --abbrev-ref HEAD` == "
    f"`{FORK_BRANCH}`, and `router_settings` is still read in "
    "`src/sparkrun/proxy/config.py` plus emitted in "
    "`src/sparkrun/plugins/litellm/engine.py`.",
    "Then restart the proxy and confirm the balancer is live in "
    "`~/.cache/sparkrun/proxy/litellm_config.yaml`: "
    "`litellm_settings.callbacks` contains `sparkrun_balancer.handler` and "
    "`sparkrun_balancer.py` sits next to the config. NOTE: "
    "`router_settings.routing_strategy: simple-shuffle` in that file is "
    "EXPECTED even when balancing is active — it is the base strategy LiteLLM "
    "calls the balancer under, not drift; the balancer callback is the live "
    "signal. (`hscc verify` check `proxy_router_settings` must go quiet.)",
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
