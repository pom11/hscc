# Backup pile + non-atomic backup writes (t_9462260b)

Follow-up on t_267f9d88 (merged @ d4ea539). Parent fixed *why* the writers ran
against the operator's live home; this card fixes the *damage shape* they left:
an unbounded `.bak-<ts>` pile and a backup path that can truncate the backup
itself.

## Measured state (2026-10-06 ~03:30, before any pruning)

`find ~/.hermes -name '*.bak-*'`, excluding `node_modules`, `.worktrees` and
`cache/scratch`: **32,502 entries** — re-measured 07:05 after the first census
was contaminated by this card's own scratch fake-home (2,129 entries under
`cache/scratch/t_9462260b/home/hooks`). Any re-measurement must exclude
`cache/scratch` and `.worktrees` or it double-counts test fixtures as operator
damage. The pile is **live**: the total moved 32,741 → 34,632 during a single
supervision tick, because suites that write backups are still running from
checkouts that predate this fix.

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

The per-profile `hooks/` counts (687 / 222) are the least stable cells in the
table: an active suite run adds one `cluster-guard.py.bak-<ts>` to the profile
hooks dir every ~5 s, so treat them as order-of-magnitude. The card filed 325
hooks baks; the real pile is ~85x that, oldest baks from June. Every
bootstrap/doctor run adds one per writer.

### The empty backups — blast radius is wider than first written

First pass here said the empties were all under `profiles/backend-engineer/
hooks/`. **Wrong, and the correction strengthens the case.** Measured at 07:05,
`find ~/.hermes -name '*.bak-*' -size 0` (excluding worktrees/scratch) → **6
files, all at the ROOT `~/.hermes/hooks/`**, stamps `20260826-054801`,
`20260924-160104`, `20260924-165320`, `20260925-183053`, `20260925-183103`,
`20260925-183129`.

So the truncation bug zeroed the **root** rollback copy too, in **August and
September** — months before the t_267f9d88 test window, and independent of it.
That means the bug was live in production installs, not only under the test
suite. The 9 empties seen at 03:25 under `profiles/backend-engineer/hooks/`
(stamps 20261006-0057..0147, the t_267 suite window) are since gone: pruned
when that family turned over to keep-3. Both cohorts were the rollback point
itself reading as a valid backup while containing nothing.

## Findings

### F1 — writers opened the backup path before streaming (truncation)

`install_soul.py` / `enable_plugins.py` / `doctor.py` created `dst.bak-<ts>` by
opening or copying *into the final name*, so an interrupt between open and
close left a zero-byte or partial file **at the rollback path**. Nothing in the
read path distinguishes a truncated backup from a real one — a rollback into it
silently restores an empty config/guard. Evidence: the 6 root + 9 profile
empties above.

`enable_plugins._ensure_hooks_file` also had a same-second collision: `os.path.exists(bak)`
is checked but `shutil.copy2` is not exclusive, so two runs in one second
overwrote each other's backup.

### F2 — no retention limit anywhere in the bootstrap/doctor path

`cluster_template.py` and `hscc_daemon/daemon_ops.py` both prune with a keep-N
loop; the bootstrap writers had no equivalent, which is how `hooks/` reached
27,119 files for one live 8,387 B file. Same for the moved-into piles
(`plugins-backups/`, `scripts/`), which have no live file beside them at all.

### F3 — mtime is the wrong clock for this pile (load-bearing)

`shutil.copy2` preserves the **source** mtime. A backup taken at 06:08 of a file
last touched in June carries a June mtime. Consequences:

- **Ordering:** pruning sorted by mtime would rank a fresh backup *below* one
  taken days ago and delete the newest first. `backup_util` orders by the
  **name stamp**, falling back to mtime only for names with no machine stamp.
- **Auditing:** `find ~/.hermes -name '*.bak-*' -newermt '...'` cannot see this
  pile. That query returns **0** while 12 files with 5-second-spaced *name*
  stamps sit in `profiles/backend-engineer/hooks`. Any age audit of this pile
  must parse the stamp from the name — the trap is easy to fall into because
  `-newermt` is the obvious tool and it silently returns nothing.

### F4 — the suites were a writer (fixed on this card)

`enable_plugins` binds `HOOKS_DIR` / `CLUSTER_GUARD_DST` from `HERMES_HOME` at
**import** time — correct for bootstrap, which exports it. But the suites run in
the worker env, where `HERMES_HOME` is exported at the *live profile*, and 85 of
87 `test_enable_plugins` cases call `enable()` without patching those names. So
each of those wrote a real `.bak-<ts>` into the operator's `hooks/` — measured at
1 per ~5 s of suite runtime, and before this card's atomicity fix, 9 of them came
out zero-byte. Reproduced and closed: `tests/conftest.py` now redirects the three
writer constants into a throwaway dir before any test imports the writers, and
`test_writers_never_reach_the_exported_hermes_home` +
`test_hooks_destination_is_not_under_a_live_home` pin it.

An **import-time redirect alone is not enough**, and the reason is worth
recording because it is easy to get wrong twice: `test_enable_plugins.py` calls
`importlib.reload(ep)` in five places (to re-read model aliases from a clean
env). `reload` **re-executes the module body**, so it re-binds
`HOOKS_DIR`/`CLUSTER_GUARD_DST` straight from the exported `HERMES_HOME` and
silently wipes any import-time patch. Measured with only the import-time
redirect: backups kept appearing mid-suite at 5 s intervals for every `enable()`
case *after the first reload*, with the count pinned at 3 — i.e. my own new
keep-3 pruning was tidying the leak as it happened, which is exactly the kind
of self-cleaning evidence that hides a defect. Fixed with an autouse teardown
fixture that re-applies the redirect after each test (teardown, not setup, so
the reload tests' own assertions on model constants complete first). Confirmed:
`test_enable_plugins` + `test_backup_util` = **137 passed** with the live profile
`hooks/` at 3 `.bak-*` before and after *and the newest stamp unchanged* — zero
new writes across the whole reload-heavy file.

Note for whoever runs the gate: a checkout that predates this conftest (e.g. the
t_163fa09f gate worktree) still leaks while its suites run. Live `cluster-guard.py`
md5 was verified unchanged (`f394d6a7`) across every run in this card, so the leak
created noise in the pile, not damage to live state. Same caution for the
`suite-as-writer` classes still open elsewhere: `install_triggers.py` and
`preserve_autodown.py` bind `HSCC_DIR` from `os.path.expanduser("~/.hscc")`, and
`test_install_triggers.py` reloads that module too — those two tests patch `HOME`
so they land in `tmp_path`, but the pattern is the one to check first if `~/.hscc`
ever grows backups during a run.

## Design

One sink — `hscc-bootstrap/backup_util.py` — for atomicity, retention, and the
one-shot sweep. `enable_plugins`, `doctor`, `install_scripts`, `install_soul`,
`install_triggers` and `install_payload` all route through it.

- **`atomic_copy` / `backup_file`**: `copy2` to a hidden `.<name>.bak-<stamp>.tmp`
  sibling, `fsync`, then `os.replace`. A half-write can never occupy a retained
  backup name. Backup failures **propagate** (a caller about to overwrite
  operator state must know), while **pruning never raises** (hygiene can't fail an
  install).
- **`select_victims`**: order by name stamp (F3), keep the newest `keep` (clamped
  at 0 — a negative slice would silently keep everything), spare anything younger
  than `max_age_s`.
- **`prune_backups(src)`** for writers; **`prune_backup_dir(dir)`** for the
  moved-into piles, grouping by everything before the first `.bak-`.
- **`stamped_only=True` default**: operator-labelled bookmarks (`*.bak-before-
  relay-fix`) are never removed by hygiene; only the CLI opts in with
  `--include-labeled`.
- **CLI is dry-run unless `--apply`** (pre-review SHOULD-FIX): a bare
  `python hscc-bootstrap/backup_util.py` reports and removes nothing. Hygiene code
  must not be one bare invocation away from deleting the operator's rollback
  history. Pinned by `test_cli_bare_invocation_deletes_nothing`.

### Deviation from the card body — deliberate, and justified

**The card body asked for pruning by "older than 7 days (keep newest 3 as
safety)". The writer path implements a hard retain cap (keep-N, `BACKUP_KEEP=3`)
with no age criterion; the age floor is applied only to the one-shot `sweep_home`
/ CLI (`max_age_days=7.0`).**

Why the cap supersedes the age gate at the writers: a retain cap is a
self-limiting invariant that bounds the pile regardless of write cadence, whereas
an age gate is cadence-dependent — under a fast writer (measured: one per 5 s) an
age gate still permits ~10k files per family per week, which is exactly how this
pile grew. It is strictly stricter than an age gate under a fast writer and no
looser than the body's "keep newest 3" once the family exceeds N. The cost is
stated plainly: **under `--keep 3` a sweep will delete a backup made 60 s ago**
(that backup is the 4th-newest of an identical-content family; the 3 kept are
byte-identical to it). The 7-day floor is kept on the sweep so the one-shot
operator tool matches the body's intent and can never clear a family created
hours ago.

### Out of scope, deliberately

`hscc-cluster/cluster_template.py` (already keep-N) and the daemon's
`shutil.copy2` of `pyproject.toml` are not touched — different plugin dirs,
different import roots, no sibling-import path to `backup_util`. Recording that
rather than inventing a shared package: the smallest upstream addition would be
promoting `backup_util` into sparkrun or a small installed `hscc-common` wheel, so
daemon and cluster code can import it without a path hack. That is a packaging
decision for the operator, not this card.

## Verification

### Hermetic sweep against a **copy** of the real pile

Per the safety rule, this ran against a scratch copy — never the live `hooks/`
(kept read-only here; `cp -Rp` preserves mtimes so the copy is faithful).

```
$ cp -Rp ~/.hermes/hooks <scratch>/home/hooks      # 27,122 entries, 318 MB
$ python3 backup_util.py --home <scratch>/home --dry-run --keep 3
  hooks            files=27,119 families=1 removed~24,990
$ python3 backup_util.py --home <scratch>/home --apply --keep 3
  hooks            files=27,119 families=1 removed=24,990      # 1.66 s
  after: 2,132 entries, 36 MB   (27,122 -> 2,132; 318 MB -> 36 MB)
```

2,132 survivors = 3 kept + 2,122 backups written inside the 7-day floor. Live
`cluster-guard.py` in the copy was byte-identical to the source afterwards. The 6
root empties (stamps Aug/Sep) are cleared by this sweep — they are far past the
age floor and outside keep-3.

### Suite results (each leg stamped with the commit it ran)

| leg | commit | interpreter | result |
| --- | --- | --- | --- |
| `test_backup_util.py` | `416eef6` | p313 | 26 passed |
| `test_backup_util` + `test_install_scripts` | `6e28e1a` | p313 | 31 passed |
| `test_enable_plugins` + `test_hooks` | `6e28e1a` | p313 | 100 passed |
| backup_util + payload + soul + triggers + scripts | `dac6605` | p313 | 93 passed |
| `test_backup_util.py` | `9f70cdd` | p313 | 48 passed |
| enable_plugins + hooks + backup_util + doctor, `HERMES_HOME` exported at live profile | `9f70cdd` | p313 | **220 passed in 418 s** |

Live-state guard around that last leg: `~/.hermes/hooks` entry count **27,122
before and after** (zero drift), `cluster-guard.py` md5 `f394d6a7…` unchanged.
Note the *profile* hooks dir did churn during that window — attribution in F4:
the operator's own gate suite (`hscc-bootstrap/tests` from `orch_gate_7864796`,
which lacks the conftest fix) was running concurrently at ~1 backup / 5 s. That
process is external to this card and is why landing this fix matters.

### Not yet run (do before/at merge)

- `hscc-bootstrap/tests` on the **hermes venv (py3.11)** interpreter — the
  landing gate, run from a clean checkout of the merge commit in the worker env
  with `HERMES_HOME` exported.
- Full-repo suite (not just bootstrap) on both interpreters.
- The live-dir sweep itself: deferred to post-merge by the operator, and gated on
  no sibling suite writing to the live dirs while it runs.

## Commits

| sha | what |
| --- | --- |
| `416eef6` | `backup_util` — atomic + retain-limited helper |
| `6e28e1a` | route enable / doctor / scripts backups through it |
| `dac6605` | stamp-ordered pruning + `atomic_copy`; wire soul / triggers / payload |
| `9f70cdd` | 7-day age floor on the sweep; stamp-parsed backup age |
| `f73db30` | CLI dry-run unless `--apply`; conftest redirects hooks writes |
