# Cards created on the wrong board — detection and recovery

`kanban_create` resolves its board from the environment (`HERMES_KANBAN_DB` →
`HERMES_KANBAN_BOARD` → the `current` symlink → `default`), which is often NOT the
board an orchestrator session is actually working on. `board="<slug>"` is therefore
mandatory on every `kanban_create` / `kanban_comment` / `kanban_complete` call.

## Why the mistake is invisible when it happens

- The create returns `{task_id, status, assignee…}` and **never echoes the board**, so
  `ok: true` proves nothing about where the row landed.
- A card on the wrong board does not appear in `kanban_list(board=...)`, is still
  claimable by its assignee, and there is **no cross-board move verb**.
- The error only surfaces later, as `no such task` from `show --board <intended>`.

So verify *in the same breath as creating*: `show --board <slug> <id>`, or
`kanban_list(board=<slug>, status="ready")`. Recording the rule in a playbook has not
been enough — the check is the rule.

## Recovery while the orphan is still `ready`

Re-create on the intended board, then archive the orphan.

When re-linking a graph, note that `show --json` reports `parents`/`children` at the
**TOP level**, not inside `task` — a naive move script that reads them out of `task`
re-creates the whole graph parentless, which means every card is `ready` and the
dispatcher can claim all of them at once. Always pass `parents` in the creating call;
if you must link afterwards, expect to `reclaim` cards the dispatcher already grabbed,
and remember reclaim alone is not enough on a card still in `ready` — the dispatcher
re-spawns it, so close or gate it in the same breath.

Verify a move with `kanban_list(board=<slug>, status="ready")` plus each card's
`parents:` line, not with the create response.

## Recovery once the dispatcher has claimed the orphan

Re-creating on the intended board now puts **two workers on one deliverable** — for a
PR-branch card, two pushes to the same branch. Do not duplicate; keep the live worker:

1. Fix the orphan's body if it is wrong (`hermes kanban edit <id> --body …`).
2. Post a MISROUTE comment on the orphan telling the worker the work itself is still
   correct and that its notification will land on the wrong board.
3. Post the matching note on the intended board's owning card, so the trail is not split.
4. Archive the orphan when its push lands.

Track the outcome from the PR, which is board-independent, rather than from board
queries that read one board at a time.

## Finding the board a card actually landed on

Resolve the id on every board and filter the noise; everything else is guesswork:

```sh
for b in $(ls ~/.hermes/kanban/boards); do
  o=$(hermes kanban --board "$b" show <id> 2>&1 | head -1)
  case "$o" in *"no such task"*|*"does not exist"*|*"invalid board slug"*) ;; *) echo "$b :: $o" ;; esac
done
```

- Filter stale-directory errors too, not just `no such task`: leftover folders under `boards/` are
  not boards, and their "board does not exist" errors print as hits if you only exclude `no such
  task` — that turned three misses into three false positives.
- `kanban_list` cannot prove absence in **either** direction. It can omit a card from the board that
  really holds it (a gated `todo` card with unfinished parents may not be listed), and it truncates
  titles so an id/title grep misses rows that exist. A zero-hit `list` result licenses exactly one
  conclusion: the query was under-scoped. Only `show <id>` per board settles location.
- Do not locate a card by grepping or `strings`-ing the board `.db` files: the legacy
  `~/.hermes/kanban.db` exceeds the command scan cap, so the whole call is refused and you learn
  nothing. The per-board `show` loop is the sanctioned probe.

## A misrouted *wave*: leave it in place, then check which repo the PR came from

For a linked graph (anchor + dependents) already running on the wrong board, do not re-create or
move it. Re-creating duplicates every deliverable and re-exposes every child as `ready` in the
window before you relink, so the dispatcher spawns a second worker per card. Leave the graph where
it is, say plainly on the card and to the operator that it is there, and use `--board <that board>`
on every later `show` / `complete` / `comment` call — including the ones you would otherwise let
default.

Then verify the thing a wrong board can actually corrupt: **which repository the worker cut its
branch from**. A card on another project's board can hand that project's workspace to the worker, so
before reviewing any PR out of a misrouted card, confirm the PR targets the intended repo and that
its head is fresh off that repo's `origin/main` — `gh pr view <n> --json baseRefName,headRefName,headRefOid`,
then `git merge-base <head> origin/main` from the intended checkout and check the merge-base is a
recent `origin/main` commit. A gate measured in the wrong tree prints numbers indistinguishable from
a gate measured in the right one.
