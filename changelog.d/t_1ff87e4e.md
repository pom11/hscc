---
kind: Fixed
task: t_1ff87e4e

- **`routes_profile.hermes_root_dir` now uses the shared `is_profile_dir`
  predicate.** The profile-dir filter in the `hermes_homes()` ladder had been
  spelled inline, so it could drift from the canonical profiles-grandparent
  rule in `hscc_daemon/hermes_python.py`. Behaviour is unchanged; the filter is
  now pinned structurally and by a hermetic skip test.
