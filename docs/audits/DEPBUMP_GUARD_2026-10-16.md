# Dep-bump fork-branch guard + router_settings drift check (t_39d7c915)

Card: t_39d7c915 (duplicate of t_2eda0b8a — see DUPLICATE_STATUS below).
Base: e2278355 (2.9.1). Branch: wt/t_39d7c915.

## Scope (as understood after the two orchestrator shape updates on this card)

1. RULE TEXT where a sparkrun dep-bump worker reads it:
   - the generated bump PR body in `.github/scripts/check_runtime_deps.py`
   - the dep-bump verification skill the bump worker loads
   The rule: sparkrun is deployed as FORK BRANCH `feat/proxy-least-in-flight`
   (remote `fork` = pom11/sparkrun) via editable install at `~/sparkrun`
   (`~/.local/bin/sparkrun` -> `~/sparkrun/.venv-sparkrun-uv312/bin/sparkrun`).
   A sparkrun bump REBASES that branch onto the new upstream tag and keeps it
   checked out. Never `git checkout main`, never reinstall from upstream.
2. WARN-check (never fail, never write, never restart) that the live
   `~/.cache/sparkrun/proxy/litellm_config.yaml` carries the `router_settings`
   from `~/.config/sparkrun/proxy.yaml`, and flag `routing_strategy: least-busy`.
3. Tests with fakes only (tmp paths).
4. Report the `hscc-dep-watcher` cron state (investigate, do not install).

## Live state observed (read-only, nothing modified)

- `git -C ~/sparkrun rev-parse --abbrev-ref HEAD` -> `feat/proxy-least-in-flight`
- `git -C ~/sparkrun log --oneline -3`:
  `609351e0 proxy: least-in-flight balancing for the LiteLLM gateway`,
  `5b0f9b5e proxy: emit LiteLLM router_settings from proxy.yaml`,
  `b444da87 Merge pull request #302 from spark-arena/develop`
- remotes: `fork` = pom11/sparkrun, `origin` = spark-arena/sparkrun
- local branches include BOTH `feat/proxy-least-in-flight` (current) and the
  now-superseded `feat/proxy-router-settings`, plus `feat/proxy-load-aware`,
  `feat/litellm-least-in-flight`, `main`, `pre-dep-bump`, ...
- `~/.local/bin/sparkrun` -> `~/sparkrun/.venv-sparkrun-uv312/bin/sparkrun` (editable)
- `~/.config/sparkrun/proxy.yaml`: `aliases: {}`,
  `router_settings: {routing_strategy: simple-shuffle}`,
  `gateways.litellm.balancing: least-in-flight`
- `~/.cache/sparkrun/proxy/litellm_config.yaml` (mode 0600) emits
  `router_settings: routing_strategy: simple-shuffle`

## DUPLICATE_STATUS

t_2eda0b8a (created 2026-10-15 16:33) and t_39d7c915 (created 2026-10-15 20:46)
carry the same deliverable and BOTH are claimed/running. The re-filed copy is
the one that received the two orchestrator updates; the earlier card has no
comments, so its worker is working from superseded instructions. Details in the
card comment thread.

## Findings

(append as they land)
