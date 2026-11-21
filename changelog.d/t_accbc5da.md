kind: Changed
task: t_accbc5da

- **The two copying watcher installers now generate a wrapper, so installed-copy drift becomes structurally impossible instead of merely detected.** `scripts/install_dep_watcher.sh` and `scripts/install_escalate_watcher.sh` used to `cp` the poller/runner into `<bound home>/scripts/`, and a second copy of the code on a host is a second copy that can go stale — the shape t_95eb9f15 had just made *visible* (a live `hscc-dep-watcher` reading as `not configured` because its deployed copy predated stamping). They now generate `<$HERMES_HOME>/scripts/hscc-dep-watcher.py` / `hscc-escalate-watcher.py` — a stub with the resolved repo path baked at install time that runs the repo's `dep_pr_watcher.py` / `escalate_watcher_run.py` — and bind the Hermes cron job to that, exactly the shape `install_alone_sweep_gate.sh` already used. `git pull` is now what changes what the job runs, and there is no second copy to compare. Two shape decisions the tests pin: the stub is **`.py`**, not `.sh`, because Hermes picks a cron script's interpreter by extension and deliberately ignores the shebang (`cron/scheduler_script.py::_script_argv`) and both watchers need Hermes' own dependency venv for `hermes_cli`; and the handoff is **in-process `runpy`**, not `exec`, because on a managed-store install those imports are reachable only inside the process Hermes bootstrapped (hermes-agent #123044), so re-exec-ing the interpreter would lose them. `cron_watchers.WATCHERS[*]["deploy"]` is `wrapper` for all three streams; the `copy` kind stays (a host's deployed plugin payload still declares these streams as copies) and stays tested. Both installers also delete the file the retired shape left behind on install *and* `--uninstall` — a stale `dep_pr_watcher.py` under `scripts/` is otherwise one re-registration away from running again.

---
kind: Added
task: t_accbc5da
order: 1

- **`--status` on both watcher installers now reports the wrapper shape's own failure mode.** The stub bakes an absolute repo path, so moving or deleting the checkout leaves it dangling and the job fails on its next tick: `wrapper:` prints `runs <target>` when the baked path exists, **`DANGLING … re-run this installer`** when it does not, `MISSING` when the stub is gone and `NOT GENERATED` when whatever sits at that path was not written by the installer; a `leftover:` line names a copy the retired shape left. `copy:` (t_95eb9f15) deliberately stays — for a host that has not re-installed it is the line that says *re-run this installer*, and it settles on `not-a-copy` once they have. That is also why `deployed_copy_drift` answers `not-a-copy` only when the copy-shape path is **empty**: a naive `deploy: wrapper` flip would have told a host still running the old `cp`-ed file that it had no deployed copy at all, un-fixing the parent card by returning `not-configured` for the stale install it was written to catch. A leftover is therefore still hashed — mismatch → `drifted` (warn), match → `in-sync` with the re-run advice.

---
kind: Docs
task: t_accbc5da
order: 2

- `scripts/README.md` no longer claims `install_dep_watcher.sh` "installs the poller to `~/.hermes/scripts/`" (it generates a stub pointing back at the checkout), documents `--status` alongside install/uninstall, and records that all three watcher installers now share the generated-stub shape.
