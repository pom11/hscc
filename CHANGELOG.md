# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

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
