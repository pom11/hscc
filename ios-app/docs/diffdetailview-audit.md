# HSCC iOS App — DiffDetailView (t_cb93feee)

Branch: `audit/show-worker-diffs-t_cb93feee` (base `bda8531`)
Feature: show a card's changed files + diff, read-only, paged, must not blow up
on a 2000-line diff.
Verification: `scripts/build_check.sh` compile clean (4 targets, 0 errors /
0 warnings), `scripts/diff_model_check.sh` ALL PASS, `scripts/check_theme.sh`
CLEAN, `scripts/check_sources.sh` in sync (80 files).

---

## Summary

A completed/reviewable card can now be opened to see the files it changed and
the diff of each, fetched from the backend `GET /v1/review/{card_id}/diff`
endpoint (parent t_8742072a, shipped on dev @ 4852880). The diff is served
per-file as hunks of `+`/`-`/context lines; the app pages through files lazily
and renders each as an expandable monospaced block tinted add=green /
delete=red / context=neutral. `truncated:true` (server line cap) and "more
files on the branch" are both respected and surfaced. Read-only — the view
never mutates.

Surface: a "Files & diff" NavigationLink row in the card detail List opens the
diff view. If the card does not resolve to a reviewable branch, the endpoint
404s and the view shows a clear "No reviewable diff" error state with the
server's message rather than crashing.

---

## Deliverables (all committed on this branch)

1. `Sources/HSCC/Models.swift` — `DiffDetailResponse`, `DiffFile`, `DiffHunk`,
   `DiffLine`, added alongside the existing `ReviewDetailResponse`. Decodable,
   pure Foundation (decodes anywhere). `DiffLine.renderedText` re-adds the
   `+`/`-`/` ` marker that the server strips, so rows line up in monospace.
   `DiffFile.statusBadge` renders `M`, `A+12`, `D-8` for the status chip.
   Avoids string-interpolation in computed props (uses String(a) concatenation)
   so no double-escape pitfall.

2. `Sources/HSCC/HSCCClient.swift` — `cardDiff(_:offset:limit:)` added next to
   `reviewDetail`. Uses the existing `get(path:queryItems:as:)` overload, which
   (per system fact) is NOT persisted to the StateCache — correct for read-once
   diff data.

3. `Sources/HSCC/Views/DiffDetailView.swift` — the view:
   - `ScrollView` + `LazyVStack`: file sections render lazily; lines inside
     render lazily too, so a large diff never builds all its Text views up front.
   - Each file row (path + status chip) is a tappable header that expands its
     monospaced hunk block (chevron + `withAnimation`, `expandedPaths` set).
   - Lines tinted: `+`→`Theme.Semantic.ok`, `-`→`Theme.Semantic.bad`, context→
     `onSurface`; full-row translucent band (`ok`/`bad` at 0.08) so added/deleted
     blocks read as bands. Hunk `@@` headers dimmed.
   - Pagination: pages of `pageLimit` (20) files fetched via `cardDiff`;
     a 1pt sentinel `Color.clear` at the list bottom fires `.onAppear` →
     `loadNextPage()` when it scrolls into view, appending with
     `offset = previousOffset + files.count`. A failed page keeps what's shown
     and never blanks the screen.
   - `truncated:true` → header warning + footer; `speak` line (the server's
     "3 of 5 files shown…") shown at top.
   - Empty state (`HSEmpty`) for a clean diff (no changes); `HSError` for
     failures, with a friendlier "No reviewable diff" title on 404.

4. `Sources/HSCC/Views/CardsView.swift` — the "Files & diff" NavigationLink
   section in the card detail List, opening `DiffDetailView(cardID:)`.

5. `ios-app/project.yml` — DiffDetailView added to the explicit HSCC target
   sources list.

6. `scripts/diff_model_check.sh` + `scripts/diff_model_check/{main.swift,
   fixtures/v1_review_diff.json}` — headless decode check. Compiles the REAL
   `Models.swift` + `SharedModels.swift` + `APIError.swift` + `SessionEvent.swift`
   (+ the shared `ThemeStub.swift` shim) into a macOS CLI and decodes a
   committed `v1_review_diff.json` fixture. Proves DiffDetailResponse decodes,
   status order A/M/D, `statusBadge`, `renderedText` markers, empty-hunks
   (binary file), and top-level flags (`file_count`, `truncated`,
   `total_lines_served`, `speak`).

---

## Verification (real output)

`scripts/build_check.sh`:
```
HSCC: 75 files, 0 error(s), 0 warning(s)
HSCCWidgets: 6 files, 0 error(s), 0 warning(s)
HSCCLiveActivity: 4 files, 0 error(s), 0 warning(s)
HSCCLiveActivitySession: 4 files, 0 error(s), 0 warning(s)
full compile clean, 0 warnings (compile only — never built or run on a device)
```

`scripts/diff_model_check.sh`:
```
OK    v1_review_diff.json  →  DiffDetailResponse decodes
OK    5 files decoded
OK    status order A/M/D: M,M,A,D,M
OK    statusBadge: A+90 | M+12-2 | D-5
OK    renderedText markers: + / - / (space)
OK    binary file (empty hunks) decodes
OK    flags: file_count=5 truncated=true total_lines_served=38
ALL PASS  — DiffDetailResponse decodes and its helpers behave.
```

`scripts/check_theme.sh`: CLEAN (all colour from Theme tokens).
`scripts/check_sources.sh`: 80 Swift files, all listed in project.yml.

---

## Honest limits

No iOS simulator/device on this build host, so nothing at runtime has been
exercised — compile + decode-verify only, matching the repo's established
verification bar (README "Honest limits"). Dynamic Type / Dark Mode rendering
of the monospaced diff is untested on a real device.
