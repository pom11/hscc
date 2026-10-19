# docs

HSCC follows a doc-driven flow: **spec → plan → build**, with the plan's
checklist as the acceptance contract the reviewer checks against.

Internal design specs and per-workstream implementation plans are kept **local
and untracked** (under `docs/superpowers/`, gitignored at `.gitignore:60`) —
they're working documents, not part of the published repo. The shipped record
of what changed lives in the top-level [`CHANGELOG.md`](../CHANGELOG.md) and
the GitHub releases.

**Authoring an entry:** never edit `CHANGELOG.md`. Add one file per kanban
card at the repo root's `changelog.d/<your-task-id>.md` (see its
[`changelog.d/README.md`](../changelog.d/README.md)), and `[Unreleased]` is
generated from those fragments at release. The rule exists because the shared
`[Unreleased]` section had become a serial merge-conflict point for concurrent
cards.

## This index

Every tracked file under `docs/` is enumerated below — one line each: what it
is, plus a status:

- **live** — current reference; trust it (re-verify claims before acting).
- **record** — historical record of a card/incident; kept for provenance,
  not current truth. Code and the live docs win on any conflict.
- **superseded** — replaced by something specific; the superseder is named
  inline.

Find a file by what it is: [Reference](#reference) · [Roadmap &
handoffs](#roadmap--handoffs) · [Investigations & top-level audits](#investigations--top-level-audits)
· [design/](#design) · [template-examples/](#template-examples) ·
[verification/](#verification) · [audits/](#audits).

## Reference

- `docs/README.md` — this index. **live**
- `docs/API.md` — `hscc api` HTTP surface: endpoints, auth, payloads, CLI. Referenced from the root README. **live**
- `docs/DESIGN-api.md` — design spec for the external-app HTTP API (stdlib, Tailscale-bound, token-auth, `speak` field); the reasoning behind `API.md`. **live**
- `docs/DESIGN-template-explicit-placement.md` — schema-v3 template design: explicit `nodes:` placement + `routing:` blocks; shipped in v1.7.x and the basis of every template since. **live**
- `docs/PROJECT-COMMANDS.md` — old↔new command mapping (`flightdeck …` → `hscc project …`) and naming notes; the sanctioned survival list for the legacy name. **live**
- `docs/COMPAT-7.30.md` — compat audit of hermes-agent upstream v2026.7.30 (v0.19.1) vs the HSCC fork+patches (patches 0001–0006). **superseded** by `docs/audits/NOTES_t_507108a7.md` (patches re-carried onto v2026.9.11); the live pins live in `hscc-bootstrap/runtime-versions.json`. **record**
- `docs/hermes-017-new-features.md` — feature digest of hermes-agent 0.17.0 (kanban lifecycle hooks etc.) as adopted by HSCC. **superseded** by later runtime bumps (pin is v2026.9.24 per `hscc-bootstrap/runtime-versions.json`). **record**

## Roadmap & handoffs

- `docs/ROADMAP.md` — the live subproject roadmap: milestones with ids (`template-routing`, `template-engine`, `profile-provisioning`…), checked off as work lands. **live**
- `docs/ROADMAP.draft.md` — earlier snapshot of the same file (pre-v1.8.3/1.8.4 progress). **superseded** by `docs/ROADMAP.md`. **record**
- `docs/HANDOFF.md` — 2026-09-01 session-resume runbook (board triage, cherry-pick-verify-ship). Obsoleted by the current kanban dispatcher + this R-wave docs set. **record**
- `docs/TASK_t_7cc2e7d5_handoff-consume.md` — task doc consuming the session-ownership handoff design (app drives session 1:1). **record**

## Investigations & top-level audits

- `docs/INVESTIGATION-bot-mode-relevance.md` — whether hermes "Bot Mode" (v2026.8.16.2 era) fits HSCC's headless deployment. **record**
- `docs/INVESTIGATION-flightdeck-port.md` — design record of porting flightdeck into hscc (hscc became the single tool; the port shipped). **record**
- `docs/INVESTIGATION-per-project-profiles.md` — per-project Hermes profiles ("bots") vs project-linking for the EcoFire family. **record**
- `docs/bootstrap-audit-1-install-audit.md` — bootstrap audit 1/3: install_payload / install_scripts / install_soul / apply_patches correctness + silent-failure hunt. **record**
- `docs/bootstrap-audit-2-correctness.md` — bootstrap audit 2/3: detect / doctor / enable_plugins / ensure_review_feature. **record**
- `docs/bootstrap-audit-3-preserve-serve-template.md` — bootstrap audit 3/3: preserve_autodown / serving_gen / suggest_template / install_triggers. **record**
- `docs/dispatch-assignee-validation.md` — t_d3c929aa: validation guard for dispatch `--assignee`. **record**
- `docs/hermeticity-audit-t_2985e00b.md` — hermeticity audit of the test suite. **record**
- `docs/qrscanner-audit.md` — QRScannerView screen audit (t_8320013b). **record**
- `docs/review_part2_5_READMEs.md` — README review part 2/5 (hscc_daemon, scripts, ios-app, templates, install), 2026-09-25. **superseded** by `docs/audits/README_REVIEW_2026-10-15.md`. **record**
- `docs/review_t_8ac9e87c.md` — README review (bootstrap, cluster, roles, api, commands), 2026-09-25. **superseded** by `docs/audits/README_REVIEW_2026-10-15.md`. **record**
- `docs/review_t_d20fb383.md` — README review (memori, memori_byodb, sparkrun-hermes), 2026-09-25. **superseded** by `docs/audits/README_REVIEW_2026-10-15.md`. **record**
- `docs/t_f109e7ea-findings.md` — app→CLI two-way findings: working/reasoning indicator + 1:1 session surfacing. **record**

## design/

- `docs/design/2026-10-15-template-declarations.md` — spec: template-declared `endpoints:`, `proxy:` and caps ("the template is the declaration, apply is the writer"). **live** — and the doc's own **2026-10-19 addendum** supersedes its §2.3/§6.2 details (nested `gateways.litellm.router_settings` read order, engine file moved to `src/sparkrun/plugins/litellm/engine.py`, blessed defaults).
- `docs/design/2026-09-30-project-session-continuity.md` — design: project session continuity across surfaces (Approach 1); verified live in `docs/audits/session-continuity-verify-2026-09-30.md`. **live**
- `docs/design/2026-10-02-session-ownership-handoff.md` — design: session-ownership handoff between app and CLI ("remote-control" continuity); consumed by `docs/TASK_t_7cc2e7d5_handoff-consume.md`. **record**
- `docs/design/idle-autodown.md` — design for the serving-layer idle autodown/autoup (the autodown feature's authoritative design; operator-facing summaries also live in the root README and `hscc_daemon/README.md`). **live**
- `docs/design/idle-autodown-report.md` — implementation map for autodown (per-phase files/functions). **live** companion to the design doc.
- `docs/design/IOS_REDESIGN_2026-10-14.md` — iOS redesign spec: AppShell drawer over one NavigationStack, 17 drawer rows — the IA the app ships today. **live** (operational state in `ios-app/README.md`)
- `docs/design/ios_redesign_mockup.html` — clickable mockup companion to the redesign spec (9 frames). **live** companion.
- `docs/design/2026-08-28-ios-app-v1.md` — original iOS operator-console v1 design (three-tab IA). **superseded** by `docs/design/IOS_REDESIGN_2026-10-14.md` (drawer IA). **record**
- `docs/design/2026-08-30-audit-report.md` — "Monday readiness" audit of the v1 app. **record**
- `docs/design/telegram-optional.md` — design + implementation log for making Telegram fully optional; the removal then shipped (`docs/audits/telegram-removal-report.md`). **record**
- `docs/design/screens/` (3 PNGs below) — earlier root-tree reviewer proof shots cited by `ios-app/README.md` and the redesign spec; the current full 12-screens × light/dark capture set lives under `ios-app/docs/design/screens/`.
- `docs/design/screens/appicon-springboard.png` — springboard proof the AppIcon replaced the `H` placeholder. **live** evidence.
- `docs/design/screens/phase2-home.png` — Phase-2 home reviewer screenshot. **live** evidence.
- `docs/design/screens/phase2-home-first-run-notifications-prompt.png` — Phase-2 first-run notifications-prompt reviewer screenshot. **live** evidence.

## template-examples/

- `docs/template-examples/example-colocation.yaml` — annotated example: colocated models on one node (not a shipped layout). **live**
- `docs/template-examples/example-routing-split.yaml` — annotated example: split serving/routing (not a shipped layout). **live**

## verification/

- `docs/verification/WS3-verification.md` — verification record: native 0.17 profile API (workstream 3). **record**

## audits/

Working directory of per-card audit/incident reports. The **live** ones below
are current operational references; everything else in `audits/` is the
**record** of a landed card — search this index (or `git log --oneline --
docs/audits/`) rather than reading them blind.

### Live references

- `docs/audits/README_REVIEW_2026-10-15.md` — repo-wide README audit (t_6e1e8443): measurements, per-file verdicts, the R1–R7 target-state spec this docs wave executes from. **live**
- `docs/audits/CLI_HELP_2026-10-15.md` — CLI help-surface audit: canonical command inventory + REAL EXAMPLE DATA renders (post-F1 forms). The F-lane landed via PR #57; this doc remains the reference for quoted CLI output. **live**
- `docs/audits/SPARKRUN_040_COMPAT_2026-10-19.md` — sparkrun v0.3.10 → v0.4.0 compat record: deployed topology, fork branch `feat/proxy-least-in-flight-040`, live verification, rollback line. **live** (supersedes the 0.3.10-era statements in `docs/audits/DEPBUMP_GUARD_2026-10-16.md` and `docs/audits/sparkrun-service-path-t_b543e530.md`)
- `docs/audits/HEAL_CASCADE_2026-10-14.md` — heal-cascade incident: why the heal-lease / heal-breaker / launch-guard trio exists; rationale cited by `hscc_daemon/README.md`. **live**
- `docs/audits/IOS_CHAT_REPORT_2026-10-14.md` — iOS chat end-to-end overnight report; baseline for the chat path as shipped. **live**
- `docs/audits/PROXY_RELAUNCH_NOOP_2026-10-18.md` — proxy :4000 outage while the 5-min relaunch loop no-opped: root cause + fix. **live** incident record.
- `docs/audits/DEPBUMP_GUARD_2026-10-16.md` — dep-bump fork-branch guard + `router_settings` drift check (t_39d7c915). Guard design **live**; the 0.3.10 fork-branch names it cites are the rollback line since the 0.4.0 cutover — see `docs/audits/SPARKRUN_040_COMPAT_2026-10-19.md`.
- `docs/audits/address-guard-binary-policy-t_3e6d3db7.md` — the address guard's binary policy (what counts as a real address, placeholder ranges `10.0.0.x` / `100.64.0.1`). **live**
- `docs/audits/commit-time-address-guard-t_ec2c2f95.md` — commit-time address-guard hook: mechanism + bypass rules. **live**
- `docs/audits/ci-backstop-address-guard-t_9a4b7687.md` — CI backstop for the address guard (`--tracked` leg). **live**
- `docs/audits/changelog-decomposition_t_95f1d6e1.md` — why `changelog.d/` exists and the proof it works; companion to `changelog.d/README.md`. **live**
- `docs/audits/session-continuity-verify-2026-09-30.md` — integration + end-to-end live verification of session continuity. **live** evidence for the design doc.
- `docs/audits/ARTIFACT_t_563350d9.md` — per-project permanent chat sessions (registry-persisted session id, relay/history resolve the same session). **record** of the shipped feature.
- `docs/audits/GAP_PRIORITISATION_2026-09-26.md` — Phase-3 triage: which missing capabilities deserve an API route / iOS surface vs CLI-only. **record** (decision rationale for the iOS backlog).

### Cluster / daemon incidents & fixes (all record)

- `docs/audits/autoheal-startup-grace-t_a763020e.md` — auto-heal startup grace keyed on container age: implementation report.
- `docs/audits/autoheal-startup-grace-remote-t_cd6a895a.md` — the same grace made inert-by-design wrong: resolve container age through sparkrun instead.
- `docs/audits/autoheal-solo-remedy-t_4c9dd6d4.md` — auto-heal remedy spawned a SOLO instead of restoring the TP pair.
- `docs/audits/daemon-loaded-but-dead-t_a6697001.md` — hscc_daemon plugin loaded but the daemon loop dead: root cause + fix.
- `docs/audits/duplicate-workload-t_e1ff0e8e.md` — gentle relaunch created a duplicate workload `serving.json` never declared.
- `docs/audits/false-fail-streams-t_3fe0cd05.md` — false-fail health streams (DGX / gateway / NAS).
- `docs/audits/stream-flow-tcc-t_fc2e955d.md` — daemon streams FAIL every tick but PASS fresh: TCC root cause.
- `docs/audits/log-rotation-t_aec5aebd.md` — daemon.log hit 299 MB with no rotation; both daemon log paths rotated.
- `docs/audits/node-stream-ownership-routes_ws-t_e1cfda68.md` — node stream ownership + ws relay / Rust panic root cause.
- `docs/audits/payload-drift-t_ab336532.md` — surface installed-payload drift automatically.
- `docs/audits/route-fleet-ssh-via-sparkrun-t_17cdc637.md` — route hscc-cluster fleet SSH through sparkrun.
- `docs/audits/sparkrun-service-path-t_b543e530.md` — sparkrun resolution under a service-supervised PATH.
- `docs/audits/t_17dc00ee_signal15.md` — investigation: who sends SIGTERM to the hscc daemon.
- `docs/audits/t_17dc00ee_context.txt` — raw signal-15 log extract attached to the investigation above.
- `docs/audits/t_873590dd_engine-wedge-inert.md` — engine-wedge self-heal is inert: measured diagnosis.
- `docs/audits/t_cc8879e0_wedge-load-aware.md` — engine-wedge check declared a BUSY (healthy) unit wedged.
- `docs/audits/t_1c4e8160_heartbeat-liveness.md` — durable daemon liveness heartbeat + `daemon_liveness()`.
- `docs/audits/t_8334f251_status-gates-on-heartbeat.md` — `hscc status` gates RUNNING on durable heartbeat liveness.
- `docs/audits/t_8c729895_report.md` — dispatcher_wedge.py: drop the expired hermes_cli.kanban_db compat shim.
- `docs/audits/t_8306890e_plan.md` — plan: configurable dispatcher worktree root + EcoFire worktree relocation.
- `docs/audits/t_8306890e_report.md` — final report: same workstream, landed.
- `docs/audits/hscc-cli-interpreter-t_2bcbe6f9.md` — hscc CLI interpreter (venv/shebang) durable fix.
- `docs/audits/ws_relay_stop_noop_flake_t_163fa09f.md` — p313 collection-order flake in `test_ws_relay_not_noop.py`.
- `docs/audits/rc143-suite-reaper-t_6bb29d46.md` — rc=143 mid-suite SIGTERM root cause.
- `docs/audits/doctor-hermes-home-t_267f9d88.md` — doctor + HERMES_HOME: was the check wrong or only the tests.
- `docs/audits/approvals-single-query-mode-t_0a90efc7.md` — bootstrap wiring of `approvals.single_query_mode` for workers.
- `docs/audits/worker-shell-diagnostics-t_74e1ff6f.md` — kanban worker shell diagnostics.
- `docs/audits/worker-shell-persistence-t_74e1ff6f.md` — audit: how workers can persist work (toolset + non-lossy gap).
- `docs/audits/backup-retain-atomicity_t_9462260b.md` — backup pile growth + non-atomic backup writes.
- `docs/audits/drift-guard-test-dir-census-t_69a1c9f2.md` — widening the DIRS drift guard to every `tests/` dir.
- `docs/audits/run-tests-scripts-leg-t_95d864fc.md` — `run_tests.sh` never ran `scripts/tests`: the fix + evidence.
- `docs/audits/memory-path-resolution-t_7a16aed0.md` — memory endpoint profile-resolution fix.
- `docs/audits/protocol-violation-budget-already-implemented.md` — audit proving the protocol-violation budget was already implemented upstream.
- `docs/audits/analyze.py` — scratch log-analysis script used by the daemon-log investigations.
- `docs/audits/overnight-2026-09-22.md` — overnight report: project session linking + digests epic.
- `docs/audits/overnight-2026-09-22.log` — raw log attached to the overnight report above.
- `docs/audits/overnight-2026-09-24.md` — overnight report: v2.1.1 cut.
- `docs/audits/GOAL_REPORT_2026-09-26.md` — summary of the 2026-09-26 24h autonomous run.
- `docs/audits/GOAL_LEDGER_2026-09-26.md` — the same run's raw ledger (2.3 MB; append-only event log).

### Guards, hygiene & upstream reports (record)

- `docs/audits/BRANCH_HYGIENE_2026-10-17.md` — origin branch-cleanup audit.
- `docs/audits/worktree-hygiene-2026-10-05.md` — worktree / `wt-*` branch hygiene for the primary checkout.
- `docs/audits/DISPATCHER_HOLD_UPSTREAM_2026-10-17.md` — upstream report: the Hermes dispatcher counts benign holds as "stuck".
- `docs/audits/telegram-removal-plan.md` — plan for removing Telegram from hscc_daemon / bootstrap / scripts / cluster / api.
- `docs/audits/telegram-removal-report.md` — the removal, shipped (t_07fd5b86).
- `docs/audits/architect-rewrite-card.md` — card spec: project digest (bounded archive digest).
- `docs/audits/architect-rewrite-findings.md` — finding the second mechanism that rewrote card assignee to `architect`.
- `docs/audits/NOTES_t_507108a7.md` — re-carriage of `patches/hermes/` onto hermes v2026.9.11 (supersedes `docs/COMPAT-7.30.md`'s patch ledger).
- `docs/audits/NOTES_t_5132deb3.md` — cli-help follow-up: `cluster up/down --help`, escalate/autoscale `-h`, CLI-wide `-h` gap.
- `docs/audits/NOTES_t_fc307a51.md` — cli-help F5: top-level help surface.
- `docs/audits/PR33-FOLLOWUPS-t_36580090.md` — PR #33 review follow-up decisions.
- `docs/audits/REBASE_PR35_2026-10-18.md` — PR #35 rebase-onto-main resolution.
- `docs/audits/readme-review-t_0ae232a4.md` — README review (docs + hscc-skills + hscc-project), 2026-09-25. **superseded** by `docs/audits/README_REVIEW_2026-10-15.md`.
- `docs/audits/readme-review-t_d90de5b6.md` — README review (root + hscc-cli + install), 2026-09-25. **superseded** by `docs/audits/README_REVIEW_2026-10-15.md`.
- `docs/audits/audit-ux-pass-2026-09-24.md` — full audit + UX pass epic decomposition.
- `docs/audits/dead-code-audit-t_58f21007.md` — dead-code & dangling-reference audit.
- `docs/audits/confirm-gate-audit-t_c77f243f.md` — confirm-gate audit: iOS client ↔ hscc-api.

### iOS / Phase-1 reconciliation & screen audits (record)

- `docs/audits/AUDIT_ACTIVITYFEED_t_03f8192d.md` — screen audit: ActivityFeedView.
- `docs/audits/AUDIT_APPROVALS_t_48bbf99b.md` — screen audit: ApprovalsView.
- `docs/audits/AUDIT_SessionHistoryView.md` — screen audit: SessionHistoryView.
- `docs/audits/AUDIT_SessionsView_t_bf2394ab.md` — screen audit: SessionsView.
- `docs/audits/CardsView-audit-t_cb300f9c.md` — screen audit: CardsView.
- `docs/audits/phone-performance.md` — phone performance audit.
- `docs/audits/token-auth-device-path.md` — token & auth handling audit, device path end-to-end.
- `docs/audits/verify-chat-roundtrip.md` — E2E chat pipeline proof (t_f909de4e).
- `docs/audits/stop-running-turn-audit-t_6efb89ff.md` — chat: stop/cancel a running turn — gap record.
- `docs/audits/monitor-liveactivity-t_03d318cd.md` — HSCC Monitor Live Activity work notes.
- `docs/audits/bgtask-crash-fix-t_1b8792ca.md` — BGTaskScheduler crash fix work notes.
- `docs/audits/t_ca133e7d_decode_drift.md` — decode-fixture drift recheck (the fixture additions the fixtures README counts).
- `docs/audits/phase1-api-history-disposition-t_e2856bfe.md` — Phase-1 [api-history] branch disposition.
- `docs/audits/phase1-approvals-autodown-disposition-t_b578972f.md` — Phase-1 [approvals + autodown controls] disposition.
- `docs/audits/phase1-chat-composer-disposition-t_d331843e.md` — Phase-1 [chat-composer] disposition.
- `docs/audits/phase1-detail-error-views-reconciliation.md` — Phase-1 [detail-error-views] ledger.
- `docs/audits/phase1-fleet-monitoring-reconciliation.md` — Phase-1 [fleet-monitoring] ledger.
- `docs/audits/phase1-serving-alerts-widget-disposition-t_c14a427c.md` — Phase-1 [serving/alerts/widget] disposition.
- `docs/audits/phase1-settings-profile-disposition-t_1bfe8908.md` — Phase-1 [settings-profile] disposition.
- `docs/audits/preserved_branches/AUDIT_BoardHygiene_t_c13011d7.md` — BoardHygieneView audit rescued from a deleted Phase-1 branch.
- `docs/audits/preserved_branches/REPORT_t_9b678f46.md` — AutodownView audit rescued from a deleted branch.
- `docs/audits/preserved_branches/REPORT_t_fd2be5be.md` — HealthResponse.decode fix report rescued from a deleted branch.
- `docs/audits/preserved_branches/searchview_audit_t_51bad645.md` — SearchView audit rescued from a deleted branch.

### RICH CLI theming epic + Phase-2 sweeps (record)

Audit notes (`*_audit.md`) ship with their e2e verify scripts (`*_verify_e2e.py`).

- `docs/audits/t_1ca5c6e9_audit.md` — theme `project` + `map_sessions` CLI output (card 1/21).
- `docs/audits/t_1ca5c6e9_verify_e2e.py` — its e2e verification script.
- `docs/audits/t_b2d2c0ae_audit.md` — theme `message`/`ask`/`report`/`qa` output (card 2/21).
- `docs/audits/t_b2d2c0ae_verify_e2e.py` — its e2e verification script.
- `docs/audits/t_4a548958_audit.md` — theme `review`/`roadmap`/`standup`/`why` output (card 3/21 group A).
- `docs/audits/t_4a548958_verify_e2e.py` — its e2e verification script.
- `docs/audits/t_1b206473_audit.md` — theme archive/hygiene/incident/reconcile output (card 3/21 group B).
- `docs/audits/t_1b206473_verify_e2e.py` — its e2e verification script.
- `docs/audits/t_8f0031e0_audit.md` — theme `daemon`/`daemon_install`/`start`/`update`/`init` output.
- `docs/audits/t_8f0031e0_verify_e2e.py` — its e2e verification script.
- `docs/audits/t_c2cae6e9_audit.md` — themed Rich audit: sync/release/ingest/metrics.
- `docs/audits/t_c2cae6e9_verify_e2e.py` — its e2e verification script.
- `docs/audits/t_d7a8d1f3_audit.md` — theme `doctor`/`decompose`/`legacy`/`lint`/`verify` (group C sub-b).
- `docs/audits/t_d7a8d1f3_verify_e2e.py` — its e2e verification script.
- `docs/audits/t_e67544b1_audit.md` — theme hscc-cluster standalone CLI entry points (21 raw sites).
- `docs/audits/t_e67544b1_verify_e2e.py` — its e2e verification script.
- `docs/audits/t_e67544b1_verify_deployed.py` — its deployed-tree verification script.
- `docs/audits/t_28d1653c_report.md` — RICH CLI sparkrun-hermes final report.
- `docs/audits/t_69ddd670_report.md` — RICH CLI hscc_daemon residual (raw sites → themed).
- `docs/audits/t_6b994390_report.md` — RICH CLI hscc_daemon/api_cli.py (raw prints → themed).
- `docs/audits/t_baef4e5f_report.md` — RICH CLI hscc-roles (17 raw sites → themed).
- `docs/audits/t_e287cfb6_report.md` — generator emits + preserves per-profile `memory:` block.
- `docs/audits/demo_memory_regen.py` — hermetic demo for the card above (double-generate must not revert `memory:`).
- `docs/audits/t_e6bff987_audit.md` — cwd-dependent MissingStyle `error` flake in test_project.py.
- `docs/audits/t_d5780187_audit.md` — make test_standup.py hermetic (stop live-state flake).
- `docs/audits/rich_markup_escape_t_716cf37c.md` — Rich markup injection in human views: audit + fix.
- `docs/audits/t_04e16e60_theme_sweep.md` — Phase-2 [detail-template] theme sweep audit.
- `docs/audits/t_320a8332_theme_sweep.md` — Phase-2 cluster + control surfaces theme sweep.
- `docs/audits/t_b89d9029_theme_sweep.md` — Phase-2 [project-sessions] theme sweep audit.
- `docs/audits/t_dacdbc4a_theme_sweep.md` — Phase-2 [chat] theme sweep audit.

### memori capture chain (record)

- `docs/audits/t_3969ace6_memori_capture.md` — root cause of memori_byodb silence since 2026-06-13.
- `docs/audits/t_57e5b3f7_memori_capture_fix.md` — fix: capture never wrote conversation/facts.
- `docs/audits/t_9e8732b8_memori_profile_capture.md` — capture still not working after the first "deployed" fix.
