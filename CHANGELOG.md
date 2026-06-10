# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [2026.06.10-alpha] — Operator commands, sparkrun plugin, daemon repair

Incident-response tooling, the official Hermes sparkrun plugin, and a working
control daemon — plus bootstrap that reproduces the full live wiring.

### Added
- **Operator slash commands** (`hscc-commands`): `/cluster`, `/orch-restart`,
  `/cluster-restart`. They run directly in the gateway (not via the LLM), so
  they work even when the orchestrator model is wedged. Confirm-first.
- **Official Hermes sparkrun plugin** (`sparkrun-hermes`): a single guarded
  `sparkrun_exec` CLI passthrough plus the run/setup/registry skills. Mirrors
  the official OpenClaw + Claude Code plugins (no Hermes plugin existed before).
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
