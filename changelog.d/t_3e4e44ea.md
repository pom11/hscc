kind: Fixed
task: t_3e4e44ea

- the merge queue's local pytest gate now narrows repo prose and Apple-client-only diffs to the `scripts` leg, mirroring `.github/scripts/leg_select.py`, so a docs-only PR no longer pays the 12-leg roster (PR #725 timed out at 3600 s). A path no rule claims still widens to the full suite.
