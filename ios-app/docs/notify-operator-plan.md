# Notify the operator when work needs them — design + implementation plan

Card: t_a009792a  |  Assignee: ios-engineer  |  Branch: dev
Date: 2026-09-03

## Problem

The operator has to open the app to discover that a card finished, a card
failed, or something needs their review. On a phone that is backwards — the
whole point of an interrupt is that it happens without the operator looking.

This doc is a design + implementation plan. It says plainly what is achievable
with the current no-push-server setup and what is not, so we build the honest
achievable slice first and do not half-build an APNs path.

## What "needs the operator" actually means (the event classes)

NOT every card transition. Only conditions that warrant pulling someone in,
dereferenced to the API endpoints we already call:

| Event class | API signal | Endpoint(s) | "New" = transition |
|---|---|---|---|
| A card needs review | review queue non-empty | `GET /v1/review/queue` | queue gained an id not seen before |
| A card failed / is blocked | pending escalations, blocked cards | `GET /v1/escalate`, `GET /v1/kanban/blocked` | escalations count rose 0→n / a blocked card id is new |
| The fleet went unreachable | daemon down, health not ok, cannot reach API | `GET /v1/daemon/status`, `GET /v1/health`, `GET /v1/ping` | reachable→unreachable transition observed |

Detection must be **differential, not absolute**: the operator should be told
"a new card is waiting for review", not "review queue has 3 items" every poll.
We differ each observation against last-known state (see State model below)
and only fire when a condition *first* appears.

## What is achievable without a push server

### 1. Foreground local notifications (fully achievable, immediate)

While the app is running, any request path that discovers a needs-operator
condition can raise a local `UNUserNotification` banner immediately. This is
instant, requires no APNs, and needs only one-time notification authorization.

This covers "operator opens the app, something needs them, they get told right
away" and also catches the case where a background poll is running while the
app is foregrounded (banner via the delegate).

### 2. Background refresh local notifications (achievable, best-effort)

iOS's `BGAppRefreshTask` (set `UIBackgroundModes: [fetch]`, register an id with
`BGTaskScheduler`) opportunistically wakes the app a few times a day on the
operator's schedule. Each wake:

1. poll the event endpoints above against the active cluster,
2. diff against last-known state,
3. if a NEW needs-operator condition exists, fire a local notification,
4. persist the new last-known state and re-schedule the next refresh.

This is the honest core of the "something needs you" notification without a
push server. It is **not real-time** — iOS batches background refreshes around
app usage and can skip them entirely. It is right for sticky, durable
conditions (a card sits in needs-review; the fleet has been unreachable) and
wrong for anything that must interrupt *this instant* (see APNs section).

### 3. The existing Live Activity surface (assessment — do NOT make it the primary alert)

The app already runs two Live Activities (fleet wake, session mirror). It is
tempting to "extend the Live Activity to carry needs-review / unreachable
state". Honest assessment of why that is the wrong primary alert surface:

- A Live Activity is an **opt-in persistent bubble**, not a transient
  interrupt. Its whole design is "stay put until the operator dismisses it",
  the opposite of a notification you want them to act on and move past.
- A Live Activity **cannot be created from a background refresh**. It can only
  be `request`ed from a running foreground app process (LiveActivityManager
  only starts on a user tap). A background poll cannot raise one — it can only
  *update* one the app already started. So it cannot deliver the asleep-phone
  case at all.
- Live Activities are capacity-limited and best kept for their single in-flight
  operational purpose (wake progress), where they already work well.

Conclusion: keep Live Activities as-is for wake/session. Notifications carry the
needs-operator alerting. We may optionally reuse the *state snapshot that the
Live Activity/widgets already share* (`AppGroup` shared defaults) so the
notified condition is consistent with what the widget shows — but the alert
itself is a `UNUserNotification`.

## What cannot work without APNs (written down honestly, not half-built)

- **Real-time, server-initiated delivery while the app is killed / not open.**
  Only APNs can push an event to a phone that is not running the app. Background
  refresh is OS-scheduled and best-effort; it never guarantees immediacy.
- **Reliability for time-critical events** ("a card just failed NOW"). If APNs
  mattered, this is where. Without it, we accept that the operator learns of a
  failure on the next refresh slot (minutes-to-hours later), not instantly.
- **Any delivery when the user force-quits the app** from the app switcher —
  iOS does not run background refresh for force-quit apps. (This also kills the
  whole local-notify-by-refresh approach; nothing we do locally fixes it.)
- **Device token registration / remote-notification plumbing.** `registerForRemoteNotifications`
  needs an APNs signing key + a push server to send to the token. Not present, not
  half-built.

So the boundary is crisp: **without APNs we get immediate-alerting-while-open
plus occasional best-effort background checks; we do NOT get guaranteed
interrupt-anytime.** The plan below builds the achievable slice cleanly and
leaves a documented seam (the same notification-decision engine) that an
APNs-backed sender can plug into later without rework.

## Architecture

### Notification decision engine (shared, testable, APNs-ready)

A pure Swift function that turns "(prior state, current observations)" into "a
set of notifications to fire". No I/O, no network — unit-testable headlessly
(the pattern the repo already uses for SessionActivitySummary etc.).

```
enum NeedsOperatorNotifier {   // Sources/HSCC/Notify/NeedsOperatorNotifier.swift
    static func compute(
        prior: LastSeenState,
        now: ObservedState
    ) -> [OperatorAlert]          // [] when nothing new
}
```

`ObservedState` is assembled from the polled endpoints:

```swift
struct ObservedState {
    var reviewQueue = Set<String>()       // review id → needs review
    var blocked = Set<String>()           // blocked card ids
    var escalationsCount = 0              // pending escalations
    var apiReachable: Bool?               // nil = couldn't reach API this poll
    var daemonRunning: Bool?              // daemon status
}
```

`LastSeenState` is `ObservedState` persisted to the shared App Group defaults
(plus a per-condition "last notified fingerprint" so we don't re-notify a
condition that has already been announced once).

### State / dedup model (persisted in App Group shared defaults)

Two persistence keys, both in `AppGroup` shared suite so widgets + the notifier
agree:

- `hscc.notify.lastSeen` — the last `ObservedState`, as JSON.
- `hscc.notify.announced` — a dictionary of `condition → announced fingerprint`
  so each condition announces only once per distinct occurrence.

Rules:
- A condition announces when it is present now AND its fingerprint differs from
  the last-announced one. After announcing, the announced fingerprint is updated
  to the current one. When the condition clears (not present in `lastSeen`), we
  forget the announced fingerprint so a *later* recurrence announces again.
- Example: review queue currently {card A}. We announce A, record
  announced.review = {A}. Later queue = {A, B}: B is new → announce B, record
  {A, B}. Queue empties → clear announced.review. Next queue = {C} → announce C.
- This prevents "review queue has 1 item" from firing on every poll while still
  catching every genuinely new card.

### Delivery paths (both funnel through the same engine)

1. **Foreground** (`NotificationCoordinator` calls `compute` whenever a polled
   observation lands while the app is active) → `UNUserNotificationCenter`
   presents immediately.
2. **Background** (`NotificationCoordinator` called from the `BGAppRefreshTask`
   handler) → same `compute`, same notification.

The engine does not care which path called it. An APNs receiver can call the
same `compute` with the same inputs later — the seam is the engine, not the
transport.

## New files / changes

### project.yml / entitlements (required for background fetch)
- Main target `Info.plist`: add `UIBackgroundModes` = `[fetch]`.
- Main target `Info.plist`: add `BGTaskSchedulerPermittedIdentifiers` listing
  `com.hscc.ios.refresh` (the identifier we `register`).

### Sources/HSCC/Notify/ (new directory)
- `OperatorAlert.swift` — the alert value: `kind`, `title`, `body`,
  `sound`, `threadIdentifier` (so notifications group per condition), and the
  targets/conditions that produced it.
- `NeedsOperatorNotifier.swift` — the pure `compute` decision engine (above).
- `LastSeenState.swift` — Codable `ObservedState` + JSON persistence to App
  Group defaults (encode/decode, tolerant of corrupt/missing data — same
  pattern as SettingsStore:76).
- `NotificationCoordinator.swift` — `@MainActor` singleton that
  (a) builds `ObservedState` by calling the client's endpoints,
  (b) feeds `compute`,
  (c) fires resulting alerts via `UNUserNotificationCenter`,
  (d) persists `lastSeen` + `announced`.
- `BackgroundRefresh.swift` — `BGTaskScheduler` registration + the
  `BGAppRefreshTask` handler that calls `NotificationCoordinator` and
  re-schedules the next slot. `requestBackgroundRefresh()` schedules the next
  check.

### Sources/HSCC/App delegates
- `HSCCApp` gains an `@UIApplicationDelegateAdaptor` to a new
  `AppDelegate` that: requests notification authorization once,
  registers the background task id, and passes a completed background task
  through to `BackgroundRefresh`. (The simplest existing pattern: add a small
  `NotificationsAppDelegate: NSObject, UIApplicationDelegate`.)

### HSCCClient
- No new endpoints required — reuse `reviewQueue()`, `kanbanBlocked()`,
  `escalations()`, `daemonStatus()`, `health()`, `verify()`. All already exist
  (HSCCClient.swift:432,688,526,516,450,511).

### SettingsView
- New "Notifications" section with toggles (one per event class: needs-review,
  card-failed/blocked, fleet-unreachable), each backed by App Group defaults,
  plus a "Test notification" button that fires a sample alert. Every toggle is a
  real per-class opt-in so the operator controls exactly how much interruption.

### Unit tests (headless, matching repo convention)
- `NeedsOperatorNotifier.compute` table-driven tests: new-card-in-review fires;
  unchanged queue does not; queue-clearing-then-recurrence fires once each;
  escalations 0→n fires; reachable→unreachable fires once, clears, re-fires.
- `LastSeenState` encode/decode round-trip + corrupt-data tolerance.

### Headless compile check
- Because the engine is the testable seam, we can compile+run it as a macOS CLI
  (like `scripts/chat_state_check.sh`) — no device needed. Verification that the
  Swift is sound happens without an iOS runtime (NO iOS runtime on this host —
  same constraint as the liveactivity audit).

## Edge cases & honest limits (called out explicitly)

- **Unreachability is only known when observed.** If the whole tailnet link drops
  and no background slot runs, we learn nothing until the next slot (or the next
  foreground open). We can only notify on the reachable→unreachable *transition
  we actually observe*. And if the API host itself is unreachable, `apiReachable
  = false` IS the alert (that's what "fleet unreachable" looks like from a
  phone). We do not claim we'll catch an outage that happens fully between slots.
- **Force-quit kills everything.** If the operator swipes the app away, no
  background refresh and therefore no notifications, ever, until they relaunch.
  Document this in Settings; it is inherent to iOS and needs APNs (or the
  operator not force-quitting) to fix.
- **Authorization denied** → the coordinator no-ops gracefully; alerting silently
  off, never crash. Toggles read `UNUserNotificationCenter` authorization state
  so Settings tells the truth about whether notifications can even fire.
- **No cluster configured** → nothing to poll, no notifications. Gate the whole
  coordinator on `settings.isConfigured`.
- **Network hiccup on a poll** → do NOT treat a transient timeout as
  "unreachable" (that would spam). Require reachability to be a *direction*
  change confirmed across state, and cooldown: don't alert on flapping within a
  short window. The state model stores `apiReachable: Bool?` — nil means "poll
  failed inconclusively", which is distinct from a confident `false`.
- **Dedup across foreground/background** — both share the same `announced`
  store, so a condition announced in background does not re-announce in
  foreground and vice-versa.

## Implementation phases

**Phase 1 — decision engine + state + persistence (no UI, no delivery).**
`OperatorAlert`, `ObservedState`/`LastSeenState`, `NeedsOperatorNotifier.compute`,
unit tests, headless compile check. This is the APNs-ready seam and the whole
clever part. Ship first; it is independently verifiable without a device.

**Phase 2 — foreground delivery.** `NotificationCoordinator` wired to existing
poll paths (or a lightweight periodic foreground check), notification
authorization + `AppDelegate`, Settings toggles + test button. Immediate alerting
while the app is open.

**Phase 3 — background refresh.** Add `UIBackgroundModes: [fetch]` +
`BGTaskSchedulerPermittedIdentifiers`, `BackgroundRefresh` registration + handler,
re-scheduling. Best-effort asleep-phone alerting. Needs a REAL device to run even
once (nothing runs on the simulator); the host has no iOS runtime, so this phase
is compile-check-headless + device-smoke-checklist, matching
docs/DEVICE-SMOKE-CHECKLIST.md practice.

**Phase 4 (NOT built now) — APNs.** A lightweight push server (reuse HSCC's own
fleet API process as the sender) that watches the same endpoints and pushes via
APNs for instant, guaranteed, force-quit-proof delivery. The `compute` engine is
the shared seam. Out of scope for this card; documented so it can be picked up
as a child task.

## Verification plan

- `NeedsOperatorNotifier` unit tests: RED before, GREEN after (TDD).
- Headless CLI compile+run of Phase 1 (`scripts/notify_check.sh`), mirroring
  `chat_state_check.sh` / `session_activity_check.sh` — proves the Swift compiles
  and `compute` returns correct alerts against synthetic observations.
- `xcodegen generate` + build_check.sh still 4/4 clean (regenerating the project
  in the worktree proves project.yml changes are well-formed).
- Phase 3 additions compile clean headlessly; runtime verified via the device
  smoke checklist (docs/DEVICE-SMOKE-CHECKLIST.md) — NO iOS runtime on this host.

## Follow-ups (created as separate cards / honest notes)
- APNs phase (Phase 4 above) — needs a decision on hosting the push sender; not
  doable inside this card.
- A widget "attention" complication could mirror the same `AnnouncedState` store
  so even without a banner the lock screen shows "2 need review" — optional
  polish, reuses the shared state.

## Reference (codebase facts the plan relies on)
- Live Activity surfaces exist and work: `HSCCLiveActivity` (wake),
  `HSCCLiveActivitySession` (session); `LiveActivityManager.swift` cannot create
  one from background (only starts on a user tap, LiveActivityManager.swift:102).
- All four targets share App Group `group.com.hscc.ios` (SharedModels.swift:127);
  cluster config + token read by extensions via `KeychainShared`/`ExtensionClient`.
- Needed endpoints already exist on HSCCClient (no server work):
  `reviewQueue` (HSCCClient.swift:432), `kanbanBlocked` (688),
  `escalations` (526), `daemonStatus` (516), `health` (450).
- `SettingsStore.isConfigured` (SettingsStore.swift:153) gates polling.
- No existing `UNUserNotificationCenter` / `BGTaskScheduler` usage in the repo —
  this is greenfield.
