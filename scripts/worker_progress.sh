#!/bin/bash
# worker_progress.sh — report what each running kanban worker has actually produced.
#
# Three separate measurement bugs made earlier checks report "zero output" for
# workers that were demonstrably writing code, and healthy cards were reclaimed
# as a result:
#   1. `find -newermt '-40 minutes'` silently matches nothing on BSD/macOS find.
#      Use -mmin.
#   2. `started_at` is NOT reset when a card is reclaimed and re-dispatched, so
#      elapsed time can read hours for a worker that started minutes ago.
#   3. The checkout path is NOT fixed. Observed layouts under one board:
#      <ws>/ios-app, <ws>/hscc, <ws>/wt, and the repo cloned at <ws> itself.
#      Discover the git root; never hardcode it.
#   4. The WORKSPACE ROOT is not fixed either — that is bug 3 one level up, and
#      it was still here after bug 3 was "fixed". A card created with
#      `--workspace worktree` lives at <repo>/.worktrees/<id>, NOT under the
#      board's scratch workspaces dir, so hardcoding the scratch dir reported
#      NO WORKSPACE for three demonstrably-working workers on 2026-09-08. The
#      card itself states the truth ("workspace: <kind> @ <path>") — ask it.
#
# Ground truth is the workspace's own git state.
# Usage: scripts/worker_progress.sh
set -uo pipefail
BOARD_WS="$HOME/.hermes/kanban/boards/hscc/workspaces"

# Resolve a card's workspace from the CARD, not from a guessed directory.
# `hermes kanban show` prints e.g. "workspace: worktree @ /path/to/ws".
# Falls back to the scratch dir when the line is absent.
resolve_ws() {
  local t="$1" path
  path=$(hermes kanban show "$t" 2>/dev/null \
         | sed -n 's/^[[:space:]]*workspace:[[:space:]]*[a-z]*[[:space:]]*@[[:space:]]*//p' \
         | head -1)
  [ -n "$path" ] && [ -d "$path" ] && { echo "$path"; return; }
  echo "$BOARD_WS/$t"
}

running=$(hermes kanban list --json 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin); t=d if isinstance(d,list) else d.get('tasks',[])
print(' '.join(x['id'] for x in t if x.get('status')=='running'))")

[ -z "$running" ] && { echo "no running cards"; exit 0; }

for t in $running; do
  ws="$(resolve_ws "$t")"
  if [ ! -d "$ws" ]; then echo "$t  NO WORKSPACE"; continue; fi
  # Find the git root: the workspace itself, or the shallowest dir containing .git
  root=""
  if [ -e "$ws/.git" ]; then
    root="$ws"
  else
    root=$(find "$ws" -maxdepth 2 -name .git -print 2>/dev/null | head -1)
    root="${root%/.git}"
  fi
  recent=$(find "$ws" -type f -mmin -20 2>/dev/null | grep -v '/\.git/' | wc -l | tr -d ' ')
  if [ -z "$root" ]; then
    echo "$t  no git checkout  files_touched_20min=$recent"
    continue
  fi
  dirty=$(git -C "$root" status --short 2>/dev/null | wc -l | tr -d ' ')
  head=$(git -C "$root" log --oneline -1 2>/dev/null)
  ahead=$(git -C "$root" log --oneline origin/dev..HEAD 2>/dev/null | wc -l | tr -d ' ')
  verdict="IDLE"
  { [ "$dirty" -gt 0 ] || [ "$ahead" -gt 0 ]; } && verdict="PRODUCING"
  [ "$dirty" -eq 0 ] && [ "$ahead" -eq 0 ] && [ "$recent" -gt 0 ] && verdict="reading"
  echo "$t  $verdict  dirty=$dirty commits_ahead=$ahead touched20m=$recent"
  echo "    root: ${root#$HOME/}"
  echo "    head: $head"
  [ "$dirty" -gt 0 ] && git -C "$root" status --short 2>/dev/null | sed 's/^/      /' | head -8
  [ "$ahead" -gt 0 ] && git -C "$root" log --oneline origin/dev..HEAD 2>/dev/null | sed 's/^/      /' | head -5
done

exit 0
