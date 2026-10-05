# Worktree / wt-* branch hygiene — hscc primary checkout

- Date: 2026-10-05 (EEST)
- Task: kanban t_ad5dd538 (hygiene only, no product code changes)
- Primary checkout: `/Users/desac/dev/hscc`
- Policy: delete **only** `wt/*` branches whose tip is a strict ancestor of
  `origin/main` (re-verified at deletion time) **and** whose worktree is gone/clean.
  `git branch -d` only, never `-D`. Refs outside `wt/*`, tags, and remotes were
  never referenced by any delete command.

## Before / after

| metric | before | after |
|---|---|---|
| registered worktrees (`git worktree list`) | 280 | 247 |
| admin dirs (`.git/worktrees/`) | 279 | 246 |
| `wt/*` branches | 339 | 305 |
| `.worktrees/` disk usage | ~2.6 GB | ~1.9 GB |

The 33 removed worktree dirs summed to ≈ 650 MB (`du -sk` per dir before
removal: 718,692 KB total minus 53,040 KB for the 5 kanban-workspace dirs kept
per scope). Observed `.worktrees/` du dropped 2.6 GB → 1.94 GB (≈ 674 MB),
consistent within block-accounting rounding. No other worktrees disappeared:
the before/after `git worktree list` diff matches the 33 removals exactly
(0 unexpected paths vanished, 0 appeared).

`git worktree prune` was run first: it reported nothing to prune (all 279 admin
entries pointed at live directories). Removals were done via `git worktree remove`
+ `git branch -d`, per-entry, with re-verification gates at delete time
(registered path, `wt/*` shape, tip unchanged + still ancestor of a freshly
re-resolved `origin/main`, worktree clean via `status --porcelain`, branch not
checked out in another worktree). Any failed gate -> keep + log.

## Removed (33 worktrees + 34 branches, all proven merged & clean)

Verified post-run by diffing `git worktree list --porcelain` snapshots taken
before and after: exactly the 33 paths below disappeared; 0 unexpected paths
disappeared; all under `/Users/desac/dev/hscc/.worktrees/t_*`; all branches
`wt/*`:

- `t_02b2246d` → `wt/t_02b2246d`
- `t_08ec083d` → `wt/project-chat`
- `t_0a90efc7` → `wt/bootstrap-sqm`
- `t_121f721d` → `wt/t_121f721d`
- `t_17cdc637` → `wt/cluster-via-sparkrun`
- `t_1a0a47e8` → `wt/tg-optional`
- `t_3fe0cd05` → `wt/false-fail-streams`
- `t_42bb5c05` → `wt/verify-chat-ready`
- `t_48b71e12` → `wt/t_48b71e12`
- `t_507108a7` → `wt/recarria-hermes-v2026911`
- `t_6bf0ad22` → `wt/t_6bf0ad22`
- `t_7114d3bc` → `wt/hpr-43425`
- `t_7cc2e7d5` → `wt/t_7cc2e7d5`
- `t_7dd5f029` → `wt/t_7dd5f029`
- `t_8736f0d7` → `wt/t_8736f0d7`
- `t_8c5363c9` → `wt/t_8c5363c9`
- `t_97bd5955` → `wt/tg-automap`
- `t_9878ebcb` → `wt/project-sessions`
- `t_a3ab0fa1` → `wt/t_a3ab0fa1`
- `t_a763020e` → `wt/autoheal-grace2`
- `t_ab336532` → `wt/payload-drift-guard`
- `t_ae291a1b` → `wt/hpr-88276`
- `t_bc1bc14f` → `wt/t_bc1bc14f`
- `t_c430f773` → `wt/changelog-1150`
- `t_cd6a895a` → `wt/autoheal-remote-docker`
- `t_d3c929aa` → `wt/dispatch-assignee`
- `t_d4ba2eff` → `wt/t_d4ba2eff`
- `t_d9ebdb7c` → `wt/t_d9ebdb7c`
- `t_df3fb7f6` → `wt/check-state-provenance`
- `t_e1ff0e8e` → `wt/duplicate-workload`
- `t_ef4a6c1d` → `wt/orch-full-control`
- `t_f2b0d534` → `wt/kanban-db-import`
- `t_fc2e955d` → `wt/stream-flow-discrepancy`

Orphan branch (no worktree) deleted additionally: `wt/t_93f1ba4d`
(tip 1a4a88019c, merged). Total branch deletions: 34 (339 → 305).

## Kept (301 `wt/*` entries) — reasons

### A. Divergent — real work not on origin/main (253 branches, KEEP)

`git cherry origin/main <branch>` shows ≥1 commit with no patch-equivalent on
origin/main. These are unmerged worker branches; deleting any would risk losing
work. Full list (branch, tip, worktree if registered):

- `wt/hscc-cli-interpreter` tip 7dcbe72175 — worktree `/Users/desac/dev/hscc/.worktrees/t_2bcbe6f9`
- `wt/persist-cooldown-t_130e77e7` tip 5d11f71392
- `wt/t_00d7dc5d` tip a75957b346 — worktree `/Users/desac/dev/hscc/.worktrees/t_00d7dc5d`
- `wt/t_01de6ec3` tip 1264b7d37c — worktree `/Users/desac/dev/hscc/.worktrees/t_01de6ec3`
- `wt/t_01f14c58` tip 7c42e0ee28
- `wt/t_023d4c4c` tip 852e4f93e1 — worktree `/Users/desac/dev/hscc/.worktrees/t_023d4c4c`
- `wt/t_038f2482` tip 6eb9186fd0 — worktree `/Users/desac/dev/hscc/.worktrees/t_038f2482`
- `wt/t_039aee57` tip 0131d8af1c — worktree `/Users/desac/dev/hscc/.worktrees/t_039aee57`
- `wt/t_03a1c989` tip ef657bf2b9 — worktree `/Users/desac/dev/hscc/.worktrees/t_03a1c989`
- `wt/t_0418d4fb` tip 95ebe83bfb — worktree `/Users/desac/dev/hscc/.worktrees/t_0418d4fb`
- `wt/t_04d7f6ac` tip 45e69b16a7 — worktree `/Users/desac/dev/hscc/.worktrees/t_04d7f6ac`
- `wt/t_051c70df` tip a68cb6bcee — worktree `/Users/desac/dev/hscc/.worktrees/t_051c70df`
- `wt/t_058ae661` tip a0bfbcb5fc — worktree `/Users/desac/dev/hscc/.worktrees/t_058ae661`
- `wt/t_062de6b0` tip 0cdf352371 — worktree `/Users/desac/dev/hscc/.worktrees/t_062de6b0`
- `wt/t_072ea2d5` tip 48c6ef117a — worktree `/Users/desac/dev/hscc/.worktrees/t_072ea2d5`
- `wt/t_077c14d2` tip aecce8161f — worktree `/Users/desac/dev/hscc/.worktrees/t_077c14d2`
- `wt/t_083b6cf8` tip cba8c230d4 — worktree `/Users/desac/dev/hscc/.worktrees/t_083b6cf8`
- `wt/t_0c80cb8b` tip 9ed847c83c
- `wt/t_0d0ca2e5` tip edcf0bbf44
- `wt/t_0d2395b1` tip 222baf00ca — worktree `/Users/desac/dev/hscc/.worktrees/t_0d2395b1`
- `wt/t_0d5ef51a` tip 7de7a309aa — worktree `/Users/desac/dev/hscc/.worktrees/t_0d5ef51a`
- `wt/t_0dd23d22` tip ab7b87d7a3 — worktree `/Users/desac/dev/hscc/.worktrees/t_0dd23d22`
- `wt/t_0f31ed9d` tip 8cf304d5d9
- `wt/t_0f45814f` tip 4e4d08667b — worktree `/Users/desac/dev/hscc/.worktrees/t_0f45814f`
- `wt/t_107e9272` tip 373ee004e4 — worktree `/Users/desac/dev/hscc/.worktrees/t_107e9272`
- `wt/t_10bc37be` tip 776d0d3f87
- `wt/t_10df56a3` tip b33d96c2e2
- `wt/t_10df56a3-b4` tip 6df7bdee46 — worktree `/Users/desac/dev/hscc/.worktrees/t_10df56a3`
- `wt/t_13f077e4` tip c401ffc061 — worktree `/Users/desac/dev/hscc/.worktrees/t_13f077e4`
- `wt/t_13f6e231` tip 3dcb22a114 — worktree `/Users/desac/dev/hscc/.worktrees/t_13f6e231`
- `wt/t_153a9c84` tip bbf7638719
- `wt/t_16dcceb4` tip 3f1c6254a6 — worktree `/Users/desac/dev/hscc/.worktrees/t_16dcceb4`
- `wt/t_17423377` tip b33d96c2e2
- `wt/t_188d37e4` tip e1a983a2b6 — worktree `/Users/desac/dev/hscc/.worktrees/t_188d37e4`
- `wt/t_18b453da` tip a735234e6d — worktree `/Users/desac/dev/hscc/.worktrees/t_18b453da`
- `wt/t_197c4df4` tip d240a277cc — worktree `/Users/desac/dev/hscc/.worktrees/t_197c4df4`
- `wt/t_1ae68edc` tip b33d96c2e2
- `wt/t_1c56c5b3` tip 1c856b2db7 — worktree `/Users/desac/dev/hscc/.worktrees/t_1c56c5b3`
- `wt/t_1d9ab164` tip d89a273a73 — worktree `/Users/desac/dev/hscc/.worktrees/t_1d9ab164`
- `wt/t_201ffe7d` tip b94d4b1bf9
- `wt/t_20ddb71e` tip d5a59cf218 — worktree `/Users/desac/dev/hscc/.worktrees/t_20ddb71e`
- `wt/t_20edba4b` tip 7444af723b — worktree `/Users/desac/dev/hscc/.worktrees/t_20edba4b`
- `wt/t_22d5b4d7` tip d04f697a0e
- `wt/t_23396868` tip 739ed92cb3 — worktree `/Users/desac/dev/hscc/.worktrees/t_23396868`
- `wt/t_2426edaf` tip 69c4822905 — worktree `/Users/desac/dev/hscc/.worktrees/t_2426edaf`
- `wt/t_24c2a27b` tip 2696bfe97a
- `wt/t_2523d07c` tip 394565204f
- `wt/t_2647a396` tip cb5558f680 — worktree `/Users/desac/dev/hscc/.worktrees/t_2647a396`
- `wt/t_268ea5f1` tip 5c5b34c7d6 — worktree `/Users/desac/dev/hscc/.worktrees/t_268ea5f1`
- `wt/t_2924a905` tip 5add04a05f — worktree `/Users/desac/dev/hscc/.worktrees/t_2924a905`
- `wt/t_2b04a47a` tip 01ebd2b5da — worktree `/Users/desac/dev/hscc/.worktrees/t_2b04a47a`
- `wt/t_2b711a94` tip 3dcbbdbbff — worktree `/Users/desac/dev/hscc/.worktrees/t_2b711a94`
- `wt/t_2bb97a26` tip 20c8f65ae2
- `wt/t_2fd6b0d2` tip edcf0bbf44 — worktree `/Users/desac/dev/hscc/.worktrees/t_2fd6b0d2`
- `wt/t_30b1e1ee` tip 11096dd95a — worktree `/Users/desac/dev/hscc/.worktrees/t_30b1e1ee`
- `wt/t_3182ea68` tip c7ed46d5f8
- `wt/t_323a5564` tip ca422a6701 — worktree `/Users/desac/dev/hscc/.worktrees/t_323a5564`
- `wt/t_34255265` tip 7cb43c8420 — worktree `/Users/desac/dev/hscc/.worktrees/t_34255265`
- `wt/t_346c67d9` tip f10d8104c3 — worktree `/Users/desac/dev/hscc/.worktrees/t_346c67d9`
- `wt/t_3480d0a1` tip 7a06d0defa — worktree `/Users/desac/dev/hscc/.worktrees/t_3480d0a1`
- `wt/t_366dae2c` tip 0ff235fa6b — worktree `/Users/desac/dev/hscc/.worktrees/t_366dae2c`
- `wt/t_36cea62f` tip f0c46ba45a — worktree `/Users/desac/dev/hscc/.worktrees/t_36cea62f`
- `wt/t_3832283d` tip 2273498dc3 — worktree `/Users/desac/dev/hscc/.worktrees/t_3832283d`
- `wt/t_38fcde1d` tip 8acc5af14d — worktree `/Users/desac/dev/hscc/.worktrees/t_38fcde1d`
- `wt/t_3954bf21` tip 6330949e1c — worktree `/Users/desac/dev/hscc/.worktrees/t_3954bf21`
- `wt/t_3aabd73a` tip a735234e6d — worktree `/Users/desac/dev/hscc/.worktrees/t_3aabd73a`
- `wt/t_3b11db70` tip 1c856b2db7 — worktree `/Users/desac/dev/hscc/.worktrees/t_3b11db70`
- `wt/t_3c02b2a8` tip bb712729e5
- `wt/t_3c09e405` tip 313657e7ff — worktree `/Users/desac/dev/hscc/.worktrees/t_3c09e405`
- `wt/t_3db321fa` tip 4ed46d38b0
- `wt/t_41727d6a` tip 0260f838b6 — worktree `/Users/desac/dev/hscc/.worktrees/t_41727d6a`
- `wt/t_41eca119` tip 40c4313ad7 — worktree `/Users/desac/dev/hscc/.worktrees/t_41eca119`
- `wt/t_4295baaa` tip c7b3d6d8e8 — worktree `/Users/desac/dev/hscc/.worktrees/t_4295baaa`
- `wt/t_42de0af1` tip eed0b36a6e — worktree `/Users/desac/dev/hscc/.worktrees/t_42de0af1`
- `wt/t_433acc5d` tip f10d8104c3 — worktree `/Users/desac/dev/hscc/.worktrees/t_433acc5d`
- `wt/t_4362765f` tip 5754d077f7 — worktree `/Users/desac/dev/hscc/.worktrees/t_4362765f`
- `wt/t_45dca1e5` tip 829edbf08a — worktree `/Users/desac/dev/hscc/.worktrees/t_45dca1e5`
- `wt/t_47f51a71` tip fce16a4fd1
- `wt/t_4817c04a` tip cb07a86544 — worktree `/Users/desac/.hermes/kanban/boards/hscc/workspaces/t_4817c04a`
- `wt/t_48326e7d` tip b33d96c2e2 — worktree `/Users/desac/dev/hscc/.worktrees/t_48326e7d`
- `wt/t_495aca7f` tip e9bcc97540 — worktree `/Users/desac/dev/hscc/.worktrees/t_495aca7f`
- `wt/t_4a85bfb6` tip f5a71a0a73
- `wt/t_4afd04ec` tip dc2047bc00
- `wt/t_4f79b097` tip 0ade569fdb
- `wt/t_501fb7f1` tip 5f07c362af — worktree `/Users/desac/dev/hscc/.worktrees/t_501fb7f1`
- `wt/t_526c167e` tip cfce20c819 — worktree `/Users/desac/dev/hscc/.worktrees/t_526c167e`
- `wt/t_52919528` tip b73035a07e
- `wt/t_55f5a064` tip 3ca7efba38 — worktree `/Users/desac/dev/hscc/.worktrees/t_55f5a064`
- `wt/t_566930f5` tip e1acb06611 — worktree `/Users/desac/dev/hscc/.worktrees/t_566930f5`
- `wt/t_581bd0ce` tip 30693c8cb9 — worktree `/Users/desac/dev/hscc/.worktrees/t_581bd0ce`
- `wt/t_5a1e8beb` tip abab52743c — worktree `/Users/desac/dev/hscc/.worktrees/t_5a1e8beb`
- `wt/t_5b0f4d2d` tip 14bb30d0bf — worktree `/Users/desac/dev/hscc/.worktrees/t_5b0f4d2d`
- `wt/t_5c554c5b` tip 7590ce764c
- `wt/t_5cda57d0` tip c81392424a — worktree `/Users/desac/dev/hscc/.worktrees/t_5cda57d0`
- `wt/t_5d1118de` tip 8e80a65348 — worktree `/Users/desac/dev/hscc/.worktrees/t_5d1118de`
- `wt/t_5d1d33d7` tip c9b7606105 — worktree `/Users/desac/dev/hscc/.worktrees/t_5d1d33d7`
- `wt/t_5dc140ad` tip 9a5e0fa6f7 — worktree `/Users/desac/dev/hscc/.worktrees/t_5dc140ad`
- `wt/t_5ed5dfa8` tip ad75e70ce2 — worktree `/Users/desac/dev/hscc/.worktrees/t_5ed5dfa8`
- `wt/t_5ef61e51` tip a47c4dd98b — worktree `/Users/desac/dev/hscc/.worktrees/t_5ef61e51`
- `wt/t_5f40a71e` tip d257e838ad — worktree `/Users/desac/dev/hscc/.worktrees/t_5f40a71e`
- `wt/t_5fca98e5` tip 9e51b94eb1 — worktree `/Users/desac/dev/hscc/.worktrees/t_5fca98e5`
- `wt/t_61f70316` tip e1b6006327 — worktree `/Users/desac/dev/hscc/.worktrees/t_61f70316`
- `wt/t_623614d6` tip 7458e93358 — worktree `/Users/desac/dev/hscc/.worktrees/t_623614d6`
- `wt/t_627a9c2c` tip a65ca04b62 — worktree `/Users/desac/dev/hscc/.worktrees/t_627a9c2c`
- `wt/t_643e6370` tip ecf9389f62 — worktree `/Users/desac/dev/hscc/.worktrees/t_643e6370`
- `wt/t_6674e494` tip d5c9c2b5a9 — worktree `/Users/desac/dev/hscc/.worktrees/t_6674e494`
- `wt/t_67350b99` tip 1e4d8e79a8 — worktree `/Users/desac/dev/hscc/.worktrees/t_67350b99`
- `wt/t_68baa394` tip 7a6c7addfc — worktree `/Users/desac/dev/hscc/.worktrees/t_68baa394`
- `wt/t_69979dd1` tip cb1e6a7fd2 — worktree `/Users/desac/dev/hscc/.worktrees/t_69979dd1`
- `wt/t_699ea45c` tip aecce8161f — worktree `/Users/desac/dev/hscc/.worktrees/t_699ea45c`
- `wt/t_6d34b46d` tip 36a73a1347 — worktree `/Users/desac/dev/hscc/.worktrees/t_6d34b46d`
- `wt/t_6d93c705` tip caab8eeb8a
- `wt/t_6dd58ae6` tip 6325fcbbfd — worktree `/Users/desac/dev/hscc/.worktrees/t_6dd58ae6`
- `wt/t_6e183b92` tip 4a70a187b9 — worktree `/Users/desac/dev/hscc/.worktrees/t_6e183b92`
- `wt/t_6e5444f7` tip 0cdf352371 — worktree `/Users/desac/dev/hscc/.worktrees/t_6e5444f7`
- `wt/t_6fe37f0f` tip 265a759172
- `wt/t_715c5f9f` tip e3a216c4f4 — worktree `/Users/desac/dev/hscc/.worktrees/t_715c5f9f`
- `wt/t_71aec48c` tip 79c4309dc9
- `wt/t_71ff512d` tip b33d96c2e2
- `wt/t_724c7a2a` tip f9bd5b3ff4 — worktree `/Users/desac/dev/hscc/.worktrees/t_724c7a2a`
- `wt/t_740d9489` tip d020221e84
- `wt/t_74ca35e4` tip eb93a5098b — worktree `/Users/desac/dev/hscc/.worktrees/t_74ca35e4`
- `wt/t_7697752b` tip b33d96c2e2 — worktree `/Users/desac/dev/hscc/.worktrees/t_7697752b`
- `wt/t_76e8f2e9` tip 28a61affbe
- `wt/t_7733c4cc` tip dc91c9d510 — worktree `/Users/desac/.hermes/kanban/workspaces/t_7733c4cc`
- `wt/t_774d5eb1` tip eb04b4a2af
- `wt/t_78a93e77` tip fb7ff67f50 — worktree `/Users/desac/dev/hscc/.worktrees/t_78a93e77`
- `wt/t_791d1a75` tip 4de394bff1
- `wt/t_7b7357b5` tip 313657e7ff — worktree `/Users/desac/dev/hscc/.worktrees/t_7b7357b5`
- `wt/t_7beb3112` tip 840b377675 — worktree `/Users/desac/dev/hscc/.worktrees/t_7beb3112`
- `wt/t_7c2470c0` tip eb04b4a2af — worktree `/Users/desac/dev/hscc/.worktrees/t_7c2470c0`
- `wt/t_7c5b0821` tip eb04b4a2af — worktree `/Users/desac/dev/hscc/.worktrees/t_7c5b0821`
- `wt/t_7cfb453d` tip 8d889fbf20 — worktree `/Users/desac/dev/hscc/.worktrees/t_7cfb453d`
- `wt/t_7e3279ae` tip ffa060fc90 — worktree `/Users/desac/dev/hscc/.worktrees/t_7e3279ae`
- `wt/t_7ef58807` tip 0acd9871dc — worktree `/Users/desac/dev/hscc/.worktrees/t_7ef58807`
- `wt/t_7f90dbb7` tip 7c83e64fe6 — worktree `/Users/desac/.hermes/kanban/boards/hscc/workspaces/t_7f90dbb7`
- `wt/t_81b02abe` tip eb04b4a2af — worktree `/Users/desac/dev/hscc/.worktrees/t_81b02abe`
- `wt/t_824cb5d5` tip 17bfdcbe2c — worktree `/Users/desac/dev/hscc/.worktrees/t_824cb5d5`
- `wt/t_8369cb68` tip 2c55a56c38 — worktree `/Users/desac/dev/hscc/.worktrees/t_8369cb68`
- `wt/t_846a4b7d` tip d89a273a73 — worktree `/Users/desac/dev/hscc/.worktrees/t_846a4b7d`
- `wt/t_864329ca` tip 772106fbe9 — worktree `/Users/desac/dev/hscc/.worktrees/t_864329ca`
- `wt/t_8899bca2` tip e038f77d1f — worktree `/Users/desac/dev/hscc/.worktrees/t_8899bca2`
- `wt/t_88ba3edb` tip 449e6c6c29 — worktree `/Users/desac/dev/hscc/.worktrees/t_88ba3edb`
- `wt/t_8e8860b1` tip 12feaa55e4
- `wt/t_939abe5f` tip 850234a233 — worktree `/Users/desac/dev/hscc/.worktrees/t_939abe5f`
- `wt/t_9666ea43` tip a394831c5b
- `wt/t_968f2f1e` tip 5c4abdace6 — worktree `/Users/desac/dev/hscc/.worktrees/t_968f2f1e`
- `wt/t_96ed2307` tip 6df6a5a516 — worktree `/Users/desac/dev/hscc/.worktrees/t_96ed2307`
- `wt/t_971d10d8` tip fa686478c9 — worktree `/Users/desac/dev/hscc/.worktrees/t_971d10d8`
- `wt/t_97adf8bb` tip 76750eb4d5 — worktree `/Users/desac/dev/hscc/.worktrees/t_97adf8bb`
- `wt/t_9bba450f` tip c149b185af — worktree `/Users/desac/.hermes/kanban/workspaces/t_9bba450f`
- `wt/t_9c15acd7` tip 7c8e7a2956 — worktree `/Users/desac/dev/hscc/.worktrees/t_9c15acd7`
- `wt/t_9ca2567e` tip e765738f68 — worktree `/Users/desac/dev/hscc/.worktrees/t_9ca2567e`
- `wt/t_9d2b5e83` tip 94901e43b7 — worktree `/Users/desac/dev/hscc/.worktrees/t_9d2b5e83`
- `wt/t_9d8dfda8` tip adc67de5a1 — worktree `/Users/desac/dev/hscc/.worktrees/t_9d8dfda8`
- `wt/t_9e678c54` tip fd4f9dfa32 — worktree `/Users/desac/.hermes/kanban/boards/hscc/workspaces/t_9e678c54`
- `wt/t_9f798c4c` tip 6745a4e8e2 — worktree `/Users/desac/dev/hscc/.worktrees/t_9f798c4c`
- `wt/t_a332940c` tip cdbd9475b4 — worktree `/Users/desac/dev/hscc/.worktrees/t_a332940c`
- `wt/t_a3ccc003` tip bef35595b6 — worktree `/Users/desac/dev/hscc/.worktrees/t_a3ccc003`
- `wt/t_a4e700ee` tip 8caa2c2728 — worktree `/Users/desac/dev/hscc/.worktrees/t_a4e700ee`
- `wt/t_a8e9b7ff` tip a174c87d90 — worktree `/Users/desac/dev/hscc/.worktrees/t_a8e9b7ff`
- `wt/t_ab126417` tip eb04b4a2af
- `wt/t_ab177036` tip 3dcbbdbbff — worktree `/Users/desac/dev/hscc/.worktrees/t_ab177036`
- `wt/t_acb35a6e` tip e1b6006327 — worktree `/Users/desac/dev/hscc/.worktrees/t_acb35a6e`
- `wt/t_ad411703` tip c9bc3754c8 — worktree `/Users/desac/dev/hscc/.worktrees/t_ad411703`
- `wt/t_ad5dd538` tip bfee357c6d — worktree `/Users/desac/dev/hscc/.worktrees/t_ad5dd538`
- `wt/t_af410fbd` tip 46fe756485 — worktree `/Users/desac/.hermes/kanban/workspaces/t_af410fbd`
- `wt/t_afad6c38` tip 87256818ae
- `wt/t_b0091d92` tip 0b0b9e5707 — worktree `/Users/desac/dev/hscc/.worktrees/t_b0091d92`
- `wt/t_b36396b1` tip 0cdf352371 — worktree `/Users/desac/dev/hscc/.worktrees/t_b36396b1`
- `wt/t_b5648ecf` tip 0e1e4d199d — worktree `/Users/desac/dev/hscc/.worktrees/t_b5648ecf`
- `wt/t_b626a556` tip 7a6c7addfc — worktree `/Users/desac/dev/hscc/.worktrees/t_b626a556`
- `wt/t_b65d97df` tip adb3835250 — worktree `/Users/desac/dev/hscc/.worktrees/t_b65d97df`
- `wt/t_b93364cb` tip 8780fb1cd9 — worktree `/Users/desac/dev/hscc/.worktrees/t_b93364cb`
- `wt/t_baaabaad` tip cd3237f5ae — worktree `/Users/desac/dev/hscc/.worktrees/t_baaabaad`
- `wt/t_bc242def` tip ff4a68fdb1 — worktree `/Users/desac/dev/hscc/.worktrees/t_bc242def`
- `wt/t_bd6eecbd` tip 4721b17b89 — worktree `/Users/desac/dev/hscc/.worktrees/t_bd6eecbd`
- `wt/t_c00c4d02` tip 57bf597dde — worktree `/Users/desac/dev/hscc/.worktrees/t_c00c4d02`
- `wt/t_c03fd5ae` tip accb32fea7 — worktree `/Users/desac/.hermes/kanban/boards/hscc/workspaces/t_c03fd5ae`
- `wt/t_c04549d6` tip 59e0261429 — worktree `/Users/desac/dev/hscc/.worktrees/t_c04549d6`
- `wt/t_c0a7f0f3` tip e765738f68 — worktree `/Users/desac/dev/hscc/.worktrees/t_c0a7f0f3`
- `wt/t_c18befdb` tip 716188c8d8
- `wt/t_c2631579` tip 88944c7ec8 — worktree `/Users/desac/.hermes/kanban/boards/hscc/workspaces/t_c2631579`
- `wt/t_c29c6a82` tip 79c223960d — worktree `/Users/desac/dev/hscc/.worktrees/t_c29c6a82`
- `wt/t_c38014ac` tip d240a277cc — worktree `/Users/desac/dev/hscc/.worktrees/t_c38014ac`
- `wt/t_c3aac28c` tip ac7818ee3d — worktree `/Users/desac/dev/hscc/.worktrees/t_c3aac28c`
- `wt/t_c3e487c7` tip 15a693b128 — worktree `/Users/desac/dev/hscc/.worktrees/t_c3e487c7`
- `wt/t_c5b9d0fc` tip e2d05f81b2 — worktree `/Users/desac/dev/hscc/.worktrees/t_c5b9d0fc`
- `wt/t_c5bdcfb3` tip c4a65fc474 — worktree `/Users/desac/dev/hscc/.worktrees/t_c5bdcfb3`
- `wt/t_c84a59b1` tip 32c316f1af — worktree `/Users/desac/.hermes/kanban/workspaces/t_c84a59b1`
- `wt/t_c8ef006a` tip 5895de3223 — worktree `/Users/desac/dev/hscc/.worktrees/t_c8ef006a`
- `wt/t_c94f8b8c` tip 9266266fe2 — worktree `/Users/desac/dev/hscc/.worktrees/t_c94f8b8c`
- `wt/t_c9cc4ef9` tip f8ba1eca67
- `wt/t_ca65fb29` tip 44ce8f87be — worktree `/Users/desac/dev/hscc/.worktrees/t_ca65fb29`
- `wt/t_cb95bfad` tip 7b5cac523a — worktree `/Users/desac/dev/hscc/.worktrees/t_cb95bfad`
- `wt/t_cbce664b` tip ce77e2fc51 — worktree `/Users/desac/dev/hscc/.worktrees/t_cbce664b`
- `wt/t_ccd21e9b` tip 209ffd5789 — worktree `/Users/desac/.hermes/kanban/workspaces/t_ccd21e9b/hscc-wt`
- `wt/t_cce98791` tip 4a70a187b9 — worktree `/Users/desac/dev/hscc/.worktrees/t_cce98791`
- `wt/t_cd08c30a` tip d7efda58f2 — worktree `/Users/desac/.hermes/kanban/boards/hscc/workspaces/t_cd08c30a`
- `wt/t_cde700be` tip bbcf8d564e
- `wt/t_cefb133c` tip 275af8a135 — worktree `/Users/desac/dev/hscc/.worktrees/t_cefb133c`
- `wt/t_cfc4b374` tip 85000dcb51 — worktree `/Users/desac/dev/hscc/.worktrees/t_cfc4b374`
- `wt/t_d07e0605` tip 0092e8a830
- `wt/t_d315e8ee` tip 73dddb174a
- `wt/t_d320640b` tip fcff2a35de — worktree `/Users/desac/dev/hscc/.worktrees/t_d320640b`
- `wt/t_d43e645f` tip 2507a38ffb — worktree `/Users/desac/dev/hscc/.worktrees/t_d43e645f`
- `wt/t_d557daa0` tip 600a5a218c
- `wt/t_d63fe1b9` tip 8dad74df92 — worktree `/Users/desac/dev/hscc/.worktrees/t_d63fe1b9`
- `wt/t_d64ea494` tip ad65bbe4fc
- `wt/t_d694ea68` tip b33d96c2e2 — worktree `/Users/desac/dev/hscc/.worktrees/t_d694ea68`
- `wt/t_d6bdec0e` tip 546e16edd3 — worktree `/Users/desac/dev/hscc/.worktrees/t_d6bdec0e`
- `wt/t_d801d62b` tip d960047e4a — worktree `/Users/desac/dev/hscc/.worktrees/t_d801d62b`
- `wt/t_d8893e2e` tip b33d96c2e2 — worktree `/Users/desac/dev/hscc/.worktrees/t_d8893e2e`
- `wt/t_dafe62bc` tip 70fd2e5994 — worktree `/Users/desac/dev/hscc/.worktrees/wt/t_dafe62bc`
- `wt/t_df47e797` tip 1425619d31 — worktree `/Users/desac/dev/hscc/.worktrees/t_df47e797`
- `wt/t_e2e7e698` tip 4d1f7465d7
- `wt/t_e38ca390` tip 3734260970 — worktree `/Users/desac/dev/hscc/.worktrees/t_e38ca390`
- `wt/t_e68c7da4` tip 3b55ff1b2c — worktree `/Users/desac/dev/hscc/.worktrees/t_e68c7da4`
- `wt/t_e751e652` tip ee72edfaf6 — worktree `/Users/desac/dev/hscc/.worktrees/t_e751e652`
- `wt/t_e8ffd787` tip 5aa9378456 — worktree `/Users/desac/.hermes/kanban/boards/hscc/workspaces/t_e8ffd787`
- `wt/t_e9833a8b` tip c6c4de419f
- `wt/t_e9b3a501` tip c3e737d965 — worktree `/Users/desac/dev/hscc/.worktrees/t_e9b3a501`
- `wt/t_eb2d6da2` tip 7d53a64bd3 — worktree `/Users/desac/dev/hscc/.worktrees/t_eb2d6da2`
- `wt/t_ecdfbfe5` tip ed5973bdf3
- `wt/t_eda83709` tip eb04b4a2af — worktree `/Users/desac/dev/hscc/.worktrees/t_eda83709`
- `wt/t_ee9bddb2` tip 79b37eabf6
- `wt/t_f0af3d6a` tip 26aec7c51d
- `wt/t_f0d4f30e` tip a735234e6d — worktree `/Users/desac/dev/hscc/.worktrees/t_f0d4f30e`
- `wt/t_f109e7ea` tip 31a1175cb0
- `wt/t_f152c967` tip 9a12d13734 — worktree `/Users/desac/dev/hscc/.worktrees/t_f152c967`
- `wt/t_f22be3c4` tip e322c4e854 — worktree `/Users/desac/.hermes/kanban/boards/hscc/workspaces/t_f22be3c4`
- `wt/t_f22f9d28` tip 5754d077f7 — worktree `/Users/desac/dev/hscc/.worktrees/t_f22f9d28`
- `wt/t_f256ce01` tip cf477f77d1 — worktree `/Users/desac/dev/hscc/.worktrees/t_f256ce01`
- `wt/t_f2c2dbb5` tip ec703f88f9
- `wt/t_f2cffbb8` tip 721c82a7f8 — worktree `/Users/desac/dev/hscc/.worktrees/t_f2cffbb8`
- `wt/t_f39bc5b5` tip 1668c0bf75 — worktree `/Users/desac/dev/hscc/.worktrees/t_f39bc5b5`
- `wt/t_f4c8b552` tip 905aea4b5d — worktree `/Users/desac/dev/hscc/.worktrees/t_f4c8b552`
- `wt/t_f50f61ec` tip 6c413516f3 — worktree `/Users/desac/dev/hscc/.worktrees/t_f50f61ec`
- `wt/t_f6909847` tip 7440b5b92c — worktree `/Users/desac/dev/hscc/.worktrees/t_f6909847`
- `wt/t_f7740ae0` tip 376d8819a4 — worktree `/Users/desac/dev/hscc/.worktrees/t_f7740ae0`
- `wt/t_f8e78b60` tip d284734573
- `wt/t_f909de4e` tip 0e6bf9eb8d — worktree `/Users/desac/.hermes/kanban/boards/hscc/workspaces/t_f909de4e/wt`
- `wt/t_f99658dc` tip a65ca04b62 — worktree `/Users/desac/dev/hscc/.worktrees/t_f99658dc`
- `wt/t_f9b0b529` tip 4d0aed2001 — worktree `/Users/desac/dev/hscc/.worktrees/t_f9b0b529`
- `wt/t_fb4740d5` tip 9191c4eca4 — worktree `/Users/desac/dev/hscc/.worktrees/t_fb4740d5`
- `wt/t_fcc88582` tip 18cf95c53b — worktree `/Users/desac/dev/hscc/.worktrees/t_fcc88582`
- `wt/t_fd202e83` tip cdbd9475b4 — worktree `/Users/desac/dev/hscc/.worktrees/t_fd202e83`
- `wt/t_fdb8360a` tip be0fad5b70 — worktree `/Users/desac/dev/hscc/.worktrees/t_fdb8360a`
- `wt/t_fe003b6c` tip b33d96c2e2
- `wt/tg-remove` tip 927f1c89dc — worktree `/Users/desac/dev/hscc/.worktrees/t_07fd5b86`
- `wt/verify-pr19` tip c22dc39d95 — worktree `/Users/desac/dev/hscc/.worktrees/t_a945cfb7`
- `wt/wedge-false-positive` tip e2a19f2b9b — worktree `/Users/desac/dev/hscc/.worktrees/t_7895145b`
- `wt/worker-shell` tip 7909466308 — worktree `/Users/desac/dev/hscc/.worktrees/t_74e1ff6f`

### B. Patch-equivalent / squash-merged (42 branches, KEEP per task rule)

All their commits already appear on origin/main as patch-equivalents (`git cherry`
shows 0 ahead), but the branch tip is **not** a strict ancestor of origin/main, so
the task rule ("ancestor or keep") says keep. Safe candidates for a future policy
decision — they contribute ~0 unique content:

- `wt/t_11f38ce4` tip 654bc1bb2e — worktree `/Users/desac/dev/hscc/.worktrees/t_11f38ce4`
- `wt/t_15a88458` tip 9d2c5835f8
- `wt/t_1d7c9c34` tip a21fc202d3
- `wt/t_1ff4dcbd` tip 2acdfa48a7
- `wt/t_267da363` tip 2ea30237c7
- `wt/t_2985e00b` tip 6d6392d581
- `wt/t_2f2f301a` tip de19bf1bcf
- `wt/t_300416f3` tip 9155cab23c — worktree `/Users/desac/dev/hscc/.worktrees/t_300416f3`
- `wt/t_36158a94` tip 17100fcc67
- `wt/t_3d0b33d7` tip d80d76906d — worktree `/Users/desac/dev/hscc/.worktrees/t_3d0b33d7`
- `wt/t_3e152ac7` tip ccbd96e62d
- `wt/t_4072c133` tip 32fcc32784
- `wt/t_44f1330f` tip d10de78e62
- `wt/t_563350d9` tip 0e824fe069
- `wt/t_57c3779c` tip e0c8177743
- `wt/t_58f21007` tip bc7be41d69 — worktree `/Users/desac/dev/hscc/.worktrees/wt/t_58f21007`
- `wt/t_6728c271` tip b7bdedaa1b — worktree `/Users/desac/.hermes/kanban/boards/hscc/workspaces/t_6728c271`
- `wt/t_776e294a` tip 2eb7521a68
- `wt/t_780c2b36` tip 234f877f45
- `wt/t_7a16aed0` tip 2301e8cddb — worktree `/Users/desac/dev/hscc/.worktrees/t_7a16aed0`
- `wt/t_7f699e3c` tip b440674a8d
- `wt/t_87914280` tip 3b9cfc1243
- `wt/t_88f8ee05` tip 2250f4561e — worktree `/Users/desac/dev/hscc/.worktrees/t_88f8ee05`
- `wt/t_974d569f` tip c88c680457 — worktree `/Users/desac/dev/hscc/.worktrees/t_974d569f`
- `wt/t_97e31dbf` tip 769a8b1569 — worktree `/Users/desac/.hermes/kanban/boards/hscc/workspaces/t_97e31dbf`
- `wt/t_9c9b35c3` tip 928e053452
- `wt/t_a4be6870` tip 83675fe52b — worktree `/Users/desac/dev/hscc/.worktrees/t_a4be6870`
- `wt/t_a779c06f` tip 87f5d5333e — worktree `/Users/desac/dev/hscc/.worktrees/t_a779c06f`
- `wt/t_acb19c01` tip be75f704b1
- `wt/t_b55400ea` tip 5ce0e50e36
- `wt/t_b89d9029` tip wt/t_b89d9 — worktree `/Users/desac/dev/hscc/.worktrees/t_b89d9029`
- `wt/t_bee9db8a` tip 48c72ce68c
- `wt/t_c77f243f` tip cd712581b8 — worktree `/Users/desac/dev/hscc/.worktrees/t_c77f243f`
- `wt/t_ca439ff4` tip 05312f7600
- `wt/t_cb300f9c` tip 1239857e1d
- `wt/t_cf296e48` tip c02fb90d56 — worktree `/Users/desac/.hermes/kanban/boards/hscc/workspaces/t_cf296e48/hscc`
- `wt/t_d61e9cbd` tip 52f9c4419b
- `wt/t_d77e927f` tip d07ce26ee8
- `wt/t_dce08413` tip 2e1446a38a
- `wt/t_e118313c` tip ad54bb79cf
- `wt/t_ec570637` tip 82ccab0d79
- `wt/t_fb69dcaa` tip ba7246d996 — worktree `/Users/desac/.hermes/kanban/boards/hscc/workspaces/t_fb69dcaa`

### C. Merged but worktree dirty (6, KEEP)

Branch tip is merged into origin/main, but the worktree has tracked modifications
and/or untracked files, so neither worktree nor branch was touched:

- `wt/log-rotation` tip 4c7971835a — `/Users/desac/dev/hscc/.worktrees/t_aec5aebd`
- `wt/t_1e7c2fe4` tip 52439afc9b — `/Users/desac/dev/hscc/.worktrees/t_1e7c2fe4`
- `wt/t_2472675d` tip c42623c467 — `/Users/desac/dev/hscc/.worktrees/t_2472675d`
- `wt/t_d1fe53e7` tip 8aca6774c8 — `/Users/desac/dev/hscc/.worktrees/t_d1fe53e7`
- `wt/t_fc53b13d` tip 4b3c4a6232 — `/Users/desac/.hermes/kanban/boards/hscc/workspaces/t_fc53b13d/wt`
- `wt/tg-archive` tip e84b51087c — `/Users/desac/dev/hscc/.worktrees/t_7a67ef92`

## Out of scope — found but not touched (hard constraints)

1. **5 merged, clean `wt/*` worktrees under
   `~/.hermes/kanban/boards/hscc/workspaces/`** (`t_218cb9ec`, `t_8901cecd`,
   `t_9a5cfc3b`, `t_c16a1ae9`, `t_c1ab8a2c`): all proven ancestor-merged + clean,
   but the task restricts removals to `.worktrees/t_*` dirs only, so they were
   left in place (~50 MB). Candidate for a follow-up card if the operator wants
   kanban workspace dirs pruned too (became ancestor-merged only after the
   mid-run origin/main push 43520a8 → 1abdf7a).
2. **19 worktrees on non-`wt/*` branches** (e.g. `feat/ios-app`, `b2-work`,
   `audit/*`, detached HEAD): task forbids touching refs/heads outside `wt/*`. Kept.
3. **Operator untracked file**
   `hscc-cluster/templates/4node/flash-next-all.yaml` in the primary checkout:
   untouched (verified before and after via `git status --short`).
4. Non-`wt/*` branches, tags, remotes: never referenced by any delete command.

## Notes

- `origin/main` advanced mid-run (43520a8 → 1abdf7a, operator heartbeat commit)
  and the dispatcher concurrently reaped some finished worker worktrees; the
  before/after diff above is the authoritative record of what *this task* removed.
- Tools used (committed alongside this report):
  `scripts/worktree_hygiene.py` (read-only classifier) and
  `scripts/worktree_prune.py` (guarded checklist executor; dry-run by default,
  `--apply` to act). Re-running the classifier after the prune reports 0 further
  in-scope removals.
- No sparkrun/daemon/cluster commands were run (per task constraint).

