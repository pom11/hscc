kind: Fixed
task: t_30dfe9fe

- **A red PR whose card was already `done` was handed back to nobody.** Hermes
  closes a card as soon as its PR opens, so `default_handback`'s comment +
  reassign + unblock all landed on a terminal card and no worker was ever
  dispatched — #734 and #767 sat stranded on 2026-11-25 until an operator
  carded them by hand. The queue's handback now checks the card on the
  project's board through the sanctioned `hermes kanban show` CLI; when there
  is no card, or its status is `done`/`archived`, it creates a fix card with
  `hermes kanban create` on that board (priority 2, the entry's assignee, else
  the previous card's assignee, else `<board>-orch`). The card title names the
  PR and the reason (`PR #N red in merge queue: gate failed`), and the body
  carries the queue's full failure evidence — gate tail, conflicted files,
  resolve instructions — followed by the rule "fix on the PR branch, push, then
  `hscc merge-queue add <project> <N>`; no hand-merge". Comment, reassign and
  unblock also name that board explicitly (`--board <board>`), so the fix
  card's first comment cannot land on whatever board the daemon last used.
- **One open fix card per PR, reused across a dropped entry.** The created id
  is written onto the queue entry and into a per-project `fix_cards` map keyed
  by PR, so a second failure of the same PR — even after the red gate dropped
  its entry and an operator re-added it — comments on the existing fix card
  instead of cutting a duplicate. A card that is still open (`running`,
  `ready`, `blocked`, ...) keeps today's comment-only behaviour. A `show` that
  fails transiently is not treated as "no such card", so a board hiccup cannot
  produce a duplicate; a gate timeout never creates a fix card because it is
  not a verdict about the code.
---
kind: Verified
order: 1
task: t_30dfe9fe

- `hscc_daemon/tests/test_merge_queue.py`: **181 -> 189 cases**, green in one
  foreground run (52.80 s) on the interpreter the queue itself gates with
  (`~/.hermes/hermes-agent/venv/bin/python`). The eight new cases pin: no card
  creates one (title, evidence body, assignee, priority, recorded id), a
  `done` card creates one and falls back to its assignee, an open card is
  comment-only, a second failure reuses the created card through the by-PR
  map even when the re-added entry still names the done card, a gate timeout
  creates none, an unreadable board does not cut a duplicate, a board-aware
  handback is told the project's board, and `default_handback` prefixes every
  verb with `--board` when it has one.
- Real-CLI smoke against a throwaway `HERMES_HOME` (never the live board):
  `hermes kanban --board default create "PR #999 red in merge queue: gate
  failed" --body-file … --assignee worker --priority 2 --json` returned
  `{"id": "t_783bc20b", …}`, `show <id> --json` answered the `{"task": {…,
  "status": "ready"}}` shape the parser expects, and a missing id answers
  `no such task: …`. The module's `_ensure_fix_card` was then run end to end
  against that temp home: it created `t_6fdd11d3`, recorded it in the project
  map, and the second call reused the same id with no second create.
- The suite's autouse board-handback fence now stubs the single `_hermes_cli`
  seam that both the real handback and the new probe/create route go through,
  so neither can reach the operator's boards from a test — not only from the
  ones that inject a `handback=`.
- `python3 scripts/changelog_fragments.py check` → pointer block intact.
  `python3 scripts/address_guard.py --tracked` clean.
