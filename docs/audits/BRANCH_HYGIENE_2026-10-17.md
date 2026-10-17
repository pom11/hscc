# Branch hygiene audit — origin cleanup — 2026-10-17

Task t_f47f89d2 (hscc board). Operator-approved hygiene pass over `origin`
(pom11/hscc). **Ordering: this report is committed BEFORE any deletion; the
final-state section is appended after deletion, push-only.** Docs-only change:
no changelog fragment needed (this PR adds no behavior).

## Method (standing rule, stranding lesson)

A branch is deleted ONLY with per-commit proof all its content is contained in
`origin/main` (tip `baf98413`, v2.9.x line). Two accepted proofs:

1. **Ancestry** — `git merge-base --is-ancestor origin/<b> origin/main` = 0, plus
   empty three-dot tree delta (`git diff --stat origin/main...origin/<b>` empty).
2. **Patch-id** — every non-merge commit ahead of main carries `-` in
   `git cherry -v origin/main origin/<b>` (patch-id equivalent exists in main —
   the rule for cherry-picked/rebased tips; merge-base ancestry alone is NOT
   used as proof for those). Empty-diff commits skipped by cherry re-verified
   with `git diff-tree` as content-empty. Merge commits ahead must be non-evil:
   `git show --cc <merge>` empty ⇒ the merge introduced no unique content.

Strengthening check applied on the borderline case (`wt/t_cfdcefa9`): byte-exact
comparison of the pick `ed678438` (on main) vs branch tip `c272e49f` — all 502
change-lines identical (only hunk context differs), so patch-id mismatch is a
pure rebase artifact.

Open-PR heads enumerated FIRST (`gh pr list --state open`) and excluded — the
card's #37-#40 list was stale; live heads are #34-#41 (below).

## Summary

- 165 origin branches audited (inventory `git for-each-ref refs/remotes/origin`).
- **DELETE 65** branches — proven fully contained in main:
  - 23 by ancestry (tip reachable from main, tree delta empty)
  - 15 by patch-id (all commits `-` in git cherry, no merges)
  - 26 merge-carried (every non-merge commit `-` in cherry;
    all merge commits non-evil by `git show --cc`)
  - 1 byte-identical pick (`wt/t_cfdcefa9`)
- **KEEP 90** — not provably contained (real unlanded commits
  or unverifiable picks). Left in place and reported; several flagged as
  possible stranding (see KEEP flags).
- **PROTECT 8** — open-PR heads, untouchable.
- **DEFER 1** — `backup/*` ref: provably merged, but kept:
  an explicitly-named backup ref is a deliberate safety net, not debris.

Deletion is `git push origin --delete` (normal push, no force, no rewrites,
no tag deletions). Every deleted tip SHA is recorded below and anchored locally
as `refs/hygiene-2026-10-17/<branch>` in the main checkout before deletion, so
any branch is one `git branch <name> <sha>` away without touching origin.

## PROTECT — open-PR heads (live `gh pr list --state open` at run time)

- `fix/nav-graph-2026-10-15` — PR #38: iOS nav lane: drawer dead-end fix, pill probe, nav-graph walker gate + sub-page back fix
- `release/2.10.0` — PR #41: release: 2.10.0
- `wt/t_1e54481a` — PR #35: dispatcher_wedge: a parked review row must not wedge the board; review work spawns only wh
- `wt/t_39d7c915` — PR #40: dep-bump guard: sparkrun bumps rebase the fork branch; proxy config drift WARN (both shape
- `wt/t_6e1e8443` — PR #37: docs(audit): README review — verdicts for all 22 READMEs + R1-R7 target-state spec
- `wt/t_ac795424` — PR #36: uniform_pool: every hermes provider behind the proxy; provider_routing doctor guard
- `wt/t_b7458cf4` — PR #34: docs(spec): template declares routing — endpoints:, proxy:, caps; apply writes it all
- `wt/t_e286c4b9` — PR #39: cron installers derive HERMES_SCRIPTS from the home cron binds (dep-watcher + escalate-wat

## DELETE — proven merged (per-branch evidence)

### By ancestry (tip is an ancestor of origin/main; three-dot tree delta empty)

| branch | tip | date |
|---|---|---|
| `cli-help-2026-10-15` | `1383b98e` | 2026-10-17 |
| `feat/cluster-monitor-live` | `983b10ab` | 2026-10-15 |
| `feat/ios-redesign-2026-10-14` | `367ab399` | 2026-10-15 |
| `feat/repl-inject-2026-10-13` | `997c64f9` | 2026-10-13 |
| `feat/screens-harness` | `5b04a8b7` | 2026-10-15 |
| `feat/tmux-aware-chat-2026-10-13` | `4cabd5cb` | 2026-10-13 |
| `fix/heal-cascade-2026-10-14` | `852b531a` | 2026-10-15 |
| `fix/ios-chat-e2e-2026-10-13` | `e70111ac` | 2026-10-14 |
| `fix/proxy-outage-2026-10-11` | `223667c0` | 2026-10-12 |
| `wt/t_1897550c` | `1b9dc3e8` | 2026-10-14 |
| `wt/t_25aed701` | `361aa1c4` | 2026-10-14 |
| `wt/t_2a29a7ab-rebased` | `5ac0cfbf` | 2026-10-15 |
| `wt/t_3e6d3db7-gated` | `91ba3fe2` | 2026-10-09 |
| `wt/t_69a1c9f2-gated` | `7b12dbd7` | 2026-10-09 |
| `wt/t_873590dd` | `c34798d2` | 2026-10-10 |
| `wt/t_9a4b7687-gated` | `7f1ca182` | 2026-10-09 |
| `wt/t_a166be3d` | `8906e151` | 2026-10-17 |
| `wt/t_a9477712` | `76930b74` | 2026-10-10 |
| `wt/t_a9477712-gated` | `76930b74` | 2026-10-10 |
| `wt/t_aeb982bc` | `b97b4b48` | 2026-10-17 |
| `wt/t_db6a32da` | `98cff42e` | 2026-10-15 |
| `wt/t_f64706b4` | `ec9de92a` | 2026-10-15 |
| `wt/t_fc307a51` | `1383b98e` | 2026-10-17 |

### By patch-id (git cherry: every commit `-`; no unique merges)

| branch | tip | date | commits ahead | per-commit twin in main |
|---|---|---|---|---|
| `feat-model-tiering` | `cb9285a9` | 2026-07-01 | 1 | `cb9285a9`→`None` |
| `t_fcc0eaa6` | `23679b7b` | 2026-08-06 | 31 | all 31 `-` in `git cherry -v` (matched count 31) |
| `wt/t_06220610` | `c0f4d1b3` | 2026-07-01 | 1 | `c0f4d1b3`→`None` |
| `wt/t_12f3c8a8` | `81e60521` | 2026-10-09 | 8 | all 8 `-` in `git cherry -v` (matched count 8) |
| `wt/t_12f3c8a8-gated` | `81e60521` | 2026-10-09 | 8 | all 8 `-` in `git cherry -v` (matched count 8) |
| `wt/t_188d37e4` | `22d38be8` | 2026-08-06 | 31 | all 31 `-` in `git cherry -v` (matched count 31) |
| `wt/t_20261016_f1_cli_help_f1` | `ff20706c` | 2026-10-16 | 2 | `ccdf9a0c`→`7166b9e3`; `ff20706c`→`eb1b272a` |
| `wt/t_20ddb71e` | `82703d38` | 2026-08-06 | 32 | all 32 `-` in `git cherry -v` (matched count 32) |
| `wt/t_36580090` | `01b1dea7` | 2026-10-16 | 1 | `01b1dea7`→`22736994` |
| `wt/t_71aec48c` | `01b832fc` | 2026-08-06 | 32 | all 32 `-` in `git cherry -v` (matched count 32) |
| `wt/t_7b0f65c8` | `ccdf9a0c` | 2026-10-15 | 1 | `ccdf9a0c`→`7166b9e3` |
| `wt/t_875c5019` | `05c9d0a2` | 2026-10-17 | 2 | `175bfd4d`→`cb352d40`; `05c9d0a2`→`73880a84` |
| `wt/t_8d2eb400` | `e161fc59` | 2026-10-16 | 2 | `ccdf9a0c`→`7166b9e3`; `e161fc59`→`79723b91` |
| `wt/t_90082309` | `fb715d0b` | 2026-10-17 | 3 | `2f81d5d5`→`cb352d40`; `585e4540`→`73880a84`; `fb715d0b`→`9babaf0e` |
| `wt/t_c84a59b1` | `a9040844` | 2026-08-06 | 31 | all 31 `-` in `git cherry -v` (matched count 31) |

### Merge-carried (all non-merge commits `-` in cherry; every merge non-evil)

`git show --cc <merge>` empty for each merge ahead ⇒ zero unique merge content.

| branch | tip | date | non-merge matched | merges (all non-evil) |
|---|---|---|---|---|
| `feat/bootstrap-telegram-optional` | `da5a7f39` | 2026-08-21 | 91 | 24 |
| `feat/hscc-api` | `f1069aaa` | 2026-08-21 | 97 | 24 |
| `feat/ios-app` | `1aec5719` | 2026-08-21 | 98 | 24 |
| `feat/project-orchestrators` | `b3c39bb0` | 2026-08-21 | 90 | 24 |
| `wt/t_083b6cf8` | `b5a08e18` | 2026-08-07 | 51 | 12 |
| `wt/t_0d5ef51a` | `31ff2fea` | 2026-08-14 | 73 | 19 |
| `wt/t_13f077e4` | `ef17fa69` | 2026-08-07 | 42 | 7 |
| `wt/t_16dcceb4` | `a1d6ee53` | 2026-08-07 | 46 | 10 |
| `wt/t_2647a396` | `c5757fd9` | 2026-08-09 | 69 | 18 |
| `wt/t_2924a905` | `f99444b6` | 2026-08-07 | 54 | 12 |
| `wt/t_2a29a7ab` | `01e1142d` | 2026-10-15 | 3 | 1 |
| `wt/t_30b1e1ee` | `70798636` | 2026-08-07 | 48 | 11 |
| `wt/t_495aca7f` | `f2affc07` | 2026-08-07 | 52 | 12 |
| `wt/t_5b0f4d2d` | `751e2d56` | 2026-08-07 | 41 | 6 |
| `wt/t_5d1d33d7` | `0c3096cf` | 2026-08-08 | 63 | 16 |
| `wt/t_7beb3112` | `d9fa3b46` | 2026-08-07 | 41 | 6 |
| `wt/t_864329ca` | `0d609da3` | 2026-08-07 | 41 | 6 |
| `wt/t_939abe5f` | `91117275` | 2026-08-07 | 53 | 12 |
| `wt/t_9bba450f` | `4bccaf1e` | 2026-08-06 | 35 | 1 |
| `wt/t_ad411703` | `a5992014` | 2026-08-06 | 35 | 1 |
| `wt/t_c29c6a82` | `938ae3cc` | 2026-08-08 | 57 | 12 |
| `wt/t_c5bdcfb3` | `f3936a51` | 2026-08-08 | 57 | 12 |
| `wt/t_c8ef006a` | `4494b182` | 2026-08-08 | 57 | 12 |
| `wt/t_ccd21e9b` | `a74a1db8` | 2026-08-07 | 47 | 10 |
| `wt/t_f256ce01` | `a6526604` | 2026-08-08 | 57 | 12 |
| `wt/t_f7740ae0` | `c0384f03` | 2026-08-08 | 66 | 17 |

### Byte-identical pick

- `wt/t_cfdcefa9` tip `c272e49f`: single commit `c272e49f` vs main pick `ed678438` —
  502/502 change-lines byte-identical (context-only drift). Body-named
  lane-pick confirmed.

### Deferred (provably merged but kept)

- `backup/pr19-pre-20260818-090812` — all content commits `-` in cherry, merges
  non-evil; still kept deliberately: the name declares it a pre-rewrite backup.

## KEEP — not provably contained (left in place)

Every KEEP branch has at least one ahead-of-main commit with no patch-id twin in
main (or an unverifiable pick / evil merge). Per standing rule these are NOT
deletable. Categories:

### Flagged: real unlanded work — STOP, do not treat as debris (11)

- `feat/fix-aux-ip` (`6e436e2f`, 2026-06-18, 3 unmatched ahead): June address-config experiment; unmerged AND commits ADD real cluster addresses to tracked files — must never be landed as-is (superseded)
- `t_188e1245-detector-wedge-silent` (`3d441276`, 2026-08-29, 2 unmatched ahead): 1 unique commit e3240f8c-subject twin differs byte-wise from main pick; unverifiable — KEEP
- `t_c0953d4c` (`23cc450f`, 2026-08-29, 3 unmatched ahead): 1 unique commit (chat unsent/retry) twin differs byte-wise from main pick 634f308e — unverifiable
- `wt/t_160d306f` (`1eeaca55`, 2026-10-17, 3 unmatched ahead): dispatcher_wedge held-vs-wedged fix (3 commits, 2026-10-17) shipped+pushed, NO open PR — needs PR/review, not deletion
- `wt/t_1b7b3166` (`363be842`, 2026-10-10, 2 unmatched ahead): address-boundary safety-property fix + leak-scan fix (2 commits) — absent from main
- `wt/t_1b7b3166-gated` (`363be842`, 2026-10-10, 2 unmatched ahead): same work as wt/t_1b7b3166 (identical tip)
- `wt/t_38fd345f` (`cc4f4efe`, 2026-10-10, 5 unmatched ahead): CI redactor round-4 D0/D1 closure (5 commits) — subjects absent from main entirely
- `wt/t_38fd345f-gated` (`cc4f4efe`, 2026-10-10, 5 unmatched ahead): same round-4 work as wt/t_38fd345f (identical tip)
- `wt/t_706129d8` (`472995f5`, 2026-10-16, 17 unmatched ahead): picked into PR #38 head EXCEPT 2 commits (changelog fragment + nav_graph_check parse fix) — re-audit after #38 merges
- `wt/t_ecc3f190` (`506b4438`, 2026-10-10, 4 unmatched ahead): guard trailing-byte artefact refusal + tests (4 commits) — absent from main
- `wt/t_ecc3f190-gated` (`506b4438`, 2026-10-10, 4 unmatched ahead): same work as wt/t_ecc3f190 (identical tip)

### Stacked on open PR heads — re-audit after those merges (3)

- `wt/t_3aadd2c0` (`4893df0a`, 2026-10-16): tip is ANCESTOR of wt/t_e286c4b9 (open PR) — re-audit after merge
- `wt/t_5ed5d281` (`d6402e7f`, 2026-10-16): tip is ANCESTOR of fix/nav-graph-2026-10-15 (open PR) — re-audit after merge
- `wt/t_e5d24c6d` (`97ab3214`, 2026-10-16): tip is ANCESTOR of fix/nav-graph-2026-10-15 (open PR) — re-audit after merge

### iOS audit family (Sept 2026) (40)

Each `audit/<view>-t_*` branch carries per-view audit-report docs and view fixes
that never landed (per-branch unmatched commits, e.g. `audit(t_03f8192d): full
ActivityFeedView findings`), plus the common unlanded harness pair (`test(ios):
commit sanitized live API fixtures...`, `test(ios): replace mirrored decode
validator...`) which is NOT byte-identical to its main twins (ab5214ca/279778d5).
Three still have LIVE local worktrees (t_18aefdb7, t_806756e4, t_e13c8c2b).
Collectively: unmerged audit corpus — flag for an operator decision (harvest or
keep), not hygiene deletion.

| branch | tip | date | unmatched |
|---|---|---|---|
| `audit/a11y-t_4cedf275` | `d16a9b67` | 2026-09-03 | 8 |
| `audit/activityfeed-t_03f8192d` | `f06fdcae` | 2026-09-02 | 10 |
| `audit/approvals-t_48bbf99b` | `656083df` | 2026-09-02 | 6 |
| `audit/autodownview-t_9b678f46` | `d0894391` | 2026-09-03 | 6 |
| `audit/banner-t_4889e978` | `13b25e17` | 2026-09-03 | 8 |
| `audit/chat-attachments-t_3fc4801c` | `64fea66f` | 2026-09-04 | 8 |
| `audit/chat-retry-t_3ae70b8c` | `169c4385` | 2026-09-03 | 10 |
| `audit/chat-stop-t_6efb89ff` | `9d1b85ba` | 2026-09-03 | 9 |
| `audit/chatreadability-t_d90753cd` | `eb50e1f6` | 2026-09-03 | 8 |
| `audit/deeplink-harness-t_5320945e` | `8c8009fb` | 2026-09-04 | 10 |
| `audit/deeplinks-t_136762f3` | `0079761a` | 2026-09-04 | 10 |
| `audit/empty-states-t_f57347f7` | `851da45a` | 2026-09-04 | 10 |
| `audit/errorcopy-t_b89d0b9a` | `48051408` | 2026-09-03 | 9 |
| `audit/fleetmonitor-t_a1aaba69` | `9c109374` | 2026-09-04 | 11 |
| `audit/fleetview-t_806756e4` | `15a1c612` | 2026-09-02 | 8 |
| `audit/history-t_b5ce7935` | `0d08af25` | 2026-09-04 | 10 |
| `audit/liveactivity-rehydration-t_7a0e9c4e` | `4f92aa79` | 2026-09-03 | 8 |
| `audit/logs-t_2eda26a6` | `898454cb` | 2026-09-03 | 9 |
| `audit/memorypicker-t_c5f20f12` | `6a25dd24` | 2026-09-03 | 8 |
| `audit/memoryview-t_1869d327` | `11761926` | 2026-09-02 | 6 |
| `audit/nodetopology-t_cbb6c6cc` | `db75b198` | 2026-09-03 | 8 |
| `audit/notify-operator-t_0454eb56` | `a7a03ca7` | 2026-09-03 | 8 |
| `audit/offline-queue-t_42ba90d2` | `f23d675b` | 2026-09-04 | 11 |
| `audit/opsview-t_ca19400d` | `186d5fb4` | 2026-09-03 | 8 |
| `audit/profileeditor-t_7e6dcf50` | `93ad59e4` | 2026-09-03 | 7 |
| `audit/searchview-t_51bad645` | `efe94bc4` | 2026-09-03 | 6 |
| `audit/servingcontrol-t_e13c8c2b` | `ed246163` | 2026-09-04 | 10 |
| `audit/sessionhistory-t_3359b983` | `2f71179b` | 2026-09-03 | 6 |
| `audit/sessions-t_bf2394ab` | `07b518cf` | 2026-09-02 | 6 |
| `audit/settings-multi-cluster-t_8f30cf67` | `defa7600` | 2026-09-04 | 11 |
| `audit/settingsview-t_1223ea1a` | `e1c8cff1` | 2026-09-02 | 6 |
| `audit/show-worker-diffs-t_178cb1a0` | `759eb54d` | 2026-09-03 | 9 |
| `audit/show-worker-diffs-t_cb93feee` | `10862d75` | 2026-09-04 | 10 |
| `audit/slashpalette-t_6f91edd3` | `e88d4d42` | 2026-09-03 | 8 |
| `audit/slashpreview-t_26c97b40` | `3822be0f` | 2026-09-04 | 10 |
| `audit/slashpreview-t_8ba85648` | `53f71c92` | 2026-09-03 | 8 |
| `audit/templatedetail-t_6060f92b` | `a92500cc` | 2026-09-03 | 8 |
| `audit/templates-t_18aefdb7` | `0037ae13` | 2026-09-03 | 6 |
| `audit/voiceinput-t_1b60e150` | `f473a209` | 2026-09-03 | 8 |
| `audit/widget-t_344fbc58` | `a75f35f1` | 2026-09-04 | 10 |

### Ledger-tick lanes (Oct 6-9) (9)

Unmatched commits are mostly ledger-tick docs whose SUBJECTS exist in main but
whose bytes differ (rewritten scrub state after rebase) — patch-id cannot prove
containment; several unique commits (e.g. `docs: scrub real LAN addresses from
the goal ledger`, `docs: ledger - verify 02:45 tick...`) have no main twin at
all. Not deletable under the rule.

| branch | tip | date | unmatched |
|---|---|---|---|
| `wt/t_5abdb13d` | `be610ba3` | 2026-10-09 | 17 |
| `wt/t_6bb29d46` | `2c4ea6a4` | 2026-10-06 | 14 |
| `wt/t_716cf37c` | `45c4a427` | 2026-10-09 | 18 |
| `wt/t_9462260b` | `844bae66` | 2026-10-06 | 14 |
| `wt/t_95d864fc` | `7ebb334b` | 2026-10-09 | 17 |
| `wt/t_95d864fc-gated` | `bc1df039` | 2026-10-09 | 18 |
| `wt/t_95f1d6e1` | `379f4364` | 2026-10-06 | 14 |
| `wt/t_9a4b7687` | `5bd7af95` | 2026-10-09 | 18 |
| `wt/t_ec2c2f95` | `01e236b2` | 2026-10-08 | 17 |

### Aug template-era single-commit lanes (8)

One unique commit each; subjects not findable in main (some clearly superseded
by the v1.7.x release line, but supersession is not containment — not provably
merged, so KEEP per rule).

| branch | tip | date | unique commit |
|---|---|---|---|
| `wt/t_0dd23d22` | `f2989b17` | 2026-08-08 | f2989b17 feat(hscc-cluster): ship example templates for v1.7.0 routing/placement |
| `wt/t_107e9272` | `182a7d35` | 2026-08-08 | 182a7d35 feat(template): preview discloses routing (resolved targets + what it w |
| `wt/t_34255265` | `b5fdcdac` | 2026-08-18 | b5fdcdac docs(pr19): surface sparkrun v0.3.4 entrypoint refusal that blocks TP2/ |
| `wt/t_7733c4cc` | `e188e8fa` | 2026-08-06 | e188e8fa fix(cluster): recreate drifted units on apply; never double-provision a |
| `wt/t_9d8dfda8` | `c7d73b56` | 2026-08-06 | c7d73b56 chore(release): prepare HSCC v1.6.0 (VERSION + CHANGELOG only) |
| `wt/t_b65d97df` | `f66e24fd` | 2026-08-07 | f66e24fd docs(spec): recommend proxy (:4000) advertise worker-model via discover |
| `wt/t_cb95bfad` | `81e083a0` | 2026-08-06 | 81e083a0 feat(cluster): config writers emit logical aliases not concrete ids |
| `wt/t_f6909847` | `b0109e3e` | 2026-08-08 | b0109e3e feat(templates): ship two teaching examples — routing-split + colocatio |

### Feature-era branches with unique commits (5)

| branch | tip | date | unmatched | sample unique |
|---|---|---|---|---|
| `feat-multiplexing-observability` | `cddf9809` | 2026-07-01 | 2 | 7e47c1c5 feat(multiplexing): enable gateway multiplex + wire ctx.profi |
| `feat/cluster-templates-2026-09` | `742fe734` | 2026-09-25 | 10 | 2ba8a131 test(ios): commit sanitized live API fixtures for the model d |
| `feat/doctor-alias-migration` | `f888fefe` | 2026-08-06 | 1 | f888fefe feat(doctor): one-time migration of concrete model ids to ali |
| `serve/alias-served-model-name` | `aa31f467` | 2026-08-06 | 2 | f80809cd feat(cluster): advertise orchestrator-model/worker-model alia |
| `tooling/t_12f3c8a8` | `1b04243b` | 2026-10-09 | 1 | 1b04243b chore(tools): markup escape-sweep checker + applier used for  |

### Other (14)

| branch | tip | date | unmatched | sample unique |
|---|---|---|---|---|
| `fix/cluster-metrics-t_e05acdfb` | `7845200f` | 2026-09-17 | 10 | 2ba8a131 test(ios): commit sanitized live API fixtures for the model d |
| `wt/t_1b8792ca` | `16823d5b` | 2026-09-29 | 12 | 2ba8a131 test(ios): commit sanitized live API fixtures for the model d |
| `wt/t_1d7c9c34` | `a21fc202` | 2026-08-28 | 2 | 2ba8a131 test(ios): commit sanitized live API fixtures for the model d |
| `wt/t_308fce41` | `2cc2fd69` | 2026-08-29 | 2 | 2ba8a131 test(ios): commit sanitized live API fixtures for the model d |
| `wt/t_3969ace6` | `3424edab` | 2026-09-25 | 10 | 2ba8a131 test(ios): commit sanitized live API fixtures for the model d |
| `wt/t_3dd8651e` | `86570ef4` | 2026-10-16 | 1 | 86570ef4 fix(cli): autodown per-subcommand --help prints help and exec |
| `wt/t_776e294a` | `2eb7521a` | 2026-08-30 | 2 | 2ba8a131 test(ios): commit sanitized live API fixtures for the model d |
| `wt/t_9032f96b` | `077a7220` | 2026-10-16 | 1 | 077a7220 fix(cli): template group help — two-line wrap, real examples, |
| `wt/t_a4be6870` | `83675fe5` | 2026-08-28 | 2 | 2ba8a131 test(ios): commit sanitized live API fixtures for the model d |
| `wt/t_b578972f` | `3ecf4f8d` | 2026-09-26 | 12 | 2ba8a131 test(ios): commit sanitized live API fixtures for the model d |
| `wt/t_bf9f1909` | `767ca047` | 2026-10-16 | 1 | 767ca047 fix(cli): api verb --help prints per-verb help and executes n |
| `wt/t_f909de4e` | `0e6bf9eb` | 2026-09-02 | 8 | 2ba8a131 test(ios): commit sanitized live API fixtures for the model d |
| `wt/t_fd031303` | `3c37f60c` | 2026-09-30 | 12 | 2ba8a131 test(ios): commit sanitized live API fixtures for the model d |
| `wt/t_fdaa575b` | `2a77ebb6` | 2026-09-24 | 10 | 2ba8a131 test(ios): commit sanitized live API fixtures for the model d |

### Unverifiable-pick detail (byte-differing twins)

Branches whose unique commits have same-subject commits in main that are NOT
byte-identical (rebase/fixup drift). Under the standing rule a same-subject but
byte-different commit is NOT proof of containment; these stay:

- `t_c0953d4c`: `2ba8a131` vs main `ab5214ca` — same subject, different bytes
- `t_c0953d4c`: `4f030f88` vs main `279778d5` — same subject, different bytes
- `t_c0953d4c`: `e3240f8c` vs main `634f308e` — same subject, different bytes
- `wt/t_9032f96b`: `077a7220` vs main `bef626e2` — same subject, different bytes
- `wt/t_bf9f1909`: `767ca047` vs main `656d6158` — same subject, different bytes
- `wt/t_3dd8651e`: `86570ef4` vs main `43aa08ad` — same subject, different bytes

## Deferred / explicitly out of scope

- No tags touched. No history rewrites. No force-pushes.
- Local-only artifacts (not origin hygiene): `ops/dev` and `wt-api-cli-theme` exist
  only as stale `refs/remotes/*` entries in the main checkout (absent from
  `git ls-remote origin`) — cleaned by `git remote prune origin` (local metadata
  only, verified before/after with `ls-remote`).

## Deletion mechanics

1. Anchor: `refs/hygiene-2026-10-17/<branch>` → tip SHA for every deleted branch
   (local only, in the main checkout) — recovery is `git branch <name> <sha>`
   or `git branch <name> refs/hygiene-2026-10-17/<name>` without origin.
2. `git push origin --delete <branch>` per branch (normal push, no force).
3. `git remote prune origin` + `git worktree prune` (dead `.worktrees/*`
   registrations pointing at deleted dirs).

## Post-deletion recovery contract (authoritative)

If any deleted branch must be restored, its tip is in the tables above; the
complete machine-readable list (branch → tip SHA) is committed with this
report as the appendix below.

## Appendix A — deletion list (branch → tip SHA)

| branch | tip SHA | proof |
|---|---|---|
| `cli-help-2026-10-15` | `1383b98e` | ancestry |
| `feat/cluster-monitor-live` | `983b10ab` | ancestry |
| `feat/ios-redesign-2026-10-14` | `367ab399` | ancestry |
| `feat/repl-inject-2026-10-13` | `997c64f9` | ancestry |
| `feat/screens-harness` | `5b04a8b7` | ancestry |
| `feat/tmux-aware-chat-2026-10-13` | `4cabd5cb` | ancestry |
| `fix/heal-cascade-2026-10-14` | `852b531a` | ancestry |
| `fix/ios-chat-e2e-2026-10-13` | `e70111ac` | ancestry |
| `fix/proxy-outage-2026-10-11` | `223667c0` | ancestry |
| `wt/t_1897550c` | `1b9dc3e8` | ancestry |
| `wt/t_25aed701` | `361aa1c4` | ancestry |
| `wt/t_2a29a7ab-rebased` | `5ac0cfbf` | ancestry |
| `wt/t_3e6d3db7-gated` | `91ba3fe2` | ancestry |
| `wt/t_69a1c9f2-gated` | `7b12dbd7` | ancestry |
| `wt/t_873590dd` | `c34798d2` | ancestry |
| `wt/t_9a4b7687-gated` | `7f1ca182` | ancestry |
| `wt/t_a166be3d` | `8906e151` | ancestry |
| `wt/t_a9477712` | `76930b74` | ancestry |
| `wt/t_a9477712-gated` | `76930b74` | ancestry |
| `wt/t_aeb982bc` | `b97b4b48` | ancestry |
| `wt/t_db6a32da` | `98cff42e` | ancestry |
| `wt/t_f64706b4` | `ec9de92a` | ancestry |
| `wt/t_fc307a51` | `1383b98e` | ancestry |
| `feat-model-tiering` | `cb9285a9` | patch-id |
| `t_fcc0eaa6` | `23679b7b` | patch-id |
| `wt/t_06220610` | `c0f4d1b3` | patch-id |
| `wt/t_12f3c8a8` | `81e60521` | patch-id |
| `wt/t_12f3c8a8-gated` | `81e60521` | patch-id |
| `wt/t_188d37e4` | `22d38be8` | patch-id |
| `wt/t_20261016_f1_cli_help_f1` | `ff20706c` | patch-id |
| `wt/t_20ddb71e` | `82703d38` | patch-id |
| `wt/t_36580090` | `01b1dea7` | patch-id |
| `wt/t_71aec48c` | `01b832fc` | patch-id |
| `wt/t_7b0f65c8` | `ccdf9a0c` | patch-id |
| `wt/t_875c5019` | `05c9d0a2` | patch-id |
| `wt/t_8d2eb400` | `e161fc59` | patch-id |
| `wt/t_90082309` | `fb715d0b` | patch-id |
| `wt/t_c84a59b1` | `a9040844` | patch-id |
| `feat/bootstrap-telegram-optional` | `da5a7f39` | merge-carried |
| `feat/hscc-api` | `f1069aaa` | merge-carried |
| `feat/ios-app` | `1aec5719` | merge-carried |
| `feat/project-orchestrators` | `b3c39bb0` | merge-carried |
| `wt/t_083b6cf8` | `b5a08e18` | merge-carried |
| `wt/t_0d5ef51a` | `31ff2fea` | merge-carried |
| `wt/t_13f077e4` | `ef17fa69` | merge-carried |
| `wt/t_16dcceb4` | `a1d6ee53` | merge-carried |
| `wt/t_2647a396` | `c5757fd9` | merge-carried |
| `wt/t_2924a905` | `f99444b6` | merge-carried |
| `wt/t_2a29a7ab` | `01e1142d` | merge-carried |
| `wt/t_30b1e1ee` | `70798636` | merge-carried |
| `wt/t_495aca7f` | `f2affc07` | merge-carried |
| `wt/t_5b0f4d2d` | `751e2d56` | merge-carried |
| `wt/t_5d1d33d7` | `0c3096cf` | merge-carried |
| `wt/t_7beb3112` | `d9fa3b46` | merge-carried |
| `wt/t_864329ca` | `0d609da3` | merge-carried |
| `wt/t_939abe5f` | `91117275` | merge-carried |
| `wt/t_9bba450f` | `4bccaf1e` | merge-carried |
| `wt/t_ad411703` | `a5992014` | merge-carried |
| `wt/t_c29c6a82` | `938ae3cc` | merge-carried |
| `wt/t_c5bdcfb3` | `f3936a51` | merge-carried |
| `wt/t_c8ef006a` | `4494b182` | merge-carried |
| `wt/t_ccd21e9b` | `a74a1db8` | merge-carried |
| `wt/t_f256ce01` | `a6526604` | merge-carried |
| `wt/t_f7740ae0` | `c0384f03` | merge-carried |
| `wt/t_cfdcefa9` | `c272e49f` | byte-identical pick |

Total: 65 branches.

## Final state (post-deletion, appended after execution)

Executed on origin (normal `git push origin --delete`, batched by proof family,
no force, no rewrites, no tag changes):

- **Deleted 65 branches** — every entry of Appendix A. Post-delete
  `git ls-remote origin refs/heads/*`: **102 heads** remain
  (165 pre-run + 2 branches created during the window by sibling workers
  (`wt/t_5132deb3`, `wt/t_f47f89d2` this report's own branch) − 65 deleted).
- **Zero** delete-set branches remain on origin (verified by `ls-remote`
  after each batch; final verify: 0 remaining).
- Open-PR heads all intact: #34 wt/t_b7458cf4, #35 wt/t_1e54481a,
  #36 wt/t_ac795424, #37 wt/t_6e1e8443, #38 fix/nav-graph-2026-10-15,
  #39 wt/t_e286c4b9, #40 wt/t_39d7c915, #41 release/2.10.0 — re-checked live
  immediately before deletion (delete-set ∩ open heads = empty; tip drift =
  none; origin/main still baf98413 so every proof applies unchanged).
- `backup/pr19-pre-20260818-090812` NOT deleted (deferred, see Deferred section).
- KEEP branches NOT deleted.

Recovery anchors created BEFORE deletion: **65 refs** under
`refs/hygiene-2026-10-17/<branch>` in the local main checkout
(`/Users/desac/dev/hscc`), each pinned to the deleted tip recorded in
Appendix A. Restore any single branch with:

```
git branch <name> refs/hygiene-2026-10-17/<name>   # local
git push origin <name>                              # if it must go back to origin
```

Local cleanup performed (metadata only, no origin effect):

- `git fetch origin --prune` — tracking refs now mirror origin exactly
  (103 refs incl. HEAD).
- Stale non-origin remote refs removed after `ls-remote` proved the branches
  absent upstream: `refs/remotes/ops/dev`, `refs/remotes/wt-api-cli-theme`.
- `git worktree prune` — 5 dead registrations removed (worktrees whose
  directories kanban had already cleaned; among them t_160d306f, t_3aadd2c0,
  t_79015ab0, t_e286c4b9); remaining `git worktree list` entries all point at
  live directories (0 missing, 0 prunable).

The 11 flagged-branch families in the KEEP section (guard-fix trio,
dispatcher_wedge fix, iOS audit corpus, PR#38-stacked branches, feat/fix-aux-ip)
were NOT touched — they hold real unlanded work per the standing rule and
await an operator decision (open PRs / harvest / explicit retire).
