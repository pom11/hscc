# OpsView Screen Audit — t_ca19400d

Operations / health surface (`ios-app/Sources/HSCC/Views/OpsView.swift`).
Full screen audit: DATA IN / RENDER / STATES / CONTROLS / OBSERVATION / LAYOUT / ACCESSIBILITY.

Status: IN PROGRESS (draft). Evidence-backed. Live API read-only proven.

---

## 1. DATA IN — endpoints + live values

| Section | Endpoint | Method | Live status |
|---------|----------|--------|-------------|
| Verify | `/v1/verify` | GET | 200, ok:true, 9 checks |
| Daemon | `/v1/daemon/status` | GET | 200, daemon_running:true, pid:50078, 12 streams |
| Triggers | `/v1/triggers` | GET | 200, 4 rules, last_run, 20 recent_events |
| Escalations | `/v1/escalate` | GET | 200, count:2, 2 escalations |
| Profiles | `/v1/profiles` | GET | 200, counts:{}, total_running:0 |

Server routes confirmed in `hscc-api/routes_ops.py:394-402` (both the read GETs
and the mutating POSTs `/v1/triggers/run` and `/v1/escalate`).

POSTs:
- `/v1/triggers/run` (POST) -> routes_ops.py:397, handler 288, confirm-gated. Returns TriggersResponse shape.
- `/v1/escalate` (POST) -> routes_ops.py:399, handler 311, confirm-gated. Returns {escalations, count, performed, speak}.

### Model decode contract (all decode-verified)
- VerifyResponse = HealthResponse (Models.swift:689) {ok, checks, speak}; HealthCheck {name, ok(Bool?), detail}. Models.swift:71-84.
- DaemonStatusResponse (Models.swift:695) {daemon_running, pid, state, streams:[String:StreamStatus], speak}.
- TriggersResponse (Models.swift:729) {rules, last_run:StreamStatus?, recent_events:[String]?, speak}.
- EscalationsResponse (Models.swift:741) {escalations:[JSONValue]?, count, speak}.
- ProfilesResponse (Models.swift:751) {counts:[String:Int]?, total_running, profiles:[JSONValue]?, speak}.

### EVERY field the view needs arrives ✓
- Verify: speak, ok, checks[].name/ok/detail all present. ✓ (incl. tri-state ok:null on `api_routes` check — rendered distinctly)
- Daemon: speak, daemon_running, pid, streams[] ok/message present. ✓
- Triggers: speak, rules[].id/trigger_params.title/condition.{metric,op,value}, last_run.message present. ✓
- Escalations: speak, escalations present. ✓
- Profiles: speak, counts present. ✓

Server route registration: routes_ops.py:394-400.

---

## 2. RENDER — what the operator sees vs. what the data has

Dropped / not-rendered fields (all decoded into the model but never shown):

| Field | Source | OpsView | Impact | Severity |
|-------|--------|---------|--------|----------|
| `daemon.state` | DaemonStatusResponse.state | not rendered (only daemon_running bool) | operator sees run bool but not the state string | low |
| `stream.timestamp` (per-stream age) | StreamStatus.timestamp | not rendered | can't tell how stale each stream is | low-med |
| `triggers.recent_events` (20 events) | TriggersResponse.recent_events | not rendered (line ~225) | "recent firings" is the headline of the server doc but never shown | low-med |
| `triggers.last_run.timestamp` (age of last run) | StreamStatus.timestamp | only `last_run.message` shown (line 220) | operator can't tell how stale the trigger eval is | med |
| `escalations.count` | EscalationsResponse.count | not rendered (line 292) | count dropped; list shown but no total | low |
| `profiles.total_running` | ProfilesResponse.total_running | not rendered (line 338) | per-profile counts shown, no total | low |

Count-disagreement check: client never renders a client-computed total count, so
there's no client count that can disagree with the server's. `counts` in /v1/profiles
is the server's own dict, rendered as-is. No disagreement possible.

---

## 3. STATES

Each section = its own LoadState; one degraded endpoint never blanks the rest.

| Section | Loading | Empty (200 w/ 0 rows) | Error | Stale/offline |
|---------|---------|----------------------|-------|---------------|
| Verify | ProgressView | "No checks reported." | errorLabel (red) | StaleBanner + body ✓ (uses Offline.load) |
| Daemon | ProgressView | (no explicit empty) — streams empty renders only speak+pid | errorLabel | NONE (no Offline.load) ✗ |
| Triggers | ProgressView | "No trigger rules configured." | errorLabel | NONE ✗ |
| Escalations | ProgressView | (no explicit empty) — empty list renders only speak | errorLabel | NONE ✗ |
| Profiles | ProgressView | "No profiles running tasks." | errorLabel | NONE ✗ |

KEY: "0 results" vs "failed to load" — NEVER look the same. Verified distinct:
- Empty: HSEmptyLabel (muted, tray icon, .onSurfaceMuted) — neutral.
- Failed: HSErrorLabel (red, exclamationmark.triangle.fill, .bad) + real message.
- Loading: ProgressView spinner.
`HSConnectGate` shown when client is nil.

ONLY verify gets offline/stale treatment. daemon/triggers/escalations/profiles
all cache via `get(_:)` (HSCCClient.swift:223 `StateCache.store` on success) but
the view never reads the cache back for those four — so on an unreachable
cluster they show a plain red error, not last-known data. Inconsistent with the
verify section on the same screen. (Design gap, not a crash.)

(Continuing…)
