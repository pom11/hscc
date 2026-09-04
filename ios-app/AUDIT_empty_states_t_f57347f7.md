# Audit: Honest empty states and first-run guidance on every screen

Card: t_f57347f7
Branch: audit/empty-states-t_f57347f7
Base: 6510da5 (operator dev head)

Task: audit every screen for the four UI states (loading, empty-success, error,
stale) and make each unmistakable, with a clear next action where one exists.
Reuse the Offline.load/.stale machinery. Report wrong screens with file:line,
fix them, verify with the compile gate, commit.

## Method

For each View file: read how the LoadState/Offline.load result is consumed and
how each of the four states is rendered. A state is a defect if (a) it is not
visually distinct from a sibling state, or (b) an inspectable error state has no
clear next action, or (c) first-run / not-configured guidance is missing or
mutates silently.

## Headline finding

The app is already built to a high four-state standard. Theme.swift ships a
single shared component set (HSLoading, HSError, HSEmpty, HSConnectGate,
HSErrorLabel, HSEmptyLabel, StaleBanner, HSSectionCard) and nearly every read
routes through Offline.load which produces .loading / .loaded / .stale / .failed
with a held .value. Prior audit cards already handled the central screens.

The one systemic, fixable defect on this card: the shared inline error
component HSErrorLabel rendered a bare red Label with NO retry button. Used by
10 views at ~20 `.failed` call sites, so the error state had no on-screen next
action (only pull-to-refresh). This card fixed it in two pieces (below).

## Fix 1 — HSErrorLabel gains an optional retry button

Views/Theme.swift:382 — added `var retry: (() -> Void)? = nil` to HSErrorLabel.
When a retry closure is provided it renders a "Try again" button beneath the
red message, turning the error state into red icon + message + explicit
next action. Backward compatible: callers that pass no retry get the old bare
label. (init(message:retry:) keeps the existing labeled call form.)

## Fix 2 — wire a real retry at every retryable `.failed` inline-error site

Each `.failed` now passes a Task that re-invokes the same load the view uses
for .refreshable / .task, so the button and pull-to-refresh always reload the
same source:

- SessionsView.swift:127  retry -> load(client)
- MemoryView.swift:205    retry -> load(client)
- ActivityFeedView.swift:90  retry -> load(client)
- AutodownView.swift:104  retry -> loadStatus()
- ServingControlView.swift:111 retry -> loadStatus()
- LogsView.swift:57       retry -> load(client)
- TemplatesView.swift:111 (applied status -> refreshStatus()) and :207
  (library list -> loadList(client)) — replaced a passive "Pull to retry"
  caption with an actual retry button.
- TemplateDetailView.swift:210 preview retry -> loadPreview() — replaced the
  passive "Couldn't load preview" caption with a retry button.
- FleetControlView.swift:74 retry -> loadStatus()
- FleetView.swift:107/159/212/301/361 retry -> loadHealth/loadThroughput/
  loadStats/loadStreams/loadAutoscale (each section's own load).
- OpsView.swift:112/167/213/285/331 retry -> loadVerify/loadDaemon/loadTriggers/
  loadEscalations/loadProfiles. The daemon/profiles sections are computed vars
  where the stored `client` is optional, so their retries guard `if let c =
  client` before calling the non-optional load (same pattern the view already
  uses in loadAll).
- ClusterView.swift:127 topology strip .failed now renders HSErrorLabel with
  retry -> loadAll(), instead of a bare red Label.

Every view's local `errorLabel(_:)` helper was updated to forward the retry
(parametrized). No old-style single-arg errorLabel or bare `errorLabel(message)`
call remains (grepped: 0).

## Views confirmed correct (distinct states + next action), no change needed

- SessionsView, MemoryView, SearchView, SessionHistoryView, TemplateDetailView,
  AutodownView, ProjectsView, FleetView — four states all distinct; reason rich.
- BoardHygieneView — .failed uses full HSError WITH retry (L73, L161); stale
  uses StaleBanner; empty distinct ("No blocked cards on any board.").
- ProfileEditorView — HSError with retry (L38).
- SelfHealHistoryView — HSError with retry (L58).
- ApprovalsView — retry present.
- ServingControlView, LogsView, TemplatesView, FleetControlView, OpsView —
  after Fix 2.
- StreamingChatView — failedState is an unmistakable connect-failure pane
  (triangle icon + reason + "pull to reconnect" guidance); chat-specific, fine.
- ClusterView — topology strip now has retry (Fix 2); NodeTopologyView is
  purely presentational (driven by passed-in pairs, no own load state).
- SettingsView, CreateCardSheet — local settings / mutation sheets with no
  remote read state; no four-state surface.

## First-run / not-configured guidance

Every data screen routes through HSConnectGate when the client is unconfigured
(no host/port/token), which shows the exact settings to fill in — e.g.
OpsView.swift:51, BoardHygieneView.swift:59, ClusterView/others. ContentView
routes to a connectivity gate. No screen silently mutates on first run.

## Verification

scripts/build_check.sh (full SIL-level compile of every target with each
target's real file set from project.yml) — HSCC app 72 files, HSCCWidgets 6,
HSCCLiveActivity 4, HSCCLiveActivitySession 4: 0 errors, 0 warnings, all four
targets.

## Files changed

- ios-app/Sources/HSCC/Views/Theme.swift          (HSErrorLabel retry param)
- ios-app/Sources/HSCC/Views/SessionsView.swift
- ios-app/Sources/HSCC/Views/MemoryView.swift
- ios-app/Sources/HSCC/Views/ActivityFeedView.swift
- ios-app/Sources/HSCC/Views/AutodownView.swift
- ios-app/Sources/HSCC/Views/ServingControlView.swift
- ios-app/Sources/HSCC/Views/LogsView.swift
- ios-app/Sources/HSCC/Views/TemplatesView.swift
- ios-app/Sources/HSCC/Views/TemplateDetailView.swift
- ios-app/Sources/HSCC/Views/FleetControlView.swift
- ios-app/Sources/HSCC/Views/FleetView.swift
- ios-app/Sources/HSCC/Views/OpsView.swift
- ios-app/Sources/HSCC/Views/ClusterView.swift
- ios-app/AUDIT_empty_states_t_f57347f7.md        (this report)
