# PR #19 — Live runtime upgrade runbook & recovery point

Task: t_34255265 · PR: https://github.com/pom11/hscc/pull/19
hermes-agent v2026.7.30 -> v2026.8.16.2 · sparkrun v0.3.1 -> v0.3.4

Status: RECOVERY POINT ESTABLISHED. Upgrade NOT yet executed — human is
performing the live upgrade manually (agent must not self-upgrade its own
running venv; this is the documented "strands the install half-updated"
failure the 0.17 series suffered).

---

## 1. Where the runtime actually lives

The 4 cluster nodes (spark@10.0.0.244/.246/.247/.248) run ONLY GPU
inference containers (scitrera/dgx-spark-vllm ... HSCC image, up 23h).
hermes-agent and sparkrun do NOT live on the nodes; they live on the macOS
CONTROL HOST (/Users/desac). The runtime upgrade is entirely on this host.

- hermes-agent install: /Users/desac/.hermes/hermes-agent  (git install)
- sparkrun install:     /Users/desac/sparkrun  (git install, editable venv)
- sparkrun CLI link:    ~/.local/bin/sparkrun -> ~/sparkrun/.venv-sparkrun-py313/bin/sparkrun

Cluster topology (from ~/.hscc/serving.json + cluster.json):
- orchestrator unit "orch": nodes 10.0.0.244 + .246, port 8000, tp=2
- worker unit (family=reasoning, keepalive): nodes 10.0.0.247 + .248, port 8000, tp=2
- NAS: 10.0.0.249
- local OpenAI-compatible proxy: http://localhost:4000

## 2. RECOVERY POINT (established 2026-08-18)

CURRENT (known-good, verified live):
- hermes-agent: branch `dep-bump-0.19.1` @ commit b1f96d250e
    = upstream release cc4cab2f59 (tag v2026.7.30) + 2 carried commits:
      82d2dac8ff  feat(kanban): kanban_submit_review + policy-gated review pairing
      b1f96d250e  fix(kanban): rewrite unknown create-assignee to default_assignee
    ROLLBACK: cd ~/.hermes/hermes-agent && git checkout dep-bump-0.19.1 && <reinstall deps>
- sparkrun: detached HEAD @ tag v0.3.1 (commit d77df74b5ce80bd5), editable venv
    .venv-sparkrun-py313 (pip install -e from ~/sparkrun)
    ROLLBACK: cd ~/sparkrun && git checkout v0.3.1 && .venv-sparkrun-py313/bin/pip install -e .

TARGET refs (already fetched locally):
- hermes-agent v2026.8.16.2 = upstream commit 7339f5f160d (tag present locally + remotely)
- sparkrun v0.3.4 = tag present locally at /Users/desac/sparkrun

LIVE BASELINE before upgrade (verified):
- sparkrun cluster status: 4 containers up 23h across 4 hosts (2 jobs, tp=2)
- proxy http://localhost:4000/v1/models: 3 models (deepseek-ai/DeepSeek-V4-Flash-0731,
  orchestrator-model, worker-model)
- real completion via proxy: POST /v1/chat/completions -> "BASELINE-OK" (1.2s, finish=stop)
- daemon verify: plugins ok, daemon_streams 10/10 healthy, proxy ok, config_wiring ok
  (multiplex check flags ~/.hermes/profiles/.pytest_cache as "not served" — false positive,
   pre-existing, unrelated to this bump)

## 3. Manual upgrade procedure (for the operator, from a clean external shell)

IMPORTANT: the gateway (PID ~92779) and all hermes workers currently run from
the shared hermes-agent venv. To mutate that venv safely the control plane must
be stopped first, upgraded, then restarted. Do this OUTSIDE any running hermes
agent. A prior 0.17 upgrade failed because the venv was rm -rf'd mid-upgrade;
hermes update handles this with pre-update backups and a migration step, so use
`hermes update` and do NOT hand-rm the venv.

### A. Stop the control plane (so the venv is idle)
    hermes gateway stop          # stops the multiplex gateway + its workers
    # confirm no hermes processes still run from the venv:
    ps -ef | grep hermes-agent/venv | grep -v grep

### B. Upgrade hermes-agent
    cd ~/.hermes/hermes-agent
    hermes update --yes         # pulls latest, reinstalls deps, runs config migration
    # (uses its own pre-update backup; keep the default backup behaviour)
    hermes --version            # expect Hermes Agent with 2026.8.16.2

### C. Upgrade sparkrun (git+editable, so NOT `sparkrun setup update` which
       requires `uv tool install`) — use git checkout:
    cd ~/sparkrun
    git checkout v0.3.4
    .venv-sparkrun-py313/bin/pip install -e .          # refresh editable install + new deps
    ~/.local/bin/sparkrun --version                    # expect 0.3.4
    # do NOT touch sparkrun/recipes/ (standing rule) — recipe registries update
    # via `sparkrun setup update --no-update-registries` only if needed; better to
    # refresh registries explicitly after upgrade if the bootstrap expects them.

### D. Re-bootstrap HSCC
    cd ~/dev/hscc   (or the repo live on the host)
    hscc-bootstrap/bootstrap.sh

### E. Component test suites
    cd ~/dev/hscc && scripts/run_tests.sh        # runs all suites (bootstrap, cluster,
                                     # commands, roles, daemon, sparkrun-hermes)

### F. Live verification (all required, a REAL completion is mandatory)
    # 1) cluster status
    ~/.local/bin/sparkrun cluster status
    # 2) proxy answers
    curl -sf http://localhost:4000/v1/models
    # 3) REAL completion through the multiplex/profile path (not just /health)
    curl -sf --max-time 120 http://localhost:4000/v1/chat/completions \
      -H 'Content-Type: application/json' \
      -d '{"model":"deepseek-ai/DeepSeek-V4-Flash-0731","messages":[{"role":"user","content":"Reply with exactly: POST-UPGRADE-OK"}],"max_tokens":16}'
    # 4) daemon streams actually update (fresh ~/.hscc/state/*.json timestamps)
    # 5) a slash command — e.g. issue a /workers-up or cluster-status slash via the gateway
    # 6) multiplex-served profiles respond with a real completion

## 4. Rollback (restore known-good), if needed

### hermes-agent
    cd ~/.hermes/hermes-agent
    git checkout dep-bump-0.19.1
    ./venv/bin/pip install -e .     # refresh deps back to 0.19.1
    hermes --version                # expect 2026.7.30 / v0.19.1

### sparkrun
    cd ~/sparkrun
    git checkout v0.3.1
    .venv-sparkrun-py313/bin/pip install -e .
    ~/.local/bin/sparkrun --version # expect 0.3.1

### restart gateway + confirm a REAL completion (mandatory after rollback)
    hermes gateway start
    curl -sf --max-time 120 http://localhost:4000/v1/chat/completions \
      -H 'Content-Type: application/json' \
      -d '{"model":"deepseek-ai/DeepSeek-V4-Flash-0731","messages":[{"role":"user","content":"Reply with exactly: RECOVERED-OK"}],"max_tokens":16}'

## 5. Release-notes scan (incompatibilities surfaced for PR #19)

~3802 commits / ~125 PRs between v2026.7.30 and v2026.8.16.2. Notable for an
HSCC headless fleet deployment:
- feat(mcp): migrate to the mcp 2.x SDK  -> any MCP server config may need re-adding
- feat(config): hermes config set now parses list/mapping literals in values
- feat(agent): empty-response guard settings moved from env vars to config.yaml
- kanban: memory-aware dispatch guard, host-level cap accounting, review-lane,
  worktree reuse/reap changes — touch dispatch/cap behaviour directly
- gateway: multiplex-profile failure surfacing (OOF-3) — improves visibility
- gateway: slash.exec skill-command check scoped per-profile
- desktop/gateway: remote profile routing changes (less relevant to headless CLI)
- cron: stop retry storms when gateway deliberately stopped

These are the changes most likely to require a config/profile migration or a
behaviour re-check on a headless multiplex cluster. Full list: git log
cc4cab2f59..7339f5f160d --oneline in ~/.hermes/hermes-agent.

## 6. [ADDED 2026-08-18] sparkrun v0.3.4 — CONFIRMED compatibility break: TP2/dspark-gx10 recipes

**HIGH-PRIORITY: this will refuse to launch unless a one-line fix is applied.**
Adds `Executor.verify_command_passthrough` (commit 078d9a4, "refuse a launch when
the image ENTRYPOINT swallows the serve command"). After a container sync, sparkrun
probes the image's ENTRYPOINT; a consuming ENTRYPOINT → hard **refusal to launch**
(fails closed).

The commit body **explicitly names `ghcr.io/anemll/dspark-vllm-gx10:0.1.1`** as the
offending image (its `ENTRYPOINT ["vllm","serve"]` eats the appended `bash -c <b64 cmd>`).

**This is the container in our TP2 recipe and 3 other recipes** (all dspark-gx10 /
anemll images), none of which set `entrypoint:`.

Fixes (either):
- launch with `-o entrypoint=''` (executor override, per-launch), OR
- set `entrypoint: ""` in the recipe (the pattern `runtimes/atlas.py` already uses).

The check is **fail-open** on unreachable host / `--dry-run` / `SPARKRUN_NO_IMAGE_PROBE=1`
/ container-less executor, so a dry-run will NOT surface it — this only bites on a
real launch. Measured cost: 0.73s passthrough / 17.8s consuming.

**Must be handled in step C/F if/when relaunching the TP2 recipes → refuse launch.
Before relaunching anything on sparkrun v0.3.4, apply `-o entrypoint=''` to the
dspark-gx10 recipes or set `entrypoint: ""` in-recipe.** (Do not edit files under
sparkrun/recipes/ — this is our local-fixed recipe dir ~/.sparkrun-local/recipes/
and/or the recipe metadata, not upstream.)
