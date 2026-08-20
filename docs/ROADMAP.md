# Subproject: hscc

## Milestone: Template apply converges on logical aliases <!-- id: alias-convergence -->
status: now
- [x] apply writes the logical alias per consumer; probe decides what is written (v1.8.0)
- [x] apply and doctor --fix converge on every key — applying then migrating is a no-op (v1.8.1)
- [x] model.default and worker-model ids use the shared alias candidate, never the concrete recipe id (v1.8.1)
- [x] _http_get threaded through apply for zero network I/O under test (v1.8.1)
- [x] probe-before-write safety net: an endpoint that does not advertise an alias keeps the concrete id (v1.8.x)
- [x] adopt sparkrun's structured cluster-status API (`--json` / ClusterStatus dataclass) in place of text-parsing `Job:`/`Idle hosts:` in ops text parsing (v1.8.3)
- [x] pre-release audit (core + bootstrap): fixed corrupt-trigger-defaults silent success, int-like cap preservation, and non-zero exit for a blocked/partial `template apply`; ELI5 README (v1.8.4)

## Milestone: Explicit placement and routing in cluster templates <!-- id: template-routing -->
status: next
- [x] schema v3 parse of nodes / allow_colocation / routing (carry only, no behavior) (v1.7.0)
- [x] T2 explicit `nodes:` bypass the resolver — deterministic placement, tp peers named (v1.7.0)
- [x] T3 routing — resolve symbolic targets, probe-before-write, omission=do-not-write (v1.7.0)
- [x] T4 two-layer template validation (structural offline + placement live) with CLI flags (v1.7.0)
- [x] T5 apply pre-flight gate reuses validate_template and blocks before touching anything (v1.7.0)
- [x] preview discloses routing via apply's resolution helpers (v1.7.3)
- [x] /v1/models readers pick concrete id by name, never by list order; multi-name endpoints handled (v1.7.2)
- [x] dual-dsv4 template declares routing — subagents on the second DSV4 (v1.7.1)

## Milestone: Model aliases, tp-peer awareness and drift detection <!-- id: aliases-drift -->
status: next
- [x] logical model aliases (orchestrator-model / worker-model) + full cluster visibility (v1.6.0)
- [x] self-heal tp-peer awareness — never mark a tp peer down (v1.6.1)
- [x] real serve-command drift detection + apply recreate-on-drift / --force-recreate (v1.6.1)
- [x] doctor served-alias safety gate for alias migration (v1.6.1)
- [x] doctor --fix + models-served check flags stale model ids (v1.5.0/1.6.1)
- [x] model cost/placement optimization via recipe_cost VRAM auto-fit (WS5)

## Milestone: Topology-free cluster templates and apply pipeline <!-- id: template-engine -->
status: later
- [x] topology-free intent template schema + resolver (no hardcoded IPs/ports) (WS5)
- [x] node-count template library for 1/2/3/4/8 nodes, VRAM-verified against sparkrun (v1.0.0-beta.1)
- [x] apply pipeline: CLI integration, atomic apply snapshot + auto-rollback (WS5 part 4a)
- [x] sparkrun-show VRAM cost parser + auto-fit placement (WS5 part 1)
- [x] live VRAM fit-check, success-on-warn, preflight validation on apply (v1.0.0)
- [x] DSV4 FP8 4-node templates, dual-dsv4 + dsv4-plus-coding (v1.5.0)
- [x] multi-model-per-node via unit-keyed worker supervision (WS5 part 3)

## Milestone: Failure escalation and autonomous operation <!-- id: autonomy-escalation -->
status: later
- [x] failure-escalation decision logic (escalate.py) from repeated failures (v1.1.0)
- [x] cpu watcher reassigns failing tasks to strong tier + Telegram human alert, deduped (v1.3.0)
- [x] escalate_watcher scans live kanban tasks for failures (v1.1.0)
- [x] auto-unblock repair + resume-note against kanban_db 0.19 API (v1.3.0)
- [x] autonomy flag (~/.hscc/autonomy) + show/on/off CLI; do-it-autonomously trigger (v1.0.0)
- [x] daily human-gated runtime dependency-update loop (automated PRs + verification cards) (v1.0.2)

## Milestone: Role framework and code-review gate <!-- id: role-framework -->
status: later
- [x] role spec format + validation; layered SOUL composition (base + role + ops) (v1.0.0)
- [x] roster expanded to 22 role profiles (full company); new roles minted via CLI create (v1.0.0)
- [x] model_tier per role (fast/strong) + per-role model endpoint override (v1.0.0-beta.10, v1.1.0)
- [x] reviewer role + sdlc-review skill; review gate gates diff + tests + spec (v1.0.0)
- [x] worker profiles = full toolset minus cluster control (orchestrator-only gate) (v1.0.0)
- [x] native 0.17 profile API + routing_description for decomposer discriminative routing (beta.9)

## Milestone: Monitoring daemon and fleet operations <!-- id: daemon-fleet-ops -->
status: later
- [x] monitoring + self-heal daemon (launchd / systemd) unit-keyed worker keep-alive (v1.0.0)
- [x] vLLM / gateway / NAS health checks; crashed models relaunch with node recipe (v1.0.0)
- [x] trigger engine with default alert rules (v1.1.0)
- [x] fleet analytics aggregation (stats.py) + verify checks module (v1.1.0)
- [x] vLLM throughput + queue-depth metrics (throughput.py) (v1.1.0)
- [x] autoscale decision logic from queue depth (read-only) (v1.1.0)
- [x] startup cruft self-clean (corrupt/stale + uncapped .bak) (v1.0.0)
- [x] operator slash commands: /status /heal /template /cluster /orch-restart /cluster-restart (v1.0.0-beta.3)
- [x] agent tool parity — orchestrator can call fleet + template tools directly (v1.2.0)

## Milestone: Bootstrap installer and upstream adoption <!-- id: bootstrap-upstream -->
status: later
- [x] topology-detecting, preflight-gated installer; --yes fully non-interactive; --no-backup (v1.0.0)
- [x] doctor preflight + --fix reconciles; copies plugins into runtime, dev/repo split (v1.0.0)
- [x] VERSION marker + integrated gw/cluster model ids; unified `hscc` CLI (v1.0.0)
- [x] standalone from OpenClaw, replaced with Hermes-native (native-Hermes-first) (v1.0.0)
- [x] verified runtime pinned (hermes v2026.7.30 / sparkrun v0.3.1) + compat audit docs (v1.4.0)
- [x] run official hermes/sparkrun; local edits as reapply-able patches (WS8)
- [x] retire sparkrun patches that landed upstream (v1.0.0-beta.8)
- [x] upstream debloat / dependency-removal audits (place-holder IP bug, sparkrun 0.3.x gap analysis) (ongoing)
- [ ] continue dropping HSCC forks/patches as the equivalent lands upstream (dependency-removal loop)
