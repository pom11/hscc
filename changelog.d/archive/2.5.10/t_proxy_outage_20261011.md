kind: Fixed
task: t_proxy_outage_20261011

- **A `200` from the proxy with an empty model list counted as healthy.**
  `check_proxy()` used `http_check()` — a pure reachability probe — so a
  litellm load-balancer whose discovery sweep had wiped its config answered
  `200` with `{"data": []}` and the daemon called it healthy forever, while
  every `worker-model` call behind it 404'd (the 2026-10-11 outage). Health
  is now a BODY verdict: `{"data": []}` / a non-parseable body / a missing
  expected role alias are all unhealthy and trigger the relaunch path; the
  expected aliases are scoped to the hosts `proxy start --hosts` actually
  points the proxy at (a non-uniform fleet reaches only keep-alive workers,
  so `orchestrator-model` is not required there; a uniform pool demands both)
  — an alias the proxy cannot serve must never make it unhealthy; with no
  serving.json the floor is `>=1` model. The state payload records the
  reason and the served ids.
- **Proxy relaunch dropped the orchestrator node under a uniform pool.**
  `serving.KEEPALIVE_NODES` excludes orchestrator nodes by design
  (`_resolve_serving_overlay`), and the relaunch passed exactly that set to
  `sparkrun proxy start --hosts`, so a uniform-pool LB relaunched after the
  outage could never re-add the orchestrator's endpoint. Under
  `is_uniform_pool()` the relaunch now passes keep-alive workers ∪
  `orchestrator_nodes(serving)` — all serving nodes. Non-uniform pools
  behave exactly as before.
- **Bootstrap never wired `auxiliary.goal_judge` — and only ever wired ONE
  config.** Hermes' goal-loop judge reads it per loaded config; the root
  config carried it (hand-edited) and NOT ONE role profile did, so every
  dispatched worker's judge fell through to provider:auto → cloud
  credential read with no active secret scope → threw. `_ensure_goal_judge`
  routes it at the local worker proxy (custom / worker-model /
  `http://localhost:4000/v1` / timeout 600), NEVER overwriting a
  goal_judge that names a model, and `ensure_goal_judge_everywhere` sweeps
  the root config AND every `profiles/*/config.yaml` — with the sweep root
  derived from `config_path`, never ambient `HERMES_HOME` (the t_267f9d88
  ambient-state class of bug: a dispatched worker's profile-scoped home
  must not steer a write sweep across the live tree).
---
kind: Added
task: t_proxy_outage_20261011

- **Durable relaunch cooldown on the proxy check (PR #22 review).** A
  persistently-unhealthy proxy used to invoke `sparkrun proxy start` every
  check tick (60s): each attempt runs a full SSH endpoint discovery sweep
  and spams WARN, even though plain `proxy start` REFUSES a running proxy
  (sparkrun `engine.py` returns 1 on `is_running()`; SIGTERM-ing the live
  proxy happens only under `--restart`, which we never pass). EVERY relaunch
  attempt now stamps `last_relaunch` in the proxy STATE FILE — success or
  failure, because `proxy start` exiting 0 over still-empty discovery is
  precisely the 2026-10-11 shape — and further attempts are suppressed
  until `HSCC_PROXY_RELAUNCH_COOLDOWN_S` (default 300s) elapses. The stamp
  lives in the state file, not module memory, so a daemon restart cannot
  reset the window (the in-memory state flaw). Suppressed ticks carry the
  original stamp forward verbatim (re-stamping would extend the window
  forever); the HEALTHY branch rewrites the state without the stamp, so a
  recovered proxy clears the cooldown on its own and the next genuine
  down-proxy recovers on its next tick.
- **Three report-only doctor checks for the outage's blind spots.**
  `sparkrun lan reachability` TCP-probes every serving node FROM SPARKRUN'S
  OWN INTERPRETER (shebang of the resolved CLI): a python denied the macOS
  Local Network grant answers errno 65 for every private-range connect
  while ssh/nc work fine, and only a probe under that binary's TCC identity
  can see it — the verdict names the fix (rebuild sparkrun on a uv Python)
  and also warns when the interpreter is a Homebrew/Framework python.
  `sparkrun cluster vs serving` warns when `sparkrun cluster default` hosts
  differ from serving.json's nodes (the outage cluster lacked the
  orchestrator node). `serving consistency` flags >1 sparkrun container on
  one serving.json-mapped node+port (the intent-hash duplicate class) and
  stale endpoint records (serving.json entry with no container). All three
  are warnings — report only, never fatal — and all take fleet I/O through
  injected seams; `run_doctor()` auto-skips them inside pytest so no test
  can accidentally touch the live fleet.
---
kind: Verified
order: 1

- `hscc_daemon/tests/test_health.py`: TestCheckProxy 3 -> **21** cases —
  the outage shape (200 + `data: []` must relaunch), alias-missing shapes
  scoped to the relaunch host set (non-uniform worker-only catalogue is
  HEALTHY and must NOT relaunch — the PR #22 blocking case; reachable
  worker alias missing DOES relaunch; uniform pool demands both), the
  healthy baseline, uniform-pool relaunch hosts INCLUDING every unit node,
  non-uniform relaunch hosts UNCHANGED (worker-only), the durable relaunch
  cooldown (EVERY attempt stamps — an ok exit over still-empty discovery is
  the 10-11 shape and must still suppress the next tick; suppressed ticks
  carry the stamp forward without extending the window; expiry retries;
  garbage stamps tolerated; healthy rewrite clears so recovery stays fast),
  plus direct units for the probe's body-parsing, the healthy-verdict
  matrix and host-scoped alias expectation. Stubs `_proxy_models_probe` +
  `serving.load_serving` — never the live `:4000`, never the operator's
  live serving.json.
- `hscc-bootstrap/tests/test_enable_plugins.py`: +**10** goal_judge cases
  (root+profiles wiring, never-overwrite, model-less-block,
  totally-untouched-when-model-set, idempotency, and
  `test_sweep_root_comes_from_config_path_not_env` — the live-tree-leak
  guard). `hscc-bootstrap/tests/test_doctor.py`: +**17** cases across the
  three checks + the skip-in-suite / warnings-never-fatal gates.
---
kind: Fixed
- **The empty-proxy outage was detected but could never heal.** Plain
  `sparkrun proxy start` refuses a proxy that is already running (exit 1),
  and the 2026-10-11 shape is exactly a LIVE proxy whose discovery emptied
  its catalogue — so every relaunch attempt failed and the proxy stayed
  empty. The relaunch now passes `--restart` when, and only when, the proxy
  answered with a parseable but EMPTY catalogue: it serves nothing, so
  there is no in-flight traffic to drop. A partial catalogue (one alias
  missing, others served) keeps a plain start so live traffic is never cut,
  and an unreachable proxy is not running, so plain start brings it up.
  No `--host` is passed, so the persisted bind address is unchanged.
