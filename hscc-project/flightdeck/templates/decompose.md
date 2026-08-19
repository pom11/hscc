Decompose this into atomic cards on the {{project}} board.

GOAL: {{goal}}

REPLY WITH A SINGLE JSON OBJECT ONLY. Do NOT create, modify, claim or
dispatch any kanban card. Do NOT run any tool. This is a request for a
PROPOSAL — a draft for review, not work to be executed. flightdeck will create
the cards itself, AFTER gating the proposal, only when the operator runs it
with --apply. Any card you create directly will be ignored and archived, so
acting has no payoff: return only the proposal.

Project context (derived — do not retype or restate it; it is already filled):
- Project: {{project}}
- Repo: {{repo}}
- Current branch: {{branch}}
- Verify command: {{verify}}
- Roadmap "Now": {{roadmap_now}}
- Open cards: {{open_cards}}
- Awaiting review: {{awaiting_review}}

EVERY CARD MUST MEET EVERY RULE, BY CONSTRUCTION:
1. EXACTLY ONE CONCERN per card — one command, one module, one behaviour.
   A card that bundles several concerns stalls. Split until each card is atomic.
2. A VERIFY: line on every card — the exact command that proves the work lands.
3. CONCRETE file/function references — exact paths, function names, line
   numbers where the task touches an existing module. If you cannot name the
   seam, LOCATE it first and inject it; do not leave the worker to guess.
4. ACCEPTANCE CRITERIA phrased so a test would FAIL if the feature were
   removed. Not "it works" — "a test asserting X fails when X is gone".
5. DEPENDENCY ORDER between cards — each card lists its prerequisites, and
   dependent cards wait for their parents.
6. One card per output entry, each scoped to its own branch/worktree.

Produce the complete, ordered card set now.
