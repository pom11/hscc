# Backup pile + non-atomic backup writes (t_9462260b)

Follow-up on t_267f9d88 (merged @ d4ea539). Parent fixed *why* the writers ran
against the operator's live home; this card fixes the *damage shape* they left:
an unbounded `.bak-<ts>` pile and a backup path that can truncate the backup
itself.

## Measured state (2026-10-06 ~03:30, before any pruning)

`find ~/.hermes -name '*.bak-*'` (excluding node_modules and `.worktrees`):
**33,173 entries**.

| location | count | families |
| --- | --- | --- |
| `~/.hermes/hooks/` | 27,119 | all `cluster-guard.py.bak-<ts>` (live file present) |
| `~/.hermes/plugins-backups/` | 3,385 | 1.2 GB, ~20 payload families x ~172 stamps |
| `~/.hermes/profiles/backend-engineer/plugins-backups/` | 678 | 265 MB |
| `~/.hermes/profiles/backend-engineer/hooks/` | 687 | `cluster-guard.py.bak-<ts>` |
| `~/.hermes/profiles/hscc-orch/plugins-backups/` | 620 | 248 MB |
| `~/.hermes/profiles/hscc-orch/hooks/` | 222 | `cluster-guard.py.bak-<ts>` |
| `~/.hermes/scripts/` | 262 | 4 x 63 `.sh` + 10 `escalate_watcher_run.py` |
| `~/.hermes/plugins_backups/` (legacy underscore) | 85 | pre-convention dir |
| `~/.hermes/` (config.yaml/SOUL.md/state.db) | 60 | mixed machine + human-named |

The card filed 325 hooks baks; the real pile is ~85x that, oldest baks from
June. Every bootstrap/doctor run adds one per writer.

### The empty backups

`find ~/.hermes/profiles -name '*.bak-*' -size 0` → **9 files**, all
`cluster-guard.py.bak-<ts>` under `profiles/backend-engineer/hooks/`, stamps
clustered 2026-10-06 00:57-01:47 (the t_267f9d88 test-suite window).

## Findings

Append as found. (findings below)
