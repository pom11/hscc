kind: Removed
task: t_60d2ef4e

- **`hscc-master-knowledge` is retired from installs, not left as an
  unmanaged orphan.** The July-era skill is present in `~/.hermes/skills/` but
  is not vendored under `install/hscc-skills/`, so no bootstrap ever updated
  it (audit t_4c12ff7f F12). A new `RETIRED_SKILLS` list in
  `hscc-skills/hscc.py` names it, and both installer paths (`install` and the
  `install-skills` stage `bootstrap.sh` runs) now move any copy out of the
  scanned `skills/` tree into `<home>/skills-backups/removed-<stamp>/` —
  a `os.rename`, never a delete, so operator edits made after July survive
  byte-for-byte as a dated backup. One stamp per run; a same-second re-run
  appends `-2` instead of overwriting. A clean home creates no directory, and
  names outside `BUNDLED_SKILLS`/`RETIRED_SKILLS` stay untouched.
---
kind: Verified
task: t_60d2ef4e

- `hscc-bootstrap/tests/test_skill_install_update.py`: 12 -> **17** cases, all
  green. The five new cases drive the real CLI as a subprocess in a sandboxed
  `HERMES_HOME` + `HSCC_SKILLS_SRC`: the orphan (with a post-July nested
  edit) leaves `skills/` and comes back out of
  `skills-backups/removed-<stamp>/hscc-master-knowledge/` by md5; a clean home
  gets no empty backup dir and a second run moves nothing; the full `install`
  verb retires it too; and two retirements under one stamp become
  `hscc-master-knowledge` + `hscc-master-knowledge-2`. `install-skills` and
  `install` were also run by hand against a throwaway `HERMES_HOME`, not just
  through pytest.
