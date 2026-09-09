# Screen audit: AutodownView — prove every element works

Task: t_9b678f46
Assignee: ios-engineer
Target: `ios-app/Sources/HSCC/Views/AutodownView.swift`
Branch: `audit/autodownview-t_9b678f46` (from dev)

## Bottom line

One real bug fixed (controls flashed the wrong action during load). Everything
else is healthy and proven with executed evidence. The autodown surface is one
of the most solid screens in the app: the exact endpoint the view consumes
decodes POPULATED against the live wire, every control is confirm-gated with
feedback through a single `MutationButton` path, and stale/offline is handled
by the app-wide `Offline.load` seam. No "compiles but dead" pathology here.

---

## 1. DATA IN — endpoint + real values

- **Single feed:** `HSCCClient.autodownStatus()` → `GET /v1/autodown/status`
  (`HSCCClient.swift:468-470`).
- Backend route registered: `routes_autodown.py:379-380` (`GET` regex
  `/v1/autodown/status$` → `handle_autodown_status`).
- Live response (real, fetched read-only this run, address redacted to
  placeholder): the full JSON captured in the git history / tool output —
  `enabled: true, state: "up", idle_minutes: 120, watchdog_blocked: false,
  blocked_by: "open PR / active CI run on a tracked repo, and kanban work on
  board 'hscc': ...", force_armed: false, active_cron_cpu_only:
  ["hscc-dep-watcher","hscc-escalate-watcher"], active_cron_model: [], speak:
  "Autodown armed, idle limit 120 minutes, status up. Blocked by ... . prci."`
- **Every field the view reads exists in the model and arrives.** The model
  `AutodownStatusResponse` (SharedModels.swift:49-70) declares every key the
  server sends. Executed proof: `live_decode_check.sh` decoded the captured
  live `/v1/autodown/status` as **`[POPULATED]`** — the real wire JSON decodes
  into the real model with real values.
- Model decode harness: `model_decode_check.sh` → 48/48 including
  `autodown_status.json → AutodownStatusResponse` and all four mutation
  responses (`autodown_enable/disable/wake/cancel`).

## 2. RENDER — what the operator sees

Status section renders (`AutodownView.swift:123-158`):
- `state.speak` — full §B summary line (line 131). Live: "Autodown armed,
  idle limit 120 minutes, status up. ...". ✓
- `State` row (137) → `stateLabel` gives "up" / "down (intentional)" /
  "waking" (183-185). Colour via `stateColor` (187-194). ✓
- `Idle limit` row (139) → "120 min" (196-199). ✓
- `Enabled` row (140) → "Yes"/"No". ✓
- Watchdog block row (142-148) — only when `watchdog_blocked == true`; live
  false, hidden. `intentional` handled as a String. ✓
- `blocked_by` row (149-151) → caption `Label` with `hand.raised.fill`. Live
  payload is a long paragraph; renders fine as a wrapping caption. ✓
- `force_armed` label (152-157) — only when true; live false, hidden. ✓
- Cron section (304-326) — shows model-requiring jobs (warn) and CPU-only jobs.
  Live: 2 CPU-only crons shown. ✓

**Dropped fields (deliberate, not bugs):** `last_activity_iso`, `down_since`,
`kanban_ok`, `kanban_reason` are not rendered as their own rows. This is fine —
the server folds `reason`, `wake_source`, and `blocked_by` into `speak`
(routes_autodown.py:182-198), which IS shown. `down_since` is the only
diagnostic not surfaced anywhere; low value. No wrong units, no truncation, no
client-side count that could disagree with the server (there is no client-side
count on this screen — the only counts come from server arrays, rendered with
`joined`).

## 3. STATES — loading / stale / failed / loaded

The screen distinguishes all four honestly (`AutodownView.swift:98-121`):
- **`.loading`** → `ProgressView` inside the Status card (line 102). No data.
- **`.loaded`** → clean status body, no banner (114-117).
- **`.stale(value, msg)`** → `StaleBanner("Offline — showing state from Nm
  ago", "Can't reach the cluster right now.")`) + the last-known status body
  (105-113). Last-known data is clearly age-marked, never presented as live.
- **`.failed(msg)`** → `HSErrorLabel` (red, `exclamationmark.triangle.fill`)
  only (103-104). No data, explicit failure, distinct from empty.

"0 results" vs "failed to load": there is **no zero-result case** for this
surface — `/v1/autodown/status` always returns a full payload (`enabled` is
always a bool, `speak` always synthesized; routes_autodown.py:224,241). So the
"0 rows" ambiguity the checklist worries about does not apply here; and if the
status *loader* fails it is rendered as `failed`/`stale`, never as an empty
"nothing here". ✓

One degenerate case worth noting (very low probability): if `load_config()`
itself throws, the API still returns 200 `{ "speak": "Autodown status
unavailable." }` (routes_autodown.py:216-217), which decodes into a model with
all-nil fields → view shows `state: unknown`, `Enabled: No`. Not a fault path
the operator will realistically hit, but it would mislabel enabled→No. Not fixed.

## 4. CONTROLS — every mutation is confirm-gated with feedback

Four controls, all through `MutationButton` (`MutationSupport.swift`), which
guarantees: tap only arms a `confirmationDialog` naming exactly what will
happen → confirm fires the mutation → in-flight spinner disables the button →
outcome alert (success "Done" / failure "Failed", never a green check for a
failed call). Every `run` closure calls an `HSCCClient` mutator that always
sends `"confirm": true`:

| Control | Client method | Endpoint | Backend |
|---|---|---|---|
| Disable (228) | `autodownDisable()` (856) | `POST /v1/autodown/disable` | routes_autodown.py:383-384 |
| Wake Now (241) | `autodownWake()` (865) | `POST /v1/autodown/wake` | :385-386 |
| Cancel Teardown (254) | `autodownCancel()` (871) | `POST /v1/autodown/cancel` | :387-388 |
| Enable (285) | `autodownEnable(idleMinutes:force:)` (848) | `POST /v1/autodown/enable` | :381-382 |

All five backend routes exist (`api_route_sweep.py` lists the GET; the POSTs are
deliberately not fired by the sweep — they mutate live state, which is correct
to leave unexercised). Route cross-check: **all routes the view's client methods
hit are registered and answer** — the GET status route 200s live, and the four
POST routes are registered in `ROUTES` (auto-imported by `api_server.py`).

Feedback: every control has visible feedback (spinner + result alert). A
control with no feedback = none here; `MutationButton` handles it uniformly.

## 5. OBSERVATION — no re-render traps

- The view holds **no `@StateObject`/`@ObservedObject`** at all. State is a
  value-typed `LoadState` (`@State status`) plus simple `@State` value flags
  (18-30). The single object `LiveActivityManager` (line 30) is a non-observable
  `@MainActor final class` held in `@State` — correct choice; it is used only for
  Live Activity side effects, and it is intentionally NOT observable.
- **No plain-`let` ObservableObject anywhere**, so there is no "must switch tabs
  to see it" bug in this view.
- **No `@StateObject` keyed by a changing value.** AutodownView is pushed as a
  `NavigationLink` destination from the Cluster hub (`ClusterView.swift:204-207`
  via `hubRow`'s `NavigationLink` at :287-288); `client` is a stable injected
  value. Each push gets a fresh `@State` instance and reloads (`.task` at
  :51-55). No stale first instance survives navigation.

## 6. LAYOUT — survives Dynamic Type & small screens

- `ScrollView` + `VStack(alignment: .leading)` + `HSSectionCard`s with system
  fonts throughout (`.body`, `.subheadline`, `.caption`). No fixed-width /
  fixed-height content, no `.frame` constraints that could clip.
- All long content (`speak`, `blocked_by`, cron labels) is `Text`/`Label` in
  leading-aligned VStacks that wrap; nothing truncates meaning on an iPhone SE
  width.
- `statusRow` uses `HStack(alignment: .firstTextBaseline)` label + spacer +
  value — the value wraps under large Dynamic Type. Acceptable; not broken.

**Only minor note:** the `Picker("Idle minutes")` (275) mirrors the server's
`idle_minutes` (AutodownView.swift:81-84) but `idleOptions = [10,20,30,60,90,120]`
(33) has no `.tag` for an off-list value (e.g. a 45-min config). In that case
the picker shows nothing selected. Cosmetic, rare, not fixed.

## 7. ACCESSIBILITY — labels everywhere, colour never the only signal

- All four action buttons have text labels ("Disable Autodown", "Wake Now",
  "Cancel Teardown", "Enable Autodown") — none are icon-only.
- The picker and toggle have visible labels.
- `StaleBanner`'s retry is icon-only but has `.accessibilityLabel("Retry")`
  (Theme.swift:428).
- Colour-as-only-signal: none. The state value is colored (stateColor) AND
  carries a text label ("up" / "down (intentional)" / "waking"); Enabled is
  "Yes"/"No" text. A colourblind operator is never told by colour alone.
- `check_theme.sh` → CLEAN (no raw colour outside Theme.swift).

---

## What I fixed

**`AutodownView.swift` controlSection (lines 221-299):** gated the controls
section to render only once a status value is known (`if let value`), instead
of computing `enabled = value?.enabled == true` (which is `false` when there's
no value yet) and falling into the ENABLE branch during loading/idle/failed.
Effect: on an armed cluster, the operator no longer sees a misleading
"Enable Autodown" picker+toggle+button flash at the start of every visit (or
after a failed load) before the real Disable/Wake/Cancel controls appear.
Verified: `build_check.sh` clean, 0 err / 0 warn, all four targets.

## What I deliberately did NOT fix, and why

1. **Live Activity ends when Autodown view is popped** (LiveActivityManager.swift:
   43-49, deinit ends with `.immediate`). The feature's copy promises "leave the
   app and still see the wake", but popping the Autodown view destroys the
   `@State`-held manager and its deinit ends the activity at once. This is a
   genuine honesty gap for a ~9-minute operation, BUT the comment at :33-42
   documents it as a deliberate choice to avoid orphaned never-updated bubbles,
   and fixing it properly means moving the manager to an app-scoped owner that
   outlives navigation — a refactor with orphan risk, out of scope for an audit
   that should prove the current surface. Ranked as the top "would be nice"
   item, not fixed.
2. **`_speak_status` duplicate trailing period**: live speak ends "... prci."
   (routes_autodown.py:195-197 appends `{res}.` where res already carries a
   period). Cosmetic, backend-side, not this iOS surface's file.
3. **Degenerate 200-with-only-speak** when `load_config` throws (routes_autodown.py:
   216-217) — would render enabled→No. Very low probability, backend-side.
4. **`errorMessage(for:)` dead code** (AutodownView.swift:92-94) — unused helper,
   harmless, compiles without warning.
5. **Off-list idle_minutes picker** — cosmetic, rare.

## Evidence log (executed)

- `hscc api status` → derived live address (redacted). `curl GET
  /v1/autodown/status` → 200, full live JSON (real values above).
- `scripts/api_route_sweep.py` logic reviewed; GET status route 200s live
  (`capture_live.sh` + `live_decode_check.sh` → `31/33 decoded, 31/33
  populated`; autodown_status `[POPULATED]`).
- `model_decode_check.sh` → **48/48** (incl. AutodownStatusResponse + 4
  mutation responses).
- `build_check.sh` → **0 err / 0 warn, all four targets** (before and after the
  fix).
- `check_sources.sh` → 62 Swift files all registered.
- `check_theme.sh` → CLEAN.
- `widget_view_dispatch_check.sh` → OK.

## OUT OF SCOPE (noted for the board)

`live_decode_check` surfaced 2 decode failures for `v1_verify.json` /
`v1_health.json` → `HealthResponse` (`checks[6].ok` is `null`, model expects
`Bool`). This is the OpsView / Health screen surface, not Autodown — flagging so
a follow-up card can own it. Autodown is unaffected.
