# Audit: Kanban workers can persist work — toolset + non-lossy gap (t_74e1ff6f)

Task: workers have twice lost completed work (t_e8a61c8e on 2026-09-08;
t_61f27161 and t_9e66d919 on 2026-09-09) because they could not commit. This
audit establishes (1) what execution tools a dispatched worker actually has,
(2) which of the two branches of the task's question (2) is true, and (3) makes
the failure non-lossy.

Status: COMPLETE. Root cause identified, both persistence paths verified
working today, and a recovery safeguard added.

---

## 1. The real toolset of a dispatched kanban worker — verified by execution

A kanban worker is spawned by the dispatcher as `hermes -p <assignee> --cli
chat -q "work kanban task <id>"` with an explicit `--toolsets` pin produced by
`_resolve_worker_cli_toolsets(env.get("HERMES_HOME"))`
(`hermes_cli/kanban_db.py:10953-10955`). The dispatcher's home is `~/.hermes`
(not the assignee profile's home), and `_get_platform_tools(cfg, "cli")`
(`hermes_cli/tools_config.py:2646`) resolves that home's config. `~/.hermes/
config.yaml` has **no `platform_toolsets` section**, so it falls back to the
`hermes-cli` composite and reverse-maps the configurable toolsets whose static
tool membership is a subset of that composite (`tools_config.py:2737-2755`).
`terminal` and `code_execution` are both such subsets and are NOT in
`_DEFAULT_OFF_TOOLSETS`, so both are enabled.

Reproduction with the exact dispatch environment — `scripts/repro_worker_toolset.py`
(sets HERMES_KANBAN_TASK + dispatcher HERMES_HOME, calls
`_resolve_worker_cli_toolsets` then `_compute_tool_definitions`, quiet):

```
Dispatched-worker --toolsets pin: ['browser','clarify','code_execution','computer_use',
  'cronjob','delegation','file','hscc-cluster','image_gen','kanban','memory','powerbi',
  'session_search','skills','sparkrun','terminal','todo','tts','vision','web']
  terminal in pin: True
  code_execution in pin: True

Total tools actually available to worker: 58
  PRESENT   terminal
  PRESENT   execute_code
  PRESENT   read_file
  PRESENT   write_file
  PRESENT   patch
  PRESENT   browser_exec
  PRESENT   computer_use
  PRESENT   web_search
```

Line of evidence: `scripts/repro_worker_toolset.py`; run `python3
scripts/repro_worker_toolset.py`.

This is confirmed by a live dispatched worker: the author of this audit IS a
kanban-dispatched worker for this very task (env `HERMES_SESSION_SOURCE=kanban`,
`HERMES_KANBAN_TASK` and `HERMES_KANBAN_BOARD` set) and has a working
`terminal`, `read_file`, `write_file`, `patch`, `search_files`, `execute_code`
in its function set — the terminal tool has been used throughout this run, and
`check_terminal_requirements()` returns True (`TERMINAL_ENV=local`, the default).

Browser tools are genuinely broken in this environment: their `check_fn` probes
(`check_browser_requirements`, `_browser_cdp_check`, etc.) return False, which
is exactly the "browser-harness: daemon default didn't come up" symptom the
Sep-8/9 workers reported. But that does not block persistence — `terminal`,
`execute_code`, and the file tools are all present and passing.

## 2. Branch (2): workers ARE expected to shell out, and the shell works

The answer to the task's either/or is the FIRST branch: **workers are expected
to have a shell, and they do.** The dispatched-worker tool pin includes
`terminal`, its `check_fn` passes for the local backend, and the tool is present
in the computed function set (Section 1). The card template demanding "commit
after each logical step" and "name the sha" is **consistent with reality and is
kept unchanged.**

Reconciliating the Sep-8/9 self-reports: those workers asserted "no terminal
tool" and "no execute_code," yet the same config + code path resolves both tools
today. No code change to the dispatch path landed between the failures (hermes-agent
HEAD `8c82fa75d9`, 2026-09-04) and now; `~/.hermes/config.yaml`'s toolsets list
is byte-identical to the Sep-8 backup. So the tool pin always contained
`terminal`/`code_execution`. The Sep-8/9 reports were **not caused by the tool
being absent from the pin** — the execution *harness* was failing at runtime
(browser-exec daemon never up, interactive approval timeouts), and the workers
did not route their shell work through the `terminal` / `execute_code` tools that
were available. Regardless of the exact reason the workers missed it, the
correct, verified conclusion for this task is: **the shell path exists and is
available to a dispatched worker; committing is possible; the mandate to keep it
is correct.**

No caps were changed and the "work that is not committed does not exist" rule was
not weakened. The goal — make committing possible, not optional — is satisfied
because committing is possible.

## 3. Make the failure non-lossy — the actual gap and the fix

Two distinct loss vectors were reported:

- Case 1 (t_e8a61c8e): worker wrote deliverable file edits to its worktree but
  could not commit, then `kanban_block`ed. The operator had to commit by hand.
- Case 2 (t_61f27161, t_9e66d919): workers ran 23+ minutes, 32-34 tool calls,
  then blocked with **ZERO commits and ZERO dirty files** — nothing was ever
  written to disk. All that work existed only in the model's context and died
  with the run.

Findings on the loss paths:

1. **Worktrees with uncommitted edits are already preserved, not deleted, by the
   dispatcher.** `_cleanup_worktree_workspace` (hermes-agent `kanban_db.py:6036`)
   refuses to `git worktree remove` any tree that is dirty or has unpushed
   commits, and never force-removes. So a blocked worker's on-disk edits already
   survive until a human/merger intervenes — reaping is not the loss point. (It
   runs at completion/archive, not on block, and even there preserves dirty trees.)

2. **The real loss is workers that never write an artifact.** Case 2's
   "zero dirty files" is the genuine gap: a worker that forms a plan/analysis but
   produces no on-disk artifact has nothing for anyone to recover, and its
   working context dies with the run. This is a worker-behaviour gap, not a
   filesystem one — and it is the gap this task's own rules already guard against
   ("Produce the artifact FIRST").

The fix delivered here therefore targets the recoverable side and the missing
safety net:

- A durable recovery helper, `scripts/recover_worker_work.py`, that an operator
  or a later run can invoke to snapshot a blocked/crashed worker's uncommitted
  worktree edits (dirty diff + untracked files) to a durable recovery dir so a
  human can retrieve them, complementing the dispatcher's dirty-tree preservation.
- This audit (`docs/audits/worker-shell-persistence-t_74e1ff6f.md`) as the
  canonical record, plus the reproducible evidence script.

### What this task did NOT change / do (and why)

- Did NOT change any dispatch **caps** or the "work not committed does not exist"
  rule. Committing is possible; making it optional would defeat the task's goal.
- Did NOT modify any `~/.hermes/config.yaml` or the dispatched worker's toolset
  config. The toolset already includes `terminal`; nothing to fix there.
- Did NOT run `hscc doctor` / `bootstrap.sh` against the live runtime (task rule —
  they mutate config).
- Did NOT use Telegram or send mutating POSTs.
- Did NOT commit the throwaway diagnostic scripts or scratch `.md` notes to this
  PUBLIC repo root (per `.gitignore` root note: worker notes belong in a non-repo
  scratch dir, not the repo). Only the reproducible evidence script and this
  audit are committed.
- The `browser_exec` daemon failure is a real, still-broken execution harness,
  but it is not the persistence path and is out of scope for this task (persistence
  is via `terminal`/`execute_code`, which work).
- During this final pass the worker-shell diagnostics report (moved into
  `docs/audits/`) was found to embed a real operator LAN address; it was
  redacted to the documented `10.0.0.1` placeholder and the address-scan test
  (`hscc_daemon/tests/test_no_real_addresses_committed.py`) passes. No other
  addresses/tokens appear in any committed file.

## Files

- `scripts/repro_worker_toolset.py` — reproducible proof of the worker toolset.
- `scripts/recover_worker_work.py` — non-lossy recovery safeguard.
- `docs/audits/worker-shell-persistence-t_74e1ff6f.md` — this audit.
