kind: Fixed
task: t_ac8de745

- **The README lint no longer rejects `ruby -c`, and the allow-list entry is
  now pinned by a test.** The CMD-BINARY rule in `scripts/readme_lint.py`
  flags any quoted command whose head is neither an hscc verb, a repo-local
  script, nor on the external-CLI allow-list. `ruby` was missing from that
  list, so the sentence in `scripts/README.md` that documents the cask render
  gate tripped it. The quoted command is real, so the allow-list was the side
  that was wrong: `ruby_check` in `scripts/bump_formula.py` and
  `scripts/bump_cask.py` runs `ruby -c` on the tap's Ruby formula and cask
  before the tap is touched, and each refuses the bump when ruby is absent
  rather than passing silently — a formula or cask that does not parse breaks
  `brew tap` for every user. `ruby` is admitted narrowly: on `EXTERNAL`, where
  the head simply passes, not on `INTERPRETERS`, which would go on to demand a
  first script argument that a `-c` invocation does not have. The rule itself
  is untouched and an unknown binary with a flag still fails.
