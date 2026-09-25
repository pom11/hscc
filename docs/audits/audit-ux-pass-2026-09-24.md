# HSCC full audit + user-experience pass — epic decomposition (2026-09-24)

Operator directive (verbatim intent): full audit + UX pass, decomposed into ATOMIC
cards — one surface per card, run serially. "A six-package sweep in one card is not
a card — it is a pile." Three parts. This doc is the durable decomposition graph;
per-card results accumulate below.

## Execution model
- ALL cards assignee=worker (catch-all implementation role), workspace_kind=worktree.
- Run SERIALLY (one card on the board at a time); report after EACH card lands, not at the end.
- Every card body carries the full non-negotiable ruleset (workers see no sibling context):
  1. Base worktree on main; merge to main; push. `dev` is deleted.
  2. commits-ahead = `git rev-list --count main..<branch>` using real branch
     `git -C .worktrees/<id> rev-parse --abbrev-ref HEAD`
  3. Worktree workspace, NEVER scratch.
  4. Suite green under BOTH interpreters: ~/.hermes/hermes-agent/venv/bin/python AND
     /Users/desac/miniconda3/envs/p313/bin/python (run_tests.sh default is hermes venv).
     Never assert live config/board in a test. Failures "pre-existing" only after measuring
     same suite on main AND branch under same interpreter.
  5. Never assert a constant's literal value; assert the constant.
  6. done != merged != pushed != deployed. After merging run
     `python3 hscc-bootstrap/install_payload.py`.
  7. pom11/hscc is PUBLIC — scan every commit for LAN addresses + secrets; scrub to
     100.64.0.1 (never drop work). No api keys/tokens ever.
  8. No working notes at repo root — they go in docs/audits/.
  9. No Claude/AI attribution anywhere.
  10. Report after each card: branch, commits-ahead-of-main before merge, suite numbers
      under BOTH interpreters, merged/pushed/deployed yes-no + the command that produced
      each number.

## STOP conditions (from operator)
- A card fails twice -> stop it, write the diagnosis, move to the next.
- A failure reported as "pre-existing" is only accepted with main+branch measurement
  under the SAME interpreter.
- Silence-swallowing (env fault rendered as data fact) is a STOP-worthy anti-pattern.
- Do NOT restart the gateway. Do not change kanban caps (2/3/2). Do not touch
  ~/.hermes/state.db. Do not push while local main is ahead of what should be pushed.
- Do not tag releases; do not change autoheal/autodown.

------------------------------------------------------------
## PART 1 — RICH CLI on every level  (10 cards)
HARD INVARIANT: `--json` stays byte-identical; non-TTY output falls back to PLAIN
(no ANSI). The daemon, scripts/ and the iOS console parse `--json` output — a single
escaped ANSI byte in a pipe breaks a watcher. Add a no-ANSI regression test per
converted command.

Baseline raw `json.dumps`/`print(` sites (operator-measured, non-test code):
  hscc-project 509 (biggest — operator's daily surface)
  hscc_daemon  190 (7 files already themed — partly done)
  hscc-bootstrap 26 | hscc-cluster 21 | hscc-roles 17 | hscc-api 6 |
  hscc-commands 1 | sparkrun-hermes 1

  RC1  hscc-project group A: project / sessions / link / map_sessions / digest
       files: flightdeck/commands/project.py, flightdeck/commands/map_sessions.py
       (sessions+link); digest runs via project.  RUN FIRST — operator's daily surface.
  RC2  hscc-project group B: message / ask / report / qa
       files: flightdeck/commands/message.py, ask.py, report.py, qa.py
  RC3  hscc-project group C: remaining command groups (~20 files in
       flightdeck/commands/: archive, daemon, daemon_install, decompose, doctor,
       hygiene, incident, ingest, init, legacy, lint, metrics, monitor, reconcile,
       release, review, roadmap, standup, start, sync, update, verify, why)
  RC4  hscc_daemon — complete the partial theme (7 files themed, 190 raw remain)
  RC5  hscc-bootstrap rich CLI (26 raw)
  RC6  hscc-cluster rich CLI (21 raw)
  RC7  hscc-roles rich CLI (17 raw)
  RC8  hscc-api rich CLI (6 raw)
  RC9  hscc-commands rich CLI (1 raw)
  RC10 sparkrun-hermes rich CLI (1 raw)

## PART 2 — FULL README REVIEW  (8 cards)
For each README: is it TRUE after (a) telegram removal, (b) main-only cutover,
(c) the Rich CLI? Does it document commands that no longer exist, or miss ones that
do? Fix or delete — a README that lies is worse than none. VERIFY every command
documented by RUNNING it. One card per README group.

  RM1  hscc-cli + install + hscc-skills   (3 stale Jun READMEs; CLI/install surface)
  RM2  memori + memori_byodb              (2 Jun stubs, 5 lines each)
  RM3  sparkrun-hermes + docs/README      (2 Jun stubs)
  RM4  root README + hscc-project README + hscc-project/docs/README (operator narrative;
       root current — verify truth post-telegram/main-only/Rich-CLI)
  RM5  hscc_daemon + scripts README       (both Sep 21 — verify currency)
  RM6  hscc-api + hscc-commands + hscc-roles README
  RM7  hscc-cluster + hscc-cluster/templates README
  RM8  ios-app + hscc-bootstrap README

## PART 3 — FULL BOOTSTRAP REVIEW  (3 cards; 12 files, 2770 lines)
Audit for correctness + SILENT FAILURE specifically: anything that swallows an
exception and reports success; anything that writes config without saying so;
anything that reverts an operator's choice. Three bugs this week were an environment
fault rendered as a fact about data — do not add a fourth.
EXPLICITLY OUT OF SCOPE: profile `memory:` / `auxiliary.compression` handling —
that is roadmap milestone `profile-provisioning`, done separately. Do NOT touch it.

  RB1  Deploy/install cluster:  install_payload.py + install_scripts.py + apply_patches.py
  RB2  Probe/config cluster:    doctor.py + detect.py + serving_gen.py + suggest_template.py
  RB3  Wiring cluster:          enable_plugins.py + ensure_review_feature.py +
       install_triggers.py + install_soul.py + preserve_autodown.py (+ hooks/cluster-guard.py)

------------------------------------------------------------
## Per-card results (filled as each lands)

### RC1 t_1ca5c6e9 — RICH CLI hscc-project group A (project/sessions/link/map_sessions/digest) — DONE
- branch wt/t_1ca5c6e9; commits-ahead of main before merge = 3
  (`git rev-list --count main..wt/t_1ca5c6e9`)
- merged YES (merge commit 447e6c8, main head 447e6c8b4aed..); pushed YES
  (`git rev-list --count origin/main..main` = 0); deployed YES (install_payload exit 0)
- suite: hermes venv 1271 passed / 1 warning; p313 1271 passed / 5 warnings
  (1254 baseline -> 1271 = +17 no-ANSI tests) — same command run under each interpreter
- --json byte-identical: `git diff main` combined zero json.dumps +/- lines;
  e2e proof at docs/audits/t_1ca5c6e9_verify_e2e.py (list --json rc0 hasANSI:False
  validJSON:True byte-identical:True)
- key design: non-TTY Rich width default 80 -> content loss (collapsed leading column /
  mid-phrase wrap) in pipes; fixed via _theme.make_console width=200 on non-terminal.
- audit note: docs/audits/t_1ca5c6e9_audit.md
- commits: 20edc8f (theme helper), d1b37ad (conversion), 2c70f47 (widen console + audit)

### RC2 t_b2d2c0ae — RICH CLI hscc-project group B (message/ask/report/qa) — DONE
- branch wt/t_b2d2c0ae; commits-ahead of main before merge = 3 (0bd4bc9, 35ea0b0, a8ca754)
  (`git rev-list --count main..wt/t_b2d2c0ae`)
- merged YES (ff a151230..a8ca754); pushed YES (`git rev-list --count origin/main..main` = 0);
  deployed YES (install_payload exit 0)
- suite: hermes venv 1256 passed/30 failed; p313 1256 passed/30 failed; branch failure-set
  == main byte-for-byte under both interpreters (diff empty)
- 15 no-ANSI regression tests; --json byte-identical via docs/audits/t_b2d2c0ae_verify_e2e.py
  (message send/read/dispatch/broadcast + ask list/show plain; ask list --json byte-identical)
- ORCHESTRATOR FLAKE FINDING: the 30 "pre-existing" test_standup failures were TRANSIENT.
  Independent run of test_standup.py on a clean detached worktree at a8ca754 =
  59 passed / 0 failed. Real in worker's window, cleared since. test_standup reads live
  ~/.hermes/hermes-agent + board -> live-state flaky. Follow-up card created to make it hermetic.
- scope: only the 4 group-B command files + 4 test files + docs/audits. qa --watch left as-is
  (live TTY re-render, not a rich target). [ask]/[dry-run]/[report] tags escaped for Rich.

### FLK t_d5780187 — make test_standup.py hermetic (flake fix) — DONE
- branch wt/t_d5780187; merged to main as 39fb82d, pushed (origin/main == main), deploy via install_payload.
- Removed live ~/.hermes/hermes-agent sys.path import + live board state reads; injected faithful in-memory/fixture
  board DB the code under test uses. test_standup.py now deterministic on clean checkout under both interpreters.
- Context: the 30 "pre-existing" failures RC2 cited were transient (independent re-run on clean main worktree:
  59 passed / 0 failed). This card makes them permanent-green. [orchestrator note]: the card's lifecycle transition
  to done was pending last observation; substantive fix confirmed on main 39fb82d.

### RC3a t_8f0031e0 — RICH CLI group C sub-a (daemon/daemon_install/start/update/init) — LANDED
- merged to main as branch head 68e680a (code commits 401d029 theme + 72dcad0 regressions + docs), pushed
  (origin/main == main, `git rev-list --count origin/main..main` = 0), deployed via install_payload.
- 2+ prior worker runs CRASHED ("pid not alive" — runs 716, 721), run 722 landed it via committed code surviving.
- scratch-workspace deviation (worker-created sub-card) — primary stayed clean; verified each push lands on main.
- Group C (23 files) decomposed by the worker into 5 serial sub-cards: a=daemon/daemon_install/start/update/init,
  b=doctor/decompose/legacy/lint/verify, c=review/roadmap/standup/why, d=ingest/sync/release/monitor/metrics,
  e=archive/hygiene/incident/reconcile. All parents=[t_36cea62f] (t_36cea62f done).

### RC3b t_d7a8d1f3 — RICH CLI group C sub-b (doctor/decompose/legacy/lint/verify) — LANDED
- merged to main as 9b956d8 (branch merge), pushed (origin/main == main, rev-list count 0),
  theme commit 6d4ad17 + regressions c808d6b on main.
- dispatcher fanned out sub-c (t_4a548958, run 724) while sub-b finalized → group-C now running approx-parallel;
  each is a correctly-scoped atomic card landing cleanly on main (not a pile), but not strict serial.

### SESSION HANDOFF — UPDATED 2026-09-25: EPIC COMPLETE (all 3 parts on main)
The 3-part Rich CLI audit + UX pass epic is DONE. Every card queued by the orchestrator has landed on main,
merged, pushed, and deployed (install_payload, gateway never restarted). Full accounting below.

PART 1 — RICH CLI (all packages themed, no-ANSI + --json byte-identical invariants met):
- RC1 t_1ca5c6e9 -> 447e6c8; RC2 t_b2d2c0ae -> a8ca754; flake-fix t_d5780187 -> 39fb82d+93558fa
- Group C (hscc-project 509 sites): sub-a t_8f0031e0 -> 68e680a, sub-b t_d7a8d1f3 -> 9b956d8, sub-c t_4a548958 -> 9d3c30b, sub-d t_c2cae6e9 -> e490e40(+be7626c), sub-e t_1b206473 -> d574b10/f640968/04bb16e. GROUP C COMPLETE.
- hscc-bootstrap t_fdaa575b -> a67de36; hscc-cluster t_e67544b1 -> cf9330f (20 no-ANSI tests, ALL GREEN both interpreters: hermes venv 422, p313 404+14skip); hscc-roles t_baef4e5f -> cb5e100; hscc-commands t_eaef219a -> b3df3ac; sparkrun-hermes t_28d1653c -> 686603c; hscc_daemon residual t_69ddd670 -> cadd4dd+26a5e1d.
- hscc-api t_8899bca2 -> closed DIAGNOSED-NOT-ACTIONABLE: hscc-api has no CLI (no __main__/argparse); its 6 json.dumps are iOS/bridge wire bytes that MUST stay byte-identical. Deliverable = docs/audits/hscc-api-no-cli-t_8899bca2.md (e038f77). Real work re-scoped by operator to t_6b994390 (hscc_daemon/api_cli.py, 28 raw prints) — running.

PART 2 — README review (all landed, verify-by-running):
- t_d90de5b6 (root+hscc-cli+install) -> f750d0b; t_d20fb383 (memori/memori_byodb/sparkrun-hermes) -> 2eb8f75; t_0ae232a4 (docs/skills/project) -> 856b045; t_8ac9e87c (bootstrap/cluster/roles/api/commands) -> 0c6ddf3; t_6b51ef5b (daemon/scripts/ios/templates/install) -> 3252d6d.

PART 3 — bootstrap correctness + silent-failure audit (all landed):
- t_24b4a9ba (1/3: install_payload/scripts/soul/apply_patches) -> 77b23b0. FOUND: apply_patches missing-dir fault + install_personality YAML no-op (silent) — fixed.
- t_ef341cfe (2/3: detect/doctor/enable_plugins/ensure_review_feature) -> 09a9405.
- t_2fdff7bd (3/3: preserve_autodown/serving_gen/suggest_template + remainder) -> 7b6ca67.
- (memory:/auxiliary.compression OUT OF SCOPE — roadmap profile-provisioning.)

NON-EPIC CARDS ALSO LANDED: t_8306890e (dispatcher worktree placement, unblocked + landed -> 2cc6d15; follow-on t_3db321fa reviewed/approved), t_d4ba2eff (telegram remnants scrub -> 018493c).
FOLLOW-ON ACTIVE CARDS — TRACKED TO DONE (2026-09-25, orchestrator "fix everything till all cards done" directive):
- t_6b994390 (hscc_daemon/api_cli.py — 28 raw stdout prints -> cli_theme, QR payload iOS wire byte-identical, 13 no-ANSI tests, suite ALL GREEN both interpreters, merged 918d9ed + report 2b951b2, pushed, DEPLOYED) -> DONE.
- t_1c1bf5f1 (--deliver desktop silent no-op — watchers notify via send_desktop_notification directly, desktop.py osascript quoting fix, commited 7d1d840, e2e verified native notifier fires; crash-looped runs 752-758 then completed run 760) -> DONE.
- t_cc8879e0 (engine-wedge load-aware verdict: BUSY != wedged; vLLM /metrics busy-signal + per-unit throughput fallback, both-direction regression tests, suite ALL GREEN both interpreters, merged da64dfd, pushed, DEPLOYED health.py byte-identical) -> DONE.
- t_8c729895 (dispatcher_wedge kanban_db shim => kanban_db_dispatch — bind spawnable helpers from hermes_cli.kanban_db_dispatch, regression test no silent narrowing, shutdown warnings gone) -> DONE. Merged 88ca315 fix via eb0a10c, pushed, DEPLOYED (install_payload 12:05; hscc start before=2 HermesPluginCompatWarnings, after=0). Suite ALL GREEN both interpreters (hermes-venv 1160 passed/52.06s; p313 1157 passed 3 skipped/53.59s). Worker report commit d6c93bf (docs/audits/t_8c729895_report.md) on main. Completed run 764 at 1790328662.
- RESULT: ALL ACTIVE HSCC CARDS DONE. Board (2026-09-25): running 0, ready 0, todo 0, blocked 0.
RELEASE: HSCC **2.2.0** published (2026-09-25). Operator requested a proper GitHub release with tag. VERSION file 2.1.1 -> 2.2.0; hscc_daemon/__init__.py __version__ aligned to "2.2.0"; CHANGELOG [Unreleased] -> [2.2.0] expanded to cover full epic scope (Rich CLI all packages, README review, bootstrap silent-failure audit) + all 4 bug fixes. Release commit c3206e2, annotated tag v2.2.0 (pushed), payload redeployed (install_payload, backups .bak-20260925-133349), daemon restarted clean at 1:35PM (PID 15527, all streams OK, no warnings). GitHub release: https://github.com/pom11/hscc/releases/tag/v2.2.0 . Also removed 4 untracked scratch leftovers from primary (t_8306890e_review.md, hscc_deliver_test.py, hscc_e2e_delivery_test.py, verify_fix.py) per operator instruct to delete.
PRIMARY DIRT resolved: t_1c1bf5f1's tracked dirt (CHANGELOG/README/desktop.py/scripts) was committed+merged by that worker into main (7d1d840). Remaining untracked scratch artifacts (docs/audits/t_8306890e_review.md, scripts/hscc_deliver_test.py, scripts/hscc_e2e_delivery_test.py, verify_fix.py) were DELETED 2026-09-25 per operator instruction. Primary checkout is clean (no untracked, no modified tracked files).
