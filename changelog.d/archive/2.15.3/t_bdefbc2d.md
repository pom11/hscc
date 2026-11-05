kind: Fixed
task: t_bdefbc2d

- **`install_review_skill.py --skill all` missed the root home and sibling
  profiles under a profile session.** Hermes exports
  `HERMES_HOME=<root>/profiles/<name>` whenever it runs under a profile, and
  `default_home()` returned that profile path verbatim; `skill_dirs()` then
  walked `<profile>/profiles`, which does not exist, so only the current
  profile was seeded. `default_home()` now resolves the Hermes ROOT via the
  existing profiles-grandparent rule in `backup_util._hermes_root` (the same
  rule `doctor.py` and `rolelib.py` use), restoring the root skills dir plus
  every root profile. An explicit `--home` is still used exactly as given, so
  a deliberately profile-scoped install remains scoped.
