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
