kind: Changed
task: t_ff665ff9

- **`repl_inject` mode vocabulary is now `auto | plain | queue | steer` — the
  operator's naming contract, same as `~/bin/hermes-inject`.** `plain` is the
  canonical name for the no-verb send (what the reference tool called
  `interrupt`, an empty verb prefix); `queue` sends `/queue `, `steer` sends
  `/steer `, and `auto` picks `queue` when the REPL is busy and `plain` when it
  is idle, so a running goal is never cut off unless the caller asked for it.
  The legacy name `interrupt` is still accepted and normalises to `plain`; the
  receipt always reports the resolved mode. The verb table is public as
  `VERB_PREFIX` and the decision as `resolve_mode(mode, busy)`, so the next
  card's CLI wrapper cannot re-derive (and drift from) the prefix mapping.
  Chosen mode + busy state are one shared decision (`plan_pane`), used by both
  the dry-run path and the real delivery — there is exactly one place in the
  repo that turns a mode into a verb.
kind: Fixed
task: t_ff665ff9

- **A pane whose first Hermes process is a WRAPPER no longer resolves as the
  `default` profile** (the `~/bin/hermes-inject` 2026-10-29 fix, now ported).
  A REPL started through a launcher (e.g. `kronos-orch` via its launcher
  script) shows both the launcher and the real `hermes -p <profile> chat`
  child in `ps -t`. The old matcher took the FIRST chat-looking line and
  stopped, so the wrapper — which carries no `-p`/`--profile` — labelled the
  pane `default` and an inject by profile name refused on a pane that was in
  fact the right one. `hermes_panes()` now scans all of the pane's lines,
  prefers the chat line that names a profile, and falls back to `default` only
  when none does. Ambiguity is still fatal: two panes that resolve to the same
  profile refuse (`ambiguous`), wrapper-resolved or not.
kind: Added
task: t_ff665ff9

- **`repl_inject` can now answer "what would this do?" without doing it.**
  `plan(profile, mode=…, pane_override=…)` / `plan_pane(pane, mode)` resolve the
  pane, capture the screen once to read idle/busy, and return the decision —
  `{pane, target, profile, busy, mode, verb, interrupts}` — with no `send-keys`,
  no buffer and no Enter; `format_plan(plan)` renders the reference tool's line
  (`target: <profile> @ <target> (busy|idle) -> <mode>`, plus
  `(interrupts the running turn)` when an explicit `plain` was chosen while
  busy). `inject(…, dry_run=True)` / `deliver(…, dry_run=True)` return the same
  plan plus the message metadata and a `dry_run: True` receipt, and send
  nothing. This is the seam `hscc orch inject --dry-run` builds on, so the CLI
  stays a thin wrapper instead of re-implementing pane logic.
kind: Verified
order: 1

- `hscc-api/tests/test_repl_inject.py`: 26 -> **44** cases. New coverage:
  wrapper-pane resolution (profile-named child preferred over a profile-less
  wrapper line, order-independent, `default` kept when no line names a profile,
  a gateway line never becomes the target); the wrapper-pane **ambiguous** case
  (two panes resolving to one profile → `ambiguous`, nothing pasted, nothing
  typed); the full mode table (`auto` busy/idle, `plain`, `queue`, `steer`,
  legacy `interrupt` alias normalising to `plain`, unknown mode refuses) plus
  `plain`-while-busy marked `interrupts=True` with Enter as the only key
  pressed; dry-run sends nothing (no `load-buffer`, no `paste-buffer`, no
  `send-keys`) while still reporting pane/busy/verb — including one case that
  leaves `is_busy` unstubbed so the idle/busy a dry run prints is pinned to the
  captured screen; dry-run still refuses empty text, a bad mode and an
  ambiguous pane; `plan` refuses a bad mode before any pane work; `plan_pane`
  and `deliver` agree field-for-field on one decision; and `format_plan` output
  for busy-queue, idle-plain and plain-interrupts cases.
  All via the `run_tmux`/`run_ps` seams — the hermeticity guards still raise
  under pytest when a test forgets to stub them (both guard tests kept).
- `bash scripts/run_tests.sh` — one pytest process per plugin dir, all green
  (`hscc-api` leg includes the new tests above);
  `python3 scripts/address_guard.py --tracked` clean;
  `python3 scripts/changelog_fragments.py check` clean.
