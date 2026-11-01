# Broken fixture — mini docs index for rule (4) (DOC-UNLISTED).

Deliberate defects, pinned by name in
scripts/tests/test_readme_lint.py::test_broken_fixture_fails_every_rule:
docs/ghost.md exists in this fixture's docs/ tree but has NO backtick-quoted
entry here (mentioned in this sentence without backticks on purpose — a
backticked mention would satisfy the gate and silence the negative). And
docs/fenced-only.md is referenced only from the fenced block at the bottom:
a fence is not an index entry, so it must still be flagged.

docs/listed.md is present AND enumerated below, proving the check is
selective, not blanket. The docs/sub/ bullet is a directory HEADING (ends
in /), not a file entry — docs/sub/nested.md is listed individually under
it, which is the b8f05170 counting convention the gate implements.

- `docs/README.md` — this mini index (rule (4) requires the index to
  enumerate itself, same as the real one). **live**
- `docs/listed.md` — enumerated, must NOT be flagged. **live**
- `docs/sub/` — directory heading, not counted as a file entry.
- `docs/sub/nested.md` — enumerated below its heading. **live**

Fenced quotes do NOT count as index entries (pinned by
test_broken_fixture_fails_every_rule):

```
see `docs/fenced-only.md` here — inside a fence, so it must not count
```
