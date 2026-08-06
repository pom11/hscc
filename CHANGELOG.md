# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.5.0] — DSV4 templates + multiplexing secret-scope wedge fixes

### Added
- **DeepSeek-V4 FP8 multi-node templates.** `deepseek-v4-orchestrator`,
  `dual-dsv4`, and `dsv4-plus-coding` (4-node FP8) added to the template set for
  serving DSV4-Flash on the orchestrator with tp>1 spanning multiple nodes.

### Fixed
- **Multiplexing secret-scope wedge (auxiliary LLM tasks).** Compaction and the
  text orchestration auxiliaries (`kanban_decomposer`, `triage_specifier`,
  `profile_describer`, `curator`, `title_generation`, `skills_hub`, `approval`,
  `mcp`) were left at Hermes' default `provider: auto`. Under multiplexing, `auto`
  falls through the auxiliary-client chain to a cloud provider whose credential
  read runs with no active secret scope and **throws** — silently breaking the
  task (e.g. `kanban_decomposer` could not decompose any card, and long sessions
  reported "max compression attempts (3) reached"). `enable_plugins.py` now routes
  these to the local orchestrator model via `_ensure_text_auxiliaries()`
  (`provider` auto→custom, fill-empty so operator choices survive). `vision` and
  `web_extract` are intentionally excluded.
- **Stale orchestrator-model defaults that 404 → cloud fall-through.**
  `ORCH_MODEL` (`enable_plugins.py`) and `COMPACT_MODEL`/`STRONG_MODEL`
  (`hscc-roles/generator.py`) defaulted to `nvidia/Qwen3.6-35B-A3B-NVFP4` /
  the placeholder `orchestrator-model` — ids the orchestrator node no longer
  serves. Every compaction/strong-tier call 404'd and hit the same cloud
  fall-through. Defaults now point at the served `deepseek-ai/DeepSeek-V4-Flash-0731`;
  `COMPACT_MODEL` follows `STRONG_MODEL` (one orchestrator-model knob).
- **Cluster provisioning:** worker model-id rewire on a worker-tier switch, stop a
  stale wrong-model container on a reused node before provisioning, wire
  `model.default` for the orchestrator in `apply` + raise the provision timeout,
  and make `tp>1` units span multiple nodes in the resolver.

## [1.4.0] — Native sparkrun proxy + verified runtime v2026.7.30

### Changed
- **Proxy management now uses native sparkrun proxy (0.3+).** `hscc-cluster/cluster_template.py`
  drives `sparkrun proxy start/stop` instead of hand-generating a LiteLLM launchd plist.
  Functions renamed to `install_proxy`/`remove_proxy`; the old names
  `install_proxy_plist`/`remove_proxy_plist` remain as backward-compat aliases.
  `DEFAULT_PROXY_PORT` stays 4000.
- **Verified runtime bumped:** hermes-agent v2026.7.20 → v2026.7.30 (0.19.1), sparkrun
  v0.2.40 → v0.3.1 in `hscc-bootstrap/runtime-versions.json`. Compatibility audit
  committed as `docs/COMPAT-7.30.md`: upstream 7.30 still lacks the kanban review-flow,
  so the pom11 fork + `patches/hermes/0001-0006` are kept (nothing dropped). Plugin
  lifecycle hooks and kanban_db API are unchanged — the bump is API-compatible for HSCC
  plugins.

### Added
- **Compatibility audit report** (`docs/COMPAT-7.30.md`): structured assessment of hermes
  upstream v2026.7.30 against HSCC's fork, confirming which patches remain required and
  verifying plugin/API compatibility.

### Fixed
- README topology table columns corrected for accuracy.
- README refreshed for the v1.1–v1.3 feature set with sharper landing copy.

## [1.3.0] — Escalation automation on + auto-unblock repair

### Added
- **Failure-escalation watcher, wired to Hermes cron** (`scripts/escalate_watcher_run.py`
  + `scripts/install_escalate_watcher.sh`). Runs every 15 min under
  `--no-agent --deliver telegram`: reassigns repeatedly-failing tasks to the
  strong tier, and when the strong tier also fails, posts a human-attention
  alert to the Telegram group. Silent when idle; the human alert is deduped
  across runs via `~/.hscc/escalated.json` so a stuck task is not re-announced
  every tick. This turns on the previously opt-in "acting" escalation path
  (autoscale remains advisory).
- **`scripts/run_tests.sh`** — runs the suite one pytest process per plugin dir
  (the six plugins are independent, some in non-importable hyphenated dirs, so a
  single process cross-contaminates via `sys.path`/`sys.modules`). Plus a root
  `pytest.ini` that keeps `.worktrees`/`_archive` out of collection.

### Fixed
- **Auto-unblock was silently dead since the 0.19 kanban upgrade.**
  `hscc-cluster/workflow.py` (`_try_auto_unblock`, `on_kanban_task_claimed`)
  called kanban_db APIs that were removed or reshaped in 0.19 — `get_comments`,
  `update_task`, `clear_lock`, and dict-style access on the now-dataclass
  `Task`/`Comment`. Every call raised and was swallowed by the best-effort
  `except`, so blocked cards whose dependencies had completed never got
  unblocked. Switched to `list_comments`/`unblock_task`/`reclaim_task`, added a
  dataclass/dict accessor shim, and let the claim hook honor an explicit repo.
- Test-suite green: repaired stale tests (comment API, dataclass field access,
  connection lifecycle, and a mock-identity bug where patching the `sys.modules`
  entry missed a `from package import module` reference).

## [1.2.1] — Correctness audit fixes

A full adversarial audit of the v1.0–v1.2 code surfaced a set of latent
correctness bugs — mostly silent failures that reported success while doing
nothing, and one safety-gate weakness. None affected the running cluster (the
escalation-watcher paths are opt-in and not enabled), but all are fixed here.

### Fixed
- **Safety gate** — `apply_template` (whole-fleet reprovision) no longer trusts
  `bool(args["confirm"])`, which treated the string `"false"` as truthy and
  could execute without real confirmation. Now requires `confirm is True`.
- **Silent success on failure** — `install_triggers` now reports `ok=False` +
  `error` (and exits non-zero) when the triggers file cannot be written,
  instead of printing a green checkmark over a no-op.
- **verify smoke-test false green** — `check_multiplex` returns `ok=None`
  ("unverified") rather than `ok=True` when multiplex is enabled but gateway
  state is missing/unreadable, and `check_daemon_streams` surfaces an
  unparseable timestamp instead of silently treating the stream as fresh.
- **stats crash** — `_read_jsonl` skips non-dict JSON lines instead of raising
  `AttributeError` and aborting the whole aggregation; `hscc stats` rejects a
  negative `--days` (which had silently returned an empty result).
- **escalate-watcher** — de-duplicates the "needs human" notification so a
  stuck task is not re-notified every tick, and records an `escalate_failed`
  action when the reassign subprocess fails instead of reporting success.
- **classify_failure** — the tooling check runs before the broad `"failed"`
  keyword so import errors are categorized as `tooling`, not `test-failure`.
- **fleet agent tools** — `_daemon_mod` restores `sys.path` via `try/finally`
  (success and error paths), removing only the entry it inserted.
- **dep-update checker** — compares versions numerically (no more downgrade
  PRs from date-latest backport tags) and fails the workflow when an upstream
  release tag cannot be fetched, instead of silently staying green.
- **hscc-roles generate** — emits valid JSON instead of a Python dict repr.

## [1.2.0] — Agent tool parity: the orchestrator can manage the fleet directly

The v1.1.0 fleet capabilities were operator-facing (CLI) only. This registers
them as `hscc-cluster` agent tools so the Hermes orchestrator can call them
itself — closing the last gap between what the operator can do and what the
agent can do.

### Added
- **Fleet read tools** (`hscc-cluster/fleet.py`): `cluster_verify` (full
  smoke-test), `cluster_throughput` (vLLM tokens + queue depth), `fleet_stats`
  (completions + tool activity), `autoscale_advice` (advisory scale decision).
  Thin, best-effort wrappers over the merged daemon modules (path-robust lazy
  import).
- **Template tools** (`hscc-cluster/template_tools.py`): `list_templates`,
  `preview_template` (dry-run), and `apply_template` (reshapes the whole fleet —
  HIGH-RISK, confirm-gated: without `confirm=true` it returns a preview). The
  agent can now inspect and reshape the fleet, not just single models.

With these, the orchestrator's tool surface reaches parity with the operator:
read/diagnose, provision/restart/stop, heal, verify, observe throughput, get
scaling advice, and apply whole-fleet templates.

## [1.1.0] — Fleet operations: verify, alerting, throughput, escalation, autoscale

A batch of operator + self-management features, each built as a small module by
the cluster itself (sub-atomic kanban cards, human-reviewed). 936 tests green.

### Added
- **`hscc verify`** — one-command smoke test of the whole stack: plugins
  registered, multiplex profiles served, daemon streams fresh, proxy serving,
  config wiring. Turns the manual post-upgrade checklist into a gate.
- **Alerting is live** — the trigger engine shipped with zero rules (inert);
  now seeded with default rules (orchestrator/DGX down, vLLM down, watchdog
  blocked) via an idempotent `install_triggers.py`, wired into bootstrap.
- **`hscc stats`** — fleet analytics: completions + tool activity per profile /
  per day, aggregated from the jsonl logs.
- **`hscc throughput`** — vLLM token throughput + per-node queue depth across
  the fleet (the meaningful "cost"/utilization view for local models).
- **Failure escalation** — `escalate.py` (decision logic) + `escalate_watcher.py`
  (applies it): a card that fails `fail_limit` times is reassigned to the strong
  tier, or flagged for a human if the strong tier is also failing.
- **Autoscale decision logic** (`autoscale.py`) — scale up on queue backlog,
  down when fully idle, from the throughput signal (read-only `hscc autoscale`
  view; the acting layer is opt-in).
- **`hscc escalate`** — dry-run view of which failing cards would be escalated.
- **Per-role model override** — role specs may set `model_endpoint`/`model_name`
  to point a role at a specialized model, superseding the fast/strong tier
  (fully backward-compatible).
- **`hscc-lessons` skill** — the fleet's hard-won engineering conventions
  (sub-atomic cards, verify-not-narrative, ports-from-serving, best-effort IO,
  preserve-operator-config, no `stop --all`, argv[2], test + verify live).

### Fixed
- CLI help asserts a version pattern, not a pinned literal (survives bumps).

## [1.0.2] — Automated runtime-dependency update loop

Keeps the cluster's runtime dependencies (hermes-agent, sparkrun) current with a
human-gated, cluster-verified loop.

### Added
- **`check-runtime-deps` GitHub Action** (`.github/workflows/`, daily): compares
  each upstream repo's latest release to `hscc-bootstrap/runtime-versions.json`
  and, on a bump, opens/updates a PR — label `needs-cluster-check`, repo owner as
  reviewer — with a cluster-verification checklist. Stdlib-only checker;
  injection-safe (dynamic values passed via env, never inlined into the shell).
- **`scripts/dep_pr_watcher.py`** + **`scripts/install_dep_watcher.sh`**: a
  Hermes-cron watcher (daily, `--no-agent`) that turns those PRs into idempotent
  kanban verification cards for the workers (one card per PR, deduped by
  `idempotency_key`). Silent when idle. The installer registers the job via
  Hermes' native cron (no external launchd/plumbing).

The loop: Action detects a release -> opens PR (owner reviews) -> Hermes cron
creates a kanban card -> a worker upgrades + bootstraps + tests + reports -> the
human merges the version-lock bump. GitHub cloud and the LAN cluster never need a
direct network path.

## [1.0.1] — Hermes 0.19 compatibility + post-update reconcile

Keeps HSCC working across a Hermes runtime upgrade (validated live on a
0.17 → v0.19.0 jump) and makes the fixes survive future `hermes update`s.
All test suites green.

### Added
- **`strip_worker_telegram.py`** (wired into `bootstrap.sh`): `hermes update`
  seeds the default profile's `.env` — including the active Telegram bot token —
  into every role profile. On 0.19+ the multiplex gateway refuses to poll one
  bot token from multiple profiles and logs ~24 errors per restart. This step
  idempotently comments out the seeded `TELEGRAM_*` vars in every non-`default`
  profile (reversible; only the `default` chat front-end keeps telegram).
- **`ensure_review_feature.py`** (wired into `bootstrap.sh`): the
  `kanban_submit_review` / `auto_review` feature (upstream PR #43425) is carried
  as a local commit on the runtime until it merges upstream, so a `hermes
  update` can drop it. This step re-applies it idempotently — presence-checked
  (no-op when already installed), and safe (refuses a dirty/mid-operation repo,
  aborts cleanly on conflict, validates the git ref against an allowlist so an
  env-set remote/branch can't smuggle a git option). No-op once the PR lands
  upstream. Run `hscc-bootstrap/bootstrap.sh` after any Hermes update to restore
  both the clean telegram setup and the review wiring.

### Fixed
- **Bootstrap status parse**: `python -c CODE _ JSON` puts the JSON at
  `sys.argv[2]` (argv[1] is the placeholder), so the reconcile steps
  misreported (telegram count stuck at 0; the review step falsely warned
  "unknown" when the feature was present). Read `argv[2]`.

## [1.0.0] — First stable release

HSCC leaves beta: the fleet self-heals for real, the daemon sees everything
it is supposed to see, one `hscc` command exposes the whole system, and the
operator surface covers the day-to-day worker lifecycle. 735 tests green
across the six components.

### Hardened (pre-release correctness audit)
A full-repo audit of all six components turned up ~26 real bugs (no live
symptom under the current single-model-per-node layout, but exposed on the
co-located templates, the operator commands, and any config/file corruption).
All fixed:
- **Daemon startup crash**: a valid-JSON-but-non-dict `cluster.json` (`[]`,
  `null`) raised `AttributeError` in `resolve_cluster_config` at import,
  killing the whole daemon; now guarded like `load_serving`.
- **Relaunch grace window now survives restarts**: it was in-memory only, so
  a daemon restart discarded it and immediately stop+relaunched a still-loading
  worker (crash-loop). Persisted (wall-clock) to `~/.hscc/worker_relaunch.json`.
- **Operator restart commands no longer over-kill or mis-port**: `restart_one`
  used `sparkrun stop --all` (killing co-located siblings) and hardcoded port
  8000; it now stops only the unit's recipe and uses the unit's real port.
  Fixes `/heal`, `/orch-restart`, `/cluster-restart`, `/workers-up`.
- **Template VRAM fit-check now actually runs**: the per-node free-VRAM
  overflow guard never fired (resolve never probed), so co-located templates
  could overcommit; preview/validate/apply now probe with graceful fallback.
- **`doctor --fix` reconciles config unconditionally** (was gated on an infra
  check also failing, so it never fixed drift on a healthy system).
- **No more silent-success / masked-failure**: template `apply` reports failure
  when provisioning warns; `reap_orphans` checks the stop result; template
  stop-loop records failures instead of swallowing them; `hscc check watchdog`
  reports degraded on a partial failure; a corrupt `gateway_state.json` fails
  the gateway check instead of passing.
- **Config-intent preserved**: `_ensure_bitwarden` no longer flips an explicit
  `enabled: false`; compaction no longer lowers a higher operator timeout;
  unexpected-shape hook/auto_review/fallback values are preserved with a warning
  instead of being clobbered.
- **Kanban auto-unblock** only promotes a blocked task when *every* referenced
  dependency is `done` (exact `t_…` id match, not a substring).
- **Discovery / node matching**: probes the node's actual served ports (not just
  8000); node-IP matching is exact (`10.0.0.1` no longer matches `10.0.0.10`).
- **Role regen resilience**: one bad spec no longer aborts the whole batch;
  `model_tier: null` defaults to `fast`; a non-UTF-8 profile is overwritten
  rather than crashing.

### Added
- **One unified `hscc` CLI** (`hscc_daemon/hscc.py`): the two separate CLIs
  are merged. `hscc` now owns daemon control **and** cluster ops **and** the
  cluster templates — `hscc template list|status|preview|validate|apply`,
  `hscc cluster status|hosts|monitor|jobs|info|stop`, `hscc profiles`. Before,
  the 11 cluster templates lived only in a separate `hscc-cluster` CLI that
  was never installed on PATH, so they were invisible from a terminal. The
  cluster engine stays in the `hscc-cluster` plugin (Hermes loads it as a
  toolset); the merged CLI imports it directly as a library — no sub-process,
  no argv rewriting. Rich grouped `--help` with a templates section and
  worked examples, plus `hscc help <command>` and `hscc help advanced`. The
  old `hscc-cluster` entry prints a deprecation note and still works.

### Fixed
- **Bootstrap no longer poisons its own runtime dir**
  (`hscc-bootstrap/install_payload.py`): plugin backups were written as
  `<name>.bak-<ts>` INSIDE `~/.hermes/plugins/` — the directory Hermes
  scans — so every backup kept a `plugin.yaml` under the same plugin name
  and a stale copy could shadow the freshly-installed plugin (observed
  live: `/workers-up` returned "unknown command" because an old
  `hscc-commands.bak-*` won manifest resolution). Backups now go to a
  sibling `~/.hermes/plugins-backups/`, and each install first sweeps any
  pre-existing `*.bak-*` entries out of the scanned dir.
- **Worker self-heal actually works** (`hscc_daemon/health.py`): relaunches
  were killed after 180 s by the subprocess timeout — weight staging alone
  takes 5–10+ min, so self-heal NEVER succeeded and failed silently (the
  error output was discarded). Relaunches are now detached
  (`subprocess.Popen`, `start_new_session=True`) with output captured in
  `~/.hscc/relaunch-<node>-<port>.log`; a launch that fails to spawn counts
  the worker down instead of "relaunched"; stream ok no longer masks
  failures (`ok = not down`); load-grace default raised 5 → 20 min to match
  staging + vLLM start reality.
- **Stale command-registration test** updated for the full 12-command
  surface.

### Added
- **Gateway check sees multiplexed profiles** (`check_gateway()`): reads
  `multiplex_profiles` from config.yaml and `served_profiles` from
  `gateway_state.json`, compares against the profile roster on disk, and
  fails the stream naming the missing profiles when multiplex is on but
  profiles are unserved. This is the check that would have caught the
  silent-multiplex-off condition fixed in beta.12.
- **`/workers-up` operator command**: brings up only the DOWN keep-alive
  workers from serving.json — health-checks each unit on its own port,
  never touches the orchestrator, no confirm needed (non-destructive).
  Fills the gap between `/cluster` (status) and `/cluster-restart`
  (full template re-apply incl. orchestrator reload).

### Changed
- **Local docker/ollama check is informational by default**
  (`check_local()`): a gateway host without docker/ollama no longer sits
  in permanent FAIL. Set `HSCC_LOCAL_REQUIRE=docker,ollama` to make their
  absence a failure again; requiring a service the check does not track
  counts as missing rather than silently passing.
- `VERSION` now tracks releases (was stuck at beta.7).

## [1.0.0-beta.12] — Fix: multiplexing never activated (top-level config key)

### Fixed
- **`_ensure_multiplex` wrote a key the gateway never reads** (beta.11 bug):
  Hermes 0.17's `gateway/config.py::load_gateway_config` maps only the
  TOP-LEVEL `multiplex_profiles` key from `~/.hermes/config.yaml`; the nested
  `gateway.multiplex_profiles` that beta.11 wrote never reached
  `GatewayConfig.from_dict` (the nested fallback fires only for the legacy
  `gateway.json` path). Result: multiplexing silently stayed off —
  `served_profiles` absent from `gateway_state.json` and every non-default
  profile unserved, even after beta.11. Found live 2026-07-21: bootstrap to
  beta.11 + gateway restart still produced zero multiplexed profiles until
  the top-level key was added by hand.
- **Fix:** `_ensure_multiplex` now writes BOTH forms (top-level for the 0.17
  loader, nested for forward compat), filling each form only when absent.
  Filling the top-level key when only the nested one is present is the point:
  every install bootstrapped on beta.11 sits in exactly that broken state,
  and both bootstrap and `doctor --fix` must be able to repair it.
  Cross-form disable guard: an explicit `false` in EITHER form bails the
  whole write — a deliberate disable is never overridden through the other
  form. A `gateway:` value that is not a mapping is left untouched (only the
  top-level key is written then), and no phantom "changed" entries are
  reported for writes that never landed.

### Changed
- Tests: rewrote `test_doctor.py` (cleaned up broken fixtures, replaced
  invalid `monkeypatch.setattr(sys, ...)` / `__import__` hacks with proper
  `patch` / `monkeypatch.setattr`); 147 bootstrap tests pass.

## [1.0.0-beta.11] — Phase 2: gateway multiplexing + per-profile observability

### Added
- **Gateway multiplexing** (`enable_plugins._ensure_multiplex`): sets
  `gateway.multiplex_profiles: true` so one gateway serves multiple profiles
  with per-profile session isolation. Only fills when absent — an operator who
  set it to `false` keeps that choice.
- **Per-profile observability** (`hscc-cluster`): a `pre_tool_call` hook
  (`workflow.on_pre_tool_call`) stamps `profile_name` on every tool call into
  `~/.hscc/tool_events.jsonl` (cheap single append), and the kanban resume note
  now records the claiming profile. Lets us correlate which profile drove which
  activity across the fleet.

## [1.0.0-beta.10] — Phase 2: doctor --fix, per-profile status, model tiering

### Added
- **`doctor.py --fix` mode**: idempotently reconciles kanban caps + routing/
  auto_review wiring via `enable_plugins.enable()` (preserves operator-set lower
  caps), and reports drift ("was X → set Y") for each corrected key.
- **Per-profile task status** (`hscc-cluster/profile_status.py` +
  `hscc profile-status`): running kanban task counts per profile (assignee),
  read from `~/.hermes/kanban.db`, degrading gracefully on a missing db/schema.
- **Model tiering per role** (`model_tier: fast | strong`): `strong` routes a
  role to the orchestrator GPU (`.244:8000`, 35B-A3B); `fast` (default) keeps
  the worker proxy (`:4000`, 27B). Only `architect` (+ orchestrator) default to
  `strong` — reviewers/coders/QA stay `fast` so the orchestrator node (which
  also runs orchestration + worker-compaction) is not saturated. Endpoints are
  env-overridable; `rolelib` validates the tier and fails loud on bad values.

## [1.0.0-beta.9] — Profile integration Phase 1 (routing + native API + caps fix)

Make HSCC's 24 role profiles actually usable by the kanban decomposer, adopt
the Hermes 0.17 native profile API, and kill the recurring concurrency-caps
footgun.

### Fixed
- **Concurrency caps footgun** (`enable_plugins.py`): `max_in_progress` /
  `max_in_progress_per_profile` were RAISED toward the HSCC defaults (30/10)
  whenever the current value was lower, so an operator-set 6/2 got clobbered
  on every bootstrap. Now mirrors the `failure_limit` pattern — a lower
  operator value is deliberate (STRICTER) and preserved; caps are only filled
  when absent/invalid. Ends the recurring hand-fix.

### Added
- **Discriminative routing descriptions for all 24 roles** (`hscc-roles`): each
  role spec carries a `routing_description` ("Claim tasks that <X>; do NOT claim
  <Y>") that the generator writes verbatim to `profile.yaml`. The kanban
  decomposer matches tasks against these, so work routes to the right
  specialist instead of collapsing onto the `worker` catch-all.
  `routing_description` is now a required role-spec field.

### Changed
- **Generator uses the Hermes 0.17 native profile API** (`hscc-roles/generator.py`):
  `create_profile()` scaffolds the profile dir and `write_profile_meta()` writes
  the descriptor, with a graceful fallback (`USE_NATIVE_API`) when `hermes_cli`
  is unavailable. HSCC-specific `config.yaml` (worker model block → proxy
  `:4000`, compaction routing, toolsets) is still written manually since the
  native API has no cluster-topology concept. Generation stays idempotent
  (verified: second run returns `changed=False`, byte-identical profile dir,
  worker `base_url` = `http://localhost:4000/v1`). Tests now set `HERMES_HOME`
  so the native path is exercised under venv python and isolated from the real
  `~/.hermes`.

## [1.0.0-beta.8] — Retire sparkrun patches that landed upstream

The two curated `patches/sparkrun/` patches are now merged into official
sparkrun, so the local delta is empty. Carrying them made
`apply_patches.py --check` fail and `bootstrap.sh` emit a misleading warning
on every run. Verified byte-identical to upstream before removal.

### Changed
- **Retired `patches/sparkrun/0001` and `0002`.** Confirmed byte-equivalent
  to upstream commits `9e4513f` (default restart policy → `unless-stopped`)
  and `37a7bdb` (OpenClaw 2026.5.19 plugin compat). The sparkrun patch set
  is now empty; recipe overlay (`~/.sparkrun-local/recipes`) is unaffected.
- **`apply_patches.py`** returns `ok: true` for an intentionally empty patch
  set (patches landed upstream) instead of an error, so `bootstrap.sh` stays
  quiet. `patches/MANIFEST.md` updated to record the upstream landing.

### Notes
- sparkrun runtime is an editable uv-tool install from `~/sparkrun`
  (`v0.2.38`+3 upstream commits). All CLI commands `hscc-cluster` calls
  (`cluster list --json`, `run`, `stop`, `status`, `proxy`) verified present
  — command surface fully compatible; no functional sparkrun upgrade needed.
- Tests: 117/117 bootstrap suite pass.

## [1.0.0-beta.7] — New kanban lifecycle hooks (blocked + completed)

Add handlers for hermes-agent 0.17's two new kanban lifecycle hooks:
`kanban_task_blocked` and `kanban_task_completed`, wired into the
`hscc-cluster` toolset alongside the existing `kanban_task_claimed`.

### Added (hscc-cluster)
- **`kanban_task_blocked` handler** (`workflow.py:on_kanban_task_blocked`):
  registers via `ctx.register_hook("kanban_task_blocked", ...)`. On fire,
  posts a concise alert to the HSCC ops Telegram topic (reuses the
  existing `hscc_daemon.telegram.notify_operations` path via a lightweight
  `_telegram_compat` shim) **with the typed `reason` field from 0.17**,
  and appends a JSON line to `~/.hscc/blocked_tasks.jsonl` for dashboard
  status reading. Best-effort: never raises.
- **`kanban_task_completed` handler** (`workflow.py:on_kanban_task_completed`):
  registers via `ctx.register_hook("kanban_task_completed", ...)`. On fire,
  appends a JSON line to `~/.hscc/task_completions.jsonl` (task_id,
  profile_name, summary, timestamp) for HSCC task metrics. Also scans
  blocked tasks for dependency references and best-effort auto-unblocks
  any blocked task whose block comments reference this completed task
  via `_try_auto_unblock()`. If HSCC has no dependency mechanism, this
  silently does nothing — does NOT build a new dependency system.
- **`_telegram_compat.py`** — shim module that wraps
  `hscc_daemon.telegram.notify_operations` for hscc-cluster plugin use.
  Best-effort: if the daemon package is not importable, provides a no-op.
  Never raises.

### Changed (hscc-cluster)
- `__init__.py:register()` now registers all three hooks (`kanban_task_claimed`,
  `kanban_task_blocked`, `kanban_task_completed`) with best-effort guards.

### Versioning
- Bumped VERSION from `1.0.0-beta.6` → `1.0.0-beta.7`.

---

## [1.0.0-beta.6] — Hermes-agent 0.17 compatibility

Update HSCC to be compatible with upstream hermes-agent 0.17.0 (commit
`885e80df7`), which renamed the kanban re-dispatch hook from
`pre_kanban_dispatch` → `kanban_task_claimed` and changed the kwargs
passed to hook handlers.

### Changed (hscc-cluster)
- `workflow.py:on_pre_kanban_dispatch` → `on_kanban_task_claimed`: renamed
  hook handler to match the upstream hook name. Adapts kwarg handling:
  the upstream hook no longer passes the full task dict or a pre-opened
  `conn`; instead HSCC fetches them via `kanban_db.connect()` +
  `kanban_db.get_task()` (pure stdlib, no extra deps).

---

## [1.0.0-beta.5] — Proxy-plist respawn-storm hardening

Hardens generated LiteLLM proxy launchd plists so a missing/failing binary can
no longer crash-loop into a memory-exhausting respawn storm. On 2026-06-17 a
proxy plist invoked a bare `litellm` (absent from launchd's minimal PATH →
`posix_spawn` error 0x2) under an always-on `KeepAlive`, respawning every ~10s;
re-applying the template 3× compounded it. (The host watchdog panic that day was
ultimately caused by an unrelated 6-wide dataset job, but the proxy storm was a
real latent fault.)

### Changed (hscc-cluster)
- `_generate_proxy_plist` now launches via `/bin/sh -c` and **resolves litellm
  at launch** instead of freezing a machine-specific path into the plist:
  `$LITELLM_BIN` override wins, else `command -v litellm`, else a glob of the
  usual conda/uv install locations (`exec` so launchd supervises litellm
  directly). It also uses a **crash-only** `KeepAlive` (`{SuccessfulExit:
  false}`) instead of bare `true`, and adds a `ThrottleInterval` of 30s so a
  persistently-failing binary backs off rather than respawning every ~10s.


## [1.0.0-beta.4] — Gateway-host-aware cluster-prune

Fixes false assumption that orchestrator reboot kills the gateway. Gateway
typically runs off-cluster (Mac host), so /cluster-prune can complete the
full chain including the post-reboot template reapply.

### Changed (hscc-commands)
- `/cluster-prune` no longer skips the final `/cluster-restart` after a
  chained reboot. New behavior: detect gateway location at runtime via
  `gateway_on_cluster()`; if off-cluster (the common case), wait for SSH
  to return on every node (up to 4 min), then re-apply the template.
  On-cluster gateway falls back to the old skip+advise path.
- `/cluster-reboot` confirm preview now states accurately whether the
  gateway will survive the reboot.

### Added (cmdlib)
- `_local_ips()` — IPv4 set from hostname + `ifconfig`/`ip addr` fallback.
- `gateway_runs_on_node(ip)` / `gateway_on_cluster()` — runtime detection.


## [1.0.0-beta.3] — Cluster lifecycle slash commands

Adds 5 new operator slash commands to `hscc-commands` covering full cluster
recycle. Triggered by a 16% swap-pressure incident on `.247` (2026-06-17)
that `/cluster-restart` couldn't clear because re-applying the template only
reloads vLLM models — the container/host memory state persisted across the
template apply. The new commands give graduated recovery without always
needing a kernel-level reboot.

### Added (hscc-commands)
- **`/cluster-down`** — confirm-first; parallel `sparkrun stop --all` per
  node. Hosts stay up; vLLM containers go away.
- **`/cluster-docker-prune`** — confirm-first; parallel `docker system
  prune -af` (no volumes — model cache safe). Reports per-node reclaimed
  space. Best run after `/cluster-down`.
- **`/cluster-reboot`** — confirm-first; SSH `shutdown -r now` on workers in
  parallel, orchestrator last with 5s delay so the confirmation message
  reaches Telegram before the gateway dies.
- **`/cluster-apt-upgrade`** — confirm-first; sequential
  `apt-get update && apt-get -y upgrade` per node (dpkg-lock safe). Detects
  `/var/run/reboot-required` and auto-chains into `/cluster-reboot`.
- **`/cluster-prune`** — macro: `/cluster-down` → `/cluster-docker-prune` →
  `/cluster-apt-upgrade` → `/cluster-restart`. One confirm runs the chain.
  Skips the final `/cluster-restart` if step 3 chained a reboot (gateway
  dies mid-reboot; vLLM relies on host-boot auto-start or a manual
  `/cluster-restart` once hosts return).

### Added (cmdlib)
- `ssh_exec(node, cmd, timeout)` — single-node SSH wrapper around `_run`.
- `ssh_exec_parallel(nodes, cmd, timeout)` — `ThreadPoolExecutor` fan-out.
- `wait_for_ssh_back(node, max_wait, probe_interval)` — poll SSH until host
  answers; for future reboot-completion gating.
- `REBOOT_REQUIRED_FILE` constant.

### Bootstrap
No changes needed — `install_payload.py` already ships the full
`hscc-commands/` directory; `enable_plugins.py` already enables the plugin.
Fresh-machine installs pick the new commands up automatically.

### Notes
- All commands are **confirm-first**: bare invocation shows a preview;
  re-run with `confirm` to execute.
- Gateway restart (or `launchctl kickstart -k`) is required after install
  for new commands to register, same as beta.2.

## [1.0.0-beta.2] — Operator watchdogs + script bootstrap

Reliability follow-up to beta.1. A real incident on 2026-06-17 surfaced a
gap: the sparkrun LiteLLM proxy (`localhost:4000`) had died days earlier
with a stale PID, and nothing restarted it — kanban worker dispatch was
silently degraded. This release ships pure-shell `--no-agent` watchdogs
that monitor the proxy, vLLM endpoints, NAS, and the cluster at large,
and wires them into bootstrap so a fresh-machine install reproduces them.

### Added
- **Operator watchdog scripts** (`scripts/hscc_*.sh`) — four no-LLM,
  silent-when-healthy shell probes registered via Hermes cron with
  `--no-agent`:
  - `hscc_proxy_watchdog.sh` (every 5m) — restarts the sparkrun proxy
    if `localhost:4000` is unreachable; covers the stale-PID regression.
  - `hscc_worker_health.sh` (every 10m) — checks all 4 vLLM endpoints
    plus per-host model-id match; reports drift.
  - `hscc_cluster_digest.sh` (every 2h) — summary message (containers,
    endpoint health, proxy state, uptimes) delivered to a chat target
    (e.g. Telegram HSCC channel).
  - `hscc_nas_watchdog.sh` (every 4h) — pings QNAP `.249`, probes the
    Mac `/Volumes/NAS` mount; falls back to NAS-export remediation docs.
- **`scripts/README.md`** — install (bootstrap or manual), per-script
  purpose, and the `hermes cron create` commands to register each job.
- **`hscc-bootstrap/install_scripts.py`** — installer parallel to
  `install_payload.py`. Copies `<repo>/scripts/hscc_*.sh` into
  `~/.hermes/scripts/` with backup-then-overwrite, preserves user-added
  scripts, re-applies the executable bit, respects `--no-backup`.
- **New bootstrap stage** — *Install: operator watchdog scripts* runs
  after the plugin-files stage. Non-fatal: a script-install failure
  warns instead of dying the install (watchdogs are operator convenience,
  not foundation).

### Fixed
- **Sparkrun proxy stays alive across crashes.** Before: stale PID =
  silent worker-dispatch outage. After: `hscc-proxy-watchdog` cron
  detects + restarts within 5m, no operator action required.

### Notes
- Cron jobs themselves are not auto-registered by bootstrap — install
  lays the script files, operators run the `hermes cron create` commands
  once per host. See `scripts/README.md`.
- 5 new tests in `hscc-bootstrap/tests/test_install_scripts.py`; full
  bootstrap suite 83/83 passing.

## [1.0.0-beta.1] — Topology-free orchestrator + hardening

First beta. The system is now feature-complete: a full in-depth audit was closed
and an 8-workstream effort turns HSCC from "runs commands" into a self-running,
**topology-free** orchestrator. ~497 tests pass across the four plugin suites,
and the work was exercised live on the cluster (subagent routing, kanban
dispatch, the review gate, and an engineered crash-and-resume).

Versioning switches to SemVer for the beta line (was CalVer `-alpha`).

### Added
- **Dynamic cluster discovery** (`discovery.py`) — one source of truth, live →
  cache → fail-loud (no silent fake-IP fallback). Tracks per-node VRAM, GPU
  model, and **power-draw idle detection** (the real GB10 signal, not util%);
  **auto-adopts** nodes added to the sparkrun cluster. New `discovery_status` +
  `nas_status` tools.
- **Topology-free cluster templates** (schema v2) — templates describe *intent*
  (recipes + family structure), never IPs or ports; those resolve from the live
  cluster at apply. **sparkrun-`show`-driven auto-fit** so a template only
  proposes layouts that actually fit (incl. **2 models co-located on one node**),
  with a node-count library for **1–8 nodes**, VRAM-verified.
- **HSCC identity** — a topology-free SOUL + ops personality named HSCC, with
  `~/dev` working-dir discipline and doc-driven/review-gate guidance.
- **New slash commands** — `/status` (live dashboard incl. free-VRAM),
  `/heal`, `/template`; `/cluster-restart` now re-applies the active template
  (template = the recovery contract).
- **Agentic work-flows** — an idempotent **resume probe** wired into dispatch via
  a new `pre_kanban_dispatch` hook (a re-dispatched worker is told what already
  landed on its branch, so it continues instead of redoing); native-kanban review
  gate with reviewer auto-pairing and 3-reject → escalate.
- **Reproducible install** — preflight `doctor`, atomic apply with
  snapshot/auto-rollback, and bootstrap now reproduces the full live state
  (applies the hermes/sparkrun patch set, wires compaction→worker proxy, seeds a
  fallback provider).
- **Per-directory READMEs** linked from the main README.

### Fixed
- Audit punch-list closed: daemon plist hardcoded python (respawn-loop on
  Homebrew-only/Spark hosts); compaction summarizing on the orchestrator
  (re-arming the freeze); broken `provision_model` invocation (no NAS cache);
  unbounded `.bak`/orphan-proxy cruft; world-readable config + a stray HF token;
  hardcoded IPs in a public repo; missing fallback provider; dead/contradictory
  daemon code; silent bootstrap failures.

### Changed
- The daemon keep-alive loop is **unit-keyed (node, port)** so co-located
  multi-model nodes are supervised independently; relaunch stops only the unit's
  own recipe (a healthy sibling survives).
- Healing is split: workers auto-heal; an orchestrator wedge alerts + activates
  the fallback and waits for a human `/cluster-restart` (template re-apply).
- Run official hermes/sparkrun; local edits are captured as a reapply-able patch
  set (`patches/`) instead of long-lived forks.

### Notes / known limits
- The patch-reapply stage is `--check`-gated: on a hermes version that has
  drifted from the patch base it warns + skips rather than half-applying (rebase
  the patch set for very different upstreams).
- vLLM-on-GPU behavior is logic-tested; a few paths are flagged for live-cluster
  validation.

## [2026.06.11-alpha] — Work runs on workers, not the orchestrator

The orchestrator was silently doing nearly all the work. This release routes the
fleet onto the worker GPU pool, makes the daemon actually self-heal, and bakes
the whole wiring into bootstrap so it survives a config rebuild.

### Fixed
- **Role work ran on the orchestrator.** All 22 role profiles (coder, architect,
  qa, …) had no model endpoint, so they inherited the gateway node. They now
  serve from the sparkrun LiteLLM proxy, which load-balances across every worker
  GPU. The orchestrator role stays on its own gateway model.
- **Catch-all kanban work piled onto one node.** The per-node worker-246/247/248
  profiles + `default_assignee=worker-246` funneled un-routed tasks to a single
  node. Collapsed into one proxy-balanced `worker` role; `default_assignee=worker`.
- **Control daemon didn't self-heal.** Check threads never started (`globals()`
  lookup of local imports); health checks probed placeholder IPs; the keep-alive
  worker check was a no-op; the watchdog latched BLOCKED forever. The daemon now
  runs all checks against real topology, relaunches crashed worker models, and
  backs off + auto-resumes instead of giving up.
- **Memory provider was down.** The BYODB provider failed to init every turn
  (camelCase config keys vs snake_case dataclass fields). Fixed with a
  casing-tolerant loader; memory works again.

### Added
- **Worker load-balancer**: a daemon stream keeps the sparkrun LiteLLM proxy alive
  so role workers + orchestrator subagents always reach the balanced worker pool.
- **Offline memory augmentation**: memory fact-extraction can run against a local
  OpenAI-compatible LLM (the cluster orchestrator) instead of the Memori cloud —
  fully offline, env-configurable.
- **Bootstrap wires fleet routing**: `kanban.default_assignee`, concurrency caps,
  and `delegation.base_url` are now set idempotently by bootstrap (only fills
  unset values / raises low caps — never clobbers operator choices), so the
  routing is reproducible and never a manual re-apply.

### Changed
- Cleaned stale HSCC skill docs (archived-plugin references → current reality).
- Removed dead `install/hscc-plugins` + `install/hscc-cli` staging copies; the
  `install/` README now describes only the live bundled-skills source.

## [2026.06.10-alpha] — Operator commands, sparkrun plugin, daemon repair

Incident-response tooling, the official Hermes sparkrun plugin, and a working
control daemon — plus bootstrap that reproduces the full live wiring.

### Added
- **Operator slash commands** (`hscc-commands`): `/cluster`, `/orch-restart`,
  `/cluster-restart`. They run directly in the gateway (not via the LLM), so
  they work even when the orchestrator model is wedged. Confirm-first.
- **Official Hermes sparkrun plugin** (`sparkrun-hermes`): a single guarded
  `sparkrun_exec` CLI passthrough plus the run/setup/registry skills. Mirrors
  the official OpenClaw plugin (no Hermes plugin existed before).
- **Bootstrap now wires the whole setup**: ensures the `sparkrun` + `hscc-cluster`
  toolsets (not just `plugins.enabled`), and installs a topology-free HSCC
  guidance block into `SOUL.md` + the `ops` personality via sentinel markers
  (idempotent, never clobbers user text).

### Fixed
- **Control daemon was non-functional after the package split.** Check threads
  never started (`globals()` lookup of locally-imported fns); health checks
  probed placeholder IPs (topology copied at import, before resolution);
  `load_serving` ignored runtime path changes. Daemon now runs all checks and
  reports real cluster health. Completed the `success`->`ok` return-contract
  migration across the split modules.

### Removed
- Archived unused plugins: `hscc-chat` (superseded by the native gateway),
  `hscc-optimizations` (dev-time detector), `hscc-provision` (redundant —
  `provision_model` lives in the `hscc-cluster` toolset). Dropped stale
  `config.example.yaml` + `cluster-config/` snapshots.

## [2026.06.09.1-alpha] — Linux compatibility

The control daemon now runs on Linux, not just macOS. The agent fleet was
always Linux (Spark nodes); this closes the gaps in the host-side daemon.

### Added
- **systemd --user service** as the Linux auto-start mechanism, mirroring the
  macOS launchd plist. `install`/`uninstall`/`plist` now dispatch by platform,
  with a plain backgrounded process as a last-resort fallback.
- `hscc_daemon/systemd-setup.sh` — Linux counterpart to `launchd-setup.sh`
  (installs the unit, enables linger, verifies status).
- Linux desktop notifications via `notify-send` (libnotify), alongside macOS
  osascript; both fall back to `~/.hscc/notifications.json` when headless.

### Changed
- Gateway liveness probe is platform-aware (launchd / `systemctl --user` /
  process match) instead of launchctl-only — fixes the gateway always reading
  "down" on Linux.
- Host system info detects the OS (reads `/etc/os-release` on Linux) instead of
  hard-coding macOS.
- Bootstrap scripts pick launchd vs systemd by `uname`; daemon log path in the
  config template moved from `~/Library/Logs/` to `~/.hscc/`.

## [2026.06.09-alpha] — Final Alpha

Major refactor to native-Hermes-first + a specialized autonomous agent fleet.

### Added
- **Role framework** (`hscc-roles`): roles are spec files generated into Hermes profiles with layered SOULs (base character + role disposition); full toolset minus cluster control. 22 starter roles; new roles minted on demand via `create`.
- **Reviewer loop**: `kanban_submit_review` producer + `sdlc-review` skill — code is gated (diff + tests + spec) and merged to an integration branch; main stays human-gated.
- **Autonomy governor**: `~/.hscc/autonomy` flag + "do it autonomously" phrase trigger for hands-off idea→shipped runs.
- **Worker keep-alive + self-heal**: daemon health-checks worker-node vLLM models and relaunches crashed ones; per-node concurrency caps.
- **Bootstrap installer**: preflight-gated, topology-detecting (`sparkrun cluster list`), minimal-interview installer that readies a machine on any sparkrun cluster.
- Operations-topic notifications for worker-model crash/recovery events.

### Changed
- Dispatch now runs on native Hermes kanban (built-in dispatcher + git worktrees); HSCC is the thin cluster-physical layer on top.
- Cluster topology resolves from `~/.hscc/serving.json` / sparkrun at runtime; source ships generic fallbacks only.
- MIT licensed; README rewritten as a project overview.

### Removed
- Duplicated agent pipeline (coordinator, projects, orchestrator, events, governance, MCP server) — superseded by native kanban.
- Legacy parallel installer (`install/hscc-cli`, plugin/template copies, `install.sh`) — replaced by `hscc-bootstrap`.

## [2026.05.28] — Initial Release

### Added
- HSCC Python CLI (`hscc init`, `hscc status`, `hscc chat`, `hscc reset`)
- 12 HSCC plugins (daemon, chat, agent-coordinator, governance, skills, bootstrap, cluster, events, orchestrator, projects, provision, optimizations)
- 7 Hermes skills (brainstorming, caveman, executing-plans, systematic-debugging, test-driven-development, verification-before-completion, writing-plans)
- 6 templates (AGENTS.md, HEARTBEAT.md, IDENTITY.md, SOUL.md, TOOLS.md, USER.md)
- Config template (`hscc-config.yaml.template`)
- SSH utilities (key generation, copy, test)
- Model utilities (cache detection, download, verify)
- Verify module (bootstrap verification, health checks)
- Installer module (wiring logic, launchd plist, config creation)
- Complete documentation (README, .gitignore, CHANGELOG)

### Features
- Idempotent `hscc init` — safe to re-run
- Model cache detection (Qwen3.6-35B on NAS and cluster nodes)
- Cluster node detection and reachability check
- Gateway health monitoring
- Daemon status via launchd
- Plugin auto-registration
- Config from YAML template
- Model deployment from NAS via rsync
