# Clean fixture — the POSITIVE input for scripts/readme_lint.py.

[![v9.9.9](https://img.shields.io/badge/version-9.9.9-blue.svg)](CHANGELOG.md)
[![MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![docs](https://img.shields.io/badge/docs-current-yellow.svg)](docs/index.html)

Every command quoted here exists in the real CLI surface or the tree.
Run `hscc status` first, then `hscc template list` and `hscc cluster
status`. External CLIs are allow-listed: `sparkrun cluster list`,
`git pull`, `brew install xcodegen`. The repo script `scripts/real_check.py`
exists, so `python3 scripts/real_check.py` is fine too. Project verbs quote
as real: `hscc project standup`, `hscc project why <card>`.

Fenced blocks are out of scope — this transcript is NOT checked:

```bash
hscc totally made-up verb --here
```
