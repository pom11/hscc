kind: Fixed
task: t_88ec1cb2

- **Generated orchestrator text claimed the merge unconditionally.** Rule 5 of
  the "How you manage work" block every `<project>-orch` SOUL is built from
  read "**You review and merge your board's PRs.**". That is true only where no
  card says otherwise: on a finish rail where the worker merges its own PR into
  the integration branch once its gate is green and review happens once, on the
  branch, the sentence tells the orchestrator to redo work the worker already
  did — duplicated review, a second merge attempt, or a wait for a merge that
  never arrives. Rule 5 now separates review from merging, names the card body's
  `HANDOFF` section as the authority for who merges, keeps "you review and merge
  your board's PRs" as the stated default for a silent card, and says that a
  card naming a worker-side or branch-level owner overrides that default
  explicitly. No project or board name appears in the text, so it reads the same
  on every board; the default is unchanged.
---
kind: Verified
order: 1

- `hscc-roles/tests`: **219 → 223** cases, both arms green and measured
  back-to-back on one interpreter (`~/.hermes/hermes-agent/venv/bin/python -m
  pytest hscc-roles/tests -q`), baseline at revision `7e546339`, head at this
  branch. The four added cases pin rule 5: the `HANDOFF` is the authority and
  says where to look, the orchestrator-review-and-merge default is stated only
  in a sentence that marks it the default, no project or board is named, and the
  policy stays out of worker SOULs. Eight mutation probes on a scratch copy of
  `hscc-roles/` (provenance-gated: a plugin asserts the imported `generator` is
  the copy) ran against a green control — seven failed the expected case (old
  unconditional wording, removed default marker, an inserted board name, all
  three `HANDOFF` mentions renamed, a dropped "card body" pointer, neutralised
  override wording, the policy leaked into `_WORKER_OPS`); the eighth renamed
  one of the three `HANDOFF` mentions only and was too narrow to bite, so it was
  re-run on all three.
