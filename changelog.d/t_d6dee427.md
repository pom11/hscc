kind: Added
task: t_d6dee427

- **The main orchestrator now has its own tmux pane.** `hscc main start`
  ensures EXACTLY ONE pane runs `hermes -p <main profile> chat`, the command
  `hscc orch inject` resolves. An existing pane is reused with no tmux write;
  when none exists a window named after the profile is created in the
  operator's tmux session (current session when run inside tmux, else the
  attached/most-recent one), detached so the operator's current window is
  never switched. With no tmux server at all, it creates a detached session
  named after the profile. It refuses on ambiguity instead of guessing, and
  never kills or selects another pane/window.

- **`hscc main status [--json]` reports name, board, pane and busy/idle.**
  Pane discovery and the busy marker are `hscc-api/repl_inject`'s — the same
  matcher and `msg=interrupt` detection `hscc orch inject` uses — so the two
  surfaces cannot disagree. States are `busy`, `idle`, `not-running`,
  `ambiguous` (never a guessed pane) and `unknown` (the screen could not be
  read); the JSON receipt always carries `name`, `profile`, `board`, `state`,
  `running`, `busy`, `pane` and `target`.

- **Bootstrap calls `hscc main start` at the end.** It runs after the daemon
  stage unless `--dry-run`, `--skip-roles`, or no tmux server exists; the last
  case prints `hscc main start` as the one-line fix instead of starting a tmux
  server itself. The stage is non-fatal and reports the CLI's own receipt.

- **The retired `general-orch` expectation in the session resolver test is
  gone.** `test_resolve_general_sentinel_is_honest` now takes its expected
  profile/session from `main_orch` (the bootstrap-stored name, default
  `general`) and still asserts the honest no-session result.

---
kind: Verified
task: t_d6dee427

- `hscc_daemon/tests/test_main_cli.py` drives the real `start`/`status` code
  with a fake repl module and a recording fake tmux runner: reuse with zero
  tmux calls, window creation with `-d`/`-n <profile>`/`-p <profile> chat`,
  current-session preference, detached-session fallback, ambiguity refusal
  with no kill/select, stored-name resolution, missing Hermes/repl/tmux
  failures, and busy/idle/not-running/ambiguous/unknown status. CLI help is
  pinned inert, `status --json` is the machine contract, and the unified
  `hscc main` branch is dispatched. `hscc-bootstrap/tests/test_bootstrap_main_pane.py`
  runs the shipped bootstrap function verbatim: dry-run plans, `--skip-roles`
  skips, no tmux server prints the fix, and a real server invokes exactly
  `main start` through the shim (or the deployed module fallback).
