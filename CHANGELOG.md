# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

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
