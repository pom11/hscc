# Fix HealthResponse.decode failure: checks[].ok null vs Bool

Task: t_fd2be5be
Assignee: ios-engineer
Target: `ios-app/Sources/HSCC/Models.swift` (`HealthCheck.ok`)
Branch: `audit/health-fix-t_fd2be5be` (from dev), worktree under the task workspace

## Bottom line — ALREADY FIXED, verified live

The fix this card asks for was already shipped in `dev` by commit `c0cecb1`
("fix(health t_6060f92b): HealthCheck.ok is a documented Tri-state (Bool?)"),
which landed during the prior TemplateDetailView audit (t_6060f92b). This card
(t_fd2be5be) was created from the t_9b678f46 report snapshot, which predated
that commit.

I verified the already-shipped fix against a **FRESH live capture** (not the
stale committed fixtures) and it fully resolves the reported failure:

```
LIVE DECODE: 33/33 decoded, 33/33 populated
ALL 33 LIVE ROUTES DECODE AND CARRY REAL DATA
```

The exact failing case from the report — `checks[].ok = null` — is present in
the current live `/v1/health` and `/v1/verify` payloads (the `api_routes` check
is `ok: null`, emitted as `null` by the server), and both now decode
[POPULATED]. No further code change was needed.

## The fix in place (already shipped)

1. **Model** — `HealthCheck.ok` is `Bool?` (Models.swift:73), matching the
   server's documented tri-state contract (`bool | None`: true = pass, false =
   hard fail, nil = could not be verified). An Optional field tolerates
   `ok: null` instead of throwing, so a single unverified check no longer
   drops `checks` / fails the whole response.
2. **Renderer** — `Views/HealthCheckIndicator.swift` maps the tri-state to
   distinct icons/tints: `true` → `checkmark.circle.fill`+ok, `false` →
   `xmark.circle.fill`+bad, `nil` → `questionmark.circle.fill`+neutral. A
   null (unverified) check is never conflated with a red hard-fail, so the
   operator isn't trained to ignore reds.
3. **Consumers** — both check-list surfaces (`OpsView.swift:140-141`,
   `FleetView.swift:132-133`) call `HealthCheckIndicator.icon/tint(check.ok)`.

## Verified (executed)

- `scripts/capture_live.sh` → 33 fresh live GET routes captured (read-only),
  `scripts/live_captures/20260903_065001/`. Manually confirmed
  v1_health.json and v1_verify.json both carry `checks` incl. `api_routes` →
  `"ok": null` (9 checks each; the exact failing field).
- `scripts/live_decode_check.sh <fresh capture>` → **33/33 decoded, 33/33
  populated** (v1_health [POPULATED], v1_verify [POPULATED]).
- `scripts/model_decode_check.sh` → **49/49** fixtures.
- `scripts/build_check.sh` → **0 errors / 0 warnings**, all four targets
  (HSCC, HSCCWidgets, HSCCLiveActivity, HSCCLiveActivitySession).
- `scripts/check_sources.sh` → sources in sync.

## Scope note

`HealthCheck.ok` already being Optional means no NEW Swift edit was required
in this run. The only change produced here is this verification report. The
underlying fix commit remains `c0cecb1`.
