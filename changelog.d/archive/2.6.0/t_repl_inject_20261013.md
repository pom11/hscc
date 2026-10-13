kind: Added
task: t_repl_inject_20261013

- **iPhone messages now type into the operator's REAL terminal REPL.** The
  API WebSocket write path is tmux-first: when the project's orchestrator
  REPL (`hermes -p <profile> chat`) is a pane in tmux, the send is delivered
  there via `repl_inject` — bracketed paste, so a multi-line message arrives
  as ONE message; `/queue` automatically when the REPL is mid-turn, so a
  running `/goal` is never interrupted; the reply comes back through the
  existing state.db store-tail (read direction), 1:1. The serve-PTY path
  remains ONLY as the fallback for "no tmux REPL exists". Every decision is
  visible in the transcript: the `working` frame carries `via=tmux` (with
  the pane and busy/queued receipt) or `via=serve`. If MULTIPLE panes run
  the project's REPL the send refuses honestly (`tmux_ambiguous`) instead of
  guessing or silently falling back — the same never-guess rule as the CLI
  tool. Pane resolution failures degrade to the serve path, never to a dead
  socket.
