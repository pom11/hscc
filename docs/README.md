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
is, plus a status. The claim is gated, not just declared: `readme_lint.py`
rule (4) (DOC-UNLISTED) fails when a tracked file under docs/ has no
backtick-quoted index entry here:

- **live** — current reference; trust it (re-verify claims before acting).
- **record** — historical record of a card/incident; kept for provenance,
  not current truth. Code and the live docs win on any conflict.
- **superseded** — replaced by something specific; the superseder is named
  inline.

Find a file by what it is: [Reference](#reference) · [Roadmap &
handoffs](#roadmap--handoffs) · [Investigations & top-level audits](#investigations--top-level-audits)
· [design/](#design) · [ops/](#ops) · [template-examples/](#template-examples) ·
[verification/](#verification) · [reports/](#reports) · [audits/](#audits).

## Reference

- `docs/README.md` — this index. **live**
- `docs/API.md` — `hscc api` HTTP surface: endpoints, auth, payloads, CLI. Referenced from the root README. **live**
- `docs/DESIGN-api.md` — design spec for the external-app HTTP API (stdlib, Tailscale-bound, token-auth, `speak` field); the reasoning behind `API.md`. **live**
- `docs/DESIGN-template-explicit-placement.md` — schema-v3 template design: explicit `nodes:` placement + `routing:` blocks; shipped in v1.7.x and the basis of every template since. **live**
- `docs/DESIGN-probe-before-write.md` — delegation routing + the probe-before-write guard: why `delegation.model` is written from what the worker proxy actually serves (`/v1/models` probe) instead of the routing alias, the two-registries 404 trap, and the one probe contract for the whole fleet. Extracted out of `hscc-cluster/README.md` (R4). **live**
- `docs/PROJECT-COMMANDS.md` — old↔new command mapping (`flightdeck …` → `hscc project …`) and naming notes; the sanctioned survival list for the legacy name. **live**
- `docs/FLEET.md` — how the fleet is organised: role specs and the profile generator (layered SOUL), the review loop, the autonomy flag, failure escalation, and the launchctl ops lore. Linked from the root README. **live**
- `docs/WHY.md` — why HSCC exists: the gaps stock Hermes leaves open (cluster control, role roster, review dispatch, autonomy, bootstrap) against what HSCC adds, plus a worked end-to-end example. Linked from the root README. **live**
- `docs/COMPAT-7.30.md` — compat audit of hermes-agent upstream v2026.7.30 (v0.19.1) vs the HSCC fork+patches (patches 0001–0006). **superseded** by `docs/audits/NOTES_t_507108a7.md` (patches re-carried onto v2026.9.11); the live pins live in `hscc-bootstrap/runtime-versions.json`. **record**
- `docs/hermes-017-new-features.md` — feature digest of hermes-agent 0.17.0 (kanban lifecycle hooks etc.) as adopted by HSCC. **superseded** by later runtime bumps (pin is v2026.9.24 per `hscc-bootstrap/runtime-versions.json`). **record**
- `docs/UPSTREAM-kanban-wake-once-gate.md` — upstream proposal (t_f26d89c0): make one kanban event wake its recipient once across the two emitters — the hermes root cause (`uuid4` wake identity), the board-DB wake claim, and what `orch_waker` retires when it lands. **live** (pending upstream decision)

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
- `docs/t_18318fd9-findings.md` — findings for the `hscc autodown status --json` flake: a foreground-mode WARN line from `daemon_ops.log` was prefixed to stdout's JSON; the fix is both sides (stderr for diagnostics, machine channel kept clean). **record**
- `docs/t_16eb73ab-profile-target-measurement.md` — t_16eb73ab STEP-1 measurement (measure-first, no code): the three profile-home sets (profile list / board-assignee census / homes already carrying payload), why the assignees census cannot isolate a board, and the dry-run baseline that framed the operator's target-profiles decision. **record** — the decision it gated shipped on t_32f97d20.
- `docs/t_32f97d20-target-profiles-seed.md` — t_32f97d20 seed derivation: the board-isolated rule behind `DEFAULT_TARGET_PROFILES` (distinct hscc-board assignees ∩ on-disk homes), the ghost exclusion, and the operator override file. **record** — the live truth is the constant in `hscc-bootstrap/install_payload.py` plus `~/.hscc/target_profiles.json`.

## design/

- `docs/design/node-pause-auto-resume-t_110adc1a.md` — decision trail for the daemon ending an expired node pause on schedule: why the tick body lives with the executor and the cadence in the loop's table, why the schedule is the overlay file read per tick instead of an in-memory timer, why the pass clears a pause only after the units are back (and never via the overlay's bulk expiry helper), and why it calls the shared resume policy rather than the verb's output. Three of those deliberately contradict the card's prose, and the file says which and why. **live** companion to `docs/ops/node-pause.md`.
- `docs/design/2026-11-20-widget-redesign.md` — operator picks for the apps 1.4 widget consolidation (5 widgets, native look): the design contract every later widget card builds against, landing pick-by-pick — Cluster C1 + the Monitor/Cluster Graph retirement (PR #487), Boards B3 + the Board Status/Projects retirement (PR #492). **live**
- `docs/design/2026-11-20-widget-redesign-mock.html` — clickable mock companion to that spec (one band per widget type at real sizes, with the sticky pick bar). **live** companion.
- `docs/design/2026-11-20-carplay-mock.html` — clickable mock for the CarPlay scene (card W5g of the same spec): six designs (A–F) drawn on a real 800×480 pt CarPlay canvas with sample data, each with template/shows/actions/updates/why and a sticky pick bar. The operator picked design C "Rows with gauges" (2026-11-20); the spec's **CarPlay** section cites this file as the shipped scene's visual reference (built in PR #509). **live** companion.
- `docs/design/2026-11-19-main-orchestrator-design.md` — design for the ONE main orchestrator (named at bootstrap, default `general`): its own board is the kanban default, cluster authority except LIVE publish, daemon-owned wake-ups, `hscc orch inject` to project orchestrators, `hscc notify` alert feed (`list`/`ack`; push dropped 2026-11-19). **live** (built as cards M1–M10).
- `docs/design/2026-11-19-merge-queue-design.md` — design for the daemon-owned local merge queue (MQ1): per-repo FIFO of green PRs in its own worktree, atomic ledger, rebase → gates → force-with-lease → squash merge, conflicts/red gates handed back to the card's worker. **live**
- `docs/design/2026-10-15-template-declarations.md` — spec: template-declared `endpoints:`, `proxy:` and caps ("the template is the declaration, apply is the writer"). **live** — and the doc's own **2026-10-19 addendum** supersedes its §2.3/§6.2 details (nested `gateways.litellm.router_settings` read order, engine file moved to `src/sparkrun/plugins/litellm/engine.py`, blessed defaults).
- `docs/design/media-serving.md` — media workstream M0 design: image/video model servers on the Sparks behind the `:4000` proxy (runtime per model, OpenAI facade contract, LiteLLM registration, template `kind`, VRAM co-tenancy, NAS staging, LAN-only boundary). **proposed** — depends on the support matrix of PR #117 (`docs/design/media-serving-support-matrix.md`, branch `wt/t_165c208f`).
- `docs/design/2026-09-30-project-session-continuity.md` — design: project session continuity across surfaces (Approach 1); verified live in `docs/audits/session-continuity-verify-2026-09-30.md`. **live**
- `docs/design/2026-10-02-session-ownership-handoff.md` — design: session-ownership handoff between app and CLI ("remote-control" continuity); consumed by `docs/TASK_t_7cc2e7d5_handoff-consume.md`. **record**
- `docs/design/idle-autodown.md` — design for the serving-layer idle autodown/autoup (the autodown feature's authoritative design; operator-facing summaries also live in the root README and `hscc_daemon/README.md`). **live**
- `docs/design/idle-autodown-report.md` — implementation map for autodown (per-phase files/functions). **live** companion to the design doc.
- `docs/design/IOS_REDESIGN_2026-10-14.md` — iOS redesign spec: AppShell drawer over one NavigationStack, 17 drawer rows — the IA the app ships today. **live** (operational state in `ios-app/README.md`)
- `docs/design/ios_redesign_mockup.html` — clickable mockup companion to the redesign spec (9 frames). **live** companion.
- `docs/design/2026-08-28-ios-app-v1.md` — original iOS operator-console v1 design (three-tab IA). **superseded** by `docs/design/IOS_REDESIGN_2026-10-14.md` (drawer IA). **record**
- `docs/design/2026-08-30-audit-report.md` — "Monday readiness" audit of the v1 app. **record**
- `docs/design/telegram-optional.md` — design + implementation log for making Telegram fully optional; the removal then shipped (`docs/audits/telegram-removal-report.md`). **record**
- `docs/design/cluster-up-launch-hardening-t_be4b94ca.md` — design record for the `hscc cluster up` launch hardening (per-unit launch phase timeout via `HSCC_CLUSTER_UP_LAUNCH_TIMEOUT`, 180s default; already-serving-unit hang fix) + per-unit alias detection; shipped in v2.12.0, written before the code. **record**
- `docs/design/doctor-row-registry-t_32a06db9.md` — design record for the doctor feature-row registry: why `run_doctor()` was a merge hotspot (three hunks every feature row edited), why a registry beat `doctor_<feature>.py` splits (the row suites patch the hooks on the doctor module), and the semantics that must not move. **live**
- `docs/design/screens/` (3 PNGs below) — earlier root-tree reviewer proof shots cited by `ios-app/README.md` and the redesign spec; the current full 12-screens × light/dark capture set lives under `ios-app/docs/design/screens/`.
- `docs/design/screens/appicon-springboard.png` — springboard proof the AppIcon replaced the `H` placeholder. **live** evidence.
- `docs/design/screens/phase2-home.png` — Phase-2 home reviewer screenshot. **live** evidence.
- `docs/design/screens/phase2-home-first-run-notifications-prompt.png` — Phase-2 first-run notifications-prompt reviewer screenshot. **live** evidence.

## ops/

Operator-facing runbooks: how to turn a shipped feature on, drive it, and retire
what it replaced. (Design rationale stays in `design/`; this directory is the
"what do I actually type" layer.)

- `docs/ops/hermes-media.md` — media workstream M6 enablement recipe: how a Hermes
  instance *calls* fleet media through the `:4000` proxy — `image_gen` config-only
  (`provider: openai` + proxy `base_url`, model id sent verbatim) and `video_gen`
  via the fleet's `openai_compat` plugin — plus the revert. Client-side companion
  to `docs/design/media-serving.md`; no fleet action lives here. **live**
- `docs/ops/node-pause.md` — the node-pause overlay's operator surface: `hscc node
  pause|resume|paused` (the plan, then the `y/N` question that replaced `--apply`,
  with `--yes` for scripts and `--dry-run` to look; the every-model-runs-alone
  eligibility rule, `--force` / `--until`, refusal strings quoted from the code),
  what a
  pause leaves byte-identical (templates, `serving.json`, the applied-template
  record, kanban caps, autodown, proxy config), which loops skip a parked node
  (watchdog auto-heal, the heal breaker, autoscale, the autodown wake,
  `hscc_daemon/health.py`'s keepalive and proxy repair paths, and `hscc cluster up`
  with the post-boot, recovery and API paths that call it), why there is no
  `--include-paused`, the span rules, the orchestrator-model refusal, and how the
  daemon keeps a past `--until` as a timer. **live**
- `docs/ops/orch-waker.md` — the built-in orchestrator waker: the eight wake-worthy
  events, batch/debounce/rate rules, skip rules, `~/.hscc/orch_waker.json` config,
  `hscc orch waker status|enable|disable` verbs, state + audit schemas, the doctor
  row, and the retirement procedure for the `~/bin/orch-waker` stopgap — including
  the resolved state-file / stream-state collision that once made the built-in inert. **live**
- `docs/ops/disk-hygiene.md` — `hscc clean`: the kanban workspace hygiene (archive a
  finished card's workspace, then remove it). The five flags, why the dry run is the
  default and read-only by construction, the `would` prefix, the engine's closed
  skip vocabulary (`dirty / unpushed / stashed / unreadable / claim / archive-failed`),
  what the verb never touches (`~/dev`, `~/.hscc`, hermes config/sessions/memory, the
  board DBs, comments, attachments, unfinished cards), the archive location under the
  hermes home and how to restore one with `tar -xzf`, and the standing note that the
  gateway's weekly `~/bin/clean-mac.sh --apply` crontab line stays until the operator
  moves it. **live**

## audit/

- `docs/audit/hermes-home-joins.md` — the HERMES_HOME join classification: every
  path join on `$HERMES_HOME` in the tree, marked ROOT-intent / PROFILE-intent /
  AMBIGUOUS with the decision rule (Hermes exports `HERMES_HOME=<root>/profiles/<name>`
  under a profile session) and a severity-ranked list of the unfixed members.
  Report-only companion to the `hscc_daemon.hermes_python` venv ladder. **live**

## template-examples/

- `docs/template-examples/example-colocation.yaml` — annotated example: colocated models on one node (not a shipped layout). **live**
- `docs/template-examples/example-routing-split.yaml` — annotated example: split serving/routing (not a shipped layout). **live**

## verification/

- `docs/verification/WS3-verification.md` — verification record: native 0.17 profile API (workstream 3). **record**

## reports/

- `docs/reports/t_23bf61f3-ci-pytest-leg.md` — final state of the card that put the 12-leg pytest roster in CI (`pytest-legs`): push/PR leg selection, per-leg matrix, venv cache, the advisory/enforce switch, and the measured wall times. **record** (t_23bf61f3)

## audits/

Working directory of per-card audit/incident reports. The **live** ones below
are current operational references; everything else in `audits/` is the
**record** of a landed card — search this index (or `git log --oneline --
docs/audits/`) rather than reading them blind.

### Live references

- `docs/audits/2026-11-23-agent-surface.md` — agent-surface coverage matrix (t_4c12ff7f): every `hscc` CLI verb × the 26 `hscc-cluster` agent tools × the 12 `hscc-commands` slash commands × the 96 API routes × the bundled skill that documents it, each cell marked MISSING / STALE / INTENTIONALLY CLI-ONLY; per-tool state sources (`serving.json`, `applied_template.json`, `node_pauses.json`, `cluster.json`, daemon state files) and who else writes each; and what `hscc bootstrap` installs from where, with its UPDATE-vs-SKIP semantics measured. Findings F1–F13 + operator decisions D1–D11. **live** — the gate doc for the 2.17.4 tools/skills/profile/doctor/QA cards.
- `docs/audits/2026-11-23-main-only-surface.md` — the main-only cluster surface, measured against the real Hermes loader: how `plugins.enabled` actually resolves **per home** (no root→profile overlay; the dispatcher injects `HERMES_HOME=<profile dir>`, so 0/52 profile configs enable any plugin and the cluster tools are already absent for workers, project orchs *and* the main orchestrator profile), the six `hscc-cluster` hooks as a 6th surface with the process each one fires in (all six worker-lane, and a disabled plugin keeps neither tools nor hooks), the `_migrate_to_21` grandfather that is one migration away from granting all 52 profiles the full surface, the SOUL/skill text that still promises the tools (`install_soul.py:75-100` → 1 of 53 deployed SOULs), the concrete per-profile YAML, and four hermes-side asks. **live** — the gate doc for the main-only-surface cards (t_e98c6ad4, t_b52e70ba, t_c3a29c37, t_caf98114, t_035b1b0b). (t_1700dfc8)
- `docs/audits/2026-11-23-sparkrun-passthrough-main-only.md` — LEFT-2 of that audit, measured read-only: how the `sparkrun-hermes` `sparkrun_exec` passthrough registers and for which home (the gate lives inside `register()` via `main_orch.plugin_tools_allowed`, so the per-home `plugins.enabled` tier — not a root overlay — decides, and `profile_name` comes from `HERMES_HOME`), who can call it today (52 scanned configs = root + 51 profiles: enabled in `general` only, plus root-listed-and-inert), why the plugin registers no hooks, and that no worker, dispatcher, hook or gateway-slash path needs the passthrough (slash verbs shell `sparkrun` themselves via `cmdlib`; hooks use their own subprocess). §7 recommends the two atomic cards to cut after operator review: **F1** — `sparkrun_exec` bypasses the `cluster-guard` capacity gate *and* its audit log (`toolset != "hscc-cluster"` early-exits), so `sparkrun stop --all` / `proxy stop` reach the fleet ungated and unlogged; **F2** — four bundled `skills/` dirs in the plugin are inert while `_DESC` points models at them. §9 lists what must NOT be "fixed". **live** — the gate doc for the F1/F2 cards. (t_471788e0)
- `docs/audits/read-surface-logging_t_88abe739.md` — the `t_2c218de8` class beyond the doctor door: the ONE leak point (`autodown._load_kanban_db_or_default` → `_note_kanban_unavailable` → `daemon_ops.log`, whose file write is unconditional while only the stderr echo is gated), the six once-per-process WARN leaves that inherit it with the budget each one spends, the five read surfaces measured in a sealed-`$HOME` sandbox to create `$HSCC_HOME/daemon.log` (`autodown status [--json]`, `kanban blocked|stale|monitor`) plus two latent ones that need a pin conflict or a malformed file to fire, what is ACCEPTABLE and why, the credential-minted-by-a-read finding explicitly kept OUT of this class (§4), and the smallest fix that closes the class (§5). **live** — the gate doc for the read-surface-logging fix cards. (t_88abe739)
- `docs/audits/load-burners-sigint-trace-stage_t_5f9fab03.md` — where the `load_burners.sh` SIGINT stall dies, measured by the harness's own opt-in `HSCC_LOAD_BURNERS_TRACE` stage trace: the stalled harness stops at `in-wait` with ZERO `int-trap` markers (4/4 paired suite-traces) while its burners keep running, the rescue TERM trap runs fine, a bounded-slice wait rewrite was measured and REJECTED (~105 command boundaries, trap still never ran) — bash 3.2 never delivers that SIGINT to its own handler, so no wait/trap shape or timeout inside the POSIX harness is the fix; next probes listed. **live** — the gate doc for the load-burners SIGINT fix cards. (t_5f9fab03)
- `docs/audits/support/t_5f9fab03/stalled-harness-traces.txt` — raw `HSCC_LOAD_BURNERS_TRACE` lines of the four stalled harnesses (pids 68888/68900/16834/16835), paths scrubbed to `/Users/<operator>`. **record**
- `docs/audits/support/t_5f9fab03/stall-68888.txt` — watch_stall-style (scratch script) snapshot of a stalled harness (pre-polling build): STAT `Ss`, burners `Rs` at 100 % CPU, load 3.62/14 cpus. **record**
- `docs/audits/support/t_5f9fab03/stall-68900.txt` — the same capture for the sibling suite's stalled harness. **record**
- `docs/audits/support/t_5f9fab03/stall-16834.txt` — stalled-harness snapshot ON the rejected polling build: the 0.1 s `sleep` slice cycling, `/usr/bin/sample` stack in `__wait4`. **record**
- `docs/audits/support/t_5f9fab03/stall-16835.txt` — same as the previous on the sibling suite. **record**
- `docs/audits/README_REVIEW_2026-10-15.md` — repo-wide README audit (t_6e1e8443): measurements, per-file verdicts, the R1–R7 target-state spec this docs wave executes from. **live**
- `docs/audits/CLI_HELP_2026-10-15.md` — CLI help-surface audit: canonical command inventory + REAL EXAMPLE DATA renders (post-F1 forms). The F-lane landed via PR #57; this doc remains the reference for quoted CLI output. **live**
- `docs/audits/SPARKRUN_041_COMPAT_2026-10-20.md` — sparkrun v0.4.0 → v0.4.1 compat record: deployed topology (fork branch `feat/proxy-least-in-flight-041` @ `1aaa6479`, checkout `~/dev/sparkrun-041-verify`, venv `.venv-sparkrun-041`), live verification, rollback line (`-040` @ `~/dev/sparkrun-live`). **live** — THE current sparkrun topology pointer (supersedes the 0.4.0-era statements in `docs/audits/SPARKRUN_040_COMPAT_2026-10-19.md`)
- `docs/audits/SPARKRUN_040_COMPAT_2026-10-19.md` — sparkrun v0.3.10 → v0.4.0 compat record: the 0.4.0 deployment (`feat/proxy-least-in-flight-040` @ `b5c60edd`, checkout `~/dev/sparkrun-live`) — that topology is the **rollback line** since the 0.4.1 cutover; see `docs/audits/SPARKRUN_041_COMPAT_2026-10-20.md` for what is live. **record** (supersedes the 0.3.10-era statements in `docs/audits/DEPBUMP_GUARD_2026-10-16.md` and `docs/audits/sparkrun-service-path-t_b543e530.md`)
- `docs/audits/HEAL_CASCADE_2026-10-14.md` — heal-cascade incident: why the heal-lease / heal-breaker / launch-guard trio exists; rationale cited by `hscc_daemon/README.md`. **live**
- `docs/audits/IOS_CHAT_REPORT_2026-10-14.md` — iOS chat end-to-end overnight report; baseline for the chat path as shipped. **live**
- `docs/audits/PROXY_RELAUNCH_NOOP_2026-10-18.md` — proxy :4000 outage while the 5-min relaunch loop no-opped: root cause + fix. **live** incident record.
- `docs/audits/2026-11-05_stream-silence-incident.md` — fleet-wide stream silence with every health check green: **verdict NOT LOCATED** — all four card layers refuted on evidence; primary suspect is established-flow loss on the gateway en0 DHCP/T1 lease clock (899 s marks, 11/11 bursts land +11…+54 s before a mark), clock confidence ~75%, packet mechanism unproven; repro recipe explicitly NOT validated. **live** — supersedes the earlier "located" framing; §9 is the ready-to-card LEFT list. (t_22b546e3)
- `docs/audits/2026-11-06-hermes-0.23-compat.md` — Hermes v0.23.0 compatibility verdict + cutover checklist: Python 3.14 runtime rebuild recipe (uv-managed 3.14, optional-package freeze diff), config migrations 32 → 52 and the values they reset, and the post-cutover verify list. **live** — the v0.23.0 upgrade authority. (t_c73f4871)
- `docs/audits/2026-11-22-proxy-connection-error.md` — proxy `:4000` outage 2026-11-22, every completion 500 `Connection error` while `/v1/models` stayed 200: investigation only, no fixes applied. Establishes the corrected window (21:58–22:55 local, not 22:10), that the check-blindness is **by construction** (`check_proxy` never probes a completion, `health.py:1113`; `proxy_stall`'s through-proxy probe must **succeed** to alarm, `proxy_stall.py:247-255`), that repair verification can never pass under a `uvx` launcher (tracked wrapper PID ≠ `lsof` listener — reproduced live on a healthy proxy), and that the same predicate arms `_reap_orphan_proxy_listener` to SIGKILL a healthy proxy. Rules **out** macOS Local Network/errno 65 on unified-log evidence, and records that the `--restart` that fixed the outage truncated the only litellm-side evidence. §6 lists six atomic fix cards; §7 is the LEFT list. **live** incident record. (t_f54f735a)
- `docs/audits/DEPBUMP_GUARD_2026-10-16.md` — dep-bump fork-branch guard + `router_settings` drift check (t_39d7c915). Guard design **live**; the 0.3.10 fork-branch names it cites are the rollback line since the 0.4.0 cutover — see `docs/audits/SPARKRUN_040_COMPAT_2026-10-19.md`.
- `docs/audits/address-guard-binary-policy-t_3e6d3db7.md` — the address guard's binary policy (what counts as a real address, placeholder ranges `10.0.0.x` / `100.64.0.1`). **live**
- `docs/audits/commit-time-address-guard-t_ec2c2f95.md` — commit-time address-guard hook: mechanism + bypass rules. **live**
- `docs/audits/ci-backstop-address-guard-t_9a4b7687.md` — CI backstop for the address guard (`--tracked` leg). **live**
- `docs/audits/changelog-decomposition_t_95f1d6e1.md` — why `changelog.d/` exists and the proof it works; companion to `changelog.d/README.md`. **live**
- `docs/audits/session-continuity-verify-2026-09-30.md` — integration + end-to-end live verification of session continuity. **live** evidence for the design doc.
- `docs/audits/2026-10-31-proxy-stalls.md` — LiteLLM proxy `:4000` stall investigation (t_447dd0aa, 2026-10-30/31 episodes): investigation only, no fixes applied; §6 (t_c95a2137) names the per-request stall component (LiteLLM router retry `asyncio.sleep`) while magnitude/trigger stay unmeasured and questions (b)/(c) stay banked to follow-up cards. **live** incident record — the current authority on proxy stalls; analysis companions `docs/audits/t_c95a2137_analyze_access.py` / `docs/audits/t_c95a2137_shape_split.py`.
- `docs/audits/2026-10-31-integration-audit.md` — the A–F integration audit synthesis (t_7c43c0ba): per-area GREEN/RED verdicts, the GAP-A…F list with fix-card status, the stopgap-retirement table and the 2.15.0 gate. **live** — the release gate this doc defines is only satisfied when every area reads GREEN.
- `docs/audits/sparkrun_proxy_venv_cutover.sh` — the detached proxy-venv cutover script (stop → swap symlink → start → verify, auto-rollback on any failed gate, bounded drain wait so rollback can restart while the old proxy drains). How the live 0.4.0→0.4.1 cutover ran (cited by `docs/audits/SPARKRUN_041_COMPAT_2026-10-20.md`); edit the config block for the target tag. **live** runbook — run only from a session whose model does not ride `:4000`.
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
- `docs/audits/t_22b546e3_renew_correlation.py` — its burst-vs-DHCP-mark correlation table generator (chunked `log show`).
- `docs/audits/t_22b546e3_stream_forensics.py` — cross-profile stale-kill / stream-drop sweep.
- `docs/audits/t_22b546e3_metrics_watch.py` — engine /metrics advancement sampler (the wedge discriminator).
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
- `docs/testing/loadstress-leg.md` — the `loadstress` leg: the stop/sidecar teardown class under bounded CPU pressure, with the measured load and the anti-rot arms.
- `docs/audits/memory-path-resolution-t_7a16aed0.md` — memory endpoint profile-resolution fix.
- `docs/audits/t_c95a2137_analyze_access.py` — read-only analysis companion to `docs/audits/2026-10-31-proxy-stalls.md`: reconstructs per-connection structure of the uvicorn access capture from the client ephemeral source port.
- `docs/audits/t_c95a2137_shape_split.py` — read-only analysis companion: shape split (long-lived vs fresh connections, loopback-holder correlation, response-backlog trace) over the operator capture.
- `docs/audits/node-recovery-watchdog-t_ebbe38d1.md` — node-recovery part 1: arm the GB10 SBSA hardware watchdog cluster-wide via systemd (no BMC on the Spark; a 2h hard-freeze with no panic line was the trigger); shipped v2.14.0.
- `docs/audits/node-recovery-doctor-t_94598b3a.md` — node-recovery part 3: the doctor's node-recovery check (watchdog + panic-to-reboot) — per-node read-only probes over the sparkrun ssh path answering "would this node come back on its own?"; shipped v2.14.0.
- `docs/audits/node-recovery-notifier-mask-t_61850b24.md` — node-recovery part 5: mask `snap firmware-updater`'s crash-looping user notifier unit cluster-wide (~7k restarts measured on one Spark); shipped.
- `docs/audits/t_9a9aeab1_brew_venv_hermes_runtime.md` — the brew keg venv shipped without the hermes runtime deps (`httpx` etc.), so `project new --apply` died mid-import and left an orphan stub profile; root cause + fix design; shipped v2.14.0.
- `docs/audits/lossless-generate-pontaje-t_0f2ee405.md` — measured report of what `hscc.py generate` dropped from `ecofire-pontaje-engineer/SOUL.md` (one spec-pointer line under an emittable heading); drove the lossless-generate fix.
- `docs/audits/content-loss-list-t_5b362400.md` — why the role-SOUL doctor check's heading sweep cannot see CONTENT loss (a hand-patched paragraph under an emittable heading reads clean until the next generate deletes it) and the content-loss list that replaced it.
- `docs/audits/protocol-violation-budget-already-implemented.md` — audit proving the protocol-violation budget was already implemented upstream.
- `docs/audits/task-completions-null-taskid-t_1497a937.md` — forensics behind the 797 all-null rows in `~/.hscc/task_completions.jsonl`: the card assumed a hermes hook kwarg drift that does not reproduce; the rows were the suite's own un-patched noop tests appending to the operator's live ledgers, plus the reader audit and the `_identity`/conftest fix.
- `docs/audits/kind-allowlist-decision-t_f48f84c4.md` — decision packet behind the changelog kind gate: re-verified evidence, the blast-radius-zero proof, and the operator's resolution (hard exit-2 gate in the parse pass; closed vocabulary = the 7 canonical kinds plus `Docs`).
- `docs/audits/analyze.py` — scratch log-analysis script used by the daemon-log investigations.
- `docs/audits/overnight-2026-09-22.md` — overnight report: project session linking + digests epic.
- `docs/audits/overnight-2026-09-22.log` — raw log attached to the overnight report above.
- `docs/audits/overnight-2026-09-24.md` — overnight report: v2.1.1 cut.
- `docs/audits/home-path-enforcement-decision_t_34de41c3.md` — the decision behind `HOME_PATHS_ENFORCE = True`: the 198-finding baseline re-measured with the shipping guard, why `SANCTIONED_HOME_USERS` was NOT widened (`spark` is a real fleet account — the `ssh_user` default), why the proposed left-boundary lookbehind was REJECTED (it blinds the guard to a home path after a `\n` escape), and which placeholder each class of finding scrubbed to.
- `docs/audits/GOAL_REPORT_2026-09-26.md` — summary of the 2026-09-26 24h autonomous run.
- `docs/audits/GOAL_LEDGER_2026-09-26.md` — the same run's raw ledger (2.3 MB; append-only event log).
- `docs/audits/api-token-mint-on-read_t_cc7b6023.md` — audit of `hscc api status` minting the API bearer token on a read: **verdict VIOLATION** — the verb advertises "Read-only report" / "QR when a token exists" while its QR block called `load_token()`, which generates on first run; the mint is one line (`api_cli.py:1275`) reached by both `status` and `verify --json` (via the `api_route_sweep.py` fallback hop), and it also silently changed `hscc notify list`. The fix moves the mint behind `hscc api start` — readers get `peek_token()`, which never creates. (t_cc7b6023)

### Guards, hygiene & upstream reports (record)

- `docs/audits/BRANCH_HYGIENE_2026-10-17.md` — origin branch-cleanup audit.
- `docs/audits/worktree-hygiene-2026-10-05.md` — worktree / `wt-*` branch hygiene for the primary checkout.
- `docs/audits/DISPATCHER_HOLD_UPSTREAM_2026-10-17.md` — upstream report: the Hermes dispatcher counts benign holds as "stuck".
- `docs/audits/board-pin-t_b014c2b8.md` — audit + decision record for `hscc_daemon/board_pin.py`: measured proof that `HERMES_KANBAN_DB` outranks an explicit `board=` inside a worker/delegation fence, every board-opening surface in this repo with `file:line`, which are guarded as reads vs refused as writes, and the (unfiled) upstream issue draft. **live**
- `docs/audits/board-pin-pause-t_70c97264.md` — the pause half of the same fork hazard: `kanban_pause` shells out to `hermes kanban` with the inherited environment too, and its `_hold` (reclaim/block) plus `resume`'s unblock are WRITES under a worker pin — the decision table (scrub the read's child env / refuse the write before anything spawns, per board) and why `enforce`'s stream goes red instead of reporting "nothing to hold". **live**
- `docs/audits/orch-waker-import-leak_t_2be3b04e.md` — root cause of the order-dependent wake-ledger failures: `autodown._load_kanban_db_or_default` leaves a planted `<tmp>/hermes-agent` at `sys.path[0]`, so the next file re-imports a fake `hermes_cli` and board enumeration silently shrinks to one board. Per-test `hermes_cli` import isolation in `hscc_daemon/tests/conftest.py`. **live**
- `docs/audits/daemon-alone-run-order-dependence_t_572b4856.md` — the *other* half of that leak: hermes' REAL tree reaching a test process and surviving a closed import window, so the same `importorskip` answers one way alone and another way in-leg. Per-file alone-vs-in-leg enumeration for `hscc_daemon`, the refuted `sys.path` hypothesis, the symmetric per-test cache restore, and what the stamp may and may not claim. (t_572b4856)
- `docs/audits/kanban-registry-hermeticity_t_c11f0d88.md` — the third host-state leak in that family, this one on the flightdeck PROJECT registry: `hscc kanban stale` narrows to registered boards, so one test's fake `hscc` board cleared the filter only because the operator has an `hscc` project — green on the laptop, `IndexError` on a registry-less runner, advisory-green in CI. Measured leg-wide (1 failure in 4100 daemon tests), the pin + fence + out-of-process poisoned-registry guard that replaced it, and the M2/M3 probes that show the guard fires on a state the file's own run still calls green. **live**
- `docs/audits/support/t_572b4856/alone_sweep.sh` — the sweep that audit is built on: run a leg once through the canonical runner, then every one of its test files alone with the same argv and the same env scrub, and diff. Takes the interpreter, so one command proves it on both.
- `docs/audits/support/t_572b4856/diff_alone_vs_leg.py` — the diff half of that sweep: per-nodeid status from junit **and** per-site skip reason from `-rs`, because a skip whose reason text changed is a different answer even when the counts match. Exit 1 = at least one file answers differently alone than in-leg.
- `docs/audits/support/t_572b4856/resid_probe.py` — the pytest plugin that named the residue in that audit: it logs every `sys.path` mutation touching hermes' real tree and every first import of a hermes name with the opener's stack, then prints what is still resident at session end. `sys.path` empty + modules resident is what separates a leaked window from a leaked module cache.
- `docs/audits/support/t_b94a2b39/leg_tmp_signal_probe.py` — the harness that produced the signal matrix: drives the shipped runner through its own `HSCC_TEST_PY` seam with a stub-interpreter pack, kills it with one signal per trial, and reports per signal whether `/tmp/hscc-legs-*` survived, how many legs were orphaned, and the leaked dir's exact file sizes. `SIGPROBE_REPEATS=n` for a rate.
- `docs/audits/support/t_b94a2b39/exit_trap_signal_arm.sh` — the isolation arm: a shell holding ONLY an `EXIT` trap, one signal each, printing whether the trap ran. It ran for TERM/INT/HUP/QUIT/PIPE and not for KILL, which is what pins `SIGKILL` as the one death no handler in a shell can beat.
- `docs/audits/support/t_b94a2b39/exit_trap_signal_arm2.sh` — the second arm: does a *co-installed* trap (TERM/INT) or sitting in an external foreground command change what bash does with an untrapped fatal signal? Both measured no — so the shipped runner's `QUIT` leak is runner-state-specific and is pinned as behaviour, not explained as a bash rule.
- `docs/audits/support/t_b94a2b39/signal_matrix_before.txt` — the raw before-table the audit's §2/§5 quote (`QUIT` 3/3 leaked dirs, `KILL` 3/3, `TERM`/`INT` 0/3, 2 orphaned legs per untrapped fatal signal).
- `docs/audits/support/t_b94a2b39/pre_existing_pair.txt` — the two `/tmp/hscc-legs-*` dirs the card inherited, captured before the sweep reclaimed them: `stat` lines plus the one byte-line each log held (`7 passed in 0.2s`), which is the *test stub's* output at its default sleep — the evidence that the litter was test-harness residue and not a gate run.
- `docs/audits/board-pin-fork-t_8616f2a5.md` — the fork half of the same hazard: the dispatcher-wedge board-state gate reads board state through `hermes kanban` in a subprocess, where `board_pin`'s in-process scrub cannot reach, so the gate must hand the child an environment instead — the decision table (scrub the read / refuse the probe when the runner cannot be handed one) and what it deliberately leaves to `t_dad0da32` and `t_70c97264`. **live**
- `docs/audits/STOPGAP_REACHABILITY_t_3367cb04.md` — is the wake-dedupe stopgap deletable yet? Measured verdict: **no**. Deleting `gateway_wake_cursors` / `gateway_already_delivered` at `ae162a7f` turns 7 of `test_orch_waker_dedupe.py`'s tests red, 4 of them an actual doubled wake, because neither the deployed Hermes (`045881137d`) nor upstream `origin/main` has §3.1's `kanban_wake_claims` — the claim channel suppresses nothing and the cursor ledger is the only evidence channel. Carries the ordered deletion predicate, the two invariants a cleanup must not "simplify" away (a claim the same emitter holds is fan-out; one-claim-one-wake holds only while a claim means one destination), and the merge state that makes the cross-emitter dedupe not-yet-live. Tripwire: `hscc_daemon/tests/test_orch_waker_stopgap_load_bearing.py`. **live** (t_3367cb04)
- `docs/audits/wedge-default-board-pin-t_dad0da32.md` — decision record for the residual left by the card above: `dispatcher_wedge` rebuilt `multi` from the *slug* (`board != "default"`), so `default` was the one board the pin guard could not see; why (a) "default is a real board" was chosen over refusing, and why `multi` is read from autodown's enumeration instead of `bool(board)`. **live**
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

### memori embedding cache (record)

- `docs/audits/fastembed-cache-dir-t_317a0f30.md` — why the memori SDK's vendored `fastembed` crate wrote one 87 MB model cache per cwd (nine roots / 782 MB measured on this host), the red/green proof that `FASTEMBED_CACHE_DIR` is honoured by the compiled crate, and why the pin lives at provider import rather than in the hermes-owned gateway launchd plist. **record** (t_317a0f30)

### memori SDK shadowing (live)

- `docs/audits/memori-sdk-shadowing-t_2ab0eda1.md` — why the cloud provider dir named `memori/` shadowed the installed `memori` SDK for any repo-root/plugins `sys.path` process, the measured options (private-name load ruled out, rename chosen), the rename to `memori_cloud/`, the installer's `RETIRED_PAYLOAD` migration for stale deployed dirs, and the regression check. **live** (t_2ab0eda1)

### test environment: the thin sibling interpreter (record)

- `docs/audits/sibling-interpreter-hermes-gap_t_02c98915.md` — why 21 hermes-integration tests hard-failed under the sanctioned `p313` sibling interpreter (it is a thin pytest env with no `ruamel`/`hermes_state`), the 21-test roster, the measured rejection of dep-stuffing the sibling env, and the capability-probe skip posture now written into `scripts/run_tests.sh`. **record** (t_02c98915)
- `docs/audits/require-real-hermes-lazy-sibling-seam_t_ace3c8d5.md` — measured-clean follow-up to the same predicate weakness: the five `require_real_hermes`-gated files were swept file-alone on the CI-faithful thin venv, `p313` and canonical (0 errors; on thin the gate does **not** skip — the gated tests run) and an `sys.addaudithook` import-attempt counter shows they never enter hermes' `connect()` at all, while a control that does enter it dies at `kanban_db_connect.py:766` on thin. Records why (`list_boards()` never connects; `orch_waker` opens its own read-only URIs), the `HERMES_DELEGATED_CHILD_CONTEXT` fencing posture that masks this seam, and two instruments that produced fake zeros before the audit hook. **record** (t_ace3c8d5)

### Test-suite leg strategy (live)

- `docs/audits/hscc-bootstrap-leg-strategy-t_b9e517a9.md` — fresh `--durations=0` measurement of the `hscc-bootstrap` leg (1183 passed in 292.03 s, 14 CPUs, load 2.13/2.53): the three installer-subprocess files are 177.54 s = 60.8 %, every duration is spread rather than timeout-quantised, and the decision is **leave as-is** — share-installed-home loses test isolation, split-leg moves wall time without reducing it on the serial runner. **live** (t_b9e517a9)
- `docs/audits/merge-queue-gate-leg-timings_t_fdc2bff0.md` — the merge queue's local gate measured leg by leg (roster **1323 s** at `ps -o pri` 20, `hscc-api`+`hscc_daemon`+`hscc-bootstrap` = 76 %) against the same suite inside the live gate at **pri 4** (the `scripts` leg alone ≥1362 s vs **192 s**, ≥7.1x), the four `taskpolicy` A/Bs showing the 2.17.3 QoS elevation cannot reach the launchd `ProcessType => Background` role (`-c utility` leaves a `-b` child at 4, `-B -p $$` exits 0 and still reports 4, no `default` clamp exists on this macOS), the serial-vs-three-deep A/B at gate priority (188 s vs **96 s**, all legs green), and the ranking that follows: the plist is the 4–7x and is operator-owned, bounded leg concurrency is the ~2x that is reachable in code and is capped by the longest leg, and `MAX_LOCAL_TEST_LEGS = 3` narrowing is already live and cannot reach the target alone. **live** (t_fdc2bff0)
- `docs/audits/packed-roster-reds_t_4fc35c1f.md` — the three red legs that kept `HSCC_TEST_PARALLEL`'s default at 1, measured one by one: neither of the two mechanisms the parent card suspected (the runner's deferred reaping, the pack's own process groups) caused any of them; `hscc-api`'s `('Z', '<defunct>')` setup error is a race inside the `sidecar` fixture asserting liveness of the child its own `stop` mode tells to exit (8/30 red under bounded burners with **no packing at all**, 0/25 at ordinary priority, 0/25 for the `serve` arm under the same load), `.github/scripts` was deterministic and never ran because the pack spliced that leg's *path* into a log *filename*, and `scripts` came back green packed. **live** (t_4fc35c1f)

### Read doors that write `daemon.log` (live)

- `docs/audits/latent-read-warns_t_4dcb34e6.md` — the four latent read-path WARNs left over from the read-surface audit: a per-leaf verdict table (`board_pin._emit`, `node_pauses._warn`, `kanban_live._note_caps_unreadable` → **FIX**; `kanban_pause._warn_pin_once` read-refused → **ACCEPT**, with the call-graph proof), and the shape the fixes must keep — a nameable WARN route per module borrowed by a `silent_read()` context manager that echoes to stderr instead of appending to `$HSCC_HOME/daemon.log`, and restores the budget in `finally` because a silent mute would be a fail-open dressed as a fix. Explains why the doctor door guard is structurally blind to latent leaves (no sandbox arms a pin or plants a corrupt overlay), why the mute lives at the borrower and never inside `collect`, and which `kanban_cli.py` / `kanban_live.py` / `cards_live.py` seams must nest rather than widen. **live** (t_4dcb34e6)
- `docs/audits/leg-tmp-leak_t_b94a2b39.md` — why a leg pack leaked its `/tmp/hscc-legs-*` dir, measured signal by signal against the shipped runner (`QUIT` 3/3 leaked dirs and orphaned both legs; `KILL` 3/3; `TERM`/`INT` clean, which is why the trap-only candidate is a no-op for this bug and why `SIGKILL` — no handler at all — is the case only a sweep can reclaim), the ruling (a bounded **age**-floor sweep at 3× the queue's own cap on one invocation, so a sibling worker's live pack is never deletable without any liveness probe, plus `HUP`/`QUIT` handlers that reap the legs and re-raise to keep `$?` at `128+N`), the two premises it corrected (the card's pre-existing dirs were *test-stub* residue, provable from the `7 passed in 0.2s` line and a missing `legs.tsv`; and `find /tmp` sweeps nothing on macOS because `/tmp` is itself a symlink), and the three scope lines it refuses to cross (untrapped `SIGPIPE`, the window after the pack where `SIGINT` is restored to its default disposition, and sweeping on the serial path). **live** (t_b94a2b39)
