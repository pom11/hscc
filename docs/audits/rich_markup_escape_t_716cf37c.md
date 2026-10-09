# Rich markup injection in HSCC human views — audit + fix (t_716cf37c)

Origin: t_5abdb13d review round 1 (execution lens), review comment 991. Pre-existing,
codebase-wide. Recorded here as its own card.

## Failure class

Rich's `Console.print` / `Table.add_row` / `Panel(...)` parse a `str` renderable as
**markup**. Any operator-controlled or filesystem-derived string that contains a
closing tag (`[/bold]`) is parsed as a style close with no opener →
`rich.errors.MarkupError: closing tag '[/bold]' at position N doesn't match any open tag`
→ an unhandled traceback instead of the command's output.

`--json` paths are unaffected (plain `print`), exit codes unaffected. Only the human view crashes.

## Scope of the audit (base: origin/main fb15d97b)

Fix rule used everywhere: **escape the DATA, not the markup the renderer builds.**
`rich.markup.escape` is applied to interpolated values only; intentional inline
styles the renderer itself emits (`[ok]`, `[error]`, `[warn]`, `[dim]`, `[label]`,
`[title]`) stay live.

(sections appended as findings land)

## Reproduced (base 6af7418a, this worktree)

`scratch/probe_markup_t_716cf37c.py` (reviewer-profile scratch) drove each command
with a markup-bearing input. Before the fix, 7/7 touched surfaces raised
`rich.errors.MarkupError`; after, all print the value literally:

| surface | before | after |
|---|---|---|
| `cmd_log` (daemon line `[/bold]`) | MarkupError pos 19 | literal |
| `cmd_triggers` (rule id `[/bold]`) | MarkupError pos 1 | literal |
| `cmd_check --repo <path with [/bold]>` | MarkupError pos 92 (cli.py table) | literal |
| `cmd_status` (watchdog `reason`) | MarkupError pos 37 | literal |
| `cmd_check dgx` (state `message`) | MarkupError pos 22 | literal |
| `cmd_check dgx/all` (check exception text) | MarkupError pos 30 | literal |
| `cmd_stop` (OSError message) | latent (same class) | literal |

## Surfaces touched (escape the DATA, keep the renderer's markup)

- `hscc_daemon/cli_theme.py` — new `esc()` (single source; `rich.markup.escape`
  over `str(value)`), exported in `__all__`. Titles were already escaped
  (`_view_title`); cells/bodies were not.
- `hscc_daemon/cli.py` — `cmd_status` (liveness detail, stream rows, watchdog +
  trigger panels), `_cmd_check_repo` (every cell + the UNVERIFIED error + the
  next-step hint), `_cmd_check_impl` (state message, exception text, results
  table), `cmd_triggers` (panel body + rule rows), `cmd_stop`, `cmd_log`.
- `hscc_daemon/cluster_render.py` — `_as_lines` is now the choke point (raw
  sparkrun/ssh output escaped once), `_emit_error_panel`, and every
  `add_row`/panel body: cluster status/hosts/monitor/jobs/info, stop/down/up,
  profiles, template list/status/preview/validate/apply.
- `hscc_daemon/kanban_blocked.py`, `kanban_cli.py` — stale/blocked card tables
  (board, id, kind, age, why + comments, title) and the recover/archive panels.
- `hscc_daemon/api_route_sweep.py`, `api_cli.py` — sweep rows/notes/dynamic
  routes/failures; token-read error line.
- `hscc_daemon/hscc.py` — verify rows + next-step hints + unverified list,
  stats/throughput panel bodies, autoscale body, escalation table.
- `hscc_daemon/event_driven.py` — install/uninstall job results, kqueue log
  tail, dispatch error lines; `_FallbackTheme.esc` added so the standalone
  path keeps the contract.
- `hscc_daemon/autodown_cli.py` — every interpolated value in the status/
  enable/disable/wake panels (cron job names, reasons, blocked_by, state).
- `hscc_daemon/verify_chat_roundtrip.py` — job id, http, status, reply,
  token counts (a model reply quoting `[/bold]` crashed the console).
- `hscc_daemon/install.py` — install/uninstall/plist views: the generated
  plist/unit bodies, every `Plist|Unit installed|removed:` line, the
  enable-failed launchctl stderr and both copy-to path hints are HOME-derived
  DATA (the new `cmd_plist` pin caught a miss here mid-stamp — test-first
  doing its job).
- `hscc_daemon/event_driven.py` (second pass) — the periodic-install header
  prints `HSCC_DIR`/`STATE_DIR`/`PLIST_DIR`, all HOME-derived.

## Deliberately NOT changed

- `hscc-cluster/`, `hscc-roles/`, `hscc-bootstrap/` renderers: already
  `_theme.escape(...)` at their data sites (precedent the card cites);
  verified by grep, no unescaped dynamic site found in this pass.
- `hscc-project/flightdeck/commands/`: 135 render call sites; `ask.py` shows
  the house pattern (`panel(f"…{name}", escape(body))`), but a full sweep of
  it is its own card — see Follow-ups.
- `cmd_watch` / `cmd_start_daemon` / `cmd_ed_*`: daemon streaming and inert
  placeholders, per the module docstring's design rule (not restyled).
- Plain `print(..., file=sys.stderr)` error paths: no markup parsing there.

## Follow-ups (not done here; carded-or-cardable)

- flightdeck `make_console().print(panel(...))` sweep (~135 sites) — same
  class, different subproject; needs its own test pin per command.
  **Carded: t_12f3c8a8** (child of this card; carries the fixed decisions —
  reuse `_theme.escape`, escape DATA not renderer markup).
- `hscc_daemon/desktop.py` `emit_event` JSON consumers: out of scope (machine
  path).


---

## Appendix — the flightdeck sweep (t_12f3c8a8, child card)

The "Deliberately NOT changed" entry above read "135 render call sites"; the
flow-classified count was **154 sites across 19 files** (a name-based scan
over-attributes across function scopes, which is why the number moved). All of
them are now escaped, and the gate now walks **all 30 modules** in the
directory — 9 beyond the 19 the estimate counted have render calls of their own
(`hygiene`, `incident`, `ingest`, `metrics`, `qa`, `reconcile`, `standup`,
`update`, `why`), and all come back clean.

### What landed

- `flightdeck/commands/_theme.py` gains module-level `esc()` — the same
  contract as `cli_theme.esc` above, including `str(value)` coercion. It is a
  separate function rather than an import because the daemon package is not on
  flightdeck's import path (a standalone plugin install has no `hscc_daemon`).
- `panel()` / `status_panel()` / `table()` in `_theme` escape their **title**
  centrally (mirroring `_view_title`), so a call site must not pre-escape one —
  double-escaping shows the operator literal backslashes. The fallback theme
  uses `rich.markup.escape`, which escapes only the opening bracket, hence the
  two render paths are not byte-identical on a markup-bearing title; the pin
  asserts the two properties that matter on both (text survives, no
  MarkupError) rather than one shared byte string.
- Every module under `flightdeck/commands/` swept — the gate walks all 30.
  `--json` paths and exit codes untouched; daemon streaming lines stay
  un-styled (same design rule as above).

### Proving the gate is not blind there

Nine of those 30 modules (`hygiene`, `incident`, `ingest`, `metrics`, `qa`,
`reconcile`, `standup`, `update`, `why`) came back CLEAN but were never in the
estimate, so their CLEAN was unverified by construction. A checker that cannot
*see* a file reports it CLEAN too. Each was copied to scratch with a provably
non-constant interpolation injected into a real `panel()` body — a name the
checker cannot prove constant — and every one was flagged, alongside a synthetic
control file. Without that control, "30 files ALL CLEAN" would have been the
same class of false green as a local `docker ps`.

### Two findings worth keeping

1. **`{escape(x)!r}` is not safe** and predated this card (5 sites in
   `sync.py`, 1 in `start.py`). `escape()` emits `\[`; `repr()` then escapes
   that backslash to `\\[`, which Rich renders as a literal backslash followed
   by a *live* tag — the markup survives and still raises. Confirmed
   empirically, not reasoned. `{esc(x!r)}` is worse: it is a `SyntaxError`,
   because a conversion flag is only legal at the top of a replacement field.
   Safe form when a repr is genuinely wanted: `esc(repr(x))`.
2. **`repr` is not an escaper.** `repr("a[/bold]b")` keeps the brackets. An
   early version of the checker whitelisted it and reported a clean tree that
   was not clean — the same class of false green as a local `docker ps`.

### What the pin caught that the checker could not

`archive.py` briefly read `{esc(result.bytes_written):,}`. The value is escaped
so the AST gate passes, but `esc()` returns `str` and `{str:,}` is an illegal
format spec — a runtime `ValueError` on the happy path. Only running the pin
found it; the fix formats the number into a named local, then escapes once.
A checker that proves "the value passed an escaper" cannot see a format spec.

### Gate

`tools/markup-sweep/check_sweep.py` (side branch `tooling/t_12f3c8a8`) is the
AST gate over every render call in the directory: it reports any non-constant
interpolation that does not pass an escaper, skips centrally-escaped title
arguments at every nesting level, does not whitelist helper functions
(`_card_label` returns raw card-title data — a name that sounds formatted is
not evidence), carries no `ALLOW` entries, and reports the `escape`-then-`!r`
shape. `RESULT: ALL CLEAN` over all 30 modules at the stamped tip.
