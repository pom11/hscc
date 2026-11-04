# HERMES_HOME join audit — root-intent vs profile-intent

Task: t_8893ab89 (report-only; no fixes applied).
Branch: `wt/t_8893ab89` @ (sha added on first commit).
Method: `git grep -n "HERMES_HOME"` over all file types, then one-hop trace of every
variable that flows from it (`hermes_home`, `home`, `_hh`, `HERMES_HOME` module
constants, `$HERMES_HOME` in shell, `PLUGINS`, `HERMES_SCRIPTS`, ...) to its JOIN SITE.

## Ground rules used for classification

- ROOT-intent = the joined path lives at the Hermes ROOT (`<root>/hermes-agent`,
  `<root>/plugins` as bootstrap payload *source*, root `config.yaml` template,
  `<root>/skills` written by the installer). A raw join under a profile session
  resolves `<root>/profiles/<name>/hermes-agent/...` which does not exist.
- PROFILE-intent = the path Hermes itself reads/writes per-profile
  (`get_hermes_home()/plugins`, profile `config.yaml`, profile `sessions`,
  profile `hooks`). A raw join is CORRECT here — this is a finding, not a defect.
- AMBIGUOUS = the same code line means different things depending on the caller
  (daemon vs dispatched profile session vs installer). Both behaviors named.

Status: IN PROGRESS — findings appended as measured.

## Findings table

(to be filled)

## MISCLASSIFIED / AMBIGUOUS list

(to be filled)
