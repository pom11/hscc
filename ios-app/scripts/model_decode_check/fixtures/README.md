# Decode fixtures

Live HSCC API responses captured 2026-08-27, **sanitized** for a public repo.

These feed `scripts/model_decode_check.sh`, which compiles the REAL model
sources (`Sources/HSCC/Models.swift`, `Sources/Shared/SharedModels.swift`,
`Sources/HSCC/APIError.swift`) into a macOS CLI and decodes every fixture. Any
decode failure exits nonzero and names the fixture + field.

## Provenance & sanitisation

Each file is a verbatim capture of a real `/v1/...` read response, with values
rewritten to strip internal detail. Sanitisation is **value-only** — it never
changes JSON shape (same keys, same value types, same nesting), because shape
is the entire point of the check.

Mapping applied:
- node IPs  `192.168.88.244/.246/.247/.248/.249`  →  `10.0.0.1/.2/.3/.4/.5`
- tailnet IP `100.115.243.3`                       →  `100.64.0.1`
- home dir  `/Users/desac`                          →  `/home/devuser`
- ssh username `spark`                              →  `node`
- hostnames `gx10-worker-N` / `gx10-gateway`        →  `node-N` / `gateway-1`
- token path reference `~/.hscc/api-token`          →  `~/.hscc/sample-token-path`

## Note on the grep

Running `grep -r token fixtures` WILL match in `fleet_throughput.json`, but only
inside the legitimate schema key names `prompt_tokens` and `generation_tokens`
— part of the model contract for the throughput endpoint, not credential
values. No actual token/Bearer/api_key **value** is present anywhere in these
fixtures (verified: zero email addresses, zero key material, zero auth
headers).

`cards.json` and `card_detail_t_049d6986.json` are shape-faithful rewrites of
the real captures: the Card model reads only `id/title/status/assignee/board`
(unknown fields are ignored by Decodable), so the bulky card `body` prose —
which contained the tailnet IP and a token path — was dropped and the real key
structure preserved. The decode contract is therefore exercised identically.

## Coverage

26 fixtures cover every decodable response model in Models.swift +
SharedModels.swift that has a live capture on this host (verified 26/26 OK).
Missing from the set (no captured fixture exists):
- mutation POST responses (`DispatchCardResponse`, `MergeCardResponse`,
  `TemplateApplyResponse`, `StopClusterResponse`, `AutodownEnable/Disable/
  Wake/Cancel`, `RecoverCardResponse`, `ClusterUpResponse`,
  `ClusterDownResponse`) — not read endpoints, no captures taken
- `ReviewDetailResponse` (`GET /v1/review/{id}`) — no capture taken
- `OrchestratorChatJobResponse` / `OrchestratorChatJobStatus` / `ChatJobError`
- `ReadResponse` — self-contained generic bucket, needs no fixture
