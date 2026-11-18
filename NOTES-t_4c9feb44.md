# t_4c9feb44 — scripts/tests leg stalls at ~test 233 (`test_cli_help_surface.py`)

Status: **investigating**. This file is the worklog; the fix (if any) lands with it.

## Restated problem

`pytest -q -rs scripts/tests` stalls at test ~233/468, exactly where collection
order reaches `scripts/tests/test_cli_help_surface.py` (cum=232 -> 258). The
whole file (26 items) makes no progress; even the trivial
`test_surface_is_enumerated` alone never completes. Reproduced on `main`
(`a65695a6`) in a clean detached worktree on this host — so it is NOT caused by
t_03c0a890 (PR #398).

## Evidence gathered (this run)

Host: macOS, `~/.hermes/hermes-agent/venv/bin/python` 3.14.10, env
`HERMES_DISABLE_LAZY_INSTALLS=1 HSCC_SKIP_COMPACT_PROBE=1`.

### Probe 1 — process-tree watch of the single trivial test (`probe/capture.sh`)

Ran `test_surface_is_enumerated` alone for 75 s. The pytest process is **not**
stuck in one child: the tree shows the CLI probe children appearing *briefly and
sparsely*, while pytest itself accumulates only ~0.1 s CPU per 5 s sample:

```
20:09:53  35718  python -m hscc_daemon.hscc bootstrap --help   (R, cpu 0:00.06)
20:10:09  35913  python -m hscc_daemon.hscc start --help        (R, cpu 0:00.05)
```

So the module-scoped `report` fixture (`audit_mod.audit(REPO)`) IS advancing,
but it spends the overwhelming majority of wall time *between* CLI probes with
essentially zero CPU — a blocked wait inside the audit's per-probe bookkeeping,
not inside the CLI subprocess itself. This refines the card's premise: the file
does not "hang", it crawls at roughly one probe per ~15 s, which for 331 probes
is >80 minutes and reads as a hang from the outside.

## Where the block is NOT (card's ruled-out traps, confirmed)

- not the compact probe (`HSCC_SKIP_COMPACT_PROBE=1` set);
- not lazy installs (`HERMES_DISABLE_LAZY_INSTALLS=1` set);
- not a per-test slowness: all 26 tests share the module fixture;
- not t_03c0a890 / PR #398: no diff to this file or `scripts/tests/conftest.py`.

## Next step (in flight)

`probe/sample_probe.sh` — native `sample` of the blocked pytest pid plus an
`lsof` snapshot, to name the C-level wait between probes.
