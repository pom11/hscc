# Screen audit: SearchView (t_51bad645)

Full audit of `ios-app/Sources/HSCC/Views/SearchView.swift` — cross-project search.

## Status
IN PROGRESS.

## How to read this
Every finding cites file:line + the command whose output proves it. Findings
marked **[executed]** were proven by real tool output (compile, live API, a harness).
Findings marked **[reasoning]** are static-code reasoning with no runtime here.

## DATA IN — endpoints that feed it
SearchView loads two sources concurrently in `load()`:
- `loadProjects` → `client.projects()` → `GET /v1/projects` (SearchView.swift:267-273)
- `loadCards` → `client.cards()` → `GET /v1/cards` (SearchView.swift:275-281)
Both are plain GETs (no query items) so both are StateCache-persisted (offline works).

[to be filled]

## RENDER
[to be filled]

## STATES
[to be filled]

## CONTROLS
[to be filled]

## OBSERVATION
[to be filled]

## LAYOUT
[to be filled]

## ACCESSIBILITY
[to be filled]

## FIXES
[to be filled]

## DEFERRED
[to be filled]
