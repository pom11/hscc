# HSCC Overnight Report — Project Session Linking + Digests Epic
2026-09-22/23 · orchestrator (hscc-orch) · report written while dev was green

Scope: the 4-card serial epic "project session linking + digests" — discovery,
link/unlink, digest, and chat-seed. Each card: implemented, tested, branch
merged INTO dev, dev pushed, and the canonical 8-package suite ALL GREEN AFTER
the merge — measured by the orchestrator, not taken on the worker's word.

Canonical test command (used for every measured number below):
  HSCC_TEST_PY=/Users/desac/miniconda3/envs/p313/bin/python bash scripts/run_tests.sh
Every suite log is a real file: /tmp/run_tests_dev.log,
/tmp/run_tests_overnight_dev.log, /tmp/run_tests_card2_mrg.log,
/tmp/run_tests_card3_mrg.log — all EXIT=0.

Dev baseline BEFORE any card (dev@e5ce828): bootstrap 248, commands 59, roles
100, cluster 384(14 skip), project 1209, daemon 1121, sparkrun 8, api 787.

-------------------------------------------------------------------------------
CARD 1/4 — t_8736f0d7 — discovery reads binding store
-------------------------------------------------------------------------------
Branch:            wt/t_8736f0d7 (worktree tmp/integrate2)
Commits ahead of dev before merge: 6
                   (git rev-list --count dev..tmp/integrate2 = 6, dev@e5ce828)
                   feature commits: 16a8c86 (feat discovery honors binding store),
                   addcc4c (tests), 0f036ef (runtime probe only on real path),
                   927b718 (harness fix) + 2 merge commits (cb6db42, 179f71c)
Suite dev@e5ce828: 8/8 ALL GREEN (run_tests_dev.log)
Suite branch:      ALL GREEN 8/8 (run on tmp/integrate2@179f71c, proc_6b6303a15302)
Suite dev AFTER merge (dev@179f71c): ALL GREEN EXIT=0 — bootstrap 248, commands
                   59, roles 100, cluster 384(14skip), project 1209, daemon 1121,
                   sparkrun 8, api 787 (proc_4a9efcf8e6fb, run_tests_overnight_dev.log)
Merged into dev:   YES — merge commit 179f71c
Pushed:            YES — origin/dev == 179f71c
+ Harness fix (scripts/run_tests.sh): `env -u HERMES_DELEGATED_CHILD_CONTEXT`
  around pytest, committed as 927b718 (message explains the HERMES_DELEGATED_
  CHILD_CONTEXT cause), landed via 179f71c.

NOTE on the operator's opening instruction ("run_tests.sh is modified and
uncommitted in the primary"): that premise was already stale by the time this
report is written. The fix was committed (927b718) and pushed (via 179f71c)
during card 1. The primary working tree is CLEAN — no uncommitted modification,
nothing blocking merges. No extra commit was fabricated.

-------------------------------------------------------------------------------
CARD 2/4 — t_8c5363c9 — project link / unlink / link --list
-------------------------------------------------------------------------------
Branch:            wt/t_8c5363c9
Commits ahead of dev before merge: 1
                   (git rev-list --count 179f71c..dc25fa4 = 1, dev@179f71c)
Suite dev@179f71c: ALL GREEN (bootstrap 248, ..., project 1209)
Suite branch:      1229/1229 hscc-project (worker measured)
Suite dev AFTER merge (dev@dc25fa4): ALL GREEN EXIT=0 — project 1229 (+20),
                   all other packages identical (proc_dd7aa46ca515,
                   run_tests_card2_mrg.log)
Merged into dev:   YES — dc25fa4 (fast-forward; wt/t_8c5363c9 strict linear
                   descendant of dev, content identical to no-ff)
Pushed:            YES — origin/dev == dc25fa4
Landed: flightdeck/core/bindings.py (shared canonical binding store:
load/save/bind/unbind/list), session_discovery + map_sessions.apply_mapping
merge into canonical mapping.json, `hscc project link`/`link --list`/`unlink`,
20 new tests (test_bindings.py).

-------------------------------------------------------------------------------
CARD 3/4 — t_7a771891 — project digest <name>
-------------------------------------------------------------------------------
Branch:            wt/t_7a771891
Commits ahead of dev before merge: 2
                   (git rev-list --count dc25fa4..f7ca7d0 = 2:
                   f3b5b60 digest core, f7ca7d0 digest cmd + tests)
Suite dev@dc25fa4: ALL GREEN (project 1229)
Suite branch:      1240/1240 hscc-project (worker measured, worktree + dev)
Suite dev AFTER merge (dev@f7ca7d0): ALL GREEN EXIT=0 — bootstrap 248, commands
                   59, roles 100, cluster 384(14skip), project 1240 (+11),
                   daemon 1121, sparkrun 8, api 787 (proc_e17146b6d70c,
                   run_tests_card3_mrg.log)
Merged into dev:   YES — f7ca7d0 (fast-forward; wt/t_7a771891 strict linear
                   descendant of dc25fa4)
Pushed:            YES — origin/dev == f7ca7d0
Landed: flightdeck/core/digest.py (bounded, read-only archive synthesis: per
session title/date-range/message-count/bounded decided-outcome extract/resume
line, hard-capped), `hscc project digest <name>` subcommand, runtime_error vs
genuine-no-history, 11 new tests (test_digest.py).
Worker reported a single hscc-roles failure in the full suite; the orchestrator's
independent post-merge measurement shows roles 100/100 under the orch context —
that failure is a profile-env artifact (missing that profile's profiles dir),
not a code defect, and reproduces every run in that context only.

-------------------------------------------------------------------------------
CARD 4/4 — t_c286ac98 — project chat seeds empty orch session with digest
-------------------------------------------------------------------------------
Branch:            wt/t_c286ac98
Commits ahead of dev before merge: 1 feature commit on the branch (c944636);
                   merged onto dev via an explicit no-ff merge af2f1b8, so dev
                   advanced by 2 commits (c944636 + af2f1b8)
                   (git rev-list --count f7ca7d0..af2f1b8 = 2)
Suite dev@f7ca7d0: ALL GREEN (project 1240)
Suite branch:      7/7 test_chat_seed, 33/33 test_project, digest+lifecycle 44/44
                   (worker measured); hscc-project 1247/1247
Suite dev AFTER merge (dev@af2f1b8): ALL GREEN EXIT=0 — bootstrap 248, commands
                   59, roles 100, cluster 384(14skip), project 1247 (+7),
                   daemon 1121, sparkrun 8, api 787 (proc_f3c978a5e9f8,
                   run_tests_card4_mrg.log)
Merged into dev:   YES — af2f1b8 (no-ff merge commit)
Pushed:            YES — origin/dev == af2f1b8
Landed: seed_session_with_digest() in project_lifecycle.py + _seed_empty_
orchestrator() in cmd_chat: when the project's orchestrator session is EMPTY
(freshly-created or message_count==0) AND the project has a digest, append the
digest verbatim as the opening user message so the operator lands already
knowing the history. Idempotent (never re-seeds a non-empty session), never
auto-resumes telegram (resume path bypasses seeding), honest messaging
("seeded session ... (N threads, M messages)" / "already has history — not
re-seeding"), env faults render as exit 3 never as empty/no-history. 7 new
tests (test_chat_seed.py).
The worker's own run_tests.sh reported 5/8 fully green + hscc-roles (1) and
hscc-cluster (5) as env artifacts; the orchestrator's independent measurement of
the SAME dev commit shows ALL 8 GREEN (roles 100, cluster 384) — confirming those
reported failures are backdrop-env artifacts of the backend-engineer profile
(missing profiles dir / live-hermes hermes_cli drift), not code defects. Neither
package is touched by this card's diff.

-------------------------------------------------------------------------------
FINAL STATE — all four cards DONE, merged, pushed, independently measured green
-------------------------------------------------------------------------------
dev == origin/dev == af2f1b8 (clean working tree).
Full chain on dev: e5ce828 -> 16a8c86 -> addcc4c -> 0f036ef -> cb6db42 ->
927b718 -> 179f71c (CARD 1) -> dc25fa4 (CARD 2) -> f3b5b60 -> f7ca7d0
(CARD 3) -> c944636 -> af2f1b8 (CARD 4).
Every card's canonical suite AFTER its merge was independently re-run by the
orchestrator and came back ALL GREEN EXIT=0 on 8 packages:
  card1 @179f71c, card2 @dc25fa4, card3 @f7ca7d0, card4 @af2f1b8.
The delegation-marker harness fix (scripts/run_tests.sh env -u
HERMES_DELEGATED_CHILD_CONTEXT) is on dev as 927b718 and pushed.
Supervisor cron (hscc-overnight-supervisor) removed — epic complete.
No tags, no main push, no gateway restart, no kanban-cap/autoheal/autodown
changes, no ~/.hermes/state.db writes. Nothing force-pushed.
REDACTED: no tailnet/IP/token values appear in any of these diffs or logs
(verified per worker/orchestrator grep), consistent with repo policy.


----------------------------------------------
FOLLOW-UP — 2026-09-23: digest content bug fix (card t_acff6dc3)
----------------------------------------------
Operator reported `hscc project digest ecofire-bc` content is wrong (card 3's
digest): correctly bounded, but 39 lines are raw tool-call JSON
(chatcmpl-tool-...) instead of a human summary, and the per-session message
count disagrees with `project sessions`. Since card 4 seeds the orchestrator
session with this digest, the tool noise primes the session instead of "here
is where the waste-reversal design landed".

CAUSE (orchestrator diag, verified 2026-09-23): digest.py parses ARCHIVE
MARKDOWN text, splitting **user**/**assistant** headers. When an assistant
message made a tool call, the archive renderer inlines the raw tool_calls JSON
as text into the assistant block -> collected as "assistant" and leaked.
state.db messages has clean columns (role/tool_calls/tool_name/active/
compacted); 60/66 active assistant rows in 20260708 have tool_calls set; ZERO
have chatcmpl-tool- in content (it lives in the tool_calls column). Compaction
notice ([CONTEXT COMPACTION -- REFERENCE ONLY]) is active=1 role=assistant, so
it also leaks.

COUNT RECONCILIATION: digest read `message count:` from the archive FILE
HEADER; `project sessions` reads sessions.message_count = count(active=1).
Session 20260708 archive header says 313 but state.db has only 149 active rows
(313 total incl 164 compacted=1) -> the archive header counted superseded
rows. Measured active-row counts: [27, 71, 270, 318, 149, 129] => total 964
(not 1128). digest@sessions disagree exactly on that one session.

FIX SPEC (card t_acff6dc3 -> backend-engineer, worktree wt/t_acff6dc3):
read decision bodies + message_count from state.db messages (active=1) via
Hermes SessionDB read-only; whitelist role in (user,assistant), exclude
assistant-with-tool_calls, exclude content starting '[CONTEXT COMPACTION';
count = count(active=1). Reconciles with `project sessions` by construction.

BEFORE (captured via digest_core.build_digest under Hermes venv):
  12,039 chars / 73 lines / 39 occurrences of 'chatcmpl-tool-' / total 1128.
AFTER (measured on dev@c425267 via digest_core.build_digest+format_digest
under the Hermes venv, command: PYTHONPATH=hscc-project
~/.hermes/hermes-agent/venv/bin/python -c "build_digest('ecofire-bc')"):
  12,039 chars / 69 lines / 0 occurrences of 'chatcmpl-tool-' / 0 'CONTEXT
  COMPACTION' / total_messages 964 / runtime_error None.
  Per-session counts: 20260717=129, 20260708=149, 20260707=318,
  20260706=270, 20260703=71, 20260702=27 -> sum 964. All six sections now
  read as human summaries (framing user ASK + assistant CONCLUSIONS + end
  state + resume line). Worker merged+pushed as commit c425267 (branch
  wt/t_acff6dc3, fully merged: git rev-list --count dev..wt/t_acff6dc3 = 0).
  Commit c425267: "feat(digest): read decision content + message counters
  from state.db via SessionDB" (4 files, +349/-106: digest.py, project.py,
  test_digest.py +174, test_chat_seed.py).

  INDEPENDENT FULL SUITE on dev@c425267 (command:
  HSCC_TEST_PY=/Users/desac/miniconda3/envs/p313/bin/python
  bash scripts/run_tests.sh -> SUITE EXIT=0): bootstrap 248, commands 59,
  roles 100, cluster 384 (14 skip), project 1249 (+2 new digest tests; was
  1247), daemon 1121, sparkrun 8, api 787. ALL 8 GREEN.
  origin/dev == local dev == c425267 (pushed, 0/0 ahead-behind).
