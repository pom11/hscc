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

1. **Where bump workers read text (all three surfaces now covered).**
   - PR body: `.github/scripts/check_runtime_deps.py` `main()` body builder
     (~L215-235). Section "sparkrun fork-branch rule" appears only when the
     bump touches sparkrun.
   - Card body: `scripts/dep_pr_watcher.py` `_card_body()` — the bump card
     created on the board for PRs whose title contains `sparkrun`. The real
     PR title format is `chore(deps): runtime bump — sparkrun v0.2.40 -> v0.2.41`
     (workflow TITLE at check-runtime-deps.yml:63), so the title match fires.
   - Skill: `references/runtime-bump-verification.md` — the bump cards are
     assigned to `backend-engineer` (dep_pr_watcher ASSIGNEE), and this is the
     only runtime-bump procedure skill. It existed ONLY in the runtime trees
     (`~/.hermes/skills/...`), was missing from the repo payload, and was not
     listed in any SKILL.md index — three fixes: canonical copy now in
     `install/hscc-skills/hscc/references/` (shipped by `hscc.py install-skills`),
     index line added to payload + both live trees.
   - All three load the SAME canonical text from `hscc_daemon/dep_bump_guard.py`
     (branch `feat/proxy-least-in-flight`, remote `pom11/sparkrun` — verified
     live before writing). A load failure emits a loud "GUARD UNAVAILABLE"
     line instead of a silently rule-less card.

2. **Drift check semantics.** `hscc_daemon/proxy_router.py::router_drift`
   mirrors sparkrun's own emission rules (verified against the live fork
   source, read-only): `ProxyConfig.router_settings` (config.py:276-285 —
   non-mapping ignored), `BALANCING_CALLBACKS = {"least-in-flight":
   "sparkrun_balancer.handler"}` (engine.py:57), emit-only-if-truthy
   (engine.py:193-194), unimplemented balancing warn-and-ignore
   (engine.py:736-738). The least-busy flag is grounded in
   `src/sparkrun/proxy/balancer.py`'s docstring: LiteLLM's least-busy counter
   is never incremented on the async path — ties go to the first model_list
   entry. Statuses: ok / warn / skip (no proxy.yaml — check not applicable) /
   unverified (unreadable). WARN never makes anything red; verify-check `ok`
   is True except None-on-unverified.

3. **Daemon stream wiring.** The `proxy` stream (daemon_ops PERIODIC_INTERVALS
   "proxy": 60) runs `health.check_proxy`, NOT verify.check_proxy. So the
   verdict is recorded inside health.check_proxy on BOTH write paths
   (healthy + unhealthy) as `router_drift` / `router_drift_warn`, behind a
   `_router_drift_probe()` seam (tests never read the operator's live
   configs); WARN log line while drift is live. It never changes the health
   verdict or triggers a restart. verify gains the sibling check
   `proxy_router_settings` in `run_all` (10 -> 11 checks).

4. **Live state passes today.** Read-only probe of the real files returned
   `status: ok` — litellm_config carries proxy.yaml's router_settings
   ({'routing_strategy': 'simple-shuffle'}) + balancer callback. No false
   warning on the operator's current setup (simple-shuffle + least-in-flight
   balancer coexist by design: the balancer callback does the balancing).

5. **hscc-dep-watcher cron (investigate-only item).** The job EXISTS and is
   ACTIVE (id 09304286e562, daily 08:00) — it is NOT paused and NOT absent,
   so `cron resume` does not apply. It has FAILED 17 runs in a row:
   `Script not found: /Users/desac/.hermes/profiles/backend-engineer/scripts/dep_pr_watcher.py`.
   Root cause: the job was created from the backend-engineer profile, and
   hermes resolves `--script` names against THIS profile's scripts dir
   (cron/scheduler_script.py:~294 — "profiles never share files"), while
   `scripts/install_dep_watcher.sh` installs the script to the SHARED
   `~/.hermes/scripts/` and the header doc suggests the default profile.
   So the watcher has never created a card since the profile split.
   NOT fixed here (card says investigate+report, do not install/mutate): the
   fix is `cp scripts/dep_pr_watcher.py
   ~/.hermes/profiles/backend-engineer/scripts/` (or re-create the job from
   the profile whose scripts dir install_dep_watcher.sh targets) —
   one-line operator action, fan a card if desired.

## Mutation battery (8/8 CAUGHT, restored, tree clean)

M1 CI rule disabled / M2 card rule disabled / M3 divergence comparison
disabled / M4 least-busy trap removed / M5 warn->hard-fail / M6 healthy-stream
write loses drift / M7 unhealthy-stream write loses drift / M8 loader-failure
goes silent. Restore via in-memory copies with sha pins (never `git checkout --`).

## Gates

- Canonical suite: see completion metadata (run after this file lands).
- `scripts/address_guard.py --repo .` exit 0 before every commit (only
  documented 10.0.0.x placeholders in test fixtures).
- changelog.d/t_39d7c915.md; no attribution lines; PUSH ONLY.

