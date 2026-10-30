kind: Added
task: t_c823ebc9

- **`hscc doctor` now catches a copy-paste-minted profile that no test can
  see.** `generate` loops over role *specs*, so a profile dir copied from
  another profile keeps the SOURCE's identity and stays invisible: the live
  `ecofire-pontaje-engineer` ran its whole life under
  `## Role: ecofire-bc-engineer` while `roles/ecofire-pontaje-engineer.yaml`
  existed (added by t_8d22ec81) and
  `test_every_hscc_owned_worker_profile_has_role_spec` was green — an
  existence test can prove a spec is present, it cannot prove the SOUL
  actually carries *its own* role. New read-only check
  (`hscc-roles/role_soul_check.py`, bridged into `run_doctor()` by
  `hscc-bootstrap/doctor_role_soul.py`) audits every profile whose SOUL
  carries the worker signature and reports, per profile: a `## Role:` header
  that disagrees with the profile dir name or with its own spec's `name:`, a
  missing or invalid spec (the one fault `generate` cannot repair), and any
  `## ` heading `compose_soul()` cannot emit — the hand-patched block the next
  `generate` silently drops, so the operator is told to fold it into the spec's
  `identity` *first*. The emittable set is read from
  `base-identity.md` and generator's three ops blocks, never re-typed here, so
  a new shared value cannot make the doctor flag a legitimate heading.
  Orchestrator-family SOULs are excluded by design: `orchestrators.py` stamps
  a per-project orch with the name of the SHARED spec it passes
  (`general-orch` legitimately carries `## Role: orchestrator`), so a dir-name
  comparison would red-flag a layout that is correct by construction.
- **Reported, not asserted — deliberately.** The card's ruling was that this
  must not become a pytest assertion over live `~/.hermes/profiles`: that is
  precisely the failure mode t_8d22ec81 removed (red on the operator machine,
  vacates in CI, so it teaches nobody and gets written off as "environmental").
  It is therefore a `hscc doctor` WARNING (non-fatal, like "models served"),
  and an absent profiles root says out loud that nothing was compared
  (`no profiles dir at … — nothing scanned (vacuous: nothing to compare)`,
  `scanned: 0` in `--json`) rather than reporting health or crashing.
  The repair — `hscc roles generate` re-stamps every
  generated profile and rewrites live profile state — stays an operator action
  the check never takes on their behalf.
- **No suite can read the operator's live profiles.** The scan's default target
  is `rolelib.PROFILES_DIR`, which is real profile state on the operator
  machine, so `run_doctor()` gained a `_role_soul_scan` seam with the same
  fence as the node-recovery check's `_ssh_factory`: inside a pytest process an
  uninjected scan returns an ERROR naming the missing seam, never a pass and
  never a real read. `hscc-roles` is not importable from `hscc-bootstrap`
  (hyphenated dirs deploy standalone), so the bridge APPENDS the sibling dir to
  `sys.path` — prepending would have let the roles plugin's `_theme.py`
  silently shadow bootstrap's own.
