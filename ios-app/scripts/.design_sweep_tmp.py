#!/usr/bin/env python3
"""Sweep full-pane loading/error states onto the shared HSLoading/HSError/HSEmpty
components. Uses EXACT string replacement (no regex) to avoid substring corruption."""
import pathlib, sys

V = pathlib.Path("/Users/desac/dev/hscc/.worktrees/t_c16a1ae9/ios-app/Sources/HSCC/Views")

# (filename, old_exact, new_exact). Each old must occur exactly the stated count.
jobs = []

def job(fname, old, new, expect=1):
    jobs.append((fname, old, new, expect))

# ---------------- Loading: standalone full-pane ProgressView ----------------
job("ProjectsView.swift", 'ProgressView("Loading…")', 'HSLoading("Loading…")', expect=4)
job("CardsView.swift",    'ProgressView("Loading…")', 'HSLoading("Loading…")', expect=1)
job("BoardHygieneView.swift",
    'ProgressView("Loading…").task { await loadBlocked(client) }',
    'HSLoading("Loading…").task { await loadBlocked(client) }', expect=1)
job("BoardHygieneView.swift",
    'ProgressView("Loading…").task { await loadStale(client) }',
    'HSLoading("Loading…").task { await loadStale(client) }', expect=1)
job("BoardHygieneView.swift", 'ProgressView("Loading…")', 'HSLoading("Loading…")', expect=2)  # remaining bare ones
job("QRScannerView.swift", 'ProgressView("Requesting camera access…")',
    'HSLoading("Requesting camera access…")', expect=1)

# ---------------- Error: ContentUnavailableView w/ retry -> HSError ----------------
job("ProjectsView.swift",
    '''            ContentUnavailableView {
                Label("Couldn't load projects", systemImage: "exclamationmark.triangle")
            } description: {
                Text(message)
            } actions: {
                Button("Try again") { Task { await load() } }
            }''',
    '''            HSError("Couldn't load projects", message: message) {
                Task { await load() }
            }''', expect=1)

job("ProjectsView.swift",
    '''                ContentUnavailableView {
                    Label("Couldn't load project", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(message)
                } actions: {
                    Button("Try again") { Task { await load() } }
                }''',
    '''                HSError("Couldn't load project", message: message) {
                    Task { await load() }
                }''', expect=1)

job("ProjectsView.swift",
    '''                ContentUnavailableView {
                    Label("Couldn't load the board", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(message)
                } actions: {
                    Button("Try again") { Task { await load() } }
                }''',
    '''                HSError("Couldn't load the board", message: message) {
                    Task { await load() }
                }''', expect=1)

job("SearchView.swift",
    '''            ContentUnavailableView {
                Label("Couldn't search", systemImage: "exclamationmark.triangle")
            } description: {
                Text(m)
            } actions: {
                Button("Try again") { Task { await load() } }
            }''',
    '''            HSError("Couldn't search", message: m) {
                Task { await load() }
            }''', expect=1)

job("CardsView.swift",
    '''                ContentUnavailableView {
                    Label("Couldn't load card", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(loadError.localizedDescription)
                } actions: {
                    Button("Try again") { Task { await load() } }
                }''',
    '''                HSError("Couldn't load card", message: loadError.localizedDescription) {
                    Task { await load() }
                }''', expect=1)

job("BoardHygieneView.swift",
    '''                ContentUnavailableView {
                    Label("Couldn't load blocked cards", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(message)
                } actions: {
                    Button("Try again") { Task { await loadBlocked(client) } }
                }''',
    '''                HSError("Couldn't load blocked cards", message: message) {
                    Task { await loadBlocked(client) }
                }''', expect=1)

job("BoardHygieneView.swift",
    '''                ContentUnavailableView {
                    Label("Couldn't load stale cards", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(message)
                } actions: {
                    Button("Try again") { Task { await loadStale(client) } }
                }''',
    '''                HSError("Couldn't load stale cards", message: message) {
                    Task { await loadStale(client) }
                }''', expect=1)

# ---------------- Empty: ContentUnavailableView w/o retry -> HSEmpty ----------------
job("SearchView.swift",
    '''            ContentUnavailableView {
                Label("No results for “\\(query)”", systemImage: "magnifyingglass")
            } description: {
                Text("Search project names, repos, boards, or card titles, ids, and statuses.")
            }''',
    '''            HSEmpty("No results for “\\(query)”",
                     message: "Search project names, repos, boards, or card titles, ids, and statuses.",
                     systemImage: "magnifyingglass")''', expect=1)

job("ProjectsView.swift",
    '''                    ContentUnavailableView {
                        Label("No cards on \\(board ?? "this") board", systemImage: "square.grid.2x2")
                    } description: {
                        Text("Nothing is open here yet. This section fills in as the project's board develops.")
                    }''',
    '''                    HSEmpty("No cards on \\(board ?? "this") board",
                             message: "Nothing is open here yet. This section fills in as the project's board develops.",
                             systemImage: "square.grid.2x2")''', expect=1)

# ---------------- Apply ----------------
errors = []
ok = 0
for fname, old, new, expect in jobs:
    p = V / fname
    s = p.read_text()
    c = s.count(old)
    if c != expect:
        errors.append(f"{fname}: expected {expect} occurrence(s) of >>{old[:50]}<<, found {c}")
        continue
    p.write_text(s.replace(old, new))
    ok += 1

if errors:
    print("FAILED:")
    for e in errors:
        print(" -", e)
    sys.exit(1)
print(f"Applied {ok}/{len(jobs)} replacements cleanly.")
