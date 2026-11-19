kind: Fixed
task: t_facdaa48

- **`hscc-api/tests/test_session_resolver.py` had no coverage left for the
  legacy `general-orch` catch-all, and its project-less test still carried the
  pre-#432 name.** The card opened against a real red at main's tip: the file
  asserted `profile == "general-orch"` for `resolve_named_session_id(None)` /
  `("general")`, while PR #432 (`f1e2477e`, "roles: one named main orchestrator")
  had made no project / `general` resolve to the ONE stored main orchestrator.
  PR #444 (`c7010aea`) re-pointed that assertion at the resolver while this card
  was in flight, so the red is gone — but the rewrite left two gaps this card
  closes: `general-orch` disappeared from the file without a companion case, so
  #432's "the legacy catch-all still resolves when explicitly named" had no
  holder on the API side, and the test was still named
  `test_resolve_general_sentinel_is_honest` for a contract it no longer stated.
  Renamed to `test_resolve_projectless_identity_is_the_stored_main_orchestrator`,
  its docstring now names #432 as the PR that amended the expectation, and the
  expectation is derived from `main_orch` (the identity's owner) rather than
  restated as a literal — so a rename cannot rot this pin again. The resolver and
  `main_orch` were not touched: #432's merged contract is the authority.
---
kind: Verified
task: t_facdaa48

* `test_resolve_explicit_legacy_general_orch_still_resolves_catch_all` (new): an
  explicitly named `general-orch` still resolves to the legacy catch-all
  (`("general-orch", "general", None)`) and `profile != main_orch.main_profile()`,
  so the retired path cannot be quietly folded onto the main orchestrator. The
  wire-name literals are deliberate — deriving them from
  `main_orch.LEGACY_GENERAL_PROFILE` would keep passing if that constant were
  renamed while existing installs keep calling `general-orch`. The roles leg's
  `test_main_orch.py::test_resolver_keeps_the_explicit_legacy_catch_all_working`
  pins the resolver dict; nothing pinned this leg's 3-tuple.
* `test_resolve_projectless_identity_follows_a_stored_main_name` (new): stores
  `ops` via `main_orch.store_name` and pins `None` / `general` / `ops` all
  resolve to `("ops", "ops", None)` — the case that keeps the derived expectation
  above from passing vacuously on the default. A resolver that stopped consulting
  the stored document fails here while the default case stays green.
* All three cases share a `sealed_hscc_dir` fixture, so the answer no longer
  depends on whether bootstrap had stored a name on the running host (the card's
  reproduction had to seal `HOME` to get a stable `1 failed`).
* Mutation-probed at the commit that adds them: restoring the pre-#432
  `_legacy_general_identity()` return for the project-less case reddens both
  project-less tests; making `general-orch` unresolvable reddens only the new
  legacy test; making `main_orch` ignore the stored document reddens only the
  stored-name test.
* `hscc-api/tests/test_session_resolver.py`: 10 -> **12** cases, green.
* Whole `hscc-api` leg at `07d45c85` (base `origin/main` `1b431c36`): **3 failed,
  1223 passed, 1 skipped**. None of the 3 is this diff. At this card's own base
  `704f1cb8` the identical leg is **2 failed / 1222 passed / 1 skipped** without
  the diff and **2 failed / 1224 passed / 1 skipped** with it — the diff moves
  zero tests red and adds two green. The third red is `1b431c36`-only and
  reproduces with nothing applied (measured over `test_contract_swift_routes.py`
  + `test_session_resolver.py` in a clean detached worktree: the same
  **3 failed / 28 passed**). All three are carded off this one:
  `t_8d153e99` (`POST /v1/devices`, `POST /v1/devices/test` ungated mutations,
  from #436) and `t_cef2a294` (`GET /v1/alerts` missing from the audited
  extension-route roster, from #446).
* Both doc gates the card named are green here: `test_contract_swift_routes.py
  -k audit_doc` -> 2 passed (the citation half went green at main when #440
  landed, and stays green over this diff).
