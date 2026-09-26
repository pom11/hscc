# Phase 1 [chat-composer] — branch reconciliation disposition — t_d331843e

Date: 2026-09-26
Assignee: ios-engineer
Group: chat composer (8 audit branches)
Baseline main: 646054d (== origin/main after review-time fetch; features verified at this tip)
VERIFIED BY EXECUTION: every evidence line below was produced by comparing main's
tree at 646054d against each branch tip via `git cat-file -e`, `git show main:<path>`
and `git diff main:<path> <branch>:<path>`.

## Disposition table

| Branch | Disposition | Evidence |
|---|---|---|
| audit/chatreadability-t_d90753cd | **SUPERSEDED** | Readability change already on main: main's StreamingChatView.swift contains the turn-marker recipe — `chatMeasure`, `turnGap`, `accentRoleColor`, `private var bubble`, `Text(isUser ? "YOU" : "ORCHESTRATOR")` (git show 646054d:.../StreamingChatView.swift lines 391/395/408/422/425/430). Feature absent from nothing — present identically. |
| audit/slashpalette-t_6f91edd3 | **SUPERSEDED** | SlashCommandPalette.swift + ios-app/docs/slashpalette-audit.md already on main, byte-identical (`git diff main:<f> branch:<f>` → 0 lines). Wired into main's views: `SlashCommandPalette(draft: $store.draft, client:)` at StreamingChatView:266 and OrchestratorChatView:305. |
| audit/slashpreview-t_26c97b40 | **SUPERSEDED** | SlashCommandPreview.swift already on main, byte-identical (0-line diff). Referenced from main's StreamingChatView:273 `SlashPreviewCard(draft:client:)`. |
| audit/voiceinput-t_1b60e150 | **SUPERSEDED** | Voice/Dictate button already on main: `Image(systemName:"mic.fill")` + `startDictation()` + `composerFocused` in BOTH StreamingChatView and OrchestratorChatView (3 marker hits each @646054d). |
| audit/offline-queue-t_42ba90d2 | **SUPERSEDED** | OfflineSendQueue.swift already on main, byte-identical (0-line diff), fully wired in ContentView (consumeDrained/drainDueToClusterSwitch/seedOfflineQueue/droppedBanner — 8 marker hits @646054d) and ChatStore (isPending, flush). Docs ios-app/docs/offline-queue-t_42ba90d2.md on main. |
| audit/chat-retry-t_3ae70b8c | **LEAVE** (WIP) | Single commit 169c438 is an explicit WIP checkpoint (`wip(t_3ae70b8c): checkpoint in-progress work`) adding streaming send-retry (restore failed text to composer in StreamingChatStore.send). That behavior is NOT on main: main's StreamingChatStore.send() (git show 646054d) still shows the pre-WIP shape — no text-restore, no editingRowID, no phantom-row removal. Per card rule, do not force-merge unfinished WIP. Branch left in place for a real follow-up. NOTE: the offline/stateful chat path DOES have retry (ChatStore.swift `retry(prompt:)` t_c0953d4c), but that is a different code path from the streaming composer this WIP targets. |
| audit/chat-stop-t_6efb89ff | **SUPERSEDED** | Docs only (no code). docs/audits/stop-running-turn-audit-t_6efb89ff.md already on main @646054d, byte-identical (0-line diff). Finding recorded. |
| audit/chat-attachments-t_3fc4801c | **SUPERSEDED** | Docs only (no code). ios-app/docs/chat-attachments-gap.md already on main @646054d, byte-identical (0-line diff). Wire-gated gap recorded. |

## Summary

7 of 8 branches are SUPERSEDED: every feature/fileset they carry is already present
on main at 646054d, byte-identical for the shared files. These reached main through
the operator's dev cherry-pick flow (prior merges such as dbd61da / 5cdd547 and the
feat/ios-app line) before this reconciliation ran. No LAND merge was required.

1 branch (chat-retry) is LEFT as-is: it is an unfinished WIP checkpoint whose retry
behavior is NOT yet on main, and the card rule forbids force-merging unfinished WIP.
It is filed as a candidate follow-up: finish the streaming send-retry (restore failed
text to composer) on a fresh card.

## Acknowledgements / scratch-check

- No commit content beyond this doc was changed in this reconciliation.
- Branches NOT deleted: every branch remains; none were proven merged by a merge-commit
  on main (they arrived via cherry-pick), so none were removed — caution per card rule.
- THEME / BUILD: no code touched in this card, so iOS compile/link gates not re-run
  (the SUPERSEDED features were already compiled when landed by their original cards).
