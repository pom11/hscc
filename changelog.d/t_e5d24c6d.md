# t_e5d24c6d — the drawer's connection pill no longer says "checking…" forever

Fixed: with the API up, the drawer's server pill (and Home's hero) sat on
"checking…" / "unknown" indefinitely. `ConnectionMonitor.status` changed only
via `requestSucceeded()`/`requestFailed()` from completed `HSCCClient`
requests — and the drawer makes no request of its own, so nothing ever
resolved `.unknown`. (The old launch ping had been deleted for a different
reason — a one-shot cold-start FAILURE pinned the banner — which never
licensed an eternal `.unknown`.)

The monitor keeps its passive contract and gains the missing active half:
while the drawer header is visible and the app is configured, a `.task`
keyed on `connectionIdentity` runs one cheap authed `GET /v1/health`
immediately, then re-checks every 15 s (30 s back-off when the server
refuses the credential), and stops with zero traffic when the drawer closes
(task cancellation on disappear). The first real result — probe or request —
flips `.unknown`; the dot then tracks the most recent outcome while visible.
A failed probe never pins the banner: the loop keeps observing and any real
success clears it (the t_4889e978 contract). `!isConfigured` fires nothing —
first-run still reads "not configured". Auth refusal (401/403/
`unauthorized`) is surfaced honestly as unreachable with a token-specific
reason ("check it in Settings"), not a Tailscale guess; any other HTTP
response still proves reachability (any-response contract). Full state
machine documented on `ConnectionMonitor`; probe logic is a pure, injected-
call `ConnectionProbe` enum so it is fully gate-covered.

New hermetic gate `scripts/connection_probe_check.sh`: compiles
`ConnectionMonitor.swift` verbatim + the committed live `/v1/health` capture,
23 assertions (endpoint pinned to /v1/health, decode contract, classification
table, first-probe flip, no-pin after both failure kinds, cluster-switch
reset), no socket opened; `CONNECTION_PROBE_NEGATIVE_TEST=1` proves it can
fail. `connection_banner_check.sh` unchanged in logic (APIError.swift joined
its verbatim compile for the probe's typed catch).
