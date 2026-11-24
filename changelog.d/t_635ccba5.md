kind: Fixed
task: t_635ccba5

* `GET /v1/projects/new/plan` answered a plan for the **wrong project** on every real
  request. `api_server._parse_path` runs `parse_qs` and collapses each key to `v[-1]`,
  and `_dispatch` merges matched path groups into that same dict, so a handler is handed
  **one scalar string per query key** — but `routes_bootstrap.handle_plan` read them as
  lists (`query.get("name", [""])[0]`), which on a scalar is the FIRST CHARACTER.
  Measured over real loopback HTTP against the shipped dispatcher (sandboxed
  `HOME`/`HSCC_DIR`/`HERMES_HOME`, subprocess entry points tripwired, no fleet contact,
  no real `~/.hscc`): `?name=demo&repo=/tmp/demo` reached `_backing_plan` as
  `('d', '/')`, and the answer was `ready: true` with
  `would_create: {profile: "d-orch", session: "d", board: "d"}` — the wizard's review
  screen green-lit minting a profile, a session and a board nobody asked for. The case
  that is wrong rather than merely different: `?name=demo%2Fx` — an invalid name whose
  first character `d` IS valid — answered `ready: true`, for a project the confirm-gated
  `POST /v1/projects/new` would then have refused.
* The defect hid from the suite in TWO places, and both were closed. The route tests
  drive the handler over real HTTP, so they did see the scalar shape — but
  `_make_plan` returned a hardcoded `acme`/`/tmp/acme` whatever the handler handed it,
  so no payload assertion could show a truncated read; it now echoes its arguments and
  asks the **production** `_validate_name` whether a name is valid instead of keeping a
  private heuristic that could disagree with the handler's. The new cases assert on the
  backing's own recorded arguments, the only place the truncation was visible: with the
  old two lines restored, three of them are red on `('a', '/')` / `('d', '')`; with the
  fix `tests/test_routes_bootstrap.py` is **20 passed**.
* Kept on purpose, because both were already promised and neither is this defect: a bad
  or already-registered name is a **blocker answered with `200`**, never a `400` (the
  wizard must show *why* it cannot proceed), and a plan backing that fails outright
  degrades to `{speak}` alone — no `ready` key, so a caller cannot read a missing key as
  a go-ahead. Blank is absent, as pinned for `/v1/cluster/history` in
  `test_cluster_history_parity.py`: `parse_qs` drops blank values before any handler sees
  them, so `?repo=` is the key being absent, not an empty string to index.

---

kind: Docs
task: t_635ccba5

* `docs/API.md`'s Endpoint reference gained the row for `GET /v1/projects/new/plan` in
  *Project / kanban (read)* — until now the handler's own docstring was the only prose
  anywhere that named this door, which is why it sat in the register
  (`("GET", "/v1/projects/new/plan")`, `deferred: t_b9808146`). The register entry was
  deleted in the same commit that added the row, as the gate demands. The reference now
  names **49 of the 98** registered routes (97 HTTP + 1 WebSocket upgrade), measured with
  the gate's own parser over this tree at `c03c456d`; it named 48 at the parent's tip
  `fd976709`. The register went **50 → 49** (12 ios / 37 surface), uncovered 0.
* The row carries the key set its handler actually returns — `blockers`, `collisions`,
  `name`, `ready`, `repo`, `speak`, `steps`, `validations`, `would_create` — measured, not
  copied from the return literal, and it says which answers are degradations rather than
  empties: an unreadable registry/board/profile reports **no collision** instead of
  guessing, and a failing backing answers `{speak}` alone.
* `hscc-api/tests/_route_shape_probe.py` gained `SHAPE_CHECKED["GET /v1/projects/new/plan"]`
  (18 → 21 → **22** across the two parent cards and this one), with the query given as the
  SCALARS `_parse_path` hands over. That pin was impossible before the fix: the only
  stub that would have measured was one shaped like `parse_qs` OUTPUT
  (`{"name": ["demo"]}`), i.e. a request the server never sends — which is exactly the
  blindness that kept the defect green. No seam stub: this route's key set IS
  `_backing_plan`'s own return literal, and each of its four collision helpers answers
  `False` from its own `except` in a sandbox, so the keys measured are the handler's.

---

kind: Verified
task: t_635ccba5

* The payload pin bites: dropping `would_create` from the new row reddens
  `test_documented_key_sets_are_the_key_sets_the_handlers_return` (measured, then
  reverted), so the row cannot quietly drift from the handler again.
* `hscc-api` route-coverage gate + the bootstrap route module together:
  **29 passed** at `c03c456d` (gate 9, bootstrap module 20 — the module grew 15 → 20).
* Full `hscc-api` leg (`pytest -q tests`, one process, hermes venv interpreter 3.14.10):
  **1423 passed, 1 skipped, 1 xfailed in 464.03 s**, rc 0 — **zero failures**, so the SIGINT
  version-pin case that failed once on the parent's run did not reproduce here. It ran
  against the tree of `c03c456d` (the fragment wording that followed touches no code the
  leg reads). 1418 → 1423 passed is exactly this card's 5 new plan cases.
* `scripts/readme_lint.py` → `findings: 0` (hermes venv interpreter; the bare `python3`
  here has no `rich`). `scripts/changelog_fragments.py check` → rc 0.
* Hermeticity: no fleet contact and no real `~/.hscc` anywhere in this card. The
  reproduction ran against a loopback server bound to `127.0.0.1:0` under a fresh
  `tempfile` sandbox exported as `HOME`/`HSCC_DIR`/`HERMES_HOME` before `api_server`
  imported, with every `subprocess` entry point and `os.system`/`os.popen` refusing to
  spawn; the gate's payload measurement runs in its own sandboxed child interpreter with
  the same tripwires.
