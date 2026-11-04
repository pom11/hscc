kind: Fixed
task: t_fc3eb536

- **`hscc verify --chat`'s deep check was dead on arrival.**
  `hscc_daemon/verify_chat_roundtrip.py` carried a `sys.path` bootstrap that was
  dead code: line 49 imported `hscc_daemon` and line 52 inserted the package
  root, so the insert ran *after* the import it was written to protect. Run the
  way `verify.run_chat_roundtrip` and the installed keg run it — a bare script
  path, so only `hscc_daemon/` lands on `sys.path` and the parent that *contains*
  the package is missing — it died before reading a single config value:
  `ModuleNotFoundError: No module named 'hscc_daemon'` at line 49, exit 1
  (reproduced 2026-11-04 with the repo venv interpreter, which has `rich`, so
  `rich` was never the blocker here). The bootstrap now computes the root from
  `__file__` and runs *before* the import — the shape proven for
  `api_route_sweep.py` in #213 (t_ef36fe71). `verify` tolerated the crash (`rc=1`
  fell back to the traceback text as the row detail, so the row was verbose
  rather than wrongly green), but the check it reports on never actually ran.
---
kind: Fixed
order: 1

- **A missing renderer can no longer take the human view down.** The
  `cli_theme` import in `verify_chat_roundtrip.py` is now guarded, and
  `render_human` falls back to `_render_plain` — the same fixed labels, no Rich.
  With no `rich` in the interpreter running us, the round trip still runs,
  `--json` stays byte-identical (verify's path is renderer-proof) and the human
  view prints plain text instead of raising `AttributeError: 'NoneType' object
  has no attribute 'make_console'` out of `render_human`. The exit code stays the
  round trip's own verdict, so a missing colour scheme is never reported as a
  chat failure.
---
kind: Verified
task: t_fc3eb536

- `hscc_daemon/tests/test_chat_roundtrip_standalone.py` (11 cases) runs the
  script by bare script path from a cwd outside the repo with a scrubbed `HOME`,
  no `hscc` on `PATH` and `PYTHONPATH` dropped so the child cannot inherit the
  real repo for free, and asserts it never dies with `ModuleNotFoundError`. It
  asserts on the preconditions path (no API host/token → exit 2 + parseable
  error), so **no real chat prompt is ever fired at the live orchestrator**; a
  fake keg in `tmp_path` supplies the package, and the real `cli_theme.py`
  planted in it proves the Rich panel actually draws from a bare script path
  (delete the bootstrap and that case goes red). Six mutation probes in a
  throwaway clone — bootstrap-after-import, bootstrap deleted, unguarded theme
  import, fallback renderer removed, fallback lying about the exit code, AST
  guard neutered — all KILLED, each reverted before the next.
- The repo was swept for the same ordering fault with an AST statement-order
  check (a module-level `import hscc_daemon` before the first module-level
  `sys.path` call, function-level lazy imports excluded), and the check now
  *ships as a test* so the report cannot rot: any bare-runnable module that
  reintroduces bootstrap-after-import fails the suite with `file:line`. The
  checker was first validated against the known-bad file so a false "all clean"
  was impossible. **Only one module had the ordering bug — this one.** Three
  others import `hscc_daemon` from a bare-runnable file with no bootstrap at all
  and are accounted for, not silently refactored: `api_route_sweep.py`
  (unguarded import and no bootstrap on `main`; its fix ships in #213 and is
  untouched here), `event_driven.py` (peer-guarded import with a
  `_FallbackTheme`, so a bare path renders unthemed instead of crashing) and
  `hscc.py` (launched with `PYTHONPATH` pointed at the package root by its
  launchd/systemd unit in `hscc_daemon/install.py` and by
  `hscc-bootstrap/restart_daemon.sh`, whose own comments record this exact
  `ModuleNotFoundError`).
