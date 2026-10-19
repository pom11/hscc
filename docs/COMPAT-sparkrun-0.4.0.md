# COMPAT: sparkrun v0.3.10 -> v0.4.0 (HSCC fork branch, live cluster)

## Verdict

**v0.4.0 IS COMPATIBLE and is the LIVE runtime since 2026-10-19** (operator-
authorized cutover executed by the orchestrator; SOT card `t_ad483446`).
Both fork-branch features — the LiteLLM `router_settings` passthrough and the
least-in-flight balancer — were **absorbed upstream** in the 0.3.10..0.4.0
range, so the re-port was a no-op rebase: the new fork branch
`feat/proxy-least-in-flight-040` carries exactly ONE commit on top of
upstream tag `v0.4.0` (`b5c60edd`, a macOS-spawn test fix). The lock
`hscc-bootstrap/runtime-versions.json` now pins `v0.4.0`.

---

## Cutover shape (what actually changed on the host)

| item | before | after |
|---|---|---|
| fork branch | `feat/proxy-least-in-flight` (tip `609351e0`, on v0.3.10) | `feat/proxy-least-in-flight-040` (tip `b5c60edd`, on v0.4.0) |
| branch checkout | `~/sparkrun` | worktree `~/dev/sparkrun-live` |
| venv | `~/sparkrun/.venv-sparkrun-uv312` | `~/sparkrun/.venv-sparkrun-040` (side-by-side, uv py3.12) |
| shim | `~/.local/bin/sparkrun` -> uv312 venv | -> `.venv-sparkrun-040/bin/sparkrun` |

Rollback point (untouched): tag `pre-rebase-v040-safety`; the old branch and
its venv remain in place — a rollback is repoint the symlink, `sparkrun proxy
stop` + `start` (never `sync`/`--restart`).

## Behavior deltas that matter to HSCC

1. **proxy.yaml key moved.** `router_settings` is now read from
   `gateways.litellm.router_settings` (0.4.0 has NO top-level reader —
   `ProxyConfig` only exposes `gateway_settings()`). The live proxy.yaml keeps
   the top-level key for 0.3.10 rollback; 0.4.0 ignores it. `hscc_daemon/
   proxy_router.py::configured_intent` reads the top-level key — harmless
   while both keys carry the same mapping, but it is the drift-check's
   assumption to revisit if the keys ever diverge.
2. **`simple-shuffle` is now the HEALTHY emitted value.** With
   `balancing: least-in-flight`, 0.4.0 deliberately FORCES
   `routing_strategy: simple-shuffle` into the generated config
   (`BALANCING_ROUTING_STRATEGY`) so LiteLLM keeps calling the
   `sparkrun_balancer.handler` callback. Do not read `simple-shuffle` in
   `litellm_config.yaml` as "the balancer is dead" — the callback line is
   the live signal.
3. **Source layout moved.** The balancer/engine now live at
   `src/sparkrun/plugins/litellm/{engine,balancer}.py` (the fork features
   moved there when upstream restructured `proxy/` into gateway plugins).
   `src/sparkrun/proxy/config.py` still hosts `ProxyConfig`. The
   post-bump grep targets in `dep_bump_guard.py` and the bump runbook were
   updated to these paths.
4. **LiteLLM pin unchanged** (`litellm[proxy]==1.82.6` via uvx; the
   least-busy trap and the balancer rationale are unaffected).

## Verification evidence (2026-10-19, live)

- `sparkrun --version` = 0.4.0; symlink resolves to `.venv-sparkrun-040`
  (editable -> `~/dev/sparkrun-live/src`).
- `:4000/v1/chat/completions` real completions, HTTP 200: `worker-model` and
  `orchestrator-model`.
- `litellm_config.yaml`: `callbacks: [sparkrun_balancer.handler]` +
  `router_settings: {routing_strategy: simple-shuffle}` — the healthy 0.4.0
  shape (delta 2 above).
- `sparkrun status`: 1 job/host across the 3 serving nodes (10.0.0.x).
- `hscc verify`: all checks green incl. `proxy_router_settings` (quiet) and
  `chat_roundtrip` (text from 3 serving units); multiplex 44 profiles;
  daemon 12/12 streams.
- hscc full suite (operator-integration run vs live 0.4.0): 11/11 green.
- dep-bump guard + watcher + CI-generator tests updated to the new branch
  name and pass.

## Follow-ups recorded (not blockers)

- If a future bump re-diverges the two proxy.yaml router_settings keys,
  teach `proxy_router.configured_intent` to prefer
  `gateways.litellm.router_settings` when present (WARN, never fail rule
  stays).
- The old `feat/proxy-least-in-flight` branch + `.venv-sparkrun-uv312` may
  be retired once 0.4.0 has survived a release cycle; until then they ARE
  the rollback path — do not prune.
