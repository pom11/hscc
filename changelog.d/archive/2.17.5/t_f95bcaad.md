kind: Docs
task: t_f95bcaad

* `docs/API.md`'s Endpoint reference had **two rows for one route** once `28da0da4` (PR #708)
  landed on `main` on top of the `#740 → #741 → #743` documentation stack: `GET
  /v1/template/list` and `GET /v1/template/status` were each named by the stack's
  `### Templates & profiles (read)` section *and* by #708's new `### Templates (read)`
  section. The gate's `_rows()` keeps the **last** row it parses for a path, so the
  hand-written one won and `test_documented_key_sets_are_the_key_sets_the_handlers_return`
  reddened: the row promised `{applied, speak}`, `handle_template_status` returns
  `{applied, note, speak}`. One route, one row now — the four template reads sit in the
  section the stack already gated and #708's section is gone, its prose folded up.
* `GET /v1/template/preview/{name}` was named by the reference **and** by the register, whose
  reason ("named outside the Endpoint reference only") #708 made false on `main`. The register
  entry is deleted in the same commit that keeps the row (register **38 → 37**).
* The card this fragment is named for predicted the gate would redden on
  `GET /v1/main/status`, `GET /v1/orch/waker/status` and `GET /v1/template/validate/{name}`
  being undocumented. **Measured false:** #708 and #709 each shipped their own rows *inside*
  `## Endpoint reference`, so `test_every_registered_route_is_documented_or_in_the_register`
  never reddened on them. What was true is one level down — those rows' `{...}` promises were
  in neither `SHAPE_CHECKED` nor `SHAPE_UNCHECKED`, so nobody had ever compared them to the
  handlers. One of the four was wrong.
* `GET /v1/main/status` promised 11 keys and `handle_main_status` answers **12**: the row had
  silently dropped `ok`. It now promises 12 and carries a `SHAPE_CHECKED` pin.
* `GET /v1/main/status` also now says what the two degraded answers are (`{ok, state, detail,
  speak}` with `state: "error"` when the identity resolver cannot be read, `hscc_daemon/
  main_cli.py:440`; `{speak}` alone when the backing raises at all), so the 12-key pin is a
  stated contract with a named exception rather than an implied one.
* `ok` is **not** a fourth `state`, which is a claim this card wrote and the source refuted
  before it shipped: pane-*discovery* failure (`main_cli.py:449`) answers `ok: false` with
  `state: "unknown"`, while a pane found whose *screen* cannot be read (`main_cli.py:475`)
  answers `ok: true` with the same `state: "unknown"`. The row tells a client to read `state`.
* Two new `SHAPE_CHECKED` pins and one recorded refusal: `GET /v1/template/validate/{name}`
  → `{ok, placement, speak, structural, template}`, `GET /v1/orch/waker/status` → 7 keys
  (its row was already correct and gained only the pin), and `GET /v1/template/preview/{name}`
  → `SHAPE_UNCHECKED`, because `preview_template()` begins with `_resolve(name, probe=True)` —
  a **live-fleet** resolution the NO-LIVE tripwire cannot cross, and an unknown name `404`s
  before it. Measured, not assumed: `ApiError: no template named 'demo'`.

---
kind: Verified
task: t_f95bcaad

Every count below was measured at the `sha` named beside it, by re-importing the gate's own
module-level parser over the live tree and by running `_route_shape_probe.py` as a script —
never subtracted from a parent card's numbers.

* Reproduction, at `22ee8c15` (= `origin/main` a79990a7 merged with `wt/t_d47d1562`, PR #743's
  head; clean merge, 0 behind main): gate **2 failed, 7 passed**. `…do_not_both_name_a_route`
  → `['GET /v1/template/preview/{name}']`; `…key_sets_are_the_key_sets_the_handlers_return` →
  `GET /v1/template/status: row promises ['applied','speak'], handler
  routes_template.handle_template_status returns ['applied','note','speak']`. **Neither
  failure names a route this card named.** That commit is red on purpose: it is the tree the
  gate runs on once #740 merges. Green from `cfcd7a1c` onward.
* `cfcd7a1c` (template dedupe + register deletion) — gate **9 passed**.
* `7f0152cd` (the D9 dashboard pair measured) — probe at this sha, no seam stub on either
  route: `GET /v1/main/status` → 12 keys `[board, busy, detail, name, ok, pane, panes,
  profile, running, speak, state, target]`; `GET /v1/orch/waker/status` → 7 keys
  `[audit, boards, config, enabled, paneless_skip, speak, stream]`; `GET
  /v1/template/validate/{name}` → 5 keys `[ok, placement, speak, structural, template]`;
  `GET /v1/template/preview/{name}` → `ApiError: no template named 'demo'` (no dict).
* At `7f0152cd`: **101** registered routes (100 HTTP + 1 WS — three more than the 98 the
  stack's gate was written against), reference names **64**, register holds **37** (all
  `surface`; the `ios` class stayed empty), `SHAPE_CHECKED` **24**, `SHAPE_UNCHECKED` **5**.
  `64 + 37 = 101` with uncovered **0**, reference/register overlap **[]** and ghost rows **[]**.
* Gate module `9 passed` at `cfcd7a1c` and at `7f0152cd`. `scripts/readme_lint.py` →
  **findings: 0** (files: 23, docs-index 227/227) at `7f0152cd`.
  `scripts/changelog_fragments.py check` rc **0** at the commit that adds this fragment.
* Full `hscc-api` leg at the tip of this branch (`e4faacb9`'s tree, hermes venv, Python 3.14.10,
  `pytest -q tests` from `hscc-api/`): **1461 passed, 1 skipped, 1 xfailed in 476.35s**, rc **0**
  — zero failures. The SIGINT version-pin test
  (`tests/test_api_server_stop_signal.py::test_the_event_shape_is_the_hazard[2]`) that failed in
  an earlier card's full-leg run did not reproduce here; it is reported as measured, not waved
  away.
* Hermetic: nolive throughout — the probe runs in a child interpreter whose `HOME`,
  `HSCC_DIR`, `HSCC_HOME` and `HERMES_HOME` are a `tempfile.mkdtemp` sandbox set before
  `api_server` imports, with every `subprocess` entry point and every `socket.connect`
  raising. No route, payload or handler changed, and no mutating handler was invoked: every
  row added here is a `GET` and every claim about a mutating route is read from source.
