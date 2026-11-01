kind: Fixed
task: t_c2ec721f

- **The docs/ index enumeration invariant now has a gate, and the 12 files
  that had drifted out of it are backfilled.** `docs/README.md` has always
  claimed "Every tracked file under `docs/` is enumerated below", but
  nothing compared the claim against `git ls-files docs` — so it decayed
  twice after the R6 rewrite (3 files short, repaired manually by
  b8f05170; 10 short by t_684caf31; 12 by this card, the last two from the
  PR #146 proxy-stall analysis scripts). `readme_lint.py` gains rule (4),
  DOC-UNLISTED: every file tracked under `docs/` must have a backtick-quoted
  entry in the index (directory-heading bullets — entries ending in `/` —
  are excluded from the index side per b8f05170's counting convention; the
  files under them are still listed individually). The rule runs whenever
  `docs/README.md` exists under `--repo`, so narrowing `--files` cannot
  silence it, and the pytest repo leg gains a coverage-count floor so a
  vacuous 0/0 enumeration is red by itself. Both directions are pinned:
  the broken fixture grew a mini docs/ tree whose unlisted file and
  fence-only reference must fire while enumerated files must not
  (anti-vacuity, t_573287aa practice), plus a live fire→clear→re-fire
  mutation test. Backfilled: the 2026-10-31 proxy-stalls audit (**live**,
  current authority on proxy stalls) + its two read-only analysis scripts
  and the detached proxy-venv cutover script (**live** runbook), the three
  node-recovery card reports, the brew-keg-venv and autodown-`--json`
  findings, the generate-loss + content-loss-list reports, and the
  cluster-up launch-hardening design record.
