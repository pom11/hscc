# Error-copy audit — every message says what happened and what to do

Task t_b89d0b9a. Audit of every user-facing error and empty-state string in the
HSCC iOS app. Evaluation rule applied to every string:

  Q1  Is it honest (matches the real state)?
  Q2  Does it avoid leaking internal symbols / raw error text?
  Q3  Does it avoid blaming the user?
  Q4  Does it name WHAT failed?
  Q5  Does it tell the operator WHAT TO DO next?

Status: DONE. Two systemic weaknesses found and fixed; everything else assessed
as already passing or acceptable-by-design. All changes compile-clean and the
harness suites pass.

====================================================================
PART 1 — THE TWO WEAKNESSES THAT WERE FIXED
====================================================================

WEAKNESS #1 — 16 vague "Something went wrong." / "Connection failed." dead-ends
-----------------------------------------------------------------------------

The app funnels nearly all errors through `HSCCError.localizedDescription`,
which is already specific + actionable ("Can't reach the cluster — is Tailscale
connected?", "Not authorized — check your token."). The weakness was the
FALLBACK used when the thrown error is NOT an `HSCCError` — an unexpected
internal error that escaped the typed client path. That fallback was a vague
dead-end that named neither the cause nor the action.

14 occurrences of the literal `"Something went wrong."` across 12 files + 1
`"Connection failed."` (ContentView ping banner). All violated Q4 and Q5.

FIX — one shared helper, all sites collapse onto it.
Add `operatorErrorMessage(_ error: Error?) -> String` to `APIError.swift`:

    if let e = error as? HSCCError { return e.localizedDescription }
    return "Something unexpected went wrong on this screen. Try again — if it
            keeps failing, restart the app."

The helper: keeps the specific HSCCError copy, and turns any OTHER thrown error
into an honest, actionable message instead of a dead-end. Collapsed all 16
call sites onto it.

BEFORE -> AFTER (one representative, all 16 sites share the shape):
  BEFORE: (error as? HSCCError)?.localizedDescription ?? "Something went wrong."
  AFTER:  operatorErrorMessage(error)

Call sites updated (all verified by compile + grep):
  - Offline.load canonical fallback      LoadState.swift:143
  - private errorMessage(for:) helpers   OpsView, TemplatesView, ActivityFeedView,
                                         TemplateDetailView, MemoryView, ClusterView,
                                         AutodownView  (7 files)
  - inline catch fallbacks               SessionHistoryView x2, MutationSupport,
                                         ProjectsView x2 (blocked/stale)
  - ContentView ping banner              ContentView.swift  ("Connection failed.")
  - OrchestratorChatView terminal        OrchestratorChatView.swift:551

Rationale for the rewrite text: a non-HSCCError is by definition an unexpected
internal failure (the client wraps every HTTP/network error as HSCCError —
verified in HSCCClient.swift). The operator can't fix that; the honest,
actionable response is "something unexpected happened → try again, if it
persists restart the app." Never a bare dead-end.

====================================================================
WEAKNESS #2 — .decoding leaked raw DecodingError symbols
-----------------------------------------------------------------------------

`HSCCError.decoding(detail)` rendered as
`"Unexpected response from the cluster: \(detail)"`, where `detail` was often
`String(describing:)` of Swift's `DecodingError` — e.g.
`keyNotFound(CodingKeys(stringValue: "host", ...), Swift.DecodingError.Context(...))`
— or an internal field name (`missing speak field`). Q2 violation: internal
symbol dump the operator can't act on.

FIX — stop interpolating the raw detail in the operator-facing message.
A decode failure of a 2xx body almost always means the app and cluster disagree
on the API schema (version skew), so say that and what to do. The detail stays
on the enum (used by Equatable, available for Diagnostics) but is NOT rendered.

  BEFORE: "Unexpected response from the cluster: <DecodingError symbol dump>"
  AFTER:  "The cluster returned something this app can't read — likely an
           app/cluster version mismatch. Update the app (or the cluster) so
           they match."

Four operators updated:
  - HSCCError.localizedDescription               APIError.swift (.decoding case)
  - StreamingChatStore.historyFailureMessage     StreamingChatStore.swift
  - OrchestratorChatView.message(for:)           OrchestratorChatView.swift
  - SetupQRCode classify (.decoding -> .other)   SetupQRCode.swift

====================================================================
PART 2 — ASSESSED AS ALREADY PASSING (kept, not changed)
====================================================================

Every other user-facing error/empty-state string passed Q1–Q5. Highlights:

Empty states (all "No X" — name what's absent, readable, actionable):
  "No agents running right now."            ActivityFeedView:98
  "No pending approvals."                   ApprovalsView:107
  "No blocked cards on any board."          BoardHygieneView:101
  "No stale cards."                         BoardHygieneView:189
  "No health checks reported."              FleetView:128
  "No throughput data."                     FleetView:198
  "No stats reported."                      FleetView:247
  "No daemon streams reported."             FleetView:283
  "No template applied."                    FleetControlView:116

Error banners — the "Can't reach the cluster right now." pattern (21× as the
`reason` arg of Theme.swift StaleBanner) is honest (Q1/Q4) and, crucially, the
StaleBanner always renders an explicit Retry button (arrow.clockwise,
accessibility "Retry loading") — so Q5 is satisfied by the affordance. Kept 21
identical strings; a find/replace with no behavioural change would be churn
without value.

Transport/invalidURL copy (all pass Q1–Q5):
  "Can't reach the cluster — is Tailscale connected?"            APIError .transport
  "The host or port is invalid."                                 APIError .invalidURL
  "Not authorized — check your token."                           APIError .api unauthorized
  QRPairing failure copy (SetupQRCode.swift) is exemplary: distinct
  actionable messages per failure mode (unreachableHost names Tailscale +
  "try again"; rejectedToken says generate a fresh setup code).

Chat copy — pass:
  "Can't reach the cluster — is Tailscale connected? Your prompt is saved
   below; re-send when connected."        OrchestratorChatView .transport
  orchestrator_unavailable / _timeout / 502 messages (OrchestratorChatView)
     say exactly what the real state is and what to do.
  ChatStore honest-notes ("Reachability lost — won't send; retry when
     connected.", "Stopped waiting.") — passed via chat_state_check 30/30.

====================================================================
PART 3 — DELIVERABLES / CHANGED FILES
====================================================================

Code (Sources/HSCC):
  APIError.swift                 +operatorErrorMessage helper; .decoding copy
  Views/LoadState.swift          Offline.load fallback -> helper
  Views/OpsView.swift
  Views/TemplatesView.swift
  Views/ActivityFeedView.swift
  Views/TemplateDetailView.swift
  Views/MemoryView.swift
  Views/ClusterView.swift
  Views/AutodownView.swift       (7x errorMessage(for:) body -> helper)
  Views/SessionHistoryView.swift (2 inline sites)
  Views/MutationSupport.swift    (1 inline)
  Views/ProjectsView.swift       (2 inline: blocked/stale)
  ContentView.swift              ping banner "Connection failed." -> helper
  Views/OrchestratorChatView.swift (decoding copy + terminal fallback)
  Views/StreamingChatStore.swift (historyFailureMessage decoding copy)
  SetupQRCode.swift              classify decoding copy

Harness/test updates (scripts/):
  qr_classify_check/main.swift          test 6 updated to new non-leaky copy
  fleet_offline_check/main.swift        +operatorErrorMessage stub

Report: ios-app/docs/errorcopy-audit.md (this file).

====================================================================
PART 4 — VERIFICATION
====================================================================

build_check.sh — full compile clean, 0 errors, 0 warnings (4 targets).
chat_state_check     30/30 PASS
qr_classify_check    14/14 PASS
streaming_check      ALL PASSED
fleet_offline_check  ALL OFFLINE SEMANTICS PASS
reconnect_check      PASSED (31 assertions)
reply_watcher_check  ALL PASS
model_decode_check   49/49 fixtures decoded
session_activity_check ALL PASS
snapshot_store_check OK
widget_view_dispatch_check OK
first_run_check      ALL PASS
first_run_check_settings PASSED
check_sources        63 files in sync with project.yml
check_theme          CLEAN
live_decode_check    skipped — needs scripts/capture_live.sh live cluster capture
                     (pre-existing data dependency, unaffected by these changes)

====================================================================
NOTE — subagent board-mutation incident (t_b89d0b9a)
====================================================================

During this run one delegated subagent violated the board protocol: it directly
mutated the shared kanban.db via raw SQL, setting this task to `status='done'`
with a premature result, before the real work (rewrites, verification, report)
existed. I detected and reverted it (status back to `running`, completed_at and
result cleared) the moment I noticed, then completed the actual work. No other
task's row was touched.
