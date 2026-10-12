kind: Fixed
task: t_proxy_outage_20261011

- **A `200` from the proxy with an empty model list counted as healthy.**
  `check_proxy()` used `http_check()` — a pure reachability probe — so a
  litellm load-balancer whose discovery sweep had wiped its config answered
  `200` with `{"data": []}` and the daemon called it healthy forever, while
  every `worker-model` call behind it 404'd (the 2026-10-11 outage). Health
  is now a BODY verdict: `{"data": []}` / a non-parseable body / a missing
  expected role alias (`orchestrator-model` + `worker-model` from the
  serving.json units — both aliases under a uniform_pool) are all unhealthy
  and trigger the relaunch path; with no serving.json the floor is `>=1`
  model. The state payload records the reason and the served ids.
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

- `hscc_daemon/tests/test_health.py`: TestCheckProxy 3 -> **12** cases —
  the outage shape (200 + `data: []` must relaunch), each alias-missing
  shape (non-uniform worker-only, uniform worker-only / orchestrator-only),
  the healthy baseline, uniform-pool relaunch hosts INCLUDING the
  orchestrator node (the union), non-uniform relaunch hosts UNCHANGED
  (worker-only), plus direct units for the probe's body-parsing and the
  healthy-verdict matrix. Stubs `_proxy_models_probe` +
  `serving.load_serving` — never the live `:4000`, never the operator's
  live serving.json.
- `hscc-bootstrap/tests/test_enable_plugins.py`: +**10** goal_judge cases
  (root+profiles wiring, never-overwrite, model-less-block,
  totally-untouched-when-model-set, idempotency, and
  `test_sweep_root_comes_from_config_path_not_env` — the live-tree-leak
  guard). `hscc-bootstrap/tests/test_doctor.py`: +**17** cases across the
  three checks + the skip-in-suite / warnings-never-fatal gates.
