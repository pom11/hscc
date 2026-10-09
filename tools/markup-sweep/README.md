# flightdeck / daemon markup escape sweep — tooling (t_12f3c8a8)

Not shipped code. These are the scripts that performed and validated the
render-surface escape sweep on `hscc-project/flightdeck/commands/` (26 files)
and its predecessor on `hscc_daemon` (t_716cf37c). Kept so the sweep is
reproducible and re-runnable when new command modules land.

Run from the repo root with the Hermes venv python (needs `rich`):

    python tools/markup-sweep/check_sweep.py hscc-project/flightdeck/commands/

## check_sweep.py — the gate

AST walk over every Rich render call (`console.print`, `panel()`,
`status_panel()`, `table().add_row/add_column`, `Print(...).print`) and reports
every f-string interpolation of non-constant data that does not pass through an
escaper. Prints `CLEAN <file>` per file, `RESULT: ALL CLEAN` at the end, exit 1
otherwise. Rules that took the most reasoning, all in the file's comments:

- **Title arguments are skipped, at every nesting level.** `panel()`/`table()`
  escape their title centrally (`cli_theme._view_title` themed, `_theme.esc`
  fallback). Escaping a title at the call site DOUBLE-escapes it.
- **Helper calls are not whitelisted.** `_card_label()` returns raw card-title
  data; a name that looks formatted is not evidence. Only builtins and the two
  escapers are trusted.
- **`ALLOW = {}` deliberately.** Every entry there is a judgment call that a
  later reader cannot audit, so the remaining safe-looking values got wrapped
  instead of allow-listed.
- **`escape(x)` followed by `!r` is reported.** `repr()` escapes the backslash
  `escape()` inserted, re-opening the hole. `esc(repr(x))` is the safe form.

## apply_lines.py / preview.py — the applier

Line-targeted edits, `(relpath, lineno, old, new[, [(ctxline, must_contain)...]])`.
`preview.py` runs the same matcher without writing. Two safety properties, both
earned the hard way:

- Context anchors are checked against the file as it will be read, so a stale
  line number fails loudly instead of editing the wrong site.
- The candidate text is `ast.parse`d BEFORE it is written. An earlier run
  landed 8 files of `{esc(x!r)}` — a SyntaxError, because `!r` is only legal at
  the top of a replacement field — and the gate makes that class un-writable.

## probe_rich.py / flow_markup.py / ctx_markup.py / show_render_fns.py

- `probe_rich.py`: empirical Rich semantics (does `[/bold]` in a body raise? what
  does a title do? does `{escape(x)!r}` survive?). This is what proved finding #1.
- `flow_markup.py`: flow-classified site scan (154 sites) — the inventory.
- `ctx_markup.py` / `show_render_fns.py`: patch-ready context, because a
  name-based scan over-attributes across function scopes.

## Known limits

- The checker is textually conservative: it can flag an interpolation that is
  provably constant. Fix by hoisting to a named constant, not by adding to ALLOW.
- It cannot see a format-spec bug. `{esc(n):,}` passes the checker and raises at
  runtime — the pin test caught exactly this (`archive.py`).
