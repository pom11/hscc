kind: Added
task: t_2bed5fe3

- **`ios-app/Sources/Shared/WidgetDataClient.swift` — the one read-only HTTP
  client every extension surface reads through.** Compiled into the app target
  (where the Live Activity drivers live) and `HSCCWidgets`; the three Live
  Activity extension targets render `ContentState` and never fetch, so they do
  not carry it. Read-only is structural, not a convention: exactly one HTTP
  method (`GET`) can be built — no body parameter, no method parameter — and only
  the nine paths of `WidgetDataPath.all` (the routes `WIDGET_DATA_CONTRACT` §3's
  five datasets are served by) are fetchable, anything else fails with
  `.pathNotAllowed` before a URL is constructed, so a later card cannot reach a
  mutating route by inventing a string. It writes nothing: no App Group value, no
  response blob (§7 persists the projection, and that is `SharedStore.swift`'s
  job). Connection settings and auth are reused unchanged — host/port from
  `APIConfig.load()` (the App Group suite `SettingsStore` writes), bearer token
  from the shared keychain per client, and the same `Authorization` +
  `Accept: application/json` headers over plain `http://` as `HSCCClient`, with
  the token never rendered, persisted or carried in a failure. Two things the
  nil-returning GET it replaces could not express: `timeout` — a per-request
  ceiling (default 8 s, both `timeoutIntervalForRequest` and
  `timeoutIntervalForResource`, cache and connectivity-wait off) instead of
  URLSession's 60 s default, because an extension that overruns its own budget is
  killed silently; and `WidgetDataFailure` — the kinds a surface must distinguish
  (`notConfigured` / `invalidURL` / `pathNotAllowed` / `timeout` / `unreachable` /
  `unauthorized` / `notFound` / `http` / `decoding`), none of which carries server
  prose, an address or the token. `showsStaleData` is the stale-while-error rule
  in one place: an unconfigured app and a bad path never fall back to a snapshot
  (the t_5c554c5b rule, now structural rather than re-invented per widget), and
  every other failure keeps the last-known value with its age.
  `WidgetDataOutcome` + `WidgetData.read(using:lastKnown:_:)` give a timeline
  provider `.live` / `.stale(value, ageMinutes:, reason:)` / `.failed(failure)` to
  switch on, so "fall back to the snapshot" is one age formula instead of five
  hand-rolled ones — and a 2xx that fails to decode lands in `.decoding`, which
  is what keeps §6 rule 1 (a missing `done` must not render as `0`) honest end to
  end. `scripts/widget_data_client_check.sh` proves all of it by executing the
  committed type through a stubbed `URLProtocol` (no network), and owns the exact
  audited path set as an assertion, so adding a read is a deliberate act in two
  files rather than a silent widening.
---
kind: Removed
task: t_2bed5fe3

- **`ios-app/Sources/Shared/ExtensionClient.swift`.** The pre-pivot extension GET
  helper: GET-only by construction already, but with a hardcoded 15 s timeout and
  `nil` on any failure, which cannot tell "not configured" from "cluster down"
  from "answered and unreadable" — so `ClusterWidget` and `MonitorWidget` each
  re-implemented the fallback by hand. It is deleted rather than kept beside
  `WidgetDataClient`, because two clients for one surface is exactly how a widget
  and a Live Activity end up disagreeing on a timeout. A
  `typealias ExtensionClient = WidgetDataClient` plus the nil-returning
  `get(_:as:queryItems:)` shim keeps both providers compiling unchanged while the
  widget cards move to the typed reads; the alias is marked `RETIRE` with the
  slice that takes it.
