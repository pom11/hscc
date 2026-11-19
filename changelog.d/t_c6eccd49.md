kind: Added
task: t_c6eccd49

- **The main orchestrator now has ONE name, asked once at bootstrap and stored
  under `~/.hscc`.** `hscc-bootstrap`'s stage-3 interview asks
  `Main orchestrator name [general]:` only when no name is stored yet; `--yes`
  takes the stored name or `general`, and `--dry-run` prints the planned
  `~/.hscc/main-orchestrator.json` write without performing it. Re-running
  bootstrap never changes a stored name — the writer refuses an overwrite even
  if a future caller forgets the check. Names are validated as lowercase
  `[a-z0-9-]` slugs, and rejected when they are a role profile from
  `hscc-roles/roles/*.yaml`, a registry project (whose `<project>-orch` already
  owns the identity), an existing `<name>-orch` profile on disk, or end in
  `-orch` themselves. The default `general` is exempt from the
  `<name>-orch` / project collision checks: every pre-migration install still
  has `general-orch` on disk (retired, never deleted).
- **`hscc-roles/main_orch.py` is the single resolver** — `main_name()`,
  `main_profile()` and `main_board()` read the stored name and fall back to
  `general` when nothing is stored. `orchestrators.resolve_orchestrator()` now
  returns that identity for no project/`general` (profile, session and board
  are all the chosen name), while the legacy `general-orch` catch-all still
  resolves when explicitly named. `GENERAL_PROFILE`/`GENERAL_BOARD` are gone;
  `orch-all`, `provision-check`, the generator's cluster-authority recognition
  and the API's role-profile sweep all read the resolver instead. The legacy
  `orchestrator` role profile remains supported on disk — this card does not
  rewrite SOULs or migrate profiles (that is the next card).
kind: Verified
task: t_c6eccd49

- **The interview, the resolver and the no-hardcoding rule are pinned by
  tests.** `hscc-bootstrap/tests/test_bootstrap_main_orch.py` extracts the
  exact `ask_main_orch_name` shell function and runs it against a throwaway
  `HSCC_DIR`/`HERMES_HOME` for stored, unstored, `--yes`, `--dry-run` and
  invalid-then-valid answers (asserting the dry run writes nothing).
  `hscc-roles/tests/test_main_orch.py` covers the `general` fallback, stored
  precedence, overwrite refusal, every validation arm and a grep guard that no
  `GENERAL_PROFILE`/`GENERAL_BOARD`/`general-orch` literal or
  `name == "orchestrator"` lookup returns outside the resolver (docs and tests
  excepted).
