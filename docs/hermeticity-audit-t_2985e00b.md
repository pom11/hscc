# Hermeticity audit — t_2985e00b

Operator-state hermeticity audit of the HSCC test suite. Question asked: does
any test, when run, mutate live operator state under `~/.hscc` or
`~/.hermes/profiles` (or read live files in a way that produces wrong
results), and is that enforced by an automated guard?

Three layers were examined:

1. Static grep of every `$HOME`-rooted module constant.
2. Static grep of function-local imports that could bypass module-attribute
   patching.
3. An empirical, cross-process proof: hash + content-snapshot all
   `~/.hscc/*.json` and `~/.hermes/profiles/*/config.yaml`, run the full
   suite, re-snapshot, and diff — distinguishing a legitimate daemon write
   from a test write by content.

## Empirical result (the ground truth)

Full suite run (all 7 packages, `scripts/run_tests.sh`, EXIT=0):

| package          | result                      |
|------------------|-----------------------------|
| hscc-bootstrap   | 270 passed                  |
| hscc-cluster     | 147 passed                  |
| hscc_daemon      | 376 passed, 14 skipped      |
| hscc-roles       | 1004 passed                 |
| sparkrun-hermes  | 8 passed                    |
| hscc-api         | 502 passed, 1 skipped       |

Snapshot comparison: 63 files tracked (24 `~/.hscc/*.json` + 39
`~/.hermes/profiles/*/config.yaml`). **Only 2 files changed hash, and both are
pure daemon writes** — no test value appears in either diff:

* `autodown.json` — only `last_activity_iso` advanced
  (12:24:10 → 12:35:46). Every fixture-signature field is byte-identical:
  `enabled`, `idle_minutes: 120`, `state: up`, `down_since: null`,
  `force_armed: false`, `force_armed_overrides: []`.
* `watchdog-block.json` — only the rolling `failures[]` ring-buffer timestamps
  rotated (the daemon records a *successful* probe every ~31s). Authoritative
  fields identical: `blocked: false`, `failed_count: 0`, `auto_restart_count:
  3`, `last_restart`.

**Zero of the 39 profile `config.yaml` files changed.** **Zero of the other 22
`~/.hscc` files changed** (serving.json, cluster.json, state/, api.json,
queued_messages.json, budget.json, events.jsonl, activity.json, etc.).

Conclusion: **the suite is hermetic against operator state (file-wise)**. There
is no current live leak. The two hash deltas are the known "daemon churns on
its own" cases the audit explicitly distinguished by content diffing.

## Static findings

### $HOME-rooted module constants (write-capable) + coverage

| constant | path | write path? | autouse-covered? |
|----------|------|-------------|------------------|
| autodown.AUTODOWN_FILE | ~/.hscc/autodown.json | yes | yes (hscc_daemon conftest) |
| autodown.AUTODOWN_LOCK | ~/.hscc/autodown.lock | yes | yes |
| autodown.AGENTS_FILE | ~/.hscc/agents.json | yes | yes |
| autodown.HTTP_ACTIVITY_STATE | ~/.hscc/activity.json | yes | yes |
| replay.QUEUE_FILE | ~/.hscc/queued_messages.json | yes | yes |
| lifecycle.BRIDGE_FILE | ~/.hscc/bridge.json | yes | yes |
| lifecycle.WATCHDOG_BLOCK_FILE | ~/.hscc/watchdog-block.json | yes | yes |
| state.STATE_DIR | ~/.hscc/state | yes | yes |
| usage.BUDGET_FILE | ~/.hscc/budget.json | yes | yes |
| recover.RECOVER_STATE_FILE | ~/.hscc/recover.json | yes | yes |
| trigger.EVENTS/TRIGGERS/COOLDOWN | ~/.hscc/* | yes | yes |
| daemon_ops.PID_FILE / LOG_FILE / STATE_DIR / HSCC_DIR | ~/.hscc | yes | yes |
| desktop.HSCC_DIR | ~/.hscc | yes | yes |
| hscc.BRIDGE_FILE / ORCH_ENDPOINT_STATE | ~/.hscc | yes | yes |
| **hscc.PROFILES_DIR** | ~/.hermes/profiles | **yes** | **was NO → now YES (this audit)** |
| **serving.SERVING_JSON / CLUSTER_JSON** | ~/.hscc | read+write | **was NO → now YES (this audit)** |
| **serving.PROFILES_DIR** | ~/.hermes/profiles | **yes** | **was NO → now YES (this audit)** |
| **serving.ORCH_ENDPOINT_STATE** | ~/.hscc/orch-endpoint | yes | **was NO → now YES (this audit)** |
| api_server.DEFAULT_HSCC_DIR | ~/.hscc | yes | yes (hscc-api conftest) |

The three rows marked "this audit" were a genuine latent seam: the autouse
`_isolate_hscc._module_attrs()` did not redirect
`serving.PROFILES_DIR`/`serving.ORCH_ENDPOINT_STATE`/`serving.SERVING_JSON`/
`serving.CLUSTER_JSON` nor `hscc.PROFILES_DIR`. Those constants are baked at
import time (NOT re-evaluated by the runtime `os.path.expanduser` redirect), and
`serving.update_orchestrator_followers()` actively REWRITES `config.yaml` files
under `~/.hermes/profiles`. It was only safe because every test that reaches
that code path patches the constants manually (e.g. `TestUpdateOrchestratorFollowers`).
Closed as defense-in-depth.

Other `$HOME` constants found were either read-only or exercised only through
tests that patch them, and all were empirically unchanged across the run
(`health._WORKER_RELATCH_FILE`, `health._HERMES_CONFIG_YAML`,
`api_cli.API_PID_FILE/API_LOG_FILE`, `cluster_template.HSCC_DIR/HERMES_HOME`,
`workflow._BLOCKED_LOG/_COMPLETION_LOG/_TOOL_EVENT_LOG`,
`profile_status.DEFAULT_KANBAN_DB`, `telegram.ENV_FILE`, `hscc-bootstrap`
hooks constants).

### Function-local imports

The dangerous pattern is `from pkg.mod import CONSTANT_PATH` inside a function
body: it copies the *value* at call time, so patching `pkg.mod.CONSTANT_PATH`
later does not intercept it.

Audit result: **no instance** of this pattern in the test tree. Function-local
imports in tests import *modules* (`from hscc_daemon import serving`) or
*functions* (`from .telegram import notify_operations`), which resolve to the
same `sys.modules` objects whose attributes the autouse fixtures patch — so
patched values ARE seen at call time. Function-local imports in production
source copy config/timing constants (VLLM_*, PERIODIC_INTERVALS,
DISPATCHER_RECOVER_CHECK_INTERVAL), none of which are `$HOME` path constants.

## Changes made

1. `hscc_daemon/tests/conftest.py` — added `serving` to the `_module_attrs()`
   import and redirected `serving.SERVING_JSON`, `serving.CLUSTER_JSON`,
   `serving.PROFILES_DIR`, `serving.ORCH_ENDPOINT_STATE`, and
   `hscc.PROFILES_DIR` to the per-test tmp root. Closes the write-capable seam
   at the source (defense-in-depth; tests that patch themselves still win via
   LIFO teardown). Full hscc_daemon suite: 1004 passed.

2. `hscc_daemon/tests/test_no_live_hscc_leak.py` — extended the existing
   sandbox guard to plant `serving.json` + two managed profile `config.yaml`
   files (one orchestrator-tracking, one worker-pinned) and to run
   `test_serving.py` + `test_serving_extra.py` inside the sandbox. A regression
   that stops patching `serving.PROFILES_DIR` / `update_orchestrator_followers()`
   now trips the guard's manifest diff.

3. `scripts/hscc_hermeticity_snapshot.py` — new snapshot tool for the
   empirical proof (sha256 + content of every target file), reusable for
   future before/after hermeticity checks.

## Deliverables

The existing `test_no_live_hscc_leak.py` guard (now extended) is the automated
guard the card asked for: it fails CI if a suite run mutates operator state,
using a deterministic sandboxed-HOME model that cannot false-positive on the
live daemon's legitimate rewrites. The empirical snapshot script provides the
authoritative whole-suite proof, run against the real `~/.hscc` now and
reproducible at any point.
