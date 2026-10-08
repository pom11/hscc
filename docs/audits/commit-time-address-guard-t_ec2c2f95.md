# Commit-time address guard — t_ec2c2f95

**Card:** `[infra] Real LAN addresses reach the public repo: the address guard never
runs on docs-only commits`
**Branch:** `wt/t_ec2c2f95` · **Repo:** pom11/hscc (PUBLIC)

Real addresses redacted per the documented placeholders: LAN `10.0.0.x`, tailnet
`100.64.0.1`. Nothing in this file, or in the diff it describes, carries a real
address — and that is now enforced at commit time, not by discipline.

---

## 1. What actually happened

At 19:52 on 2026-10-08 the orchestrator's ledger tick committed
`docs/audits/GOAL_LEDGER_2026-09-26.md`. Two lines recorded the NAS diagnosis
verbatim — a `showmount -e` export list and an operator-queue line carrying a
complete `sudo mount_nfs -o resvport <real NAS host>:/models /Volumes/NAS`
command — so the real NAS host and its subnet were in the file. The commit
reached `origin/main` on a **public** repository. The operator scrubbed the
working tree at `524024a4`; the addresses remain in the pushed history of the
leaking commit (rewriting published history is the operator's separate decision
and was NOT done here).

Two facts make this the second occurrence of the same class:

| date | how a real address reached a tracked file | caught by |
|---|---|---|
| 2026-08-30 | audit report quoting a live tailnet host | after it reached origin |
| 2026-10-08 | ledger tick recording a `mount_nfs` command | the pytest gate, after the fact |

## 2. Root cause — the guard was right, the trigger was wrong

`hscc_daemon/tests/test_no_real_addresses_committed.py` scanned everything git
*tracks* and it **fails** on the leak: run against the leaking content it reports
`docs/audits/GOAL_LEDGER_2026-09-26.md:4217` and `:4220`. So the pattern was
never the problem, and neither was the file population.

The problem is the trigger. A pytest gate runs when somebody runs the suite. A
ledger tick is a `git add docs/... && git commit` performed by a cron-driven
agent, and nothing in that path runs pytest. The guard had 100% recall and ~0%
coverage on exactly the commits that leak: docs-only ones. Adding a second,
stronger regex would have changed nothing — a detector that never runs has
perfect precision and zero effect.

So this card moved the trigger, not the detector.

## 3. Design

One implementation, two triggers:

```
scripts/address_guard.py          <- THE detector (regex, skip list, verdicts, report text)
   ├── scan_staged(repo)          -> the INDEX (staged blobs)   → .githooks/pre-commit
   └── scan_tracked(repo)         -> every tracked file on disk → the pytest gate
```

Decisions worth recording, each because a plausible alternative was wrong:

**Committed `.githooks/` + `core.hooksPath`, not `.git/hooks`.** `.git/hooks` is
not cloned, so a hook written there exists only on the machine that wrote it and
is absent on every fresh clone and every worker worktree. `core.hooksPath` is the
mechanism git provides for shipping hooks with a repository.

**Relative value (`.githooks`), not absolute.** Measured: with a *relative* path a
linked worktree resolves the hook from **its own** checkout (`pwd` = worktree), so
each worktree guards its own staged tree. An absolute path would make every
worktree execute the main checkout's hook — wrong repo, wrong index.

**Written to the COMMON config.** `install_hooks.py` writes once and every linked
worktree inherits it, including a worktree created *outside* the repo directory
(the dispatcher's shape). Measured, see §4.

**Staged blobs, never the working tree.** The 2026-08-30 audit's pre-push check
grepped the working tree — which had already been scrubbed while the committed
blob still carried the address. `--staged` reads `:path` specs from the index via
one `git cat-file --batch`, so a scrubbed checkout cannot mask a dirty commit.
This is tested deliberately (`test_staged_blob_beats_scrubbed_worktree`).

**Every staged tracked file, not just `*.py`.** The leak was a Markdown file. The
only skips are the original noise list (`.png`, `.pdf`, …) plus a NUL-byte
binary check, and deletions (a removed file cannot introduce a new leak).

**Fail closed.** No interpreter, no git, or a missing `address_guard.py` exits
non-zero with "cannot run", which blocks the commit. A guard that cannot run must
never wave a commit through — that is the same failure mode as the local
`docker ps` that returned empty instead of erroring.

**`--no-verify` is not blocked, and is called out in the failure text.** git
provides no supported way to prevent an operator from bypassing a hook, and this
repo is the operator's. The mitigation is that bypassing is now a visible,
deliberate act on a file the guard just named.

## 3b. Recall against the real leak (not a synthetic fixture)

The detector was run against the **actual pushed blobs** in the operator's
checkout (addresses printed only as `192.x.x.x`):

| blob | offenders found | lines |
|---|---|---|
| `bbaeaa2f` (the leaking ledger tick) | **2** | **4217, 4220** |
| `524024a4` (the operator's scrub) | 0 | — |
| `HEAD` (today's ledger) | 0 | — |

Lines 4217 and 4220 are exactly the two lines the card names, so this is the
card's own incident reproduced by the shipped code — not a fixture invented to
match the regex.

## 4. Measured, not assumed

Every one of these was run on this host; none is inferred from git's docs.

| claim | how it was measured | result |
|---|---|---|
| `core.hooksPath` fires in a **linked worktree** | throwaway repo + `git worktree add`, one inside the repo, one **outside** it | hook fired in both; `PWD` = the worktree, `--git-common-dir` = main `.git` |
| the hook sees the **staged** set | hook printed `git diff --cached --name-only` | correct file per commit |
| `git commit -am` is **not** an evasion | hook that logs the staged set and exits 1 | blocked; staged set included the never-`git add`-ed file |
| missing/empty `.githooks` is harmless | commit with `core.hooksPath=.githooks` and no such dir / empty dir | rc=0, no warning (so arming the config before this branch merges breaks nothing) |
| exec bit matters | non-executable hook whose body returns 1 | **ignored** with a hint → installer repairs the bit |
| `--batch` framing | `cat-file --batch -z` vs plain, byte-level reply inspection | `-z` affects specs only, **not** reply framing (0 NULs in reply) — so newline-in-path names get a per-path `cat-file blob` instead of desyncing the stream |
| **speed** | real repo, 1070 tracked files, 13.3 MB | `ls-files` + one `--batch` = **0.036 s**, scan **0.096 s**, total **0.133 s** |
| dependency-free | import audit asserted in CI | stdlib only (`argparse`, `re`, `subprocess`, `sys`, `pathlib`) |
| the hook fires on a **real commit in a real worktree** | staged a docs file with a real-shaped LAN address in this card's own worktree | **blocked**, printing `docs/audits/_hook_e2e_probe.md:1` + both placeholders. First commit of this card had already been blocked the same way by my own test file's comment. |

## 5. Test inventory

| file | cases | covers |
|---|---|---|
| `scripts/tests/test_address_guard.py` | **25** | detector verdicts (real LAN / real subnet / real tailnet / sanctioned `100.64.0.0/24` fixtures / both placeholders / out-of-scope ranges), line numbers, binary + suffix skips, index-vs-worktree, awkward path names (space, unicode, **newline-in-path**), no-HEAD first commit, CLI exit codes 0/1/2, report text |
| `hscc_daemon/tests/test_precommit_address_hook.py` | **14** | real `git commit` in a throwaway repo carrying the **shipped** hook + detector: docs leak blocked, placeholder accepted, fixture block accepted, tailnet leak blocked, clean tree no-op, unstaged dirt ≠ staged content, **dirty blob + scrubbed worktree blocked**, rename/delete, spaces in paths, fail-closed, subdirectory commit, interpreter discovery without the override, import-whitelist + wall-clock bound |
| `hscc_daemon/tests/test_no_real_addresses_committed.py` | **4** (1 was there before) | the original tracked-tree gate unchanged, plus structural guarantees: the regex exists in **exactly one** tracked file, the hook delegates (no pattern of its own, calls `--staged`, executable), `core.hooksPath` armed |
| `hscc-bootstrap/tests/test_install_hooks.py` | **11** | install/verify/skip paths, idempotent re-run (**zero writes**, asserted by config mtime), exec-bit repair, wrong-path repair, `--dry-run` writes nothing, refuses to fake success for a non-checkout or hookless revision, relative-not-absolute hooksPath, **not-installed→commits / installed→blocks**, **linked worktree outside the repo inherits and blocks** |

Counts verified with `pytest --collect-only` on this branch, not by counting lines.

## 6. Known limits (stated, not hidden)

- `git commit --no-verify` bypasses any pre-commit hook. Unpreventable; called
  out in the failure text.
- `git apply --cached`, `git stash pop`/`checkout`, `format-patch`, and
  fast-import paths write blobs without running `pre-commit`. A human can run
  `python3 scripts/address_guard.py --tracked`; CI could too.
- `core.hooksPath` is **local machine state**, so a fresh clone without bootstrap
  has no hook. `test_hooks_path_is_configured` therefore **skips** (does not fail)
  when the value is unset — the repo's contents are fine; a *wrong* value is still
  a hard failure, since that is the bypass signature. Bootstrap arms it.
- `git push --no-verify` / `git push --force` can publish already-existing dirty
  history. Out of scope: history rewrite is the operator's call.
- A worktree on an older revision (no `.githooks` yet) finds no hook and commits
  normally — which is the pre-card status quo, not a regression, and is why
  arming the config early is safe.

## 7. Follow-ups worth carding (not done here)

1. **CI backstop.** The local gate has the gaps above; a GitHub Actions job running
   `scripts/address_guard.py --tracked` on every push would close them. Needs an
   operator decision to add a workflow to this repo.
2. **Pre-push mirror.** A `.githooks/pre-push` scanning the range being pushed
   would catch the `git apply --cached` shape. Deliberately not added: the card
   asks for a fast commit-time guard, and two hooks doubles the bypass surface.
3. **Ledger hygiene.** The leaking content came from the orchestrator pasting raw
   command output (`showmount -e`, `mount`) into the ledger. A redaction filter on
   the ledger-tick emitter would stop the leak at the source rather than at the
   commit.
4. **History rewrite decision** (operator): the addresses are still in the pushed
   history of the leaking commit; `524024a4` only fixed the working tree.
5. **Make guard status observable.** `hscc check` / `doctor.py` could report
   whether the checkout they run in has the guard armed (`core.hooksPath` set +
   hook executable), so "the guard is silently inactive on this machine" becomes a
   status line instead of an assumption. Deliberately not added here: `doctor.py`
   runs against `~/.hscc`/Hermes state, not against an arbitrary repo, so the
   right home for it is `hscc check --repo <path>` and that needs a decision.
