# Landing a superseded PR (the #317 / #332 pattern)

Situation: branch A is an open PR with a stale base; a worker has replayed the same work onto
current `main` and published it as a new ref + new PR, because `git push --force-with-lease` is
hard-blocked in worker runs. Two open PRs, one tree. You decide which side lands.

## Decide on measured facts, in this order

1. **Remote truth first.** `git fetch origin --prune` then `git ls-remote origin refs/heads/main
   refs/heads/<both branches>`. Never carry shas from a card body — main moves under you. Re-check
   immediately before any merge or close: it moved twice during one of these landings.
2. **What each PR really merges.** `git merge-tree --write-tree origin/main <head>` → line 1 is the
   merged tree sha, `grep '^CONFLICT'` gives the conflict list, `git diff --name-only origin/main
   <tree>` gives the true file count. NOT `git diff main..<head>` (that is a tree difference and
   tells you nothing about merge effect — see the pitfall in SKILL.md).
3. **The registration files.** In this repo, `ios-app/project.yml` and
   `ios-app/Sources/HSCCWidgets/HSCCWidgetsBundle.swift` are the hotspot: every widget card appends
   there, `-X ours` resolutions silently drop the branch's own append, and `build_check.sh` stays
   green when it happens. Read the actual hunks (`git diff <main>..<head> -- <those two files>`).
   A widget file present but unregistered is dead code that no build gate catches.
4. **Invariants, expressed as deltas, not absolutes.** "app target identical to main, widget target
   exactly +1" survives a divergent-clone mistake; an absolute count quoted in prose does not.

## Land it

- Re-run every gate the card claims, in a **clean detached worktree at the exact head sha**
  (`git worktree add --detach <path> <sha>`, unique name per run, `git worktree remove --force`
  when done — never in the primary checkout).
- **Squash merge** is the house convention for PRs #321+ (single-parent commits on `main`,
  `(#N)`-style subject). Set `--subject` yourself; no attribution lines.
- **Prove it landed by content, not ancestry.** After a squash merge `git merge-base --is-ancestor`
  and `git branch --contains` both say *not merged*, because the branch tip is not in main's history.
  The check is `git diff origin/main <merged-head>` — it must be **empty**. Also assert the
  first-parent commit exists and the new files are present in `origin/main`.
- Close the old PR with `gh pr close <n> --delete-branch --comment "$(cat file)"`. Write long
  comments to a file. Verify the stored length afterwards (`gh pr view <n> --json comments --jq
  '.comments[-1].body | length'`) — silent truncation is real. If the comment contains a claim you
  later find is wrong, **edit it in place** (`gh pr comment <n> --edit-last --body-file <file>`) and
  re-verify the false sentence greps to zero. Deleting it destroys the audit trail.

## Sibling fallout is yours to sequence, not to ignore

Moving `main` can break every sibling branch that touches the hotspot. Measure each with
`git merge-tree` against the NEW main (GitHub reports `UNKNOWN` for a minute while it recomputes,
so do not wait on it), then card the fallout with the facts in the body. Serialise cards that touch
the same hotspot files — concurrent rebases all appending to one `sources:` list is how a
registration gets eaten. Card what you found, in the card, even when it contradicts a card you
wrote an hour earlier: `main` moving under a card you authored is normal, and a stale card body is
your defect to correct, not the worker's to discover.

## What a worker cannot do

Force-push is blocked (single-query mode, no approver). So a rebase of an existing PR branch is
always: push to `<branch>-rebased`, open a new PR whose body says "supersedes #N", orchestrator
merges and closes the old one. Say this in the card body — otherwise the worker burns its budget
attempting a blocked command.
