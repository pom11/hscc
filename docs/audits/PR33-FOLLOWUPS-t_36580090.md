# PR #33 review follow-ups — decisions (t_36580090)

Date: 2026-10-16. Lane: `cli-help-2026-10-15` (PR #33), base tip `43aa08ad`.
Operator-approved as low-pri follow-ups to the PR #33 review (comment 1189 on
t_7b0f65c8). NOTE: the kanban card body was stored truncated (orchestrator-side
context compression at creation); these decisions are recorded against the
card's title + verifiable lane state. If the cut body text diverges, reopen.

## Item 1 — `hscc project project <verb>` reads badly: DECISION = ALIAS (keep), polish the surface

The doubled form exists because flightdeck's own `project` subgroup (lifecycle
verbs: new/list/remove/repair/pull/push/chat/sync/sessions/digest/link/unlink)
sits under `hscc project` — so flightdeck's `flightdeck project chat` became
`hscc project project chat` at the 2.x port-in.

**Decision: keep the inner `project` subgroup as-is (no rename, no verb
promotion into flightdeck's namespace) and make the promoted alias the
documented single form.** Rationale:

1. Renaming/promoting inside flightdeck breaks flightdeck's own surface
   (`flightdeck project new` is the standalone contract; `docs/COMMANDS.md`,
   MCP tools, and 1234-project test suite key on it) for a cosmetic win on the
   hscc side only.
2. The hscc-side alias already shipped in 1.16.0 (CHANGELOG: "Flightdeck's
   project-subgroup verbs are aliased to the top of `hscc project`") with a
   runtime guard that queries the parser, so a future top-level command
   shadows the alias rather than the reverse. It is safe and tested
   (hscc_daemon/tests/test_hscc_cli.py).
3. A rename is a behaviour change; this lane is help/text-only by contract.

What was actually wrong and is fixed here:

* **The alias table was stale.** `_PROJECT_SUBGROUP_VERBS`
  (hscc_daemon/hscc.py:993) listed only 8 of the 12 subgroup verbs —
  `sessions`, `digest`, `link`, `unlink` were added later and were NOT
  aliased, so those four really did read `hscc project project <verb>`.
  Fixed: the table is pinned to flightdeck's real subgroup by a test.
* **Every epilog still taught the doubled form** (`example: hscc project
  project list` etc. — 16 sites in project.py/sync.py, plus hscc-project/
  README.md). Since the alias works, the short form is the canonical
  user-facing spelling; epilogs/README now use `hscc project <verb>`.
  The doubled form keeps working (parse + behaviour unchanged) — it is just
  no longer advertised.
* **The gate's parse oracle rejected the short form**: `ExampleParser.
  _parse_project` only knows flightdeck's real parser, where `chat` alone is
  not a top-level command. F1 called the doubled form "canonical" for
  exactly this reason. Fixed: the oracle now models the hscc alias the same
  way `_handle_project` implements it (set membership + top-level-wins
  guard), so short-form examples parse and the oracle stays a faithful
  model of the shipped dispatcher.

## Item 2 — CLI_HELP_FINDINGS.json: DECISION = STOP COMMITTING, regenerate on demand

The artifact (docs/audits/artifacts/CLI_HELP_FINDINGS.json, ~2.4k lines) is
the raw `audit()` report — a pure function of the tree. Committing it means
every fix card that prunes findings silently makes it stale, and the lane has
already moved it stale (326 findings at audit time → the rules file is down to
56 after F1/BROKEN-* landed). It is not read by the gate; `test_write_findings_artifact`
only re-emits it on demand.

**Decision: the committed snapshot is removed and gitignored; the gate and the
audit CLI regenerate it on demand.** Recovery paths, both zero-config:

* `HSCC_CLI_HELP_WRITE_ARTIFACT=1 pytest scripts/tests/test_cli_help_surface.py -k artifact`
* `python scripts/hscc_cli_help_audit.py --json docs/audits/artifacts/CLI_HELP_FINDINGS.json`

The findings DOC (docs/audits/CLI_HELP_2026-10-15.md, human-curated narrative)
stays committed — it is the authored deliverable, not generated output. The
doc now says how to regenerate the JSON.

## Gates run

Full canonical suite (`env -u CONDA_* bash scripts/run_tests.sh`) ALL GREEN
(11 legs, SUITE_EXIT=0) against the shipped commit: project leg 1411 passed,
daemon leg 1546 passed (incl. the 31 tests of `test_hscc_cli.py`), scripts
leg 125 passed/1 skipped (incl. the help-surface gate). Gate specifics:
0 NEW / 0 STALE findings, 75 examples parse-checked with 0 failures (all
rewritten short-form examples included), artifact regeneration proven via
`HSCC_CLI_HELP_WRITE_ARTIFACT=1` (writes the gitignored path; 109 findings
on the current tree vs the 326 committed snapshot it replaces — evidence the
snapshot had rotted). `scripts/address_guard.py --repo .` rc=0; diff
self-audit for addresses clean. Real-CLI smoke (sandbox HOME, read-only):
`hscc project link --help` → `usage: hscc project link …`, doubled
`hscc project project digest --help` works and its usage line no longer
doubles either.
