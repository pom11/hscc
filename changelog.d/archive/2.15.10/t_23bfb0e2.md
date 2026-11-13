kind: Added
task: t_23bfb0e2

- **`hscc cards monitor` now streams what the worker thought, not only what it
  called.** Each per-card panel read only `content` plus one `┊` line per tool
  call, while a worker's thinking sits in the session store's reasoning
  columns. Measured on one operator session store (2026-11-13, read-only): of
  5424 assistant rows, 5396 carry reasoning and only 2116 carry any reply text
  at all — the majority of turns rendered as a bare tool line, which was the
  whole point of the panel. A panel now prints a row's reasoning above that
  row's reply text in dim italic, through the same `clean_line` + wrapping the
  rest of the body uses, collapsed to one line per row and cut at
  `REASON_PREVIEW` (320 chars) with an explicit `…+N more` marker — a measured
  thought reaches 70 KB, and one thought must not scroll the transcript off a
  panel that is a dozen rows tall. `t` stays the tools-only view it always
  was: a thinking line never passes `is_tool_line`. `reasoning` is read, and
  `reasoning_content` only as its fallback, never both — on that store the two
  columns hold identical text for every reasoning-bearing row, so a concat or
  a naive `COALESCE` would print every thought twice. Whether the columns
  exist is probed with `PRAGMA table_info` once per db file and cached, so a
  store that predates them reads the same rows and renders as before instead
  of landing in the blind-read error panel. The transcript page stays ONE
  definition — only the SELECT list varies, never the display set — and the
  read is the same additive, read-only `sqlite3` path with no `hermes_state`
  import, so the brew-installed CLI keeps working with no wrapper.
