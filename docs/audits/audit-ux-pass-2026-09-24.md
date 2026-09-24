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
(append each card's report here: card id, branch, commits-ahead, suite numbers both
interpreters, merged/pushed/deployed, command that produced each number)
