kind: Docs
task: t_dcf684c9

* `docs/API.md`'s Endpoint reference gained **20 rows** and three new read sections —
  *Daemon & host (read)*, *Autodown (read)*, *Agent surface (read)*, *Templates &
  profiles (read)* — plus `GET /v1/fleet/usage` in Fleet and six Project/kanban reads
  (`/v1/projects`, `/v1/kanban/{blocked,stale,running}`, `/v1/merge-queue`,
  `/v1/mobile/summary`). Every new row names the payload key set its handler returns, and
  the reference now names **45 of the 98** routes `api_server` registers (97 HTTP + 1 WS
  upgrade, from 24 `routes_*.py` modules). Audit F2 could stay unresolved in the docs
  precisely because nothing compared the two.
* The reference says out loud that it is curated, not exhaustive, and points at the gate
  and the reasoned register — so a reader is never left guessing whether an absent route
  means an absent feature.
* No route, no payload, no handler changed: docs and tests only.

---
kind: Verified
task: t_dcf684c9

* Measured at `1e024992` (main, with the F2 docs commit `fd15ecc6` below it): the live
  dispatch table holds **98 routes** (97 HTTP + 1 WebSocket upgrade) from **24**
  `routes_*.py` modules; the Endpoint reference named **25** of them, so **73 registered
  routes were unnamed** — and **15** of those are routes the shipped iOS client calls
  (`ios-app/Sources/HSCC/HSCCClient.swift`, parsed the way `test_contract_swift_routes.py`
  parses it). That 15 is the F2 class: a decoder that exists and a door the canonical
  reference never mentions.
* New pin: `hscc-api/tests/test_api_reference_route_coverage.py` — **9 tests** (counted
  with `pytest -q --collect-only`, not remembered). It reddens when a route is registered
  that is neither documented nor in `hscc-api/tests/route_coverage_allowlist.py`, and the
  other way round for a row naming a route nobody serves. Path cells are compared
  segment-for-segment (a documented `/v1/foo` cannot cover `/v1/foo/list` — the F2 prefix
  trap, kept as a live mutation of the parsed set), and every cell is resolved through the
  live dispatcher and compared by **handler identity**, because
  `^/v1/profiles/(?P<name>[^/]+)$` also matches `/v1/profiles/list`.
* The register is reasons, not a rename: all **53** entries carry `deferred: t_b9808146`
  (the card that owes the rows, ordered iOS-facing first) and `see <file>` — and the gate
  asserts that file exists **and** names the route, so a pointer cannot rot into a guess.
* Payload promises re-measured, not remembered: for the **18** read rows this card
  documented, the first `{...}` group in the row is compared with the key set the handler
  actually returns. `_route_shape_probe.py` does it in a child interpreter whose
  `HOME`/`HSCC_DIR`/`HERMES_HOME` are a temp sandbox set **before** `api_server` is
  imported (several route modules fold those paths into import-time constants), with every
  `subprocess` entry point and every `socket.connect` refusing — a route that reaches for
  the fleet fails the measurement instead of doing real cluster work. Two rows whose
  payload is conditional (`/v1/cron/list`, `/v1/mobile/summary`) are listed in
  `SHAPE_UNCHECKED` with the reason, so the exception is a record rather than a hole.
* Mutation probes (driver on a throwaway `git worktree --detach` of `e8a45e38`, every
  write guarded by a path assertion, originals restored): deleting the `/v1/merge-queue`
  row reddened the coverage test; inventing a key in the `/v1/template/status` row reddened
  the key-set test; deleting one register entry reddened coverage; breaking a register
  pointer's `see <file>` reddened the reason test; deleting the `/v1/profiles/list` row
  reddened the prefix-trap test. All five died; after `restore` the module was green again.
* Full `hscc-api` leg at `575ea31d` (`pytest -q tests`, one process): **1418 passed, 1
  skipped, 1 xfailed** in 460 s, rc 0 — **1420 items** = `origin/main`'s **1400**
  (collect-only, throwaway `--detach` worktree) + PR #714's 11 (this branch's base) + this
  file's 9. `scripts/readme_lint.py` → `findings: 0` (CI interpreter; the bare `python3`
  here has no `rich`); `scripts/changelog_fragments.py check` → rc 0. No route, payload or
  handler is in the diff.
