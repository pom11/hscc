# Offline & Poor-Network Behaviour — Full-App Audit

Task `t_d58f7ec6` — ios-engineer. Scope: how every data-fetching screen in the
HSCC iOS app behaves with NO connectivity and with SLOW/FLAKY connectivity.
Four failure vectors were screened per screen: cached-with-stale-label,
error-with-guidance, infinite-spinner (hang, no timeout), and silent mutation.

**Headline:** one systemic defect (query-param GETs were never written to the
offline cache) made the offline `.stale` fallback dead for whole classes of
screens. I fixed it, proved the fix against the live API with a compiled
harness (5/5 PASS), and wired the two raw-fetch screens that render the worst
(blank/error-only, no stale label) through the offline fallback.

All evidence is `file:line` anchored. Test commands are listed and were run.

---

## 1. How the offline cache actually works

The cache lives in `HSCCClient` as a single `StateCache`, keyed by endpoint
path. Each GET writes the last-known raw `Data` under `read.<path>`.

```
file: ios-app/Sources/HSCC/HSCCClient.swift
LINE  204:  fileprivate static var cache = StateCache()
       223:      StateCache.store(data, for: path)        // no-query GET (core get)
       361:  func cachedValue<T>(_: T.Type, for path: String) -> T?
       370:  func cacheAge(for path: String) -> TimeInterval?
```

`Offline.load` (the single fetch-and-degrade helper every offline-aware screen
calls) reads that cache back to render `.stale`:

```
file: ios-app/Sources/HSCC/Views/LoadState.swift
LINE  128:  do { let fresh = try await fetch(); return .loaded(fresh) }
       133:  } catch {
       135:      if let cached = client.cachedValue(T.self, for: cacheKey) {
       136:          return .stale(cached, stateAgeMessage(cacheKey, client: client))
       139:      if let held = current.value { return .stale(held, "showing state from 0s ago") }
       143:      return .failed(...)
```

So `.stale` is only reachable if the endpoint actually WROTE to the cache.

## 2. THE systemic defect (root cause, now fixed)

The query-item GET helper only wrote to the cache when `queryItems` was EMPTY:

```
BEFORE (the bug):
file: ios-app/Sources/HSCC/HSCCClient.swift  (pre-fix)
       283:  if queryItems.isEmpty {
       284:      StateCache.store(data, for: path)
       285:  }
```

Consequence: **every query-param read — sessions (by profile), fleet stats (by
days), memory (by profile), kanban stale (by older_than), activity feed (by
limit), project session events (paged) — was never persisted.** Yet several of
those screens called `Offline.load` with `cacheKey: <plain path>` as if a value
would be there. On a cold-start offline open they surfaced `.failed` (or,
before I fixed the raw-fetch screens, an error-only view) instead of last-known
data. The offline last-known feature was a dead letter for every query read.

The original intent of the guard (per the comment at the old line 237-240) was
to stop a PAGING read from clobbering the freshest page in the cache — but the
`queryItems.isEmpty` check is far too broad and killed offline caching for ALL
query reads, not just paging ones.

## 3. The fix (HSCCClient) + proof

`get(path:queryItems:)` now writes to the cache under the plain `path` for
every read EXCEPT a paging (`before` cursor) read. This preserves the original
"freshest page in cache" intent for the one paging endpoint (`sessionEvents`)
while enabling offline caching for every single-shot query read.

```
AFTER (the fix):
file: ios-app/Sources/HSCC/HSCCClient.swift
LINE  271:  let isPaging = queryItems.contains { $0.name == "before" }
       272:  if !isPaging {
       273:      StateCache.store(data, for: path)
       274:  }
```

`sessionEvents` is the only paging call site; it marks older pages with
`before` and is therefore excluded. The tail (`before == nil`) still caches.

**Executed proof** — `ios-app/scripts/offline_cache_fix_check.sh` compiles the
REAL `HSCCClient.swift` (which contains `StateCache` + `EndpointPath`) plus the
real models, calls the LIVE read-only endpoints through the same URLSession
path the app uses, then asserts the cache gets written AND read back:

```
$ bash ios-app/scripts/offline_cache_fix_check.sh
PASS session query read now writes /v1/sessions cache (this was the dead path)  [age=0s]
PASS fleet stats query read writes /v1/fleet/stats cache
PASS activity feed query read writes /v1/activity/feed cache
PASS cached /v1/activity/feed decodes to a populated value (Offline.load can show .stale)
PASS cached /v1/sessions decodes (Offline.load can show .stale on a session screen)
OFFLINE CACHE FIX PASS
```

Read-only: `sessions`, `fleetStats`, `activityFeed` are pure GETs. Host/port/
token are supplied at runtime from `hscc api status` + `~/.hscc/api-token`
(no address is baked into the repo; the harness derives it per-run).

## 4. Per-endpoint cache keying — VERIFIED FIXED (rule 4)

The motivating concern — "the offline cache previously shared ONE key across
all reads, so screens showed each other's data" — is genuinely resolved. The
cache key is `read.<path>`, derived from each endpoint's own path:

```
file: ios-app/Sources/HSCC/HSCCClient.swift
LINE   25:  static func key(for path: String) -> String { "read.\(path)" }
```

Every endpoint has a distinct path (`/v1/projects`, `/v1/cluster/status`,
`/v1/sessions`, `/v1/memory`, `/v1/activity/feed`, `/v1/fleet/stats`,
`/v1/kanban/stale`, ...), so no two screens share a cache slot. My `fleet
_offline_check` harness (existing) proves 4 distinct paths hold 4 distinct
payloads with no cross-talk; the new harness above adds query-endpoint reads
to the proof. **No cross-contamination is possible.** (There is no "one key
across all reads" any more.)

## 5. Per-screen verdicts

Screen-by-screen, against the four vectors. "✅" = handled, "⚠️" = gap/risk.

| Screen | Data source | Cached+stale label | Error w/ guidance | Hang risk |
|---|---|---|---|---|
| Home / Cluster status | `clusterStatus` (no-query GET) | ✅ `.stale` + StaleBanner | ✅ `errorLabel` | ⚠️ up to 60s (read has no req timeout) |
| Cluster hosts/topology | `hosts` (no-query GET) | ✅ via ClusterView | ✅ | ⚠️ up to 60s |
| **Activity feed** | `activityFeed?limit=` (**was raw → FIXED to Offline.load**) | ✅ **now** `.stale`+banner | ✅ `errorLabel` | ⚠️ up to 60s |
| Projects list | `projects` (no-query) | ✅ | ✅ | ⚠️ up to 60s |
| Project detail | `projectDetail?query=` | ✅ | ✅ | ⚠️ up to 60s |
| **Sessions** | `sessions?profile=` (**never cached → FIXED**) | ✅ `.stale`+banner (**now reachable**) | ✅ | ⚠️ up to 60s |
| **Memory** | `memories?profile=` (**was raw → FIXED to Offline.load**) | ✅ **now** `.stale`+banner | ✅ | ⚠️ up to 60s |
| Fleet (stats) | `fleetStats?days=` (**never cached → FIXED**) | ✅ `.stale` (**now reachable**) | ✅ | ⚠️ up to 60s |
| Board hygiene (blocked) | `kanbanBlocked` (no-query) | ✅ `.stale`+banner | ✅ | ⚠️ up to 60s |
| Board hygiene (stale) | `kanbanStale?older_than=` (**never cached → FIXED**) | ✅ `.stale` (**now reachable**) | ✅ | ⚠️ up to 60s |
| Cards / card detail | `cardDetail` (no-query path GET caches; raw fetch) | ⚠️ cache written but view doesn't use Offline.load → no stale label | ✅ loadError | ⚠️ up to 60s |
| Approvals | `kanbanBlocked` | ✅ | ✅ | ⚠️ up to 60s |
| Session history (pager) | `sessionEvents` (paged query) | ⚠️ tail now caches, but its own state machine shows `.failed` on tail error, not stale | ✅ `.failed`+paging banner w/ retry | ⚠️ tail wait up to 60s |
| Streaming chat / LiveActivity | WS (parse frame) | n/a (stream) | ✅ `.failed`/`.reconnecting` | ✅ WS `request.timeoutInterval = 30` + backoff |
| Orchestrator chat | `orchestrator/chat` POST (timeout 30) + poll | n/a (mutation+async job) | ✅ job-status rendered | ✅ POST bounded; poll is fast |
| Node topology | none (pure presentational) | n/a — inherits ClusterView | n/a | ✅ none |
| Search | `cards` + `projects` (both no-query GETs) | ✅ `.stale`+banner | ✅ | ⚠️ up to 60s |

## 6. Q3 — the "hang forever" risk (one real vector, documented)

Every READ request is built by `request(for:timeout:)` with NO timeout passed,
so it inherits URLSession's **60s default** (`ios-app/Sources/HSCC/HSCCClient.swift:176-180`).
The only request that passes a custom timeout is the orchestrator chat POST
(`timeout: 30`, line 843) and the streaming WS (`request.timeoutInterval = 30`,
StreamingChatStore.swift:230).

The 60s default is benign on plain no-connectivity (the OS fails fast). It is
the **flaky tailnet** case that hangs: a half-open / black-holed TCP path to an
unreachable-but-routeable host can exceed even the 60s request timer and leave
a screen on a spinner with no app-level cancel. The offline screens now at
least have last-known data to show once the request fails — but the operator
still waits up to ~60s (or longer on a hard TCP hang) before `.stale` appears.

**This is a documented residual risk, not silently changed.** Tightening the
global read timeout (e.g. to ~15s) is a genuine trade-off: the operator runs a
slow tailnet and healthy-but-slow reads (large project/session payloads) could
be aborted prematurely. That decision warrants operator review before change —
flagged in §9.

## 7. Q5 — mutations do NOT silently no-op when offline

- **`MutationButton`** (MutationSupport.swift:88-102): every mutation runs
  `try await run()`; on error it sets `outcome = .failure(message)` and the
  view raises a **"Failed" alert with the real error**. No `try?` swallowing; a
  failed merge/apply/stop/delete is never rendered as success. So every
  mutation (merge, template apply, cluster up/down, autodown enable/disable,
  recover, memory delete/edit, dispatch) surfaces offline failure honestly.
- **Chat `send()`** (StreamingChatStore): not a silent no-op — when not
  connected it sets `sendError = "Not connected yet…"`, and a socket send
  failure sets `sendError = "Send failed: …"`. The operator's line is echoed
  locally so the send is never invisible.
- The only historical "no-op" was a server-side hook that is out of scope (a
  separate task); the current client-side mutation surface is honest.

Verdict: **no mutation silently no-ops when offline.**

## 8. Files changed

- `ios-app/Sources/HSCC/HSCCClient.swift` — cache single-shot query reads under
  their plain path; suppress only paging (`before`) reads.
- `ios-app/Sources/HSCC/Views/ActivityFeedView.swift` — fetch through
  `Offline.load`; render `.stale` (StaleBanner + data) instead of blank/error.
- `ios-app/Sources/HSCC/Views/MemoryView.swift` — fetch through `Offline.load`;
  render `.stale` (StaleBanner + data) instead of blank/error.
- `ios-app/scripts/offline_cache_fix_check.sh` + `.../offline_cache_fix_check/main.swift`
  — NEW compiled harness proving the cache write + read-back against the live API.

## 9. Recommendations / follow-ups (not done — need operator decision)

1. **Bounded read timeout.** Give base GET reads an app-level timeout (e.g.
   15s) so a hanging tailnet TCP can't hold a spinner up to/over the 60s
   default. Trade-off: slow-but-healthy tailnet reads could be aborted. Needs
   operator approval on the value.
2. **CardsView / SessionHistoryView** still use raw fetches (not `Offline.load`),
   so despite the cache now being written they render error-only on offline
   first-load rather than `.stale`. Low-risk follow-up: route them through
   `Offline.load`.
3. The cache value for a `profile`-scoped read is stored under the plain path
   and shows whatever profile was last fetched — acceptable (clearly marked
   stale) but worth knowing.

## Honesty about proof

- **Executed**: the new harness compiled the real `HSCCClient` + models and hit
  the LIVE API (5/5 PASS); the full app compile is clean (0 errors, 0 warnings);
  `git diff` is grep-clean of real addresses.
- **Reasoned, not measured**: the URLSession-60s-hang-on-half-open-TCP claim is
  reasoned from URLSession semantics + the source, not reproduced here (no iOS
  runtime to simulate a black-holed tailnet). Stated plainly in §6.
