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

Mapping applied (value → replacement):
- private node IPs  →  `10.0.0.1` / `10.0.0.2` / … (one distinct fake IP per real node)
- private tailnet IP → `100.64.0.1`
- operator home dir  →  `/home/devuser`
- ssh username       →  `node`
- node hostnames     →  `node-N` / `gateway-1`
- token-file path reference → `~/.hscc/sample-token-path`

## Note on grepping the fixtures

A naive `grep -r token fixtures` in the JSON data matches only the legitimate
schema key names `prompt_tokens` and `generation_tokens` in
`fleet_throughput.json` — those are part of the model contract, not credential
values. No actual authorization value, API key, or private key material is
present anywhere in the JSON data (verified: the only sensitive-token hits in
the whole fixtures tree are this README naming the patterns). The real
authorization header value never appears in any capture.

`cards.json` and `card_detail_t_049d6986.json` are shape-faithful rewrites of
the real captures: the Card model reads only `id/title/status/assignee/board`
(unknown fields are ignored by Decodable), so the bulky card `body` prose —
which contained a tailnet IP and a token-file path — was dropped and the real
key structure preserved. The decode contract is therefore exercised identically.
All 26 files autovalidate as JSON (verified). If ever in doubt, re-encode a
fresh capture and diff the key structure.

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

## Running the check

From `ios-app/`:

    ./scripts/model_decode_check.sh       # exit 0 == every fixture decodes

The script compiles the three real model files with the harness into a temporary
macOS CLI and runs it, so it never falls out of sync with the models the app
actually ships.
