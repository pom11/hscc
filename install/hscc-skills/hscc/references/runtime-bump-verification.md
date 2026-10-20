# Runtime-Dependency Bump Verification (live cluster)

Use when a dependabot-style PR bumps a runtime HSCC depends on (hermes-agent,
sparkrun, vLLM) and it must be verified against the LIVE cluster before merge
(e.g. pom11/hscc PR #19: hermes v2026.7.30→v2026.8.16.2, sparkrun v0.3.1→v0.3.4).
The verified-versions lock lives at `hscc-bootstrap/runtime-versions.json`
(the `check-runtime-deps` workflow opens the PR; a local dep-watcher routes a
verification card to the hscc kanban board).

## sparkrun runs from OUR FORK BRANCH — the bump is a rebase, not an install

The cluster does not run upstream sparkrun `main`. Live state (operator-verified
2026-10-20 08:37, post 0.4.1 cutover): `~/.local/bin/sparkrun` ->
`~/sparkrun/.venv-sparkrun-041/bin/sparkrun`, an editable install
(`__editable__.sparkrun-0.4.1.pth` -> `~/dev/sparkrun-041-verify/src`) of
worktree `~/dev/sparkrun-041-verify` checked out on
**`feat/proxy-least-in-flight-041`** @ `1aaa6479` (remote `fork` =
pom11/sparkrun; `origin` = spark-arena/sparkrun).
v0.4.1 absorbed both fork features upstream (the `router_settings` passthrough
and the least-in-flight balancer), so the branch carries only a macOS-spawn test
fix on top of the tag — but it is still a fork branch, and the rules below still
apply (a future bump may re-introduce divergence, and `git checkout main` in a
stale checkout still drops whatever the branch carries).

**Rollback line — do NOT rebase or bump these:** the v0.4.0 deployment is kept
intact at worktree `~/dev/sparkrun-live` on `feat/proxy-least-in-flight-040` @
`b5c60edd`, editable via `~/sparkrun/.venv-sparkrun-040`. Rolling back the proxy
means repointing `~/.local/bin/sparkrun` at that venv, not editing the `-040`
branch. The 0.3.10-era branch `feat/proxy-least-in-flight` (tip `609351e0`) is
kept in the `~/sparkrun` worktree as the deeper rollback point, tagged
`pre-rebase-v040-safety`.

Rules for a sparkrun bump (canonical text: `hscc_daemon/dep_bump_guard.py`,
pinned by tests; the bump PR body and the watcher card both carry it):

1. `git -C ~/dev/sparkrun-041-verify fetch origin <new-tag>`, then **rebase
   `feat/proxy-least-in-flight-041` onto that tag** (the rebase happens in
   `~/dev/sparkrun-041-verify`, where the branch is checked out) and LEAVE THE
   BRANCH CHECKED OUT.
2. **Never `git checkout main`, never reinstall from upstream.** Either one
   silently drops the balancer on the next proxy restart — the proxy keeps
   serving, routing quietly reverts to `simple-shuffle`, nothing errors.
3. Verify after the bump:
   `git -C ~/dev/sparkrun-041-verify rev-parse --abbrev-ref HEAD` ==
   `feat/proxy-least-in-flight-041`, and `router_settings` is still read in
   `src/sparkrun/plugins/litellm/engine.py`
   (`_configured_router_settings`, which pulls `gateways.litellm.router_settings`
   via `ProxyConfig.gateway_settings`) and emitted into the generated litellm
   config there (`build_litellm_config`). Do NOT grep `src/sparkrun/proxy/config.py`
   for `router_settings` and read a no-match as drift: since v0.4.0 that file
   keeps only the generic `gateways.<name>` accessor and has no `router_settings`
   at all (verified against tags v0.4.0 and v0.4.1).
4. Restart the proxy, then confirm `~/.cache/sparkrun/proxy/litellm_config.yaml`
   carries the `router_settings` from `~/.config/sparkrun/proxy.yaml` — the
   `hscc verify` check `proxy_router_settings` (and the `proxy` daemon
   stream's `router_drift` field) must be quiet. `routing_strategy:
   least-busy` in router_settings is a trap on the pinned LiteLLM (no-op);
   the working knob is `gateways.litellm.balancing: least-in-flight`.
   NOTE (0.4.0): the balancer makes sparkrun FORCE `routing_strategy:
   simple-shuffle` in the emitted config (`BALANCING_ROUTING_STRATEGY`) so
   LiteLLM keeps calling the callback — `simple-shuffle` in
   `litellm_config.yaml` is the HEALTHY emitted value when the balancer is
   live, not a regression. Since 0.4.0, `router_settings` is read from
   `gateways.litellm.router_settings`; the top-level proxy.yaml key is kept
   only for 0.3.10 rollback and 0.4.0 ignores it (the drift check mirrors
   that read order — see `proxy_router.router_settings_source`).

## Golden rule: recovery point FIRST, before touching anything live

Never proceed past the first step until you can state precisely how you'd roll
back. A prior hermes 0.17 upgrade FAILED LIVE (setup script `rm -rf`'d the venv
mid-upgrade) — do not repeat. Establish all three:

1. **Git tag + safety branch** at known-good `main` and **push to origin** so the
   point is durable (not just local): `git tag recover/pr19-pre-<ts>` +
   `git branch backup/pr19-pre-<ts> HEAD` + `git push origin <tag> <branch>`.
2. **On-disk backup dir** `~/.hscc/_recovery-<id>/` with `runtime-versions.json.pre`
   + a `recovery-manifest.txt` listing: hscc HEAD, live `hermes --version`,
   `sparkrun --version`, install paths, rollback commands.
3. **Independent known-good re-confirmation**: verify the live baseline is
   actually healthy with a REAL completion, not a version check — 2 nodes / 2
   jobs up, proxy answers, real `/v1/chat/completions` returns. This is the
   baseline future rollbacks must restore to.

For sparkrun specifically, also snapshot the live fork branch tip (`git -C ~/dev/sparkrun-041-verify
rev-parse HEAD` into the manifest) — it is the thing a bad bump destroys.

## Technique: read the ACTUAL commit range, not just release notes

Release notes miss behavior changes. Diff the real source between the installed
and target tags/commits:

```bash
# sparkrun
cd ~/sparkrun && git log --oneline v0.3.1..v0.3.4            # what changed
git show <commit-that-changed-runtime-behavior>              # read the body — explains WHY
git diff --stat v0.3.1 v0.3.4
# hermes (tags may not all exist; find release commits via --grep then diff SHAs)
cd ~/.hermes/hermes-agent && git rev-list --count <oldrelease>..<newrelease>
git log --oneline <old>..<new> | grep -iE "breaking|feat"|head
```

Hunt for **hard behavior changes that break existing recipes/config**, e.g.:
- A launch **refusal** the old version didn't have.
- A config/schema migration that invalidates existing entries.
- A protocol rewrite (MCP 2.x SDK, stateless protocol) that needs servers re-added.
- A **proxy config-shape change** (e.g. upstream PR #317 moved settings under
  `gateways: {litellm: {...}}` and removed `ProxyConfig.router_settings`) —
  rebase conflicts on the fork branch land exactly here.

## Worked example: sparkrun v0.3.4 entrypoint refusal (PR #19)

Commit `078d9a4` added `Executor.verify_command_passthrough`: after container
sync, sparkrun probes the image's ENTRYPOINT and a **consuming ENTRYPOINT → hard
refusal to launch** (fails closed). The commit body explicitly named
`ghcr.io/anemll/dspark-vllm-gx10:0.1.1` — exactly our TP2 recipe's container +
3 others, none setting `entrypoint:`. Fix: `-o entrypoint=''` per-launch, or
`entrypoint: ""` in-recipe (the `runtimes/atlas.py` pattern). The check is
**fail-open on `--dry-run`**/unreachable host/`SPARKRUN_NO_IMAGE_PROBE=1`, so it
won't surface in a dry run — only a real launch. Lesson: always grep for whether
the changed version touches the exact container/image/recipe the cluster runs.

## Architectural constraint: an agent cannot safely self-upgrade its own runtime

The gateway + all workers run from the shared hermes-agent venv. `hermes update`
stops/restarts the gateway and mutates that venv — running it from a live agent
cuts off the agent's own control plane mid-upgrade and strands the venv
half-updated (the 0.17 `rm -rf` failure). **The operator (human) must run the live
upgrade from a clean external shell.** The agent's job is: establish the recovery
point, write a runbook with the exact upgrade + rollback + live-verification
commands, surface incompatibilities, and leave the PR OPEN. Do not attempt the
upgrade yourself, and do not merge to main (that is always the human's call).

## Verification handoff

- Write the runbook into the assigned worktree and **commit + push its branch**
  (a worktree can be reaped; the pushed branch persists the findings).
- Update the PR's own checklist with evidence per item.
- Note incompatibilities directly on the PR as a comment.
- Leave the PR OPEN with `needs-cluster-check`; never auto-merge a live runtime change.
- Diff parity: `git fetch` the PR branch before diffing — a stale local ref can
  show an older diff (e.g. `v2026.8.16` before a force-push to `v2026.8.16.2`).
  Re-fetch then re-diff to confirm the local branch matches the GitHub PR.
