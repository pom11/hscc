# Show worker diffs — API gap record (t_178cb1a0) + shipped status

Feature: for a completed card, show the files changed and the diff, read-only,
paged/scrollable, must not blow up on a 2000-line diff.

## STATUS: API ENDPOINT SHIPPED (t_8742072a)

The backend gap described below is now CLOSED. `GET /v1/review/{card_id}/diff`
serves the per-file diff for a reviewable card, read-only, with file-level
paging and a server-side line cap. The iOS side (DiffDetailView) is a separate
follow-up card (t_cb93feee) and is no longer API-blocked.

What shipped (all in branch `dev`, commit landed by backend-engineer):

- `hscc-api/routes_project.py::handle_review_diff` — resolves card → project →
  branch exactly like `handle_review_detail`, runs read-only git, returns:
  ```json
  {
    "id", "project", "repo", "branch", "base",
    "offset", "limit",
    "files": [{"path", "status": "A"|"M"|"D", "additions", "deletions",
               "hunks": [{"header", "lines": [{"type": "+"|"-"|"context", "text"}]}]}],
    "file_count", "truncated", "total_lines_served",
    "speak"
  }
  ```
- Paging: `?offset=<file index>&limit=<max files>` (defaults 0 / 20, limit
  capped at 200) — file-level pagination so the iOS view lazy-loads files.
- Truncation: `?max_lines=<cap>` (default 2000, absolute cap 20000) bounds the
  number of diff LINES served; when exceeded, the response stops early and sets
  `truncated: true`. A 2000-line diff degrades gracefully, never blows up.
- 404 when the card does not resolve to a reviewable branch (mirrors
  review_detail), or when the branch does not exist in the repo.
- Read-only: the handler only resolves + runs `git diff`; it never merges,
  closes, or mutates (guarded by a no-mutation test).
- Parsing lives in `hscc-project/flightdeck/commands/review.py`:
  `_branch_diff(repo, branch, base, _run)` (read-only git) and
  `_parse_patch(patch)` (unified-diff → per-file hunks of typed lines).
- Tests: `hscc-api/tests/test_routes_project.py` — 9 new cases covering shape,
  404 (unresolvable + missing branch), pagination ranges, line-cap truncation,
  invalid-query degradation, and no-mutation.

---

## Original gap record (superseded by the shipped endpoint above)

## Verdict (as recorded, 2026-09-03)

**The HSCC HTTP API serves NO commit diff content.** This card is an API-gated
feature gap: no endpoint returns a unified diff / patch / file-contents diff
for a worker's completed card, so the iOS app cannot display one. Per the
card's instruction, we record exactly what is missing rather than inventing a
route (inventing one would mean executing `git diff` server-side and shipping
file contents through the API — an api-engineer decision, and one with real
security taste).

## What DID exist (all read-only, none served diff text)

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
     and parses COUNTS. It never emitted patch text.

3. `GET /v1/why/{card_id}` — the card's full story (kanban + git facts).
   - Route: `routes_project.py::handle_why` (line 530).
   - Payload via `flightdeck/commands/why.py::gather` + `render_json`:
     - `commits`: list of commit SUBJECTS (messages only)
     - `uncommitted`: list of file NAMES
     - `branch`, `branch_exists`, `landed`, `workspace_path`, `is_worktree`, `verdict`
   - Again metadata only. Commit subjects ≠ diff content.

## What was MISSING (now shipped)

The API had a card → branch → project mapping already (both `review` and `why`
resolve a card to a branch and repo) and already shelled out to git read-only
on the server (`git diff --numstat`, `git log`, `git status`). What was missing
was the payload — an endpoint that returns the actual diff:

- No endpoint ran `git diff base...branch` for the operator.  ✅ now `.../diff`
- No endpoint returned changed file paths WITH their patch bodies.  ✅
- No endpoint returned per-file or whole-diff content, paging, or truncation. ✅

## Proposed shape (now implemented — see STATUS above)

API endpoint:
  `GET /v1/review/{card_id}/diff` (read-only, mirrors the existing resolve path
  in `handle_review_detail`). Per-file:
  `[{path, status, additions, deletions, hunks: [{header, lines:[{type: +|-|context, text}...]}...]}]`
  Paged via `?offset=&limit=`; server caps total lines and reports
  `truncated:true` so a 2000-line diff degrades, never blows up.

iOS side (owned by t_cb93feee):
  - New `DiffDetailResponse` model in `Models.swift` (alongside the existing
    `ReviewDetailResponse`).
  - New `DiffDetailView` in `Views/`, opened from the card detail /
    review-queue row: a `List`/`ScrollView` of file sections, each an expandable
    monospaced block of `+`/`-`/context lines (a few accent/red/green colours,
    no full syntax highlighting needed — optional).
  - Lazy-loading (`List` + `LazyVStack`, or paginated fetch) so a large diff
    renders incrementally without blocking the main thread.
  - HSCC `get` with queryItems → NOT cached by the StateCache (system finding:
    query-param GETs are never persisted). Fine here: diffs are read-once
    review data, no offline requirement.

## Security note

Serving diffs means the server ships file contents (possibly including
secrets) to the phone. The API is already auth-gated (bearer token) and the
cluster only reaches the tailnet host (`100.64.0.1`, never a public address).
The shipped endpoint stays read-only and scoped to cards the operator can
already see (`/v1/review/{id}/diff` resolves through the same card→project
attribution review already uses), so it exposes no card the operator could not
already view via `review/{id}`.
