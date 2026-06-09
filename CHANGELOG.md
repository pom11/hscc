# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

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
