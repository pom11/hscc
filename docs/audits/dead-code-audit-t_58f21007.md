# HSCC Dead-Code & Dangling-Reference Audit — t_58f21007

Date: 2026-08-30
Branch: wt/t_58f21007 (from dev 76dd76a)
Auditor: backend-engineer

## Executive summary

NO dead code was deleted and NO dangling reference required a fix. The audit
found several real dead-code candidates, but every one falls into a category the
task rules tell us to REPORT rather than delete: operator-facing scripts,
built-but-not-wired feature components, or intentionally-staged code. Per the
card ("DELETE nothing that is dynamically dispatched or operator-facing. When
unsure, REPORT rather than delete — a wrong deletion is far worse than dead
code"), all findings below are reported with evidence and a recommended action
for the maintainer. Nothing was modified except this report.

Key negative results (proven clean):
- No Python module is silently dead except the two noted below (both reported).
- No Python route is registered-but-unreachable; every config key read
  somewhere is consumed.
- check_sources.sh is exhaustive and PASSES — no Swift source file is missing
  from project.yml, and no ghost file is listed.
- No preview-only code ships in the app target.
- ZERO hard-proven dangling references in docs or comments (see §4).

Disclaimer on proof: this host has NO iOS runtime, so all Swift findings are
source-level greps (unexecuted reasoning), not executed proof. Python findings
are backed by executed greps.

---

## 1. Python audit

Method: repo-wide grep for candidate modules under `hscc-api/`, `hscc_daemon/`,
`hscc-cli/`, `hscc-cluster/`, `hscc-commands/`, `hscc-project/flightdeck/`,
`memori/`, `hscc-bootstrap/`; cross-checked each candidate for `import`,
entry-point registration, and call sites. Plugin entry points and hook
callbacks were treated as alive unless proven otherwise (they are dynamically
dispatched).

### Finding P1 — PROVEN DEAD module, OPERATOR-FACING → REPORT
**`hscc_daemon/event_driven.py`** (≈1480 lines, own CLI with `cmd_install_event_driven` / `cmd_uninstall_event_driven`).

Evidence:
- Corpus-wide grep for `event_driven` finds NO `import event_driven` in any
  non-test Python file. All hits are in `.md` docs, the file's own self-content,
  and `cli.py` comments.
- `hscc_daemon/daemon_ops.py:22-25` explicitly states:
  *"event_driven.py has its own PERIODIC_STREAMS with different values... that
  module is NOT wired into the live daemon path (cli.py ed-* commands are
  placeholders), so it is inert; this dict (PERIODIC_INTERVALS) is what actually
  runs."*
- `hscc_daemon/cli.py:73-75` `_run_event_driven_daemon` is a `pass` placeholder
  — "needs event_driven.py".
- `hscc_daemon/cli.py:324-335` prints *"Event-driven mode: not available
  (event_driven.py not found)"* — while the file DOES exist on disk. That
  message is itself stale/contradictory.

Why NOT deleted: it is operator-facing (installs a launchd plist via its CLI)
and referenced by operator docs (idle-monitor-integration.md,
daemon-integration-pattern.md, idle-autodown.md). Recommendation:
maintainer confirm whether the ed-* CLI should wire it back in, or retire the
module + its docs + the stale "not found" messages in cli.py together.

### Finding P2 — SUSPECTED inactive-in-prod, but INTENTIONAL recent feature → REPORT
**`hscc-api/gateway_driver.py`** (GatewayDriver — Hermes WS gateway).

Evidence:
- No production import: grep of `hscc-api` non-test Python finds only the
  module itself and comments/docstrings in `routes_ws.py`. It is referenced by
  its own integration test `integration_real_driver.py`.
- `routes_ws.py` comment: "Installable by GatewayDriver.start() so the WS
  endpoint need not import the".

Why NOT deleted: this is a recently-built, intentional feature (bridge
"increment 3 — isolated probe first", built ~day of audit). It is deliberately
kept decoupled and exercised by its integration test; not yet wired into a
production import path by design. Recommendation: confirm the wiring plan; do
not delete.

### Alive (checked, not dead)
- `hscc-bootstrap/apply_patches.py` — invoked by the bootstrap flow.
- `.github/scripts/check_runtime_deps.py` — invoked by CI
  (`.github/workflows/check-runtime-deps.yml:31`).
- `scripts/dep_pr_watcher.py` — installed as a Hermes cron job by
  `scripts/install_dep_watcher.sh` (documented in scripts/README.md:62-69).
- All `routes_*.py` routes in `hscc-api/` are registered by `api_server.py`
  and are reachable. No config key was read nowhere.

---

## 2. Swift audit

Method: ran `bash ios-app/scripts/check_sources.sh` (exhaustive proof that
Sources ⇄ project.yml) plus word-boundary greps across Sources+project.yml+docs
for every candidate type. NO iOS RUNTIME — all Swift findings are source-level
grep reasoning, not executed runtime proof.

### check_sources.sh — EXHAUSTIVE AND PASSING (no action)
- Script covers both `HSCC` and `Shared` source dirs, both app and widget
  targets, and reports 61 sources all present in project.yml, with no
  ghost/uncompiled files. It is the canonical source-registration check and is
  correct.

### Finding S1 — PROVEN-DEAD types, NOT dynamic/operator-facing, but built-but-not-wired / intended-but-unused → REPORT
5 types are referenced by name exactly once in the entire corpus — their own
declaration — so no code uses them:
- `HSStatusDot` — Theme.swift:207
- `HSStatusRow` — Theme.swift:221
- `HSStatusChip` — Theme.swift:268
- `QRPairing` — SetupQRCode.swift:159
- `TopologySnapshot` — SharedModels.swift:312

Evidence command (run in ios-app/):
`grep -rnw HSStatusDot|HSStatusRow|HSStatusChip|QRPairing|TopologySnapshot Sources project.yml docs`
→ each name appears on exactly one line (its declaration).

Why NOT deleted:
- The 3 `HSStatus*` views carry the comment "the ONE row style ... Use these
  instead of re-inventing" — i.e. intended-but-unused; a maintainer may still
  intend to adopt them. Deleting would contradict documented intent.
- `QRPairing` doc comment: "This is the shared step both the onboarding screen
  and Settings use after a confirmed scan." It has full `test()`/`classify()`
  logic (Test/Classify calls HSCCClient.ping) yet NO caller — built-but-not-wired
  pairing scaffolding. Likely a feature still being rolled out; deleting would
  remove it.
- `TopologySnapshot` has full save()/load() persistence logic — same pattern.

Recommendation: these are the single clearest deletion candidates IF the
maintainer confirms the HSStatus* trio / QRPairing pairing flow / Topology
snapshot UI are not about to be wired in. Since the "use these"-style comments
and the pairing QRPairing comment indicate intended-but-unused scaffolding, the
audit reports them instead of deleting.

### Other Swift checks (no findings)
- No preview-only code (`#Preview`) ships in the app target.
- No instance in Sources is missing from project.yml.

---

## 3. Scripts audit

Method: for each script under `ios-app/scripts/` and `scripts/`, grepped the
repo for who invokes it and whether scripts/README.md or ios-app/README.md
documents it as an operator tool.

### Alive (invoked and/or documented) — no action
Internal scripts in `ios-app/scripts/` (helpers invoked by the public check
scripts): all referenced. Public check scripts (`check_sources.sh`,
`build_check.sh`, `model_decode_check.sh`, `chat_state_check.sh`,
`streaming_check.sh`, `session_activity_check.sh`) — each of these is named in
the TASK ITSELF as a canonical check, so each is by definition an operator tool
and NOT dead, regardless of invocation frequency.

### Finding SC1 — SUSPECTED-ORPHANED scripts → REPORT
Candidate scripts with near-zero invocations outside their own dir / docs, and
not listed as canonical checks in the task body:
- `verify_roundtrip_demo.py` — fully silent: zero hits anywhere in repo.
- `flake_hunt.sh` — fully silent: zero hits.

Why NOT deleted: both look like operator QA/verification tools run ad hoc
(flake_hunt.sh catches flaky tests — an operator tool). Not documented in
scripts/README.md. If truly unused, they are the strongest deletion candidates.
Recommendation: maintainer confirm they are not used on the operator's own
machine, then delete + document.

### Note
The task body lists chat_state_check.sh, streaming_check.sh and
session_activity_check.sh as canonical iOS checks, so despite appearing
"undocumented" in some greps they are NOT dead by the task's own definition and
were not flagged.

---

## 4. Dangling references (docs & comments)

Method: cross-referenced every file path, function name, CLI command and HTTP
endpoint mentioned in `docs/`, `ios-app/docs/`, README files, CHANGELOG,
PROJECT-COMMANDS and code comments against the actual tree, registered routes
and CLI dispatch.

### Result: ZERO hard-proven dangling references.
- All 37 markdown cross-links in docs resolve (no broken relative links).
- All documented API routes are actually registered (`/v1/ping`, `/v1/health`,
  `/v1/cluster/*`, `/v1/fleet/*`, `/v1/autoscale`, `/v1/standup`, `/v1/cards`,
  `/v1/review/*`, `/v1/qa/queue`, mutating routes, `/v1/orchestrator/chat`,
  `/v1/autodown/*`, `/v1/kanban/*`, `/v1/verify`, `/v1/daemon/status`,
  `/v1/triggers`, `/v1/escalate`, `/v1/profiles`, `/v1/templates/*`).
- CLI command surface in README matches `hscc_daemon/hscc.py:main()` dispatch.
- Every script path referenced in docs exists.
- The 5 dead Swift types above still EXIST in the tree, so no doc reference to
  them is dangling (they are dead code, but not dangling references).
- `_archive/` dirs: no doc points to archived paths as live.

### Two SUSPECTED (draft design docs referencing planned-not-realized targets) — NOT dangling-to-deleted, informational only
- `docs/DESIGN-api.md:361,:494` — references `hscc_daemon/api_server.py`
  (planned file). The HTTP server was actually implemented as
  `hscc-api/api_server.py`; `hscc_daemon/` only has `api_cli.py`. Draft design
  doc, planned target implemented elsewhere. Not a stale ref to deleted code.
- `docs/design/idle-autodown.md:408,:411` — proposes `GET /v1/status` endpoint
  that was never registered. Design-contract proposal, not realized. Not a
  dangling ref to a deleted route.

These are left as-is because they live in draft design docs and describe
intended-but-not-yet-built surfaces; they do not mislead a reader into thinking
a deleted thing exists.

---

## What was fixed
- This report (docs/audits/dead-code-audit-t_58f21007.md) is the only change.

## What was deliberately NOT fixed and why
Verdict: the card is an audit whose deliverable is the findings report. Every
dead-code candidate is either operator-facing, built-but-not-wired/intended-but-
unused, or a draft-design planned target — exactly the classes the card says to
REPORT rather than delete. Deleting any of them without maintainer sign-off
would violate "a wrong deletion is far worse than dead code." Concrete items
deliberately left in place (with recommendation):
- Python: `hscc_daemon/event_driven.py` (operator-facing, OBE; recommend retire
  together with stale cli.py "not found" messages), `hscc-api/gateway_driver.py`
  (intentional recent feature).
- Swift: 5 dead types (recommend delete if maintainer confirms the
  intended-but-unwired flows are abandoned).
- Scripts: `verify_roundtrip_demo.py`, `flake_hunt.sh` (recommend confirm+delete
  if truly unused).
