kind: Docs
task: t_bcebb042

- **The API contract now names its own confirm exemptions.** Push registration
  landed with PR #443 as two mutating POSTs — `POST /v1/devices` and
  `POST /v1/devices/test` — that neither client call site marks with
  `"confirm": true` and whose handlers never call `_require_confirm`, and the
  confirm gates had no idea they existed. They are outside the confirm contract
  deliberately: a device registering or refreshing **its own** APNs token sits
  behind the server's single bearer gate, writes no cluster, workload or board
  state, and runs on every app launch with no operator decision in front of it,
  so a `confirm` flag there could only be a hardcoded `true` that nothing checks.
  `hscc-api/docs/audit-client-server-params.md` and the `POST /v1/devices` section
  of `WIDGET_ROUTES.md` now state that, and
  `hscc-api/tests/test_contract_swift_routes.py` holds the exemption as a named,
  per-route `CONFIRM_EXEMPT_POSTS` list that both confirm gates read — so the
  client half and the server half can never disagree. Adding to it is audited by
  `test_confirm_exemption_is_closed_and_justified`, which fails if an entry names
  a route the app no longer calls, carries no reason, resolves outside
  `routes_devices.py`, or points at a handler that *does* check `confirm` (that
  last case is the one neither gate can see on its own: the call would 409 on the
  phone while both gates stayed green).
- **`POST /v1/devices` joined the required-body-field matrix** with its four
  required fields (`platform`, `token`, `bundle_id`, `env`) and a citation that
  still resolves, so the audit no longer under-advertises what registering
  actually demands, and a client that drops one of them now fails the suite
  instead of silently stopping registration.
- **`GET /v1/alerts` is now in the audited extension read set.** The alert-feed
  poll shipped with PR #446 and its row in `WIDGET_ROUTES.md`, but the audited set
  the widget route gate compares against never learned about it, so the doc
  advertised a read the contract did not cover. The set's comment also stops
  implying every entry is a widget dataset — this one is an app-only read that
  lives in the shared GET-only client file, which is why the scanner sees it.
