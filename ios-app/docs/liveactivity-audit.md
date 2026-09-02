# t_10bc37be — Live Activity deep audit (both targets, full lifecycle)

Branch: wt/t_10bc37be (from dev @ 64aef5f)
Date: 2026-09-02

Scope: HSCCLiveActivity (fleet wake) + HSCCLiveActivitySession.
Never run on device; NSSupportsLiveActivities was absent until this week, so
nothing here has ever executed on hardware. NO iOS runtime on this host —
findings below are labelled EXECUTED (compiled/decoded/logic run headlessly)
vs REASONING (traced statically).

## Baseline (all EXECUTED)
- xcodegen generate: ok (project regenerated in this worktree).
- build_check.sh: 4/4 targets clean — HSCC (56 files), HSCCWidgets (6),
  HSCCLiveActivity (4), HSCCLiveActivitySession (4); 0 errors, 0 warnings.
- session_activity_check.sh: all passed (pure SessionActivitySummary
  derivation: phase/headline/detail/activityCount across message/tool/card/
  agent/system/error rows + listener idle).
- capture_live.sh: captured 33 real GET routes from the live API.
- live_decode_check.sh: 33/33 decoded, 33/33 POPULATED — the models the Live
  Activity code consumes (AutodownStatusResponse, ClusterStatusResponse, …)
  decode real live API responses into Swift value types.
- check_theme.sh: CLEAN (no raw colour outside Theme.swift) — extension views
  use Theme semantic tokens only.

The two `ActivityConfiguration`s (wake + session) and both drivers are inside
those 4 clean targets, so the source registration + compile is proven.

## Q1 — Full lifecycle request→update→end. Can an activity be orphaned?

ANSWER: YES — and it was the one real bug. Fixed.

Root cause: the running `Activity` is held in `@MainActor` state owned by a VIEW
(`LiveActivityManager` = `@State` in AutodownView; `SessionActivityDriver` =
`let` in StreamingChatView). Both drivers' polling/cleanup Tasks capture
`[weak self]` (`LiveActivityManager.swift:70`, `SessionActivityDriver.swift:77,
88`) or die with the owner. Releasing/attempting to release the `Activity`
object does NOT end the ActivityKit activity — the bubble stays on the Lock
Screen/Dynamic Island, never updated, never ended. This is the same class as
the already-fixed clobber bug (c30f8d9), which fixed the *reference* handling;
this one is the activity never being *ended at all*.

Concrete trigger: operator taps "Wake Now" (AutodownView.swift:248 →
beginWaking:332 → liveActivity.beginWake:338), then navigates back / the
Autodown screen is torn down while the wake is still inflight (wake takes up
to ~9 min). SwiftUI destroys the @State; no onDisappear (AutodownView has
none — verified), no deinit. The wake bubble shows "Waking the fleet" + timer
forever.

FIX (EXECUTED — compiles clean, 0 warnings):
- LiveActivityManager: added `deinit` that ends the in-flight wake activity
  (LiveActivityManager.swift:34-49).
- SessionActivityDriver: added `deinit` that ends the in-flight session
  activity (SessionActivityDriver.swift:37-50).

Why deinit and not onDisappear: the doc comment on `beginWake` (and the view
intent "the operator can close the app and still see the wake") means we do
NOT want to end the activity merely because the view disappears (tab switch,
app background) — that would defeat the whole point of a Live Activity. The
orphan is specifically the *ownership teardown* path (view popped/destroyed),
which is exactly what deinit covers. onDisappear fires in both cases and can't
distinguish, and SwiftUI may destroy the view before end() reaches the system.

NOTE on remaining risk (REASONING, not fixed): if the app PROCESS is killed
by the OS/force-quit mid-wake, deinit does not run. ActivityKit keeps the
activity. The robust fix is re-hydration on launch — sweep
`Activity<WakingActivityAttributes>.activities` at app start and end any
leftover. I did NOT implement this: it is a larger feature (needs a launch
path that knows whether the wake is genuinely still inflight vs stale),
beyond this audit's scope. Recorded as follow-up.

## Q2 — Lock Screen AND Dynamic Island fitting / truncation

Wake (HSCCLiveActivity.swift):
- LockScreen/banner: HStack(icon + headline + Spacer + timer) + caption line
  ("Waking N of 4 units" or failure message). `.padding()`. Fits standard
  lock-screen banner. (line 95-124)
- DY compact: leading = bolt image only; trailing = HStack(stateDot +
  TextTimerView mm:ss) (line 49-57). Compact trailing is the one tight slot;
  a time string is the standard compact idiom and hscrolls none (monospaced,
  short). OK.
- DY expanded: leading stateDot, center VStack(headline + failure message),
  bottom topology pairs (line 28-48). Topology is 2 pairs x 2 dots with a
  10pt connector — compact enough. OK.
- minimal: single stateDot (line 58-60). OK.

Session (HSCCLiveActivitySession.swift):
- LockScreen: project + event count row, headline row, detail (lineLimit 2).
  (line 95-128)
- DY compact: bubble icon leading + sessionGlyph trailing; minimal: glyph
  only. OK.
- DY expanded: center VStack(headline + detail lineLimit 2), bottom event
  count. (line 36-58)

Truncation handling is present: session `detail` is capped at 80 chars in
SessionActivitySummary.textTail (line 135-139) and rendered with lineLimit(2)
in the views. Wake `message` (failure reason) has NO lineLimit — it can wrap
long (a captured `reason` was ~90 chars). On a bounded lock-screen layout this
truncates gracefully, but a very long reason would be cut. This is cosmetic,
not a defect the operator reported; I did NOT fix it (deliberate — see below).

Verdict: no fitting bug found. All surfaces have explicit structure + sensible
capping; the one unbounded string (wake failure message) wraps and truncates
rather than corrupting layout.

## Q3 — Stale-state handling when updates stop arriving

Both drivers pass `staleDate: nil` on EVERY request/update:
- LiveActivityManager.swift:59 (request), 167 (update), 175 (end).
- SessionActivityDriver.swift:77 (update), 97 (start).

So nothing ever marks an activity stale, and nothing auto-ends on silence.

EXECUTED evidence of the *intended* end-on-settle path (wake):
- fetchOutcome (LiveActivityManager.swift:111-143) maps API state → outcome:
  "up" → succeeded, isSettled; "down"/other → failed with a message,
  isSettled; "waking" → not settled. Real live capture showed state "up" with
  a populated reason — decode lives (v1_autodown_status.json POPULATED).
- upNodes parsing proven against REAL idle_hosts (standalone run): resolves
  all four .244/.246/.247/.248 labels. PASS.

REASONING on stale:
- Wake: the activity self-heals while the app lives (poll every 30s, only ends
  on a settled outcome, transient network errors keep polling). It goes
  genuinely stale only if the process dies (poll dies; deinit doesn't run) or
  network is permanently down. In those cases it shows the last state forever.
  This is inherent to Live Activities and is the same residual risk as Q1's
  process-kill note.
- Session: a "living mirror" frozen at last known state when updates stop is
  the design ("mirror simply goes away when not watched"); onDisappear ends
  it. Freezing at last state is acceptable.

DELIBERATE NON-FIX: I did not add `staleDate`. Setting it tells the system to
refresh/refresh the UI after the date, but it does NOT end an orphaned
activity, and a wrongly-set staleDate on the self-polling wake would mark a
healthily-progressing wake "stale" between polls. The deinit fix addresses the
reachable orphan; the process-kill case needs re-hydration, not a staleDate.

## Q4 — ActivityAttributes ContentState Codable-stable across app update?

EXECUTED proof (standalone Codable round-trip run):
- Both ContentState shapes round-trip Codable with all fields present,
  including nil `message` and non-nil `startedAt`/`lastActivityAt` Dates.
- App-update stability: an OLD (subset) payload decodes into a struct that
  ADDED an optional field → decodes fine, new field = nil (backward
  compatible).
- Adding a REQUIRED non-defaulted field to the struct → old payload FAILS to
  decode (the breakage mode).

The current fields are all primitives (String / [String] / Date? / Bool /
String? / Int / Date). None were recently renamed or removed. So a running
activity survives an app update as-is. Verdict: healthy, no bug.

Caveat (REASONING): the files live under Sources/Shared/ and are compiled into
BOTH the app and the extension — so app and extension always use the SAME
type, avoiding app/extension drift. This is why the stability question only
matters across app-version boundaries, where the rules above hold.

## Q5 — Second activity for a different project

Wake: NOT project-scoped (fleet-level). One wake at a time enforced by
`isRunning` guard (LiveActivityManager.swift:39) + `endCurrentSilently`
replaces any prior. Python-free REASONING: starting a second wake ends the
first (via endCurrentSilently), and the isRunning guard stops re-entry from a
double-tap. The prior clobber fix (c30f8d9) ensures the reference to the NEW
activity survives. Handled.

Session: each project's StreamingChatView owns its OWN SessionActivityDriver,
so switching projects routes to a different view → its own activity. The
driver's project-change branch (SessionActivityDriver.swift:49-53) is
defensive (can't fire within one view, since project is constant per view) but
is correct: it ends the prior activity, resets the event count, then starts
fresh. onDisappear ends the outgoing one. Net effect: the lock screen shows the
most recent project's session; a prior project's activity is ended when its
view disappears.

Minor observation (REASONING, not a bug): if navigation transitions overlap and
onDisappear is delayed, two session activities could briefly coexist; the
ActivityKit list would show both, the DY shows the newest. Self-corrects when
the outgoing view finishes disappearing. No pile-up because each view ends on
disappear + deinit now backstops it.

Event-count off-by-one (REASONING, cosmetic): SessionActivityDriver.reflect
derives the summary with the PRIOR activityCount, then updates
`activityCount = max(activityCount, rows.count)` (line 64) AFTER. So the very
first reflect after rows exist shows a 1-old count; the next reflect corrects
it. Cosmetic, self-correcting, not fixed.

## What I fixed
1. LiveActivityManager deinit — ends the in-flight wake activity on ownership
   teardown, preventing a start-never-ended orphan. (LiveActivityManager.swift:34)
2. SessionActivityDriver deinit — belt-and-suspenders end for the session
   mirror. (SessionActivityDriver.swift:37)

Both EXECUTED-proven to compile clean (4/4 targets, 0 warnings).

## What I deliberately did NOT fix (and why)
1. Re-hydration on launch (sweep Activity.activities to end process-killed
   orphans) — larger feature, needs a decision about whether a found wake is
   genuinely inflight vs stale; out of audit scope. Recorded as follow-up.
2. staleDate anywhere — does not end an orphan and would falsely mark a
   healthy self-polling wake stale between polls; the reachable orphan is
   already fixed via deinit.
3. Wake failure message lineLimit — long reason strings truncate on the
   bounded lock screen but don't corrupt layout; cosmetic, nothing the operator
   hit.
4. Session `lastActivityAt` unused in views — dead data, not a defect;
   removing it risks nothing but also gains nothing.

## Follow-ups (created as child cards)
- [ ] Re-hydration: end/restore leftover ActivityKit activities at app launch
      (from prior process kills). Assignee: ios-engineer.
