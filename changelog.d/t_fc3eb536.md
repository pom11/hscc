### Fixed

- `hscc verify --chat`'s deep check is no longer dead on arrival.
  `hscc_daemon/verify_chat_roundtrip.py` carried a `sys.path` bootstrap that was
  dead code: line 49 imported `hscc_daemon` and line 52 inserted the package root,
  so the insert ran *after* the import it was written to protect. Run the way
  `verify.run_chat_roundtrip` and the installed keg run it — a bare script path, so
  only `hscc_daemon/` lands on `sys.path` and the parent that *contains* the
  package is missing — it died before reading a single config value::

      $ python <repo>/hscc_daemon/verify_chat_roundtrip.py --json   # cwd=/private/tmp
      ModuleNotFoundError: No module named 'hscc_daemon'            # at line 49
      exit code 1

  The bootstrap now computes the root from `__file__` and runs *before* the import,
  the same shape proven for `api_route_sweep.py` in 2.15.2 (t_ef36fe71). `verify`
  tolerated the crash — `rc=1` fell back to the traceback text as the row detail,
  so the row was verbose rather than wrongly green — but the check it reports on
  never actually ran.
- A missing renderer can no longer take the human view down. The `cli_theme`
  import is now guarded: with no `rich` in the interpreter running us, the round
  trip still runs, `--json` is byte-identical (verify's path is renderer-proof),
  and the human view prints the same labels as plain text instead of raising
  `AttributeError: 'NoneType'` out of `render_human`. The exit code stays the round
  trip's own verdict — a missing colour scheme is never reported as a chat failure.

### Verified

- `hscc_daemon/tests/test_chat_roundtrip_standalone.py` (10 cases) runs the script
  by bare script path from a cwd outside the repo with a scrubbed HOME and no
  `hscc` on `PATH`, and asserts it never dies with `ModuleNotFoundError`. It
  asserts on the preconditions path (no API host/token → exit 2 + parseable error),
  so **no real chat prompt is ever fired at the live orchestrator**; a fake keg in
  `tmp_path` supplies the package, and the real `cli_theme.py` planted in it proves
  the Rich panel actually draws from a bare script path (delete the bootstrap and
  that case goes red).
- The repo was swept for the same ordering fault with an AST statement-order check
  (`import hscc_daemon` in a module-level statement before the first module-level
  `sys.path` call), and the check now *ships as a test*: any bare-runnable module
  that reintroduces bootstrap-after-import fails the suite with `file:line`. Only
  one module had the bug — this one. Three others import `hscc_daemon` from a
  bare-runnable file with no bootstrap at all and are accounted for, not silently
  refactored here: `api_route_sweep.py` (unguarded import, no bootstrap on `main` —
  its fix ships in PR #213 / t_ef36fe71 and is untouched by this card),
  `event_driven.py` (peer-guarded import with a `_FallbackTheme`, so a bare path
  renders unthemed instead of crashing) and `hscc.py` (launched with `PYTHONPATH`
  pointed at the package root by its launchd/systemd unit in
  `hscc_daemon/install.py` and by `hscc-bootstrap/restart_daemon.sh`, whose own
  comments record this exact `ModuleNotFoundError`).
