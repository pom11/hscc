# sparkrun v0.3.10 → v0.4.0 — compatibility record (2026-10-19)

Operator-authorized cutover, executed same-day. This doc is the compat record
for the `runtime-versions.json` bump; the fork-branch invariants live in
`hscc_daemon/dep_bump_guard.py` (pinned by `test_dep_bump_guard.py`).

## Deployed topology (v0.4.0)

| Piece | Value |
|---|---|
| Fork branch | `feat/proxy-least-in-flight-040` @ `b5c60edd` (fork remote `pom11/sparkrun`), cut off upstream tag `v0.4.0` |
| Editable checkout | `~/dev/sparkrun-live` (a worktree of `~/sparkrun`) |
| Venv | `~/sparkrun/.venv-sparkrun-040` (uv, CPython 3.12) |
| CLI chain | `~/.local/bin/sparkrun` → `.venv-sparkrun-040/bin/sparkrun` → venv python |
| Rollback line | `.venv-sparkrun-uv312` (0.3.10) + branch `feat/proxy-least-in-flight` @ `609351e0` + tag `pre-rebase-v040-safety`, all untouched |

The cherry-pick re-port of the two fork commits (`5b0f9b5e` router_settings
passthrough, `609351e0` least-in-flight balancing) was **aborted on evidence:
upstream v0.4.0 already absorbed both features** (config reads
`gateways.litellm.router_settings`, balancer ships in
`src/sparkrun/plugins/litellm/`). The fork branch at this tag is upstream
`v0.4.0` plus one macOS test fix — the rebase rule is kept anyway because it
is tag-independent.

## Live verification (all at cutover time)

- `sparkrun --version` → `0.4.0` via the live symlink.
- Proxy `:4000`: `worker-model` and `orchestrator-model` chat → HTTP 200 with
  real completion content (finish_reason=stop, usage) — not a hollow 200.
- Balancer live: generated `~/.cache/sparkrun/proxy/litellm_config.yaml`
  carries `litellm_settings.callbacks: [sparkrun_balancer.handler]`;
  `proxy.yaml` has `gateways.litellm.balancing: least-in-flight`. Operator
  burst test: 6 concurrent requests spread evenly across the 3 serving hosts
  (.244/.247/.248) — load-aware routing demonstrably active.
- `sparkrun status`: 1 job/host on all 3 serving hosts.
- Daemon: launchd-managed, re-resolved sparkrun to `.venv-sparkrun-040`
  (CLI + structured-status python), zero `structured status unavailable`
  lines post-restart, gateway check ok, `hscc project doctor` 14/14.
- HSCC full suite against the 0.4.0 runtime: 11/11 legs green (see PR body).

### `simple-shuffle` in `router_settings` is EXPECTED

The generated `litellm_config.yaml` shows
`router_settings.routing_strategy: simple-shuffle` even though balancing is
active. That line is the **base strategy LiteLLM invokes the balancer under**;
the `sparkrun_balancer.handler` callback overrides routing decisions. It is
**not drift and not a failed migration** — do not "fix" it and do not
stop/start the proxy over it. The live signal is the `callbacks` line (and
observed spread across hosts). `dep_bump_guard.FORK_RULE_LINES` states this
so future bump verifiers don't mis-flag it.

## sparkrun's own suite on this host — honest status

- Targeted gates for the cutover surface
  (`test_litellm_balancer.py`, `test_proxy.py`,
  `test_proxy_config_concurrency.py`): **151 passed, RC=0**.
- Full suite on this macOS host: **270 failed / 12724 passed / 95 skipped**
  (deterministic across two runs). Failure concentration is in host-dependent
  areas (`test_model_cache_*`, `test_model_artifacts`, `test_rdma_script`,
  `test_distribution_*`, `test_local_state_*`), with sampled causes that are
  host-environment mismatches (e.g. tests shelling out with GNU-only flags →
  BSD `realpath: illegal option`; `an absolute HF cache home is required`).
  The v0.4.0 test tree is ~2× the fork's 0.3.10 tree (455 vs 244 files) —
  upstream added most of these areas, and **the fork's 0.3.10 suite was never
  run on this host either**, so there is no local baseline for comparison.
- Verdict recorded honestly: the 0.4.0 cutover was gated on the targeted
  proxy/balancer suite + the live cluster verification above, **not** on a
  green full sparkrun suite. A full-suite classification on a supported
  (Linux/DGX) runner — or a green xfail policy from upstream — is the
  follow-up owed before the *next* bump.
  (Process note: an early "sparkrun suite green" claim in the cutover session
  was `tail`'s exit code read through a pipe — caught and corrected the same
  day. Do not trust `cmd | tail; echo $?` as a gate, ever, including by us.)

## Runbook lessons (both operator-confirmed the hard way)

1. **Never stop the proxy from a session whose own model rides the proxy.**
   The orchestrator's model is served by :4000; an inline `proxy stop` kills
   the assistant's own inference mid-cutover. A cutover must be one detached
   script: stop → swap symlink → start → verify, run outside the dependent
   session.
2. **`proxy stop` must treat a zombie PID as dead.** The stop→start left an
   `already running (PID …)` error because an orphaned 0.3.10 autodiscover
   process was holding a zombie PID that `stop` could not clear; it needed a
   manual SIGTERM of the orphan. Upstream ask (t_bbce7f58 class): stop should
   verify the port owner and reap/ignore stale state-file PIDs instead of
   refusing.
3. Proxy changes stay **stop + start only** — never `sync`, never
   `--restart` (operator rule, structurally true: 0.4.0's `proxy`
   subcommands expose start/stop/status/sync only).

## Rollback (tested-by-preparation, not executed)

```bash
ln -sfn ~/sparkrun/.venv-sparkrun-uv312/bin/sparkrun ~/.local/bin/sparkrun
sparkrun proxy stop && sparkrun proxy start
launchctl kickstart -k gui/$(id -u)/com.hermes.hscc_daemon
```

Run from a detached script / session not riding :4000 (lesson 1). Config
backup: `~/.config/sparkrun/proxy.yaml.pre-040-backup`. The nested
`gateways.litellm.router_settings` in proxy.yaml is inert for 0.4.0 (it
ignores the top-level duplicate), so it stays for the 0.3.10 rollback path.

## What the watcher will do next

`check-runtime-deps` compares the lock (`v0.4.0`) against the latest upstream
tag and will open the next bump PR when upstream moves again (v0.4.1-line
tags are already shipping). The dep-bump guard now names the correct live
branch/checkout for that flow.
