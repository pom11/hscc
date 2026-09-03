# Show worker diffs — API gap record (t_178cb1a0)

Feature: for a completed card, show the files changed and the diff, read-only,
paged/scrollable, must not blow up on a 2000-line diff.

## Verdict

**The HSCC HTTP API serves NO commit diff content.** This card is an API-gated
feature gap: no endpoint returns a unified diff / patch / file-contents diff
for a worker's completed card, so the iOS app cannot display one. Per the
card's instruction, we record exactly what is missing rather than inventing a
route (inventing one would mean executing `git diff` server-side and shipping
file contents through the API — an api-engineer decision, and one with real
security taste).

## What DOES exist (all read-only, none serve diff text)

The iOS app already consumes these card/review endpoints:

1. `GET /v1/cards/{card_id}` — the raw kanban card record.
   - Route: `routes_project.py::handle_card_detail` (line 202).
   - Fields: kanban card dict (id, title, status, assignee, board, workspace_path, branch, etc.). No diff.

2. `GET /v1/review/{card_id}` — DRY-RUN review facts (read-only, never merges).
   - Route: `routes_project.py::handle_review_detail` (line 240).
   - Payload via `flightdeck/commands/review.py::_render_json` (line 378):
     - `subject` (last commit message)
     - `files_changed` (INT count only — from `git diff --numstat`)
     - `insertions`, `deletions` (INT counts only)
     - `conflicts`, `landed`, `verify`, `dependents`
   - `_branch_facts` (review.py:218) runs `git diff --numstat base...branch`
     and parses COUNTS. It never emits patch text.

3. `GET /v1/why/{card_id}` — the card's full story (kanban + git facts).
   - Route: `routes_project.py::handle_why` (line 530).
   - Payload via `flightdeck/commands/why.py::gather` + `render_json`:
     - `commits`: list of commit SUBJECTS (messages only)
     - `uncommitted`: list of file NAMES
     - `branch`, `branch_exists`, `landed`, `workspace_path`, `is_worktree`, `verdict`
   - Again metadata only. Commit subjects ≠ diff content.

## What is MISSING

The API has a card → branch → project mapping already (both `review` and `why`
resolve a card to a branch and repo). It already shells out to git read-only on
the server (`git diff --numstat`, `git log`, `git status`). What is missing is
the payload — an endpoint that returns the actual diff for a card's branch:

- No endpoint runs `git diff base...branch` for the operator.
- No endpoint returns changed file paths WITH their patch bodies.
- No endpoint returns per-file or whole-diff content, no paging/offset params,
  no truncation handling.

## Proposed shape (for the follow-up api + ios task, NOT built here)

API endpoint (api-engineer owns it):
  `GET /v1/review/{card_id}/diff` (or extend `/v1/review/{id}` with a
  `?diff=1` / paginated `files` array). Read-only, mirrors the existing
  resolve path in `handle_review_detail`. Return per-file:
  `[{path, status, additions, deletions, hunks: [{header, lines:[{type: +|-|context, text}...]}...]}]`
  Paged via `?offset=&limit=` or `?file=`; server caps total lines and reports
  `truncated:true` so a 2000-line diff degrades, never blows up.

iOS side (ios-engineer owns it):
  - New `DiffDetailResponse` model in `Models.swift` (alongside the existing
    `ReviewDetailResponse`).
  - New `DiffDetailView` in `Views/`, opened from the card detail /
    review-queue row: a `List`/`ScrollView` of file sections, each an expandable
    monospaced block of `+`/`-`/context lines (a few accent/red/green colours,
    no full syntax highlighting needed per the card — optional).
  - Lazy-loading (`List` + `LazyVStack`, or paginated fetch) so a large diff
    renders incrementally without blocking the main thread.
  - HSCC `get` with queryItems → NOT cached by the StateCache (system finding:
    query-param GETs are never persisted). Fine here: diffs are read-once
    review data, no offline requirement.

## Security note

Serving diffs means the server ships file contents (possibly including
secrets) to the phone. The API is already auth-gated (bearer token) and the
cluster only reaches the tailnet host (`100.64.0.1`, never a public address).
Still, the follow-up task should be deliberate about not adding a route that
dumps raw file contents unauthenticated. Diff render should stay read-only and
scoped to cards the operator can already see.

## Next step

One follow-up card, assigned to `api-engineer`, to add the diff endpoint; then
this ios task (or a child) to build `DiffDetailView` against it. The iOS side
is fully understood and scoped here; it is blocked purely on API delivery.
