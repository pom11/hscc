# Broken fixture — the NEGATIVE input for scripts/readme_lint.py.

Every deliberate defect below is pinned by name in
scripts/tests/test_readme_lint.py::test_broken_fixture_fails_every_rule.
Keep this file and that test in step: a defect removed here must be removed
there too, and the test must never be made green by editing this file alone.

[![v1.2.3](https://img.shields.io/badge/version-1.2.3-blue.svg)](CHANGELOG.md)
[![v7.7.7](https://img.shields.io/badge/release-v7.7.7-blue.svg)](CHANGELOG.md)

The first badge disagrees with the VERSION file (7.7.7); the second matches
it exactly, so a red run over this file must carry exactly ONE badge
finding — proving the badge check is selective, not blanket.

Dead verb (the audit X-column hscc-doctor shape): the CLI has no
`hscc frobnicate --now`, and the lint must catch it even quoted with flags.

Dead script: `python3 scripts/ghost_check.py` points at a file that does
not exist — while the fixture's own `scripts/` directory DOES exist, so the
path anchors there and the leaf must fail as a genuinely broken reference
(this is the real shape, not an unanchorable foreign-repo path).

```bash
hscc also-made-up --in-a-fence   # out of scope: fenced blocks are not quoted commands
```
